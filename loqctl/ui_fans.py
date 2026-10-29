# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve panel for the dashboard (mixed into Dashboard).

Temperatures are fixed and only a point's speed is edited, in steps the module can store. One
speed drives both fans, and the graph plots fan 1 speed against the CPU up temperature of each
point. GPU/IC temperatures, hysteresis and the per-point table sit behind the details key.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from threading import Lock

from .api import http_client
from .common import ControlError, _lazy, clamp
from .curvestore import (check_curve, clear_active, curve_options, mark_written, read_stock, save_active, save_named,
                         save_stock)
from .fancurve import SPEEDS, WRITABLE, Scale, merge_curve, min_speed, validate_curve

curses = _lazy("curses")
textwrap = _lazy("textwrap")

UNAVAILABLE = ("Fan curves need the LenovoLegionLinux legion_laptop module "
               "(load it with enable_platformprofile=0 next to lenovo-wmi-gamezone)")
FAN_ERRORS = (ControlError, OSError, ValueError, http_client.HTTPException)
BIG_STEPS = 5  # Shift+arrow
BRAILLE = ((0x01, 0x08), (0x02, 0x10), (0x04, 0x20), (0x40, 0x80))  # dot bit by [row][column]


def move_speed(points, i, steps, scale):
    """Move point i's speed `steps` storable speeds up (down when negative) and set fan 1 and
    fan 2 to it, each kept between its neighbours' speeds, its floor and 0..top.
    Returns (points, changed)."""
    fields = [f for f in SPEEDS if points[i].get(f) is not None]
    if not fields:
        return points, False
    speed = scale.next_speed(points[i][fields[0]], steps)
    floor = scale.show(min_speed(points, i, scale))
    new = {}
    for field in fields:
        before = points[i - 1].get(field) if i else None
        after = points[i + 1].get(field) if i + 1 < len(points) else None
        lo = max(floor, scale.show(before) if before is not None else 0)
        hi = min(scale.top, scale.show(after) if after is not None else scale.top)
        new[field] = scale.pwm(clamp(speed, lo, max(lo, hi)))
    if all(scale.same(points[i][f], v, exact=True) for f, v in new.items()):
        return points, False
    points = copy.deepcopy(points)
    points[i].update(new)
    return points, True


