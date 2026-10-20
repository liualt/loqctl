# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
import copy
import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from loqctl import api as m_api, backend as m_backend, cli as m_cli, common as m_common
from loqctl import curvestore as m_store, fancurve as m_fan, hardware as m_hardware, sensors as m_sensors, ui as m_ui
from tests import test_loqctl as base

CURVE = [
    dict(cpu_up=40, cpu_down=0, gpu_up=40, gpu_down=0, ic_up=40, ic_down=0, fan1=0, fan2=0, accel=1, decel=1),
    dict(cpu_up=60, cpu_down=35, gpu_up=60, gpu_down=35, ic_up=60, ic_down=35, fan1=80, fan2=80, accel=1, decel=1),
    dict(cpu_up=90, cpu_down=55, gpu_up=90, gpu_down=55, ic_up=90, ic_down=55, fan1=180, fan2=180, accel=1, decel=1),
]


def make_hwmon(root, curve=CURVE, unit="percent", name="legion_hwmon"):
    hwmon = Path(root) / "class/hwmon/hwmon5"
    (hwmon / "device").mkdir(parents=True)
    (hwmon / "name").write_text(name)
    if unit:
        (hwmon / "device/fancurve_speed_unit").write_text(unit)
    (hwmon / "fan1_input").write_text("2100")
    (hwmon / "fan1_target").write_text("0")
    for n, point in enumerate(curve, 1):
        for field, value in point.items():
            (hwmon / m_fan.FILE_MAP[field].format(n=n)).write_text(str(value))
    return hwmon


def edit(curve, changes):
    """Copy of curve with {(point, field): value} changes."""
    out = copy.deepcopy(curve)
    for (i, field), value in changes.items():
        out[i][field] = value
    return out


class FanCurveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hwmon = make_hwmon(self.root)

    def fields(self, errors):
        return {(i, f) for i, f, _ in errors}

    def test_discovery(self):
        self.assertEqual(m_fan.find_legion_hwmon(self.root), self.hwmon)
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertIsNone(m_fan.find_legion_hwmon(empty.name))
        (self.hwmon / "name").write_text("k10temp")
        self.assertIsNone(m_fan.find_legion_hwmon(self.root))

    def test_read_curve_with_missing_files(self):
        (self.hwmon / "pwm3_auto_point2_temp").unlink()
        (self.hwmon / "pwm1_auto_point1_accel").unlink()
        points = m_fan.read_curve(self.hwmon)
        self.assertEqual(len(points), 3)
        self.assertIsNone(points[1]["ic_up"])
        self.assertIsNone(points[0]["accel"])
        self.assertEqual(points[2]["fan2"], 180)
        self.assertEqual(m_fan.speed_unit(self.hwmon), "percent")

    def test_validate_ok_and_each_rule(self):
        self.assertEqual(m_fan.validate_curve(copy.deepcopy(CURVE)), [])
        cases = {
            "temps": (edit(CURVE, {(1, "cpu_up"): 30}), (1, "cpu_up")),
            "speeds": (edit(CURVE, {(2, "fan1"): 70}), (2, "fan1")),
            "lower_vs_upper": (edit(CURVE, {(1, "gpu_down"): 60}), (1, "gpu_down")),
            "lower_vs_previous": (edit(CURVE, {(1, "ic_down"): 40}), (1, "ic_down")),
            "range_temp": (edit(CURVE, {(2, "cpu_up"): 128}), (2, "cpu_up")),
            "range_pwm": (edit(CURVE, {(2, "fan2"): 256}), (2, "fan2")),
            "type": (edit(CURVE, {(0, "cpu_up"): "40"}), (0, "cpu_up")),
            "bool": (edit(CURVE, {(0, "fan1"): True}), (0, "fan1")),
            "last_point": (edit(CURVE, {(2, "fan1"): 140}), (2, "fan1")),
        }
        for name, (curve, cell) in cases.items():
            errors = m_fan.validate_curve(curve)
            self.assertTrue(errors, name)
            if cell:
                self.assertIn(cell, self.fields(errors), name)

    def test_hot_rule_needs_40_percent_after_hot_point(self):
        hot = edit(CURVE, {(0, "cpu_up"): 80, (1, "cpu_up"): 85, (2, "cpu_up"): 90,
                             (1, "fan1"): 0, (0, "fan1"): 0, (1, "fan2"): 0, (2, "fan1"): 160, (2, "fan2"): 160,
                             (0, "cpu_down"): 0, (1, "cpu_down"): 35, (2, "cpu_down"): 55})
        self.assertEqual(self.fields(m_fan.validate_curve(hot)), {(1, "fan1"), (1, "fan2")})
        hot[1]["fan1"] = hot[1]["fan2"] = 102
        self.assertEqual(m_fan.validate_curve(hot), [])
        hot[1]["fan1"] = 101
        self.assertEqual(self.fields(m_fan.validate_curve(hot)), {(1, "fan1")})

    def test_write_only_changed_files_in_order(self):
        writes = []
        real = m_fan._write_file
        with patch.object(m_fan, "_write_file", side_effect=lambda p, v: (writes.append(p.name), real(p, v))):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90, (1, "cpu_up"): 61,
                                                                    (2, "gpu_up"): 91}), "percent")
        self.assertEqual(writes, ["pwm1_auto_point2_temp", "pwm2_auto_point3_temp", "pwm1_auto_point2_pwm"])
        self.assertEqual({r["status"] for r in results}, {"APPLIED"})
        self.assertEqual(m_fan.read_curve(self.hwmon)[1]["fan1"], 90)
        self.assertEqual((self.hwmon / "fan1_target").read_text(), "0")
        self.assertEqual(m_fan.write_curve(self.hwmon, m_fan.read_curve(self.hwmon), "percent"), [])

    def test_never_touches_fan_target_or_other_files(self):
        self.assertTrue(all("target" not in p and "points_size" not in p for p in m_fan.FILE_MAP.values()))
        with patch.object(m_fan.os, "open", wraps=os.open) as opened:
            m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan2"): 100}), "percent")
        self.assertEqual([Path(c.args[0]).name for c in opened.call_args_list], ["pwm2_auto_point2_pwm"])

    def test_invalid_target_refused_with_no_writes(self):
        with patch.object(m_fan, "_write_file") as write:
            with self.assertRaises(m_common.ControlError):
                m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}), "percent")
            with self.assertRaises(m_common.ControlError):
                m_fan.write_curve(self.hwmon, CURVE[:2], "percent")
            with self.assertRaises(m_common.ControlError):
                m_fan.write_curve(self.hwmon, edit(CURVE, {(0, "bogus"): 1}), "percent")
        write.assert_not_called()

    def test_symlink_and_foreign_path_rejected(self):
        outside = self.root / "outside"
        outside.write_text("1")
        link = self.hwmon / "pwm1_auto_point2_pwm"
        link.unlink()
        link.symlink_to(outside)
        results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90}), "percent")
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertEqual(outside.read_text(), "1")
        with self.assertRaises(m_common.ControlError):
            m_fan._path(self.hwmon, "fan1_target", 1)

    def test_readback_mismatch_and_level_rounding(self):
        with patch.object(m_fan, "_write_file"):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90}), "percent")
        self.assertEqual((results[0]["status"], results[0]["current"]), ("FAILED", 80))
        self.assertIn("firmware reports", results[0]["error"])

        def rounding(path, value):  # level unit: pwm snaps to level * 255 / 10
            path.write_text(str(round(round(value * 10 / 255) * 255 / 10)))
        with patch.object(m_fan, "_write_file", side_effect=rounding):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 100}), "level")
        self.assertEqual(results[0]["status"], "APPLIED")
        self.assertEqual(m_fan.pct_to_pwm(46, "level"), 128)
        self.assertEqual(m_fan.pwm_to_pct(128, "level"), 50)

    def test_stops_at_first_failure(self):
        calls = []

        def failing(path, value):
            calls.append(path.name)
            raise m_common.ControlError("boom")
        with patch.object(m_fan, "_write_file", side_effect=failing):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "cpu_up"): 61, (1, "fan1"): 90}), "percent")
        self.assertEqual([r["status"] for r in results], ["FAILED", "NOT SENT"])
        self.assertEqual(len(calls), 1)

    def test_errno_messages(self):
        path = self.hwmon / "pwm1_auto_point2_pwm"
        for code, text in ((errno.EPERM, "refuses unvalidated"), (errno.EBUSY, "custom"),
                           (errno.EOPNOTSUPP, "does not support")):
            with patch.object(m_fan.os, "open", side_effect=OSError(code, "x")):
                with self.assertRaisesRegex(m_common.ControlError, text):
                    m_fan._write_file(path, 1)
        with self.assertRaises(m_common.ControlError):
            m_fan._write_file(self.hwmon / "missing", 1)  # never created
        self.assertFalse((self.hwmon / "missing").exists())


