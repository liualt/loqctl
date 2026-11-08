# SPDX-License-Identifier: GPL-3.0-or-later
import curses
import errno
import fcntl
import json
import os
import pty
import re
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

    def test_idle_run_loop_draws_only_on_change(self):
        dashboard = self.dashboard()
        dashboard.running = True
        dashboard.paused = False
        dashboard.notice = "Ready."
        dashboard.keyboard = Mock(last_seq=0)
        dashboard.backend.start = dashboard.backend.close = lambda: None
        stamp = time.time()
        dashboard.backend.state = lambda: {"mode": "custom", "metrics": {"timestamp": stamp}}
        keys = iter([-1, -1, -1, ord("x"), -1])
        def getch():
            try:
                return next(keys)
            except StopIteration:
                dashboard.running = False
                return -1
        dashboard.s = Mock(getch=getch, getmaxyx=lambda: (40, 120))
        dashboard.MIN_H = dashboard.MIN_W = 1
        dashboard.draw = Mock()
        dashboard.handle = Mock()
        dashboard.run()
        # First frame, then one redraw after the key press; idle timeouts draw nothing.
        self.assertEqual(dashboard.draw.call_count, 2)

    def test_frozen_timestamp_still_adopts_fan_result(self):
        dashboard = self.dashboard()
        dashboard.running = True
        dashboard.paused = False
        dashboard.notice = "Ready."
        dashboard.fan_result = None
        dashboard.keyboard = Mock(last_seq=0)
        dashboard.backend.start = dashboard.backend.close = lambda: None
        stamp = time.time()
        dashboard.backend.state = lambda: {"mode": "custom", "metrics": {"timestamp": stamp}}
        passes = []
        def getch():
            passes.append(1)
            if len(passes) == 2:
                dashboard.fan_result = {"curve": []}  # the loader thread finished
            if len(passes) == 4:
                dashboard.running = False
            return -1
        dashboard.s = Mock(getch=getch, getmaxyx=lambda: (40, 120))
        dashboard.MIN_H = dashboard.MIN_W = 1
        dashboard.draw = Mock()
        dashboard.run()
        self.assertEqual(dashboard.draw.call_count, 2)

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

    def test_locked_rows_stay_readable(self):
        self.hw.set_mode("balanced")
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.theme = Mock(TEXT=7, ACCENT=1, WARN=3)
        calls = []
        dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append((text, color, dim))
        dashboard.box = Mock()
        dashboard.draw_controls(state, 0, 0, 20, 50)
        row = next(c for c in calls if "CPU sustained" in c[0])
        self.assertFalse(row[2])  # not dimmed: A_DIM is near invisible in many terminals
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
                        self.assertIn(f"{mode} → performance", lines[1])
                    elif mode == "custom" or rows[sel] in ("ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp"):
                        self.assertTrue(any(t == "▶" for y, t in calls if y > 1), (mode, h, w, sel))
                    if mode == "balanced":
                        self.assertLessEqual(sum(t in ("▶", " ") for y, t in calls if y > 1), 4)
                        self.assertTrue(any("custom" in t and "edit" in t for t in lines.values()))

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
        for _ in range(4):
            calls = []
            dashboard.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append(str(text))
            dashboard.draw_controls(state, 0, 0, 17, 58)
            self.assertTrue(any(t.startswith("▶") for t in calls), rows[dashboard.selected])
            visited.append(rows[dashboard.selected])
            if len(visited) < 4:
                dashboard.handle(curses.KEY_DOWN, state)
        self.assertEqual(visited, ["mode", "ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp"])

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
            dashboard.draw_controls(state, 0, 0, 20, 60)  # too narrow for the range column: the detail strip has it
            detail = next(text for text, _ in calls if "step" in text or "default" in text)
            for part in expected:
                self.assertIn(part, detail, unreadable)
            self.assertNotIn("None", detail, unreadable)

    def test_help_lists_only_keys_that_work_now(self):
        dash = self.dash()
        dash.fan_init()
        dash.keyboard = Mock(available=True)
        dash.panel = "main"
        editable = "\n".join(dash.help_lines({"mode": "custom"}))
        locked = "\n".join(dash.help_lines({"mode": "balanced"}))
        self.assertIn("0-9", editable)
        self.assertNotIn("0-9", locked)  # no typing in a stock mode
        self.assertIn("custom mode only", locked)
        dash.panel = "fans"  # no curve loaded: the page has no keys of its own
        missing = dash.help_lines({"mode": "custom"})
        self.assertEqual(missing[-1], "Fan curves need the legion_laptop module.")
        self.assertFalse(any(line.startswith(("←→", "↑↓", "Enter")) for line in missing))

    def test_invalid_value_says_the_allowed_range(self):
        self.add_attr("ppt_pl2_sppt", 60, 50, 100, 5, 60)
        state = self.hw.capabilities()
        dashboard = self.dashboard()
        dashboard.set_field(state, "ppt_pl2_sppt", "61")
        self.assertEqual(dashboard.notice, "61 W is not allowed. Use 50–100 W in steps of 5.")
        self.assertIn("ppt_pl2_sppt", dashboard.invalid)

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
        self.type_keys(dashboard, state, "6", "0", curses.KEY_DOWN, "8", "5", curses.KEY_BACKSPACE, 127, curses.KEY_DOWN, "5", "1")
        self.assertEqual(dashboard.pending, {"ppt_pl1_spl": 60})
        self.assertEqual(dashboard.invalid, {"gpu_temp"})
        self.assertEqual(dashboard.fields, {"ppt_pl1_spl": "60", "gpu_temp": "51"})

    def test_enter_refuses_with_invalid_and_jumps(self):
        dashboard, state = self.multi()
        dashboard.selected = 3
        self.type_keys(dashboard, state, "5", "1", curses.KEY_UP, curses.KEY_UP)
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
        self.assertIn("1 staged, 1 invalid", dashboard.box.call_args_list[0][0])
        row = next(c for c in calls if "→ 60_" in c[0])
        self.assertEqual(row[1], 1)
        staged = next(c for c in calls if "CPU" in c[0].upper() and "→ 90" in c[0])
        self.assertEqual(staged[1], 3)
        draw("gpu_temp")
        row = next(c for c in calls if "→ 7_" in c[0])
        self.assertEqual(row[1], 5)

    def test_empty_state_does_not_crash_handle(self):
        dashboard = self.dashboard()
        dashboard.panel = "main"
        dashboard.keyboard = m_keyboard.Keyboard(binary="/usr/bin/false",
                                          config=self.root / "missing.json")
        dashboard.handle(ord("x"), {})
        dashboard.handle(9, {})
        self.assertEqual(dashboard.panel, "fans")
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

    def test_recursive_sudo_output_raises_control_error(self):
        dash = self.dash()
        with patch.object(m_ui.Dashboard, "sudo_call", return_value=("[[[", "")), \
                patch.object(m_ui.json, "loads", side_effect=RecursionError):
            for run in (dash.run_sudo_apply, dash.run_sudo_fancurve):
                with self.assertRaises(m_common.ControlError):
                    run([])

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

    def test_help_key_opens_from_every_panel(self):
        for panel in ("overview", "main", "keyboard", "fans"):
            dash = self.dash()
            dash.panel = panel
            dash.handle(ord("?"), {})
            self.assertTrue(dash.help, panel)
            dash.handle(ord("?"), {})
            self.assertFalse(dash.help, panel)
            self.assertEqual(dash.panel, panel)

    def test_duplicate_keys_are_gone(self):
        state = self.hw.capabilities()
        for panel, keys in (("main", "jk+-=lf"), ("keyboard", "jk+-=l")):
            for key in keys:
                dash = self.dash()
                dash.panel, dash.selected, dash.kb_channel, dash.kb_zone = panel, 1, 1, 1
                dash.keyboard.available = True
                dash.handle(ord(key), state)
                self.assertEqual((dash.panel, dash.selected, dash.pending, dash.kb_channel),
                                 (panel, 1, {}, 1), (panel, key))

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

    def test_tab_leaves_every_page_and_draws_each_at_small_sizes(self):
        state = {"metrics": {}, "attributes": {}, "modes": ["custom"], "mode": "custom"}
        point = dict(cpu_up=40, cpu_down=0, gpu_up=40, gpu_down=0, ic_up=40, ic_down=0, accel=1, decel=1)
        loaded = {"available": True, "mode": "custom",
                  "points": [dict(point, fan1=f, fan2=f) for f in (0, 80, 180)]}
        for h, w in ((20, 72), (24, 80)):
            for key in (9, curses.KEY_BTAB):
                puts = []
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append((y, x, text[:n]))
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
                dash.keyboard = m_keyboard.Keyboard(binary="/nonexistent/legionaura", config=self.root / "legionaura.json")
                dash.sections = lambda state: ["overview", "main", "fans", "keyboard"]
                dash.panel, dash.paused = "overview", True
                seen = []
                for _ in range(4):
                    dash.fan, dash.fan_result, dash.fan_loading = loaded, None, False
                    dash.fan_sel = 1
                    dash.draw(state)
                    self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w, dash.panel))
                    seen.append(dash.panel)
                    dash.handle(key, state)
                    self.assertNotEqual(dash.panel, seen[-1], (h, w, key, seen))
                    self.assertEqual(dash.fan_sel, 1, (h, w, key, seen))
                self.assertEqual(sorted(seen), ["fans", "keyboard", "main", "overview"], (h, w, key))

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

    def test_extra_limits_are_drawn_read_only_on_small_screens(self):
        template = {"label": "x", "unit": "W", "available": True, "current_value": 55, "min_value": 5,
                    "max_value": 140, "scalar_increment": 1, "default_value": 50, "effective_value": 55}
        attrs = {k: {**template, "label": m_common.FIELDS[k][0]} for k in m_common.FIELDS if k != "ppt_pl1_tau"}
        extras = [{"key": "cpu_l1_tau", "label": "CPU Tau", "unit": "s", "value": 56},
                  {"key": "gpu_ppab_powerlimit", "label": "GPU PPAB", "unit": "W", "value": 10}]
        for h, w in ((20, 72), (24, 80)):
            grid = [[" "] * w for _ in range(h)]
            screen = Mock()
            screen.getmaxyx.return_value = (h, w)
            screen.addnstr.side_effect = lambda y, x, text, n, attr=0: grid[y].__setitem__(
                slice(x, x + len(text[:n])), text[:n])
            with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=False):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
            dash.panel = "main"
            state = {"metrics": {}, "attributes": attrs, "extra_limits": extras, "mode": "custom",
                     "modes": ["balanced", "custom"], "mode_limits": None}
            dash.selected = len(dash.rows(state)) - 1
            dash.draw(state)
            text = "\n".join("".join(r) for r in grid)
            self.assertRegex(text, r"CPU Tau\s+56 s", (h, w))
            self.assertRegex(text, r"↑ \d+ more", (h, w))  # scrolled to the end: the cue says rows sit above
            self.assertNotIn("GPU PPAB", text)  # the same limit is already the GPU dynamic boost row
            self.assertEqual(dash.rows(state), ["mode"] + list(attrs))

    def test_power_page_status_mode_row_and_overlay_fit(self):
        modes = ["low-power", "balanced", "performance", "max-power", "custom"]
        item = {"unit": "W", "available": True, "current_value": 55, "effective_value": None, "min_value": 5,
                "max_value": 140, "scalar_increment": 1, "default_value": 55}
        attrs = {k: {**item, "label": m_common.FIELDS[k][0]} for k in m_common.FIELDS}
        for h, w in ((20, 72), (24, 80)):
            for case in ("page", "overlay", "error"):
                grid = [[" "] * w for _ in range(h)]

                def addnstr(y, x, text, n, attr=0):
                    for i, c in enumerate(text[:n]):
                        grid[y][x + i] = c
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = addnstr
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=False), sudo_ok=False)
                dash.keyboard = Mock(last_seq=0)
                dash.notice = ""
                dash.panel, dash.show_limits = "main", case == "overlay"
                state = {"metrics": {}, "attributes": attrs, "modes": modes, "mode": "balanced",
                         "mode_limits": None, "mode_limits_error": "needs root"}
                if case == "error":
                    state["error"] = "Could not read firmware attributes"
                with patch.object(os, "geteuid", return_value=1000):
                    dash.draw(state)
                text = ["".join(r) for r in grid]
                joined = "\n".join(text)
                self.assertIn("Stock limits need root once per boot", joined, (h, w, case))
                if case == "overlay":
                    row = next(t for t in text if "Control" in t)
                    for head in ("low", "[bal]", "perf", "max", "custom"):
                        self.assertIn(head, row, (h, w))
                    self.assertNotIn("╭─╭", joined)
                    self.assertIn("Enter / Esc / m closes", joined)
                    self.assertTrue(text[h - 2].startswith(" Read-only"), (h, w))  # box stays off the status row
                else:
                    self.assertRegex(joined, r"Mode\s+low\s+\[balanced\]\s+perf\s+max\s+custom")
                    self.assertEqual(joined.count("custom mode only"), 1, (h, w))
                    status = text[h - 2]
                    self.assertIn("Error" if case == "error" else "Read-only", text[1])
                    if case == "error":
                        self.assertIn("Could not read firmware attributes", status)
                        self.assertNotIn("Press s", status)
                    else:
                        self.assertIn("Read-only. Press s to unlock.", status)
                dash.panel, dash.show_limits = "overview", False
                grid[:] = [[" "] * w for _ in range(h)]
                dash.draw(state)
                self.assertNotIn("Press s to unlock", "".join(grid[h - 2]))

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
                    self.assertIn("custom mode", text[0], (h, w))
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
                    "health_percent": 98.13, "cycles": 77, "full_wh": 58.88, "design_wh": 60.0,
                    "time_remaining_min": 135, "time_kind": "to empty"}]
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
                    self.assertTrue(any("2 h 15 min left" in t for t in texts), (h, w))
                    self.assertTrue(any("limit 80%" in t for t in texts), (h, w))
                else:
                    self.assertTrue(any(t.strip().startswith("BAT1") and "cycles 77" in t for t in texts), (h, w))
                self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w, view))

    def test_wide_overview_has_sensors_column_and_temperature_graphs(self):
        metrics = {"timestamp": time.time(), "cpu": {"temperature_c": 61.0, "power_w": 20.0},
                   "gpu": {"devices": [{"temperature_c": 50, "power_w": 5.0}]},
                   "temperatures": [{"group": "cpu", "label": "Core 0", "c": 55.0},
                                    {"group": "cpu", "label": "Package id 0", "c": 63.0},
                                    {"group": "nvme", "label": "Composite", "c": 40.0}],
                   "fans": [{"name": "Fan 1", "rpm": 2600.0}], "battery": [{"percent": 80.0, "status": "Charging"}],
                   "ac_online": True, "memory": {"used_mib": 4096, "total_mib": 16384}, "load": [0.5, 0.4, 0.3]}
        state = {"mode": "custom", "metrics": metrics,
                 "attributes": {"cpu_temp": {"label": "CPU temperature limit", "unit": "°C", "effective_value": 95}}}
        for (h, w), wide in (((40, 120), True), ((24, 80), False)):
            puts = []
            screen = Mock()
            screen.getmaxyx.return_value = (h, w)
            screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append((y, x, text[:n]))
            with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=False):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
            dash.panel = "overview"
            dash.draw(state)
            text = "\n".join(t for _, _, t in puts)
            self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w))
            self.assertEqual(" Sensors " in text, wide, (h, w))
            self.assertEqual("limit 95" in text, wide, (h, w))  # the CPU temperature graph marks the throttle limit
            if wide:  # one row per sensor group, holding its hottest reading
                self.assertIn("63°C", text)
                self.assertNotIn("55°C", text)
            self.assertNotIn("limit N/A", text)  # an unknown charge limit is left out, not shown as N/A
            self.assertNotIn("AC N/A", text)

    def test_paused_and_stale_are_marked_on_screen(self):
        for h, w in ((20, 72), (24, 80)):
            for paused, stale, panel in ((True, False, "main"), (False, True, "main"), (True, False, "overview"),
                                         (False, False, "fans"), (False, False, "keyboard")):
                puts = []
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append((y, x, text[:n]))
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
                dash.paused, dash.notice, dash.panel = paused or panel in ("fans", "keyboard"), "Ready.", panel
                dash.draw({"metrics": {}, "attributes": {}, "modes": [], "stale": stale})
                texts = [t for _, _, t in puts]
                self.assertEqual(any("graphs paused" in t for t in texts), paused, (h, w))
                self.assertEqual(any(t.startswith("Firmware values are stale.") for t in texts), stale, (h, w))
                self.assertTrue(all(0 <= y < h and x + len(t) <= w for y, x, t in puts), (h, w))

    def draw_rows(self, h, w, panel, state, expired=False, fan=None):
        puts = {}
        screen = Mock()
        screen.getmaxyx.return_value = (h, w)
        screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.__setitem__((y, x), text[:n])
        with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                patch.object(curses, "has_colors", return_value=False):
            dash = m_ui.Dashboard(screen, Mock(notice="", remote=False), sudo_ok=not expired)
        dash.panel, dash.paused = panel, True
        if fan:
            dash.fan = fan
        with patch.object(m_ui.os, "geteuid", return_value=1000):
            dash.draw({"metrics": {}, "attributes": {}, "modes": [], **state})
        return ["".join(t for (y, x), t in sorted(puts.items()) if y == row) for row in range(h)]

    def test_idle_notice_is_empty_and_warnings_stand_alone(self):
        for h, w in ((20, 72), (24, 80)):
            for panel in ("overview", "fans", "keyboard"):
                self.assertNotIn("Ready", self.draw_rows(h, w, panel, {})[h - 2], (h, w, panel))
            locked = {"mode": "balanced"}
            rows = self.draw_rows(h, w, "main", locked, expired=True)
            self.assertIn("Read-only. Press s to unlock.", rows[h - 2], (h, w))
            self.assertIn("s unlock", rows[h - 1], (h, w))
            rows = self.draw_rows(h, w, "fans", locked, expired=True)  # no curve: nothing to unlock
            self.assertEqual(rows[h - 2].strip(), "", (h, w))  # the page says what is missing; the status line does not repeat it
            self.assertNotIn("unlock", rows[h - 1], (h, w))
            rows = self.draw_rows(h, w, "main", {**locked, "stale": True}, expired=True)
            self.assertEqual(rows[h - 2].strip(), "Firmware values are stale. Wait for the next refresh.")
            rows = self.draw_rows(h, w, "main", {"error": "Backend down"})
            self.assertIn("Backend down. Run loqctl doctor for details.", rows[h - 2])
            self.assertEqual(rows[h - 1].strip(), "? help  Tab page  q quit")  # no dead keys while the backend is down

    def test_power_page_survives_an_empty_state(self):
        for h, w in ((20, 72), (24, 80)):
            rows = self.draw_rows(h, w, "main", {"attributes": None, "mode": None})
            self.assertIn("Mode unreadable", "".join(rows))

    def test_missing_load_and_uptime_draw_without_error(self):
        for h, w in ((20, 72), (24, 80)):
            rows = self.draw_rows(h, w, "overview", {"metrics": {"load": [None, None], "uptime_s": None}})
            self.assertEqual(len(rows), h)

    def test_header_names_the_curve_mode_when_firmware_mode_is_unknown(self):
        fan = {"available": True, "mode": "balanced", "points": [{"cpu_up": 80, "fan1": 40}]}
        for h, w in ((20, 72), (24, 80)):
            head = self.draw_rows(h, w, "fans", {"mode": None}, fan=fan)[0]
            self.assertIn("balanced mode", head)
            self.assertNotIn("Waiting for firmware", head)

    def test_footer_lists_only_working_keys(self):
        for h, w in ((20, 72), (24, 80)):
            foot = self.draw_rows(h, w, "fans", {"mode": "balanced"})[h - 1]
            self.assertEqual(foot.strip(), "? help  Tab page  q quit")
            self.assertNotIn("Enter apply", foot)
            foot = self.draw_rows(h, w, "main", {"mode": "balanced"})[h - 1]
            self.assertIn("←→ mode", foot)
            self.assertNotIn("Enter apply", foot)
            foot = self.draw_rows(h, w, "keyboard", {})[h - 1]
            self.assertIn("1-4 zone", foot)
            self.assertNotIn("w wave", foot)
            self.assertNotIn("terminal swatch", "".join(self.draw_rows(h, w, "keyboard", {})))

    def test_battery_charge_type_drops_before_limit(self):
        battery = [{"name": "BAT1", "percent": 90, "status": "Discharging", "charge_limit": "80%",
                    "charge_type": "Long_Life", "rapid_charge": "off", "time_remaining_min": 135,
                    "time_kind": "to empty", "health_percent": 93}]
        for h, w in ((20, 72), (24, 80)):
            puts = []
            screen = Mock()
            screen.getmaxyx.return_value = (h, w)
            screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append(text[:n])
            with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=False):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
            dash.panel, dash.view = "overview", "summary"
            dash.draw({"metrics": {"battery": battery, "ac_online": False}, "attributes": {}, "modes": []})
            self.assertTrue(any("limit 80%" in t for t in puts), (h, w))
            self.assertFalse(any("charge Long Life" in t for t in puts), (h, w))

    def test_battery_health_shows_when_plugged_in(self):
        battery = [{"name": "BAT1", "percent": 90, "status": "Not charging", "charge_limit": "80%",
                    "charge_type": "Long_Life", "health_percent": 98.13, "time_remaining_min": None, "time_kind": None}]
        for h, w in ((20, 72), (24, 80)):
            puts = []
            screen = Mock()
            screen.getmaxyx.return_value = (h, w)
            screen.addnstr.side_effect = lambda y, x, text, n, attr=0: puts.append(text[:n])
            with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=False):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
            dash.panel, dash.view = "overview", "summary"
            dash.draw({"metrics": {"battery": battery, "ac_online": True}, "attributes": {}, "modes": []})
            self.assertTrue(any("health 98%" in t for t in puts), (h, w))
            self.assertTrue(any("limit 80%" in t for t in puts), (h, w))
            if w == 80:
                self.assertTrue(any("charge Long Life" in t for t in puts), (h, w))

    def test_battery_left_text(self):
        left = m_ui.battery_left
        self.assertEqual(left({"time_remaining_min": 40, "time_kind": "to full"}), "40 min to full")
        self.assertEqual(left({"time_remaining_min": 135, "time_kind": "to empty"}), "2 h 15 min left")
        self.assertEqual(left({"time_remaining_min": None}), "")

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
        self.assertIn("Get it from github.com/nivedck/LegionAura.", rows)
        with patch.object(m_keyboard.Keyboard, "available", new_callable=PropertyMock, return_value=True):
            dash.draw({"metrics": {}, "attributes": {}, "modes": []})
        rows = ["".join(r)[m_ui.Dashboard.NAV_W + 1:].strip("│ ") for r in grid]
        tip = next(r for r in rows if r.startswith("Hardware"))
        self.assertIn(tip, ("Hardware shows the true color. UI uses your terminal theme only.",
                            "Hardware shows the true color."))
        self.assertIn("zone 1  HSV picker", rows)
        self.assertFalse(any("swatch" in r for r in rows))

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

    def test_s_authenticates_on_every_page(self):
        state = self.hw.capabilities()
        for panel in ("fans", "keyboard"):
            dash = self.dash()
            dash.panel = panel
            dash.fan = {"available": False}
            dash.keyboard.available = True
            dash.backend.remote = False
            calls = []
            dash.reauth = lambda: calls.append(panel)
            with patch("os.geteuid", return_value=1000):
                dash.handle(ord("s"), state)
            self.assertEqual(calls, [panel])

    def test_fan_graph_shows_percent_and_step_temperatures(self):
        from loqctl.fancurve import Scale
        dash = self.dash()
        dash.fan_sel = 0
        dash.theme = Mock(TEXT=7, ACCENT=1, WARN=3, INFO=6, BAD=2)
        calls = []
        dash.put = lambda y, x, text, color=0, bold=False, dim=False: calls.append(str(text))
        points = [{"cpu_up": t, "fan1": 40 + i} for i, t in enumerate((88, 92, 92, 99, 100))]
        dash.fan_graph(points, Scale("rpm", 10000), 0, 0, 60, 8)
        self.assertIn("100%", calls)
        self.assertEqual(calls.count("●") + calls.count("◉"), 5)
        self.assertEqual(calls.count("◉"), 1)  # the selected step has its own glyph
        self.assertEqual([c for c in calls if c in ("88", "92", "99", "100")], ["88", "92", "99", "100"])  # a run of 92s reads once
        self.assertIn("  °C", calls)  # the tick row is labelled once
        self.assertFalse(any("RPM" in c for c in calls))

    def test_arrows_leave_power_and_keyboard_at_the_edges(self):
        dash = self.dash()
        dash.fan = {"available": False}
        dash.keyboard.available = True
        dash.goto_section = lambda name: setattr(dash, "panel", name)  # opening a section reads hardware
        state = self.hw.capabilities()
        visit = dash.shown_rows(state)
        rows = dash.rows(state)
        dash.panel = "main"
        dash.selected = rows.index(visit[0])
        dash.handle(curses.KEY_UP, state)
        self.assertEqual(dash.panel, "overview")
        dash.panel = "main"
        dash.selected = rows.index(visit[-1])
        dash.handle(curses.KEY_DOWN, state)
        self.assertEqual(dash.panel, "fans")
        dash.panel, dash.kb_channel = "keyboard", 2
        dash.handle(curses.KEY_DOWN, state)
        self.assertEqual(dash.panel, "overview")
        dash.panel, dash.kb_channel = "keyboard", 0
        dash.handle(curses.KEY_UP, state)
        self.assertEqual(dash.panel, "fans")

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
        dash.draw_telemetry(self.state(), metrics, 1, 20, 42, 70)
        text = "\n".join(t for _, _, t in calls)
        self.assertIn("throttling · 72°C · 45.2 W · 4.1 GHz", text)
        self.assertIn("temp N/A · 80.1 W", text)  # a missing reading says what it is; no clock, no slot
        self.assertIn("throttle: sw_power_cap", text)
        self.assertIn("PL1 55", text)
        self.assertEqual([c.args[4] for c in dash.chart_rows.call_args_list], ["cpu_w", "gpu_w"])
        self.assertTrue(all(0 <= y < 45 and x + len(t) <= 100 for y, x, t in calls))
        calls.clear()
        dash.chart_rows.reset_mock()
        dash.draw_telemetry({}, {"cpu": {}}, 1, 40, 22, 60)  # no limits, small box
        self.assertEqual([c.args[5] for c in dash.chart_rows.call_args_list], [60])  # history max, not 1
        self.assertIn("temp N/A · power N/A", "\n".join(t for _, _, t in calls))

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

    def test_overview_keeps_fans_and_explains_no_data_at_small_sizes(self):
        metrics = {"cpu": {"power_w": None, "power_source": "Waiting for RAPL samples"}, "ac_online": True,
                   "fans": [{"name": "fan1", "rpm": 2100}], "load": [0.5, 0.4, 0.3], "uptime_s": 4000,
                   "battery": [{"percent": 80, "status": "Charging", "charge_limit": "80%"}]}
        for h, w in ((20, 72), (24, 80)):
            for data, wanted in ((metrics, ("fan1 2100 RPM", "limit 80%", "0-")), ({}, ("No sensor data yet. Run loqctl doctor.",))):
                grid = [[" "] * w for _ in range(h)]
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = lambda y, x, text, n, attr=0: grid[y].__setitem__(
                    slice(x, x + len(text[:n])), text[:n])
                with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                        patch.object(curses, "has_colors", return_value=False):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
                dash.panel, dash.view = "overview", "summary"
                dash.draw({"metrics": data, "attributes": {}, "modes": []})
                text = "\n".join("".join(r) for r in grid)
                for needle in wanted:
                    self.assertIn(needle, text, (h, w))
                if data:
                    self.assertNotIn("N/A W", text)
                    self.assertEqual(text.count("load 0.50"), h >= 24)

    def full_screen(self, h, w, panel, state):
        grid = [[" "] * w for _ in range(h)]
        screen = Mock()
        screen.getmaxyx.return_value = (h, w)
        screen.addnstr.side_effect = lambda y, x, text, n, attr=0: grid[y].__setitem__(slice(x, x + len(text[:n])), text[:n])
        with patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                patch.object(curses, "has_colors", return_value=False):
            dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
        dash.panel, dash.paused = panel, True
        dash.history["cpu_w"].extend([20, 45, 60] * 20)
        dash.draw(state)
        return dash, "\n".join("".join(r) for r in grid)

    def test_overview_labels_chart_span_limits_and_header_at_all_sizes(self):
        metrics = {"cpu": {"temperature_c": 60, "power_w": 45.0}, "ac_online": True, "timestamp": time.time()}
        state = {"metrics": metrics, "attributes": self.state()["attributes"], "model": "LOQ 15", "mode": "custom", "modes": []}
        for h, w in ((20, 72), (24, 80), (40, 120)):
            _, text = self.full_screen(h, w, "overview", state)
            for needle in ("PL1 55", "PL2 115", " s", "AC power"):
                self.assertIn(needle, text, (h, w, needle))
        _, text = self.full_screen(24, 80, "overview", {"metrics": {}, "attributes": {}, "modes": []})
        self.assertIn("Waiting for firmware", text)
        self.assertNotIn("N/A  ·", text)

    def test_power_page_shows_position_labels_and_live_strip_at_all_sizes(self):
        attrs = {k: {"label": m_common.FIELDS[k][0], "unit": m_common.FIELDS[k][1], "effective_value": 50, "current_value": 50,
                     "min_value": 1, "max_value": 99, "default_value": 40, "scalar_increment": 1, "read_error": None}
                 for k in m_common.FIELDS}
        metrics = {"cpu": {"temperature_c": 61, "power_w": 33.0}, "gpu": {"devices": [{"temperature_c": 55, "power_w": 20.0}]}}
        state = {"metrics": metrics, "attributes": attrs, "model": "LOQ", "mode": "custom",
                 "modes": ["quiet", "balanced", "performance", "custom"]}
        for h, w in ((20, 72), (24, 80), (40, 120)):
            dash = None
            with patch.object(m_ui.Dashboard, "rows", lambda self, st: ["mode"] + list(attrs)), \
                    patch.object(m_ui.Dashboard, "shown_rows", lambda self, st: ["mode"] + list(attrs)):
                dash, text = self.full_screen(h, w, "main", {**state})
            self.assertIn("Mode", text, (h, w))
            self.assertNotIn("gpu_temp", text)
            self.assertRegex(text, r"CPU sustained / PL1\s+50 W")
            if h < 26:
                self.assertRegex(text, r"↓ \d+ more", (h, w))
            else:  # every row fits, with the live CPU graph and its limits under them
                self.assertIn("CPU 61°C · 33.0 W", text)
                self.assertNotIn(" more", text)

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

    def test_details_view_survives_entries_missing_keys(self):
        dash, calls = self.stub_dash()
        dash.draw_details({"battery": [{}], "temperatures": [{}], "voltages": [{}], "power": {"usbc": [{}]}}, 1, 0, 30, 60)
        self.assertIn("Temperatures", "\n".join(t for _, _, t in calls))

    def test_power_list_stays_above_the_detail_strip_in_a_short_box(self):
        item = {"unit": "W", "available": True, "current_value": 55, "effective_value": 55, "min_value": 5,
                "max_value": 140, "scalar_increment": 1, "default_value": 55}
        attrs = {k: {**item, "label": m_common.FIELDS[k][0]} for k in m_common.FIELDS}
        dash, calls = self.stub_dash(20, 72)
        dash.selected, dash.show_limits, dash.keyboard = 0, False, Mock(last_seq=0)
        dash.box = Mock()
        dash.notice = ""
        state = {"metrics": {}, "attributes": attrs, "modes": [], "mode": "custom"}
        with patch.object(os, "geteuid", return_value=1000):
            dash.draw_controls(state, 0, 0, 7, 72)
        labels = [v[0] for v in m_common.FIELDS.values()]
        low = [(y, t) for y, x, t in calls if y >= 7 - 4 and any(t.strip().startswith(label) for label in labels)]
        self.assertEqual(low, [])  # list rows stop above the 4-row detail strip

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


