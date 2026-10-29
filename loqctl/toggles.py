# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Device switches (Fn lock, Win key lock, camera, lid and port lights) with verified writes.

Explicit allowlist: name -> (label, file under the sysfs root, maximum value). Do not use
legion's fn_lock (it runs an ACPI method) and leave out the touchpad switch: ideapad's ABI and
legion's source disagree on what 0 and 1 mean. LED maxima are read from max_brightness.
"""
from __future__ import annotations

import errno
from pathlib import Path

from .common import ControlError, read

TOGGLES = {
    "fn_lock": ("Fn lock", "bus/platform/devices/VPC2004:00/fn_lock", 1),
    "camera": ("Camera power", "bus/platform/devices/VPC2004:00/camera_power", 1),
    "winkey": ("Windows key lock", "devices/platform/legion/winkey", 1),
    "logo_light": ("Lid logo light", "class/leds/platform::ylogo/brightness", None),
    "port_light": ("Rear port light", "class/leds/platform::ioport/brightness", None),
}


def _find(root, name):
    """(path, maximum) for a supported switch, else None."""
    if name not in TOGGLES:
        return None
    _, relative, top = TOGGLES[name]
    path = Path(root) / relative
    if top is None:
        text = read(path.with_name("max_brightness"))
        top = int(text) if text and text.isdigit() else None
    current = read(path)
    if top is None or not path.is_file() or current is None or not current.isdigit():
        return None
    return path, top


def list_toggles(root):
    """Supported switches only: [{name, label, value, max}]."""
    result = []
    for name, (label, _, _) in TOGGLES.items():
        found = _find(root, name)
        if found:
            result.append({"name": name, "label": label, "value": int(read(found[0])), "max": found[1]})
    return result


def set_toggle(root, name, value):
    found = _find(root, name)
    if found is None:
        raise ControlError("Unknown or unavailable switch.")
    path, top = found
    if type(value) is not int or not 0 <= value <= top:
        raise ControlError(f"Value must be an integer from 0 to {top}.")
    previous = int(read(path))
    try:
        path.write_text(f"{value}\n")
    except PermissionError as exc:
        raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
    except OSError as exc:
        raise ControlError(f"The device rejected the write: {errno.errorcode.get(exc.errno, exc.strerror)}") from exc
    actual = read(path)
    if actual != str(value):
        raise ControlError(f"Write was sent, but readback is {actual!r}, expected {value}. Refresh before retrying.")
    return {"switch": name, "value": value, "previous": previous, "current": int(actual), "verified": True}
