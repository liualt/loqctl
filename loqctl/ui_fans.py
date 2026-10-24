# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve panel for the dashboard (mixed into Dashboard).

Temperatures are fixed and only a point's speed is edited. One speed drives both fans, and
the graph plots fan 1 speed against the CPU up temperature of each point.
"""
from __future__ import annotations

import copy
import json
import os
import threading

from .api import http_client
from .common import ControlError, _lazy, clamp
from .curvestore import preset_options, save_active, save_named
from .fancurve import SPEEDS, WRITABLE, Scale, merge_curve, min_speed, validate_curve

curses = _lazy("curses")
textwrap = _lazy("textwrap")

UNAVAILABLE = ("Fan curves need the LenovoLegionLinux legion_laptop module "
               "(load it with enable_platformprofile=0 next to lenovo-wmi-gamezone)")
FAN_ERRORS = (ControlError, OSError, ValueError, http_client.HTTPException)


def move_speed(points, i, delta, scale):
    """Change point i's speed by delta display units and set fan 1 and fan 2 to it, each kept
    between its neighbours' speeds, its floor and 0..top. Returns (points, changed)."""
    fields = [f for f in SPEEDS if points[i].get(f) is not None]
    if not fields:
        return points, False
    speed = scale.show(points[i][fields[0]]) + delta
    floor = scale.show(min_speed(points, i, scale))
    new = {}
    for field in fields:
        before = points[i - 1].get(field) if i else None
        after = points[i + 1].get(field) if i + 1 < len(points) else None
        lo = max(floor, scale.show(before) if before is not None else 0)
        hi = min(scale.top, scale.show(after) if after is not None else scale.top)
        new[field] = scale.pwm(clamp(speed, lo, max(lo, hi)))
    if all(points[i].get(f) == v for f, v in new.items()):
        return points, False
    points = copy.deepcopy(points)
    points[i].update(new)
    return points, True


