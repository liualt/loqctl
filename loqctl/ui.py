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

def battery_left(b):
    "Time text for one battery metrics dict, such as '2 h 15 min left' or '40 min to full'; '' when unknown."
    minutes = b.get("time_remaining_min")
    if minutes is None:
        return ""
    h, m = divmod(round(minutes), 60)
    span = f"{h} h {m:02d} min" if h else f"{m} min"
    return f"{span} {'to full' if b.get('time_kind') == 'to full' else 'left'}"


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


# One-line meaning of each limit for the Power detail strip (the firmware ids stay in the API).
DESCRIBE = {
    "ppt_pl1_spl": "power the CPU can hold indefinitely",
    "ppt_pl2_sppt": "short-term CPU power for bursts",
    "ppt_pl3_fppt": "very short CPU power spike ceiling",
    "ppt_pl4_ipl": "absolute CPU peak current limit",
    "ppt_cpu_cl": "CPU power cap while the GPU is loaded",
    "ppt_pl1_spl_cl": "sustained CPU power while the GPU is loaded",
    "ppt_pl2_sppt_cl": "burst CPU power while the GPU is loaded",
    "ppt_pl3_fppt_cl": "spike CPU power while the GPU is loaded",
    "ppt_pl4_ipl_cl": "peak CPU current while the GPU is loaded",
    "ppt_pl1_apu_spl": "sustained power for the integrated graphics",
    "ppt_pl1_tau": "how long the CPU may boost above sustained",
    "cpu_temp": "CPU temperature where it starts to throttle",
    "gpu_nv_ctgp": "extra GPU power on top of its base",
    "gpu_nv_ppab": "power shifted between CPU and GPU",
    "gpu_nv_ac_offset": "GPU power offset when on AC",
    "gpu_nv_bpl": "GPU power at base clocks",
    "gpu_nv_cpu_boost": "GPU power gained from CPU headroom",
    "gpu_temp": "GPU temperature where it starts to throttle",
}
# legion_laptop read-only limits and the firmware attribute that holds the same value.
EXTRA_TWIN = {"cpu_temperature_limit": "cpu_temp", "gpu_temperature_limit": "gpu_temp", "cpu_l1_tau": "ppt_pl1_tau",
              "cpu_cross_loading_powerlimit": "ppt_cpu_cl", "gpu_power_target_offset": "gpu_nv_ac_offset",
              "gpu_ppab_powerlimit": "gpu_nv_ppab"}