class FanCurveSurfaceTests(unittest.TestCase):
    setUp, tearDown, dashboard = base.FirmwareTests.setUp, base.FirmwareTests.tearDown, base.FirmwareTests.dashboard

    def setUp(self):
        base.FirmwareTests.setUp(self)
        self.hwmon = make_hwmon(self.root)

    def set_mode(self, mode):
        self.hw.set_mode(mode)

    def test_hardware_fancurve_and_gate(self):
        data = self.hw.fancurve()
        self.assertTrue(data["available"])
        self.assertEqual((data["unit"], data["rpm"][0], data["rpm"][1]), ("percent", 2100.0, None))
        self.assertEqual(len(data["points"]), 3)
        self.set_mode("balanced")
        with self.assertRaisesRegex(m_common.ControlError, "locked in balanced mode"):
            self.hw.write_fancurve(CURVE)
        self.assertFalse(m_hardware.Hardware(self.root / "nowhere").fancurve()["available"])

    def test_internal_cli_prints_results(self):
        out = Mock()
        payload = json.dumps(edit(CURVE, {(1, "fan1"): 90}))
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("builtins.print", out):
            self.assertEqual(m_cli.main(["_fancurve", payload]), 0)
        self.assertEqual(json.loads(out.call_args.args[0])[0]["status"], "APPLIED")
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("sys.stderr"):
            self.assertEqual(m_cli.main(["_fancurve", json.dumps(CURVE[:1])]), 1)

    def test_fan_cli_json(self):
        out = Mock()
        backend = m_backend.Backend(str(self.root / "none.sock"))
        for root, expected in ((self.root, True), (self.root / "nowhere", False)):
            backend.hardware = m_hardware.Hardware(root)
            with patch.object(m_cli, "Backend", return_value=backend), patch("builtins.print", out):
                self.assertEqual(m_cli.main(["fan"]), 0)
            self.assertEqual(json.loads(out.call_args.args[0])["available"], expected)
        self.assertIn("reason", json.loads(out.call_args.args[0]))

    def test_api_get_and_post(self):
        sampler = type("Sampler", (), {"latest": {}})()
        path = str(self.root / "t.sock")
        server = m_api.APIServer(path, self.hw, sampler, os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(len(m_api.request(path, "GET", "/v1/fancurve")["points"]), 3)
            results = m_api.request(path, "POST", "/v1/fancurve", {"points": edit(CURVE, {(1, "fan1"): 90})})
            self.assertEqual(results[0]["status"], "APPLIED")
            for payload in ({"points": edit(CURVE, {(1, "cpu_up"): 30})}, {"points": [], "x": 1}):
                with self.assertRaises(m_common.ControlError):
                    m_api.request(path, "POST", "/v1/fancurve", payload)
            self.set_mode("balanced")
            with self.assertRaisesRegex(m_common.ControlError, "locked"):
                m_api.request(path, "POST", "/v1/fancurve", {"points": CURVE})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def dash(self, mode="custom"):
        dash = self.dashboard()
        dash.notice = ""
        dash.panel = "main"
        dash.show_limits = dash.show_results = dash.help = False
        dash.view = "summary"
        dash.running = True
        dash.keyboard = Mock()
        dash.backend.fancurve = self.hw.fancurve
        dash.backend.write_fancurve = self.hw.write_fancurve
        dash.theme = m_ui.Theme(False)
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (30, 110)
        dash.puts = []
        dash.put = lambda y, x, text, *a, **k: dash.puts.append(str(text))
        dash.box = Mock()
        dash.fan_init()
        self.set_mode(mode)
        return dash

    def keys(self, dash, state, *keys):
        for key in keys:
            dash.handle(ord(key) if isinstance(key, str) else key, state)

    def test_locked_notice_in_balanced(self):
        dash = self.dash("balanced")
        state = {"mode": "balanced"}
        self.keys(dash, state, "f", "4", "5", 10)
        self.assertEqual(dash.panel, "fans")
        self.assertIn("Fan curves are locked in balanced mode", dash.notice)
        self.assertEqual(dash.fan_pending, {})
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["cpu_up"], 40)

    def test_typing_and_enter_applies_via_backend(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "4", "5")
        self.assertEqual(dash.fan_pending, {(0, "cpu_up"): 45})
        self.keys(dash, state, 10)
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["cpu_up"], 45)
        self.assertEqual(dash.fan_pending, {})
        self.assertIn("Applied and verified 1", dash.notice)

    def test_speed_typed_in_percent_and_step(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", 258, 258, 258, 258, 258, 258, "5", "0")
        self.assertEqual(dash.fan_cell, [6, 0])
        self.assertEqual(dash.fan_pending, {(0, "fan1"): 128})
        self.keys(dash, state, 27, "+")
        self.assertEqual(dash.fan_pending, {(0, "fan1"): 13})  # 0 % + 5 % = pwm 13

    def test_invalid_jump_and_discard(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "9", "9", 10)
        self.assertEqual(dash.fan_cell, [0, 1])  # point 2 cpu_up now drops below point 1
        self.assertIn("must not drop", dash.notice)
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["cpu_up"], 40)
        self.keys(dash, state, "r")
        self.assertEqual((dash.fan_pending, dash.fan_fields), ({}, {}))
        self.keys(dash, state, "9", "9", "9")  # 999 is out of range for a temperature
        self.assertEqual(dash.fan_invalid, {(1, "cpu_up")})
        self.keys(dash, state, 10)
        self.assertEqual(dash.notice, "Fix the value marked red before applying.")

    def test_sudo_path_for_non_root_local(self):
        dash = self.dash()
        dash.backend.remote = False
        dash.backend.refresh = lambda: None
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "4", "5")
        with patch.object(os, "geteuid", return_value=1000), \
                patch.object(m_ui.Dashboard, "run_sudo_fancurve", return_value=[{"status": "APPLIED"}]) as run:
            self.keys(dash, state, 10)
        self.assertEqual(run.call_args.args[0][0]["cpu_up"], 45)
        with patch.object(m_ui, "sudo_run", side_effect=m_common.SudoExpired("x")):
            with self.assertRaisesRegex(m_common.ControlError, "expired"):
                dash.sudo_call(["_fancurve", "[]"])
        self.assertTrue(dash.sudo_expired)

    def test_panel_draws_without_module(self):
        dash = self.dash()
        self.hw.root = self.root / "none"
        dash.fan = {"available": False, "reason": "legion_laptop hwmon not found"}
        dash.draw_fans({"mode": "custom"}, {"fans": [{"rpm": 1800.0}, {"rpm": None}]}, 1, 0, 25, 100)
        text = "\n".join(dash.puts)
        self.assertIn("LenovoLegionLinux legion_laptop", text)
        self.assertIn("F1 1800 RPM", dash.box.call_args.args[4])
        self.assertIn("F2 N/A", dash.box.call_args.args[4])
        self.assertIn("enable_platformprofile=0", text)
        dash.draw_fans({"mode": "custom"}, {}, 1, 0, 25, 100)

    def test_panel_draws_with_module(self):
        dash = self.dash()
        dash.fan_load()
        dash.fan_set_field((1, "fan1"), "50")
        dash.fan_set_field((2, "cpu_up"), "999")
        dash.draw_fans({"mode": "custom"}, {"fans": [{"rpm": 1800.0}]}, 1, 0, 25, 100)
        text = "\n".join(dash.puts)
        self.assertIn("CPU up", text)
        self.assertIn("accel", text)
        self.assertIn("F1 1800 RPM", dash.box.call_args_list[0].args[4])
        dash.draw_fans({"mode": "balanced"}, {}, 1, 0, 17, 72)
        self.assertIn("locked in balanced mode", "\n".join(dash.puts))


class CurveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)

    def test_config_dir_respects_xdg_and_serve_uid_home(self):
        self.assertEqual(m_store.config_dir(), self.config / "loqctl")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(m_store.config_dir(), Path.home() / ".config/loqctl")
        home = Mock(pw_dir="/home/other")
        with patch.object(m_store.pwd, "getpwuid", return_value=home):
            self.assertEqual(m_store.config_dir(os.getuid() + 1), Path("/home/other/.config/loqctl"))
        self.assertEqual(m_store.config_dir(os.getuid()), self.config / "loqctl")

    def test_presets_scale_and_clamp_to_minimums(self):
        quiet = m_store.preset_curve(CURVE, 0.8)
        self.assertEqual([p["fan1"] for p in quiet], [0, 64, 153])  # last point raised to 60 %
        cool = m_store.preset_curve(CURVE, 1.2)
        self.assertEqual([p["fan2"] for p in cool], [0, 96, 216])
        loud = m_store.preset_curve(edit(CURVE, {(2, "fan1"): 250}), 1.2)
        self.assertEqual(loud[2]["fan1"], 255)
        hot = edit(CURVE, {(0, "cpu_up"): 85, (1, "cpu_up"): 86, (2, "cpu_up"): 90})
        self.assertEqual(m_store.preset_curve(hot, 0.5)[1]["fan1"], 102)
        for curve in (quiet, cool, m_store.preset_curve(CURVE, 1.0)):
            self.assertEqual(m_fan.validate_curve(curve), [])
        self.assertEqual(m_fan.validate_curve(m_store.preset_curve(CURVE, 0.2)), [])
        level = m_store.preset_curve(CURVE, 0.8, "level")
        self.assertEqual([p["fan1"] for p in level], [0, 76, 153])

    def test_atomic_write_mode_and_round_trip(self):
        path = m_store.save_active(CURVE)
        self.assertEqual(path, self.config / "loqctl/active.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob(".*tmp")), [])
        points = m_store.read_curve_file(path, os.getuid())
        self.assertNotIn("accel", points[0])
        self.assertEqual(points[1]["fan1"], 80)
        with patch.object(m_store.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                m_store.save_active(edit(CURVE, {(1, "fan1"): 90}))
        self.assertEqual(m_store.read_curve_file(path)[1]["fan1"], 80)
        self.assertEqual(list(path.parent.glob(".*tmp")), [])

    def test_saved_curves_listed_with_invalid(self):
        current = m_fan.read_curve(make_hwmon(self.root))
        m_store.write_atomic(self.config / "loqctl/curves/b-good.json", edit(CURVE, {(1, "fan1"): 90}))
        m_store.write_atomic(self.config / "loqctl/curves/a-bad.json", edit(CURVE, {(1, "cpu_up"): 30}))
        (self.config / "loqctl/curves/c-junk.json").write_text("{")
        saved = m_store.saved_curves(current)
        self.assertEqual([n for n, _ in saved], ["a-bad", "b-good", "c-junk"])
        self.assertEqual([p is None for _, p in saved], [True, False, True])
        names = [n for n, _ in m_store.preset_options(current, current)]
        self.assertEqual(names[:3], ["firmware", "quiet", "cool"])

    def test_untrusted_files_rejected(self):
        path = m_store.save_active(CURVE)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path, os.getuid() + 1)
        path.chmod(0o666)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path, os.getuid())
        link = path.with_name("link.json")
        link.symlink_to(path)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(link)


