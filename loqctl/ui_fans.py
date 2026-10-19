# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve panel for the dashboard (mixed into Dashboard)."""
from __future__ import annotations

import copy
import json
import os

from .api import http_client
from .common import ControlError, _lazy, clamp
from .curvestore import preset_options, save_active, save_named
from .fancurve import LABELS, POINT_FIELDS, SPEEDS, WRITABLE, merge_curve, pct_to_pwm, pwm_to_pct, validate_curve

curses = _lazy("curses")
textwrap = _lazy("textwrap")

UNAVAILABLE = ("Fan curves need the LenovoLegionLinux legion_laptop module "
               "(load it with enable_platformprofile=0 next to lenovo-wmi-gamezone)")
FAN_ERRORS = (ControlError, OSError, ValueError, http_client.HTTPException)


def strike(text):
    return "".join(c + "̶" for c in str(text))


class FanPanel:
    def fan_init(self):
        self.fan = {"available": False, "reason": "not loaded"}
        self.fan_cell = [0, 0]  # row index into fan_rows(), point index
        self.fan_pending = {}  # (point, field) -> staged raw value
        self.fan_fields = {}  # (point, field) -> typed text
        self.fan_invalid = set()
        self.fan_base = None  # firmware curve when the panel first opened this session
        self.fan_preset = -1  # index into fan_opts, -1 = none staged
        self.fan_opts = []  # [(name, points or None)], read from disk on open and on `p`

    def fan_load(self):
        try:
            self.fan = self.backend.fancurve()
        except FAN_ERRORS as exc:
            self.fan = {"available": False, "reason": str(exc)}
        points = self.fan.get("points") or []
        for cell in list(self.fan_pending):
            if cell[0] >= len(points) or self.fan_pending[cell] == points[cell[0]].get(cell[1]):
                self.fan_pending.pop(cell)
                self.fan_fields.pop(cell, None)

    def fan_points(self):
        return self.fan.get("points") or [] if self.fan.get("available") else []

    def fan_rows(self):
        points = self.fan_points()
        return [f for f in POINT_FIELDS if any(p.get(f) is not None for p in points)]

    def fan_target(self):
        points = copy.deepcopy(self.fan_points())
        for (i, field), value in self.fan_pending.items():
            points[i][field] = value
        return points

    def fan_unit(self):
        return self.fan.get("unit")

    def fan_locked(self, state):
        return state.get("mode") != "custom"

    def open_fans(self):
        if not hasattr(self, "fan"):
            self.fan_init()
        self.panel = "fans"
        self.fan_load()
        if self.fan_base is None and self.fan.get("available"):
            self.fan_base = copy.deepcopy(self.fan["points"])
        self.fan_refresh_options()
        self.notice = "Fan curves. Esc returns." if self.fan.get("available") else self.fan.get("reason", "")

    # --- editing ---
    def fan_display(self, field, raw):
        if raw is None:
            return "N/A"
        return str(pwm_to_pct(raw, self.fan_unit()) if field in SPEEDS else raw)

    def fan_set_field(self, cell, text):
        i, field = cell
        if not text:
            for store in (self.fan_fields, self.fan_pending):
                store.pop(cell, None)
            self.fan_invalid.discard(cell)
            return
        self.fan_fields[cell] = text
        speed = field in SPEEDS
        if not text.isdigit() or int(text) > (100 if speed else 127):
            self.fan_invalid.add(cell)
            self.fan_pending.pop(cell, None)
            self.notice = "Value is not valid for this cell."
            return
        self.fan_invalid.discard(cell)
        value = pct_to_pwm(int(text), self.fan_unit()) if speed else int(text)
        if value == self.fan_points()[i][field]:
            self.fan_pending.pop(cell, None)
        else:
            self.fan_pending[cell] = value
        self.notice = "Edit staged. Press Enter to apply."

    def fan_current_cell(self):
        rows = self.fan_rows()
        row = clamp(self.fan_cell[0], 0, len(rows) - 1)
        return rows, (clamp(self.fan_cell[1], 0, len(self.fan_points()) - 1), rows[row])

    def fan_nudge(self, cell, direction):
        i, field = cell
        value = self.fan_pending.get(cell, self.fan_points()[i][field])
        if field in SPEEDS:
            step = 10 if self.fan_unit() == "level" else 5
            text = clamp(pwm_to_pct(value, self.fan_unit()) + direction * step, 0, 100)
        else:
            text = clamp(value + direction, 0, 127)
        self.fan_set_field(cell, str(text))

    def fan_first_problem(self):
        """Jump to the first invalid cell (typed, then curve rule). True when one exists."""
        rows = self.fan_rows()
        bad = sorted(self.fan_invalid, key=lambda c: (c[0], rows.index(c[1])))
        if bad:
            self.fan_cell = [rows.index(bad[0][1]), bad[0][0]]
            self.notice = "Fix the value marked red before applying."
            return True
        errors = validate_curve(self.fan_target())
        if errors:
            i, field, message = errors[0]
            if field in rows:
                self.fan_cell = [rows.index(field), i]
            self.notice = message
            return True
        return False

    def fan_apply(self, state):
        if self.fan_locked(state):
            raise ControlError(f"Fan curves are locked in {state.get('mode')} mode. Select custom first.")
        if not (self.fan_pending or self.fan_invalid):
            self.notice = "No pending changes."
            return
        if self.fan_first_problem():
            return
        points = [{f: p[f] for f in WRITABLE if p.get(f) is not None} for p in self.fan_target()]
        try:
            if self.backend.remote or os.geteuid() == 0:
                results = self.backend.write_fancurve(points)
            else:
                results = self.run_sudo_fancurve(points)
        finally:
            self.backend.refresh()
        self.fan_load()
        failed = next((r for r in results if r["status"] != "APPLIED"), None)
        done = sum(r["status"] == "APPLIED" for r in results)
        if failed:
            raise ControlError(f"Fan curve apply stopped after {done} writes: {failed.get('error') or failed['status']}")
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
            self.fan_opts = preset_options(self.fan_base or self.fan_points(), self.fan_points(), self.fan_unit())

    def fan_stage_preset(self):
        self.fan_refresh_options()
        options = self.fan_opts
        self.fan_preset = (self.fan_preset + 1) % len(options)
        name, points = options[self.fan_preset]
        if points is None:
            self.notice = f"Saved curve {name} is invalid and cannot be staged."
            return
        current = self.fan_points()
        self.fan_pending.clear()
        self.fan_fields.clear()
        self.fan_invalid.clear()
        for i, point in enumerate(merge_curve(current, points)):
            for field in WRITABLE:
                if point.get(field) is not None and point[field] != current[i][field]:
                    self.fan_pending[(i, field)] = point[field]
        self.notice = f"Preset {name} staged ({len(self.fan_pending)} changes). Press Enter to apply."

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
        if ch == ord("q"):
            self.running = False
            return
        rows = self.fan_rows() if self.fan.get("available") else []
        cell = None
        if rows:
            rows, cell = self.fan_current_cell()
        if ch == ord("f") or (ch == 27 and not (cell and (cell in self.fan_fields or cell in self.fan_pending))):
            self.panel = "main"
            self.notice = "Back to firmware controls."
            return
        if not rows:
            return
        count = len(self.fan_points())
        locked = self.fan_locked(state)
        if locked and (self.fan_fields or self.fan_pending):
            self.fan_fields.clear()
            self.fan_pending.clear()
            self.fan_invalid.clear()
            self.notice = f"Staged fan edits dropped: mode changed to {state.get('mode')}."
        editable = cell[1] in WRITABLE
        if ch in (curses.KEY_UP, ord("k")):
            self.fan_cell[0] = (rows.index(cell[1]) - 1) % len(rows)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.fan_cell[0] = (rows.index(cell[1]) + 1) % len(rows)
        elif ch in (curses.KEY_LEFT, ord("h")):
            self.fan_cell[1] = (cell[0] - 1) % count
        elif ch in (curses.KEY_RIGHT, ord("l")):
            self.fan_cell[1] = (cell[0] + 1) % count
        elif ch == ord("W"):
            self.fan_save()
        elif ch in (10, 13, curses.KEY_ENTER, ord("r"), ord("p"), ord("+"), ord("="), ord("-"), *range(48, 58),
                    curses.KEY_BACKSPACE, 127, 8, 27) and locked:
            self.notice = f"Fan curves are locked in {state.get('mode')} mode. Select custom first."
        elif ch == ord("r"):
            self.fan_pending.clear()
            self.fan_fields.clear()
            self.fan_invalid.clear()
            self.fan_preset = -1
            self.notice = "Pending fan edits discarded."
        elif ch == ord("p"):
            self.fan_stage_preset()
        elif ch in (10, 13, curses.KEY_ENTER):
            self.fan_apply(state)
        elif ch == 27:
            self.fan_set_field(cell, "")
            self.notice = "Cell cleared."
        elif not editable:
            self.notice = f"{LABELS[cell[1]]} is read-only."
        elif ch in (curses.KEY_BACKSPACE, 127, 8):
            self.fan_set_field(cell, self.fan_fields.get(cell, "")[:-1])
        elif ch in range(48, 58):
            text = self.fan_fields.get(cell, "")
            if len(text) < 3:
                self.fan_set_field(cell, text + chr(ch))
        elif ch in (ord("+"), ord("="), ord("-")):
            self.fan_nudge(cell, -1 if ch == ord("-") else 1)

    # --- drawing ---
    def fan_footer(self, state, y, ix, iw):
        rows = self.fan_rows()
        _, cell = self.fan_current_cell()
        lines = []
        if self.fan_locked(state):
            lines.append((f"Fan curves are locked in {state.get('mode')} mode. Select custom to edit.", self.theme.WARN))
        errors = [m for i, f, m in validate_curve(self.fan_target()) if (i, f) == cell]
        if cell in self.fan_invalid:
            lines.append(("Not a valid value for this cell.", self.theme.BAD))
        lines += [(m, self.theme.BAD) for m in errors[:1]]
        lines.append(("arrows move  0-9 type  +/- step  Enter apply  r discard  p preset  W save  Esc clear/back",
                      self.theme.ACCENT))
        x = ix
        self.put(y, x, "presets", self.theme.TEXT, dim=True)
        x += 9
        for k, (name, points) in enumerate(self.fan_opts):
            label = f"{name} (invalid)" if points is None else name
            if x + len(label) >= ix + iw:
                break
            color = self.theme.BAD if points is None else self.theme.WARN if k == self.fan_preset else self.theme.TEXT
            self.put(y, x, label, color, bold=k == self.fan_preset, dim=color == self.theme.TEXT)
            x += len(label) + 2
        for k, (text, color) in enumerate(lines[-2:]):
            self.put(y + 1 + k, ix, text[:iw], color, dim=color == self.theme.ACCENT)

    def draw_fans(self, state, metrics, y, x, height, width):
        fans = metrics.get("fans") or []
        rpm = "  ".join(f"F{n + 1} {f['rpm']:.0f} RPM" if f.get("rpm") is not None else f"F{n + 1} N/A"
                        for n, f in enumerate(fans[:2])) or "RPM N/A"
        if not self.fan.get("available"):
            self.box(y, x, height, width, "fans / " + rpm, self.theme.GPU, "unavailable")
            lines = textwrap.wrap(UNAVAILABLE + ".", max(20, width - 4))
            for k, line in enumerate(lines):
                self.put(y + 2 + k, x + 2, line, self.theme.WARN)
            self.put(y + 3 + len(lines), x + 2, ("(" + self.fan.get("reason", "") + ")")[: width - 4], self.theme.TEXT, dim=True)
            self.put(y + 5 + len(lines), x + 2, "Esc returns to firmware controls.", self.theme.TEXT, dim=True)
            return
        unit = self.fan_unit() or "pwm"
        self.box(y, x, height, width, "fans / " + rpm, self.theme.GPU, "unit " + unit)
        points, rows = self.fan_points(), self.fan_rows()
        target = self.fan_target()
        ix, iw = x + 2, width - 4
        label_w = 10
        cw = max(6, (iw - label_w) // len(points))
        inner = height - 2
        chart_h = min(8, inner - len(rows) - 6)
        top = y + 1
        if chart_h >= 2:
            for r, mark in enumerate(("100", "50", "0")):
                self.put(top + (0, (chart_h - 1) // 2, chart_h - 1)[r], ix, f"{mark:>4}%", self.theme.TEXT, dim=True)
            for i, point in enumerate(target):
                for fan, (field, glyph, color) in enumerate((("fan1", "●", self.theme.ACCENT),
                                                            ("fan2", "◆", self.theme.GPU))):
                    if point.get(field) is None:
                        continue
                    pct = pwm_to_pct(point[field], self.fan_unit())
                    row = top + (chart_h - 1) - round(pct / 100 * (chart_h - 1))
                    self.put(row, ix + label_w + i * cw + fan * 2, glyph, color, True)
            self.put(top + chart_h, ix, "● fan1  ◆ fan2", self.theme.TEXT, dim=True)
            top += chart_h + 1
        for i in range(len(points)):
            self.put(top, ix + label_w + i * cw, f" #{i + 1}", self.theme.ACCENT, True)
        _, selected = self.fan_current_cell()
        for r, field in enumerate(rows):
            row = top + 1 + r
            readonly = field not in WRITABLE
            self.put(row, ix, f"{LABELS[field]:<{label_w}}", self.theme.TEXT, dim=readonly)
            for i, point in enumerate(points):
                cell = (i, field)
                shown = self.fan_display(field, point.get(field))
                color, bold = self.theme.TEXT, False
                body = f"{shown:<{cw - 1}}"
                if cell in self.fan_fields:
                    typed = "→" + self.fan_fields[cell]
                    old = strike(shown) if len(shown) + len(typed) <= cw - 2 else ""
                    visible = len(typed) + (len(shown) if old else 0)
                    body = old + typed + " " * max(0, cw - 1 - visible)
                    color = self.theme.BAD if cell in self.fan_invalid else self.theme.OK
                if cell == selected:
                    color, bold = (self.theme.BAD if cell in self.fan_invalid else self.theme.WARN), True
                text = ("▶" if cell == selected else " ") + body
                self.put(row, ix + label_w + i * cw, text, color, bold, dim=readonly)
        self.fan_footer(state, y + height - 4, ix, iw)
