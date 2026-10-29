# SPDX-License-Identifier: GPL-3.0-or-later
import curses
import errno
import fcntl
import json
import os
import pty
import select
import shutil
import struct
import subprocess
import sys
import termios
import time
import unittest
from unittest.mock import patch, Mock, PropertyMock

from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import backend as m_backend
from loqctl import cli as m_cli
from loqctl import common as m_common
from loqctl import hardware as m_hardware
from loqctl import keyboard as m_keyboard
from loqctl import ui as m_ui


class UiTests(helpers.SysfsCase):
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

    def test_dashboard_mode_lock(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        for key in (ord("6"), curses.KEY_LEFT, ord("d"), 10):
            dashboard = self.dashboard()
            dashboard.handle(key, state)
            self.assertEqual(dashboard.pending, {})
            self.assertIn("locked in balanced mode", dashboard.notice)
        # Staging custom does not unlock the limits.
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
        # Apply rechecks the live mode.
        dashboard = self.dashboard()
        dashboard.backend.write = Mock()
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaisesRegex(m_common.ControlError, "locked in balanced mode"):
            dashboard.apply(self.hw.capabilities())
        dashboard.backend.write.assert_not_called()
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

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

    def test_limit_note_precedence(self):
        # A non-EINVAL read error shows as itself; otherwise a gamezone holder explains the N/A,
        # even when the reads succeeded or the attributes are absent.
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        dashboard.box = Mock()
        cases = (("custom", 0, None, None, "held by legion_wmi"),
                 ("custom", 1, "Permission denied", errno.EACCES, "unreadable (Permission denied)"),
                 ("custom", 1, "Invalid argument", errno.EINVAL, "held by legion_wmi"),
                 ("balanced", 1, None, None, "held by legion_wmi"),
                 ("balanced", 1, "Permission denied", errno.EACCES, "unreadable (Permission denied)"),
                 ("balanced", 0, None, None, "held by legion_wmi"))
        for mode, selected, error, code, expected in cases:
            self.hw.set_mode(mode)
            state = {**self.hw.capabilities(), "gamezone_holder": "legion_wmi", "mode_driver": None}
            for item in state["attributes"].values():
                item.update(effective_value=None, read_error=error, read_errno=code)
                if error:
                    item["current_value"] = None
            calls = []
            dashboard.selected = selected
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color))
            dashboard.draw_controls(state, 0, 0, 20, 90)
            self.assertTrue(any(expected in text and color == 3 for text, color in calls), (mode, selected, calls))
        self.assertIn("held by legion_wmi", m_ui.limit_note({"gamezone_holder": "legion_wmi"}, {}))

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
        self.assertTrue(any("Locked in balanced mode" in c[0] for c in calls))

    def test_power_section_fits_grid_with_all_fields(self):
        self.hw.set_mode("custom")
        state = self.hw.capabilities()
        template = next(iter(state["attributes"].values()))
        state["attributes"] = {k: {**template, "label": m_common.FIELDS[k][0], "unit": m_common.FIELDS[k][1]}
                               for k in m_common.FIELDS}
        state["modes"] = ["quiet", "balanced", "performance", "custom"]
        for mode in ("custom", "balanced"):
            state["mode"] = mode
            for h, w in ((20, 72), (24, 80)):
                dashboard = self.dashboard()
                dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3, INFO=6, BAD=2)
                dashboard.box = Mock()
                rows = dashboard.rows(state)
                for sel in range(len(rows)):
                    calls = []
                    dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((y, str(text)))
                    dashboard.selected = sel
                    dashboard.pending = {"mode": "performance"} if sel == 0 else {}
                    dashboard.draw_controls(state, 0, 14, h - 3, w - 14)
                    lines = dict(calls)
                    self.assertTrue(all(0 < y < h - 3 and len(t) <= w - 16 for y, t in calls), (mode, h, w, sel))
                    self.assertRegex(lines[1], r"Mode\s+")
                    if sel == 0:
                        self.assertTrue(lines[1].endswith(f"{mode} → performance"), lines[1])
                    elif mode == "custom" or rows[sel] in ("ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp"):
                        self.assertTrue(any(t.startswith("▶") for t in lines.values()), (mode, h, w, sel))
                    if mode == "balanced":
                        self.assertLessEqual(sum(t.startswith(("▶", " ")) for y, t in calls if y > 1), 4)
                        self.assertTrue(any("select custom" in t.lower() for t in lines.values()))

    def test_stock_mode_cursor_visits_only_drawn_rows(self):
        self.hw.set_mode("balanced")
        for key in ("ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp", "cpu_temp"):
            self.add_attr(key, 60, 5, 100, 1, 60)
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3, INFO=6, BAD=2)
        dashboard.box = Mock()
        rows = dashboard.rows(state)
        dashboard.selected = rows.index("cpu_temp")  # left over from custom mode
        visited = []
        for _ in range(5):
            calls = []
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append(str(text))
            dashboard.draw_controls(state, 0, 0, 17, 58)
            self.assertTrue(any(t.startswith("▶") for t in calls), rows[dashboard.selected])
            visited.append(rows[dashboard.selected])
            dashboard.handle(curses.KEY_DOWN, state)
        self.assertEqual(visited[0], "mode")
        self.assertEqual(visited[1:], ["ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp", "mode"])

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
        dashboard.backend.write = Mock(return_value={"verified": True})
        dashboard.pending = {"ppt_pl1_spl": 60}
        with self.assertRaises(m_common.ControlError):
            dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.apply_results[0]["status"], "UNVERIFIED")
        self.assertIsNone(dashboard.apply_results[0]["current"])
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})

    def test_remote_apply_reads_capabilities_once(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        dashboard = self.dashboard()
        dashboard.backend.controls = Mock(return_value=self.hw.capabilities())
        dashboard.backend.write = Mock(side_effect=lambda key, value: {"current": value})
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        dashboard.apply(self.hw.capabilities())
        self.assertEqual(dashboard.backend.controls.call_count, 1)
        self.assertEqual([(r["key"], r["before"], r["current"], r["status"]) for r in dashboard.apply_results],
                         [("ppt_pl1_spl", 50, 60, "APPLIED"), ("cpu_temp", 80, 90, "APPLIED")])
        self.assertEqual(dashboard.pending, {})

    def test_remote_apply_rereads_skipped_items(self):
        self.add_attr("cpu_temp", 80, 70, 100, 5, 90)
        dashboard = self.dashboard()
        caps = self.hw.capabilities()
        fresh = self.hw.capabilities()
        fresh["attributes"]["cpu_temp"]["current_value"] = 85
        dashboard.backend.controls = Mock(side_effect=[caps, fresh])
        dashboard.backend.write = Mock(return_value={"current": "50"})
        dashboard.pending = {"ppt_pl1_spl": 60, "cpu_temp": 90}
        with self.assertRaises(m_common.ControlError):
            dashboard.apply(caps)
        first, skipped = dashboard.apply_results
        self.assertEqual((first["status"], first["current"]), ("FAILED", 50))
        self.assertEqual((skipped["status"], skipped["before"], skipped["current"]), ("NOT SENT", 80, 85))

    def test_remote_apply_normalises_reported_type(self):
        dashboard = self.dashboard()
        dashboard.backend.controls = Mock(return_value=self.hw.capabilities())
        dashboard.backend.write = Mock(return_value={"current": "60"})
        dashboard.pending = {"ppt_pl1_spl": 60}
        dashboard.apply(self.hw.capabilities())
        self.assertEqual((dashboard.apply_results[0]["status"], dashboard.apply_results[0]["current"]),
                         ("APPLIED", 60))

    def test_direct_root_apply_fills_before(self):
        dashboard = self.local_dashboard()
        dashboard.backend.hardware = self.hw
        dashboard.pending = {"ppt_pl1_spl": 60}
        with patch.object(os, "geteuid", return_value=0):
            dashboard.apply(self.hw.capabilities())
        result = dashboard.apply_results[0]
        self.assertEqual((result["before"], result["current"], result["status"]), (50, 60, "APPLIED"))

    def test_invalid_changes_clear_old_results(self):
        dashboard = self.dashboard()
        dashboard.apply_results, dashboard.show_results = [{"key": "old"}], True
        dashboard.pending = {"ppt_pl1_spl": "60"}
        with self.assertRaises(m_common.ControlError):
            dashboard.apply(self.hw.capabilities())
        self.assertEqual((dashboard.apply_results, dashboard.show_results), ([], False))

    def local_dashboard(self):
        dashboard = self.dashboard()
        dashboard.backend.remote = False
        dashboard.backend.load_mode_limits = Mock()
        dashboard.s = Mock()
        return dashboard

    def test_sudo_failure_keeps_edits_and_flags_expiry(self):
        cases = (("sudo: authentication failed", "authentication failed", False),
                 ("sudo: a password is required", "Press s", True))
        for stderr, message, expired in cases:
            with self.subTest(stderr=stderr):
                dashboard = self.local_dashboard()
                dashboard.pending = {"ppt_pl1_spl": 60}
                result = subprocess.CompletedProcess([], 1, "", stderr)
                with patch.object(os, "geteuid", return_value=1000), \
                     patch.object(subprocess, "run", return_value=result):
                    with self.assertRaisesRegex(m_common.ControlError, message):
                        dashboard.apply(self.hw.capabilities())
                self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
                self.assertEqual(dashboard.sudo_expired, expired)

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
                                          config=self.root / "missing.json")
        dashboard.handle(ord("j"), {})
        dashboard.handle(ord("l"), {})
        self.assertEqual(dashboard.panel, "keyboard")
        dashboard.handle(27, {})
        self.assertEqual(dashboard.panel, "main")

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

    def test_help_overlay_fits_80x24_and_shows_close_hint(self):
        dash = self.dashboard()
        dash.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        lines = m_ui.Dashboard.HELP_LINES
        self.assertLessEqual(max(map(len, lines)), 60)
        self.assertTrue(lines[0].startswith("Global"))
        for size in ((24, 80), (20, 72), (20, 30)):
            calls = []
            dash.s = Mock()
            dash.s.getmaxyx.return_value = size
            dash.put = lambda y, x, text, *a, **k: calls.append(text)
            dash.fill = Mock()
            dash.draw_help()
            self.assertTrue(any("? or Esc to close" in c for c in calls))
            if size == (24, 80):
                self.assertTrue(set(filter(None, lines)) <= set(calls))
            elif size == (20, 72):
                self.assertIn(lines[0], calls)
                self.assertTrue(any("more, resize" in c for c in calls))
            else:
                self.assertNotIn(" Help ", calls)

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

    def test_help_key_opens_from_every_panel(self):
        for panel in ("overview", "main", "keyboard", "fans"):
            dash = self.dash()
            dash.panel = panel
            dash.handle(ord("?"), {})
            self.assertTrue(dash.help, panel)
            dash.handle(ord("?"), {})
            self.assertFalse(dash.help, panel)
            self.assertEqual(dash.panel, panel)

    def test_tab_cycles_supported_sections_and_hides_missing(self):
        dash = self.dash()
        dash.panel = "overview"
        dash.keyboard.available = False
        state = self.hw.capabilities()
        self.assertEqual(dash.sections(state), ["overview", "main", "fans"])
        dash.handle(9, state)
        self.assertEqual(dash.panel, "main")
        dash.handle(curses.KEY_BTAB, state)
        self.assertEqual(dash.panel, "overview")
        dash.keyboard.available = True
        self.assertEqual(dash.sections(state)[-1], "keyboard")

    def test_esc_returns_to_overview_when_power_is_hidden(self):
        dash = self.dash()
        dash.rows = lambda state: []
        dash.panel = "keyboard"
        dash.handle(27, {})
        self.assertEqual(dash.panel, "overview")
        dash.panel = "fans"
        dash.fan_take = lambda: None
        dash.fan_edit = None
        dash.handle(27, {})
        self.assertEqual(dash.panel, "overview")

    def test_details_gpu_row_shows_mode_and_fits_the_screen(self):
        for h, w in ((20, 72), (24, 80)):
            puts = []
            screen = Mock()
            screen.getmaxyx.return_value = (h, w)
            screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append((y, x, text[:n]))
            with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=False):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
            dash.panel, dash.view = "overview", "details"
            gpu = {"name": "NVIDIA GeForce RTX 4060 Laptop GPU"}
            dash.draw({"metrics": {"gpu": {"devices": [gpu]}, "gpu_mode": "Hybrid (auto)"},
                       "attributes": {}, "modes": []})
            self.assertTrue(any(t.startswith("GPU") and "mode Hybrid (auto)" in t for _, _, t in puts), (h, w))
            self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w))

    def test_dialogs_and_header_stay_inside_the_screen(self):
        modes = ["quiet", "balanced", "performance", "custom"]
        template = {"label": "x", "unit": "W", "available": True, "current_value": 55, "min_value": 5,
                    "max_value": 140, "scalar_increment": 1}
        attrs = {k: {**template, "label": m_common.FIELDS[k][0]} for k in m_common.FIELDS}
        for h, w in ((20, 72), (24, 80)):
            for case in ("limits", "results", "header"):
                grid = [[" "] * w for _ in range(h)]

                def addnstr(y, x, text, n, attr=0):
                    for i, c in enumerate(text[:n]):
                        grid[y][x + i] = c
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = addnstr
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
                dash.panel = "main"
                state = {"metrics": {}, "attributes": attrs, "modes": modes, "mode": "custom",
                         "model": "Lenovo " + "LOQ Gaming Notebook " * 3, "mode_limits": None}
                if case == "limits":
                    dash.show_limits = True
                elif case == "results":
                    dash.show_results = True
                    dash.apply_results = [{"key": "ppt_pl1_spl", "before": 1, "requested": 2, "current": 2,
                                           "status": "APPLIED"}] * 14
                dash.draw(state)
                text = ["".join(r) for r in grid]
                if case == "limits":
                    self.assertIn("custom", text[next(i for i, t in enumerate(text) if "Control" in t and "quiet" in t)])
                    self.assertTrue(any("more" in t and "…" in t for t in text), (h, w))
                elif case == "results":
                    self.assertTrue(any("… " in t and "more" in t for t in text), (h, w))
                    drawn = [t for t in text if "ppt" in t.lower() or "APPLIED" in t]
                    self.assertTrue(drawn and all(t.rstrip(" │").endswith("APPLIED") for t in drawn), (h, w))
                    self.assertTrue(any("Enter / Esc closes" in t for t in text), (h, w))
                else:
                    self.assertIn("CUSTOM", text[0], (h, w))
                    self.assertIn("API", text[0], (h, w))

    def test_failed_poll_shows_error_and_na_readings(self):
        backend = m_backend.Backend(str(self.root / "none.sock"))
        backend.stop = Mock(is_set=Mock(side_effect=[False, True]))
        backend.wake = Mock()
        backend.latest = {"mode": "custom", "metrics": {"cpu": {"temp_c": 61}}}
        with patch.object(m_backend, "request", side_effect=OSError("gone")), \
                patch.object(m_backend.threading, "Thread", lambda target, daemon: Mock(start=target)):
            backend.remote = True
            backend.start()
        self.assertNotIn("metrics", backend.latest)
        self.assertEqual(backend.latest["mode"], "custom")
        puts = []
        screen = Mock()
        screen.getmaxyx.return_value = (24, 80)
        screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append(text[:n])
        with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                patch.object(curses, "has_colors", return_value=False):
            dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
        dash.draw(backend.latest)
        self.assertTrue(any("API connection failed" in t for t in puts))
        self.assertFalse(any("61" in t for t in puts))

    def test_battery_health_fits_summary_and_details(self):
        battery = [{"name": "BAT1", "percent": 90, "status": "Discharging", "charge_limit": "80%",
                    "health_percent": 98.13, "cycles": 77, "full_wh": 58.88, "design_wh": 60.0}]
        for h, w in ((20, 72), (24, 80)):
            for view in ("summary", "details"):
                puts = []
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append((y, x, text[:n]))
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
                dash.panel, dash.view = "overview", view
                dash.draw({"metrics": {"battery": battery, "ac_online": False}, "attributes": {}, "modes": []})
                texts = [t for _, _, t in puts]
                if view == "summary":
                    self.assertTrue(any("health 98%" in t for t in texts), (h, w))
                else:
                    self.assertTrue(any(t.strip().startswith("BAT1") and "cycles 77" in t for t in texts), (h, w))
                self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w, view))

    def test_keyboard_page_never_cuts_a_line_mid_word_at_72x20(self):
        grid = [[" "] * 72 for _ in range(20)]

        def addnstr(y, x, text, n, attr=0):
            for i, c in enumerate(text[:n]):
                grid[y][x + i] = c

        screen = Mock()
        screen.getmaxyx.return_value = (20, 72)
        screen.addnstr.side_effect = addnstr
        with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                patch.object(curses, "has_colors", return_value=False):
            dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
        dash.keyboard = m_keyboard.Keyboard(binary="/nonexistent/legionaura", config=self.root / "legionaura.json")
        dash.panel = "keyboard"
        dash.draw({"metrics": {}, "attributes": {}, "modes": []})
        rows = ["".join(r)[m_ui.Dashboard.NAV_W + 1:].strip("│ ") for r in grid]
        self.assertIn("for 4-zone RGB control.", rows)
        with patch.object(m_keyboard.Keyboard, "available", new_callable=PropertyMock, return_value=True):
            dash.draw({"metrics": {}, "attributes": {}, "modes": []})
        rows = ["".join(r)[m_ui.Dashboard.NAV_W + 1:].strip("│ ") for r in grid]
        tip = next(r for r in rows if r.startswith("Hardware"))
        self.assertIn(tip, ("Hardware shows the true color. UI uses your terminal theme only.",
                            "Hardware shows the true color."))
        title = next(r for r in rows if "HSV picker" in r)
        self.assertIn(title, ("zone 1  HSV picker  (terminal swatch ≈ nearest ANSI)", "zone 1  HSV picker"))

    def test_overview_arrows_switch_section_but_never_edit(self):
        dash = self.dash()
        dash.panel = "overview"
        dash.fan = {"available": False}
        dash.keyboard.available = False
        state = self.hw.capabilities()
        dash.handle(curses.KEY_RIGHT, state)
        dash.handle(ord("5"), state)
        self.assertEqual((dash.pending, dash.fields), ({}, {}))
        dash.handle(curses.KEY_DOWN, state)
        self.assertEqual(dash.panel, "main")

    def test_footer_hint_fits_and_keeps_help_and_quit(self):
        for panel in ("overview", "main", "keyboard", "fans"):
            for width in (72, 80, 120):
                dash = self.dash()
                dash.panel = panel
                calls = []
                dash.put = lambda y, x, text, *a, **k: calls.append(text)
                dash.s = Mock()
                dash.s.getmaxyx.return_value = (30, width)
                dash.kb_seen = dash.keyboard.last_seq
                dash.paused = False
                dash.push_history = lambda metrics: None
                dash.help = dash.show_results = dash.show_limits = False
                dash.sudo_expired = False
                dash.theme = Mock()
                dash.draw_main = dash.draw_keyboard = dash.draw_fans = dash.draw_overview = lambda *a, **k: None
                dash.draw_nav = lambda *a, **k: None
                dash.draw({"metrics": {}})
                hint = next(t for t in calls if isinstance(t, str) and t.startswith("? help"))
                self.assertLessEqual(len(hint), width - 3, (panel, width))
                self.assertTrue(hint.endswith("q quit"), (panel, width))
                if width >= 80 and panel != "overview":
                    self.assertIn("Enter apply", hint, (panel, width))
                if width == 120 and panel == "fans":
                    self.assertIn("R stock", hint)
                dash.s.getmaxyx.return_value = (30, 200)
                self.assertEqual(dash.footer_hint(197).count("  "), len(m_ui.Dashboard.HINTS[panel]) + 1)

    def test_external_mode_change_drops_staged_limits(self):
        dash = self.dash()
        dash.pending = {"ppt_pl1_spl": 60, "mode": "balanced"}
        state = self.hw.capabilities()
        state["mode"] = "performance"
        dash.handle(ord("?"), state)
        self.assertEqual(dash.pending, {"mode": "balanced"})
        self.assertIn("Staged limit edits dropped: mode changed to performance", dash.notice)