class ReapplyTests(unittest.TestCase):
    def setUp(self):
        base.FirmwareTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.hwmon = make_hwmon(self.root)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        m_store.save_active(edit(CURVE, {(1, "fan1"): 90}))
        self.log = []
        self.reapply = m_store.Reapplier(self.hw, os.getuid(), self.log.append)
        self.clock = {"boot": 1000.0, "mono": 500.0}
        patcher = patch.object(m_store.time, "clock_gettime",
                               side_effect=lambda c: self.clock["boot" if c == m_store.time.CLOCK_BOOTTIME else "mono"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def fan1(self):
        return m_fan.read_curve(self.hwmon)[1]["fan1"]

    def tick(self, mode):
        self.reapply({"mode": mode})

    def test_mode_change_into_custom_writes_once(self):
        self.tick("balanced")
        self.assertEqual(self.fan1(), 80)
        self.tick("custom")
        self.assertEqual(self.fan1(), 90)
        self.assertIn("re-applied", self.log[-1])
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.tick("custom")  # no change of mode: no write
        self.assertEqual(self.fan1(), 80)
        self.assertEqual(len(self.log), 1)

    def test_other_modes_never_write(self):
        self.tick("custom")
        self.hw.set_mode("balanced")
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.tick("balanced")
        self.tick("performance")
        self.assertEqual(self.fan1(), 80)

    def test_resume_detection(self):
        self.tick("custom")
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.clock["boot"] += 5.0
        self.clock["mono"] += 5.0
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.clock["boot"] += 3600.0 + 5
        self.clock["mono"] += 5.0
        self.tick("custom")
        self.assertEqual(self.fan1(), 90)
        self.assertIn("after resume", self.log[-1])

    def test_untrusted_or_invalid_file_skipped_with_log(self):
        path = self.config / "loqctl/active.json"
        path.chmod(0o666)
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        path.chmod(0o600)
        other = m_store.Reapplier(self.hw, os.getuid() + 1, self.log.append)
        with patch.object(m_store, "config_dir", return_value=path.parent):
            other({"mode": "custom"})
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        m_store.save_active(edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}))
        self.tick("balanced")
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertEqual(len(self.log), 3)
        m_store.save_active(CURVE[:2])  # wrong size
        self.tick("balanced")
        self.tick("custom")
        self.assertIn("skipped", self.log[-1])

    def test_missing_file_is_silent_and_sampler_hook_survives_errors(self):
        (self.config / "loqctl/active.json").unlink()
        self.tick("custom")
        self.assertEqual(self.log, [])
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = Mock(side_effect=RuntimeError("boom"))
        with patch.object(sampler.sensors, "sample", return_value={}), patch("builtins.print"):
            sampler.tick()
        sampler.on_firmware.assert_called_once()
        self.assertEqual(sampler.latest["mode"], "custom")


