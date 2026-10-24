# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import curses
import errno
import fcntl
import io
import json
import os
import pty
import select
import shutil
import socket
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import loqctl
from loqctl import api as m_api, backend as m_backend, cli as m_cli, colors as m_colors
from loqctl import common as m_common, hardware as m_hardware, keyboard as m_keyboard
from loqctl import sensors as m_sensors, ui as m_ui


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        profile = self.root / "class/platform-profile/platform-profile-0"
        profile.mkdir(parents=True)
        for key, value in {"name": "lenovo-wmi-gamezone", "profile": "custom",
                           "choices": "low-power balanced performance custom"}.items():
            (profile / key).write_text(value)
        self.attr = self.root / "class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_pl1_spl"
        self.attr.mkdir(parents=True)
        for key, value in {"current_value": 50, "min_value": 50, "max_value": 100,
                           "default_value": 60, "scalar_increment": 5}.items():
            (self.attr / key).write_text(str(value))
        self.hw = m_hardware.Hardware(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_boundaries_and_step(self):
        for value in (50, 100, 65):
            self.assertTrue(self.hw.set_value("ppt_pl1_spl", value)["verified"])
        for value in (49, 101, 51, True, 55.0, "55", None):
            with self.assertRaises(m_common.ControlError):
                self.hw.set_value("ppt_pl1_spl", value)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "65")

    def test_zero_increment_is_writable(self):
        (self.attr / "scalar_increment").write_text("0")
        self.hw.set_value("ppt_pl1_spl", 53)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "53")

    def test_mode_gates_custom_writes(self):
        self.hw.set_mode("balanced")
        with self.assertRaisesRegex(m_common.ControlError, "custom"):
            self.hw.set_value("ppt_pl1_spl", 60)
        self.assertEqual((self.attr / "current_value").read_text(), "50")
        with self.assertRaises(m_common.ControlError):
            self.hw.set_mode("turbo")

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

    def test_unix_api(self):
        sampler = type("Sampler", (), {"latest": {"mode": "custom", "metrics": {"cpu": {"power_w": 12}}}})()
        path = str(self.root / "test.sock")
        server = m_api.APIServer(path, self.hw, sampler, os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(m_api.request(path, "GET", "/v1/status")["mode"], "custom")
            self.assertEqual(m_api.request(path, "GET", "/v1/metrics")["cpu"]["power_w"], 12)
            self.assertTrue(m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 70})["verified"])
            for payload in ({"value": True}, {"value": 500}, {"value": 50, "path": "/tmp/x"}, []):
                with self.assertRaises(m_common.ControlError):
                    m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", payload)
            m_api.request(path, "POST", "/v1/mode", {"mode": "balanced"})
            with self.assertRaises(m_common.ControlError):
                m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 60})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_rapl_wrap_and_duplicate_exclusion(self):
        rapl = self.root / "class/powercap/intel-rapl:0"
        rapl.mkdir(parents=True)
        (rapl / "name").write_text("package-0")
        (rapl / "energy_uj").write_text("9000000")
        (rapl / "max_energy_range_uj").write_text("10000000")
        duplicate = self.root / "class/powercap/intel-rapl-mmio:0"
        duplicate.mkdir()
        (duplicate / "name").write_text("package-0")
        (duplicate / "energy_uj").write_text("99999999999")
        sensors = m_sensors.Sensors(self.root)
        with patch.object(time, "monotonic", return_value=1):
            self.assertIsNone(sensors.cpu_power()[0])
        (rapl / "energy_uj").write_text("3000000")
        with patch.object(time, "monotonic", return_value=3):
            self.assertEqual(sensors.cpu_power()[0], 2)

    def test_unsupported_sensor_values_are_null(self):
        for value in ("[N/A]", "nan", "inf", None, "Not Supported"):
            self.assertIsNone(m_common.number(value))

    def add_attr(self, key, current, lo, hi, step, default):
        path = self.attr.parent / key
        path.mkdir(exist_ok=True)
        for name, value in {"current_value": current, "min_value": lo, "max_value": hi,
                            "default_value": default, "scalar_increment": step}.items():
            (path / name).write_text(str(value))

    def test_ui_partial_apply_retains_only_unapplied_edits(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        state = self.hw.capabilities()
        # First write fails: nothing sent after it.
        dashboard = self.dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        calls = []
        def reject(key, value):
            calls.append(key)
            raise m_common.ControlError("rejected")
        dashboard.backend.write = reject
        with self.assertRaisesRegex(m_common.ControlError, "after 0 writes"):
            dashboard.apply(state)
        self.assertEqual(calls, ["ppt_pl1_spl"])
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60, "cpu_temp": 90})
        self.assertEqual([r["status"] for r in dashboard.apply_results], ["FAILED", "NOT SENT"])
        # First write succeeds, second fails: only the failed key stays staged.
        dashboard = self.dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        def partial(key, value):
            if key == "cpu_temp":
                raise m_common.ControlError("rejected")
            self.hw.set_value(key, value)
        dashboard.backend.write = partial
        with self.assertRaisesRegex(m_common.ControlError, "after 1 writes"):
            dashboard.apply(state)
        self.assertEqual(dashboard.pending, {"cpu_temp": 90})
        self.assertEqual([r["status"] for r in dashboard.apply_results], ["APPLIED", "FAILED"])

    def test_locked_rows_reject_edits_in_non_custom_mode(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        for key in (ord("6"), curses.KEY_LEFT, ord("d"), 10):
            dashboard = self.dashboard()
            dashboard.handle(key, state)
            self.assertEqual(dashboard.pending, {})
            self.assertIn("locked in balanced mode", dashboard.notice)

    def test_staging_custom_does_not_unlock_limits(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.selected = 0
        for _ in range(4):
            dashboard.handle(curses.KEY_RIGHT, state)
            if dashboard.pending.get("mode") == "custom":
                break
        self.assertEqual(dashboard.pending, {"mode": "custom"})
        dashboard.selected = 1
        for key in (ord("6"), curses.KEY_RIGHT):
            dashboard.handle(key, state)
            self.assertEqual(dashboard.pending, {"mode": "custom"})
            self.assertIn("locked in balanced mode", dashboard.notice)

    def test_mode_and_limit_edits_never_batched(self):
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.stage(state, "ppt_pl1_spl", 60)
        dashboard.stage(state, "mode", "balanced")
        self.assertEqual(dashboard.pending, {"mode": "balanced"})
        self.assertIn("limit edits dropped", dashboard.notice)
        dashboard.stage(state, "ppt_pl1_spl", 60)
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.assertIn("mode change dropped", dashboard.notice)
        dashboard.stage(state, "mode", "custom")
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_apply_refuses_limits_when_live_mode_not_custom(self):
        self.hw.set_mode("balanced")
        dashboard = self.dashboard()
        dashboard.backend.write = Mock()
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaisesRegex(m_common.ControlError, "locked in balanced mode"):
            dashboard.apply(self.hw.capabilities())
        dashboard.backend.write.assert_not_called()
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

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

    def test_gamezone_profile_preferred_over_other_handlers(self):
        shutil.rmtree(self.root / "class/platform-profile")
        self.add_profile(0, "lenovo-legion")
        gamezone = self.add_profile(1, "lenovo-wmi-gamezone")
        hw = m_hardware.Hardware(self.root)
        self.assertEqual(hw.capabilities()["mode_driver"], "lenovo-wmi-gamezone")
        hw.set_mode("custom")
        self.assertEqual((gamezone / "profile").read_text().strip(), "custom")

    def test_other_class_handler_used_before_legacy_file(self):
        shutil.rmtree(self.root / "class/platform-profile")
        legion = self.add_profile(0, "lenovo-legion")
        hw = m_hardware.Hardware(self.root)
        self.assertEqual(hw.capabilities()["mode_driver"], "lenovo-legion")
        hw.set_mode("custom")
        self.assertEqual((legion / "profile").read_text().strip(), "custom")

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

    def test_mode_row_warns_when_gamezone_missing(self):
        dashboard = self.dashboard()
        dashboard.selected = 0
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        dashboard.box = Mock()
        for driver, expected in (("lenovo-legion", "via lenovo-legion"), (None, "legacy file")):
            calls = []
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color))
            dashboard.draw_controls({**self.hw.capabilities(), "mode_driver": driver}, 0, 0, 20, 60)
            self.assertTrue(any(expected in text and color == 3 for text, color in calls), calls)

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
        self.assertEqual((state["gamezone_holder"], item["current_value"], item["read_error"]),
                         ("legion_wmi", None, "Invalid argument"))
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        dashboard.box = Mock()
        for selected, state_driver in ((0, None), (1, state["mode_driver"])):
            calls = []
            dashboard.selected = selected
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color))
            dashboard.draw_controls({**state, "mode_driver": state_driver}, 0, 0, 20, 90)
            self.assertTrue(any("held by legion_wmi" in text and color == 3 for text, color in calls), calls)

    def test_draw_dims_locked_rows(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        calls = []
        dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color, dim))
        dashboard.box = Mock()
        dashboard.draw_controls(state, 0, 0, 20, 50)
        row = next(c for c in calls if "CPU sustained" in c[0] or "PL1" in c[0] or c[0].startswith("▶"))
        self.assertEqual(row[1:], (7, True))
        self.assertTrue(any("locked in balanced mode" in c[0] for c in calls))

    def test_limit_detail_shows_na_for_unreadable_limits(self):
        self.hw.set_mode("custom")
        cases = (
            (("min_value", "max_value", "default_value"), "N/A–N/A", "step 1", "default N/A"),
            (("max_value",), "5–N/A", "step 1", "default 60"),
            (("scalar_increment",), "5–100", "step N/A", "default 60"),
        )
        for unreadable, *expected in cases:
            self.add_attr("ppt_pl1_spl", 60, 5, 100, 1, 60)
            for name in unreadable:
                (self.attr.parent / "ppt_pl1_spl" / name).write_text("garbage")
            state = self.hw.capabilities()
            dashboard = self.dashboard()
            dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
            calls = []
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color))
            dashboard.box = Mock()
            dashboard.selected = dashboard.rows(state).index("ppt_pl1_spl")
            dashboard.draw_controls(state, 0, 0, 20, 90)
            detail = next(text for text, _ in calls if "step" in text or "default" in text)
            for part in expected:
                self.assertIn(part, detail, unreadable)
            self.assertNotIn("None", detail, unreadable)

    def dashboard(self):
        dashboard = object.__new__(m_ui.Dashboard)
        dashboard.help = False
        dashboard.selected = 1
        dashboard.pending = {}
        dashboard.fields = {}
        dashboard.invalid = set()
        dashboard.sudo_expired = False
        dashboard.apply_results = []
        dashboard.show_results = False
        dashboard.backend = type("Backend", (), {"remote": True})()
        dashboard.backend.write = self.hw.set_value
        dashboard.backend.refresh = lambda: None
        dashboard.backend.controls = self.hw.capabilities
        return dashboard

    def type_keys(self, dashboard, state, *keys):
        for key in keys:
            dashboard.handle(ord(key) if isinstance(key, str) else key, state)

    def multi(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        self.add_attr("gpu_temp", 70, 60, 90, 5, 85)
        return self.dashboard(), self.hw.capabilities()

    def test_typing_and_enter_applies(self):
        dashboard = self.dashboard()
        state = self.hw.capabilities()
        self.type_keys(dashboard, state, "6", "0", 10)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "60")
        self.assertEqual(dashboard.pending, {})
        self.assertEqual(dashboard.fields, {})
        self.assertIn("Applied and verified", dashboard.notice)
        self.assertEqual(dashboard.apply_results[0], {
            "key": "ppt_pl1_spl", "requested": 60, "before": 50,
            "current": 60, "status": "APPLIED", "error": None})

    def test_moving_keeps_valid_drops_empty_keeps_invalid(self):
        dashboard, state = self.multi()
        self.type_keys(dashboard, state, "6", "0", "j", "8", "5", curses.KEY_BACKSPACE, 127, "j", "5", "1")
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.assertEqual(dashboard.invalid, {"gpu_temp"})
        self.assertEqual(dashboard.fields, {"ppt_pl1_spl": "60", "gpu_temp": "51"})

    def test_enter_refuses_with_invalid_and_jumps(self):
        dashboard, state = self.multi()
        dashboard.selected = 3
        self.type_keys(dashboard, state, "5", "1", "k", "k")
        self.assertEqual(dashboard.selected, 1)
        self.type_keys(dashboard, state, 10)
        self.assertEqual(dashboard.selected, 3)
        self.assertIn("red", dashboard.notice)
        self.assertEqual((self.attr / "current_value").read_text(), "50")

    def test_arrows_step_from_staged_value(self):
        dashboard = self.dashboard()
        state = self.hw.capabilities()
        right, left = curses.KEY_RIGHT, curses.KEY_LEFT
        self.type_keys(dashboard, state, right, right)
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.type_keys(dashboard, state, left, left)
        self.assertEqual(dashboard.pending, {})
        self.assertEqual(dashboard.fields, {"ppt_pl1_spl": "50"})
        self.type_keys(dashboard, state, left)
        self.assertEqual(dashboard.pending, {})

    def test_d_stages_custom_default(self):
        dashboard = self.dashboard()
        state = self.hw.capabilities()
        self.type_keys(dashboard, state, "d")
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        dashboard.selected = 0
        self.type_keys(dashboard, state, "d")
        self.assertIn("no default", dashboard.notice)
        self.hw.set_mode("balanced")
        locked = self.dashboard()
        self.type_keys(locked, self.hw.capabilities(), "d")
        self.assertEqual(locked.pending, {})
        self.assertIn("locked in balanced mode", locked.notice)

    def test_report_detects_unchanged_firmware_despite_write_success(self):
        dashboard = self.dashboard()
        dashboard.backend.write = Mock(return_value={"verified": True})
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaisesRegex(m_common.ControlError, "firmware reports 50"):
            dashboard.apply(self.hw.capabilities())
        result = dashboard.apply_results[0]
        self.assertEqual((result["before"], result["requested"], result["current"], result["status"]),
                         (50, 60, 50, "FAILED"))
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_report_cannot_claim_success_when_readback_missing(self):
        dashboard = self.dashboard()
        dashboard.backend.controls = Mock(side_effect=[self.hw.capabilities(), OSError("read failed")])
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaises(m_common.ControlError):
            dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.apply_results[0]["status"], "UNVERIFIED")
        self.assertIsNone(dashboard.apply_results[0]["current"])
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def local_dashboard(self):
        dashboard = self.dashboard()
        dashboard.backend.remote = False
        dashboard.backend.load_mode_limits = Mock()
        dashboard.s = Mock()
        return dashboard

    def test_sudo_error_is_preserved_for_report(self):
        dashboard = self.local_dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60}
        result = subprocess.CompletedProcess([], 1, "", "sudo: authentication failed")
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(subprocess, "run", return_value=result):
            with self.assertRaisesRegex(m_common.ControlError, "authentication failed"):
                dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.assertFalse(dashboard.sudo_expired)

    def sudo_results(self, *statuses):
        keys = ["ppt_pl1_spl", "cpu_temp"]
        return [{"key": k, "requested": 1, "before": 0, "current": 1, "status": st,
                 "error": "boom" if st == "FAILED" else None} for k, st in zip(keys, statuses)]

    def test_apply_uses_one_sudo_n_call(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        dashboard = self.local_dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        dashboard.fields = {"ppt_pl1_spl": "60", "cpu_temp": "90"}
        out = subprocess.CompletedProcess([], 0, json.dumps(self.sudo_results("APPLIED", "APPLIED")), "")
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(subprocess, "run", return_value=out) as run:
            dashboard.apply(self.hw.capabilities())
        self.assertEqual(run.call_count, 1)
        argv = run.call_args[0][0]
        self.assertEqual(argv[:7], ["sudo", "-n", "--", sys.executable, "-I", m_common.ENTRY, "_apply"])
        self.assertEqual(json.loads(argv[7]), [["ppt_pl1_spl", 60], ["cpu_temp", 90]])
        self.assertEqual((dashboard.pending, dashboard.fields), ({}, {}))
        self.assertTrue(dashboard.show_results)

    def test_apply_partial_failure_keeps_failed_staged(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        dashboard = self.local_dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        out = subprocess.CompletedProcess([], 0, json.dumps(self.sudo_results("APPLIED", "FAILED")), "")
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(subprocess, "run", return_value=out):
            with self.assertRaisesRegex(m_common.ControlError, "after 1 writes"):
                dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.pending, {"cpu_temp": 90})

    def test_sudo_expiry_sets_banner_and_keeps_edits(self):
        dashboard = self.local_dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60}
        out = subprocess.CompletedProcess([], 1, "", "sudo: a password is required")
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(subprocess, "run", return_value=out):
            with self.assertRaisesRegex(m_common.ControlError, "Press s"):
                dashboard.apply(self.hw.capabilities())
        self.assertTrue(dashboard.sudo_expired)
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_s_key_reauthenticates(self):
        state = self.hw.capabilities()
        for code, expired in ((0, False), (1, True)):
            dashboard = self.local_dashboard()
            dashboard.sudo_expired = True
            dashboard.notice = ""
            out = subprocess.CompletedProcess([], code)
            with patch.object(os, "geteuid", return_value=1000), \
                 patch.object(curses, "def_prog_mode"), patch.object(curses, "endwin"), \
                 patch.object(curses, "reset_prog_mode"), \
                 patch.object(subprocess, "run", return_value=out) as run:
                dashboard.handle(ord("s"), state)
            run.assert_called_once_with(["sudo", "-v"])
            self.assertEqual(dashboard.sudo_expired, expired)

    def test_preauth_skips(self):
        caps = {"attributes": {"x": {}}, "modes": ["custom"]}
        def backend(remote=False, caps=caps):
            return Mock(remote=remote, hardware=Mock(capabilities=Mock(return_value=caps)))
        def run_with(rcs, **kw):
            def fake(argv, **_):
                return subprocess.CompletedProcess(argv, rcs[tuple(argv)])
            return patch.object(subprocess, "run", side_effect=fake)
        nv = ("sudo", "-n", "true")
        with patch.object(os, "geteuid", return_value=0), patch.object(subprocess, "run") as run:
            self.assertTrue(m_cli.preauth(backend()))
            run.assert_not_called()
        with patch.object(os, "geteuid", return_value=1000), patch.object(subprocess, "run") as run:
            self.assertTrue(m_cli.preauth(backend(remote=True)))
            self.assertTrue(m_cli.preauth(backend(caps={"attributes": {}, "modes": []})))
            run.assert_not_called()
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(shutil, "which", return_value="/usr/bin/sudo"):
            with run_with({nv: 0}) as run:
                self.assertTrue(m_cli.preauth(backend()))
                self.assertEqual(run.call_count, 1)
            with run_with({nv: 1, ("sudo", "-v"): 0}), patch("builtins.print"):
                self.assertTrue(m_cli.preauth(backend()))
            with run_with({nv: 1, ("sudo", "-v"): 1}), patch("builtins.print"):
                self.assertFalse(m_cli.preauth(backend()))
        with patch.object(os, "geteuid", return_value=1000), \
             patch.object(shutil, "which", return_value=None):
            self.assertFalse(m_cli.preauth(backend()))

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

    def test_apply_subcommand_prints_json(self):
        out = io.StringIO()
        with patch.object(m_cli, "Hardware", return_value=self.hw), contextlib.redirect_stdout(out):
            self.assertEqual(m_cli.main(["_apply", json.dumps([["ppt_pl1_spl", 60]])]), 0)
        self.assertEqual(json.loads(out.getvalue())[0]["status"], "APPLIED")

    def test_draw_unlocked_selected_row(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        self.add_attr("gpu_temp", 70, 60, 90, 5, 85)
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3, BAD=5)
        dashboard.pending = {"cpu_temp": 90}
        dashboard.fields = {"ppt_pl1_spl": "60", "cpu_temp": "90", "gpu_temp": "7"}
        dashboard.invalid = {"gpu_temp"}
        calls = []
        dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color))
        dashboard.box = Mock()
        rows = dashboard.rows(state)
        def draw(key):
            calls.clear()
            dashboard.selected = rows.index(key)
            dashboard.draw_controls(state, 0, 0, 20, 60)
        draw("ppt_pl1_spl")
        self.assertIn("1 staged, 1 invalid", dashboard.box.call_args[0])
        row = next(c for c in calls if c[0].startswith("▶"))
        self.assertIn("\u0336", row[0])
        self.assertIn("-> 60_", row[0])
        self.assertEqual(row[1], 1)
        staged = next(c for c in calls if "CPU" in c[0].upper() and "→ 90" in c[0])
        self.assertEqual(staged[1], 3)
        draw("gpu_temp")
        row = next(c for c in calls if c[0].startswith("▶"))
        self.assertIn("-> 7_", row[0])
        self.assertEqual(row[1], 5)

    def test_empty_state_does_not_crash_handle(self):
        dashboard = self.dashboard()
        dashboard.panel = "main"
        dashboard.keyboard = m_keyboard.Keyboard(binary="/usr/bin/false",
                                          config=Path(self.temp.name) / "missing.json")
        dashboard.handle(ord("j"), {})
        dashboard.handle(ord("l"), {})
        self.assertEqual(dashboard.panel, "keyboard")
        dashboard.handle(27, {})
        self.assertEqual(dashboard.panel, "main")

    def test_keyboard_color_helpers(self):
        self.assertEqual(m_colors.rgb_to_hex(242, 118, 10), "f2760a")
        self.assertEqual(m_colors.hex_to_rgb("f2760a"), (242, 118, 10))
        with self.assertRaises(m_common.ControlError):
            m_colors.hex_to_rgb("gg0000")
        h, s, v = m_colors.rgb_to_hsv(255, 0, 0)
        self.assertAlmostEqual(h, 0, delta=0.1)
        self.assertGreater(s, 99)
        self.assertGreater(v, 99)
        self.assertEqual(m_colors.rgb_to_hex(*m_colors.hsv_to_rgb(120, 100, 100)), "00ff00")

    def test_keyboard_status_loads_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "effect": "breath", "speed": 3, "brightness": 1,
                "waveDir": "rtl", "zones": ["ff0000", "00ff00"],
            }))
            kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
            status = kb.status()
            self.assertEqual(status["effect"], "breath")
            self.assertEqual(status["speed"], 3)
            self.assertEqual(status["brightness"], 1)
            self.assertEqual(status["wave_dir"], "rtl")
            self.assertEqual(status["zones"], ["ff0000", "00ff00", "00ff00", "00ff00"])

    def test_dashboard_init_and_draw_under_pty(self):
        """Catch curses API mistakes that unit tests with __new__ miss."""
        attributes = self.hw.capabilities()["attributes"]
        pid, fd = pty.fork()
        if pid == 0:
            # Child: never touch the host's /sys, socket path or ~/.config.
            os.environ.setdefault("TERM", "xterm-256color")
            os.environ.update(HOME=str(self.root), XDG_CONFIG_HOME=str(self.root / "config"),
                              XDG_CACHE_HOME=str(self.root / "cache"))
            m_backend.Hardware = lambda: m_hardware.Hardware(self.root)
            m_ui.Keyboard = lambda: m_keyboard.Keyboard(binary="/nonexistent/legionaura",
                                                        config=self.root / "legionaura.json")
            try:
                def run(screen):
                    backend = m_backend.Backend(str(self.root / "none.sock"))
                    dash = m_ui.Dashboard(screen, backend)
                    state = {
                        "model": "test", "mode": "balanced",
                        "modes": ["balanced", "custom"],
                        "attributes": attributes,
                        "metrics": {"cpu": {}, "gpu": {}, "memory": {},
                                    "battery": [], "fans": [], "load": [0, 0, 0],
                                    "timestamp": time.time()},
                    }
                    dash.draw(state)
                    dash.panel = "keyboard"
                    dash.draw(state)
                    dash.open_fans()
                    dash.draw(state)
                    dash.help = True
                    dash.draw(state)
                    sys.stdout.write("TUI_OK\n")
                    sys.stdout.flush()
                curses.wrapper(run)
            except Exception as exc:
                sys.stdout.write("TUI_FAIL " + repr(exc) + "\n")
                sys.stdout.flush()
                os._exit(1)
            os._exit(0)

        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
        out = b""
        deadline = time.time() + 6
        status = None
        while time.time() < deadline:
            ready, _, _ = select.select([fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    chunk = b""
                if chunk:
                    out += chunk
            waited = os.waitpid(pid, os.WNOHANG)
            if waited[0]:
                status = waited[1]
                # Drain any remaining PTY output after exit.
                while True:
                    ready, _, _ = select.select([fd], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(fd, 8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                break
        if status is None:
            os.kill(pid, 9)
            os.waitpid(pid, 0)
            self.fail("dashboard PTY smoke timed out: " + out.decode("utf-8", "replace")[-500:])
        self.assertTrue(os.WIFEXITED(status), out.decode("utf-8", "replace")[-500:])
        self.assertEqual(os.WEXITSTATUS(status), 0, out.decode("utf-8", "replace")[-500:])
        self.assertIn(b"TUI_OK", out)

    def test_import_does_not_load_heavy_modules(self):
        code = (
            "import sys; sys.path.insert(0, %r); import loqctl.cli as c; "
            "r = c.main(['--version']); "
            "print(r, '_curses' in sys.modules, 'loqctl.ui' in sys.modules, "
            "any(type(sys.modules.get(m)).__name__ == 'module' "
            "for m in ('socketserver', 'http.server', 'http.client', 'curses', 'csv')))" % str(REPO)
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), [loqctl.VERSION, "0", "False", "False", "False"])

    def test_entry_script_runs_isolated(self):
        with tempfile.TemporaryDirectory() as cwd:
            ver = subprocess.run([sys.executable, "-I", str(REPO / "loqctl.py"), "--version"],
                                 capture_output=True, text=True, cwd=cwd)
        self.assertEqual(ver.returncode, 0, ver.stderr)
        self.assertEqual(ver.stdout.strip(), loqctl.VERSION)

class KeyboardWorkerTests(unittest.TestCase):
    def kb(self, config=None):
        return m_keyboard.Keyboard(binary="/usr/bin/false", config=config or "/nonexistent/loqctl.json")

    def test_keyboard_command_matches_effects(self):
        kb = self.kb()
        kb.zones = ["ff0000", "00ff00", "0000ff", "ffffff"]
        kb.speed, kb.brightness, kb.wave_dir = 2, 1, "rtl"
        expected = {
            "off": ["off"], "apply": ["apply"],
            "static": ["static", *kb.zones, "--brightness", "1"],
            "breath": ["breath", *kb.zones, "--speed", "2", "--brightness", "1"],
            "wave": ["wave", "rtl", "--speed", "2", "--brightness", "1"],
            "hue": ["hue", "--speed", "2", "--brightness", "1"],
        }
        for effect, argv in expected.items():
            kb.effect = effect
            self.assertEqual(kb.command(), argv)
        kb.effect = "bogus"
        with self.assertRaises(m_common.ControlError):
            kb.command()

    def test_load_skipped_while_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"zones": ["aaaaaa"] * 4}))
            kb = self.kb(config)
            kb.zones = ["bbbbbb"] * 4
            kb.dirty = True
            kb.load()
            self.assertEqual(kb.zones, ["bbbbbb"] * 4)
            kb.load(force=True)
            self.assertEqual(kb.zones, ["aaaaaa"] * 4)

    def test_keyboard_worker_latest_wins_single_process(self):
        kb = self.kb()
        release, started = threading.Event(), threading.Event()
        calls, state = [], {"now": 0, "max": 0}

        def fake_run(args):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            calls.append(args[0])
            if len(calls) == 1:
                started.set()
                release.wait(2)
            state["now"] -= 1
            return "OK"

        kb._run = fake_run
        kb.dirty = True
        kb.submit([1], "one")
        self.assertTrue(started.wait(2))
        for n in (2, 3, 4):
            kb.submit([n], str(n))
        self.assertTrue(kb.busy)
        release.set()
        deadline = time.monotonic() + 2
        while kb.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(calls, [1, 4])
        self.assertEqual(state["max"], 1)
        self.assertIn("(4)", kb.last_message)
        self.assertEqual(kb.last_seq, 2)
        self.assertFalse(kb.dirty)
        kb.close()

    def test_dirty_kept_for_edits_after_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"zones": ["aaaaaa"] * 4}))
            kb = self.kb(config)
            release, started = threading.Event(), threading.Event()

            def fake_run(args):
                started.set()
                release.wait(2)
                return "OK"

            kb._run = fake_run
            kb.mark_dirty()
            kb.submit(["a"], "a")
            self.assertTrue(started.wait(2))
            kb.zones = ["bbbbbb"] * 4
            kb.mark_dirty()
            release.set()
            deadline = time.monotonic() + 2
            while kb.busy and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(kb.dirty)
            kb.load()
            self.assertEqual(kb.zones, ["bbbbbb"] * 4)
            kb.close()

    def test_worker_survives_oserror(self):
        kb = self.kb()
        results = [OSError("boom"), "OK"]

        def fake_run(args):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        kb._run = fake_run
        kb.submit(["a"], "a")
        deadline = time.monotonic() + 2
        while kb.last_seq < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("boom", kb.last_message)
        kb.submit(["b"], "b")
        while kb.last_seq < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(kb.last_message, "Keyboard applied (b). OK")
        kb.close()

    def test_close_stops_worker(self):
        kb = self.kb()
        kb._run = lambda args: "OK"
        kb.submit(["a"], "a")
        thread = kb._thread
        kb.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())


class ServerSamplerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def stale_socket(self, name="s.sock"):
        path = str(self.root / name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.close()
        return path

    def test_socket_alive(self):
        self.assertFalse(m_api.socket_alive(self.root / "missing"))
        regular = self.root / "file"
        regular.write_text("x")
        self.assertFalse(m_api.socket_alive(regular))
        self.assertFalse(m_api.socket_alive(self.stale_socket()))
        live = self.root / "live.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(live))
        server.listen(1)
        self.assertTrue(m_api.socket_alive(live))

    def test_socket_dir_is_traversable_under_any_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        fresh = self.root / "fresh"
        m_api.prepare_socket_dir(fresh)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o711)
        fresh.chmod(0o700)
        with patch.object(Path, "lstat", return_value=Mock(st_uid=0, st_mode=0o40700)):
            m_api.prepare_socket_dir(fresh)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o711)

    def test_backend_notice_when_socket_dir_unreachable(self):
        directory = self.root / "run"
        directory.mkdir()
        directory.chmod(0o600)
        self.addCleanup(directory.chmod, 0o700)
        with patch.object(m_backend.os, "access", return_value=False):
            backend = m_backend.Backend(directory / "control.sock")
        self.assertFalse(backend.remote)
        self.assertIn("no access to its directory", backend.notice)
        self.assertEqual(m_backend.Backend(self.root / "none" / "control.sock").notice, "")

    def test_backend_ignores_stale_socket(self):
        path = self.stale_socket()
        backend = m_backend.Backend(path)
        self.assertFalse(backend.remote)
        self.assertIn("Ignored stale API socket", backend.notice)
        self.assertEqual(m_backend.Backend(str(self.root / "none")).notice, "")

    def test_serve_unlink_rules(self):
        regular = self.root / "file"
        regular.write_text("x")
        with self.assertRaises(m_common.ControlError):
            m_api.prepare_socket_path(regular)
        self.assertTrue(regular.exists())
        stale = self.stale_socket()
        link = self.root / "link"
        link.symlink_to(stale)
        with self.assertRaises(m_common.ControlError):
            m_api.prepare_socket_path(link)
        self.assertTrue(link.is_symlink())
        self.assertTrue(Path(stale).exists())
        m_api.prepare_socket_path(self.root / "absent")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            m_api.prepare_socket_path(stale)
        self.assertFalse(Path(stale).exists())
        self.assertIn("Removed stale socket", out.getvalue())
        live = self.root / "live.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(live))
        server.listen(1)
        with self.assertRaisesRegex(m_common.ControlError, "already running"):
            m_api.prepare_socket_path(live)
        self.assertTrue(live.exists())

    def sampler(self, hardware):
        sampler = m_sensors.Sampler.__new__(m_sensors.Sampler)
        sampler.hardware = hardware
        sampler.sensors = Mock()
        sampler.sensors.sample.return_value = {"cpu": {}}
        sampler.latest, sampler.caps, sampler.metrics = {}, {}, {}
        sampler.fw_error, sampler.next_firmware = None, None
        sampler.force = threading.Event()
        return sampler

    def test_sampler_keeps_last_good_caps(self):
        hardware = Mock()
        hardware.capabilities.side_effect = [{"mode": "custom"}, RuntimeError("gone")]
        sampler = self.sampler(hardware)
        sampler.tick()
        self.assertEqual(sampler.latest["mode"], "custom")
        self.assertNotIn("error", sampler.latest)
        sampler.refresh()
        first = sampler.latest
        sampler.tick()
        self.assertEqual(sampler.latest["mode"], "custom")
        self.assertEqual(sampler.latest["error"], "gone")
        self.assertIsNot(sampler.latest, first)
        self.assertEqual(sampler.latest["metrics"], {"cpu": {}})

    def test_sampler_polls_firmware_every_5s_and_on_refresh(self):
        hardware = Mock()
        hardware.capabilities.return_value = {"mode": "custom"}
        sampler = self.sampler(hardware)
        with patch.object(time, "monotonic") as clock:
            clock.return_value = 100.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 1)
            clock.return_value = 101.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 1)
            clock.return_value = 105.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 2)
            clock.return_value = 106.0
            sampler.refresh()
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 3)

