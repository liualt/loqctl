#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Report how legion_hwmon stores fan curve speeds, for checking percent-unit models.

Read-only by default. Prints the model, fancurve_speed_unit and each pwmN_auto_pointM_pwm value
with the speed loqctl derives from it, and appends the output to ~/loqctl-fancurve-check.log.

--write also changes point 1 fan 1 by 1 % through loqctl's allowlisted fan curve write, shows
what the module reads back, then restores the original value. It needs root. It never touches
fanN_target.
"""
import argparse
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loqctl import fancurve  # noqa: E402
from loqctl.common import ControlError, read  # noqa: E402

LOG = Path.home() / "loqctl-fancurve-check.log"


def report(hwmon, scale, out):
    out(f"unit: {fancurve.speed_unit(hwmon) or 'none (percent)'}, fan1_max: {read(hwmon / 'fan1_max')}")
    for n in range(1, fancurve.MAX_POINTS + 1):
        for name in ("pwm1", "pwm2"):
            path = hwmon / f"{name}_auto_point{n}_pwm"
            if path.exists():
                raw = fancurve._int(read(path))
                out(f"{path.name}: {raw} -> {scale.text(raw)}")


def write_check(hwmon, scale, out):
    points = fancurve.read_curve(hwmon)
    original = points[0]["fan1"] if points else None
    if original is None:
        out("write check skipped: point 1 fan 1 is not readable")
        return
    out(f"write check: point 1 fan 1 starts at {original} ({scale.text(original)})")
    try:
        for step in (1, -1):
            target = [{"fan1": scale.pwm(scale.show(original) + step)}]
            try:
                results = fancurve.write_curve(hwmon, target, scale)
            except ControlError as exc:
                out(f"{step:+d} %: refused: {exc}")
                continue
            now = fancurve.read_curve(hwmon)[0]["fan1"]
            out(f"{step:+d} %: wrote {target[0]['fan1']}, module reads back {now} ({scale.text(now)}), "
                f"{[r['status'] for r in results]}")
            break
    finally:
        if fancurve.read_curve(hwmon)[0]["fan1"] != original:
            try:
                fancurve.write_curve(hwmon, [{"fan1": original}], scale)
            except ControlError as exc:
                out(f"RESTORE FAILED, set pwm1_auto_point1_pwm back to {original} by hand: {exc}")
        now = fancurve.read_curve(hwmon)[0]["fan1"]
        out(f"restored: point 1 fan 1 is {now} ({scale.text(now)}), originally {original}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="also test a 1 %% write and restore it (root)")
    args = parser.parse_args()
    lines = []

    def out(text):
        lines.append(text)
        print(text)

    out(f"--- {datetime.now():%Y-%m-%d %H:%M:%S} ---")
    out(f"model: {read(Path('/sys/class/dmi/id/product_name'))}")
    hwmon = fancurve.find_legion_hwmon("/sys")
    if hwmon is None:
        out("no legion_hwmon found; is legion_laptop loaded?")
    else:
        scale = fancurve.curve_scale(hwmon)
        report(hwmon, scale, out)
        if args.write:
            write_check(hwmon, scale, out)
    with LOG.open("a") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"logged to {LOG}")


if __name__ == "__main__":
    main()
