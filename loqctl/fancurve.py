# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve reads, validation and verified writes through the legion_laptop hwmon files."""
from __future__ import annotations

import errno
import os
from pathlib import Path

from .common import ControlError, read

HWMON_NAME = "legion_hwmon"
MAX_POINTS = 10
# Explicit write allowlist (plus the read-only accel/decel). Never add fanN_target,
# auto_points_size, minifancurve, lockfancontroller or fan_fullspeed. `_temp_hyst` is the
# absolute LOWER temperature of a step.
FILE_MAP = {
    "cpu_up": "pwm1_auto_point{n}_temp", "cpu_down": "pwm1_auto_point{n}_temp_hyst",
    "gpu_up": "pwm2_auto_point{n}_temp", "gpu_down": "pwm2_auto_point{n}_temp_hyst",
    "ic_up": "pwm3_auto_point{n}_temp", "ic_down": "pwm3_auto_point{n}_temp_hyst",
    "fan1": "pwm1_auto_point{n}_pwm", "fan2": "pwm2_auto_point{n}_pwm",
    "accel": "pwm1_auto_point{n}_accel", "decel": "pwm1_auto_point{n}_decel",
}
TEMPS = ("cpu_up", "cpu_down", "gpu_up", "gpu_down", "ic_up", "ic_down")
SPEEDS = ("fan1", "fan2")
WRITABLE = TEMPS + SPEEDS
POINT_FIELDS = tuple(FILE_MAP)
LABELS = {"cpu_up": "CPU up", "cpu_down": "CPU down", "gpu_up": "GPU up", "gpu_down": "GPU down",
          "ic_up": "IC up", "ic_down": "IC down", "fan1": "Fan 1 %", "fan2": "Fan 2 %",
          "accel": "Accel", "decel": "Decel"}
HOT_C, HOT_MIN_PWM, LAST_MIN_PWM = 80, 102, 153  # 40 % and 60 %


def find_legion_hwmon(root):
    for path in sorted(Path(root).glob("class/hwmon/hwmon*")):
        if read(path / "name") == HWMON_NAME:
            return path
    return None


def _int(text):
    try:
        return int(text)
    except (ValueError, TypeError):
        return None


def read_curve(hwmon):
    """Points 1..N as dicts keyed by POINT_FIELDS; None for a missing or unreadable file."""
    hwmon, points = Path(hwmon), []
    for n in range(1, MAX_POINTS + 1):
        paths = {f: hwmon / pattern.format(n=n) for f, pattern in FILE_MAP.items()}
        if not any(p.exists() for p in paths.values()):
            break
        points.append({f: _int(read(p)) for f, p in paths.items()})
    return points


def speed_unit(hwmon):
    return read(Path(hwmon) / "device/fancurve_speed_unit")


def pwm_to_pct(pwm, unit=None):
    pct = round(pwm * 100 / 255)
    return round(pct / 10) * 10 if unit == "level" else pct


def pct_to_pwm(pct, unit=None):
    return round((round(pct / 10) * 10 if unit == "level" else pct) * 255 / 100)


def min_speed(points, i):
    """Lowest allowed pwm at point i: hot previous step, and the last step."""
    need = 0
    if i and any(type(points[i - 1].get(k)) is int and points[i - 1][k] >= HOT_C
                 for k in ("cpu_up", "gpu_up")):
        need = HOT_MIN_PWM
    return LAST_MIN_PWM if i == len(points) - 1 else need


def validate_curve(points):
    """List of (point index, field, message); empty when the curve may be written."""
    errors = []
    if not isinstance(points, list):
        return [(0, "point", "Curve must be a list of points.")]
    pts = [p if isinstance(p, dict) else {} for p in points]

    def num(i, field):
        value = pts[i].get(field)
        return value if type(value) is int else None

    for i, point in enumerate(points):
        name = f"Point {i + 1}"
        if not isinstance(point, dict):
            errors.append((i, "point", f"{name} must be an object."))
        for field in WRITABLE:
            value = pts[i].get(field)
            hi = 255 if field in SPEEDS else 127
            if value is None:
                continue
            if type(value) is not int:
                errors.append((i, field, f"{name} {LABELS[field]} must be an integer."))
            elif not 0 <= value <= hi:
                errors.append((i, field, f"{name} {LABELS[field]} must be 0-{hi}."))
    for field in ("cpu_up", "gpu_up", "ic_up") + SPEEDS:
        prev = None
        for i in range(len(pts)):
            value = num(i, field)
            if value is not None:
                if prev is not None and value < prev:
                    errors.append((i, field, f"Point {i + 1} {LABELS[field]} must not drop below point {i}."))
                prev = value
    for i in range(len(pts)):
        for sensor in ("cpu", "gpu", "ic"):
            low, up = num(i, sensor + "_down"), num(i, sensor + "_up")
            before = num(i - 1, sensor + "_up") if i else None
            if low is not None and up is not None and low >= up:
                errors.append((i, sensor + "_down", f"Point {i + 1} {LABELS[sensor + '_down']} must be below its up temperature."))
            elif low is not None and before is not None and low >= before:
                errors.append((i, sensor + "_down", f"Point {i + 1} {LABELS[sensor + '_down']} must be below point {i}'s up temperature."))
        need = min_speed(pts, i)
        for field in SPEEDS:
            value = num(i, field)
            if value is not None and value < need:
                errors.append((i, field, f"Point {i + 1} {LABELS[field]} needs at least {round(need * 100 / 255)}% here."))
    return errors


