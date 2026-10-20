# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Shared helpers and constants."""
from __future__ import annotations

import importlib.util
import math
import os
from pathlib import Path
import subprocess
import sys

def _lazy(name):
    """Import a module on first attribute access to keep CLI startup cheap."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader = importlib.util.LazyLoader(spec.loader)
    loader.exec_module(module)
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


# Root loqctl.py, which sudo runs with python -I.
ENTRY = str(Path(__file__).resolve().parent.parent / "loqctl.py")
SLOW_INTERVAL = 5  # seconds between refreshes of slow telemetry sources
LEGIONAURA = "legionaura"
LEGIONAURA_CONFIG = Path.home() / ".config/legionaura/config.json"
# Explicit write allowlist. Other firmware attributes are never writable here.
# Never add dgpu_enable, gpu_mode, dgpu_didvid or dgpu_boost_clk. Only attributes the
# firmware exposes appear (capabilities skips missing ones).
FIELDS = {
    "ppt_pl1_spl": ("CPU sustained / PL1", "W"),
    "ppt_pl2_sppt": ("CPU boost / PL2", "W"),
    "ppt_cpu_cl": ("CPU cross-load", "W"),
    "ppt_pl1_spl_cl": ("CPU PL1 cross-load", "W"),
    "ppt_pl2_sppt_cl": ("CPU PL2 cross-load", "W"),
    "ppt_pl3_fppt": ("CPU fast / PL3", "W"),
    "ppt_pl3_fppt_cl": ("CPU PL3 cross-load", "W"),
    "ppt_pl4_ipl": ("CPU peak / PL4", "W"),
    "ppt_pl4_ipl_cl": ("CPU PL4 cross-load", "W"),
    "ppt_pl1_apu_spl": ("APU sustained", "W"),
    "ppt_pl1_tau": ("CPU boost duration", "s"),
    "cpu_temp": ("CPU temperature limit", "°C"),
    "gpu_nv_ctgp": ("GPU configurable TGP", "W"),
    "gpu_nv_ppab": ("GPU dynamic boost", "W"),
    "gpu_nv_ac_offset": ("GPU AC power offset", "W"),
    "gpu_nv_bpl": ("GPU base power", "W"),
    "gpu_nv_cpu_boost": ("GPU CPU boost", "W"),
    "gpu_temp": ("GPU temperature limit", "°C"),
}
# FIELDS key -> (dev, feat, type) in the firmware capability table (kernel wmi-other.c).
FIELD_IDS = {
    "ppt_pl2_sppt": (1, 1, 0), "ppt_pl1_spl": (1, 2, 0), "ppt_pl3_fppt": (1, 3, 0),
    "cpu_temp": (1, 4, 0), "ppt_pl1_apu_spl": (1, 5, 0), "ppt_cpu_cl": (1, 6, 0),
    "ppt_pl1_tau": (1, 7, 0), "ppt_pl4_ipl": (1, 9, 0), "ppt_pl1_spl_cl": (1, 2, 1),
    "ppt_pl2_sppt_cl": (1, 1, 1), "ppt_pl3_fppt_cl": (1, 3, 1), "ppt_pl4_ipl_cl": (1, 9, 1),
    "gpu_nv_ppab": (2, 1, 0), "gpu_nv_ctgp": (2, 2, 0), "gpu_temp": (2, 3, 0),
    "gpu_nv_ac_offset": (2, 4, 0), "gpu_nv_bpl": (2, 10, 0), "gpu_nv_cpu_boost": (2, 11, 0),
}
# NVML clocks-event reasons; 0x1 (gpu_idle) is not a throttle and is omitted.
THROTTLE_BITS = ((0x2, "app_clocks"), (0x4, "sw_power_cap"), (0x8, "hw_slowdown"),
                 (0x10, "sync_boost"), (0x20, "sw_thermal"), (0x40, "hw_thermal"),
                 (0x80, "hw_power_brake"), (0x100, "display_clocks"))
TEMP_GROUPS = {"coretemp": "cpu", "k10temp": "cpu", "zenpower": "cpu", "acpitz": "acpi",
               "nvme": "nvme", "spd5118": "ram", "jc42": "ram", "amdgpu": "gpu",
               "legion_hwmon": "ec"}  # legion_hwmon: LenovoLegionLinux module name, unverified
GROUP_ORDER = ("cpu", "gpu", "ec", "acpi", "nvme", "ram", "wifi")
SPARK = "▁▂▃▄▅▆▇█"
BAR_FILL = "█"
BAR_EMPTY = "░"


class ControlError(Exception):
    pass


class SudoExpired(ControlError):
    pass


def sudo_run(args, timeout=30):
    """Run `loqctl.py <args>` as root via `sudo -n`; returns stdout, raises ControlError."""
    command = ["sudo", "-n", "--", sys.executable, "-I", ENTRY, *args]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ControlError("sudo timed out.") from exc
    stderr = (result.stderr or "").strip()
    if result.returncode:
        if "password is required" in stderr or "a terminal is required" in stderr:
            raise SudoExpired("sudo credentials expired.")
        raise ControlError(stderr or (result.stdout or "").strip() or f"sudo exited with code {result.returncode}")
    return result.stdout or "", stderr


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def scaled(path, divisor=1):
    value = number(read(path))
    return value / divisor if value is not None else None


def owner_uid():
    return int(os.environ.get("SUDO_UID", os.getuid()))


def socket_path():
    return f"/run/loqctl-{owner_uid()}/control.sock"


def unit_sep(unit):
    """Space between a number and its unit, except for °C and %."""
    return "" if unit in ("", "%", "°C") else " "


def fmt(value, unit="", decimals=0):
    unit = unit.strip()
    return "N/A" if value is None else f"{value:.{decimals}f}{unit_sep(unit)}{unit}"


def clamp(value, lo, hi):
    return max(lo, min(hi, value))
