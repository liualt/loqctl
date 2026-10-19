# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Firmware sysfs reads and verified writes."""
from __future__ import annotations

import errno
from pathlib import Path
import threading

from .common import FIELDS, ControlError, read

class Hardware:
    def __init__(self, root=Path("/sys")):
        self.root = Path(root)
        self.lock = threading.RLock()
        bases = sorted(self.root.glob("class/firmware-attributes/lenovo-wmi-other*/attributes"))
        self.base = bases[0] if bases else None
        profiles = sorted(self.root.glob("class/platform-profile/*"))
        self.profile = next((p / "profile" for p in profiles
                             if read(p / "name") == "lenovo-wmi-gamezone"),
                            self.root / "firmware/acpi/platform_profile")
        self.choices = self.profile.parent / ("choices" if self.profile.name == "profile"
                                               else "platform_profile_choices")

    def capabilities(self):
        with self.lock:
            attrs = {}
            for key, (label, unit) in FIELDS.items():
                path = self.base / key if self.base else None
                if path is None or not path.is_dir():
                    continue
                item = {"label": label, "unit": unit, "description": read(path / "display_name", label)}
                for name in ("current_value", "min_value", "max_value", "default_value", "scalar_increment"):
                    value = read(path / name)
                    try:
                        item[name] = int(value)
                    except (ValueError, TypeError):
                        item[name] = None
                item["available"] = all(item[n] is not None for n in
                                        ("current_value", "min_value", "max_value", "scalar_increment"))
                attrs[key] = item
            return {"model": read(self.root / "class/dmi/id/product_name", "Lenovo LOQ"),
                    "mode": read(self.profile), "modes": (read(self.choices, "") or "").split(),
                    "attributes": attrs}

    def _write(self, path, value):
        try:
            path.write_text(str(value) + "\n")
        except PermissionError as exc:
            raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise ControlError("Limits are only editable in custom mode. Select custom first.") from exc
            raise ControlError(f"Firmware rejected the write: {exc.strerror}") from exc
        actual = read(path)
        if actual != str(value):
            raise ControlError(f"Write was sent, but readback is {actual!r}, expected {value}. Refresh before retrying.")
        return actual

    def set_mode(self, mode):
        with self.lock:
            caps = self.capabilities()
            if not isinstance(mode, str) or mode not in caps["modes"]:
                raise ControlError("Unsupported platform mode.")
            actual = self._write(self.profile, mode)
            return {"mode": mode, "previous": caps["mode"], "current": actual, "verified": True}

    def set_value(self, key, value):
        with self.lock:
            caps = self.capabilities()
            if key not in FIELDS or key not in caps["attributes"]:
                raise ControlError("Unknown or unavailable control.")
            if caps["mode"] != "custom":
                raise ControlError("Select custom mode before changing firmware limits.")
            item = caps["attributes"][key]
            if not item["available"]:
                raise ControlError("Firmware did not report usable limits for this control.")
            if type(value) is not int:
                raise ControlError("Value must be an integer.")
            lo, hi = item["min_value"], item["max_value"]
            if not lo <= value <= hi:
                raise ControlError(f"Value must be between {lo} and {hi} {item['unit']}.")
            # Zero means the firmware supplies no step constraint, not a read-only attribute.
            step = item["scalar_increment"]
            if step > 0 and (value - lo) % step:
                raise ControlError(f"Value must advance in steps of {step} from {lo}.")
            actual = self._write(self.base / key / "current_value", value)
            return {"attribute": key, "value": value, "previous": item["current_value"],
                    "current": int(actual), "verified": True}


def apply_batch(hardware, changes):
    """Apply [key, value] pairs in order as root; stop at the first failure."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= len(FIELDS) + 1:
        raise ControlError("Changes must be a list of 1 to %d [key, value] pairs." % (len(FIELDS) + 1))
    keys = []
    for item in changes:
        if not isinstance(item, list) or len(item) != 2:
            raise ControlError("Each change must be a [key, value] pair.")
        key, value = item
        if key == "mode":
            if not isinstance(value, str):
                raise ControlError("Mode must be a string.")
        elif isinstance(key, str) and key in FIELDS:
            if type(value) is not int:
                raise ControlError("Limit values must be integers.")
        else:
            raise ControlError("Unknown control.")
        if key in keys:
            raise ControlError("Duplicate control in changes.")
        keys.append(key)
    if "mode" in keys and len(keys) > 1:
        raise ControlError("Mode and limits are never applied together.")

    def current(key):
        caps = hardware.capabilities()
        if key == "mode":
            return caps.get("mode")
        return caps.get("attributes", {}).get(key, {}).get("current_value")

    results, stopped = [], False
    for key, value in changes:
        result = {"key": key, "requested": value, "before": None,
                  "current": None, "status": "NOT SENT", "error": None}
        try:
            result["before"] = current(key)
        except (ControlError, OSError, ValueError):
            pass
        if stopped:
            result["current"] = result["before"]
            results.append(result)
            continue
        error = None
        try:
            hardware.set_mode(value) if key == "mode" else hardware.set_value(key, value)
        except (ControlError, OSError, ValueError) as exc:
            error = str(exc)
        write_failed = error is not None
        try:
            result["current"] = current(key)
        except (ControlError, OSError, ValueError) as exc:
            error = error or "Could not read firmware after the write: " + str(exc)
        if error or result["current"] != value:
            result["status"] = "UNVERIFIED" if result["current"] is None and not write_failed else "FAILED"
            result["error"] = error or f"Requested {value}, but firmware reports {result['current']!r}."
            stopped = True
        else:
            result["status"] = "APPLIED"
        results.append(result)
    return results