def merge_curve(current, target):
    """Current points with the writable fields of `target` laid over files that exist."""
    if not isinstance(target, list) or len(target) != len(current):
        raise ControlError(f"Curve must have {len(current)} points.")
    merged = []
    for have, want in zip(current, target):
        if not isinstance(want, dict) or any(k not in POINT_FIELDS for k in want):
            raise ControlError("Each point must be an object of known fan curve fields.")
        merged.append({f: (want[f] if f in WRITABLE and f in want and v is not None else v)
                       for f, v in have.items()})
    return merged


def _same(field, a, b, unit):
    if unit == "level" and field in SPEEDS and a is not None and b is not None \
            and type(a) is int and type(b) is int:
        return round(a * 10 / 255) == round(b * 10 / 255)
    return a == b


def _path(hwmon, field, n):
    """File for a writable field; refuses anything that is not that exact file inside hwmon."""
    if field not in WRITABLE:
        raise ControlError("Not a writable fan curve field.")
    name = FILE_MAP[field].format(n=n)
    path = hwmon / name
    if path.is_symlink() or path.resolve().parent != hwmon or path.name != name:
        raise ControlError("Refusing to write outside the hwmon directory.")
    return path


def _write_file(path, value):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_NOFOLLOW)  # never creates a file
        try:
            os.write(fd, f"{value}\n".encode())
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno == errno.EBUSY:
            raise ControlError("Fan curve is busy. The firmware only accepts it in custom mode.") from exc
        if exc.errno == errno.EPERM:
            raise ControlError("module refuses unvalidated fan curve writes for this model") from exc
        if exc.errno == errno.EOPNOTSUPP:
            raise ControlError("This model does not support fan curve writes through legion_laptop.") from exc
        if exc.errno == errno.EACCES:
            raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
        raise ControlError(f"Fan curve write failed: {exc.strerror}") from exc


def write_curve(hwmon, target, unit=None):
    """Write changed fields (temps first, then speeds), verify by re-reading everything.

    Returns one result per attempted field; raises ControlError before any write when the
    target is malformed or invalid.
    """
    hwmon = Path(hwmon).resolve()
    current = read_curve(hwmon)
    if not current:
        raise ControlError("No fan curve files found.")
    merged = merge_curve(current, target)
    errors = validate_curve(merged)
    if errors:
        raise ControlError(errors[0][2] + (f" ({len(errors) - 1} more problems)" if len(errors) > 1 else ""))
    changes = [(i, f) for fields in (TEMPS, SPEEDS) for f in fields for i in range(len(current))
               if current[i][f] is not None and not _same(f, current[i][f], merged[i][f], unit)]
    results = [{"key": f"point{i + 1}.{f}", "point": i, "field": f, "requested": merged[i][f],
                "before": current[i][f], "current": None, "status": "NOT SENT", "error": None}
               for i, f in changes]
    sent = len(results)  # results[sent:] stay NOT SENT
    for n, result in enumerate(results):
        try:
            _write_file(_path(hwmon, result["field"], result["point"] + 1), result["requested"])
        except ControlError as exc:
            result["status"], result["error"], sent = "FAILED", str(exc), n + 1
            break
    after = read_curve(hwmon)
    for n, result in enumerate(results):
        i, f = result["point"], result["field"]
        result["current"] = after[i][f] if i < len(after) else None
        if n >= sent or result["status"] == "FAILED":
            continue
        if _same(f, result["current"], result["requested"], unit):
            result["status"] = "APPLIED"
        else:
            result["status"] = "FAILED"
            result["error"] = f"Requested {result['requested']}, but firmware reports {result['current']!r}."
    return results
