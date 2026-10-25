# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Keyboard RGB through LegionAura."""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import threading
import time

from .colors import hex_to_rgb, rgb_to_hex
from .common import LEGIONAURA, LEGIONAURA_CONFIG, ControlError, clamp

class Keyboard:
    """Lenovo 4-zone RGB via LegionAura. Config lives in ~/.config/legionaura/."""

    EFFECTS = ("static", "breath", "wave", "hue", "off")
    ZONES = 4
    FIELDS = ("effect", "speed", "brightness", "wave_dir", "zones")

    def __init__(self, binary=LEGIONAURA, config=LEGIONAURA_CONFIG):
        self.binary = binary
        self.config = Path(config)
        self.effect = "static"
        self.speed = 1
        self.brightness = 2
        self.wave_dir = "ltr"
        self.zones = ["ffffff"] * self.ZONES
        self.from_config = set()  # fields the last load read from the config
        self.known = set()  # fields set by an edit; fields in neither set are placeholders
        self.problem = ""  # why the config could not be used
        self.dirty = False  # in-memory edits not yet confirmed applied; load() leaves them alone
        self.edits = 0  # bumped per in-memory edit; a job only clears dirty if no edit came after it
        self.last_message = ""
        self.last_seq = 0
        self._cond = threading.Condition()
        self._job = None
        self._running = False
        self._stop = False
        self._thread = None
        self._found = (False, None)  # (available, monotonic time of the last PATH lookup)
        self.load()

    @property
    def available(self):
        found, when = self._found
        if when is None or time.monotonic() - when >= 5:
            found = bool(shutil.which(self.binary))
            self._found = (found, time.monotonic())
        return found

    def recheck(self):
        self._found = (False, None)

    def load(self, force=False):
        if self.dirty and not force:
            return
        try:
            data = json.loads(self.config.read_text())
        except FileNotFoundError:
            return self._load_failed("no config yet")
        except OSError:
            return self._load_failed("config not readable")
        except (json.JSONDecodeError, TypeError):
            return self._load_failed("config is not valid JSON")
        if not isinstance(data, dict):
            return self._load_failed("config is not a JSON object")
        found = {}
        if data.get("effect") in self.EFFECTS:
            found["effect"] = data["effect"]
        for name, key, low, high in (("speed", "speed", 1, 4), ("brightness", "brightness", 1, 2)):
            value = data.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and low <= value <= high:
                found[name] = value
        if data.get("waveDir") in ("ltr", "rtl"):
            found["wave_dir"] = data["waveDir"]
        zones = data.get("zones")
        if isinstance(zones, list) and len(zones) >= self.ZONES:
            try:
                found["zones"] = [rgb_to_hex(*hex_to_rgb(zone)) for zone in zones[:self.ZONES]]
            except (ControlError, TypeError, AttributeError):
                pass
        missing = [name for name in self.FIELDS if name not in found]
        with self._cond:
            for name, value in found.items():
                setattr(self, name, value)
            self.from_config = set(found)
            self.problem = "config is missing: " + ", ".join(missing) if missing else ""

    def _load_failed(self, problem):
        """Forget what the config said; fields set by edits stay known."""
        with self._cond:
            self.from_config = set()
            self.problem = problem

    def status(self):
        def value(name, current):
            return current if name in self.known | self.from_config else None
        return {
            "available": self.available,
            "binary": shutil.which(self.binary) or self.binary,
            "config": str(self.config),
            "effect": value("effect", self.effect),
            "speed": value("speed", self.speed),
            "brightness": value("brightness", self.brightness),
            "wave_dir": value("wave_dir", self.wave_dir),
            "zones": value("zones", list(self.zones)),
            "problem": self.problem,
        }

    def _run(self, args):
        resolved = shutil.which(self.binary)
        if not resolved:
            raise ControlError("legionaura not found. Install the LegionAura package.")
        try:
            result = subprocess.run([resolved, *args], capture_output=True, text=True, timeout=8, check=False)
        except subprocess.TimeoutExpired as exc:
            raise ControlError("legionaura did not answer within 8 s.") from exc
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise ControlError("legionaura: " + detail)
        return (result.stdout or "").strip() or "OK"

    def command(self):
        effect = self.effect
        if effect == "off":
            return ["off"]
        brightness = ["--brightness", str(clamp(self.brightness, 1, 2))]
        speed = ["--speed", str(clamp(self.speed, 1, 4))]
        if effect == "static":
            return ["static", *self.zones, *brightness]
        if effect == "breath":
            return ["breath", *self.zones, *speed, *brightness]
        if effect == "wave":
            return ["wave", self.wave_dir, *speed, *brightness]
        if effect == "hue":
            return ["hue", *speed, *brightness]
        raise ControlError(f"Unknown keyboard effect {effect!r}.")

    def apply_current(self):
        return self._run(self.command())

    def apply_saved(self):
        """Re-apply the settings LegionAura saved, without sending any from here."""
        return self._run(["apply"])

    def mark_dirty(self, *fields):
        """Note an in-memory edit; no fields means every field now holds a real value."""
        with self._cond:
            self.edits += 1
            self.dirty = True
            self.known |= set(fields) or set(self.FIELDS)

    @property
    def busy(self):
        with self._cond:
            return self._job is not None or self._running

    def submit(self, args, label):
        """Queue one legionaura run; a newer submit replaces one that has not started."""
        with self._cond:
            self._job = (list(args), label, self.edits)
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _worker(self):
        while True:
            with self._cond:
                while self._job is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                args, label, edits = self._job
                self._job = None
                self._running = True
            ok = False
            try:
                message = f"Keyboard applied ({label}). {self._run(args)}"
                ok = True
            except ControlError as exc:
                message = str(exc)
            except Exception as exc:
                message = f"legionaura: {exc}"
            with self._cond:
                self._running = False
                if ok and self._job is None and edits == self.edits:
                    self.dirty = False
                self.last_message = message
                self.last_seq += 1

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(0.1)

    def set_brightness(self, value):
        try:
            self.brightness = clamp(int(value), 1, 2)
        except ValueError as exc:
            raise ControlError(f"brightness must be 1 or 2, not {value!r}.") from exc
        return self._run(["--brightness", str(self.brightness)])


