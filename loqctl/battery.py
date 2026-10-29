# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Battery charging mode: normal, conservation or rapid charge, with verified writes."""
from __future__ import annotations

from pathlib import Path

from .common import ControlError, read

MODES = ("normal", "conservation", "rapid")
# Explicit write allowlist: these attributes, 0 or 1 only. Prefer the in-tree ideapad
# conservation attribute; fall back to legion_laptop's.
CONSERVATION_GLOBS = ("bus/platform/drivers/ideapad_acpi/*/conservation_mode",
                      "devices/platform/legion/battery_conservation")
RAPID_PATH = "devices/platform/legion/rapidcharge"


def find_paths(root):
    """{"conservation": Path|None, "rapid": Path|None}; only attributes that read as 0 or 1."""
    root = Path(root)
    conservation = next((p for g in CONSERVATION_GLOBS for p in sorted(root.glob(g))
                         if read(p) in ("0", "1")), None)
    rapid = root / RAPID_PATH
    return {"conservation": conservation, "rapid": rapid if read(rapid) in ("0", "1") else None}


def read_battery(root):
    """Current mode and the choices this laptop offers; mode is None (N/A) without any attribute."""
    paths = find_paths(root)
    if all(p is None for p in paths.values()):
        return {"available": False, "mode": None, "choices": []}
    on = {k: p is not None and read(p) == "1" for k, p in paths.items()}
    mode = "conservation" if on["conservation"] else "rapid" if on["rapid"] else "normal"
    choices = ["normal"] + [m for m, k in (("conservation", "conservation"), ("rapid", "rapid")) if paths[k]]
    return {"available": True, "mode": mode, "choices": choices}


def _write(path, value):
    try:
        path.write_text(f"{value}\n")
    except PermissionError as exc:
        raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
    except OSError as exc:
        raise ControlError(f"Firmware rejected the write: {exc.strerror}") from exc
    actual = read(path)
    if actual != str(value):
        raise ControlError(f"Write was sent, but readback is {actual!r}, expected {value}. Refresh before retrying.")


def write_battery(root, mode):
    """Set the charging mode; the other of conservation and rapid charge is turned off first."""
    if not isinstance(mode, str) or mode not in MODES:
        raise ControlError(f"Unsupported battery mode {str(mode)[:40]!r}. Choose one of {', '.join(MODES)}.")
    state = read_battery(root)
    if not state["available"]:
        raise ControlError("No battery charging mode attribute found.")
    if mode not in state["choices"]:
        raise ControlError(f"This laptop does not expose {mode} charging.")
    paths = find_paths(root)
    wanted = {"conservation": int(mode == "conservation"), "rapid": int(mode == "rapid")}
    # Zeros first, so the two are never both set.
    for key, value in sorted(wanted.items(), key=lambda item: item[1]):
        if paths[key] is not None and (value or read(paths[key]) != "0"):
            _write(paths[key], value)
    current = read_battery(root)["mode"]
    if current != mode:
        raise ControlError(f"Battery mode reads {current!r} after the write, expected {mode!r}.")
    return {"mode": mode, "previous": state["mode"], "current": current, "verified": True}