MODE_SHORT = {"low-power": "low", "balanced": "bal", "performance": "perf", "max-power": "max"}
ROW_SHORT = {**MODE_SHORT, "balanced": "balanced"}  # the Power mode row has room for the one long name


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
        if not self.enabled:
            # No colors: mark state by attribute so warnings stay visible.
            base = {self.BAD: curses.A_BOLD | curses.A_REVERSE, self.WARN: curses.A_BOLD}.get(color, 0)
        elif not color:
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
        "overview": ("Tab page", "t details", "m modes", "Space pause"),
        "main": ("↑↓ select", "←→ adjust", "Enter apply", "Tab page", "m modes"),
        "fans": ("←→ step", "↑↓ speed", "Enter apply", "t table", "Tab page"),
        "keyboard": ("1-4 zone", "←→↑↓ HSV", "e effect", "b bright", "Tab page"),
    }
    HINTS_READONLY = {
        "main": ("←→ mode", "m modes", "Tab page"),
        "fans": ("←→ step", "t table", "Tab page"),
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
        self.notice = backend.notice or ""
        self.history = {k: collections.deque(maxlen=240) for k in ("cpu", "gpu", "cpu_w", "gpu_w", "cpu_t", "gpu_t")}
        self.last_timestamp = None
        self.fields = {}  # key -> raw text typed for that row
        self.invalid = set()  # keys whose field text is invalid
        self.help = False
        self.running = True
        self.paused = False
        self.panel = "overview"  # overview | main (Power) | fans | keyboard
        self.view = "summary"  # summary | details (overview only)
        self.theme = Theme(curses.has_colors() and not os.environ.get("NO_COLOR"))
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

    def chart_rows(self, y, x, width, height, key, ceiling=100, color=1, markers=(), floor=0):
        """Multi-row bar chart (eighth blocks), right-aligned; markers are (value, color) lines.
        The bottom row stands for `floor`, so a temperature graph spends its rows where it moves."""
        width, height = max(0, width), max(0, height)
        if not width or not height:
            return
        ceiling = max(1, ceiling - floor)
        values = [None if v is None else v - floor for v in list(self.history[key])[-width:]]
        markers = [(None if v is None else v - floor, c) for v, c in markers]
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
                           ("cpu_w", cpu.get("power_w")), ("gpu_w", gpu.get("power_w")),
                           ("cpu_t", cpu.get("temperature_c")), ("gpu_t", gpu.get("temperature_c"))):
            self.history.setdefault(key, collections.deque(maxlen=240)).append(value)
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
        model = state.get("model") or ""
        mode = f"{state['mode']} mode" if state.get("mode") else ""
        access = "API" if self.backend.remote else "root" if os.geteuid() == 0 else "local"
        if self.paused and self.panel in ("overview", "main"):  # Space toggles it only there
            access = f"graphs paused · {access}"
        title = f" loqctl {VERSION} "
        self.put(0, 0, "━" * (w - 1), self.theme.ACCENT, dim=True)
        self.put(0, 1, title, self.theme.ACCENT, True)
        room = w - len(access) - 4 - (len(title) + 1) - len(mode) - 5
        if len(model) > room:
            model = model[: max(room - 1, 0)].rsplit(" ", 1)[0]
            model = model + "…" if model else ""
        mid = f"{model}  ·  {mode}" if model and mode else model or mode or ("" if state.get("error") else "Waiting for firmware")
        mx = max(len(title) + 1, (w - len(mid)) // 2)
        self.put(0, mx - 1, " " * (len(mid) + 2), self.theme.OK)
        self.put(0, mx, mid, self.theme.OK, bold=not model and bool(mode))
        if model and mode:
            self.put(0, mx + len(model) + 5, mode, self.theme.OK, True)
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
        if state.get("error"):
            status, warn = f"{str(state['error']).rstrip('. ')}. Run loqctl doctor for details.", True
        elif stale:
            status, warn = "Sensor data is stale. Check that the backend is running.", True
        elif state.get("stale"):
            status, warn = "Firmware values are stale. Wait for the next refresh.", True
        else:
            status, warn = self.notice, False
        if not status and self.needs_unlock():
            status, warn = "Read-only. Press s to unlock.", True
        if len(status) > w - 3:  # cut at a word boundary
            status = status[: w - 3].rsplit(" ", 1)[0].rstrip(" ,;:.") if " " in status[: w - 3] else status[: w - 3]
        self.put(h - 2, 1, status, self.theme.WARN if warn else self.theme.TEXT)
        keys = "? or Esc close  q quit" if self.help else self.footer_hint(w - 3, state)
        self.put(h - 1, 1, keys, self.theme.ACCENT)

        if self.help:
            self.draw_help(state)
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
            self.notice = ""

    def leave_section(self, state):
        "Back from Fans or Keyboard to Power, or to Overview when Power is hidden."
        self.panel = "main" if self.rows(state) else "overview"

    def open_keyboard(self):
        self.panel = "keyboard"
        self.view = "summary"
        self.keyboard.recheck()
        self.keyboard.load()
        self.notice = ""

    def step_section(self, state, direction):
        found = self.sections(state)
        at = found.index(self.panel) if self.panel in found else 0
        self.goto_section(found[(at + direction) % len(found)])

    def toggle_details(self):
        if self.panel == "overview":
            self.view = "summary" if self.view == "details" else "details"

    def draw_overview(self, state, metrics, y, x, height, width):
        if self.view != "details" and not metrics and not state.get("mode"):
            self.box(y, x, height, width, "Overview", self.theme.INFO)
            self.put(y + 2, x + 2, "No sensor data yet. Run loqctl doctor."[: width - 4], self.theme.WARN)
            return
        if self.view == "details":
            self.draw_details(metrics, y, x, height, width)
        else:
            self.draw_telemetry(state, metrics, y, x, height, width)

    def draw_main(self, state, metrics, y, x, height, width):
        self.draw_controls(state, y, x, height, width)

    def power_columns(self, state, shown, extras, iw):
        """Column widths for the limit list: (label, value, extra) where extra holds the range and
        default in custom mode, or the custom setting in a stock mode; extra is 0 when it does not fit."""
        attrs = state.get("attributes") or {}
        labels = [attrs[k]["label"] for k in shown if k in attrs] + [e["label"] for e in extras]
        lw = max([len(label) for label in labels] + [8]) + 2
        vw = 16 if not self.locked(state) else max(12, len(state.get("mode") or "") + 2)  # "60 W → 65_" while typing
        ew = 26 if not self.locked(state) else 10
        if lw + vw + ew > iw - 2:
            ew = 0
        return min(lw, max(8, iw - 2 - vw)), vw, ew

    def limit_extra(self, state, item):
        "The extra column: range and default in custom mode; the custom setting in a stock mode."
        unit = item["unit"]
        if self.locked(state):
            return fmt(item.get("current_value"), unit_sep(unit) + unit if unit else "")
        lo, hi = fmt(item.get("min_value")), fmt(item.get("max_value"))
        default = item.get("default_value")
        span = f"{lo}–{hi}{unit_sep(unit)}{unit}"
        return f"{span:<14}{'N/A' if default is None else f'{default}{unit_sep(unit)}{unit}'}"

    def draw_controls(self, state, y, x, height, width):
        attrs = state.get("attributes") or {}
        locked = self.locked(state)
        pending_n = len(self.pending)
        stamp = (state.get("metrics") or {}).get("timestamp")
        right = ("Error" if state.get("error") else "No data" if stamp is None and not state.get("mode")
                 else "Stale" if state.get("stale") else "Read-only" if locked else "")
        if pending_n or self.invalid:
            right = ", ".join(([f"{pending_n} staged"] if pending_n else []) + (
                [f"{len(self.invalid)} invalid"] if self.invalid else []))
        self.box(y, x, height, width, "Power", self.theme.ACCENT, right)
        rows = self.rows(state)
        if not rows:
            self.put(y + 2, x + 2, "No firmware attributes exposed.", self.theme.WARN)
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        shown = self.shown_rows(state)
        if rows[self.selected] not in shown:
            self.selected = 0
        ix, iw = x + 2, width - 4

        # Mode selector on top: every mode the firmware offers, the live one in brackets.
        modes, live = state.get("modes") or [], state.get("mode")
        staged_mode = self.pending.get("mode")
        names = [f"[{m}]" if m == live else m for m in modes] or [str(live or "N/A")]
        if staged_mode is not None:
            choice = f"{live or 'N/A'} → {staged_mode}   Enter applies"
        else:
            choice = "  ".join(names)
            if len(choice) > width - 14:  # abbreviate so every mode, custom included, stays visible
                for table in (ROW_SHORT, MODE_SHORT):
                    names = [(f"[{table.get(m, m)}]" if m == live else table.get(m, m)) for m in modes]
                    choice = "  ".join(names)
                    if len(choice) <= width - 14:
                        break
                else:
                    choice = fit_parts(names, width - 14, " ")
        chosen = self.selected == 0
        self.put(y + 1, x + 1, f"{'▶' if chosen else ' '} {'Mode':<7}{choice}"[: width - 3],
                 self.theme.WARN if staged_mode is not None else (self.theme.ACCENT if chosen else self.theme.TEXT),
                 bold=chosen)

        # Limits grouped by subsystem; a group name is a row in the list, not part of the selection.
        entries, group = [], None
        for row, key in enumerate(rows[1:], 1):
            if key not in shown:
                continue  # stock modes show the few key limits, not the whole list
            name = "GPU" if key.startswith("gpu_") else "CPU"
            if name != group:
                entries.append((None, name))
                group = name
            entries.append((row, key))
        extras = [e for e in state.get("extra_limits") or [] if EXTRA_TWIN.get(e.get("key")) not in shown]
        if extras:  # read-only legion_laptop values, left out when the same limit is already a row
            entries += [(None, "Firmware (read-only)")] + [("ro", item) for item in extras]
        lw, vw, ew = self.power_columns(state, [k for k in shown if k != "mode"], extras, iw - 2)
        list_top = y + 2
        if ew:
            head = f"{(live or 'Now') if locked else 'Value':<{vw}}{'custom' if locked else 'Range         Default'}"
            self.put(list_top, ix + 2 + lw, head[: iw - 2 - lw], self.theme.TEXT, dim=True)
            list_top += 1

        detail_h = 4
        note_h = 1 if locked and any(attrs[k].get("effective_value") is None for k in shown if k in attrs) else 0
        room = y + height - detail_h - list_top - note_h  # rows between the list top and the detail strip
        chart_h = 0
        if room - len(entries) >= 9 and width >= 60:  # a tall screen shows the draw next to its limits
            chart_h = room - len(entries) - 1
            room -= chart_h + 1
        visible = max(3, room)
        at = next((i for i, (row, _) in enumerate(entries) if row == self.selected), 0)
        if not any(isinstance(row, int) for row, _ in entries[at + 1:]):
            at = len(entries) - 1  # on the last editable row, scroll the read-only rows into view
        cue = len(entries) > visible
        if cue:
            visible -= 1  # the last row says how much is above and below
        start = max(0, at - visible + 1)
        if start and entries[start][0] is not None and entries[start - 1][0] is None and start - 1 + visible > at:
            start -= 1  # keep a group name above its first visible row
        window = entries[start:start + visible]
        if window and window[-1][0] is None:
            window.pop()
        for i, (row, key) in enumerate(window):
            yy = list_top + i
            if row is None:
                self.put(yy, ix, key, self.theme.INFO, bold=True)
                continue
            if row == "ro":
                value = f"{key['value']}{unit_sep(key['unit'])}{key['unit']}"
                self.put(yy, ix + 2, f"{key['label']:<{lw}}{value}"[: iw - 2], self.theme.TEXT)
                continue
            chosen = row == self.selected
            item = attrs[key]
            label, current, unit = item["label"], item.get("effective_value"), item["unit"]
            staged = self.pending.get(key)
            value = "N/A" if current is None else f"{current}{unit_sep(unit)}{unit}"
            if current is None and self.needs_unlock():
                value = "N/A (press s)"
            text, bad = self.fields.get(key), key in self.invalid
            if not locked and chosen:
                value += " → " + (f"{text}_" if text else "_")
            elif bad:
                value += f" → {text}"
            elif staged is not None:
                value += f" → {staged}"
            if len(label) > lw - 1:
                label = label[: lw - 2] + "…"
            extra = self.limit_extra(state, item) if ew else ""
            line = f"{label:<{lw}}{value:<{vw}}{extra}".rstrip()
            color = (self.theme.BAD if bad else self.theme.WARN if staged is not None
                     else self.theme.ACCENT if chosen else self.theme.TEXT)
            self.put(yy, ix, "▶" if chosen else " ", color, bold=chosen)
            self.put(yy, ix + 2, line[: iw - 2], color, bold=chosen)
        if cue:
            limit_rows = [i for i, (row, _) in enumerate(entries) if row is not None]
            above = sum(1 for i in limit_rows if i < start)
            below = sum(1 for i in limit_rows if i >= start + len(window))
            text = "  ".join(([f"↑ {above} more"] if above else []) + ([f"↓ {below} more"] if below else []))
            self.put(list_top + len(window), ix, text[:iw], self.theme.ACCENT, dim=True)
        list_end = list_top + len(window) + (1 if cue else 0)

        if note_h:  # one line saying why a limit is N/A; "select custom" is said once, below
            reason = state.get("mode_limits_error")
            note = limit_note(state, {k: attrs[k] for k in shown if k in attrs}) or (
                "Stock limits need root once per boot: press s" if reason and reason.startswith("needs root")
                and os.geteuid() != 0 else reason or "N/A")
            self.put(list_end, ix, note[:iw], self.theme.WARN)
            list_end += 1

        if chart_h:
            self.power_boxes(state, state.get("metrics") or {}, y + height - detail_h - chart_h, x + 1, chart_h,
                             width - 2, side=True)

        # Detail strip inside the box: what the selected row is and what can be done with it.
        detail_y = y + height - detail_h
        self.put(detail_y, x + 1, "─" * (width - 2), self.theme.ACCENT, dim=True)
        key = rows[self.selected]

        def say(line, text, color=self.theme.TEXT, dim=False):
            self.put(detail_y + line, ix, text if len(text) <= iw else text[:iw - 1].rsplit(" ", 1)[0] + "…", color,
                     dim=dim)

        if key == "mode":
            driver = state.get("mode_driver", GAMEZONE)
            if driver != GAMEZONE:
                via = f"{driver}, not {GAMEZONE}" if driver else f"legacy file, {GAMEZONE} not found"
                say(1, limit_note(state, state.get("attributes", {})) or f"Mode set via {via}", self.theme.WARN)
            elif state.get("mode"):
                say(1, "Platform mode: ← → picks one, Enter applies it.", self.theme.ACCENT)
            if not state.get("mode"):
                say(2, "Mode unreadable; limits stay locked.", self.theme.WARN)
            elif locked:
                say(2, "Limits are editable in custom mode only.", self.theme.TEXT)
            return
        item = attrs[key]
        describe = DESCRIBE.get(key, "")
        line1 = f"{item['label']}: {describe}" if describe else item["label"]
        if len(line1) > iw and describe:
            line1 = describe[0].upper() + describe[1:]
        if item.get("current_value") is None and not locked:
            error = item.get("read_error") or "missing"
            say(1, limit_note(state, {key: item}) or f"{item['label']}: current value unreadable ({error})", self.theme.WARN)
        else:
            say(1, line1, self.theme.ACCENT)
        if locked:
            say(2, f"Locked in {state.get('mode') or 'this'} mode. Pick custom to edit.")
            return
        step = item["scalar_increment"]
        step_txt = "N/A" if step is None else "any whole number" if not step else str(step)
        if ew:
            say(2, f"Step {step_txt}. Type a value or use ← →, then Enter. d sets the default.")
        else:
            say(2, fit_parts([f"{fmt(item['min_value'])}–{fmt(item['max_value'])} {item['unit']}",
                              f"default {fmt(item['default_value'])}", f"step {step_txt}"], iw), dim=True)

    def telemetry_box(self, y, x, height, width, name, color, parts, key, ceiling, chart_color, marks,
                      note="", alert=None, names=(), unit="W", floor=0):
        """One box: the numbers go in the title, one graph fills the inside, an optional note takes the last row."""
        if key:
            self.history.setdefault(key, collections.deque(maxlen=240))
        span = min(width - 4, self.history[key].maxlen) if key else 0  # one sample a second
        top = f"{ceiling:.0f}{'' if unit == '°C' else ' '}{unit}"
        scale = (f"{floor:.0f}-{top}, {span // 60} min" if span >= 120 else f"{floor:.0f}-{top}, {span} s") if key else ""
        room = width - len(name) - 8 - (len(scale) + 4 if scale else 0)
        if parts and room < len(parts[0]):  # the reading matters more than the scale
            scale, room = "", width - len(name) - 8
        self.box(y, x, height, width, name, color, scale)
        text = fit_parts(parts, room) if parts and room > 0 and len(parts[0]) <= room else ""
        if text:
            self.put(y, x + len(name) + 4, f"{text} ", alert if alert is not None else color, alert is not None)
        ix, iw = x + 2, width - 4
        rows = height - 2
        if note and rows > 1 and len(note) <= iw:
            self.put(y + height - 2, ix, note, self.theme.TEXT, dim=True)
            rows -= 1
        if key and rows > 0:
            self.chart_rows(y + 1, ix, iw, rows, key, ceiling, chart_color, marks, floor)
            labels = {}  # limit names at the right edge, on the line they mark
            for (value, mark_color), name in zip(marks, names):
                line = min(rows - 1, int(clamp((value - floor) / max(1, ceiling - floor), 0, 1) * rows))
                labels.setdefault(line, []).append((f"{name} {value:g}", mark_color))
            for line, found in labels.items():
                text = " ".join(t for t, _ in found)
                if len(text) < iw:
                    self.put(y + rows - line, ix + iw - len(text), text, found[-1][1], True)

    @staticmethod
    def reading(value, unit, decimals=0, what=""):
        "One title reading; a missing one names what it is, so a bare N/A never stands alone."
        return fmt(value, unit, decimals) if value is not None else f"{what} N/A".strip()

    def draw_telemetry(self, state, metrics, y, x, height, width):
        """Wide screens: power (and on tall ones temperature) graphs on the left, every other
        reading in a Sensors column on the right. Narrow screens stack CPU, GPU and System."""
        if width >= 96:
            sw = clamp(width * 36 // 100, 34, 44)
            self.draw_sensors(metrics, y, x + width - sw, height, sw)
            width -= sw
            temp_h = clamp(height // 4, 7, 10) if height >= 28 else 0
            self.power_boxes(state, metrics, y, x, height - temp_h, width)
            if temp_h:
                self.temp_boxes(state, metrics, y + height - temp_h, x, temp_h, width)
            return
        sys_h = 6 if height >= 18 else 5
        self.power_boxes(state, metrics, y, x, height - sys_h, width)
        self.system_box(metrics, y + height - sys_h, x, sys_h, width)

    def power_boxes(self, state, metrics, y, x, height, width, side=False):
        "The CPU and GPU power graphs with their limits, stacked in `height`, or side by side when `side`."
        cpu = metrics.get("cpu") or {}
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {})
        gpu_meta = metrics.get("gpu") or {}
        throttle = metrics.get("throttle") or {}
        attrs = state.get("attributes") or {}

        def limit(key):
            return (attrs.get(key) or {}).get("effective_value")

        cpu_h = max(5, height * 55 // 100)
        gpu_h = height - cpu_h
        cpu_w = gpu_w = width
        gx, gy = x, y + cpu_h
        if side:
            cpu_h = gpu_h = height
            cpu_w, gpu_w, gx, gy = width // 2, width - width // 2, x + width // 2, y

        def clock(dev):
            return [] if dev.get("clock_mhz") is None else [fmt(dev["clock_mhz"] / 1000, "GHz", 1)]

        # CPU. Short boxes have no spare row for "Waiting"; a real reason replaces the graph's bottom row.
        throttled = bool((throttle.get("cpu") or {}).get("active"))
        source = cpu.get("power_source") or ""
        sampling = cpu.get("power_w") is None and source.startswith("Waiting")
        reason = source if cpu.get("power_w") is None and not sampling else ""
        pl1, pl2 = limit("ppt_pl1_spl"), limit("ppt_pl2_sppt")
        self.telemetry_box(
            y, x, cpu_h, cpu_w, "CPU", self.theme.OK,
            (["throttling"] if throttled else []) + [self.reading(cpu.get("temperature_c"), "°C", what="temp"),
                                                     "…" if sampling else self.reading(cpu.get("power_w"), "W", 1, "power")]
            + clock(cpu),
            "cpu_w", max(pl2 or 0, max((v for v in self.history.get("cpu_w", ()) if v is not None), default=0), 1), self.theme.WARN,
            [(v, c) for v, c in ((pl1, self.theme.WARN), (pl2, self.theme.BAD)) if v is not None],
            reason, self.theme.BAD if throttled else None, [n for n, v in (("PL1", pl1), ("PL2", pl2)) if v is not None])

        # GPU
        if gpu:
            ctgp, plimit = limit("gpu_nv_ctgp"), gpu.get("power_limit_w")
            reasons = gpu.get("throttle") or []
            self.telemetry_box(
                gy, gx, gpu_h, gpu_w, "GPU", self.theme.GPU,
                (["throttling"] if reasons else []) + [self.reading(gpu.get("temperature_c"), "°C", what="temp"),
                                                       self.reading(gpu.get("power_w"), "W", 1, "power")] + clock(gpu),
                "gpu_w", max(ctgp or 0, plimit or 0, max((v for v in self.history.get("gpu_w", ()) if v is not None), default=0), 1), self.theme.WARN,
                [(v, c) for v, c in ((ctgp, self.theme.WARN), (plimit, self.theme.BAD)) if v is not None],
                ("throttle: " + ", ".join(reasons)) if reasons else "", self.theme.BAD if reasons else None,
                [n for n, v in (("CTGP", ctgp), ("limit", plimit)) if v is not None])
        else:
            self.telemetry_box(gy, gx, gpu_h, gpu_w, "GPU", self.theme.GPU,
                               ["Asleep" if gpu_meta.get("suspended") else "Offline"], None, 1, 0, (),
                               gpu_meta.get("error") or "", None)

    def temp_boxes(self, state, metrics, y, x, height, width):
        "CPU and GPU temperature graphs side by side, each with the firmware limit where it throttles."
        attrs = state.get("attributes") or {}
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {})
        left = width // 2
        for bx, bw, name, color, now, key, limit_key in (
                (x, left, "CPU temp", self.theme.OK, (metrics.get("cpu") or {}).get("temperature_c"), "cpu_t", "cpu_temp"),
                (x + left, width - left, "GPU temp", self.theme.GPU, gpu.get("temperature_c"), "gpu_t", "gpu_temp")):
            limit = (attrs.get(limit_key) or {}).get("effective_value")
            self.telemetry_box(y, bx, height, bw, name, color, [self.reading(now, "°C")], key,
                               max(100, limit or 0), self.theme.heat(now),
                               [(limit, self.theme.BAD)] if limit is not None else [], names=["limit"], unit="°C",
                               floor=30)

    TEMP_GROUPS = {"cpu": "CPU", "gpu": "GPU", "ec": "Embedded controller", "nvme": "SSD", "ram": "RAM",
                   "wifi": "Wi-Fi", "acpi": "Board"}

    def sensor_sections(self, metrics):
        "[(heading, [(label, value, color)])] for the Sensors column, most important first; empty sections left out."
        text = self.theme.TEXT
        hottest = {}
        for t in metrics.get("temperatures") or []:
            if t.get("c") is not None:
                group = self.TEMP_GROUPS.get(t.get("group"), str(t.get("group") or "Other").capitalize())
                hottest[group] = max(hottest.get(group, t["c"]), t["c"])
        temps = [(name, fmt(c, "°C"), self.theme.heat(c)) for name, c in hottest.items()]
        fans = [(f["name"], fmt(f["rpm"], " RPM"), text) for f in metrics.get("fans") or [] if f.get("rpm") is not None]
        battery = next(iter(metrics.get("battery") or []), {})
        ac = metrics.get("ac_online")
        bat = []
        if battery.get("percent") is not None:
            bat.append(("Charge", fmt(battery["percent"], "%"), self.theme.OK if ac else self.theme.WARN))
        if ac is not None or battery.get("status"):
            bat.append(("Source", "AC power" if ac else "Battery" if ac is False else battery["status"], text))
            if battery.get("status") and ac is not None:
                bat.append(("Status", battery["status"], text))
        if battery_left(battery):
            bat.append(("Time", battery_left(battery), text))
        if battery.get("health_percent") is not None:
            bat.append(("Health", fmt(battery["health_percent"], "%"), text))
        if battery.get("charge_limit"):
            bat.append(("Charge limit", battery["charge_limit"], text))
        memory = metrics.get("memory") or {}
        used, total = memory.get("used_mib"), memory.get("total_mib")
        mem = [("Used", f"{used / 1024:.1f} of {total / 1024:.1f} GiB", text)] if used is not None and total else []
        loads, uptime = metrics.get("load") or [], metrics.get("uptime_s")
        system = ([("Load", " ".join(fmt(v, decimals=2) for v in loads), text)] if loads else []) + (
            [("Uptime", f"{int(uptime // 3600)} h {int(uptime % 3600 // 60):02d} min", text)] if uptime is not None else [])
        return [(h, rows) for h, rows in (("Temperatures", temps), ("Fans", fans), ("Battery", bat), ("Memory", mem),
                                          ("System", system)) if rows]

    def draw_sensors(self, metrics, y, x, height, width):
        "Every reading the graphs leave out, as label and value rows; the details view (t) has the full lists."
        self.box(y, x, height, width, "Sensors", self.theme.INFO, "t details")
        ix, iw, room = x + 2, width - 4, height - 2
        sections = self.sensor_sections(metrics)
        if not sections:
            self.put(y + 1, ix, "No sensor data yet."[:iw], self.theme.WARN)
            return
        # Drop whole sections from the end, then shorten the temperature list, until it fits.
        while len(sections) > 1 and sum(len(r) + 1 for _, r in sections) + len(sections) - 1 > room:
            if sections[0][0] == "Temperatures" and len(sections[0][1]) > 3:
                sections[0] = (sections[0][0], sections[0][1][:-1])
            else:
                sections.pop()
        row = y + 1
        for n, (heading, rows) in enumerate(sections):
            if n:
                row += 1
            self.put(row, ix, heading[:iw], self.theme.INFO, bold=True)
            row += 1
            for label, value, color in rows:
                if row >= y + height - 1:
                    return
                value = value[:iw - 2]
                self.put(row, ix, label[: iw - len(value) - 1], self.theme.TEXT)
                self.put(row, ix + iw - len(value), value, color)
                row += 1

    def system_box(self, metrics, y, x, height, width):
        "Narrow screens: memory, battery, fans and load in one box under the graphs."
        ix, iw = x + 2, width - 4
        memory = metrics.get("memory") or {}
        used, total = memory.get("used_mib"), memory.get("total_mib")
        mem_right = f"{used / 1024:.1f}/{total / 1024:.1f} GiB" if used is not None and total else ""
        self.box(y, x, height, width, "System", self.theme.INFO, mem_right)
        if used is not None and total:
            self.bar(y + 1, ix, iw, used / total * 100, self.theme.INFO)
        battery = next(iter(metrics.get("battery") or []), {})
        ac = metrics.get("ac_online")
        bat = f"{fmt(battery['percent'], '%')} {battery.get('status') or ''}".strip() if battery.get("percent") is not None else ""
        power = ("AC" if ac else "Battery" if ac is False else "") + (f"  {bat}" if bat else "")
        left, health = battery_left(battery), battery.get("health_percent")
        parts = ([power.strip()] if power.strip() else ["Battery N/A"]) + (
            [f"limit {battery['charge_limit']}"] if battery.get("charge_limit") else []) + ([left] if left else []) + (
            [f"health {health:.0f}%"] if health is not None else []) + (
            [f"charge {battery['charge_type'].replace('_', ' ')}"] if battery.get("charge_type") else [])
        if ac and len("  ".join(parts)) + 6 <= iw:  # spell it out when the whole line still fits
            parts[0] = parts[0].replace("AC", "AC power", 1)
        self.put(y + 2, ix, fit_parts(parts, iw, "  "), self.theme.OK if ac else self.theme.WARN)
        if height >= 5:
            fans = [f for f in metrics.get("fans") or [] if f.get("rpm") is not None]
            fan_txt = "  ".join(f"{f['name']} {fmt(f['rpm'], ' RPM')}" for f in fans) if fans else "Fans N/A"
            self.put(y + 3, ix, fan_txt[:iw], self.theme.TEXT)
        if height >= 6:
            loads, uptime = metrics.get("load") or [], metrics.get("uptime_s")
            up = f"up {int(uptime // 3600)} h {int(uptime % 3600 // 60):02d} min" if uptime is not None else ""
            parts = (["load " + " ".join(fmt(v, decimals=2) for v in loads)] if loads else []) + ([up] if up else [])
            if parts:
                self.put(y + 4, ix, fit_parts(parts, iw, "  "), self.theme.TEXT, dim=True)

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
            wh = [f"{b['full_wh']:.1f}/{b['design_wh']:.1f} Wh"] if None not in (b.get("full_wh"), b.get("design_wh")) else []
            items.append(row(f"  {b['name'][:8]}", [f"health {na(b.get('health_percent'), '%', 0)}",
                                                    f"cycles {na(b.get('cycles'), '', 0)}"] + ([battery_left(b)] if battery_left(b) else []) + wh))
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
                if found:
                    lines.append((name, head, True))
                    lines += found[:left]
                else:
                    lines.append((f"{name}: N/A", head, True))
        if more or len(lines) > room:
            lines = lines[:max(0, min(len(lines), room - 1))] + [("…", text, False)]
        for i, (line, color, bold) in enumerate(lines):
            self.put(y + 1 + i, ix, line, color, bold)

    def draw_keyboard(self, y, x, height, width):
        kb = self.keyboard
        status = "changes apply now" if self.kb_live else "Preview off"
        if not kb.available:
            status = "Missing"
        self.box(y, x, height, width, "Keyboard (LegionAura)", self.theme.GPU, status)
        ix, iw = x + 2, width - 4
        if not kb.available:
            self.put(y + 2, ix, "Keyboard lighting needs LegionAura on PATH."[:iw], self.theme.WARN)
            self.put(y + 3, ix, "Get it from github.com/nivedck/LegionAura."[:iw], self.theme.TEXT)
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
            self.put(y + 5, zx, f"[Z{zi + 1}]" if chosen else f" Z{zi + 1} ", self.theme.GPU if chosen else self.theme.TEXT, bold=chosen)
            if self.theme.enabled:
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
        self.put(cy, ix, f"zone {self.kb_zone + 1}  HSV picker"[:iw], self.theme.ACCENT)
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

    HELP_GLOBAL = (
        "Tab       next page   Shift+Tab  previous page",
        "Esc       back one level, close a box, clear a row",
        "s         authenticate sudo (Power, Fans, Keyboard)",
        "?         this help   q  quit",
        "Top right: local, root or API (how loqctl runs)",
    )
    HELP_PAGES = {
        "overview": (
            "↑↓        previous or next page",
            "t         details view (Esc closes it)",
            "m         every mode's firmware limits",
            "Space     pause the graphs",
        ),
        "main": (
            "↑↓        select a row (past the edge: change page)",
            "←→        step the value, or cycle the mode",
            "0-9       type a value   Backspace  delete",
            "Enter     apply   r  discard   d  firmware default",
            "m         every mode's limits   v  last results",
            "Space     pause the graphs",
            "Limits are editable in custom mode only.",
        ),
        "fans": (
            "←→        select a step",
            "↑↓        change its speed (Shift: five steps)",
            "Enter     apply   r  discard   R  stock curve",
            "p         stage the next saved or stock curve",
            "W         save the curve on screen",
            "t         step table instead of the chart",
            "Now: is live; set is what the curve asks for.",
        ),
        "keyboard": (
            "1-4       select a zone",
            "↑↓        pick H, S or V (past the edge: change page)",
            "←→        nudge it   H L  fine steps",
            "Enter     apply   Space  toggle live apply",
            "e b w     effect, brightness, wave direction",
            "c         copy this zone's color to all zones",
        ),
    }

    HELP_READONLY = {
        "main": (
            "↑↓        select a row (past the edge: change page)",
            "←→        cycle the mode on the mode row",
            "Enter     apply the chosen mode",
            "m         every mode's limits   v  last results",
            "Space     pause the graphs",
            "Limits are editable in custom mode only.",
        ),
        "fans": (
            "←→        select a step",
            "t         step table instead of the chart",
            "W         save the curve on screen",
            "Pick custom mode on Power to edit the curve.",
        ),
    }

    def page_missing(self):
        "What the current page needs when it cannot work, else an empty string."
        if self.panel == "fans" and not self.fan_points():
            return "needs legion_laptop"
        if self.panel == "keyboard" and not self.keyboard.available:
            return "install legionaura"
        return ""

    MISSING_TEXT = {"fans": "Fan curves need the legion_laptop module.", "keyboard": "Keyboard lighting needs LegionAura."}

    def no_data(self, state):
        "True when nothing can be read or edited: the backend failed, or no sensor or firmware data has arrived."
        if state.get("error"):
            return True
        return self.panel in ("overview", "main") and not state.get("mode") and not (state.get("metrics") or {}).get("timestamp")

    def needs_unlock(self):
        return (self.sudo_expired and self.panel in ("main", "fans") and not self.page_missing()
                and not self.backend.remote and os.geteuid() != 0)

    def footer_hint(self, width, state):
        missing = self.page_missing()
        if missing or self.no_data(state):
            entries = ["Tab page"]
        elif self.panel in self.HINTS_READONLY and self.locked(state):
            entries = list(self.HINTS_READONLY[self.panel])
        else:
            entries = list(self.HINTS.get(self.panel, self.HINTS["main"]))
            if self.panel == "main" and not self.pending:
                entries.remove("Enter apply")
            if self.panel == "fans":
                if self.fan_edit is not None:
                    entries.insert(entries.index("Enter apply") + 1, "r discard")
                else:
                    entries[entries.index("Enter apply")] = "p curves"
            if self.panel == "keyboard":
                if self.keyboard.effect == "wave":
                    entries.insert(-1, "w wave")
        if self.needs_unlock():
            entries.insert(0, "s unlock")
        while entries and len("  ".join(["? help", *entries, "q quit"])) > width:
            entries.pop()
        return "  ".join(["? help", *entries, "q quit"])

    def help_lines(self, state=None):
        "The global keys, then only the keys that work on this page right now."
        if self.page_missing():
            return (*self.HELP_GLOBAL, "", self.MISSING_TEXT[self.panel])
        page = self.HELP_PAGES.get(self.panel, self.HELP_PAGES["main"])
        if state is not None and self.locked(state) and self.panel in self.HELP_READONLY:
            page = self.HELP_READONLY[self.panel]
        return (*self.HELP_GLOBAL, "", *page)

    def draw_help(self, state=None):
        "Fills the content area only, so the nav column and the status and key lines stay visible."
        h, w = self.s.getmaxyx()
        bx, by, bw, bh = self.NAV_W + 1, 1, w - self.NAV_W - 1, h - 3
        self.fill(by, bx, bh, bw)
        hint = "? or Esc to close"
        title = f"Help: {self.SECTION_LABELS.get(self.panel, 'Power')}"
        self.box(by, bx, bh, bw, title if bw >= len(title) + len(hint) + 10 else "", self.theme.ACCENT, right=hint)
        wrapped = []
        for line in self.help_lines(state):
            wrapped += textwrap.wrap(line, bw - 4, subsequent_indent=" " * 10, drop_whitespace=False) or [""]
        for i, line in enumerate(wrapped[: bh - 2]):
            self.put(by + 1 + i, bx + 2, line.rstrip(), self.theme.TEXT)

    def draw_results(self):
        h, w = self.s.getmaxyx()
        bw = min(104, w - 4)
        bh = min(h - 2, len(self.apply_results) + 11)
        bx, by = (w - bw) // 2, max(0, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "Apply results", self.theme.ACCENT)
        self.put(by + 1, bx + 2, "Firmware readback immediately after each attempted change.", self.theme.TEXT, dim=True)
        sw = max([len("Result")] + [len(r["status"]) for r in self.apply_results])
        lw = max(8, bw - 4 - 33 - 2 - sw)
        self.put(by + 3, bx + 2, f"{'Control':<{lw}}{'Before':>11}{'Requested':>11}{'Read back':>11}  Result",
                 self.theme.ACCENT, True)
        room = bh - 9
        shown = self.apply_results
        if len(shown) > room:
            shown = shown[: max(room - 1, 0)]
            self.put(by + 4 + len(shown), bx + 2, f"… {len(self.apply_results) - len(shown)} more", self.theme.TEXT, dim=True)
        for i, result in enumerate(shown):
            key = result["key"]
            label, unit = ("Platform mode", "") if key == "mode" else FIELDS[key]
            def display(value):
                return "N/A" if value is None else f"{value}{unit_sep(unit)}{unit}"
            if len(label) > lw:
                label = label[: lw - 1].rstrip() + "…"
            line = (f"{label:<{lw}}{display(result['before']):>11}{display(result['requested']):>11}"
                    f"{display(result['current']):>11}  {result['status']}")
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
        live = state.get("mode")
        heads = [f"[{MODE_SHORT.get(m, m)}]" if m == live else MODE_SHORT.get(m, m) for m in modes]
        cw = max(map(len, heads), default=0) + 2
        bw = min(w - 4, 30 + cw * len(modes))
        bh = min(h - 4, len(rows) + 6)  # stay clear of the status and key rows
        bx, by = (w - bw) // 2, 1 + (h - 3 - bh) // 2
        inner = bw - 4
        lw = clamp(inner - cw * len(modes), 14, 26)
        self.fill(1, 0, h - 3, w)  # clear the page behind, so no border fragments show around the box
        self.box(by, bx, bh, bw, "Mode limits", self.theme.ACCENT)
        self.put(by + 1, bx + 2, "Control", self.theme.ACCENT, True)
        for col, head in enumerate(heads):
            self.put(by + 1, bx + 2 + lw + cw * col, f"{head:>{cw - 1}}", self.theme.ACCENT, True)
        room = bh - 5
        shown = rows
        if len(rows) > room:
            shown = rows[: max(room - 1, 0)]
            self.put(by + 2 + len(shown), bx + 2, f"… {len(rows) - len(shown)} more", self.theme.TEXT, dim=True)
        for i, key in enumerate(shown):
            label = attrs[key]["label"]
            if len(label) >= lw:
                label = label[: lw - 2].rstrip() + "…"
            self.put(by + 2 + i, bx + 2, label, self.theme.TEXT)
            for col, mode in enumerate(modes):
                value = effective_value(mode, key, attrs[key].get("current_value"), table)
                self.put(by + 2 + i, bx + 2 + lw + cw * col, f"{fmt(value):>{cw - 1}}", self.theme.TEXT,
                         bold=mode == state.get("mode"))
        note = "Custom is live; other modes come from the firmware table."
        if table is None:
            note = "Stock limits need root once per boot: press s"
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
                unit = item.get("unit") or ""
                steps = f" in steps of {step}" if step > 1 else ""
                raise ControlError(f"{value}{unit_sep(unit)}{unit} is not allowed. Use {lo}–{hi}{unit_sep(unit)}{unit}{steps}.")
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
        except ControlError as exc:
            self.invalid.add(key)
            self.pending.pop(key, None)
            self.notice = str(exc)
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
        except (ValueError, RecursionError) as exc:
            raise ControlError(stderr or "sudo apply returned unreadable output.") from exc
        return results

    def authenticate(self):
        if self.backend.remote or os.geteuid() == 0:
            self.notice = "No sudo needed."
        else:
            self.reauth()

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
        if ch == 27:
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
        elif ch == curses.KEY_UP:
            if self.kb_channel == 0:
                self.step_section(state, -1)  # off the top edge: the previous section
            else:
                self.kb_channel -= 1
        elif ch == curses.KEY_DOWN:
            if self.kb_channel == 2:
                self.step_section(state, 1)
            else:
                self.kb_channel += 1
        elif ch == curses.KEY_LEFT:
            self.kb_nudge(-1)
        elif ch == curses.KEY_RIGHT:
            self.kb_nudge(1)
        elif ch == ord("H"):  # fine adjust
            self.kb_nudge(-1, fine=True)
        elif ch == ord("L"):
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
        if ch in (9, curses.KEY_BTAB):
            self.step_section(state, -1 if ch == curses.KEY_BTAB else 1)
            return
        if ch == ord("s") and self.panel in ("fans", "keyboard"):  # the status line offers it on every page
            self.authenticate()
            return
        if self.panel == "overview" and ch in (curses.KEY_UP, curses.KEY_DOWN):
            self.step_section(state, 1 if ch == curses.KEY_DOWN else -1)
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

        if self.panel == "overview" and ch not in map(ord, "q?tm sv"):
            return  # the overview is read-only; edits belong to Power
        rows = self.rows(state)
        if not rows:
            if ch == ord("q"):
                self.running = False
            elif ch == ord("?"):
                self.help = True
            elif ch == ord("t"):
                self.toggle_details()
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
        elif ch == ord(" "):
            self.paused = not self.paused
            self.notice = "Graph history paused; readings stay live." if self.paused else "Telemetry resumed."
        elif ch == ord("s"):
            self.authenticate()
        elif ch == ord("v"):
            self.show_results = bool(self.apply_results)
            if not self.apply_results:
                self.notice = "No apply results yet. Edit a row and press Enter."
        elif ch == curses.KEY_DOWN:
            at = visit.index(key)
            if at == len(visit) - 1:
                self.step_section(state, 1)  # off the bottom edge: the next section
            else:
                self.selected = rows.index(visit[at + 1])
        elif ch == curses.KEY_UP:
            at = visit.index(key)
            if at == 0:
                self.step_section(state, -1)
            else:
                self.selected = rows.index(visit[at - 1])
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
                ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("d"), *digits)):
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
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            direction = 1 if ch == curses.KEY_RIGHT else -1
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
        drawn = None
        try:
            while self.running:
                state = self.backend.state() or {}
                stamp = (state.get("metrics") or {}).get("timestamp")
                seen = (stamp, state.get("error"), state.get("mode"), self.keyboard.last_seq, self.notice,
                        self.s.getmaxyx(), stamp is not None and time.time() - stamp > 5,
                        self.paused, self.sudo_expired, state.get("stale"),
                        getattr(self, "fan_loading", None), getattr(self, "fan_result", None) is not None)
                if seen != drawn:
                    self.draw(state)
                    drawn = seen
                ch = self.s.getch()
                if ch != -1:
                    drawn = None  # a key may change anything on screen
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