def kb_cli(argv):
    """LegionAura keyboard subcommands."""
    kb = Keyboard()
    if not argv:
        return kb.status()
    action = argv[0]
    rest = argv[1:]
    if action == "status":
        return kb.status()
    if action == "apply":
        return {"result": kb.apply_saved(), **kb.status()}
    if action == "off":
        kb.effect = "off"
        kb.mark_dirty("effect")
        return {"result": kb.apply_current(), **kb.status()}
    if action == "brightness" and rest:
        result = kb.set_brightness(rest[0])
        kb.mark_dirty("brightness")
        return {"result": result, **kb.status()}
    if action in ("static", "breath"):
        if not rest:
            raise ControlError(f"{action} needs at least one RRGGBB color.")
        colors, brightness, speed = [], kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            colors.append(rgb_to_hex(*hex_to_rgb(token)))
            i += 1
        if not colors:
            raise ControlError(f"{action} needs at least one RRGGBB color.")
        while len(colors) < Keyboard.ZONES:
            colors.append(colors[-1])
        kb.zones = colors[:Keyboard.ZONES]
        kb.effect = action
        kb.mark_dirty("effect", "zones", "brightness", *(("speed",) if action == "breath" else ()))
        kb.brightness = clamp(brightness, 1, 2)
        kb.speed = clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    if action == "wave":
        direction = "ltr"
        brightness, speed = kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in ("ltr", "rtl"):
                direction = token; i += 1; continue
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            raise ControlError(f"Unexpected wave argument: {token}")
        kb.effect, kb.wave_dir = "wave", direction
        kb.mark_dirty("effect", "wave_dir", "speed", "brightness")
        kb.brightness, kb.speed = clamp(brightness, 1, 2), clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    if action == "hue":
        brightness, speed = kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            raise ControlError(f"Unexpected hue argument: {token}")
        kb.effect = "hue"
        kb.mark_dirty("effect", "speed", "brightness")
        kb.brightness, kb.speed = clamp(brightness, 1, 2), clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    raise ControlError("kb usage: status|apply|off|brightness|static|breath|wave|hue …")