class FanPanelPresetTests(FanCurveSurfaceTests):
    def test_preset_cycle_stage_and_save_keys(self):
        config = self.root / "config"
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(config)}):
            dash = self.dash()
            state = {"mode": "custom"}
            self.keys(dash, state, "f", "p")
            self.assertEqual(dash.fan_preset, 0)  # firmware snapshot equals the live curve
            self.assertEqual(dash.fan_pending, {})
            self.keys(dash, state, "p")
            self.assertEqual(dash.fan_pending[(2, "fan1")], 153)
            self.keys(dash, state, 10)
            self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 153)
            self.assertEqual(m_store.read_curve_file(config / "loqctl/active.json")[2]["fan1"], 153)
            self.assertEqual(dash.fan_base[2]["fan1"], 180)  # snapshot from the first open
            self.keys(dash, state, "W")
            self.assertTrue(dash.notice.startswith("Saved "))
            self.assertEqual(len(list((config / "loqctl/curves").glob("saved-*.json"))), 1)
            dash.draw_fans(state, {}, 1, 0, 25, 100)
            self.assertTrue(any(t == "firmware" for t in dash.puts))
            m_store.write_atomic(config / "loqctl/curves/zz.json", edit(CURVE, {(0, "cpu_up"): 99}))
            self.keys(dash, state, "r", "p", "p", "p", "p", "p")
            self.assertEqual(dash.fan_pending, {})
            self.assertIn("invalid and cannot be staged", dash.notice)
            dash.draw_fans(state, {}, 1, 0, 25, 100)
            self.assertTrue(any(t == "zz (invalid)" for t in dash.puts))

    def test_locked_mode_blocks_presets(self):
        dash = self.dash("balanced")
        self.keys(dash, {"mode": "balanced"}, "f", "p")
        self.assertEqual(dash.fan_pending, {})
        self.assertIn("locked in balanced mode", dash.notice)
