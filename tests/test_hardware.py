# SPDX-License-Identifier: GPL-3.0-or-later
import errno
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import common as m_common
from loqctl import hardware as m_hardware
from loqctl import ui as m_ui


class HardwareTests(helpers.SysfsCase):
    def test_boundaries_and_step(self):
        for value in (50, 100, 65):
            self.assertTrue(self.hw.set_value("ppt_pl1_spl", value)["verified"])
        for value in (49, 101, 51, True, 55.0, "55", None):
            with self.assertRaises(m_common.ControlError):
                self.hw.set_value("ppt_pl1_spl", value)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "65")
        (self.attr / "scalar_increment").write_text("0")
        self.hw.set_value("ppt_pl1_spl", 53)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "53")

    def test_capabilities_read_limits_once_but_writes_check_fresh(self):
        real_read = Path.read_text
        reads = []
        (self.attr / "display_name").write_text("Sustained power")

        def counting(path, *args, **kwargs):
            reads.append(path.name)
            return real_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", counting):
            self.hw.capabilities()
            reads.clear()
            item = self.hw.capabilities()["attributes"]["ppt_pl1_spl"]
        self.assertNotIn("max_value", reads)
        self.assertEqual((item["current_value"], item["max_value"]), (50, 100))
        (self.attr / "max_value").write_text("60")
        with self.assertRaises(m_common.ControlError):
            self.hw.set_value("ppt_pl1_spl", 70)
        self.assertEqual(self.hw.capabilities()["attributes"]["ppt_pl1_spl"]["max_value"], 60)

    def test_incomplete_limits_are_read_again(self):
        (self.attr / "max_value").unlink()
        (self.attr / "default_value").unlink()
        self.assertFalse(self.hw.capabilities()["attributes"]["ppt_pl1_spl"]["available"])
        (self.attr / "max_value").write_text("100")
        self.assertIsNone(self.hw.capabilities()["attributes"]["ppt_pl1_spl"]["default_value"])
        (self.attr / "default_value").write_text("60")
        (self.attr / "display_name").write_text("Sustained power")
        item = self.hw.capabilities()["attributes"]["ppt_pl1_spl"]
        self.assertEqual((item["available"], item["default_value"], item["description"]),
                         (True, 60, "Sustained power"))

    def test_mode_change_rereads_limits(self):
        (self.attr / "display_name").write_text("Sustained power")
        self.hw.capabilities()
        self.assertIn("ppt_pl1_spl", self.hw._static)
        (self.attr / "max_value").write_text("80")
        self.hw.set_mode("balanced")
        self.assertEqual(self.hw.capabilities()["attributes"]["ppt_pl1_spl"]["max_value"], 80)
        (self.attr / "max_value").write_text("90")
        (self.root / "class/platform-profile/platform-profile-0/profile").write_text("performance")
        self.assertEqual(self.hw.capabilities()["attributes"]["ppt_pl1_spl"]["max_value"], 90)
        shutil.rmtree(self.attr)
        self.assertNotIn("ppt_pl1_spl", self.hw.capabilities()["attributes"])
        self.assertNotIn("ppt_pl1_spl", self.hw._static)

    def test_mode_gates_custom_writes(self):
        self.hw.set_mode("balanced")
        with self.assertRaisesRegex(m_common.ControlError, "custom"):
            self.hw.set_value("ppt_pl1_spl", 60)
        self.assertEqual((self.attr / "current_value").read_text(), "50")
        with self.assertRaisesRegex(m_common.ControlError, r"'turbo'\. Choose one of .*balanced"):
            self.hw.set_mode("turbo")

    def test_set_mode_rejects_non_str_and_empty_choices(self):
        for bad in (None, 5):
            with self.assertRaisesRegex(m_common.ControlError, "Choose one of"):
                self.hw.set_mode(bad)
        self.hw.choices.write_text("")
        with self.assertRaisesRegex(m_common.ControlError, "No platform modes"):
            self.hw.set_mode("balanced")

    def test_allowlist_blocks_traversal_and_unknowns(self):
        for key in ("../ppt_pl1_spl", "ppt_pl1_spl/current_value", "gpu_mode", "/etc/passwd"):
            with self.assertRaises(m_common.ControlError):
                self.hw.set_value(key, 60)

    def test_missing_limits_never_write(self):
        (self.attr / "max_value").unlink()
        with self.assertRaises(m_common.ControlError):
            self.hw.set_value("ppt_pl1_spl", 60)

    def test_readback_mismatch_reported(self):
        with patch.object(Path, "write_text", return_value=3):
            with self.assertRaisesRegex(m_common.ControlError, "readback"):
                self.hw.set_value("ppt_pl1_spl", 60)

    def test_ebusy_maps_to_custom_mode_message(self):
        with patch.object(Path, "write_text", side_effect=OSError(errno.EBUSY, "busy")):
            with self.assertRaisesRegex(m_common.ControlError, "custom mode"):
                self.hw.set_value("ppt_pl1_spl", 60)

    def add_profile(self, slot, name, mode="balanced"):
        profile = self.root / f"class/platform-profile/platform-profile-{slot}"
        profile.mkdir(parents=True)
        for key, value in {"name": name, "profile": mode,
                           "choices": "low-power balanced performance custom"}.items():
            (profile / key).write_text(value)
        return profile

    def test_profile_handler_order(self):
        for names, driver in ((("lenovo-legion", "lenovo-wmi-gamezone"), "lenovo-wmi-gamezone"),
                              (("lenovo-legion",), "lenovo-legion")):
            with self.subTest(driver=driver):
                shutil.rmtree(self.root / "class/platform-profile", ignore_errors=True)
                slots = [self.add_profile(i, name) for i, name in enumerate(names)]
                hw = m_hardware.Hardware(self.root)
                self.assertEqual(hw.capabilities()["mode_driver"], driver)
                hw.set_mode("custom")
                self.assertEqual((slots[-1] / "profile").read_text().strip(), "custom")

    def test_legacy_profile_never_writes_custom(self):
        # The kernel's legacy platform_profile lists custom but rejects writing it with EINVAL.
        shutil.rmtree(self.root / "class/platform-profile")
        legacy = self.root / "firmware/acpi"
        legacy.mkdir(parents=True)
        (legacy / "platform_profile").write_text("balanced")
        (legacy / "platform_profile_choices").write_text("low-power balanced performance custom")
        hw = m_hardware.Hardware(self.root)
        self.assertIsNone(hw.capabilities()["mode_driver"])
        with self.assertRaisesRegex(m_common.ControlError, "legacy platform_profile"):
            hw.set_mode("custom")
        self.assertEqual((legacy / "platform_profile").read_text(), "balanced")
        hw.set_mode("performance")
        self.assertEqual((legacy / "platform_profile").read_text().strip(), "performance")

    def test_gamezone_held_by_legion_wmi_is_reported(self):
        # legion_laptop's legion_wmi can claim the gamezone GUID first; then every current_value
        # read fails with EINVAL and the UI must say why instead of a bare N/A.
        drivers = self.root / "bus/wmi/drivers"
        device = self.root / f"bus/wmi/devices/{m_hardware.GAMEZONE_GUID}-22"
        device.mkdir(parents=True)
        for name in ("legion_wmi", "lenovo_wmi_gamezone"):
            (drivers / name).mkdir(parents=True)
        (device / "driver").symlink_to(drivers / "lenovo_wmi_gamezone")
        self.assertIsNone(self.hw.capabilities()["gamezone_holder"])
        (device / "driver").unlink()
        self.assertEqual(self.hw.capabilities()["gamezone_holder"], "no driver")
        (device / "driver").symlink_to(drivers / "legion_wmi")
        real_read = Path.read_text

        def einval(path, *args, **kwargs):
            if path.name == "current_value":
                raise OSError(errno.EINVAL, "Invalid argument")
            return real_read(path, *args, **kwargs)

        with patch.object(Path, "read_text", einval):
            state = self.hw.capabilities()
        item = state["attributes"]["ppt_pl1_spl"]
        self.assertEqual((state["gamezone_holder"], item["current_value"], item["read_errno"]),
                         ("legion_wmi", None, errno.EINVAL))
        self.assertIn("not bound to any driver", m_ui.limit_note({**state, "gamezone_holder": "no driver"}, {}))

    def test_apply_batch_stops_at_first_failure(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        results = m_hardware.apply_batch(self.hw, [["ppt_pl1_spl", 500], ["cpu_temp", 90]])
        self.assertEqual([r["status"] for r in results], ["FAILED", "NOT SENT"])
        self.assertIn("between", results[0]["error"])
        self.assertEqual(results[1]["current"], 80)
        self.assertEqual((self.attr.parent / "cpu_temp/current_value").read_text().strip(), "80")
        self.assertEqual((self.attr / "current_value").read_text().strip(), "50")
        results = m_hardware.apply_batch(self.hw, [["ppt_pl1_spl", 60]])
        self.assertEqual((results[0]["status"], results[0]["before"], results[0]["current"]), ("APPLIED", 50, 60))

    def test_apply_batch_validates_before_writing(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        bad = ["x", [], [["ppt_pl1_spl"]], [["gpu_mode", 1]], [["ppt_pl1_spl", True]],
               [["ppt_pl1_spl", 60], ["ppt_pl1_spl", 65]], [["mode", "balanced"], ["ppt_pl1_spl", 60]],
               [["mode", 3]], [["ppt_pl1_spl", 60]] * (len(m_common.FIELDS) + 2)]
        for changes in bad:
            with self.assertRaises(m_common.ControlError, msg=repr(changes)):
                m_hardware.apply_batch(self.hw, changes)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "50")
        self.assertEqual((self.root / "class/platform-profile/platform-profile-0/profile").read_text(), "custom")


if __name__ == "__main__":
    unittest.main()