class FanPanel:
    def fan_init(self):
        self.fan = {"available": False, "reason": "not loaded"}
        self.fan_sel = 0  # selected point
        self.fan_edit = None  # staged curve (full points) or None
        self.fan_cycle = None  # name of the last curve `p` tried, valid or not
        self.fan_preset = None  # name of the staged option in fan_opts, None = none staged
        self.fan_opts = []  # [(name, points or None)]
        self.fan_details = False  # show GPU/IC temperatures, hysteresis and the point table
        self.fan_loading = False
        self.fan_gen = 0  # bumped by each fresh read; results from older loads are dropped
        self.fan_result = None  # set by the loader thread, taken by the UI thread
        self.fan_lock = Lock()  # guards fan_gen, fan_result and fan_loading

    # --- loading (a curve read takes about a second on EC models) ---
    def fan_request(self, wait=False):
        with self.fan_lock:
            if self.fan_loading and not wait:
                return
            self.fan_gen += 1
            gen = self.fan_gen
            self.fan_result = None  # a result stored before this read may predate a write
            self.fan_loading = True

        def work():
            try:
                data = self.backend.fancurve()
            except FAN_ERRORS as exc:
                data = {"available": False, "reason": str(exc)}
            with self.fan_lock:
                if gen != self.fan_gen:
                    return  # a newer read started meanwhile; this one may predate a write
                self.fan_result = data
                self.fan_loading = False
        if wait:
            work()
            self.fan_take()
        else:
            threading.Thread(target=work, daemon=True).start()

    def fan_take(self):
        """Adopt a finished load. Staged edits survive only if the point count still matches."""
        with self.fan_lock:
            data, self.fan_result = self.fan_result, None
        if data is None:
            return
        self.fan = data
        points = self.fan_points()
        if self.fan_edit is not None and (len(self.fan_edit) != len(points) or self.fan_edit == points):
            self.fan_edit = self.fan_preset = None
            self.fan_cycle = None
        self.fan_sel = clamp(self.fan_sel, 0, max(0, len(points) - 1))
        self.fan_refresh_options()
        if self.panel == "fans":
            self.notice = "Fan curve loaded. Esc returns." if points else self.fan.get("reason", "")

    def fan_points(self):
        return self.fan.get("points") or [] if self.fan.get("available") else []

    def fan_scale(self):
        key = (self.fan.get("unit"), self.fan.get("max_rpm"))
        if getattr(self, "_fan_scale", (None,))[0] != key:
            self._fan_scale = (key, Scale(*key))
        return self._fan_scale[1]

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
        target, moved = move_speed(target, i, direction * (BIG_STEPS if big else 1), self.fan_scale())
        if not moved:
            self.notice = f"Point {i + 1} cannot move: {self.fan_held_reason(target, i, direction)}."
            return
        self.fan_edit = None if target == self.fan_points() else target
        self.fan_preset = None
        self.fan_cycle = None
        self.notice = "Edit staged. Press Enter to apply."

    def fan_held_reason(self, points, i, direction):
        scale = self.fan_scale()
        field = next((f for f in SPEEDS if points[i].get(f) is not None), None)
        if field is None:
            return "it has no fan speed"
        speed = scale.show(points[i][field])
        j = i + direction
        if 0 <= j < len(points) and points[j].get(field) is not None and scale.show(points[j][field]) == speed:
            return f"point {j + 1} has the same speed"
        floor = scale.show(min_speed(points, i, scale))
        if direction < 0 and speed <= floor:
            return f"it is at its {scale.text(scale.pwm(floor))} floor"
        if direction > 0 and speed >= scale.top:
            return "it is at the top speed"
        return "the firmware would store the same speed"

    def fan_write(self, points):
        """Write `points` as the right user and refresh; returns the number of verified writes."""
        try:
            if self.backend.remote or os.geteuid() == 0:
                results = self.backend.write_fancurve(points)
            else:
                results = self.run_sudo_fancurve(points)
        finally:
            self.backend.refresh()
            self.fan_request(wait=True)
        failed = next((r for r in results if r["status"] != "APPLIED"), None)
        done = sum(r["status"] == "APPLIED" for r in results)
        if failed:
            raise ControlError(f"Fan curve apply stopped after {done} writes: {failed.get('error') or failed['status']}")
        return done

    def fan_apply(self, state):
        if self.fan_locked(state):
            raise ControlError(f"Fan curves are locked in {state.get('mode')} mode. Select custom first.")
        if self.fan_edit is None:
            self.notice = "No pending changes."
            return
        errors = validate_curve(self.fan_edit, self.fan_scale(), self.fan_points())
        if errors:
            self.fan_sel = errors[0][0]
            self.notice = errors[0][2]
            return
        points = [{f: p[f] for f in WRITABLE if p.get(f) is not None} for p in self.fan_edit]
        problems = self.fan_record_stock(state.get("mode"))
        done = self.fan_write(points)
        self.fan_edit = None
        self.notice = f"Applied and verified {done} fan curve change(s)."
        if problems:
            self.notice += " " + problems
        if os.geteuid() == 0:
            self.notice += " Not saved as active.json while running as root."
            return
        try:
            save_active(points)
        except OSError as exc:
            self.notice += f" Could not save active.json: {exc.strerror}"

    def fan_record_stock(self, mode):
        """Before loqctl's first write in `mode`, record the live curve as stock from a fresh read
        and mark the mode as written. Returns notice text for what failed ("" when nothing did)."""
        if os.geteuid() == 0:
            return ""
        problems = []
        try:
            data = self.backend.fancurve()
            if data.get("available") and data.get("points") and data.get("mode") == mode:
                save_stock(mode, data["points"])
        except (*FAN_ERRORS, KeyError):
            pass
        if read_stock(mode) is None:
            problems.append("The stock curve was not recorded.")
        problems += self.fan_mark(mode)
        return " ".join(problems)

    def fan_mark(self, mode):
        try:
            mark_written(mode)
        except OSError as exc:
            return [f"Could not save the stock marker: {exc.strerror or exc}."]
        return []

    def fan_reset(self, state):
        """Write the recorded stock curve of the current mode and stop re-applying a custom one."""
        mode = state.get("mode")
        self.fan_edit = self.fan_preset = None
        self.fan_cycle = None
        if os.geteuid() == 0:
            self.notice = "Reset needs the dashboard to run as your user, not root."
            return
        stock = read_stock(mode)
        marker = " ".join(self.fan_mark(mode))
        if stock is not None:
            reason = check_curve(self.fan_points(), stock, self.fan_scale())
            if reason:
                self.notice = f"The recorded stock curve no longer fits this firmware, so nothing was written: {reason}"
                return
        try:
            if stock is None:
                clear_active()
                self.notice = (f"The stock curve for {mode} mode was never recorded, so nothing was written. "
                               "The firmware restores it on the next mode change or power cycle.")
            else:
                done = self.fan_write([{f: p[f] for f in WRITABLE if p.get(f) is not None} for p in stock])
                clear_active()
                self.notice = f"Reset to the stock curve: applied and verified {done} fan curve change(s)."
        except OSError as exc:
            self.notice = f"Reset failed: {exc.strerror or exc}"
        if marker:
            self.notice += " " + marker

    def fan_refresh_options(self):
        if self.fan.get("available"):
            self.fan_opts = curve_options(self.fan.get("mode"), self.fan_points(), self.fan_scale())

    def fan_stage_preset(self):
        self.fan_refresh_options()
        if not self.fan_opts:
            self.notice = "No stock or saved curves yet."
            return
        names = [name for name, _ in self.fan_opts]
        last = next((n for n in (self.fan_cycle, self.fan_preset) if n in names), None)
        at = ((names.index(last) if last else -1) + 1) % len(names)
        self.fan_cycle = names[at]
        name, points = self.fan_opts[at]
        label = f"{name} ({at + 1}/{len(names)})"
        if points is None:
            self.notice = f"Curve {label} is invalid and cannot be staged."
            return
        current = self.fan_points()
        try:
            merged = merge_curve(current, points)
        except ControlError as exc:
            self.notice = f"Curve {label} cannot be staged: {exc}"
            return
        self.fan_preset = name
        self.fan_edit = None if merged == current else merged
        changes = sum(p.get(f) != c.get(f) for p, c in zip(merged, current) for f in WRITABLE)
        self.notice = f"Curve {label} staged ({changes} changes). Press Enter to apply."

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
            self.fan_edit = self.fan_preset = None
            self.fan_cycle = None
            self.notice = f"Staged fan edits dropped: mode changed to {state.get('mode')}."
        shifted = {curses.KEY_SR: 1, curses.KEY_SF: -1}
        plain = {curses.KEY_UP: 1, curses.KEY_DOWN: -1}
        if ch in (curses.KEY_LEFT, curses.KEY_RIGHT):
            self.fan_sel = clamp(self.fan_sel + (1 if ch == curses.KEY_RIGHT else -1), 0, count - 1)
        elif ch in (9, ord("]")):
            self.fan_sel = (self.fan_sel + 1) % count
        elif ch in (curses.KEY_BTAB, ord("[")):
            self.fan_sel = (self.fan_sel - 1) % count
        elif ch == ord("d"):
            self.fan_details = not self.fan_details
        elif ch == ord("W"):
            self.fan_save()
        elif locked and (ch in plain or ch in shifted or ch in (10, 13, curses.KEY_ENTER, ord("r"), ord("R"), ord("p"))):
            self.notice = f"Fan curves are locked in {state.get('mode')} mode. Select custom first."
        elif ch in plain or ch in shifted:
            self.fan_move(plain.get(ch) or shifted[ch], big=ch in shifted)
        elif ch == ord("r"):
            self.fan_edit = self.fan_preset = None
            self.fan_cycle = None
            self.notice = "Pending fan edits discarded."
        elif ch == ord("R"):
            self.fan_reset(state)
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
        unit = "Unit: " + scale.label
        right = unit
        if self.fan_edit is not None:
            room = width - len("Fans · " + rpm) - len(unit) - 14  # title, side padding and "Staged  · "
            name = self.fan_preset or ""
            if len(name) > room:
                name = name[:max(0, room - 1)] + "…" if room > 1 else ""
            right = f"Staged {name} · {unit}" if name else f"Staged · {unit}"
        self.box(y, x, height, width, "Fans · " + rpm, self.theme.GPU, right)
        target = self.fan_target()
        cpu_temp = (metrics.get("cpu") or {}).get("temperature_c")
        inner = height - 2
        table_h = 7 if self.fan_details and inner >= 21 else 0  # 6 rows and a note
        graph_h = inner - table_h - 5  # point line, axis, axis labels, 2 message rows
        top = y + 1
        self.fan_point_line(target, scale, cpu_temp, top, ix, iw)
        if graph_h >= 4:
            self.fan_graph(target, scale, cpu_temp, top + 1, ix, iw, graph_h)
            top += graph_h + 3
        else:
            top += 1
        if table_h:
            self.fan_table(target, scale, top, ix, iw)
        self.fan_footer(state, target, scale, y + height - 3, ix, iw)

    def fan_point_line(self, target, scale, cpu_temp, y, x, w):
        """The selected point on one line (with details on, every up/down temperature) and the live
        CPU temperature on the right."""
        p = target[self.fan_sel]
        head = f"Point {self.fan_sel + 1}/{len(target)}"
        if self.fan_details:
            def temps(sensor):
                up, down = p.get(sensor + "_up"), p.get(sensor + "_down")
                return "N/A" if up is None else f"{up}" + (f"/{down}" if down is not None else "") + "°C"
            text = (f"{head} · CPU {temps('cpu')} · GPU {temps('gpu')} · IC {temps('ic')} · "
                    f"Fan 1 {scale.text(p.get('fan1'))} · Fan 2 {scale.text(p.get('fan2'))}")
        else:
            up = p.get("cpu_up")
            text = f"{head} · {'N/A' if up is None else f'{up}°C'} · {scale.text(p.get('fan1'))}"
            if p.get("fan2") is not None and scale.show(p["fan2"]) != scale.show(p.get("fan1")):
                text += f" · fan 2 {scale.text(p['fan2'])}"
            current = self.fan_points()
            old = current[self.fan_sel] if self.fan_sel < len(current) else {}
            for field, tag in (("fan1", ""), ("fan2", "fan 2 ")):
                was = old.get(field)
                if p.get(field) is not None and scale.show(was) != scale.show(p[field]):
                    text += f" ({tag}was {scale.text(was)})"
        cpu = f"▲ CPU {cpu_temp:.0f}°C" if cpu_temp is not None else ""
        if cpu and len(text) + len(cpu) + 2 <= w:
            self.put(y, x + w - len(cpu), cpu, self.theme.WARN)
        self.put(y, x, text[:w], self.theme.WARN, True)

    def fan_graph(self, target, scale, cpu_temp, y, x, w, h):
        """Fan 1 speed against CPU up temperature: straight segments between the points drawn
        in braille dots (2x4 per cell), one marker per point with the selected one highlighted,
        and a tick on the temperature axis at the live CPU temperature."""
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

        def dot_x(t):
            return round((clamp(t, lo_t, hi_t) - lo_t) / (hi_t - lo_t) * (gw * 2 - 1))

        def dot_y(v):
            return round((1 - clamp(v, 0, top) / top) * (h * 4 - 1))

        dim = self.theme.TEXT
        step = next(s for s in (1, 2, 5, 10, 20, 25, 50, 500, 1000, 2000, 2500, 5000, 10 ** 9)
                    if top / s <= max(2, min(5, h // 2)))
        for r in range(y, y + h):
            self.put(r, gx - 1, "│", dim, dim=True)
        for value in range(0, top + 1, step):  # speed labels
            r = y + dot_y(value) // 4
            self.put(r, x, f"{value:>{label_w - 2}}", dim, dim=True)
            self.put(r, gx - 1, "┤", dim, dim=True)
        self.put(y + h, gx - 1, "└" + "─" * gw, dim, dim=True)
        self.put(y + h + 1, x, scale.label, dim, dim=True)
        for t in range(lo_t, hi_t + 1, 10):  # temperature ticks
            c, label = gx + dot_x(t) // 2, f"{t}°C"
            self.put(y + h, c, "┴", dim, dim=True)
            self.put(y + h + 1, clamp(c - len(label) // 2, gx, gx + gw - len(label)), label, dim, dim=True)
        if cpu_temp is not None and lo_t <= cpu_temp <= hi_t:  # live CPU temperature
            self.put(y + h, gx + dot_x(cpu_temp) // 2, "▲", self.theme.WARN, True)
        marks = [(i, dot_x(p["cpu_up"]), dot_y(scale.show(p["fan1"]))) for i, p in enumerate(target)
                 if p.get("cpu_up") is not None and p.get("fan1") is not None]
        cells = {}
        for (_, ax, ay), (_, bx, by) in zip(marks, marks[1:]):
            n = max(abs(bx - ax), abs(by - ay), 1)
            for k in range(n + 1):
                dx, dy = round(ax + (bx - ax) * k / n), round(ay + (by - ay) * k / n)
                cell = (dy // 4, dx // 2)
                cells[cell] = cells.get(cell, 0) | BRAILLE[dy % 4][dx % 2]
        for (r, c), bits in cells.items():
            self.put(y + r, gx + c, chr(0x2800 + bits), self.theme.ACCENT)
        for i, mx, my in sorted(marks, key=lambda m: m[0] == self.fan_sel):  # selected point on top
            chosen = i == self.fan_sel
            self.put(y + my // 4, gx + mx // 2, "●", self.theme.WARN if chosen else self.theme.ACCENT, chosen)

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
        self.put(y + len(rows), x, "Up/down temperatures in °C. The graph plots fan 1.  "
                 "Shift+↑↓ five steps  r discard  W save", self.theme.TEXT, dim=True)

    def fan_footer(self, state, target, scale, y, x, w):
        lines = []
        if self.fan_locked(state):
            lines.append((f"Fan curves are locked in {state.get('mode')} mode. Select custom to edit.", self.theme.WARN))
        errors = validate_curve(target, scale, self.fan_points())
        if self.fan_edit is not None and errors:
            lines.append((errors[0][2], self.theme.BAD))
        for k, (text, color) in enumerate(lines[-2:]):
            self.put(y + k, x, text[:w], color)
