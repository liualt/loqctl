# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Firmware sysfs reads and verified writes."""
from __future__ import annotations

import errno
import os
from pathlib import Path
import sys
import threading
import time

from .capdata import effective_value, read_mode_table
from .common import FIELDS, ControlError, read, scaled
from .fancurve import curve_scale, find_legion_hwmon, read_curve, write_curve

REDISCOVER_SECONDS = 5
GAMEZONE = "lenovo-wmi-gamezone"
# The gamezone WMI device. lenovo-wmi-other needs lenovo_wmi_gamezone bound to it to know the
# current mode; LenovoLegionLinux's legion_wmi driver claims the same GUID when it loads first,
# and then every current_value read fails with EINVAL.
GAMEZONE_GUID = "887B54E3-DDDC-4B2C-8B88-68A26A8835D0"
GAMEZONE_WMI_DRIVER = "lenovo_wmi_gamezone"


class Hardware:
    def __init__(self, root=Path("/sys"), clock=time.monotonic):
        self.root = Path(root)
        self.clock = clock
        self.lock = threading.RLock()
        self.state = (None, None, None, None)  # (base, profile, choices, mode_driver), swapped whole
        self.on_new_base = None  # called when the attributes directory first appears or moves
        self._hook_pending = False
        self._hook_logged = False
        self._limits = (None, None)  # (table, reason); static per boot
        self._limits_tried = False
        self._static = {}  # key -> display name and limits; dropped when the mode changes
        self._static_mode = None
        self._discovered_at = clock()
        self._discover()

    base = property(lambda self: self.state[0])
    profile = property(lambda self: self.state[1])
    choices = property(lambda self: self.state[2])
    mode_driver = property(lambda self: self.state[3])

    def _discover(self):
        """Find the attributes directory and platform profile; the caller holds the lock."""
        bases = sorted(self.root.glob("class/firmware-attributes/lenovo-wmi-other*/attributes"))
        base = bases[0] if bases else None
        # Prefer gamezone's class device, then any other handler. The legacy
        # /sys/firmware/acpi/platform_profile lists "custom" but the kernel rejects writing it
        # (EINVAL), so it is only a last resort.
        profiles = [p for p in sorted(self.root.glob("class/platform-profile/*")) if (p / "profile").exists()]
        chosen = next((p for p in profiles if read(p / "name") == GAMEZONE), profiles[0] if profiles else None)
        if chosen is not None:
            found = (base, chosen / "profile", chosen / "choices", read(chosen / "name"))
        else:
            found = (base, self.root / "firmware/acpi/platform_profile",
                     self.root / "firmware/acpi/platform_profile_choices", None)
        if found == self.state:
            return
        self._static, self._static_mode = {}, None
        if found[0] != self.state[0]:
            self._limits, self._limits_tried = (None, None), False
            self._hook_pending = True
        self.state = found

    def _rediscover(self, force=False):
        """Look again for files that appeared or moved after start, at most every 5 s unless forced."""
        if not force and self.clock() - self._discovered_at < REDISCOVER_SECONDS:
            return
        with self.lock:
            self._discovered_at = self.clock()
            self._discover()
            hook = self.on_new_base if self._hook_pending else None
        if hook:  # outside the lock: it may do blocking I/O, and a failure is retried next time
            try:
                hook()
                self._hook_pending = False
            except Exception as exc:
                if not self._hook_logged:
                    self._hook_logged = True
                    print(f"loqctl: reloading mode limits failed: {exc}", file=sys.stderr)

    def mode_limits(self):
        """Per-mode firmware table as (table, None) or (None, reason); retried only as root."""
        with self.lock:
            if not self._limits_tried or (self._limits[0] is None and os.geteuid() == 0):
                self._limits, self._limits_tried = read_mode_table(self.root), True
            return self._limits

    def set_mode_limits(self, table, reason=None):
        """Inject a table fetched elsewhere (sudo helper or cache) for non-root reads."""
        with self.lock:
            self._limits, self._limits_tried = (table, None if table else reason), True

    def read_mode(self):
        self._rediscover()
        return read(self.state[1])

    def read_attribute(self, key):
        """One attribute's current_value as int, or None when missing or unreadable."""
        self._rediscover()
        base = self.state[0]
        path = base / key / "current_value" if base and key in FIELDS else None
        try:
            return int(read(path)) if path else None
        except (ValueError, TypeError):
            return None

    def _read_static(self, path, label):
        """Display name and limits, and whether all five files read cleanly."""
        description = read(path / "display_name")
        static = {"description": description or label}
        for name in ("min_value", "max_value", "default_value", "scalar_increment"):
            try:
                static[name] = int((path / name).read_text().strip())
            except (OSError, ValueError):
                static[name] = None
        return static, description is not None and None not in static.values()

    def _read_item(self, base, key, mode, table, fresh=False):
        """One attribute; display name and limits are cached once read in full, unless `fresh`."""
        label, unit = FIELDS[key]
        path = base / key if base else None
        if path is None or not path.is_dir():
            self._static.pop(key, None)
            return None
        static = None if fresh else self._static.get(key)
        if static is None:
            static, complete = self._read_static(path, label)
            if complete:
                self._static[key] = static
        item = {"label": label, "unit": unit, **static, "read_error": None, "read_errno": None}
        try:
            item["current_value"] = int((path / "current_value").read_text().strip())
        except OSError as exc:
            item["current_value"] = None
            item["read_error"], item["read_errno"] = exc.strerror, exc.errno
        except ValueError:
            item["current_value"] = None
        item["available"] = all(item[n] is not None for n in
                                ("current_value", "min_value", "max_value", "scalar_increment"))
        item["effective_value"] = effective_value(mode, key, item["current_value"], table)
        return item

    def gamezone_holder(self):
        """Name of the driver holding the gamezone WMI device when it is not lenovo_wmi_gamezone."""
        for device in sorted(self.root.glob(f"bus/wmi/devices/{GAMEZONE_GUID}*")):
            driver = device / "driver"
            name = driver.resolve().name if driver.exists() else "no driver"
            if name != GAMEZONE_WMI_DRIVER:
                return name
        return None

    def capabilities(self):
        self._rediscover()
        with self.lock:
            base, profile, choices, mode_driver = self.state
            attrs = {}
            mode = read(profile)
            if mode != self._static_mode:
                self._static, self._static_mode = {}, mode
            table, reason = self.mode_limits()
            for key in FIELDS:
                item = self._read_item(base, key, mode, table)
                if item:
                    attrs[key] = item
            return {"model": read(self.root / "class/dmi/id/product_name", "Lenovo LOQ"),
                    "mode": mode, "modes": (read(choices, "") or "").split(),
                    "mode_driver": mode_driver, "gamezone_holder": self.gamezone_holder(), "attributes": attrs, "mode_limits": table, "mode_limits_error": reason}

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
        self._rediscover(force=True)
        with self.lock:
            _, profile, choices_path, mode_driver = self.state
            previous = read(profile)
            choices = (read(choices_path, "") or "").split()
            if not choices:
                raise ControlError("No platform modes are available.")
            if not isinstance(mode, str) or mode not in choices:
                raise ControlError(f"Unsupported platform mode {str(mode)[:40]!r}. Choose one of {', '.join(choices)}.")
            if mode == "custom" and mode_driver is None:
                raise ControlError("Custom cannot be selected through the legacy platform_profile file, "
                                   f"and no {GAMEZONE} platform profile was found.")
            self._static = {}
            actual = self._write(profile, mode)
            return {"mode": mode, "previous": previous, "current": actual, "verified": True}

    def set_value(self, key, value):
        self._rediscover(force=True)
        with self.lock:
            base, profile = self.state[:2]
            item = self._read_item(base, key, None, None, fresh=True) if key in FIELDS else None
            if item is None:
                raise ControlError("Unknown or unavailable control.")
            if read(profile) != "custom":
                raise ControlError("Select custom mode before changing firmware limits.")
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
            actual = self._write(base / key / "current_value", value)
            return {"attribute": key, "value": value, "previous": item["current_value"],
                    "current": int(actual), "verified": True}


    def fancurve(self):
        """Fan curve and RPM from the legion_laptop hwmon, or {"available": False, "reason": ...}.

        Read from firmware on every call and never cached: a resume, another program or the sudo
        helper may change the curve behind our back. A full read takes about a second on EC models.
        """
        with self.lock:
            hwmon = find_legion_hwmon(self.root)
            if hwmon is None:
                return {"available": False, "reason": "legion_laptop hwmon not found"}
            mode = self.read_mode()
            points, scale = read_curve(hwmon), curve_scale(hwmon)
            if not points:
                return {"available": False, "reason": "legion_hwmon exposes no fan curve points"}
            return {"available": True, "mode": mode, "hwmon": str(hwmon), "unit": scale.unit, "max_rpm": scale.max_rpm,
                    "points": points, "rpm": [scaled(hwmon / f"fan{n}_input") for n in (1, 2)]}

    def write_fancurve(self, points):
        with self.lock:
            mode = self.read_mode()
            if mode != "custom":
                raise ControlError(f"Fan curves are locked in {mode} mode. Select custom first.")
            hwmon = find_legion_hwmon(self.root)
            if hwmon is None:
                raise ControlError("legion_laptop hwmon not found.")
            return write_curve(hwmon, points, curve_scale(hwmon))


