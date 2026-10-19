# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import errno
import fcntl
import io
import importlib.util
import json
import os
import pty
import select
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

spec = importlib.util.spec_from_file_location("loqctl", Path(__file__).resolve().parents[1] / "loqctl.py")
loq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loq)


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
        self.hw = loq.Hardware(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_boundaries_and_step(self):
        for value in (50, 100, 65):
            self.assertTrue(self.hw.set_value("ppt_pl1_spl", value)["verified"])
        for value in (49, 101, 51, True, 55.0, "55", None):
            with self.assertRaises(loq.ControlError):
                self.hw.set_value("ppt_pl1_spl", value)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "65")

    def test_zero_increment_is_writable(self):
        (self.attr / "scalar_increment").write_text("0")
        self.hw.set_value("ppt_pl1_spl", 53)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "53")

    def test_mode_gates_custom_writes(self):
        self.hw.set_mode("balanced")
        with self.assertRaisesRegex(loq.ControlError, "custom"):
            self.hw.set_value("ppt_pl1_spl", 60)
        self.assertEqual((self.attr / "current_value").read_text(), "50")
        with self.assertRaises(loq.ControlError):
            self.hw.set_mode("turbo")

    def test_allowlist_blocks_traversal_and_unknowns(self):
        for key in ("../ppt_pl1_spl", "ppt_pl1_spl/current_value", "gpu_mode", "/etc/passwd"):
            with self.assertRaises(loq.ControlError):
                self.hw.set_value(key, 60)

    def test_missing_limits_never_write(self):
        (self.attr / "max_value").unlink()
        with self.assertRaises(loq.ControlError):
            self.hw.set_value("ppt_pl1_spl", 60)

    def test_readback_mismatch_reported(self):
        with patch.object(Path, "write_text", return_value=3):
            with self.assertRaisesRegex(loq.ControlError, "readback"):
                self.hw.set_value("ppt_pl1_spl", 60)

    def test_unix_api(self):
        sampler = type("Sampler", (), {"latest": {"metrics": {"cpu": {"power_w": 12}}}})()
        path = str(self.root / "test.sock")
        server = loq.APIServer(path, self.hw, sampler, loq.os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(loq.request(path, "GET", "/v1/status")["mode"], "custom")
            self.assertEqual(loq.request(path, "GET", "/v1/metrics")["cpu"]["power_w"], 12)
            self.assertTrue(loq.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 70})["verified"])
            for payload in ({"value": True}, {"value": 500}, {"value": 50, "path": "/tmp/x"}, []):
                with self.assertRaises(loq.ControlError):
                    loq.request(path, "POST", "/v1/attributes/ppt_pl1_spl", payload)
            loq.request(path, "POST", "/v1/mode", {"mode": "balanced"})
            with self.assertRaises(loq.ControlError):
                loq.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 60})
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
        sensors = loq.Sensors(self.root)
        with patch.object(loq.time, "monotonic", return_value=1):
            self.assertIsNone(sensors.cpu_power()[0])
        (rapl / "energy_uj").write_text("3000000")
        with patch.object(loq.time, "monotonic", return_value=3):
            self.assertEqual(sensors.cpu_power()[0], 2)

    def test_unsupported_sensor_values_are_null(self):
        for value in ("[N/A]", "nan", "inf", None, "Not Supported"):
            self.assertIsNone(loq.number(value))

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
            raise loq.ControlError("rejected")
        dashboard.backend.write = reject
        with self.assertRaisesRegex(loq.ControlError, "after 0 writes"):
            dashboard.apply(state)
        self.assertEqual(calls, ["ppt_pl1_spl"])
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60, "cpu_temp": 90})
        self.assertEqual([r["status"] for r in dashboard.apply_results], ["FAILED", "NOT SENT"])
        # First write succeeds, second fails: only the failed key stays staged.
        dashboard = self.dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        def partial(key, value):
            if key == "cpu_temp":
                raise loq.ControlError("rejected")
            self.hw.set_value(key, value)
        dashboard.backend.write = partial
        with self.assertRaisesRegex(loq.ControlError, "after 1 writes"):
            dashboard.apply(state)
        self.assertEqual(dashboard.pending, {"cpu_temp": 90})
        self.assertEqual([r["status"] for r in dashboard.apply_results], ["APPLIED", "FAILED"])

    def test_locked_rows_reject_edits_in_non_custom_mode(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        for key in (ord("6"), loq.curses.KEY_LEFT, ord("d"), 10):
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
            dashboard.handle(loq.curses.KEY_RIGHT, state)
            if dashboard.pending.get("mode") == "custom":
                break
        self.assertEqual(dashboard.pending, {"mode": "custom"})
        dashboard.selected = 1
        for key in (ord("6"), loq.curses.KEY_RIGHT):
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
        with self.assertRaisesRegex(loq.ControlError, "locked in balanced mode"):
            dashboard.apply(self.hw.capabilities())
        dashboard.backend.write.assert_not_called()
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_ebusy_maps_to_custom_mode_message(self):
        with patch.object(Path, "write_text", side_effect=OSError(errno.EBUSY, "busy")):
            with self.assertRaisesRegex(loq.ControlError, "custom mode"):
                self.hw.set_value("ppt_pl1_spl", 60)

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

    def dashboard(self):
        dashboard = object.__new__(loq.Dashboard)
        dashboard.help = False
        dashboard.selected = 1
        dashboard.editing = False
        dashboard.edit_text = ""
        dashboard.pending = {}
        dashboard.apply_results = []
        dashboard.show_results = False
        dashboard.backend = type("Backend", (), {"remote": True})()
        dashboard.backend.write = self.hw.set_value
        dashboard.backend.controls = self.hw.capabilities
        return dashboard

    def test_apply_key_commits_typed_value(self):
        for apply_key in ("a", "A"):
            with self.subTest(key=apply_key):
                (self.attr / "current_value").write_text("50")
                dashboard = self.dashboard()
                state = self.hw.capabilities()
                for key in (10, ord("6"), ord("0"), ord(apply_key)):
                    dashboard.handle(key, state)
                self.assertEqual((self.attr / "current_value").read_text().strip(), "60")
                self.assertFalse(dashboard.editing)
                self.assertEqual(dashboard.pending, {})
                self.assertIn("Applied and verified", dashboard.notice)
                self.assertTrue(dashboard.show_results)
                self.assertEqual(dashboard.apply_results[0], {
                    "key": "ppt_pl1_spl", "requested": 60, "before": 50,
                    "current": 60, "status": "APPLIED", "error": None})

    def test_enter_stages_without_applying(self):
        dashboard = self.dashboard()
        state = self.hw.capabilities()
        for key in (10, ord("6"), ord("0"), 10):
            dashboard.handle(key, state)
        self.assertEqual((self.attr / "current_value").read_text(), "50")
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.assertFalse(dashboard.editing)
        dashboard.handle(ord("a"), state)
        self.assertEqual((self.attr / "current_value").read_text().strip(), "60")

    def test_apply_in_editor_rejects_invalid_value(self):
        for value in ("", "101", "51"):
            with self.subTest(value=value):
                dashboard = self.dashboard()
                dashboard.editing = True
                dashboard.edit_text = value
                with self.assertRaises(loq.ControlError):
                    dashboard.handle(ord("a"), self.hw.capabilities())
                self.assertEqual((self.attr / "current_value").read_text(), "50")
                self.assertFalse(dashboard.editing)

    def test_report_detects_unchanged_firmware_despite_write_success(self):
        dashboard = self.dashboard()
        dashboard.backend.write = Mock(return_value={"verified": True})
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaisesRegex(loq.ControlError, "firmware reports 50"):
            dashboard.apply(self.hw.capabilities())
        result = dashboard.apply_results[0]
        self.assertEqual((result["before"], result["requested"], result["current"], result["status"]),
                         (50, 60, 50, "FAILED"))
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_report_preserves_write_failure_and_existing_limit(self):
        dashboard = self.dashboard()
        dashboard.backend.write = Mock(side_effect=loq.ControlError("Firmware rejected the write"))
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        with self.assertRaises(loq.ControlError):
            dashboard.apply(self.hw.capabilities())
        result = dashboard.apply_results[0]
        self.assertEqual(result["current"], 50)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual(result["error"], "Firmware rejected the write")
        self.assertEqual(dashboard.apply_results[1]["status"], "NOT SENT")
        self.assertEqual(dashboard.backend.write.call_count, 1)

    def test_report_cannot_claim_success_when_readback_missing(self):
        dashboard = self.dashboard()
        dashboard.backend.controls = Mock(side_effect=[self.hw.capabilities(), OSError("read failed")])
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaises(loq.ControlError):
            dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.apply_results[0]["status"], "UNVERIFIED")
        self.assertIsNone(dashboard.apply_results[0]["current"])
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_sudo_error_is_preserved_for_report(self):
        dashboard = self.dashboard()
        dashboard.backend.remote = False
        dashboard.s = Mock()
        result = loq.subprocess.CompletedProcess([], 1, "", "sudo: authentication failed")
        with patch.object(loq.os, "geteuid", return_value=1000), \
             patch.object(loq.curses, "def_prog_mode"), \
             patch.object(loq.curses, "endwin"), \
             patch.object(loq.curses, "reset_prog_mode"), \
             patch.object(loq.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(loq.ControlError, "authentication failed"):
                dashboard.write_one("ppt_pl1_spl", 60)

    def test_api_write_result_includes_before_and_readback(self):
        result = self.hw.set_value("ppt_pl1_spl", 60)
        self.assertEqual((result["previous"], result["current"], result["verified"]), (50, 60, True))

    def test_result_panel_can_close_and_reopen(self):
        dashboard = self.dashboard()
        dashboard.pending = {"ppt_pl1_spl": 60}
        state = self.hw.capabilities()
        dashboard.apply(state)
        dashboard.handle(10, state)
        self.assertFalse(dashboard.show_results)
        dashboard.handle(ord("v"), state)
        self.assertTrue(dashboard.show_results)

    def test_empty_state_does_not_crash_handle(self):
        dashboard = self.dashboard()
        dashboard.panel = "main"
        dashboard.keyboard = loq.Keyboard(binary="/usr/bin/false",
                                          config=Path(self.temp.name) / "missing.json")
        dashboard.handle(ord("j"), {})
        dashboard.handle(ord("l"), {})
        self.assertEqual(dashboard.panel, "keyboard")
        dashboard.handle(27, {})
        self.assertEqual(dashboard.panel, "main")

    def test_keyboard_color_helpers(self):
        self.assertEqual(loq.rgb_to_hex(242, 118, 10), "f2760a")
        self.assertEqual(loq.hex_to_rgb("f2760a"), (242, 118, 10))
        with self.assertRaises(loq.ControlError):
            loq.hex_to_rgb("gg0000")
        h, s, v = loq.rgb_to_hsv(255, 0, 0)
        self.assertAlmostEqual(h, 0, delta=0.1)
        self.assertGreater(s, 99)
        self.assertGreater(v, 99)
        self.assertEqual(loq.rgb_to_hex(*loq.hsv_to_rgb(120, 100, 100)), "00ff00")

    def test_keyboard_status_loads_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "effect": "breath", "speed": 3, "brightness": 1,
                "waveDir": "rtl", "zones": ["ff0000", "00ff00"],
            }))
            kb = loq.Keyboard(binary="/usr/bin/false", config=config)
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
            os.environ.setdefault("TERM", "xterm-256color")
            try:
                def run(screen):
                    backend = loq.Backend()
                    dash = loq.Dashboard(screen, backend)
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
                    dash.help = True
                    dash.draw(state)
                    sys.stdout.write("TUI_OK\n")
                    sys.stdout.flush()
                loq.curses.wrapper(run)
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
        repo = str(Path(loq.__file__).resolve().parent)
        code = (
            "import sys; sys.path.insert(0, %r); import loqctl; "
            "print('_curses' in sys.modules, any(type(sys.modules.get(m)).__name__ == 'module' "
            "for m in ('socketserver', 'http.server', 'http.client', 'curses', 'csv')))" % repo
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
        self.assertEqual(out.stdout.split(), ["False", "False"])
        ver = subprocess.run([sys.executable, loq.__file__, "--version"], capture_output=True, text=True)
        self.assertEqual(ver.returncode, 0)
        self.assertEqual(ver.stdout.strip(), loq.VERSION)

    def test_main_rejects_old_python(self):
        err = io.StringIO()
        with patch.object(loq.sys, "version_info", (3, 9, 0)), contextlib.redirect_stderr(err):
            self.assertEqual(loq.main(["--version"]), 1)
        self.assertIn("3.10", err.getvalue())


if __name__ == "__main__":
    unittest.main()