class FakeNVML:
    """Plain-Python stand-in for libnvidia-ml; bad names return nonzero."""

    def __init__(self, bad=()):
        self.bad = set(bad)
        f = self
        def out(name, setter):
            def call(*args):
                if name in f.bad:
                    return 3
                setter(args[-1]._obj)
                return 0
            return call
        self.nvmlInit_v2 = lambda: 0
        self.nvmlDeviceGetCount_v2 = out("count", lambda o: setattr(o, "value", 1))
        self.nvmlDeviceGetHandleByIndex_v2 = lambda i, h: 0
        self.nvmlDeviceGetName = lambda h, buf, n: (setattr(buf, "value", b"RTX Test"), 0)[1]
        self.nvmlDeviceGetTemperature = out("temp", lambda o: setattr(o, "value", 51))
        self.nvmlDeviceGetPowerUsage = out("power", lambda o: setattr(o, "value", 45000))
        self.nvmlDeviceGetEnforcedPowerLimit = out("limit", lambda o: setattr(o, "value", 80000))
        self.nvmlDeviceGetClockInfo = out("clock", lambda o: setattr(o, "value", 1500))
        def util(o):
            o.gpu, o.memory = 33, 10
        self.nvmlDeviceGetUtilizationRates = out("util", util)
        def mem(o):
            o.total, o.free, o.used = 8192 * 2**20, 6144 * 2**20, 2048 * 2**20
        self.nvmlDeviceGetMemoryInfo = out("mem", mem)


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def pci(self, status):
        self.put("bus/pci/devices/0000:01:00.0/vendor", "0x10de\n")
        self.put("bus/pci/devices/0000:01:00.0/class", "0x030000\n")
        self.put("bus/pci/devices/0000:01:00.0/power/runtime_status", status + "\n")

    def test_nvml_reads_devices(self):
        devices = m_sensors.NVML(FakeNVML()).read()
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual((d["name"], d["temperature_c"], d["power_w"], d["power_limit_w"]),
                         ("RTX Test", 51, 45.0, 80.0))
        self.assertEqual((d["clock_mhz"], d["memory_clock_mhz"], d["utilization_pct"]), (1500, 1500, 33))
        self.assertEqual((d["memory_used_mib"], d["memory_total_mib"]), (2048, 8192))

    def test_nvml_unsupported_field_is_none(self):
        d = m_sensors.NVML(FakeNVML(bad={"power"})).read()[0]
        self.assertIsNone(d["power_w"])
        self.assertEqual(d["temperature_c"], 51)
        self.assertEqual(d["power_limit_w"], 80.0)

    def test_nvml_failure_falls_back_to_nvidia_smi(self):
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        csv_out = "RTX Test, 50, 20.5, 80, 300, 405, 7, 100, 8192\n"
        run = Mock(return_value=Mock(returncode=0, stdout=csv_out))
        with patch("ctypes.CDLL", side_effect=OSError("no lib")), \
                patch.object(shutil, "which", return_value="/x/nvidia-smi"), \
                patch.object(subprocess, "run", run):
            gpu = sensors.gpu()
            self.assertTrue(sensors.nvml.failed)
        self.assertEqual(gpu["source"], "nvidia-smi")
        self.assertEqual(gpu["devices"][0]["power_w"], 20.5)
        self.assertEqual(run.call_args[0][0][0], "/x/nvidia-smi")
        with patch.object(shutil, "which", return_value=None):
            self.assertEqual(sensors.gpu(), {"available": False, "error": "NVIDIA driver unavailable"})

    def test_gpu_skipped_while_suspended(self):
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        sensors.nvml = Mock()
        sensors.nvml.read.return_value = []
        self.pci("suspended")
        with patch.object(subprocess, "run") as run:
            gpu = sensors.gpu()
        self.assertTrue(gpu["suspended"])
        self.assertFalse(gpu["available"])
        sensors.nvml.read.assert_not_called()
        run.assert_not_called()
        self.pci("active")
        sensors.nvml.read.return_value = [{"name": "x"}]
        self.assertEqual(sensors.gpu()["source"], "nvml")
        sensors.nvml.read.assert_called_once()

    def test_gpu_switching_attributes_never_writable(self):
        forbidden = ("dgpu_enable", "gpu_mode", "dgpu_didvid", "dgpu_boost_clk")
        for key in forbidden:
            self.assertNotIn(key, m_common.FIELDS)
        attr = self.root / "class/firmware-attributes/lenovo-wmi-other-0/attributes/dgpu_enable"
        attr.mkdir(parents=True)
        (attr / "current_value").write_text("0")
        with self.assertRaises(m_common.ControlError):
            m_hardware.Hardware(self.root).set_value("dgpu_enable", 1)

    def hwmon(self, n, name, temps):
        self.put(f"class/hwmon/hwmon{n}/name", name)
        for i, (label, value) in enumerate(temps, 1):
            self.put(f"class/hwmon/hwmon{n}/temp{i}_input", str(value * 1000))
            if label:
                self.put(f"class/hwmon/hwmon{n}/temp{i}_label", label)

    def temp_of(self):
        return m_sensors.Sensors.cpu_temperature(sorted((self.root / "class/hwmon").glob("hwmon*")))

    def test_cpu_temperature_labels(self):
        self.assertEqual(self.temp_of(), (None, None))
        self.hwmon(0, "coretemp", [("Package id 0", 60), ("Core 0", 70)])
        self.assertEqual(self.temp_of(), (60, "Package id"))
        self.temp.cleanup(); self.setUp()
        self.hwmon(0, "k10temp", [("Tctl", 80), ("Tdie", 65), ("Tccd1", 90)])
        self.assertEqual(self.temp_of(), (65, "Tdie"))
        self.temp.cleanup(); self.setUp()
        self.hwmon(0, "k10temp", [("Tctl", 80), ("Tccd1", 90)])
        self.assertEqual(self.temp_of(), (80, "Tctl"))
        self.temp.cleanup(); self.setUp()
        self.hwmon(0, "zenpower", [("Tdie", 55), ("Tctl", 75)])
        self.assertEqual(self.temp_of(), (55, "Tdie"))

    def test_amd_rapl_package_power(self):
        self.hwmon(0, "k10temp", [("Tctl", 50)])
        self.put("class/powercap/intel-rapl:0/name", "package-0")
        self.put("class/powercap/intel-rapl:0/energy_uj", "1000000")
        self.put("class/powercap/intel-rapl:0/max_energy_range_uj", "262143328850")
        self.put("proc/uptime", "10 5")
        self.put("proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 kB")
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        sensors.gpu = lambda: {"available": False}
        clock = [100.0]
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            first = sensors.sample()
            self.put("class/powercap/intel-rapl:0/energy_uj", "21000000")
            clock[0] = 102.0
            second = sensors.sample()
        self.assertIsNone(first["cpu"]["power_w"])
        self.assertAlmostEqual(second["cpu"]["power_w"], 10.0)
        self.assertEqual(second["cpu"]["temperature_source"], "Tctl")

    def test_legionaura_resolved_via_path(self):
        kb = m_keyboard.Keyboard(binary="legionaura", config="/nonexistent/loqctl.json")
        run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
        with patch.object(shutil, "which", return_value="/opt/bin/legionaura"), \
                patch.object(subprocess, "run", run):
            self.assertEqual(kb._run(["off"]), "ok")
            self.assertEqual(kb.status()["binary"], "/opt/bin/legionaura")
        self.assertEqual(run.call_args[0][0], ["/opt/bin/legionaura", "off"])
        with patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(m_common.ControlError, "not found"):
                kb._run(["off"])


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.put("proc/uptime", "10 5")
        self.put("proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 kB")

    def tearDown(self):
        self.temp.cleanup()

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def sensors(self):
        s = m_sensors.Sensors(self.root, self.root / "proc", self.root / "dev")
        s.gpu = lambda: {"available": False}
        return s

    def rapl(self, d, name, uj):
        self.put(f"class/powercap/{d}/name", name)
        self.put(f"class/powercap/{d}/energy_uj", str(uj))
        self.put(f"class/powercap/{d}/max_energy_range_uj", "1000000000")

    def test_rapl_all_domains(self):
        for d, n in (("intel-rapl:0", "package-0"), ("intel-rapl:0:0", "core"), ("intel-rapl:1", "psys"),
                     ("intel-rapl-mmio:0", "package-0")):
            self.rapl(d, n, 1000000)
        s = self.sensors()
        with patch.object(time, "monotonic", side_effect=[0, 0, 0, 2, 2, 2]):
            s.rapl_power()
            for d, uj in (("intel-rapl:0", 11000000), ("intel-rapl:0:0", 5000000), ("intel-rapl:1", 21000000),
                          ("intel-rapl-mmio:0", 99000000)):
                self.put(f"class/powercap/{d}/energy_uj", str(uj))
            rapl, package, _ = s.rapl_power()
        self.assertEqual(set(rapl), {"package-0", "package-0/core", "psys"})
        self.assertAlmostEqual(rapl["package-0"], 5.0)
        self.assertAlmostEqual(rapl["package-0/core"], 2.0)
        self.assertAlmostEqual(rapl["psys"], 10.0)
        self.assertAlmostEqual(package, 5.0)

    def test_battery_and_usbc_power(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        self.put("class/power_supply/BAT1/status", "Discharging")
        self.put("class/power_supply/BAT1/power_now", "15000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/online", "1")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/voltage_now", "20000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/current_now", "3000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:002/online", "0")
        self.put("class/power_supply/ucsi-source-psy-USBC000:002/voltage_now", "5000000")
        out = self.sensors().sample()["power"]
        self.assertEqual((out["battery_w"], out["battery_status"]), (15.0, "Discharging"))
        by = {u["name"][-3:]: u for u in out["usbc"]}
        self.assertEqual((by["001"]["online"], by["001"]["watts"]), (True, 60.0))
        self.assertEqual((by["002"]["online"], by["002"]["volts"], by["002"]["amps"], by["002"]["watts"]),
                         (False, 5.0, None, None))

    def test_temperature_groups(self):
        def hw(n, name, temps):
            self.put(f"class/hwmon/hwmon{n}/name", name)
            for i, (label, v) in enumerate(temps, 1):
                self.put(f"class/hwmon/hwmon{n}/temp{i}_input", str(v * 1000))
                if label:
                    self.put(f"class/hwmon/hwmon{n}/temp{i}_label", label)
        hw(0, "spd5118", [(None, 30)])
        hw(1, "spd5118", [(None, 31)])
        hw(2, "nvme", [("Composite", 40), ("Sensor 1", 41)])
        hw(3, "coretemp", [("Package id 0", 60), ("Core 10", 55), ("Core 2", 54)])
        hw(4, "acpitz", [(None, 50)])
        hw(5, "iwlwifi_1", [(None, 45)])
        hw(6, "legion_hwmon", [("CPU Temperature", 48), ("IC Temperature", 0)])
        hw(7, "mystery", [(None, 99)])
        temps = self.sensors().sample()["temperatures"]
        self.assertEqual([(t["group"], t["label"]) for t in temps], [
            ("cpu", "Core 2"), ("cpu", "Core 10"), ("cpu", "Package id 0"), ("ec", "CPU Temperature"),
            ("ec", "IC Temperature"), ("acpi", "temp 1"),
            ("nvme", "Composite"), ("nvme", "Sensor 1"), ("ram", "DIMM 1"), ("ram", "DIMM 2"), ("wifi", "temp 1")])
        ec = {t["label"]: t["c"] for t in temps if t["group"] == "ec"}
        self.assertEqual(ec, {"CPU Temperature": 48.0, "IC Temperature": None})  # 0 means no sensor

    def test_voltages(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        self.put("class/power_supply/BAT1/voltage_now", "16415000")
        self.put("class/hwmon/hwmon0/name", "BAT1")
        self.put("class/hwmon/hwmon0/in0_input", "16415")
        s = self.sensors()
        s.gpu = lambda: {"available": True, "devices": [{"name": "G", "temperature_c": 50}]}
        with patch.object(os, "geteuid", return_value=1000):
            m = s.sample()
        volts = {v["label"]: v["v"] for v in m["voltages"]}
        self.assertEqual(volts["Battery"], 16.415)
        self.assertEqual(volts["BAT1 in0"], 16.415)
        self.assertIsNone(volts["GPU core"])
        self.assertIsNone(volts["CPU 0 Vcore"])
        self.assertIn({"group": "gpu", "label": "G", "c": 50}, m["temperatures"])

    def test_cpu_vcore_msr(self):
        s = self.sensors()
        self.put("proc/cpuinfo", "processor : 0\nvendor_id : GenuineIntel\n")
        msr = self.root / "dev/cpu/0/msr"
        with patch.object(os, "geteuid", return_value=0):
            self.assertIsNone(s.cpu_vcore())  # no msr node
            msr.parent.mkdir(parents=True)
            msr.write_bytes(bytes(0x198) + (0x2000 << 32).to_bytes(8, "little"))
            self.assertEqual(s.cpu_vcore(), 1.0)
            for field, volts in ((2458, 2458 / 8192), (0x4000, 2.0)):  # lowest raw value >= 0.3 V, 2.0 V
                msr.write_bytes(bytes(0x198) + (field << 32).to_bytes(8, "little"))
                self.assertEqual(s.cpu_vcore(), volts)
            for field in (0, 0x0800, 0x5000, 0xFFFF):  # 0 V, 0.25 V, 2.5 V, 8 V
                msr.write_bytes(bytes(0x198) + (field << 32).to_bytes(8, "little"))
                self.assertIsNone(s.cpu_vcore())
            self.put("proc/cpuinfo", "vendor_id : AuthenticAMD\n")
            self.assertIsNone(s.cpu_vcore())
        self.put("proc/cpuinfo", "vendor_id : GenuineIntel\n")
        with patch.object(os, "geteuid", return_value=1000):
            self.assertIsNone(s.cpu_vcore())

    def test_cpu_throttle_deltas(self):
        def counts(core, pkg):
            for i in (0, 1):
                self.put(f"devices/system/cpu/cpu{i}/thermal_throttle/core_throttle_count", str(core))
                self.put(f"devices/system/cpu/cpu{i}/thermal_throttle/package_throttle_count", str(pkg))
                self.put(f"devices/system/cpu/cpu{i}/topology/physical_package_id", "0")
        s = self.sensors()
        counts(1, 5)
        first = s.cpu_throttle()
        self.assertEqual(first, {"core_events": None, "package_events": None, "active": None})
        counts(3, 6)
        second = s.cpu_throttle()
        self.assertEqual((second["core_events"], second["package_events"], second["active"]), (4, 1, True))
        self.assertIs(s.cpu_throttle()["active"], False)

    def test_nvml_throttle_reasons(self):
        lib = FakeNVML()
        lib.nvmlDeviceGetCurrentClocksEventReasons = lambda h, box: (setattr(box._obj, "value", 0x1 | 0x4 | 0x40), 0)[1]
        self.assertEqual(m_sensors.NVML(lib).read()[0]["throttle"], ["sw_power_cap", "hw_thermal"])
        self.assertIsNone(m_sensors.NVML(FakeNVML()).read()[0]["throttle"])

    def stub_dash(self, h=30, w=100):
        dash = m_ui.Dashboard.__new__(m_ui.Dashboard)
        dash.theme = m_ui.Theme(False)
        dash.history = {k: m_ui.collections.deque(maxlen=240) for k in ("cpu", "gpu", "cpu_w", "gpu_w")}
        dash.view = "summary"
        dash.pending, dash.fields, dash.invalid = {}, {}, {}
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (h, w)
        calls = []
        dash.put = lambda y, x, text, *a, **k: calls.append((y, x, str(text)))
        return dash, calls

    def state(self):
        attrs = {k: {"effective_value": v} for k, v in (("ppt_pl1_spl", 55), ("ppt_pl2_sppt", 115),
                                                      ("gpu_nv_ctgp", 115))}
        return {"attributes": attrs}

    def test_summary_view_with_markers(self):
        dash, calls = self.stub_dash(45)
        dash.history["cpu_w"].extend([20, 45, 60])
        dash.history["gpu_w"].extend([30, 80])
        metrics = {"cpu": {"utilization_pct": 5, "power_w": 45.2, "cores": {}},
                   "gpu": {"devices": [{"name": "G", "power_w": 80.1, "power_limit_w": 140,
                                        "throttle": ["sw_power_cap"]}]},
                   "throttle": {"cpu": {"active": True}}}
        dash.draw_telemetry(self.state(), metrics, 1, 40, 42, 60)
        text = "\n".join(t for _, _, t in calls)
        self.assertIn("W 45.2 · PL1 55 · PL2 115", text)
        self.assertIn("W 80.1 · cTGP 115 · limit 140", text)
        self.assertIn("THROTTLE", text)
        self.assertIn("throttle: sw_power_cap", text)
        self.assertIn("┄", text)
        self.assertTrue(all(0 <= y < 45 and x + len(t) <= 100 for y, x, t in calls))
        calls.clear()
        dash.draw_telemetry({}, {"cpu": {}}, 1, 40, 22, 60)  # no limits, small box
        self.assertIn("PL1 N/A", "\n".join(t for _, _, t in calls))

    def test_sensors_view_draws_na(self):
        dash, calls = self.stub_dash()
        dash.draw_sensors({"temperatures": [{"group": "cpu", "label": "Core 0", "c": None}],
                           "power": {"rapl": {"psys": None}, "battery_w": None, "battery_status": None,
                                     "usbc": []},
                           "voltages": [{"label": "CPU 0 Vcore", "v": None}],
                           "throttle": {"cpu": {"core_events": None}, "gpu": None}}, 1, 0, 30, 60)
        text = "\n".join(t for _, _, t in calls)
        for needle in ("Temperatures", "Core 0", "psys", "CPU 0 Vcore", "Throttle"):
            self.assertIn(needle, text)
        self.assertGreaterEqual(text.count("N/A"), 6)
        calls.clear()
        dash.draw_sensors({"temperatures": [{"group": "cpu", "label": str(i), "c": 1} for i in range(40)]},
                          1, 0, 8, 60)
        self.assertEqual(calls[-1][2], "…")
        self.assertTrue(all(y <= 8 for y, _, _ in calls))
        dash.help = False
        dash.show_results = False
        dash.panel = "main"
        dash.paused = False
        dash.rows = lambda state: ["mode"]
        dash.selected = 0
        dash.locked = lambda state: True
        dash.fields, dash.invalid = {}, set()
        dash.handle(ord("t"), {})
        self.assertEqual(dash.view, "sensors")
        dash.handle(ord("t"), {})
        self.assertEqual(dash.view, "summary")


class RegressionTests(unittest.TestCase):
    setUp, tearDown, dashboard = FirmwareTests.setUp, FirmwareTests.tearDown, FirmwareTests.dashboard

    def dash(self):
        dash = self.dashboard()
        dash.notice = ""
        dash.backend.remote = False
        dash.backend.hardware = self.hw
        dash.panel = "main"
        dash.show_limits = False
        dash.view = "summary"
        dash.running = True
        dash.keyboard = Mock()
        return dash

    def test_sudo_failure_keeps_overlay_closed_and_previous_results(self):
        dash = self.dash()
        dash.pending = {"ppt_pl1_spl": 60}
        dash.apply_results = [{"key": "mode", "status": "APPLIED"}]
        with patch.object(os, "geteuid", return_value=1000), \
                patch.object(m_ui.Dashboard, "run_sudo_apply", side_effect=m_common.ControlError("sudo expired")):
            with self.assertRaises(m_common.ControlError):
                dash._apply(self.hw.capabilities())
        self.assertFalse(dash.show_results)
        self.assertEqual(dash.apply_results, [{"key": "mode", "status": "APPLIED"}])

    def test_empty_results_never_claim_match(self):
        dash = self.dash()
        dash.theme = m_ui.Theme(False)
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (30, 100)
        calls = []
        dash.put = lambda y, x, text, *a, **k: calls.append(str(text))
        dash.fill = dash.box = Mock()
        dash.draw_results()
        self.assertFalse(any("matched firmware" in c for c in calls))

    def test_put_draws_the_last_column(self):
        dash = m_ui.Dashboard.__new__(m_ui.Dashboard)
        dash.theme = m_ui.Theme(False)
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (10, 20)
        dash.put(0, 19, "│")  # right box border
        dash.s.addnstr.assert_called_once_with(0, 19, "│", 1, 0)
        dash.put(0, 20, "x")
        dash.put(10, 0, "x")
        self.assertEqual(dash.s.addnstr.call_count, 1)

    def test_external_mode_change_drops_staged_limits(self):
        dash = self.dash()
        dash.pending = {"ppt_pl1_spl": 60, "mode": "balanced"}
        state = self.hw.capabilities()
        state["mode"] = "performance"
        dash.handle(ord("?"), state)
        self.assertEqual(dash.pending, {"mode": "balanced"})
        self.assertIn("Staged limit edits dropped: mode changed to performance", dash.notice)

    def test_keyboard_load_tolerates_bad_config(self):
        config = self.root / "kb.json"
        for text in ('[1, 2]', '{"speed": "fast", "brightness": [], "zones": [5, "zzz", "ff0000"], "effect": "nope"}',
                     '{"zones": 7}'):
            config.write_text(text)
            kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
            kb.load(force=True)
            self.assertEqual((kb.effect, kb.speed, kb.brightness), ("static", 1, 2))
            self.assertEqual(len(kb.zones), 4)
        config.write_text('{"zones": [5, "ff0000"]}')
        kb.load(force=True)
        self.assertEqual(kb.zones, ["ffffff", "ff0000", "ff0000", "ff0000"])

    def test_smt_siblings_counted_once(self):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        s = m_sensors.Sensors(root, root / "proc", root / "dev")
        def counts(core):
            for i, core_id in ((0, "0"), (1, "0"), (2, "1")):
                base = root / f"devices/system/cpu/cpu{i}"
                (base / "thermal_throttle").mkdir(parents=True, exist_ok=True)
                (base / "topology").mkdir(parents=True, exist_ok=True)
                (base / "thermal_throttle/core_throttle_count").write_text(str(core))
                (base / "topology/core_id").write_text(core_id)
                (base / "topology/physical_package_id").write_text("0")
        counts(1)
        s.cpu_throttle()
        counts(3)
        self.assertEqual(s.cpu_throttle()["core_events"], 4)


class KbCliTests(unittest.TestCase):
    def run_cli(self, *argv):
        """kb_cli with a missing config; returns the legionaura argv it would run."""
        calls = []
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)

        class KB(m_keyboard.Keyboard):
            def __init__(self):
                super().__init__(binary="legionaura", config=Path(tmp.name) / "none.json")

            def _run(self, args):
                calls.append(list(args))
                return "OK"
        with patch.object(m_keyboard, "Keyboard", KB), patch.object(shutil, "which", return_value=None):
            result = m_keyboard.kb_cli(list(argv))
        return calls[-1] if calls else None, result

    def test_effect_arguments(self):
        cases = {
            ("static", "#FF0000", "--brightness", "1"): ["static", *["ff0000"] * 4, "--brightness", "1"],
            ("breath", "ff0000", "00ff00", "--speed", "9"):
                ["breath", "ff0000", *["00ff00"] * 3, "--speed", "4", "--brightness", "2"],
            ("wave", "rtl", "--speed", "2"): ["wave", "rtl", "--speed", "2", "--brightness", "2"],
            ("hue", "--brightness", "5"): ["hue", "--speed", "1", "--brightness", "2"],
            ("off",): ["off"],
            ("brightness", "0"): ["--brightness", "1"],
        }
        for argv, expected in cases.items():
            self.assertEqual(self.run_cli(*argv)[0], expected, argv)
        args, result = self.run_cli()
        self.assertIsNone(args)
        self.assertEqual(result["zones"], ["ffffff"] * 4)

    def test_bad_arguments_never_run(self):
        for argv in (("static",), ("static", "--brightness", "1"), ("breath", "zzzzzz"),
                     ("wave", "up"), ("hue", "fast"), ("brightness",), ("bogus",)):
            with self.assertRaises(m_common.ControlError, msg=argv):
                self.run_cli(*argv)


class AnsiColorTests(unittest.TestCase):
    def test_theme_uses_ansi_palette_only(self):
        pairs = []
        with patch.object(curses, "start_color"), patch.object(curses, "use_default_colors"), \
                patch.object(curses, "init_pair", side_effect=lambda *a: pairs.append(a)), \
                patch.object(curses, "init_color", side_effect=AssertionError("init_color redefines RGB")):
            m_ui.Theme(True)
        self.assertTrue(pairs)
        for _, fg, bg in pairs:
            self.assertTrue(-1 <= fg <= 15 and -1 <= bg <= 15, (fg, bg))

    def test_sources_never_redefine_colors_or_emit_raw_escapes(self):
        import tokenize
        for path in sorted((REPO / "loqctl").glob("*.py")):
            with path.open("rb") as handle:
                for token in tokenize.tokenize(handle.readline):
                    if token.type == tokenize.NAME:
                        self.assertNotIn(token.string, ("init_color", "can_change_color"), path.name)
                    elif token.type == tokenize.STRING:
                        for escape in ("\\x1b[", "\\033[", "\\u001b["):
                            self.assertNotIn(escape, token.string.lower(), f"{path.name}: raw escape")
                    elif token.type == tokenize.NUMBER and token.line.lstrip().startswith("curses.init_pair"):
                        # Color numbers above 15 are outside the 16-color ANSI palette.
                        self.assertLessEqual(int(token.string), 15, token.line)


class CompatibilityTests(unittest.TestCase):
    def test_sources_parse_as_python_310(self):
        import ast
        banned = ("tomllib", "ExceptionGroup", "typing.Self", "datetime.UTC", "itertools.batched")
        for path in [REPO / "loqctl.py", *sorted((REPO / "loqctl").glob("*.py"))]:
            source = path.read_text()
            ast.parse(source, str(path), feature_version=(3, 10))
            for name in banned:
                self.assertNotIn(name, source, f"{path.name} uses 3.11+ {name}")


if __name__ == "__main__":
    unittest.main()
