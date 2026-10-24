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
from .common import (BAR_EMPTY, BAR_FILL, FIELDS, SPARK, ControlError, SudoExpired, _lazy, clamp, fmt,
                     sudo_run, unit_sep)
from .hardware import GAMEZONE
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
    show_limits = False

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
        self.panel = "main"  # main | keyboard | fans
        self.view = "summary"  # summary | sensors (main panel only)
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

        if self.panel == "keyboard":
            self.draw_keyboard(1, 0, h - 3, w)
        elif self.panel == "fans":
            self.draw_fans(state, metrics, 1, 0, h - 3, w)
        else:
            self.draw_main(state, metrics, 1, 0, h - 3, w)

        # Status + keys
        stamp = metrics.get("timestamp")
        stale = stamp is not None and time.time() - stamp > 5
        status = state.get("error") or (("Sensor data is stale. " if stale else "") + self.notice)
        warn = bool(state.get("error") or stale)
        if self.sudo_expired and not self.backend.remote and os.geteuid() != 0:
            status, warn = "sudo not authenticated · press s to authenticate · edits stay staged", True
        self.put(h - 2, 1, status[: w - 3], self.theme.WARN if warn else self.theme.TEXT)
        if self.panel == "keyboard":
            keys = "1–4 zone  ←→ value  ↑↓ HSV  e effect  b bright  c copy-all  Space live  Enter apply  Esc back  q quit"
        elif self.panel == "fans":
            keys = "Tab/[ ] point  ←→ temp  ↑↓ speed  Shift bigger step  Enter apply  r discard  p preset  W save  Esc back  q quit"
        else:
            keys = "↑↓ select  0-9 type  ←→ adjust  Enter apply  Esc clear  d default  r discard  l keyboard  f fans  t sensors  m modes  Space pause  v results  ? help  q"
        self.put(h - 1, 1, keys[: w - 3], self.theme.ACCENT, dim=True)

        if self.help:
            self.draw_help()
        if self.show_results:
            self.draw_results()
        if self.show_limits:
            self.draw_limits(state)
        self.s.refresh()

    def draw_main(self, state, metrics, y, x, height, width):
        left_w = clamp(width * 42 // 100, 34, 58)
        right_x = x + left_w + 1
        right_w = width - left_w - 1
        self.draw_controls(state, y, x, height, left_w)
        if self.view == "sensors":
            self.draw_sensors(metrics, y, right_x, height, right_w)
        else:
            self.draw_telemetry(state, metrics, y, right_x, height, right_w)

    def draw_controls(self, state, y, x, height, width):
        pending_n = len(self.pending)
        right = f"{pending_n} staged" if pending_n else "Live"
        if self.invalid:
            right = (f"{pending_n} staged" if pending_n else "") + f"{', ' if pending_n else ''}{len(self.invalid)} invalid"
        self.box(y, x, height, width, "Firmware", self.theme.ACCENT, right)
        self.put(y + 1, x + 2, f"{'Control':<22} Live", self.theme.TEXT, dim=True)
        rows = self.rows(state)
        if not rows:
            self.put(y + 3, x + 2, "No firmware attributes exposed.", self.theme.WARN)
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        list_top = y + 2
        detail_h = 4
        list_h = max(3, height - detail_h - 3)
        visible = list_h
        start = 0
        if self.selected >= visible:
            start = self.selected - visible + 1
        for i, key in enumerate(rows[start:start + visible]):
            row = start + i
            yy = list_top + i
            chosen = row == self.selected
            marker = "▶" if chosen else " "
            if key == "mode":
                label, current, unit = "Platform mode", state.get("mode", "N/A"), ""
            else:
                item = state["attributes"][key]
                label, current, unit = item["label"], item.get("effective_value"), item["unit"]
            staged = self.pending.get(key)
            value = "N/A" if current is None else f"{current}{unit_sep(unit)}{unit}"
            if key != "mode" and self.locked(state):
                line = f"{marker} {label:<20} {value}"
                self.put(yy, x + 1, line[: width - 3], self.theme.TEXT, bold=chosen, dim=True)
                continue
            if key != "mode":
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
            color = self.theme.WARN if staged is not None else (self.theme.ACCENT if chosen else self.theme.TEXT)
            line = f"{marker} {label:<20} {value}"
            self.put(yy, x + 1, line[: width - 3], color, bold=chosen, dim=not chosen and staged is None)

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
                note = f"{mode} limit from firmware table · custom default {fmt(item['default_value'])}"
            else:
                reason = state.get("mode_limits_error")
                note = limit_note(state, {key: item}) or (
                    reason if reason and not reason.startswith("needs root") else
                    "Stock-mode limits need root: press s or run sudo loqctl serve")
            self.put(detail_y + 1, x + 2, note[: width - 4], self.theme.WARN)
            self.put(detail_y + 2, x + 2, f"Limits are locked in {mode} mode; select custom on the mode row."[: width - 4],
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
                     f"{fmt(item['min_value'])}–{fmt(item['max_value'])} {item['unit']}  ·  step {step_txt}  ·  default {fmt(item['default_value'])}"
                     [: width - 4], self.theme.TEXT, dim=True)

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
        cpu_right = " · ".join(filter(None, [
            fmt(cpu.get("utilization_pct"), "%"),
            fmt(cpu.get("temperature_c"), "°C"),
            fmt(cpu.get("power_w"), "W", 1),
        ]))
        throttled = bool((throttle.get("cpu") or {}).get("active"))
        if throttled:
            cpu_right = (cpu_right + " · " if cpu_right else "") + "THROTTLE"
        self.box(y, x, cpu_h, width, "CPU", self.theme.OK, cpu_right)
        if throttled:
            self.put(y, x + width - 11, "THROTTLE", self.theme.BAD, True)
        ix, iw = x + 2, width - 4
        self.bar(y + 1, ix, iw, cpu.get("utilization_pct"), self.theme.load(cpu.get("utilization_pct")))
        temp_c = cpu.get("temperature_c")
        self.put(y + 2, ix,
                 f"avg {fmt(cpu.get('clock_mhz'), ' MHz')}   max {fmt(cpu.get('max_clock_mhz'), ' MHz')}   "
                 f"{fmt(temp_c, '°C')}"[:iw],
                 self.theme.heat(temp_c))
        if cpu_h >= 7:
            self.chart(y + 3, ix, iw, "cpu", color=self.theme.OK)
        pl1, pl2 = limit("ppt_pl1_spl"), limit("ppt_pl2_sppt")
        cpu_label = f"W {fmt(cpu.get('power_w'), '', 1)} · PL1 {fmt(pl1)} · PL2 {fmt(pl2)}"
        cpu_ceiling = max(pl2 or 0, max((v for v in self.history["cpu_w"] if v is not None), default=0), 1)
        cpu_marks = [(v, c) for v, c in ((pl1, self.theme.WARN), (pl2, self.theme.BAD)) if v is not None]
        if cpu_h >= 9:
            self.put(y + 4, ix, cpu_label[:iw], self.theme.WARN, dim=True)
            self.chart_rows(y + 5, ix, iw, 3, "cpu_w", cpu_ceiling, self.theme.WARN, cpu_marks)
            if cpu_h >= 10:
                cores = list((cpu.get("cores") or {}).values())
                spark = "".join("·" if c is None else SPARK[min(7, round((c or 0) * 7 / 100))] for c in cores)
                self.put(y + 8, ix, f"cores {spark}"[:iw], self.theme.OK)
            if cpu_h >= 11:
                self.put(y + 9, ix, (cpu.get("power_source") or "Sampling")[:iw], self.theme.TEXT, dim=True)
        elif cpu_h >= 8:
            self.put(y + 4, ix, cpu_label[:iw], self.theme.WARN, dim=True)
            self.chart(y + 4, ix + len(cpu_label) + 1, iw - len(cpu_label) - 1, "cpu_w", cpu_ceiling, self.theme.WARN)

        # GPU
        gy = y + cpu_h
        gpu_right = " · ".join(filter(None, [
            fmt(gpu.get("utilization_pct"), "%"),
            fmt(gpu.get("temperature_c"), "°C"),
            fmt(gpu.get("power_w"), "W", 1),
        ])) if gpu else "Asleep" if gpu_meta.get("suspended") else "Offline"
        self.box(gy, x, gpu_h, width, "GPU", self.theme.GPU, gpu_right)
        name = gpu.get("name") or gpu_meta.get("error") or "Sampling"
        self.put(gy + 1, ix, name[:iw], self.theme.GPU, True)
        if gpu:
            self.bar(gy + 2, ix, iw, gpu.get("utilization_pct"), self.theme.load(gpu.get("utilization_pct")))
            self.put(gy + 3, ix,
                     f"core {fmt(gpu.get('clock_mhz'), ' MHz')}  mem {fmt(gpu.get('memory_clock_mhz'), ' MHz')}"[:iw],
                     self.theme.TEXT, dim=True)
            r, last = gy + 4, gy + gpu_h - 2
            if r <= last and gpu_h >= 7:
                self.put(r, ix,
                         f"vram {fmt(gpu.get('memory_used_mib'))}/{fmt(gpu.get('memory_total_mib'), ' MiB')}  "
                         f"limit {fmt(gpu.get('power_limit_w'), ' W')}"[:iw],
                         self.theme.TEXT, dim=True)
                r += 1
            reasons = gpu.get("throttle") or []
            if reasons and r <= last:
                self.put(r, ix, ("throttle: " + ", ".join(reasons))[:iw], self.theme.BAD)
                r += 1
            if r <= last and gpu_h >= 8:
                self.chart(r, ix, iw, "gpu", color=self.theme.GPU)
                r += 1
            ctgp, plimit = limit("gpu_nv_ctgp"), gpu.get("power_limit_w")
            gpu_label = f"W {fmt(gpu.get('power_w'), '', 1)} · cTGP {fmt(ctgp)} · limit {fmt(plimit)}"
            gpu_ceiling = max(ctgp or 0, plimit or 0, max((v for v in self.history["gpu_w"] if v is not None), default=0), 1)
            gpu_marks = [(v, c) for v, c in ((ctgp, self.theme.WARN), (plimit, self.theme.BAD)) if v is not None]
            if gpu_h >= 9 and last - r + 1 >= 4:
                self.put(r, ix, gpu_label[:iw], self.theme.WARN, dim=True)
                self.chart_rows(r + 1, ix, iw, min(3, last - r), "gpu_w", gpu_ceiling, self.theme.WARN, gpu_marks)
            elif gpu_h >= 9 and r <= last:
                self.put(r, ix, gpu_label[:iw], self.theme.WARN, dim=True)
                self.chart(r, ix + len(gpu_label) + 1, iw - len(gpu_label) - 1, "gpu_w", gpu_ceiling, self.theme.WARN)

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
        bat = f"{fmt(battery.get('percent'), '%')} {battery.get('status') or ''}".strip()
        self.put(sy + 2, ix, f"{power}  {bat}"[:iw], self.theme.OK if ac else self.theme.WARN)
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

    def draw_sensors(self, metrics, y, x, height, width):
        self.box(y, x, height, width, "Sensors", self.theme.INFO, "t: summary")
        ix, iw = x + 2, width - 4
        na = lambda v, unit="", d=1: fmt(v, unit, d)
        lines = [("Temperatures", self.theme.ACCENT, True)]
        temps = metrics.get("temperatures") or []
        lines += [(f"  {t['group']:<6} {t['label']:<24} {na(t['c'], '°C')}", self.theme.heat(t["c"]), False)
                  for t in temps] or [("  N/A", self.theme.TEXT, False)]
        power = metrics.get("power") or {}
        lines.append(("Power", self.theme.ACCENT, True))
        for name, watts in (power.get("rapl") or {}).items():
            lines.append((f"  {name:<24} {na(watts, ' W')}", self.theme.TEXT, False))
        if "battery_w" in power:
            lines.append((f"  {'battery':<24} {na(power.get('battery_w'), ' W')}  {power.get('battery_status') or 'N/A'}",
                          self.theme.TEXT, False))
        for u in power.get("usbc") or []:
            lines.append((f"  {u['name'][:24]:<24} {na(u['watts'], ' W')}  {na(u['volts'], ' V', 2)}  "
                          f"{na(u['amps'], ' A', 2)}", self.theme.TEXT, False))
        lines.append(("Voltages", self.theme.ACCENT, True))
        lines += [(f"  {v['label'][:30]:<30} {na(v['v'], ' V', 3)}", self.theme.TEXT, False)
                  for v in metrics.get("voltages") or []] or [("  N/A", self.theme.TEXT, False)]
        throttle = metrics.get("throttle") or {}
        cpu_t, gpu_t = throttle.get("cpu") or {}, throttle.get("gpu")
        lines.append(("Throttle", self.theme.ACCENT, True))
        lines.append((f"  cpu core {na(cpu_t.get('core_events'), '', 0)}  package {na(cpu_t.get('package_events'), '', 0)}"
                      f"  active {'N/A' if cpu_t.get('active') is None else 'yes' if cpu_t['active'] else 'no'}",
                      self.theme.BAD if cpu_t.get("active") else self.theme.TEXT, False))
        lines.append(("  gpu " + ("N/A" if gpu_t is None else ", ".join(gpu_t) or "none"),
                      self.theme.BAD if gpu_t else self.theme.TEXT, False))
        room = height - 2
        if len(lines) > room:
            lines = lines[:max(0, room - 1)] + [("…", self.theme.TEXT, False)]
        for i, (text, color, bold) in enumerate(lines):
            self.put(y + 1 + i, ix, text[:iw], color, bold)

    def draw_keyboard(self, y, x, height, width):
        kb = self.keyboard
        status = "Live" if self.kb_live else "Preview off"
        if not kb.available:
            status = "Missing"
        self.box(y, x, height, width, "Keyboard (LegionAura)", self.theme.GPU, status)
        ix, iw = x + 2, width - 4
        if not kb.available:
            self.put(y + 2, ix, "Install LegionAura (AUR: legionaura) for 4-zone RGB control.", self.theme.WARN)
            self.put(y + 4, ix, "Esc returns to firmware controls.", self.theme.TEXT, dim=True)
            return

        # Effect row
        effects = []
        for name in kb.EFFECTS:
            tag = f"[{name}]" if name == kb.effect else f" {name} "
            effects.append(tag)
        self.put(y + 1, ix, "effect  " + " ".join(effects), self.theme.ACCENT)
        bright = "low" if kb.brightness == 1 else "high"
        wave = f"  wave {kb.wave_dir}" if kb.effect == "wave" else ""
        self.put(y + 2, ix, f"brightness  {bright}   speed {kb.speed}/4{wave}", self.theme.TEXT, dim=True)

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
        self.put(cy, ix, f"zone {self.kb_zone + 1}  HSV picker  (terminal swatch ≈ nearest ANSI)", self.theme.ACCENT)
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
        self.put(tip_y, ix, "Hardware shows the true color. UI uses your terminal theme only.", self.theme.TEXT, dim=True)
        self.put(tip_y + 1, ix, f"config {kb.config}"[:iw], self.theme.TEXT, dim=True)

    def draw_help(self):
        h, w = self.s.getmaxyx()
        bw = min(86, w - 4)
        bh = min(21, h - 2)
        bx, by = (w - bw) // 2, max(1, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "Help", self.theme.ACCENT)
        lines = [
            "Firmware  j/k or ↑↓ select   ←/→ or -/+ step the value",
            "          0-9 type a value   Backspace delete   Esc clear row",
            "          Enter apply all    d custom default   r discard all",
            "          v last apply results   Limits need custom mode (Enter on mode row)",
            "          s sudo authenticate (shown when needed)",
            "          m mode limits overlay: every mode's firmware limits",
            "",
            "Keyboard  l open lighting panel   Esc back to firmware",
            "          1–4 select zone   ←/→ adjust HSV   ↑↓ channel",
            "          e cycle effect   b brightness   w wave dir",
            "          c copy color to all zones   Space toggle live apply",
            "          Enter apply via legionaura",
            "",
            "Fans      f open fan curves (edits need custom mode)   Tab/[ ] select point",
            "          ↑↓ speed (both fans)   Shift+arrow bigger step",
            "          Enter apply   r discard   R reset to stock   p next curve   W save   Esc back",
            "Global    t sensors view   Space pause telemetry   ? help   q quit",
            "Colors use your terminal ANSI palette only (no custom RGB UI).",
            "Press ? or Esc to close.",
        ]
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
        controls = self.backend.controls()
        if key == "mode":
            return controls.get("mode")
        return controls.get("attributes", {}).get(key, {}).get("current_value")

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
        completed = 0
        stopped = None
        if any(k != "mode" for k in self.pending) and self.locked(state):
            raise ControlError(f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter.")
        if not (self.backend.remote or os.geteuid() == 0):
            results = self.run_sudo_apply([[k, v] for k, v in changes])
            if results:
                self.apply_results, self.show_results = results, True
            for result in results:
                if result["status"] == "APPLIED":
                    self.pending.pop(result["key"], None)
                    self.fields.pop(result["key"], None)
                    completed += 1
            failed = next((r for r in results if r["status"] != "APPLIED"), None)
            if failed:
                raise ControlError(f"Apply stopped after {completed} writes: {failed.get('error') or failed['status']}")
            self.notice = f"Applied and verified {completed} change(s)."
            return
        results = self.apply_results = []  # changes is non-empty: every pass appends a result
        self.show_results = True
        for key, value in changes:
            result = {"key": key, "requested": value, "before": None,
                      "current": None, "status": "NOT SENT", "error": None}
            try:
                result["before"] = self.read_current(key)
            except (ControlError, OSError, ValueError, http_client.HTTPException):
                pass
            if stopped:
                result["current"] = result["before"]
                results.append(result)
                continue
            error = None
            try:
                self.backend.write(key, value)
            except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                error = str(exc)
            write_failed = error is not None
            try:
                result["current"] = self.read_current(key)
            except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                error = error or "Could not read firmware after the write: " + str(exc)
            if error or result["current"] != value:
                result["status"] = "UNVERIFIED" if result["current"] is None and not write_failed else "FAILED"
                result["error"] = error or f"Requested {value}, but firmware reports {result['current']!r}."
                stopped = result["error"]
            else:
                result["status"] = "APPLIED"
                self.pending.pop(key)
                completed += 1
            results.append(result)
        if stopped:
            raise ControlError(f"Apply stopped after {completed} writes: {stopped}")
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

    def handle_keyboard(self, ch):
        kb = self.keyboard
        if ch in (27, ord("l")):
            self.panel = "main"
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
        if getattr(self, "panel", "main") == "keyboard":
            self.handle_keyboard(ch)
            return
        if getattr(self, "panel", "main") == "fans":
            self.handle_fans(ch, state)
            return

        rows = self.rows(state)
        if not rows:
            if ch == ord("q"):
                self.running = False
            elif ch == ord("t"):
                self.view = "sensors" if self.view == "summary" else "summary"
            elif ch == ord("l"):
                self.panel = "keyboard"
                self.keyboard.recheck()
                self.keyboard.load()
                self.notice = "Keyboard lighting."
            elif ch == ord("f"):
                self.open_fans()
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
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
            self.view = "sensors" if self.view == "summary" else "summary"
        elif ch == ord("l"):
            self.panel = "keyboard"
            self.keyboard.recheck()
            self.keyboard.load()
            self.notice = "Keyboard lighting. Esc returns."
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
            self.selected = (self.selected + 1) % len(rows)
        elif ch in (curses.KEY_UP, ord("k")):
            self.selected = (self.selected - 1) % len(rows)
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
