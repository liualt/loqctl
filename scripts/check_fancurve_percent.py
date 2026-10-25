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
import os
import pwd
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loqctl import fancurve  # noqa: E402
from loqctl.common import ControlError, read  # noqa: E402

LOG_NAME = "loqctl-fancurve-check.log"


def log_path():
    """The log in the invoking user's home, not root's, when run through sudo."""
    uid = os.environ.get("SUDO_UID")
    home = Path(pwd.getpwuid(int(uid)).pw_dir) if uid and uid.isdigit() else Path.home()
    return home / LOG_NAME


def append_log(path, text):
    new = not path.exists()
    with path.open("a") as handle:  # per line, so a crash still leaves a log
        handle.write(text + "\n")
    uid, gid = os.environ.get("SUDO_UID"), os.environ.get("SUDO_GID")
    if new and uid and gid and uid.isdigit() and gid.isdigit():
        os.chown(path, int(uid), int(gid))


def report(hwmon, scale, out):
    out(f"unit: {fancurve.speed_unit(hwmon) or 'none (percent)'}, fan1_max: {read(hwmon / 'fan1_max')}")
    for n in range(1, fancurve.MAX_POINTS + 1):
        for name in ("pwm1", "pwm2"):
            path = hwmon / f"{name}_auto_point{n}_pwm"
            if path.exists():
                raw = fancurve._int(read(path))
                out(f"{path.name}: {raw} -> {scale.text(raw)}")


def write_check(hwmon, scale, out):
    original = first(hwmon)
    if original is None:
        out("write check skipped: point 1 fan 1 is not readable")
        return
    out(f"write check: point 1 fan 1 starts at {original} ({scale.text(original)})")
    for step in (1, -1):
        target = only_fan1(hwmon, scale.pwm(scale.show(original) + step))
        try:
            results = fancurve.write_curve(hwmon, target, scale)
            out(f"{step:+d} %: wrote {target[0]['fan1']}, module reads back {first(hwmon)} "
                f"({scale.text(first(hwmon))}), {[r['status'] for r in results]}")
        except ControlError as exc:
            out(f"{step:+d} %: refused: {exc}")
        if not restore(hwmon, scale, original, out):
            return  # do not test anything else while the original value is not back


def only_fan1(hwmon, pwm):
    """A full target (one entry per point) that changes point 1 fan 1 alone."""
    return [{"fan1": pwm}] + [{} for _ in fancurve.read_curve(hwmon)[1:]]


def first(hwmon):
    points = fancurve.read_curve(hwmon)
    return points[0]["fan1"] if points else None


def restore(hwmon, scale, original, out):
    """Put the original pwm back by writing the allowlisted file directly, so loqctl's curve
    validation cannot refuse it. Returns whether it reads back as the original."""
    try:
        if first(hwmon) != original:
            fancurve._write_file(fancurve._path(hwmon, "fan1", 1), original)
    except ControlError as exc:
        out(f"RESTORE FAILED, set pwm1_auto_point1_pwm back to {original} by hand: {exc}")
        return False
    now = first(hwmon)
    ok = now is not None and scale.same(now, original)
    out(f"restored: point 1 fan 1 is {now} ({scale.text(now)}), originally {original}"
        + ("" if ok else ". RESTORE FAILED, set it back by hand"))
    return ok


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="also test a 1 %% write and restore it (root)")
    args = parser.parse_args()
    if args.write and os.geteuid() != 0:
        sys.exit("--write needs root: the fan curve files are only writable by root.")

    log = log_path()

    def out(text):
        print(text)
        append_log(log, text)

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
    print(f"logged to {log}")


if __name__ == "__main__":
    main()