def check_changes(changes):
    """Raise ControlError unless changes is a valid, non-duplicated [key, value] list."""
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


def run_changes(changes, read, write, errors, read_before=None):
    """Write [key, value] pairs in order and verify each; stop at the first failure.

    errors is the exception tuple that counts as a failed read or write. write(key, value)
    returns a read-back value it already trusts, or None to have read(key) check the firmware.
    read_before(key) supplies the "before" value (default read).
    """
    results, stopped = [], False
    for key, value in changes:
        result = {"key": key, "requested": value, "before": None,
                  "current": None, "status": "NOT SENT", "error": None}
        try:
            result["before"] = (read_before or read)(key)
        except errors:
            pass
        if stopped:
            try:
                result["current"] = read(key)
            except errors:
                pass
            results.append(result)
            continue
        error = None
        reported = None
        try:
            reported = write(key, value)
        except errors as exc:
            error = str(exc)
        write_failed = error is not None
        try:
            result["current"] = reported if reported is not None and not write_failed else read(key)
        except errors as exc:
            error = error or "Could not read firmware after the write: " + str(exc)
        if error or result["current"] != value:
            result["status"] = "UNVERIFIED" if result["current"] is None and not write_failed else "FAILED"
            result["error"] = error or f"Requested {value}, but firmware reports {result['current']!r}."
            stopped = True
        else:
            result["status"] = "APPLIED"
        results.append(result)
    return results


def apply_batch(hardware, changes):
    """Apply [key, value] pairs in order as root; stop at the first failure."""
    check_changes(changes)
    def write(key, value):
        hardware.set_mode(value) if key == "mode" else hardware.set_value(key, value)

    return run_changes(
        changes,
        lambda key: hardware.read_mode() if key == "mode" else hardware.read_attribute(key),
        write, (ControlError, OSError, ValueError))