class ViewTests(unittest.TestCase):
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

    def test_summary_numbers_in_titles_and_one_graph_per_box(self):
        dash, calls = self.stub_dash(45)
        dash.history["cpu_w"].extend([20, 45, 60])
        dash.history["gpu_w"].extend([30, 80])
        metrics = {"cpu": {"temperature_c": 72, "power_w": 45.2, "clock_mhz": 4100},
                   "gpu": {"devices": [{"name": "G", "power_w": 80.1, "power_limit_w": 140,
                                        "throttle": ["sw_power_cap"]}]},
                   "throttle": {"cpu": {"active": True}}}
        dash.chart_rows = Mock()
        dash.draw_telemetry(self.state(), metrics, 1, 40, 42, 60)
        text = "\n".join(t for _, _, t in calls)
        self.assertIn("THROTTLE · 72°C · 45.2 W · 4.1 GHz", text)
        self.assertIn("80.1 W · N/A", text)  # missing GPU temperature and clock
        self.assertIn("throttle: sw_power_cap", text)
        self.assertNotIn("PL1", text)
        self.assertEqual([c.args[4] for c in dash.chart_rows.call_args_list], ["cpu_w", "gpu_w"])
        self.assertTrue(all(0 <= y < 45 and x + len(t) <= 100 for y, x, t in calls))
        calls.clear()
        dash.chart_rows.reset_mock()
        dash.draw_telemetry({}, {"cpu": {}}, 1, 40, 22, 60)  # no limits, small box
        self.assertEqual([c.args[5] for c in dash.chart_rows.call_args_list], [60])  # history max, not 1
        self.assertIn("N/A · N/A · N/A", "\n".join(t for _, _, t in calls))

    def test_cpu_power_reason_at_box_heights(self):
        reason = "RAPL needs root: run sudo loqctl serve"
        for height in (18, 22, 27, 29):
            with self.subTest(height=height):
                dash, calls = self.stub_dash(height, 80)
                dash.chart_rows = Mock()
                dash.draw_telemetry({}, {"cpu": {"power_w": None, "power_source": reason}}, 0, 0, height, 80)
                shown = [t for _, _, t in calls if reason in t]
                self.assertEqual(len(shown), 1)
        dash, calls = self.stub_dash(22, 80)
        dash.draw_telemetry({}, {"cpu": {"power_w": None, "power_source": "Waiting for RAPL samples"}}, 0, 0, 22, 80)
        self.assertNotIn("Waiting", "\n".join(t for _, _, t in calls))

    def test_details_view_draws_na(self):
        dash, calls = self.stub_dash()
        dash.draw_details({"temperatures": [{"group": "cpu", "label": "Core 0", "c": None}],
                           "power": {"rapl": {"psys": None}, "battery_w": None, "battery_status": None,
                                     "usbc": []},
                           "voltages": [{"label": "CPU 0 Vcore", "v": None}],
                           "throttle": {"cpu": {"core_events": None}, "gpu": None}}, 1, 0, 30, 60)
        text = "\n".join(t for _, _, t in calls)
        for needle in ("Temperatures", "Core 0", "psys", "CPU 0 Vcore", "Throttle", "CPU", "GPU"):
            self.assertIn(needle, text)

    def test_details_view_shows_gpu_mode(self):
        for mode, shown in (("Hybrid", "mode Hybrid"), (None, "mode N/A")):
            dash, calls = self.stub_dash()
            dash.draw_details({"gpu_mode": mode}, 1, 0, 30, 60)
            self.assertIn(shown, "\n".join(t for _, _, t in calls))

    def test_details_grid_keeps_throttle_and_whole_numbers(self):
        metrics = {"throttle": {"cpu": {"active": True, "core_events": 3, "package_events": 12}, "gpu": ["power cap"]},
                   "cpu": {"utilization_pct": 12, "clock_mhz": 3100, "max_clock_mhz": 4500, "cores": {"0": 5, "1": 90},
                           "power_source": "RAPL energy counters from sysfs"},
                   "gpu": {"devices": [{"name": "NVIDIA GeForce RTX 4050 Laptop GPU", "memory_clock_mhz": 8000,
                                        "memory_used_mib": 405, "memory_total_mib": 6144, "clock_mhz": 2100}]},
                   "power": {"rapl": {"package": 21.5, "core": 9.0}, "battery_w": 0.0, "battery_status": "Charging"},
                   "battery": [{"name": "BAT1", "health_percent": 98.1, "cycles": 77, "full_wh": 58.9, "design_wh": 60.0}],
                   "voltages": [{"label": f"Vcore {i}", "v": 1.0} for i in range(6)],
                   "temperatures": [{"group": "cpu", "label": f"Core {i}", "c": 50} for i in range(12)]}
        for h, w in ((20, 72), (24, 80)):
            with self.subTest(size=(h, w)):
                dash, calls = self.stub_dash(h, w)
                dash.draw_details(metrics, 1, 14, h - 3, w - 14)
                rows = {y: t for y, x, t in calls if x == 16}
                self.assertTrue(any("Throttle" in t and "cpu yes" in t for t in rows.values()))
                self.assertTrue(any(t == "Power" for t in rows.values()))
                self.assertTrue(any(t.strip().startswith("package") and "21.5" in t for t in rows.values()))
                self.assertTrue(any(t.strip().startswith("battery") and "battery0" not in t for t in rows.values()))
                self.assertTrue(any(t.startswith("  BAT1") and "health 98%" in t and "cycles 77" in t
                                    for t in rows.values()))
                self.assertTrue(all(y < h - 2 for y in rows))
                for t in rows.values():
                    self.assertLessEqual(len(t), w - 14 - 4)
                    self.assertNotRegex(t, r"\b(max|memory|vram) \d*$")

    def test_details_view_shows_secondary_readings(self):
        dash, calls = self.stub_dash()
        dash.draw_details({"cpu": {"utilization_pct": 12, "cores": {"0": 0, "1": 100}, "max_clock_mhz": 4500},
                           "gpu": {"devices": [{"name": "RTX 4050", "memory_total_mib": 6144,
                                                "memory_clock_mhz": 8000}]}}, 1, 0, 30, 70)
        text = "\n".join(t for _, _, t in calls)
        for needle in ("12%", "▁█", "4500 MHz", "RTX 4050", "8000 MHz", "6144 MiB"):
            self.assertIn(needle, text)
        self.assertGreaterEqual(text.count("N/A"), 6)
        calls.clear()
        dash.draw_details({"temperatures": [{"group": "cpu", "label": str(i), "c": 1} for i in range(40)]},
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
        self.assertEqual(dash.view, "summary")  # only Overview has details
        dash.panel = "overview"
        dash.handle(ord("t"), {})
        self.assertEqual(dash.view, "details")
        dash.handle(27, {})
        self.assertEqual(dash.view, "summary")
        dash.handle(ord("t"), {})
        dash.handle(ord("t"), {})
        self.assertEqual(dash.view, "summary")


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


class FitPartsTests(unittest.TestCase):
    def test_drops_trailing_parts_instead_of_cutting_a_number(self):
        parts = ["custom default 95", "low-power limit from firmware table"]
        self.assertEqual(m_ui.fit_parts(parts, 60), "custom default 95 · low-power limit from firmware table")
        self.assertEqual(m_ui.fit_parts(parts, 30), "custom default 95")
        self.assertEqual(m_ui.fit_parts(parts, 10), "custom def")


if __name__ == "__main__":
    unittest.main()