class FanPanel:
    def fan_init(self):
        self.fan = {"available": False, "reason": "not loaded"}
        self.fan_sel = 0  # selected point
        self.fan_edit = None  # staged curve (full points) or None
        self.fan_base = None  # firmware curve when the panel first opened this session
        self.fan_preset = -1  # index into fan_opts, -1 = none staged
        self.fan_opts = []  # [(name, points or None)]
        self.fan_loading = False
        self.fan_result = None  # set by the loader thread, taken by the UI thread

    # --- loading (a curve read takes about a second on EC models) ---
    def fan_request(self, fresh=False, wait=False):
        if self.fan_loading:
            return
        self.fan_loading = True

        def work():
            try:
                data = self.backend.fancurve(fresh)
            except FAN_ERRORS as exc:
                data = {"available": False, "reason": str(exc)}
            self.fan_result = data
            self.fan_loading = False
        if wait:
            work()
            self.fan_take()
        else:
            threading.Thread(target=work, daemon=True).start()

    def fan_take(self):
        """Adopt a finished load. Staged edits survive only if the point count still matches."""
        data, self.fan_result = self.fan_result, None
        if data is None:
            return
        self.fan = data
        points = self.fan_points()
        if self.fan_edit is not None and (len(self.fan_edit) != len(points) or self.fan_edit == points):
            self.fan_edit = None
        if self.fan_base is None and points:
            self.fan_base = copy.deepcopy(points)
        self.fan_sel = clamp(self.fan_sel, 0, max(0, len(points) - 1))
        self.fan_refresh_options()
        if self.panel == "fans":
            self.notice = "Fan curve loaded. Esc returns." if points else self.fan.get("reason", "")

    def fan_points(self):
        return self.fan.get("points") or [] if self.fan.get("available") else []

    def fan_scale(self):
        return Scale(self.fan.get("unit"), self.fan.get("max_rpm"))

    def fan_target(self):
        return copy.deepcopy(self.fan_edit if self.fan_edit is not None else self.fan_points())

    def fan_locked(self, state):
        return state.get("mode") != "custom"

    def open_fans(self):
        if not hasattr(self, "fan"):
            self.fan_init()
        self.panel = "fans"
        self.notice = "Reading the fan curve…"
        self.fan_request()

    # --- editing ---
    def fan_move(self, direction, big=False):
        target, i = self.fan_target(), self.fan_sel
        scale = self.fan_scale()
        target, moved = move_speed(target, i, direction * scale.step(big), scale)
        if not moved:
            self.notice = f"Point {i + 1} is held by its neighbours or the limits."
            return
        self.fan_edit = None if target == self.fan_points() else target
        self.fan_preset = -1
        self.notice = "Edit staged. Press Enter to apply."

    def fan_apply(self, state):
        if self.fan_locked(state):
            raise ControlError(f"Fan curves are locked in {state.get('mode')} mode. Select custom first.")
        if self.fan_edit is None:
            self.notice = "No pending changes."
            return
        errors = validate_curve(self.fan_edit, self.fan_scale())
        if errors:
            self.fan_sel = errors[0][0]
            self.notice = errors[0][2]
            return
        points = [{f: p[f] for f in WRITABLE if p.get(f) is not None} for p in self.fan_edit]
        try:
            if self.backend.remote or os.geteuid() == 0:
                results = self.backend.write_fancurve(points)
            else:
                results = self.run_sudo_fancurve(points)
        finally:
            self.backend.refresh()
            self.fan_request(fresh=True, wait=True)
        failed = next((r for r in results if r["status"] != "APPLIED"), None)
        done = sum(r["status"] == "APPLIED" for r in results)
        if failed:
            raise ControlError(f"Fan curve apply stopped after {done} writes: {failed.get('error') or failed['status']}")
        self.fan_edit = None
        self.notice = f"Applied and verified {done} fan curve change(s)."
        if os.geteuid() == 0:
            self.notice += " Not saved as active.json while running as root."
            return
        try:
            save_active(points)
        except OSError as exc:
            self.notice += f" Could not save active.json: {exc.strerror}"

    def fan_refresh_options(self):
        if self.fan.get("available"):
            self.fan_opts = preset_options(self.fan_base or self.fan_points(), self.fan_points(), self.fan_scale())

    def fan_stage_preset(self):
        self.fan_refresh_options()
        if not self.fan_opts:
            return
        self.fan_preset = (self.fan_preset + 1) % len(self.fan_opts)
        name, points = self.fan_opts[self.fan_preset]
        if points is None:
            self.notice = f"Saved curve {name} is invalid and cannot be staged."
            return
        current = self.fan_points()
        merged = merge_curve(current, points)
        self.fan_edit = None if merged == current else merged
        changes = sum(p.get(f) != c.get(f) for p, c in zip(merged, current) for f in WRITABLE)
        self.notice = f"Preset {name} staged ({changes} changes). Press Enter to apply."

    def fan_save(self):
        if os.geteuid() == 0:
            self.notice = "Not saving while running as root; run loqctl as your user."
            return
        self.notice = f"Saved {save_named(self.fan_target())}"
        self.fan_refresh_options()

    def run_sudo_fancurve(self, points):
        stdout, stderr = self.sudo_call(["_fancurve", json.dumps(points)])
        try:
            results = json.loads(stdout)
            if not isinstance(results, list) or not all(isinstance(r, dict) and "status" in r for r in results):
                raise ValueError
        except ValueError as exc:
            raise ControlError(stderr or "sudo fan curve apply returned unreadable output.") from exc
        return results

    def handle_fans(self, ch, state):
        self.fan_take()
        if ch == ord("q"):
            self.running = False
            return
        if ch in (ord("f"), 27):
            self.panel = "main"
            self.notice = "Back to firmware controls." + (" Fan edits stay staged." if self.fan_edit else "")
            return
        count = len(self.fan_points())
        if not count:
            return
        locked = self.fan_locked(state)
        if locked and self.fan_edit is not None:
            self.fan_edit = None
            self.notice = f"Staged fan edits dropped: mode changed to {state.get('mode')}."
        shifted = {curses.KEY_SR: 1, curses.KEY_SF: -1}
        plain = {curses.KEY_UP: 1, curses.KEY_DOWN: -1}
        if ch in (9, ord("]")):
            self.fan_sel = (self.fan_sel + 1) % count
        elif ch in (curses.KEY_BTAB, ord("[")):
            self.fan_sel = (self.fan_sel - 1) % count
        elif ch == ord("W"):
            self.fan_save()
        elif locked and (ch in plain or ch in shifted or ch in (10, 13, curses.KEY_ENTER, ord("r"), ord("p"))):
            self.notice = f"Fan curves are locked in {state.get('mode')} mode. Select custom first."
        elif ch in plain or ch in shifted:
            self.fan_move(plain.get(ch) or shifted[ch], big=ch in shifted)
        elif ch == ord("r"):
            self.fan_edit, self.fan_preset = None, -1
            self.notice = "Pending fan edits discarded."
        elif ch == ord("p"):
            self.fan_stage_preset()
        elif ch in (10, 13, curses.KEY_ENTER):
            self.fan_apply(state)

    # --- drawing ---
    def draw_fans(self, state, metrics, y, x, height, width):
        self.fan_take()
        fans = metrics.get("fans") or []
        rpm = "  ".join(f"F{n + 1} {f['rpm']:.0f} RPM" if f.get("rpm") is not None else f"F{n + 1} N/A"
                        for n, f in enumerate(fans[:2])) or "RPM N/A"
        ix, iw = x + 2, width - 4
        if not self.fan.get("available"):
            loading = self.fan_loading
            self.box(y, x, height, width, "Fans · " + rpm, self.theme.GPU, "Loading" if loading else "Unavailable")
            if loading:
                self.put(y + 2, ix, "Reading the fan curve from the embedded controller…", self.theme.TEXT, dim=True)
                return
            lines = textwrap.wrap(UNAVAILABLE + ".", max(20, iw))
            for k, line in enumerate(lines):
                self.put(y + 2 + k, ix, line, self.theme.WARN)
            self.put(y + 3 + len(lines), ix, ("(" + self.fan.get("reason", "") + ")")[:iw], self.theme.TEXT, dim=True)
            self.put(y + 5 + len(lines), ix, "Esc returns to firmware controls.", self.theme.TEXT, dim=True)
            return
        scale = self.fan_scale()
        right = ("Staged · " if self.fan_edit is not None else "") + "Unit: " + scale.label
        self.box(y, x, height, width, "Fans · " + rpm, self.theme.GPU, right)
        target = self.fan_target()
        inner = height - 2
        table_h = 7 if inner >= 21 else 0  # 6 rows and a note
        graph_h = inner - table_h - 6  # selection line, axis, axis labels, 3 footer rows
        top = y + 1
        self.fan_selection_line(target, scale, top, ix, iw)
        if graph_h >= 4:
            self.fan_graph(target, scale, (metrics.get("cpu") or {}).get("temperature_c"), top + 1, ix, iw, graph_h)
            top += graph_h + 3
        else:
            top += 1
        if table_h:
            self.fan_table(target, scale, top, ix, iw)
        self.fan_footer(state, target, scale, y + height - 4, ix, iw)

    def fan_selection_line(self, target, scale, y, x, w):
        p = target[self.fan_sel]

        def temps(sensor):
            up, down = p.get(sensor + "_up"), p.get(sensor + "_down")
            return "N/A" if up is None else f"{up}°C" + (f" (down {down}°C)" if down is not None else "")
        text = (f"Point {self.fan_sel + 1}/{len(target)} · CPU {temps('cpu')} · GPU {temps('gpu')} · "
                f"Fan 1 {scale.text(p.get('fan1'))} · Fan 2 {scale.text(p.get('fan2'))}")
        self.put(y, x, text[:w], self.theme.WARN, True)

    def fan_graph(self, target, scale, cpu_temp, y, x, w, h):
        """Step plot of fan speed against CPU up temperature, with axes, grid and a live cursor."""
        label_w = 7
        gx, gw = x + label_w, max(10, w - label_w)
        temps = [p["cpu_up"] for p in target if p.get("cpu_up") is not None]
        seen = temps + ([round(cpu_temp)] if cpu_temp is not None else [])
        lo_t = max(0, (min(seen, default=30) - 5) // 10 * 10)
        hi_t = max(min(130, -(-max(seen, default=100) // 10) * 10), lo_t + 20)
        top = scale.top
        if scale.unit == "rpm":
            speeds = [scale.show(p[f]) for p in target for f in ("fan1", "fan2") if p.get(f) is not None]
            top = max(1000, -(-int(max(speeds, default=1000) * 1.1) // 1000) * 1000)

        def col(t):
            return gx + round((clamp(t, lo_t, hi_t) - lo_t) / (hi_t - lo_t) * (gw - 1))

        def row(v):
            return y + h - 1 - round(clamp(v, 0, top) / top * (h - 1))

        dim = self.theme.TEXT
        step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 500, 1000, 2000, 2500, 5000, 10 ** 9)
                    if top / s <= max(2, min(5, h // 2)))
        for value in range(0, top + 1, step):  # horizontal grid and speed labels
            self.put(row(value), x, f"{value:>{label_w - 2}}", dim, dim=True)
            self.put(row(value), gx, "┈" * gw, dim, dim=True)
        for r in range(y, y + h):
            self.put(r, gx - 1, "│", dim, dim=True)
        self.put(y + h, gx - 1, "└" + "─" * gw, dim, dim=True)
        self.put(y + h + 1, x, scale.label, dim, dim=True)
        for t in range(lo_t, hi_t + 1, 10):  # temperature ticks
            label = f"{t}°C"
            self.put(y + h, col(t), "┴", dim, dim=True)
            self.put(y + h + 1, clamp(col(t) - len(label) // 2, gx, gx + gw - len(label)), label, dim, dim=True)
        if cpu_temp is not None and lo_t <= cpu_temp <= hi_t:  # live CPU temperature cursor
            c = col(cpu_temp)
            for r in range(y, y + h):
                self.put(r, c, "│", self.theme.WARN, dim=True)
            label = f"CPU {cpu_temp:.0f}°C"
            self.put(y, clamp(c + 1, gx, gx + gw - len(label)), label, self.theme.WARN)
        prev_t = lo_t
        for p in target:  # fan 1 runs at a point's speed up to that point's temperature
            if p.get("cpu_up") is None or p.get("fan1") is None:
                continue
            a, b = col(prev_t), col(p["cpu_up"])
            if b > a:
                self.put(row(scale.show(p["fan1"])), a, "─" * (b - a), self.theme.ACCENT)
            prev_t = p["cpu_up"]
        for field, glyph, color in (("fan2", "◆", self.theme.GPU), ("fan1", "●", self.theme.ACCENT)):
            for p in target:
                if p.get("cpu_up") is not None and p.get(field) is not None:
                    self.put(row(scale.show(p[field])), col(p["cpu_up"]), glyph, color, True)
        p = target[self.fan_sel]
        if p.get("cpu_up") is not None and p.get("fan1") is not None:
            self.put(row(scale.show(p["fan1"])), col(p["cpu_up"]), "◉", self.theme.WARN, True)

    def fan_table(self, target, scale, y, x, w):
        """Every point's values; staged changes in the warning color."""
        current = self.fan_points()
        label_w = 7
        cw = max(5, (w - label_w) // max(1, len(target)))
        rows = (("Point", None), ("CPU", ("cpu_up", "cpu_down")), ("GPU", ("gpu_up", "gpu_down")),
                ("IC", ("ic_up", "ic_down")), ("Fan 1", ("fan1",)), ("Fan 2", ("fan2",)))
        for r, (label, fields) in enumerate(rows):
            self.put(y + r, x, label, self.theme.TEXT, dim=True)
            for i, p in enumerate(target):
                cx = x + label_w + i * cw
                if cx + cw - 1 > x + w:
                    break
                chosen = i == self.fan_sel
                if fields is None:
                    self.put(y + r, cx, f"{i + 1:>{cw - 1}}", self.theme.ACCENT, bold=chosen, dim=not chosen)
                    continue
                if fields[0] in ("fan1", "fan2"):
                    value = scale.show(p.get(fields[0]))
                    text = "N/A" if value is None else str(value)
                else:
                    up, down = p.get(fields[0]), p.get(fields[1])
                    text = "N/A" if up is None else f"{up}" + (f"/{down}" if down is not None and cw >= 7 else "")
                changed = i < len(current) and any(p.get(f) != current[i].get(f) for f in fields)
                color = self.theme.WARN if changed else self.theme.TEXT
                self.put(y + r, cx, f"{text:>{cw - 1}}"[: cw - 1], color, bold=chosen, dim=not (chosen or changed))
        self.put(y + len(rows), x, "Temperatures are up/down in °C.  Graph: ● Fan 1  ◆ Fan 2  ◉ selected point",
                 self.theme.TEXT, dim=True)

    def fan_footer(self, state, target, scale, y, x, w):
        lines = []
        if self.fan_locked(state):
            lines.append((f"Fan curves are locked in {state.get('mode')} mode. Select custom to edit.", self.theme.WARN))
        errors = validate_curve(target, scale)
        if self.fan_edit is not None and errors:
            lines.append((errors[0][2], self.theme.BAD))
        cx = x
        self.put(y, cx, "Presets", self.theme.TEXT, dim=True)
        cx += 9
        for k, (name, points) in enumerate(self.fan_opts):
            label = f"{name} (invalid)" if points is None else name
            if cx + len(label) >= x + w:
                break
            color = self.theme.BAD if points is None else self.theme.WARN if k == self.fan_preset else self.theme.TEXT
            self.put(y, cx, label, color, bold=k == self.fan_preset, dim=color == self.theme.TEXT)
            cx += len(label) + 2
        for k, (text, color) in enumerate(lines[-2:]):
            self.put(y + 1 + k, x, text[:w], color)
