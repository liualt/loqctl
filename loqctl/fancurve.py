# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Fan curve reads, validation and verified writes through the legion_laptop hwmon files.

Speed conversions follow LenovoLegionLinux kernel_module/legion-laptop.c
(fancurve_set_speed_pwm / fancurve_get_speed_pwm); no code is copied from it.
The hwmon `pwmN_auto_pointM_pwm` files take 0-255, which the module turns into the byte
the firmware stores:
- "rpm":     hundreds of RPM, ceil(pwm * max_rpm / 25500); max_rpm is fanN_max.
- "level":   firmware levels 0-10, round(pwm * 10 / 255).
- "percent": percent (or raw pwm on some models), about pwm * 100 / 255.
"""
from __future__ import annotations

import errno
import math
import os
from pathlib import Path

from .common import ControlError, read

HWMON_NAME = "legion_hwmon"
MAX_POINTS = 10
DEFAULT_MAX_RPM = 10000  # the module's MAX_RPM when a model sets no fan_max_rpm
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
          "ic_up": "IC up", "ic_down": "IC down", "fan1": "Fan 1", "fan2": "Fan 2",
          "accel": "Accel", "decel": "Decel"}
# Safety floors, a loqctl policy and not a firmware limit: the last point, and any point after
# a step that starts at HOT_C or above, must not run the fans slower than this.
HOT_C = 80
FLOORS = {"rpm": (2000, 3000), None: (40, 60)}  # (after a hot step, last point); RPM or percent


class Scale:
    """How one curve's 0-255 pwm values map to what the firmware stores and what we show."""

    def __init__(self, unit=None, max_rpm=None):
        self.unit = unit
        self.max_rpm = max_rpm if max_rpm and 0 < max_rpm <= 25500 else DEFAULT_MAX_RPM

    @property
    def label(self):
        return {"rpm": "RPM", "level": "level"}.get(self.unit, "%")

    @property
    def top(self):
        """Highest displayable speed."""
        return {"rpm": self.max_rpm // 100 * 100, "level": 10}.get(self.unit, 100)

    def step(self, big=False):
        return {"rpm": (100, 500), "level": (1, 2)}.get(self.unit, (5, 20))[big]

    def stored(self, pwm):
        """The byte the firmware keeps for a pwm value (also recovers it from a readback)."""
        if self.unit == "rpm":
            return math.ceil(pwm * self.max_rpm / 25500)
        if self.unit == "level":
            return round(pwm * 10 / 255)
        return round(pwm * 100 / 255)

    def show(self, pwm):
        """Display value: RPM, level or percent."""
        return None if pwm is None else self.stored(pwm) * (100 if self.unit == "rpm" else 1)

    def text(self, pwm):
        value = self.show(pwm)
        return "N/A" if value is None else f"{value}{'' if self.label == '%' else ' '}{self.label}"

    def pwm(self, value):
        """Canonical pwm for a display value; on rpm and level it reads back unchanged."""
        if self.unit == "rpm":
            return min(255, int(value) // 100 * 25500 // self.max_rpm)
        if self.unit == "level":
            return min(255, int(value) * 255 // 10)
        return min(255, math.ceil(int(value) * 255 / 100))

    def same(self, a, b):
        if type(a) is not int or type(b) is not int:
            return a == b
        tolerance = 0 if self.unit in ("rpm", "level") else 1  # percent rounds both ways
        return abs(self.stored(a) - self.stored(b)) <= tolerance

    def floor(self, after_hot, last):
        hot, final = FLOORS["rpm" if self.unit == "rpm" else None]
        need = final if last else hot if after_hot else 0
        if not need:
            return 0
        return self.pwm(need) if self.unit == "rpm" else math.ceil(need * 255 / 100)


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
    """Points 1..N as dicts keyed by POINT_FIELDS; None for a missing or unreadable file.

    Slow on EC models: every file read makes the module re-read the whole curve (~11 ms on a
    LOQ 15IRX9), so callers should cache the result.
    """
    hwmon, points = Path(hwmon), []
    for n in range(1, MAX_POINTS + 1):
        present = {f: hwmon / pattern.format(n=n) for f, pattern in FILE_MAP.items()}
        present = {f: p for f, p in present.items() if p.exists()}
        if not present:
            break
        points.append({f: _int(read(present[f])) if f in present else None for f in FILE_MAP})
    return points


def speed_unit(hwmon):
    return read(Path(hwmon) / "device/fancurve_speed_unit")


def curve_scale(hwmon):
    return Scale(speed_unit(hwmon), _int(read(Path(hwmon) / "fan1_max")))


def min_speed(points, i, scale=None):
    """Lowest allowed pwm at point i (see FLOORS)."""
    scale = scale or Scale()
    hot = bool(i) and any(type(points[i - 1].get(k)) is int and points[i - 1][k] >= HOT_C
                          for k in ("cpu_up", "gpu_up"))
    return scale.floor(hot, i == len(points) - 1)


def validate_curve(points, scale=None):
    """List of (point index, field, message); empty when the curve may be written.

    Rules: values in range; each sensor's up temperature and each fan speed never drop from
    one point to the next; a point's down temperature is not above its up temperature; the
    safety floors. The stock LOQ curve has down temperatures above the previous point's up
    temperature, so that is allowed.
    """
    scale = scale or Scale()
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
            if low is not None and up is not None and low > up:
                errors.append((i, sensor + "_down",
                               f"Point {i + 1} {LABELS[sensor + '_down']} must not be above its up temperature."))
        need = min_speed(pts, i, scale)
        for field in SPEEDS:
            value = num(i, field)
            if value is not None and need and scale.stored(value) < scale.stored(need):
                errors.append((i, field, f"Point {i + 1} {LABELS[field]} needs at least {scale.text(need)} here."))
    return errors


def merge_curve(current, target):
    """Current points with the writable fields of `target` laid over files that exist."""
    if not isinstance(target, list) or len(target) != len(current):
        raise ControlError(f"Curve must have {len(current)} points.")
    merged = []
    for i, (have, want) in enumerate(zip(current, target)):
        if not isinstance(want, dict) or any(k not in POINT_FIELDS for k in want):
            raise ControlError("Each point must be an object of known fan curve fields.")
        # None marks a missing file, so validate_curve skips it; refuse it for a file that exists
        for f in WRITABLE:
            if want.get(f, 0) is None and have.get(f) is not None:
                raise ControlError(f"Point {i + 1} {LABELS[f]} must be an integer.")
        merged.append({f: (want[f] if f in WRITABLE and f in want and v is not None else v)
                       for f, v in have.items()})
    return merged


def _same(field, a, b, scale):
    return scale.same(a, b) if field in SPEEDS else a == b


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
            raise ControlError("Fan curve is busy: the fan controller is locked.") from exc
        if exc.errno == errno.EPERM:
            raise ControlError("module refuses unvalidated fan curve writes for this model") from exc
        if exc.errno == errno.EOPNOTSUPP:
            raise ControlError("The module rejected this fan curve value or does not support writes here.") from exc
        if exc.errno == errno.EACCES:
            raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
        raise ControlError(f"Fan curve write failed: {exc.strerror}") from exc


def write_curve(hwmon, target, scale=None):
    """Write changed fields (temps first, then speeds), verify by re-reading everything.

    Returns one result per attempted field; raises ControlError before any write when the
    target is malformed or invalid. Each write makes the module rewrite the whole curve, so a
    failure part way leaves the fields before it applied.
    """
    hwmon = Path(hwmon).resolve()
    scale = scale or Scale()
    current = read_curve(hwmon)
    if not current:
        raise ControlError("No fan curve files found.")
    merged = merge_curve(current, target)
    errors = validate_curve(merged, scale)
    if errors:
        raise ControlError(errors[0][2] + (f" ({len(errors) - 1} more problems)" if len(errors) > 1 else ""))
    changes = [(i, f) for fields in (TEMPS, SPEEDS) for f in fields for i in range(len(current))
               if current[i][f] is not None and not _same(f, current[i][f], merged[i][f], scale)]
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
    after = read_curve(hwmon) if results else current
    for n, result in enumerate(results):
        i, f = result["point"], result["field"]
        result["current"] = after[i][f] if i < len(after) else None
        if n >= sent or result["status"] == "FAILED":
            continue
        if _same(f, result["current"], result["requested"], scale):
            result["status"] = "APPLIED"
        else:
            result["status"] = "FAILED"
            result["error"] = f"Requested {result['requested']}, but firmware reports {result['current']!r}."
    return results