class NoColorTests(unittest.TestCase):
    def test_no_color_marks_state_by_attribute(self):
        t = m_ui.Theme(False)
        self.assertEqual(t.attr(t.BAD), curses.A_BOLD | curses.A_REVERSE)
        self.assertEqual(t.attr(t.WARN), curses.A_BOLD)
        self.assertEqual(t.attr(t.OK), 0)
        self.assertEqual(t.attr(t.ACCENT, bold=True), curses.A_BOLD)

    def test_no_color_env_disables_theme(self):
        for env, enabled in (({"NO_COLOR": "1"}, False), ({"NO_COLOR": ""}, True)):
            screen = Mock()
            screen.getmaxyx.return_value = (24, 80)
            with patch.dict(os.environ, env), patch.object(curses, "curs_set"), patch.object(curses, "meta"), \
                    patch.object(curses, "has_colors", return_value=True), \
                    patch.object(curses, "start_color"), patch.object(curses, "use_default_colors"), \
                    patch.object(curses, "init_pair"):
                dash = m_ui.Dashboard(screen, Mock(notice="", remote=False))
            self.assertEqual(dash.theme.enabled, enabled)

    def test_every_panel_fits_and_marks_state_without_colors(self):
        modes = ["quiet", "balanced", "performance", "custom"]
        template = {"label": "x", "unit": "W", "available": True, "current_value": 55, "min_value": 5,
                    "max_value": 140, "scalar_increment": 1}
        attrs = {k: {**template, "label": m_common.FIELDS[k][0]} for k in m_common.FIELDS}
        state = {"metrics": {"temperatures": [{"group": "cpu", "label": "Package", "c": 95},
                                                  {"group": "gpu", "label": "Core", "c": 70}]}, "attributes": attrs, "modes": modes, "mode": "custom",
                 "mode_limits": None}
        for h, w in ((20, 72), (24, 80)):
            for panel, view in (("overview", "summary"), ("overview", "details"), ("main", "summary"),
                                ("fans", "summary"), ("keyboard", "summary")):
                cells = []
                screen = Mock()
                screen.getmaxyx.return_value = (h, w)
                screen.addnstr.side_effect = lambda y, x, text, n, attr=0: cells.append((x, text[:n], attr))
                with patch.dict(os.environ, {"NO_COLOR": "1"}), patch.object(curses, "curs_set"), \
                        patch.object(curses, "meta"), patch.object(curses, "has_colors", return_value=True):
                    dash = m_ui.Dashboard(screen, Mock(notice="", remote=True))
                dash.panel, dash.view = panel, view
                dash.show_results = panel == "main"
                dash.apply_results = [{"key": "ppt_pl1_spl", "before": 1, "requested": 2, "current": 1,
                                       "status": "FAILED"}]
                dash.draw(state)
                self.assertTrue(all(x + len(t) <= w for x, t, _ in cells), (panel, view, h, w))
                self.assertTrue(all(a & curses.A_COLOR == 0 for _, _, a in cells), (panel, view, h, w))
                if panel == "main":
                    self.assertTrue(any("FAILED" in t and a == curses.A_BOLD | curses.A_REVERSE
                                        for _, t, a in cells), (h, w))
                if panel == "overview" and view == "details" and h == 24:
                    self.assertTrue(any("95" in t and a == curses.A_BOLD | curses.A_REVERSE
                                        for _, t, a in cells), (h, w))
                    self.assertTrue(any("70" in t and a == curses.A_BOLD for _, t, a in cells), (h, w))


class FitPartsTests(unittest.TestCase):
    def test_drops_trailing_parts_instead_of_cutting_a_number(self):
        parts = ["custom default 95", "low-power limit from firmware table"]
        self.assertEqual(m_ui.fit_parts(parts, 60), "custom default 95 · low-power limit from firmware table")
        self.assertEqual(m_ui.fit_parts(parts, 30), "custom default 95")
        self.assertEqual(m_ui.fit_parts(parts, 10), "custom def")


if __name__ == "__main__":
    unittest.main()

