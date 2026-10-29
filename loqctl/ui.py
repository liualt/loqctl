# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Curses dashboard."""
from __future__ import annotations

import errno
import json
import os
import subprocess
import time

from . import VERSION
from .api import http_client
from .capdata import effective_value
from .colors import hex_to_rgb, hsv_to_rgb, nearest_ansi, rgb_to_hex, rgb_to_hsv
from .common import (BAR_EMPTY, BAR_FILL, FIELDS, SPARK, ControlError, SudoExpired, _lazy, clamp, fit_parts, fmt,
                     sudo_run, unit_sep)
from .hardware import GAMEZONE, check_changes, run_changes
from .keyboard import Keyboard
from .ui_fans import FanPanel

collections = _lazy("collections")
curses = _lazy("curses")
textwrap = _lazy("textwrap")

def strike(text):
    return "".join(c + "\u0336" for c in str(text))


def limit_note(state, items):
    """Why limits read N/A: the first non-EINVAL read error among items, else the gamezone note, else None.

    EINVAL is what a driver holding the gamezone WMI device causes; any other error is shown as it is."""
    for key, item in items.items():
        if item.get("read_error") and item.get("read_errno") != errno.EINVAL:
            return f"{key}: current value unreadable ({item['read_error']})"
    holder = state.get("gamezone_holder")
    if not holder:
        return None
    where = "not bound to any driver" if holder == "no driver" else f"held by {holder}"
    return f"Gamezone WMI device {where}, limits can't be read; see README"


class Theme:
    """UI colors from the terminal palette only. Never redefines RGB."""

    ACCENT = 1
    OK = 2
    WARN = 3
    BAD = 4
    GPU = 5
    INFO = 6
    TEXT = 7

    def __init__(self, enabled):
        self.enabled = enabled
        if not enabled:
            return
        curses.start_color()
        curses.use_default_colors()
        for pair, fg in (
            (self.ACCENT, curses.COLOR_CYAN),
            (self.OK, curses.COLOR_GREEN),
            (self.WARN, curses.COLOR_YELLOW),
            (self.BAD, curses.COLOR_RED),
            (self.GPU, curses.COLOR_MAGENTA),
            (self.INFO, curses.COLOR_BLUE),
            (self.TEXT, curses.COLOR_WHITE),
        ):
            curses.init_pair(pair, fg, -1)

    def attr(self, color=0, bold=False, dim=False):
        if not self.enabled or not color:
            base = 0
        else:
            base = curses.color_pair(color)
        if bold:
            base |= curses.A_BOLD
        if dim:
            base |= curses.A_DIM
        return base

    def heat(self, value, cool=60, hot=85):
        if value is None:
            return self.TEXT
        if value < cool:
            return self.OK
        if value < hot:
            return self.WARN
        return self.BAD

    def load(self, value):
        if value is None:
            return self.TEXT
        if value < 50:
            return self.OK
        if value < 80:
            return self.WARN
        return self.BAD

    def ansi_pair(self, ansi_color):
        """Map a raw curses.COLOR_* to our pair index for swatches."""
        return {
            curses.COLOR_RED: self.BAD,
            curses.COLOR_GREEN: self.OK,
            curses.COLOR_YELLOW: self.WARN,
            curses.COLOR_BLUE: self.INFO,
            curses.COLOR_MAGENTA: self.GPU,
            curses.COLOR_CYAN: self.ACCENT,
            curses.COLOR_WHITE: self.TEXT,
        }.get(ansi_color, self.TEXT)


