# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve panel for the dashboard (mixed into Dashboard).

Temperatures are fixed and only a step's speed is edited, in steps the module can store. One
speed drives both fans. The graph shows that speed in percent on evenly spaced steps from cool to
hot, like Lenovo Legion Toolkit; exact temperatures sit behind the details key and RPM is not shown.
"""
from __future__ import annotations

import copy
import json
import os
import threading
from threading import Lock

from .api import http_client
from .common import ControlError, _lazy, clamp, fit_parts, fmt
from .curvestore import (check_curve, clear_active, curve_options, mark_written, read_stock, save_active, save_named,
                         save_stock)
from .fancurve import SPEEDS, WRITABLE, Scale, merge_curve, min_speed, validate_curve

curses = _lazy("curses")
textwrap = _lazy("textwrap")

UNAVAILABLE = ("Fan curves need the LenovoLegionLinux legion_laptop module "
               "(load it with enable_platformprofile=0 next to lenovo-wmi-gamezone)")
FAN_ERRORS = (ControlError, OSError, ValueError, http_client.HTTPException)
BIG_STEPS = 5  # Shift+arrow


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
        self.fan_details = False  # narrow screens: the step table in place of the chart
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
            self.notice = "" if points else self.fan.get("reason", "")

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
        self.view = "summary"
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
        speed = next((target[i][f] for f in SPEEDS if target[i].get(f) is not None), None)
        self.notice = f"Step {i + 1} now {self.fan_pct_text(self.fan_scale(), speed)}. Press Enter to apply."

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
            return f"it is at its {self.fan_pct_text(scale, scale.pwm(floor))} floor"
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
            raise ControlError(f"Press Enter to retry or r to discard. Apply stopped after {done} writes: "
                               f"{failed.get('error') or failed['status']}.")
        return done

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
        problems = self.fan_record_stock(state.get("mode"))
        steps = sum(a != b for a, b in zip(self.fan_edit, self.fan_points()))
        done = self.fan_write(points)
        self.fan_edit = None
        self.notice = (f"Applied and verified {steps} step{'' if steps == 1 else 's'}." if done
                       else "No change needed.")
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
        except (ValueError, RecursionError) as exc:
            raise ControlError(stderr or "sudo fan curve apply returned unreadable output.") from exc
        return results

    def handle_fans(self, ch, state):
        self.fan_take()
        if ch == ord("q"):
            self.running = False
            return
        if ch == 27:
            self.leave_section(state)
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
        elif ch == ord("t"):
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
    def fan_pct(self, scale, pwm):
        """A stored speed as percent of the top speed, the only unit the page shows."""
        value = scale.show(pwm)
        return None if value is None else round(value * 100 / scale.top)

    def fan_pct_text(self, scale, pwm):
        value = self.fan_pct(scale, pwm)
        return "N/A" if value is None else f"{value}%"

    def draw_fans(self, state, metrics, y, x, height, width):
        """The curve as a chart, plus a step table when the box is wide (or `t` swaps the chart for
        it). A "Now" line keeps the live temperatures and fan speeds apart from the curve's settings."""
        self.fan_take()
        mode = state.get("mode")
        shown = self.fan.get("mode") if self.fan.get("available") else None
        if not self.fan_loading and shown is not None and shown != mode:
            self.fan_request()  # each platform mode has its own curve, so the one on screen is stale
        scale = self.fan_scale()
        ix, iw = x + 2, width - 4
        title = f"Fan curve · {shown} mode" if shown else "Fan curve"
        if not self.fan_points():
            loading = self.fan_loading
            state_label = "Loading" if loading else "Unavailable"
            self.box(y, x, height, width, "Fan curve", self.theme.GPU, state_label)
            if loading:
                self.put(y + 2, ix, "Reading the fan curve from the embedded controller…", self.theme.TEXT, dim=True)
                return
            lines = textwrap.wrap(UNAVAILABLE + ".", max(20, iw))
            for k, line in enumerate(lines):
                self.put(y + 2 + k, ix, line, self.theme.WARN)
            reason = self.fan.get("reason", "")
            if reason and reason != "not loaded" and "legion_laptop" not in reason:  # the paragraph above already names the module
                for k, line in enumerate(textwrap.wrap(reason, max(20, iw))):
                    self.put(y + 3 + len(lines) + k, ix, line, self.theme.TEXT, dim=True)
            return
        right = ""
        if self.fan_edit is not None:
            name = self.fan_preset or ""
            room = width - len(title) - 26
            if len(name) > room:
                name = name[:max(0, room - 1)] + "…" if room > 1 else ""
            right = f"Unsaved changes {name}" if name else "Unsaved changes"
        elif self.fan_locked(state):
            right = "Read-only"
        self.box(y, x, height, width, fit_parts([title], max(8, width - len(right) - 10)), self.theme.GPU, right)
        target = self.fan_target()
        row = y + 1
        self.put(row, ix, self.fan_now_line(metrics, scale, iw), self.theme.TEXT)
        row += 1
        self.fan_step_line(target, scale, row, ix, iw)
        row += 1
        bottom = y + height - 3  # the footer's two rows
        table_w = 36
        if iw >= 90:  # wide: chart and table side by side
            graph_w = iw - table_w - 3
            self.fan_table(target, scale, row + 1, ix + graph_w + 3, table_w, bottom - row - 1)
        else:
            graph_w = iw
        if self.fan_details and iw < 90:
            self.fan_table(target, scale, row + 1, ix, min(iw, table_w + 10), bottom - row - 1)
        else:
            graph_h = bottom - row - 2  # the axis and its tick labels sit under the graph
            if graph_h >= 4:
                self.fan_graph(target, scale, row + 1 if graph_h >= 8 else row, ix, graph_w,
                               graph_h - 1 if graph_h >= 8 else graph_h)
        self.fan_footer(state, target, scale, bottom, ix, iw)

    def fan_now_line(self, metrics, scale, w):
        "Live readings, labelled as such: the fans' speed now and the temperatures the curve follows."
        fans = [f["rpm"] for f in (metrics.get("fans") or [])[:2] if f.get("rpm") is not None]
        cpu = (metrics.get("cpu") or {}).get("temperature_c")
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {}).get("temperature_c")
        rpm = ", ".join(f"{r:.0f}" for r in fans) + " RPM"
        if not fans:
            speed = "N/A"
        elif scale.max_rpm:  # percent, like the curve; RPM only on the table view
            speed = ", ".join(f"{round(r * 100 / scale.max_rpm)}%" for r in fans)
            if self.fan_details:
                speed += f" ({rpm})"
        else:
            speed = rpm  # no top speed to take a percent of
        parts = [f"Now: fans {speed}", f"CPU {fmt(cpu, '°C')}", f"GPU {fmt(gpu, '°C')}"]
        return fit_parts(parts, w)

    def fan_step_line(self, target, scale, y, x, w):
        """The selected step: where it starts, its speed, and what it was before a staged edit.
        Fan 2 is named only when it differs from fan 1."""
        p = target[self.fan_sel]
        current = self.fan_points()
        old = current[self.fan_sel] if self.fan_sel < len(current) else {}
        speed = self.fan_pct_text(scale, p.get("fan1"))
        if p.get("fan1") is not None and scale.show(old.get("fan1")) != scale.show(p["fan1"]):
            speed += f" (was {self.fan_pct_text(scale, old.get('fan1'))})"
        parts = [f"Step {self.fan_sel + 1} of {len(target)}"]
        if p.get("cpu_up") is not None:
            parts.append(f"at CPU {p['cpu_up']}°C")
        apart = p.get("fan2") is not None and (
            self.fan_pct(scale, p.get("fan2")) != self.fan_pct(scale, p.get("fan1"))
            or self.fan_pct(scale, old.get("fan2")) != self.fan_pct(scale, old.get("fan1")))
        parts.append(f"fan 1 {speed}" if apart else f"both fans {speed}")
        if apart:  # fan 2 is named only when the fans differ, before or after the edit
            fan2 = self.fan_pct_text(scale, p["fan2"])
            if scale.show(old.get("fan2")) != scale.show(p["fan2"]):
                fan2 += f" (was {self.fan_pct_text(scale, old.get('fan2'))})"
            parts.append(f"fan 2 {fan2}")
        text = fit_parts(parts, w)
        if text != " · ".join(parts):  # short of room: drop the old speeds before a whole fan
            text = fit_parts([part.split(" (was ")[0] for part in parts], w)
        self.put(y, x, text, self.theme.WARN, True)

    def fan_table(self, target, scale, y, x, w, h):
        """Every step: the CPU, GPU and heatsink temperatures it starts at and its speed. The
        selected step is marked; a staged speed shows next to the old one."""
        current = self.fan_points()
        head = f"  {'Step':<5}{'CPU':>5}{'GPU':>5}{'Heat':>6}  Speed"
        self.put(y, x, head[:w], self.theme.TEXT, dim=True)
        rows = max(0, h - 1)
        start = clamp(self.fan_sel - rows + 1, 0, max(0, len(target) - rows))
        for i, p in enumerate(target[start:start + rows], start):
            chosen = i == self.fan_sel
            old = current[i] if i < len(current) else {}

            def temp(key):
                return "N/A" if p.get(key) is None else f"{p[key]}°"

            speed = self.fan_pct_text(scale, p.get("fan1"))
            changed = any(scale.show(old.get(f)) != scale.show(p.get(f)) for f in ("fan1", "fan2"))
            if scale.show(old.get("fan1")) != scale.show(p.get("fan1")):
                speed = f"{self.fan_pct_text(scale, old.get('fan1'))} → {speed}"
            if self.fan_pct(scale, p.get("fan2")) != self.fan_pct(scale, p.get("fan1")):
                speed += f", fan 2 {self.fan_pct_text(scale, p.get('fan2'))}"
            line = f"{'▶' if chosen else ' '} {i + 1:<5}{temp('cpu_up'):>5}{temp('gpu_up'):>5}{temp('ic_up'):>6}  {speed}"
            self.put(y + 1 + i - start, x, line[:w], self.theme.WARN if changed else self.theme.ACCENT if chosen
                     else self.theme.TEXT, chosen)

    def fan_graph(self, target, scale, y, x, w, h):
        """Speed in percent on the y axis and one vertical rail per step, evenly spaced from cool
        to hot. Each step is a dot on its rail; the area under the curve is shaded and the selected
        step is highlighted. The CPU temperature each step starts at labels its rail when there is room."""
        label_w = 6
        gx, gw = x + label_w, max(10, w - label_w)
        dim = self.theme.TEXT
        n = len(target)
        span = max(1, n - 1)
        gap = max(1, (gw - 5) // span)
        cols = [gx + 2 + min(i * gap, gw - 3) for i in range(n)]

        def row_of(percent):
            return round((1 - clamp(percent, 0, 100) / 100) * (h - 1))

        for r in range(h):
            self.put(y + r, gx - 1, "│", dim, dim=True)
        step = next(s for s in (20, 25, 50, 100) if 100 // s <= max(1, h // 2))
        for value in range(0, 101, step):
            r = y + row_of(value)
            self.put(r, x, f"{value}%".rjust(label_w - 2), dim, dim=True)
            self.put(r, gx - 1, "┤", dim, dim=True)
        self.put(y + h, gx - 1, "└" + "─" * (cols[-1] - gx + 3 if cols else gw), dim, dim=True)
        # Tick labels: the CPU temperature a step starts at, under its rail, only where it changes from the
        # step before (a run of equal temperatures reads as one), the selected step always, none that touch.
        self.put(y + h + 1, x, "°C".rjust(label_w - 2), dim, dim=True)
        end, last = 0, None
        for i, (c, p) in enumerate(zip(cols, target)):
            if p.get("cpu_up") is None:
                continue
            text = str(p["cpu_up"])
            repeat, last = p["cpu_up"] == last, p["cpu_up"]
            at = c - (len(text) - 1) // 2
            if (not repeat or i == self.fan_sel) and at > end and at + len(text) <= gx + gw:
                self.put(y + h + 1, at, text, self.theme.WARN if i == self.fan_sel else dim, i == self.fan_sel,
                         dim=i != self.fan_sel)
                end = at + len(text)
        for i, c in enumerate(cols):
            chosen = i == self.fan_sel
            for r in range(h):
                self.put(y + r, c, "┆", dim, dim=not chosen)
        marks = [(c, self.fan_pct(scale, p.get("fan1")), i) for i, (c, p) in enumerate(zip(cols, target))
                 if p.get("fan1") is not None]
        for (ac, ap, _), (bc, bp, _) in zip(marks, marks[1:]):
            for c in range(ac + 1, bc):
                if c in cols:
                    continue
                top = row_of(ap + (bp - ap) * (c - ac) / (bc - ac))
                for r in range(top, h):
                    self.put(y + r, c, "▒" if r == top else "░", dim, dim=True)
        for c, percent, i in sorted(marks, key=lambda m: m[2] == self.fan_sel):  # selected dot on top
            chosen = i == self.fan_sel
            self.put(y + row_of(percent), c, "◉" if chosen else "●", self.theme.WARN if chosen else self.theme.ACCENT, chosen)

    def fan_footer(self, state, target, scale, y, x, w):
        lines = []
        if self.fan_locked(state):
            mode = state.get("mode")
            texts = (f"This is the {mode} curve. Select custom to edit.", "Select custom to edit.")
            if not mode:
                texts = ("Mode unreadable. Select custom to edit.", "Mode unreadable.")
            text = next((t for t in texts if len(t) <= w), texts[-1])
            lines.append((text, self.theme.WARN))
        errors = validate_curve(target, scale)
        if self.fan_edit is not None and errors:
            lines.append((errors[0][2], self.theme.BAD))
        for k, (text, color) in enumerate(lines[-2:]):
            self.put(y + k, x, text[:w], color)
