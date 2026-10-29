# SPDX-License-Identifier: GPL-3.0-or-later
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import profiles
from loqctl.common import ControlError


class FakeBackend:
    remote = True

    def __init__(self, hw):
        self.hw, self.curve, self.writes = hw, {"available": False, "reason": "no hwmon"}, []

    def controls(self):
        return self.hw.capabilities()

    def fancurve(self):
        return self.curve

    def write(self, key, value):
        self.writes.append((key, value))
        return self.hw.set_mode(value) if key == "mode" else self.hw.set_value(key, value)

    def write_fancurve(self, points):
        self.writes.append(("fancurve", points))
        return [{"status": "APPLIED"}]


class ProfileTests(helpers.SysfsCase):
    def setUp(self):
        super().setUp()
        self.config = helpers.make_root(self)
        patcher = patch.dict("os.environ", {"XDG_CONFIG_HOME": str(self.config)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = FakeBackend(self.hw)

    def put(self, name, data):
        path = profiles.profile_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def test_save_then_list_and_apply_round_trip(self):
        profiles.save_profile(self.backend, "gaming")
        saved = json.loads(profiles.profile_path("gaming").read_text())
        self.assertEqual(saved, {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 50}})
        self.assertEqual(profiles.list_profiles(), [{"name": "gaming", "mode": "custom", "limits": 1, "fancurve": False}])
        self.put("gaming", {**saved, "limits": {"ppt_pl1_spl": 70}})
        report = profiles.apply_profile(self.backend, "gaming")
        self.assertTrue(report["applied"])
        self.assertEqual(self.backend.writes, [("ppt_pl1_spl", 70)])
        self.assertEqual((self.attr / "current_value").read_text().strip(), "70")

    def test_apply_switches_mode_before_limits(self):
        (self.root / "class/platform-profile/platform-profile-0/profile").write_text("balanced")
        self.put("p", {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60}})
        self.assertTrue(profiles.apply_profile(self.backend, "p")["applied"])
        self.assertEqual([k for k, _ in self.backend.writes], ["mode", "ppt_pl1_spl"])

    def test_bad_profiles_are_refused_before_any_write(self):
        cases = [{"version": 1, "mode": "custom", "limits": {"dgpu_enable": 1}},
                 {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60}, "extra": 1},
                 {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": True}},
                 {"version": 1, "mode": "balanced", "limits": {"ppt_pl1_spl": 60}},
                 {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 500}},
                 {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 53}},
                 {"version": 1, "mode": "turbo"},
                 {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60}, "fancurve": [{"fan1": 5}]}]
        for data in cases:
            self.put("bad", data)
            with self.subTest(data=data), self.assertRaises(ControlError):
                profiles.apply_profile(self.backend, "bad")
        self.assertEqual(self.backend.writes, [])

    def test_failed_limit_is_reported_and_stops_the_curve(self):
        self.put("p", {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60},
                       "fancurve": [{"fan1": 255}]})
        self.backend.curve = {"available": True, "points": [{"fan1": 200}], "unit": None, "max_rpm": None}
        with patch.object(self.hw, "_write", side_effect=ControlError("denied")):
            report = profiles.apply_profile(self.backend, "p")
        self.assertFalse(report["applied"])
        self.assertEqual(report["results"][0]["status"], "FAILED")
        self.assertEqual([k for k, _ in self.backend.writes], ["ppt_pl1_spl"])

    def test_fan_curve_stage_applies_after_limits(self):
        self.put("p", {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60},
                       "fancurve": [{"fan1": 255}]})
        self.backend.curve = {"available": True, "points": [{"fan1": 200}], "unit": None, "max_rpm": None}
        report = profiles.apply_profile(self.backend, "p")
        self.assertTrue(report["applied"])
        self.assertEqual(self.backend.writes, [("ppt_pl1_spl", 60), ("fancurve", [{"fan1": 255}])])

    def test_stage_error_keeps_earlier_results_and_previous_mode(self):
        (self.root / "class/platform-profile/platform-profile-0/profile").write_text("balanced")
        self.put("p", {"version": 1, "mode": "custom", "limits": {"ppt_pl1_spl": 60}})
        real = self.backend.write

        def write(key, value):
            if key == "ppt_pl1_spl":
                raise ControlError("denied")
            return real(key, value)

        self.backend.write = write
        report = profiles.apply_profile(self.backend, "p")
        self.assertFalse(report["applied"])
        self.assertEqual(report["previous_mode"], "balanced")
        self.assertEqual([(r["key"], r["status"]) for r in report["results"]],
                         [("mode", "APPLIED"), ("ppt_pl1_spl", "FAILED")])

    def test_profile_name_is_restricted(self):
        for name in ("../x", ".hidden", "a/b", ""):
            with self.subTest(name=name), self.assertRaises(ControlError):
                profiles.profile_path(name)


if __name__ == "__main__":
    unittest.main()
