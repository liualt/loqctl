# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Read-only check of which loqctl features this machine supports."""
from __future__ import annotations

import os
from pathlib import Path
import platform
import shutil

from . import VERSION
from .capdata import read_mode_table
from .common import LEGIONAURA, read
from .fancurve import find_legion_hwmon
from .hardware import Hardware

OK, MISSING, NA = "ok", "missing", "n/a"


def cpu_vendor(proc):
    text = read(Path(proc) / "cpuinfo", "") or ""
    return next((line.split(":", 1)[1].strip() for line in text.splitlines() if line.startswith("vendor_id")), None)


def check_attributes(root, hw, **_):
    if hw.base and hw.base.is_dir():
        return OK, ""
    return MISSING, "needs kernel 6.17+ with the lenovo-wmi-other driver"


def check_profile(root, hw, **_):
    if "custom" in (read(hw.choices, "") or "").split():
        return OK, ""
    return MISSING, "no platform profile offers custom; limits stay read-only"


def check_gamezone(root, hw, **_):
    if not list(Path(root).glob("bus/wmi/devices/887B54E3-*")):
        return MISSING, "gamezone WMI device not found; load lenovo_wmi_gamezone"
    holder = hw.gamezone_holder()
    return (OK, "") if holder is None else (MISSING, f"held by {holder}; load lenovo_wmi_gamezone first")


def check_hwmon(root, **_):
    if find_legion_hwmon(root):
        return OK, ""
    return MISSING, "install the legion_laptop module for fan curves"


def check_capdata(root, **_):
    table, reason = read_mode_table(root)
    return (OK, "") if table else (MISSING, reason)


def check_rapl(root, **_):
    paths = sorted(Path(root).glob("class/powercap/intel-rapl:*/energy_uj"))
    if not paths:
        return MISSING, "no powercap RAPL domain; load intel_rapl_common"
    try:
        paths[0].read_text()
    except OSError:
        return MISSING, "energy_uj needs root; run sudo loqctl serve"
    return OK, ""


def check_msr(dev, proc, **_):
    if cpu_vendor(proc) != "GenuineIntel":
        return NA, "CPU voltage is Intel only"
    msr = Path(dev) / "cpu/0/msr"
    if not msr.exists():
        return MISSING, "modprobe msr for CPU voltage"
    if not os.access(msr, os.R_OK):
        return MISSING, "msr needs root; run sudo loqctl serve"
    return OK, ""


def check_nvidia(**_):
    if shutil.which("nvidia-smi"):
        return OK, ""
    try:
        import ctypes
        ctypes.CDLL("libnvidia-ml.so.1")
        return OK, ""
    except OSError:
        return MISSING, "no NVIDIA driver; GPU panels stay N/A"


def check_legionaura(**_):
    if shutil.which(LEGIONAURA):
        return OK, ""
    return MISSING, "install legionaura for keyboard RGB"


def check_sudo(**_):
    if os.geteuid() == 0 or shutil.which("sudo"):
        return OK, ""
    return MISSING, "firmware writes need sudo or a root loqctl serve"


CHECKS = (("firmware-attributes", check_attributes), ("platform-profile custom", check_profile),
          ("gamezone binding", check_gamezone), ("legion_laptop hwmon", check_hwmon),
          ("debugfs capdata", check_capdata), ("RAPL readable", check_rapl), ("msr", check_msr),
          ("NVML or nvidia-smi", check_nvidia), ("legionaura", check_legionaura), ("sudo", check_sudo))


def doctor(root=Path("/sys"), proc=Path("/proc"), dev=Path("/dev")):
    """Report lines: environment details, then one line per check with a hint when not ok."""
    root = Path(root)
    hw = Hardware(root)
    dmi = root / "class/dmi/id"
    info = [("product", read(dmi / "product_name")), ("BIOS", read(dmi / "bios_version")),
            ("CPU vendor", cpu_vendor(proc)), ("kernel", platform.release()), ("loqctl", VERSION),
            ("legion_laptop", read(root / "module/legion_laptop/version"))]
    lines = [f"{name}: {value}" for name, value in info if value]
    for name, check in CHECKS:
        state, hint = check(root=root, hw=hw, proc=proc, dev=dev)
        lines.append(f"{state:8} {name}" + (f" ({hint})" if hint else ""))
    return lines
