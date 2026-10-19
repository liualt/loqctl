# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Per-mode firmware limits from the kernel's debugfs dump of the capability table (capdata01).

The dump format comes from the kernel's lenovo-wmi-capdata driver; debugfs is not stable ABI.
Stock modes cannot be read from sysfs (current_value always holds the custom settings), so the
table is read as root once and cached per product and BIOS version.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil

from .common import FIELD_IDS, ControlError, SudoExpired, read, sudo_run

# platform_profile names -> firmware mode ids.
MODE_IDS = {"low-power": 1, "balanced": 2, "performance": 3, "max-power": 224, "custom": 255}
NEEDS_ROOT = "needs root"
NOT_FOUND = "debugfs capability table not found"
CACHE_VERSION = 1
_ID = re.compile(r"^\s*id:\s+0x[0-9a-fA-F]+\s+\[dev:\s*(\d+),\s*feat:\s*(\d+),\s*mode:\s*(\d+),\s*type:\s*(\d+)\]")
_FLAGS = re.compile(r"^\s*supported:\s+0x[0-9a-fA-F]+\s+\[(.{4})\]")
_VALUE = re.compile(r"^\s*(default_value|step|min_value|max_value):\s+(-?\d+)\s*$")
_NAMES = {"default_value": "default", "step": "step", "min_value": "min", "max_value": "max"}


def parse_capdata(text):
    """Parse dumps into {(dev, feat, mode, type): {default, step, min, max}} for supported (V) capdata01 rows."""
    entries, key, valid, values = {}, None, False, {}

    def finish():
        if key is not None and valid and len(values) == 4:
            entries[key] = dict(values)

    for line in text.splitlines():
        match = _ID.match(line)
        if match:
            finish()
            key, valid, values = tuple(int(g) for g in match.groups()), False, {}
            continue
        match = _FLAGS.match(line)
        if match:
            valid = match.group(1)[3] == "V"
            continue
        match = _VALUE.match(line)
        if match:
            values[_NAMES[match.group(1)]] = int(match.group(2))
    finish()
    return entries


def mode_table(entries):
    """{mode_name: {FIELDS key: default_value}} for every known mode."""
    table = {}
    for mode, mode_id in MODE_IDS.items():
        row = {}
        for field, (dev, feat, kind) in FIELD_IDS.items():
            entry = entries.get((dev, feat, mode_id, kind))
            if entry is not None:
                row[field] = entry["default"]
        table[mode] = row
    return table


def read_mode_table(root=Path("/sys")):
    """Read the debugfs dump(s); returns (table, None) or (None, reason)."""
    base = Path(root) / "kernel/debug/lenovo_wmi"
    try:
        guids = sorted(os.listdir(base))
    except PermissionError:
        return None, NEEDS_ROOT
    except OSError:
        return None, NOT_FOUND
    entries = {}
    for guid in guids:
        try:
            entries.update(parse_capdata((base / guid / "capdata").read_text()))
        except PermissionError:
            return None, NEEDS_ROOT
        except (OSError, UnicodeDecodeError):
            continue
    if not entries:
        return None, NOT_FOUND
    return mode_table(entries), None


def effective_value(mode, key, current, table):
    """Live value for a mode: sysfs current_value only in custom, else the table (never current_value)."""
    if mode == "custom":
        return current
    return ((table or {}).get(mode) or {}).get(key)


def valid_table(table):
    """True for a non-empty dict mode -> dict FIELDS key -> int."""
    return isinstance(table, dict) and bool(table) and all(
        isinstance(mode, str) and isinstance(row, dict)
        and all(k in FIELD_IDS and type(v) is int for k, v in row.items())
        for mode, row in table.items())


def limits_report(caps):
    """{mode: {key: value}} (custom = live current_value) plus "error" when the table is unavailable."""
    attrs, table = caps.get("attributes") or {}, caps.get("mode_limits")
    report = {mode: {key: effective_value(mode, key, item.get("current_value"), table)
                     for key, item in attrs.items()} for mode in caps.get("modes") or []}
    if table is None:
        report["error"] = caps.get("mode_limits_error") or NOT_FOUND
    return report


def cache_path():
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "loqctl" / "mode-limits.json"


def _dmi(root):
    return read(Path(root) / "class/dmi/id/product_name"), read(Path(root) / "class/dmi/id/bios_version")


def load_cache(root=Path("/sys")):
    """The cached table when version, product and BIOS match the live DMI values, else None."""
    product, bios = _dmi(root)
    try:
        data = json.loads(cache_path().read_text())
    except (OSError, ValueError):
        return None
    if (not isinstance(data, dict) or data.get("version") != CACHE_VERSION or product is None
            or data.get("product") != product or data.get("bios") != bios or not valid_table(data.get("table"))):
        return None
    return data["table"]


def save_cache(table, root=Path("/sys")):
    """Atomically write the cache as the current user (0600); failures are ignored."""
    product, bios = _dmi(root)
    if product is None or bios is None:
        return
    path = cache_path()
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump({"version": CACHE_VERSION, "product": product, "bios": bios, "table": table}, handle)
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def fetch_via_sudo():
    """Run `sudo -n loqctl.py _capdata`; returns (table, None) or (None, reason)."""
    if shutil.which("sudo") is None:
        return None, NEEDS_ROOT
    try:
        out, _ = sudo_run(["_capdata"])
    except SudoExpired:
        return None, NEEDS_ROOT + " (sudo credentials expired)"
    except ControlError as exc:
        return None, str(exc)
    try:
        table = json.loads(out)
    except ValueError:
        table = None
    if not valid_table(table):
        return None, "sudo returned an unreadable capability table"
    return table, None
