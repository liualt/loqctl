# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Saved fan curves, the recorded stock curve per mode and the root-side re-apply of the last applied curve."""
from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import pwd
import re
import stat
import time

from .common import ControlError
from .fancurve import WRITABLE, merge_curve, validate_curve

MAX_FILE = 65536
STOCK = "stock"


def config_dir():
    """~/.config/loqctl, with XDG_CONFIG_HOME respected."""
    base = os.environ.get("XDG_CONFIG_HOME", "")
    return (Path(base) if os.path.isabs(base) else Path.home() / ".config") / "loqctl"


def active_path(uid=None):
    """The user's ~/.config/loqctl/active.json, from the passwd entry of `uid` (default: this user).
    HOME and XDG_CONFIG_HOME are ignored: sudo resets them, so the server could not see the same path."""
    uid = os.getuid() if uid is None else uid
    try:
        home = pwd.getpwuid(uid).pw_dir
    except KeyError:
        home = ""
    if not os.path.isabs(home):
        raise OSError(errno.ENOENT, "No home directory for this user")
    return Path(home) / ".config/loqctl/active.json"


def writable_points(points):
    return [{f: p[f] for f in WRITABLE if p.get(f) is not None} for p in points]


def write_atomic(path, points):
    """{"version": 1, "points": ...} via tmp + os.replace, mode 0600."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump({"version": 1, "points": writable_points(points)}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def save_active(points):
    return write_atomic(active_path(), points)


def clear_active():
    active_path().unlink(missing_ok=True)


def stock_path(mode):
    return config_dir() / "stock" / (re.sub(r"[^a-z0-9-]", "_", mode) + ".json")


def read_stock(mode):
    """The curve recorded as stock for `mode`, or None when none was recorded or it is unreadable."""
    if not mode:
        return None
    try:
        return read_curve_file(stock_path(mode))
    except ControlError:
        return None


def marker_path(mode):
    return stock_path(mode).with_suffix(".written")


def mark_written(mode):
    """Note that loqctl has written a curve in `mode`, so the live curve is no longer known stock."""
    if mode:
        path = marker_path(mode)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.touch(mode=0o600)


def save_stock(mode, points):
    """Record `points` as the stock curve of `mode` unless one exists, loqctl already wrote in
    that mode, or active.json exists (also at the legacy XDG path). Returns True when it wrote."""
    if (not mode or stock_path(mode).exists() or marker_path(mode).exists()
            or active_path().exists() or (config_dir() / "active.json").exists()):
        return False
    write_atomic(stock_path(mode), points)
    return True


def save_named(points):
    return write_atomic(config_dir() / "curves" / (time.strftime("saved-%Y%m%d-%H%M%S") + ".json"), points)


def read_curve_file(path, uid=None):
    """Points from a curve file. With `uid`, the file must belong to that user and not be
    group/world-writable (the check runs on the opened descriptor). Errors name only the file,
    since the server logs them."""
    name = os.path.basename(path)
    try:
        # O_NONBLOCK so a FIFO cannot hang the open; cleared below once it is known to be a regular file.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ControlError(f"Cannot open {name}: {exc.strerror}") from exc
    with os.fdopen(fd) as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
            raise ControlError(f"{name} is not a regular curve file.")
        os.set_blocking(handle.fileno(), True)
        if uid is not None and (info.st_uid != uid or info.st_mode & 0o022):
            raise ControlError(f"{name} must be owned by uid {uid} and not group/world-writable.")
        try:
            text = handle.read(MAX_FILE + 1)
            if len(text) > MAX_FILE:
                raise ControlError(f"{name} is not a regular curve file.")
            data = json.loads(text)
        except ControlError:
            raise
        except ValueError as exc:  # UnicodeDecodeError is one
            raise ControlError(f"{name} is not valid JSON.") from exc
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("points"), list):
        raise ControlError(f"{name} is not a version 1 fan curve.")
    return data["points"]


def check_curve(current, points, scale=None):
    """None when `points` may be staged over `current`, else the reason."""
    try:
        errors = validate_curve(merge_curve(current, points), scale)
    except ControlError as exc:
        return str(exc)
    return errors[0][2] if errors else None


def saved_curves(current, scale=None):
    """[(name, points or None)] sorted by name; None marks a curve that does not validate."""
    found = []
    for path in sorted((config_dir() / "curves").glob("*.json")):
        try:
            points = read_curve_file(path)
        except ControlError:
            found.append((path.stem, None))
            continue
        found.append((path.stem, None if check_curve(current, points, scale) else points))
    return found


def curve_options(mode, current, scale=None):
    """[(name, points or None)]: the stock curve of `mode` when recorded, then the saved curves."""
    stock = read_stock(mode)
    first = [] if stock is None else [(STOCK, None if check_curve(current, stock, scale) else stock)]
    # a hand-placed curves/stock.json must not share the built-in entry's name
    saved = [(f"{name} (saved)" if name == STOCK else name, points) for name, points in saved_curves(current, scale)]
    return first + saved


class Reapplier:
    """Sampler hook for `serve`: re-write active.json when the firmware would have reset it."""
    RESUME_GAP = 10  # seconds that BOOTTIME gains on MONOTONIC across a suspend

    def __init__(self, hardware, uid, log=None):
        self.hardware, self.uid = hardware, uid
        self.log = log or (lambda message: print(message, flush=True))
        self.mode = self.gap = None
        self.missing = False

    def __call__(self, caps):
        mode = caps.get("mode")
        gap = time.clock_gettime(time.CLOCK_BOOTTIME) - time.clock_gettime(time.CLOCK_MONOTONIC)
        resumed = self.gap is not None and gap - self.gap >= self.RESUME_GAP
        entered = mode == "custom" and self.mode != "custom"
        self.mode, self.gap = mode, gap
        if mode == "custom" and (entered or resumed):
            self.apply("resume" if resumed and not entered else "custom mode")

    def apply(self, reason):
        try:
            path = active_path(self.uid)
            if not os.path.lexists(path):
                if not self.missing:
                    self.log(f"fancurve: nothing to re-apply after {reason}: no ~/.config/loqctl/active.json")
                self.missing = True
                return
            self.missing = False
            points = read_curve_file(path, self.uid)
            results = self.hardware.write_fancurve(points)
            failed = next((r for r in results if r["status"] != "APPLIED"), None)
            if failed:
                self.log(f"fancurve: re-apply after {reason} stopped: {failed.get('error') or failed['status']}")
            else:
                self.log(f"fancurve: re-applied {path.name} after {reason} ({len(results)} changes)")
        except ControlError as exc:
            self.log(f"fancurve: skipped re-apply after {reason}: {exc}")
        except Exception as exc:  # str(exc) can carry the user's paths; strerror and the type name do not
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else type(exc).__name__
            self.log(f"fancurve: skipped re-apply after {reason}: {detail}")