class Dashboard(FanPanel):
    MIN_W, MIN_H = 72, 20
    # Footer entries in priority order. footer_hint() drops from the end to fit.
    HINTS = {
        "overview": ("Tab section", "↑↓ section", "t details", "m modes", "Space pause", "v results"),
        "main": ("Tab section", "↑↓ select", "←→ adjust", "Enter apply", "0-9 type", "Esc clear",
                 "d default", "r discard", "m modes", "Space pause", "v results"),
        "fans": ("←→ point", "↑↓ speed", "Enter apply", "t details", "R stock", "Esc back", "r discard",
                 "W save", "p curves"),
        "keyboard": ("Tab section", "1–4 zone", "←→ adjust HSV", "↑↓ channel", "Enter apply", "Esc back", "e effect",
                     "b bright", "c copy-all", "Space live"),
    }
    show_limits = False
    panel = "main"  # instances built without __init__ (tests) act as Power
    NAV_W = 13
    SECTION_LABELS = {"overview": "Overview", "main": "Power", "fans": "Fans", "keyboard": "Keyboard"}

    def __init__(self, screen, backend, sudo_ok=True):
        self.s, self.backend = screen, backend
        self.sudo_expired = not sudo_ok
        self.selected = 0
        self.pending = {}
        self.apply_results = []
        self.show_results = False
        self.notice = backend.notice or "Ready. Edit a row, then press Enter to apply.  l opens keyboard lighting."
        self.history = {k: collections.deque(maxlen=240) for k in ("cpu", "gpu", "cpu_w", "gpu_w")}
        self.last_timestamp = None
        self.fields = {}  # key -> raw text typed for that row
        self.invalid = set()  # keys whose field text is invalid
        self.help = False
        self.running = True
        self.paused = False
        self.panel = "overview"  # overview | main (Power) | fans | keyboard
        self.view = "summary"  # summary | details (overview only)
        self.theme = Theme(curses.has_colors())
        self.keyboard = Keyboard()
        self.kb_zone = 0
        self.kb_channel = 0  # 0=H 1=S 2=V
        self.kb_live = True
        self.kb_seen = 0
        self.fan_init()
        self._set_escdelay()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(120)
        try:
            curses.meta(True)
        except curses.error:
            pass

    @staticmethod
    def _set_escdelay():
        if "ESCDELAY" not in os.environ:
            try:
                curses.set_escdelay(25)
            except (AttributeError, curses.error):
                pass

    def put(self, y, x, text, color=0, bold=False, dim=False):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        try:  # the bottom-right cell writes but raises because the cursor cannot advance
            self.s.addnstr(y, x, str(text), w - x, self.theme.attr(color, bold, dim))
        except curses.error:
            pass

    def fill(self, y, x, height, width, char=" "):
        for row in range(y, y + height):
            self.put(row, x, char * max(0, width))

    def box(self, y, x, height, width, title="", color=1, right=""):
        if height < 2 or width < 4:
            return
        self.put(y, x, "╭" + "─" * (width - 2) + "╮", color)
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "│", color)
            self.put(row, x + width - 1, "│", color)
        self.put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", color)
        if title:
            label = f" {title} "
            self.put(y, x + 2, label, color, True)
        if right:
            label = f" {right} "
            self.put(y, x + width - len(label) - 2, label, color)

    def bar(self, y, x, width, value, color=None):
        width = max(0, width)
        if width == 0:
            return
        pct = clamp(value if value is not None else 0, 0, 100)
        filled = round(pct * width / 100)
        color = color if color is not None else self.theme.load(pct)
        self.put(y, x, BAR_FILL * filled, color, True)
        self.put(y, x + filled, BAR_EMPTY * (width - filled), self.theme.TEXT, dim=True)

    def chart(self, y, x, width, key, ceiling=100, color=1):
        width = max(0, width)
        values = list(self.history[key])[-width:]
        plot = []
        for value in values:
            if value is None:
                plot.append(" ")
            else:
                plot.append(SPARK[min(7, max(0, round(value / max(1, ceiling) * 7)))])
        self.put(y, x, "".join(plot).rjust(width), color)

    def chart_rows(self, y, x, width, height, key, ceiling=100, color=1, markers=()):
        """Multi-row bar chart (eighth blocks), right-aligned; markers are (value, color) lines."""
        width, height = max(0, width), max(0, height)
        if not width or not height:
            return
        ceiling = max(1, ceiling)
        values = list(self.history[key])[-width:]
        pad = width - len(values)
        eighths = [None if v is None else round(clamp(v / ceiling, 0, 1) * height * 8) for v in values]
        marks = {}
        for value, mark_color in markers:
            if value is not None:
                marks.setdefault(min(height - 1, int(clamp(value / ceiling, 0, 1) * height)), []).append(mark_color)
        for line in range(height):  # line 0 is the bottom row
            cells = []
            for e in eighths:
                n = 0 if e is None else clamp(e - line * 8, 0, 8)
                cells.append(" " if n == 0 else SPARK[n - 1])
            row = y + height - 1 - line
            self.put(row, x, "".join(cells).rjust(width), color)
            if line in marks:
                for col in range(width):
                    if col < pad or cells[col - pad] == " ":
                        self.put(row, x + col, "┄", marks[line][-1])

    @staticmethod
    def locked(state):
        # Gate on the LIVE mode; a staged mode never unlocks limit rows.
        return state.get("mode") != "custom"

    SUMMARY = ("ppt_pl1_spl", "ppt_pl2_sppt", "gpu_nv_ctgp")

    def shown_rows(self, state):
        "Rows the Power box draws and the cursor can visit: the mode, then the key limits in stock modes."
        rows = self.rows(state)
        return [k for k in rows if k == "mode" or not self.locked(state) or k in self.SUMMARY]

    def rows(self, state):
        attrs = state.get("attributes") or {}
        return ["mode"] + [k for k in FIELDS if k in attrs]

    def push_history(self, metrics):
        cpu = metrics.get("cpu", {})
        gpu = next(iter(metrics.get("gpu", {}).get("devices", [])), {})
        stamp = metrics.get("timestamp")
        if not stamp or stamp == self.last_timestamp:
            return
        for key, value in (("cpu", cpu.get("utilization_pct")), ("gpu", gpu.get("utilization_pct")),
                           ("cpu_w", cpu.get("power_w")), ("gpu_w", gpu.get("power_w"))):
            self.history[key].append(value)
        self.last_timestamp = stamp

    def draw(self, state):
        if self.keyboard.last_seq != self.kb_seen:
            self.kb_seen = self.keyboard.last_seq
            self.notice = self.keyboard.last_message
        self.s.erase()
        h, w = self.s.getmaxyx()
        if h < self.MIN_H or w < self.MIN_W:
            self.put(1, 2, "loqctl", self.theme.ACCENT, True)
            self.put(3, 2, f"Need at least {self.MIN_W}×{self.MIN_H}. Now {w}×{h}.")
            self.put(5, 2, "Press q to quit.", self.theme.TEXT, dim=True)
            self.s.refresh()
            return

        metrics = state.get("metrics") or {}
        if not self.paused:
            self.push_history(metrics)

        # Header
        model = state.get("model") or "…"
        mode = (state.get("mode") or "…").upper()
        access = "API" if self.backend.remote else "ROOT" if os.geteuid() == 0 else "LOCAL"
        if self.paused:
            access = "PAUSED"
        title = f" loqctl {VERSION} "
        self.put(0, 0, "━" * (w - 1), self.theme.ACCENT, dim=True)
        self.put(0, 1, title, self.theme.ACCENT, True)
        mid = f"{model}  ·  {mode}"
        self.put(0, max(len(title) + 2, (w - len(mid)) // 2), mid, self.theme.OK)
        self.put(0, w - len(access) - 3, f" {access} ", self.theme.GPU if access == "API" else self.theme.WARN)

        self.draw_nav(state, 1, 0, h - 3, self.NAV_W)
        cx, cw = self.NAV_W + 1, w - self.NAV_W - 1
        if self.panel == "keyboard":
            self.draw_keyboard(1, cx, h - 3, cw)
        elif self.panel == "fans":
            self.draw_fans(state, metrics, 1, cx, h - 3, cw)
        elif self.panel == "overview":
            self.draw_overview(state, metrics, 1, cx, h - 3, cw)
        else:
            self.draw_main(state, metrics, 1, cx, h - 3, cw)

        # Status + keys
        stamp = metrics.get("timestamp")
        stale = stamp is not None and time.time() - stamp > 5
        status = state.get("error") or (("Sensor data is stale. " if stale else "") + self.notice)
        warn = bool(state.get("error") or stale)
        if self.sudo_expired and not self.backend.remote and os.geteuid() != 0:
            status, warn = "sudo not authenticated · press s to authenticate · edits stay staged", True
        self.put(h - 2, 1, status[: w - 3], self.theme.WARN if warn else self.theme.TEXT)
        keys = self.footer_hint(w - 3)
        self.put(h - 1, 1, keys, self.theme.ACCENT, dim=True)

        if self.help:
            self.draw_help()
        if self.show_results:
            self.draw_results()
        if self.show_limits:
            self.draw_limits(state)
        self.s.refresh()

    def sections(self, state):
        "Section ids the laptop supports. Fans stay listed so a missing curve can explain itself there."
        found = ["overview"]
        if self.rows(state):
            found.append("main")
        found.append("fans")
        if self.keyboard.available:
            found.append("keyboard")
        return found

    def draw_nav(self, state, y, x, height, width):
        self.box(y, x, height, width, "", self.theme.ACCENT)
        for i, name in enumerate(self.sections(state)[: max(0, height - 2)]):
            current = name == self.panel
            label = ("▶ " if current else "  ") + self.SECTION_LABELS[name]
            self.put(y + 1 + i, x + 1, label[: width - 2], self.theme.ACCENT if current else self.theme.TEXT,
                     current, not current)

    def goto_section(self, name):
        if name == "fans":
            self.open_fans()
        elif name == "keyboard":
            self.open_keyboard()
        else:
            self.panel = name
            self.view = "summary"
            self.notice = "Ready."

    def leave_section(self, state):
        "Back from Fans or Keyboard to Power, or to Overview when Power is hidden."
        self.panel = "main" if self.rows(state) else "overview"

    def open_keyboard(self):
        self.panel = "keyboard"
        self.view = "summary"
        self.keyboard.recheck()
        self.keyboard.load()
        self.notice = "Keyboard lighting. Esc returns."

    def step_section(self, state, direction):
        found = self.sections(state)
        at = found.index(self.panel) if self.panel in found else 0
        self.goto_section(found[(at + direction) % len(found)])

    def toggle_details(self):
        if self.panel == "overview":
            self.view = "summary" if self.view == "details" else "details"

    def draw_overview(self, state, metrics, y, x, height, width):
        if self.view == "details":
            self.draw_details(metrics, y, x, height, width)
        else:
            self.draw_telemetry(state, metrics, y, x, height, width)

    def draw_main(self, state, metrics, y, x, height, width):
        self.draw_controls(state, y, x, height, width)

    def draw_controls(self, state, y, x, height, width):
        pending_n = len(self.pending)
        right = f"{pending_n} staged" if pending_n else "Live"
        if self.invalid:
            right = (f"{pending_n} staged" if pending_n else "") + f"{', ' if pending_n else ''}{len(self.invalid)} invalid"
        self.box(y, x, height, width, "Power", self.theme.ACCENT, right)
        rows = self.rows(state)
        if not rows:
            self.put(y + 2, x + 2, "No firmware attributes exposed.", self.theme.WARN)
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        if rows[self.selected] not in self.shown_rows(state):
            self.selected = 0
        # Mode selector on top: every mode the firmware offers, the live one in brackets.
        modes, live = state.get("modes") or [], state.get("mode")
        staged_mode = self.pending.get("mode")
        names = [f"[{m}]" if m == live else m for m in modes] or [str(live or "N/A")]
        if staged_mode is not None:
            choice = f"{live or 'N/A'} → {staged_mode}"
        else:
            choice = fit_parts(names, width - 14, "  ")
        chosen = self.selected == 0
        self.put(y + 1, x + 1, f"{'▶' if chosen else ' '} {'Mode':<7}{choice}"[: width - 3],
                 self.theme.WARN if staged_mode is not None else (self.theme.ACCENT if chosen else self.theme.TEXT),
                 bold=chosen)
        # Limits grouped by subsystem; a group name is a row in the list, not part of the selection.
        shown, entries, group = self.shown_rows(state), [], None
        for row, key in enumerate(rows[1:], 1):
            if key not in shown:
                continue  # stock modes show the few key limits, not the whole list
            name = "GPU" if key.startswith("gpu_") else "CPU"
            if name != group:
                entries.append((None, name))
                group = name
            entries.append((row, key))
        list_top = y + 2
        detail_h = 4
        visible = max(3, height - detail_h - 3)
        at = next((i for i, (row, _) in enumerate(entries) if row == self.selected), 0)
        start = max(0, at - visible + 1)
        if start and entries[start][0] is not None and entries[start - 1][0] is None and start - 1 + visible > at:
            start -= 1  # keep a group name above its first visible row
        window = entries[start:start + visible]
        if window and window[-1][0] is None:
            window.pop()
        for i, (row, key) in enumerate(window):
            yy = list_top + i
            if row is None:
                self.put(yy, x + 2, key, self.theme.INFO, bold=True)
                continue
            chosen = row == self.selected
            marker = "▶" if chosen else " "
            item = state["attributes"][key]
            label, current, unit = item["label"], item.get("effective_value"), item["unit"]
            staged = self.pending.get(key)
            value = "N/A" if current is None else f"{current}{unit_sep(unit)}{unit}"
            if self.locked(state):
                line = f"{marker} {label:<20} {value}"
                self.put(yy, x + 1, line[: width - 3], self.theme.TEXT, bold=chosen, dim=True)
                continue
            text = self.fields.get(key)
            bad = key in self.invalid
            if chosen:
                line = (f"{marker} {label:<20} {strike(value)} -> "
                        + (f"{text}_" if text else "__"))
                self.put(yy, x + 1, line[: width - 3], self.theme.BAD if bad else self.theme.ACCENT, bold=True)
                continue
            if bad:
                value += f" → {text}"
                self.put(yy, x + 1, f"{marker} {label:<20} {value}"[: width - 3], self.theme.BAD)
                continue
            if staged is not None:
                value += f" → {staged}"
            color = self.theme.WARN if staged is not None else self.theme.TEXT
            self.put(yy, x + 1, f"{marker} {label:<20} {value}"[: width - 3], color, dim=staged is None)

        if self.locked(state):
            self.put(list_top + len(window), x + 2, "Select custom on the mode row to edit.", self.theme.TEXT, dim=True)

        # Detail footer inside the box
        detail_y = y + height - detail_h
        self.put(detail_y, x + 1, "─" * (width - 2), self.theme.ACCENT, dim=True)
        key = rows[self.selected]
        if key == "mode":
            modes = " ".join(state.get("modes") or []) or "none"
            driver = state.get("mode_driver", GAMEZONE)
            if driver == GAMEZONE:
                self.put(detail_y + 1, x + 2, "← → cycles platform mode", self.theme.ACCENT)
            else:
                via = f"{driver}, not {GAMEZONE}" if driver else f"legacy file, {GAMEZONE} not found"
                note = limit_note(state, state.get("attributes", {})) or f"Mode set via {via}"
                self.put(detail_y + 1, x + 2, note[: width - 4], self.theme.WARN)
            self.put(detail_y + 2, x + 2, f"Available: {modes}"[: width - 4], self.theme.TEXT, dim=True)
        elif self.locked(state):
            item, mode = state["attributes"][key], state.get("mode") or "N/A"
            if item.get("effective_value") is not None:
                note = fit_parts([f"custom default {fmt(item['default_value'])}", f"{mode} limit from firmware table"],
                                 width - 4)
            else:
                reason = state.get("mode_limits_error")
                note = limit_note(state, {key: item}) or (
                    reason if reason and not reason.startswith("needs root") else
                    "Stock-mode limits need root: press s or run sudo loqctl serve")
            self.put(detail_y + 1, x + 2, note[: width - 4], self.theme.WARN)
            self.put(detail_y + 2, x + 2, (lambda long: long if len(long) <= width - 4 else "Select custom on the mode row to edit")(
                         f"Locked in {mode} mode; select custom to edit"),
                     self.theme.TEXT, dim=True)
        else:
            item = state["attributes"][key]
            step = item["scalar_increment"]
            step_txt = "N/A" if step is None else "any int" if not step else str(step)
            if item.get("current_value") is None:
                error = item.get("read_error") or "missing"
                note = limit_note(state, {key: item}) or f"{key}: current value unreadable ({error})"
                self.put(detail_y + 1, x + 2, note[: width - 4], self.theme.WARN)
            else:
                self.put(detail_y + 1, x + 2, key, self.theme.ACCENT)
            self.put(detail_y + 2, x + 2,
                     fit_parts([f"{fmt(item['min_value'])}–{fmt(item['max_value'])} {item['unit']}",
                                f"default {fmt(item['default_value'])}", f"step {step_txt}"], width - 4),
                     self.theme.TEXT, dim=True)

    def telemetry_box(self, y, x, height, width, name, color, parts, key, ceiling, chart_color, marks,
                      note="", alert=None):
        """One box: the numbers go in the title, one power graph fills the inside, an optional note takes the last row."""
        self.box(y, x, height, width, name, color)
        room = width - len(name) - 8
        text = fit_parts(parts, room) if room > 0 and len(parts[0]) <= room else ""
        if text:
            self.put(y, x + len(name) + 4, f"{text} ", alert if alert is not None else color, alert is not None)
        ix, iw = x + 2, width - 4
        rows = height - 2
        if note and rows > 1 and len(note) <= iw:
            self.put(y + height - 2, ix, note, self.theme.TEXT, dim=True)
            rows -= 1
        if key and rows > 0:
            self.chart_rows(y + 1, ix, iw, rows, key, ceiling, chart_color, marks)

    def draw_telemetry(self, state, metrics, y, x, height, width):
        cpu = metrics.get("cpu") or {}
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {})
        gpu_meta = metrics.get("gpu") or {}
        memory = metrics.get("memory") or {}
        throttle = metrics.get("throttle") or {}
        attrs = state.get("attributes") or {}

        def limit(key):
            return (attrs.get(key) or {}).get("effective_value")

        # Split right column into cpu / gpu / sys with flexible heights.
        cpu_h = max(7, height * 38 // 100)
        gpu_h = max(6, height * 32 // 100)
        if cpu_h + gpu_h > height - 6:
            gpu_h = max(6, height - cpu_h - 6)
        sys_h = height - cpu_h - gpu_h
        if sys_h < 5:
            sys_h = 5
            gpu_h = max(5, height - cpu_h - sys_h)

        # CPU
        throttled = bool((throttle.get("cpu") or {}).get("active"))
        # Short boxes have no spare row for "Waiting"; a real reason replaces the graph's bottom row.
        source = cpu.get("power_source") or ""
        reason = source if cpu.get("power_w") is None and not source.startswith("Waiting") else ""
        pl1, pl2 = limit("ppt_pl1_spl"), limit("ppt_pl2_sppt")
        self.telemetry_box(
            y, x, cpu_h, width, "CPU", self.theme.OK,
            (["THROTTLE"] if throttled else []) + [fmt(cpu.get("temperature_c"), "°C"), fmt(cpu.get("power_w"), "W", 1),
                                                 fmt(None if cpu.get("clock_mhz") is None else cpu["clock_mhz"] / 1000, "GHz", 1)],
            "cpu_w", max(pl2 or 0, max((v for v in self.history["cpu_w"] if v is not None), default=0), 1), self.theme.WARN,
            [(v, c) for v, c in ((pl1, self.theme.WARN), (pl2, self.theme.BAD)) if v is not None],
            reason, self.theme.BAD if throttled else None)

        # GPU
        gy = y + cpu_h
        if gpu:
            ctgp, plimit = limit("gpu_nv_ctgp"), gpu.get("power_limit_w")
            reasons = gpu.get("throttle") or []
            self.telemetry_box(
                gy, x, gpu_h, width, "GPU", self.theme.GPU,
                (["THROTTLE"] if reasons else []) + [fmt(gpu.get("temperature_c"), "°C"), fmt(gpu.get("power_w"), "W", 1),
                                                   fmt(None if gpu.get("clock_mhz") is None else gpu["clock_mhz"] / 1000, "GHz", 1)],
                "gpu_w", max(ctgp or 0, plimit or 0, max((v for v in self.history["gpu_w"] if v is not None), default=0), 1), self.theme.WARN,
                [(v, c) for v, c in ((ctgp, self.theme.WARN), (plimit, self.theme.BAD)) if v is not None],
                ("throttle: " + ", ".join(reasons)) if reasons else "", self.theme.BAD if reasons else None)
        else:
            self.telemetry_box(gy, x, gpu_h, width, "GPU", self.theme.GPU,
                               ["Asleep" if gpu_meta.get("suspended") else "Offline"], None, 1, 0, (),
                               gpu_meta.get("error") or "", None)

        ix, iw = x + 2, width - 4

        # System
        sy = gy + gpu_h
        used, total = memory.get("used_mib"), memory.get("total_mib")
        mem_right = ""
        if used is not None and total:
            mem_right = f"{used / 1024:.1f}/{total / 1024:.1f} GiB"
        self.box(sy, x, sys_h, width, "System", self.theme.INFO, mem_right)
        if used is not None and total:
            self.bar(sy + 1, ix, iw, used / total * 100, self.theme.INFO)
        battery = next(iter(metrics.get("battery") or []), {})
        ac = metrics.get("ac_online")
        power = "AC" if ac else "Battery" if ac is False else "Power N/A"
        bat = f"{fmt(battery.get('percent'), '%')} {battery.get('status') or ''}  limit {battery.get('charge_limit') or 'N/A'}".strip()
        health = battery.get("health_percent")
        parts = [f"{power}  {bat}"] + ([f"health {health:.0f}%"] if health is not None else [])
        self.put(sy + 2, ix, fit_parts(parts, iw, "  "), self.theme.OK if ac else self.theme.WARN)
        if sys_h >= 6:
            fans = metrics.get("fans") or []
            fan_txt = "  ".join(f"{f['name']} {fmt(f['rpm'], 'RPM')}" for f in fans) if fans else "Fans N/A"
            self.put(sy + 3, ix, fan_txt[:iw], self.theme.TEXT, dim=True)
        if sys_h >= 7:
            loads = metrics.get("load") or []
            uptime = metrics.get("uptime_s")
            up = f"  up {int(uptime // 3600)}h{int(uptime % 3600 // 60):02d}m" if uptime is not None else ""
            self.put(sy + 4, ix, ("load " + " ".join(fmt(v, decimals=2) for v in loads) + up)[:iw],
                     self.theme.TEXT, dim=True)

    def draw_details(self, metrics, y, x, height, width):
        self.box(y, x, height, width, "Details", self.theme.INFO, "t or Esc: back")
        ix, iw = x + 2, width - 4
        na = lambda v, unit="", d=1: fmt(v, unit, d)
        cpu = metrics.get("cpu") or {}
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {})
        text = self.theme.TEXT
        head = self.theme.ACCENT

        def row(label, parts, color=text, bold=False):
            "One row: a label, then parts that drop from the end rather than cut a number."
            return (f"{label:<10}" + fit_parts(parts, iw - 10, "  "), color, bold)

        # Throttle first: an active throttle is the row to see on a short terminal.
        throttle = metrics.get("throttle") or {}
        cpu_t, gpu_t = throttle.get("cpu") or {}, throttle.get("gpu")
        active = cpu_t.get("active")
        lines = [row("Throttle", [f"cpu {'N/A' if active is None else 'yes' if active else 'no'}",
                                  f"core {na(cpu_t.get('core_events'), '', 0)}",
                                  f"package {na(cpu_t.get('package_events'), '', 0)}"],
                     self.theme.BAD if active else text, bool(active)),
                 row("", ["gpu " + ("N/A" if gpu_t is None else ", ".join(gpu_t) or "none")],
                     self.theme.BAD if gpu_t else text, bool(gpu_t))]
        cores = list((cpu.get("cores") or {}).values())
        spark = "".join("·" if c is None else SPARK[min(7, round(c * 7 / 100))] for c in cores)
        lines += [row("CPU", [f"util {na(cpu.get('utilization_pct'), '%', 0)}", f"avg {na(cpu.get('clock_mhz'), ' MHz', 0)}",
                              f"max {na(cpu.get('max_clock_mhz'), ' MHz', 0)}", cpu.get("power_source") or "N/A"]),
                  row("", [f"cores {spark or 'N/A'}"], self.theme.OK)]
        mode = f"mode {metrics.get('gpu_mode') or 'N/A'}"
        if gpu:
            lines += [row("GPU", [mode, gpu.get("name") or "N/A"], self.theme.GPU),
                      row("", [f"util {na(gpu.get('utilization_pct'), '%', 0)}", f"core {na(gpu.get('clock_mhz'), ' MHz', 0)}",
                               f"memory {na(gpu.get('memory_clock_mhz'), ' MHz', 0)}"]),
                      row("", [f"vram {na(gpu.get('memory_used_mib'), '', 0)}/{na(gpu.get('memory_total_mib'), ' MiB', 0)}",
                               f"limit {na(gpu.get('power_limit_w'), ' W')}"])]
        else:
            lines.append(row("GPU", ["N/A", mode]))
        power = metrics.get("power") or {}
        items = [(f"  {name:<24} {na(watts, ' W')}", text, False) for name, watts in (power.get("rapl") or {}).items()]
        if "battery_w" in power:
            items.append(row("  battery", [na(power.get("battery_w"), " W"), power.get("battery_status") or "N/A"]))
        for b in metrics.get("battery") or []:
            items.append(row(f"  {b['name'][:8]}", [f"health {na(b.get('health_percent'), '%', 0)}",
                                                    f"cycles {na(b.get('cycles'), '', 0)}",
                                                    f"{na(b.get('full_wh'), '', 1)}/{na(b.get('design_wh'), ' Wh', 1)}"]))
        for u in power.get("usbc") or []:
            items.append(row(f"  {u['name'][:6]}", [na(u["watts"], " W"), na(u["volts"], " V", 2), na(u["amps"], " A", 2)]))
        temps = [(f"  {t['group']:<6} {t['label'][:24]:<24} {na(t['c'], '°C')}", self.theme.heat(t["c"]), False)
                 for t in metrics.get("temperatures") or []]
        volts = [(f"  {v['label'][:30]:<30} {na(v['v'], ' V', 3)}", text, False) for v in metrics.get("voltages") or []]
        room, more = height - 2, False
        # Power keeps its place; temperatures and voltages take what is left, shortest first to go.
        for name, found in (("Power", items), ("Temperatures", temps), ("Voltages", volts)):
            left = room - len(lines) - 1
            more = more or left < 1 or len(found) > left
            if left >= 1:
                lines.append((name, head, True))
                lines += (found or [("  N/A", text, False)])[:left]
        if more or len(lines) > room:
            lines = lines[:max(0, min(len(lines), room - 1))] + [("…", text, False)]
        for i, (line, color, bold) in enumerate(lines):
            self.put(y + 1 + i, ix, line, color, bold)

    def draw_keyboard(self, y, x, height, width):
        kb = self.keyboard
        status = "Live" if self.kb_live else "Preview off"
        if not kb.available:
            status = "Missing"
        self.box(y, x, height, width, "Keyboard (LegionAura)", self.theme.GPU, status)
        ix, iw = x + 2, width - 4
        if not kb.available:
            self.put(y + 2, ix, "Install LegionAura (AUR: legionaura)", self.theme.WARN)
            self.put(y + 3, ix, "for 4-zone RGB control.", self.theme.WARN)
            self.put(y + 5, ix, "Esc returns to firmware controls.", self.theme.TEXT, dim=True)
            return

        # Effect row
        effects = []
        for name in kb.EFFECTS:
            tag = f"[{name}]" if name == kb.effect else f" {name} "
            effects.append(tag)
        self.put(y + 1, ix, fit_parts(["effect  " + " ".join(effects)], iw, ""), self.theme.ACCENT)
        bright = "low" if kb.brightness == 1 else "high"
        wave = f"  wave {kb.wave_dir}" if kb.effect == "wave" else ""
        self.put(y + 2, ix, fit_parts([f"brightness  {bright}", f"speed {kb.speed}/4", wave.strip()], iw, "   "), self.theme.TEXT, dim=True)

        # Zones
        self.put(y + 4, ix, "zones", self.theme.TEXT, dim=True)
        for zi, hexcolor in enumerate(kb.zones):
            try:
                r, g, b = hex_to_rgb(hexcolor)
            except ControlError:
                r, g, b = 255, 255, 255
            pair = self.theme.ansi_pair(nearest_ansi(r, g, b))
            chosen = zi == self.kb_zone
            zx = ix + zi * max(12, iw // 4)
            border = "▶" if chosen else " "
            self.put(y + 5, zx, f"{border}Z{zi + 1}", self.theme.GPU if chosen else self.theme.TEXT, bold=chosen)
            self.put(y + 6, zx, "████", pair, True)
            self.put(y + 7, zx, f"#{hexcolor}", self.theme.TEXT, dim=not chosen)

        # HSV channels for selected zone
        try:
            rgb = hex_to_rgb(kb.zones[self.kb_zone])
        except ControlError:
            rgb = (255, 255, 255)
        h_deg, s_pct, v_pct = rgb_to_hsv(*rgb)
        channels = (("H", h_deg, 360, "°"), ("S", s_pct, 100, "%"), ("V", v_pct, 100, "%"))
        cy = y + 9
        self.put(cy, ix, fit_parts([f"zone {self.kb_zone + 1}  HSV picker", "(terminal swatch ≈ nearest ANSI)"], iw, "  "), self.theme.ACCENT)
        for i, (name, value, ceiling, unit) in enumerate(channels):
            yy = cy + 1 + i
            chosen = i == self.kb_channel
            marker = "▶" if chosen else " "
            bar_w = max(8, min(40, iw - 18))
            self.put(yy, ix, f"{marker}{name} ", self.theme.WARN if chosen else self.theme.TEXT, bold=chosen)
            self.bar(yy, ix + 4, bar_w, value / ceiling * 100,
                     self.theme.WARN if chosen else self.theme.ACCENT)
            self.put(yy, ix + 5 + bar_w, f" {value:5.1f}{unit}", self.theme.TEXT, dim=not chosen)

        tip_y = min(y + height - 3, cy + 5)
        self.put(tip_y, ix, fit_parts(["Hardware shows the true color.", "UI uses your terminal theme only."], iw, " "), self.theme.TEXT, dim=True)
        self.put(tip_y + 1, ix, f"config {kb.config}"[:iw], self.theme.TEXT, dim=True)

    HELP_LINES = (
        "Global    Tab section t details  Space pause  ? help  q quit",
        "Firmware  j/k or ↑↓ select   ←/→ or -/+ step the value",
        "          0-9 type a value   Backspace delete   Esc clear",
        "          Enter apply all   d custom default   r discard all",
        "          Limits need custom mode (Enter on mode row)",
        "          v last results   s sudo authenticate (if needed)",
        "          m overlay of every mode's firmware limits",
        "",
        "Keyboard  l open lighting panel   Esc back to firmware",
        "          1–4 select zone   ←/→ adjust HSV   ↑↓ channel",
        "          e cycle effect   b brightness   w wave dir",
        "          c copy color to all zones   Space live apply",
        "          Enter apply via legionaura",
        "",
        "Fans      f open fan curves (edits need custom mode)",
        "          ←→ or Tab/[ ] select point",
        "          ↑↓ next speed the fan stores (both fans)",
        "          Shift+↑↓ five steps   t details",
        "          Enter apply   r discard   R reset to stock",
        "          p next curve   W save   Esc back",
    )

    def footer_hint(self, width):
        entries = list(self.HINTS.get(self.panel, self.HINTS["main"]))
        while entries and len("  ".join(["? help", *entries, "q quit"])) > width:
            entries.pop()
        return "  ".join(["? help", *entries, "q quit"])

    def draw_help(self):
        h, w = self.s.getmaxyx()
        bw = min(86, w - 4)
        bh = min(len(self.HELP_LINES) + 2, h - 2)
        bx, by = (w - bw) // 2, max(1, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        hint = "? or Esc to close"
        title = "Help" if bw >= len(hint) + 16 else ""
        self.box(by, bx, bh, bw, title, self.theme.ACCENT, right=hint)
        lines = list(self.HELP_LINES)
        if len(lines) > bh - 2:
            keep = max(0, bh - 3)
            lines = lines[:keep] + [f"… ({len(lines) - keep} more, resize)"]
        for i, line in enumerate(lines[: bh - 2]):
            self.put(by + 1 + i, bx + 2, line[: bw - 4], self.theme.TEXT, dim=not line)

    def draw_results(self):
        h, w = self.s.getmaxyx()
        bw = min(104, w - 4)
        bh = min(h - 2, len(self.apply_results) + 11)
        bx, by = (w - bw) // 2, max(0, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "Apply results", self.theme.ACCENT)
        self.put(by + 1, bx + 2, "Firmware readback immediately after each attempted change.", self.theme.TEXT, dim=True)
        self.put(by + 3, bx + 2, f"{'Control':<26}{'Before':>12}{'Requested':>12}{'Read back':>12}  Result",
                 self.theme.ACCENT, True)
        for i, result in enumerate(self.apply_results):
            key = result["key"]
            label, unit = ("Platform mode", "") if key == "mode" else FIELDS[key]
            def display(value):
                return "N/A" if value is None else f"{value}{unit_sep(unit)}{unit}"
            line = (f"{label:<26}{display(result['before']):>12}{display(result['requested']):>12}"
                    f"{display(result['current']):>12}  {result['status']}")
            color = self.theme.OK if result["status"] == "APPLIED" else (
                self.theme.WARN if result["status"] == "NOT SENT" else self.theme.BAD)
            self.put(by + 4 + i, bx + 2, line[: bw - 4], color)
        error = next((r["error"] for r in self.apply_results if r.get("error")),
                     "All requested values matched firmware readback." if self.apply_results else "No results yet.")
        for i, line in enumerate(textwrap.wrap(error, bw - 4)[:3]):
            self.put(by + bh - 5 + i, bx + 2, line, self.theme.WARN)
        self.put(by + bh - 2, bx + 2, "Enter / Esc closes · v reopens · failed edits remain staged", self.theme.ACCENT)

    def draw_limits(self, state):
        h, w = self.s.getmaxyx()
        modes = state.get("modes") or []
        rows = self.rows(state)[1:]
        attrs, table = state.get("attributes") or {}, state.get("mode_limits")
        bw = min(w - 4, 26 + 13 * len(modes))
        bh = min(h - 2, len(rows) + 6)
        bx, by = (w - bw) // 2, max(0, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "Mode limits", self.theme.ACCENT)
        self.put(by + 1, bx + 2, "Control", self.theme.ACCENT, True)
        for col, mode in enumerate(modes):
            self.put(by + 1, bx + 26 + 13 * col, f"{mode:>12}", self.theme.ACCENT, True)
        for i, key in enumerate(rows[: bh - 5]):
            self.put(by + 2 + i, bx + 2, attrs[key]["label"][:23], self.theme.TEXT)
            for col, mode in enumerate(modes):
                value = effective_value(mode, key, attrs[key].get("current_value"), table)
                self.put(by + 2 + i, bx + 26 + 13 * col, f"{fmt(value):>12}", self.theme.TEXT,
                         bold=mode == state.get("mode"))
        note = "Custom is live; other modes come from the firmware table."
        if table is None:
            note = "Stock-mode limits need root: press s or run sudo loqctl serve"
        self.put(by + bh - 3, bx + 2, note[: bw - 4], self.theme.WARN if table is None else self.theme.TEXT, dim=table is not None)
        self.put(by + bh - 2, bx + 2, "Enter / Esc / m closes", self.theme.ACCENT)

    def stage(self, state, key, value):
        if key == "mode":
            if value not in state.get("modes", []):
                raise ControlError("Unsupported mode.")
            current = state.get("mode")
            dropped = [k for k in self.pending if k != "mode"]
            if value != current:
                for k in dropped:
                    del self.pending[k]
        else:
            item = state["attributes"][key]
            if not item["available"]:
                raise ControlError("This control is unavailable.")
            lo, hi, step = item["min_value"], item["max_value"], max(1, item["scalar_increment"])
            if not lo <= value <= hi or (value - lo) % step:
                raise ControlError(f"Use {lo}–{hi} in steps of {step}.")
            current = item["current_value"]
            dropped_mode = self.pending.pop("mode", None) is not None
        if value == current:
            self.pending.pop(key, None)
        else:
            self.pending[key] = value
        self.notice = "Edit staged. Press Enter to apply."
        if key == "mode":
            if value != current and dropped:
                self.notice = "Staged limit edits dropped: limits apply only in custom mode."
        elif dropped_mode:
            self.notice = "Staged mode change dropped: apply mode and limits separately."

    def set_field(self, state, key, text):
        if not text:
            self.fields.pop(key, None)
            self.pending.pop(key, None)
            self.invalid.discard(key)
            return
        self.fields[key] = text
        try:
            if not text.isdigit():
                raise ControlError("Enter an integer.")
            self.stage(state, key, int(text))
        except ControlError:
            self.invalid.add(key)
            self.pending.pop(key, None)
            self.notice = "Value is not valid for this control."
        else:
            self.invalid.discard(key)

    def sudo_call(self, args):
        """Run an internal subcommand through `sudo -n`; flags the expiry banner when it lapsed."""
        try:
            return sudo_run(args)
        except SudoExpired as exc:
            self.sudo_expired = True
            raise ControlError("sudo credentials expired. Press s to re-authenticate; edits stay staged.") from exc

    def run_sudo_apply(self, changes):
        """Apply all changes in one `sudo -n` call; returns the per-item results."""
        stdout, stderr = self.sudo_call(["_apply", json.dumps(changes)])
        try:
            results = json.loads(stdout)
            if not isinstance(results, list) or not all(isinstance(r, dict) and "key" in r and "status" in r
                                                        for r in results):
                raise ValueError
        except ValueError as exc:
            raise ControlError(stderr or "sudo apply returned unreadable output.") from exc
        return results

    def reauth(self):
        curses.def_prog_mode()
        curses.endwin()
        try:
            ok = subprocess.run(["sudo", "-v"]).returncode == 0
        except KeyboardInterrupt:
            ok = False
        finally:
            curses.reset_prog_mode()
            self.s.refresh()
        if ok:
            self.sudo_expired = False
        self.notice = "sudo authenticated." if ok else "sudo authentication failed."
        if ok:
            self.backend.load_mode_limits()

    def read_current(self, key):
        if not self.backend.remote:
            hardware = self.backend.hardware
            return hardware.read_mode() if key == "mode" else hardware.read_attribute(key)
        return self.pick_current(self.backend.controls(), key)

    @staticmethod
    def pick_current(controls, key):
        if key == "mode":
            return controls.get("mode")
        return controls.get("attributes", {}).get(key, {}).get("current_value")

    def write_reported(self, key, value):
        """Write through the backend; in API mode return the response's read-back, else None."""
        response = self.backend.write(key, value)
        if not self.backend.remote or not isinstance(response, dict):
            return None
        current = response.get("current")
        if key == "mode":
            return current if isinstance(current, str) else None
        try:
            return int(current)
        except (TypeError, ValueError):
            return None

    def apply(self, state):
        try:
            self._apply(state)
        finally:
            self.backend.refresh()

    def _apply(self, state):
        if not self.pending:
            self.notice = "No pending changes."
            return
        changes = sorted(self.pending.items(), key=lambda pair: pair[0] != "mode")
        if any(k != "mode" for k in self.pending) and self.locked(state):
            raise ControlError(f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter.")
        if not (self.backend.remote or os.geteuid() == 0):
            results = self.run_sudo_apply([[k, v] for k, v in changes])
        else:
            try:
                check_changes([list(pair) for pair in changes])
            except ControlError:
                self.apply_results, self.show_results = [], False  # drop results of an earlier apply
                raise
            snapshot = None
            if self.backend.remote:
                try:
                    snapshot = self.backend.controls()  # one capabilities call for every "before"
                except (ControlError, OSError, ValueError, http_client.HTTPException):
                    pass
            results = run_changes(
                changes, self.read_current, self.write_reported,
                (ControlError, OSError, ValueError, http_client.HTTPException),
                (lambda key: self.pick_current(snapshot, key) if snapshot is not None else None)
                if self.backend.remote else None)
        if results:
            self.apply_results, self.show_results = results, True
        completed = 0
        for result in results:
            if result["status"] == "APPLIED":
                self.pending.pop(result["key"], None)
                self.fields.pop(result["key"], None)
                completed += 1
        failed = next((r for r in results if r["status"] != "APPLIED"), None)
        if failed:
            raise ControlError(f"Apply stopped after {completed} writes: {failed.get('error') or failed['status']}")
        self.notice = f"Applied and verified {completed} change(s)."

    def kb_set_hsv(self, h, s, v):
        kb = self.keyboard
        kb.zones[self.kb_zone] = rgb_to_hex(*hsv_to_rgb(h, s, v))
        kb.mark_dirty()
        if self.kb_live:
            # Live tweaking uses static so zone colors are visible regardless of effect.
            try:
                if kb.effect in ("static", "breath"):
                    kb.submit(kb.command(), kb.effect)
                else:
                    kb.submit(["static", *kb.zones, "--brightness", str(kb.brightness)], "static")
            except ControlError as exc:
                self.notice = str(exc)

    def kb_nudge(self, direction, fine=False):
        try:
            rgb = hex_to_rgb(self.keyboard.zones[self.kb_zone])
        except ControlError:
            rgb = (255, 255, 255)
        h, s, v = rgb_to_hsv(*rgb)
        step = 1 if fine else (5 if self.kb_channel else 10)
        if self.kb_channel == 0:
            h = (h + direction * step) % 360
        elif self.kb_channel == 1:
            s = clamp(s + direction * step, 0, 100)
        else:
            v = clamp(v + direction * step, 0, 100)
        self.kb_set_hsv(h, s, v)
        self.notice = f"Zone {self.kb_zone + 1} → #{self.keyboard.zones[self.kb_zone]}"

    def handle_keyboard(self, ch, state):
        kb = self.keyboard
        if ch in (27, ord("l")):
            self.leave_section(state)
            self.notice = "Back to firmware controls."
            return
        if ch == ord("q"):
            self.running = False
            return
        if not kb.available:
            return
        if ch in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.kb_zone = ch - ord("1")
            self.notice = f"Selected zone {self.kb_zone + 1}."
        elif ch in (curses.KEY_UP, ord("k")):
            self.kb_channel = (self.kb_channel - 1) % 3
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.kb_channel = (self.kb_channel + 1) % 3
        elif ch in (curses.KEY_LEFT, ord("-")):
            self.kb_nudge(-1)
        elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self.kb_nudge(1)
        elif ch in (ord("H"),):  # fine adjust
            self.kb_nudge(-1, fine=True)
        elif ch in (ord("L"),):
            self.kb_nudge(1, fine=True)
        elif ch == ord("e"):
            idx = kb.EFFECTS.index(kb.effect) if kb.effect in kb.EFFECTS else 0
            kb.effect = kb.EFFECTS[(idx + 1) % len(kb.EFFECTS)]
            kb.mark_dirty()
            self.notice = f"Effect → {kb.effect}"
            if self.kb_live:
                try:
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("b"):
            kb.brightness = 1 if kb.brightness == 2 else 2
            kb.mark_dirty()
            self.notice = f"Brightness → {'low' if kb.brightness == 1 else 'high'}"
            if self.kb_live:
                try:
                    kb.submit(["--brightness", str(kb.brightness)], "brightness")
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("w"):
            kb.wave_dir = "rtl" if kb.wave_dir == "ltr" else "ltr"
            kb.mark_dirty()
            self.notice = f"Wave → {kb.wave_dir}"
            if self.kb_live and kb.effect == "wave":
                try:
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("c"):
            color = kb.zones[self.kb_zone]
            kb.zones = [color] * kb.ZONES
            kb.mark_dirty()
            self.notice = f"Copied #{color} to all zones."
            if self.kb_live:
                try:
                    kb.effect = "static" if kb.effect == "off" else kb.effect
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord(" "):
            self.kb_live = not self.kb_live
            self.notice = "Live keyboard apply on." if self.kb_live else "Live apply off — Enter to commit."
        elif ch in (10, 13, curses.KEY_ENTER):
            try:
                kb.submit(kb.command(), kb.effect)
                self.notice = "Applying keyboard…"
            except ControlError as exc:
                self.notice = str(exc)

    def handle(self, ch, state):
        if self.show_results:
            if ch in (10, 13, curses.KEY_ENTER, 27, ord("v")):
                self.show_results = False
            elif ch == ord("q"):
                self.running = False
            return
        if self.show_limits:
            if ch in (10, 13, curses.KEY_ENTER, 27, ord("m")):
                self.show_limits = False
            elif ch == ord("q"):
                self.running = False
            return
        if self.help:
            if ch in (ord("?"), 27):
                self.help = False
            elif ch == ord("q"):
                self.running = False
            return
        if ch == ord("?") and getattr(self, "panel", "main") not in ("main", "overview"):
            self.help = True
            return
        if ch in (9, curses.KEY_BTAB) and self.panel != "fans":  # the fan panel keeps Tab for curves
            self.step_section(state, -1 if ch == curses.KEY_BTAB else 1)
            return
        if self.panel == "overview" and ch in (curses.KEY_UP, curses.KEY_DOWN, ord("j"), ord("k")):
            self.step_section(state, 1 if ch in (curses.KEY_DOWN, ord("j")) else -1)
            return
        if getattr(self, "panel", "main") == "keyboard":
            self.handle_keyboard(ch, state)
            return
        if ch == 27 and self.panel == "overview" and self.view == "details":
            self.view = "summary"
            return
        if getattr(self, "panel", "main") == "fans":
            self.handle_fans(ch, state)
            return

        if self.panel == "overview" and ch not in map(ord, "q?tmlf sv"):
            return  # the overview is read-only; edits belong to Power
        rows = self.rows(state)
        if not rows:
            if ch == ord("q"):
                self.running = False
            elif ch == ord("?"):
                self.help = True
            elif ch == ord("t"):
                self.toggle_details()
            elif ch == ord("l"):
                self.open_keyboard()
            elif ch == ord("f"):
                self.open_fans()
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        visit = self.shown_rows(state)
        if rows[self.selected] not in visit:
            self.selected = 0  # the mode changed to a stock mode; the cursor was on a row that is no longer drawn
        key = rows[self.selected]

        stale_staged = any(k != "mode" for k in self.pending)
        if self.locked(state) and (self.fields or self.invalid or stale_staged):
            # Limit fields only exist for custom mode; drop stale ones if the mode changed.
            self.fields.clear()
            self.invalid.clear()
            if stale_staged:
                self.pending = {k: v for k, v in self.pending.items() if k == "mode"}
                self.notice = f"Staged limit edits dropped: mode changed to {state.get('mode')}."
        unlocked_limit = key != "mode" and not self.locked(state)
        digits = range(48, 58)

        if ch == ord("q"):
            self.running = False
        elif ch == ord("?"):
            self.help = True
        elif ch == ord("m"):
            self.show_limits = True
        elif ch == ord("t"):
            self.toggle_details()
        elif ch == ord("l"):
            self.open_keyboard()
        elif ch == ord("f"):
            self.open_fans()
        elif ch == ord(" "):
            self.paused = not self.paused
            self.notice = "Telemetry paused." if self.paused else "Telemetry resumed."
        elif ch == ord("s"):
            if self.backend.remote or os.geteuid() == 0:
                self.notice = "No sudo needed."
            else:
                self.reauth()
        elif ch == ord("v"):
            self.show_results = bool(self.apply_results)
            if not self.apply_results:
                self.notice = "No apply results yet. Edit a row and press Enter."
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.selected = rows.index(visit[(visit.index(key) + 1) % len(visit)])
        elif ch in (curses.KEY_UP, ord("k")):
            self.selected = rows.index(visit[(visit.index(key) - 1) % len(visit)])
        elif ch == ord("r"):
            self.pending.clear()
            self.fields.clear()
            self.invalid.clear()
            self.notice = "Pending edits discarded."
        elif ch in (10, 13, curses.KEY_ENTER):
            if self.invalid:
                first = next(k for k in rows if k in self.invalid)
                self.selected = rows.index(first)
                self.notice = "Fix the value marked red before applying."
            elif self.pending:
                before = set(self.pending)
                try:
                    self.apply(state)
                finally:
                    for done in before - set(self.pending):
                        self.fields.pop(done, None)
            elif key == "mode":
                self.notice = "Use ← → to choose a mode, then press Enter."
            elif unlocked_limit:
                self.notice = "Type a value or use ← →, then press Enter."
            else:
                self.notice = f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter."
        elif key != "mode" and self.locked(state) and (
                ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("+"), ord("-"), ord("="), ord("d"), *digits)):
            self.notice = f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter."
        elif ch == ord("d"):
            if key == "mode":
                self.notice = "Platform mode has no default."
            else:
                default = state["attributes"][key]["default_value"]
                if default is None:
                    raise ControlError("Firmware default is unavailable.")
                self.set_field(state, key, str(default))
        elif unlocked_limit and ch == 27:
            self.set_field(state, key, "")
            self.notice = "Row cleared."
        elif unlocked_limit and ch in (curses.KEY_BACKSPACE, 127, 8):
            self.set_field(state, key, self.fields.get(key, "")[:-1])
        elif unlocked_limit and ch in digits:
            text = self.fields.get(key, "")
            if len(text) < 6:
                self.set_field(state, key, text + chr(ch))
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("+"), ord("-"), ord("=")):
            direction = 1 if ch in (curses.KEY_RIGHT, ord("+"), ord("=")) else -1
            if key == "mode":
                modes = state.get("modes", [])
                current = self.pending.get(key, state.get("mode"))
                if current in modes:
                    self.stage(state, key, modes[(modes.index(current) + direction) % len(modes)])
            else:
                item = state["attributes"][key]
                if not item["available"]:
                    raise ControlError("This control is unavailable.")
                step = max(1, item["scalar_increment"])
                current = self.pending.get(key, item["current_value"])
                value = clamp(current + direction * step, item["min_value"], item["max_value"])
                self.set_field(state, key, str(value))

    def run(self):
        self.backend.start()
        try:
            while self.running:
                state = self.backend.state() or {}
                self.draw(state)
                ch = self.s.getch()
                h, w = self.s.getmaxyx()
                if (h < self.MIN_H or w < self.MIN_W) and ch != ord("q"):
                    continue
                if ch != -1:
                    try:
                        self.handle(ch, state)
                    except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                        self.notice = str(exc)
        finally:
            self.keyboard.close()
            self.backend.close()
