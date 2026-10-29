# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Telemetry sources and the background sampler."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import threading
import time

from .common import (GROUP_ORDER, SLOW_INTERVAL, TEMP_GROUPS, THROTTLE_BITS, _lazy, number, read, scaled)
from .hardware import Hardware

DISCOVERY_INTERVAL = 60  # seconds between sysfs directory re-scans

csv = _lazy("csv")
re = _lazy("re")

_NVML_STRUCTS = None


def _nvml_structs(ct):
    """Util and Mem ctypes Structures, built once on first use (ctypes is imported lazily)."""
    global _NVML_STRUCTS
    if _NVML_STRUCTS is None:
        class Util(ct.Structure):
            _fields_ = [("gpu", ct.c_uint), ("memory", ct.c_uint)]

        class Mem(ct.Structure):
            _fields_ = [("total", ct.c_ulonglong), ("free", ct.c_ulonglong), ("used", ct.c_ulonglong)]

        _NVML_STRUCTS = (Util, Mem)
    return _NVML_STRUCTS


class NVML:
    """NVIDIA management library through ctypes; read() is None when unusable."""

    def __init__(self, lib=None):
        self.lib = lib
        self.failed = False
        self.ready = False
        self.ctypes = None

    def _init(self):
        import ctypes
        self.ctypes = ctypes
        if self.lib is None:
            try:
                self.lib = ctypes.CDLL("libnvidia-ml.so.1")
            except OSError:
                self.failed = True
                return
        try:
            if self.lib.nvmlInit_v2() != 0:
                self.failed = True
                return
        except (OSError, AttributeError):
            self.failed = True
            return
        self.ready = True

    def read(self):
        if self.failed:
            return None
        if self.ctypes is None or not self.ready:
            self._init()
            if self.failed:
                return None
        ct, lib = self.ctypes, self.lib

        Util, Mem = _nvml_structs(ct)

        def value(call, kind, *args):
            box = kind()
            try:
                return box if call(*args, ct.byref(box)) == 0 else None
            except (OSError, AttributeError):
                return None

        try:
            count = value(lib.nvmlDeviceGetCount_v2, ct.c_uint)
            if count is None:
                return None
            devices = []
            for index in range(count.value):
                handle = ct.c_void_p()
                if lib.nvmlDeviceGetHandleByIndex_v2(index, ct.byref(handle)) != 0:
                    continue
                name = ct.create_string_buffer(96)
                named = lib.nvmlDeviceGetName(handle, name, 96) == 0
                temp = value(lib.nvmlDeviceGetTemperature, ct.c_uint, handle, 0)
                power = value(lib.nvmlDeviceGetPowerUsage, ct.c_uint, handle)
                limit = value(lib.nvmlDeviceGetEnforcedPowerLimit, ct.c_uint, handle)
                core = value(lib.nvmlDeviceGetClockInfo, ct.c_uint, handle, 0)
                memclk = value(lib.nvmlDeviceGetClockInfo, ct.c_uint, handle, 2)
                util = value(lib.nvmlDeviceGetUtilizationRates, Util, handle)
                mem = value(lib.nvmlDeviceGetMemoryInfo, Mem, handle)
                reasons = None
                for symbol in ("nvmlDeviceGetCurrentClocksEventReasons",  # older drivers: ...ThrottleReasons
                               "nvmlDeviceGetCurrentClocksThrottleReasons"):
                    call = getattr(lib, symbol, None)
                    reasons = value(call, ct.c_ulonglong, handle) if call else None
                    if reasons is not None:
                        break
                mib = 1024 * 1024
                devices.append({
                    "name": name.value.decode(errors="replace") if named else None,
                    "temperature_c": temp.value if temp is not None else None,
                    "power_w": power.value / 1000 if power is not None else None,
                    "power_limit_w": limit.value / 1000 if limit is not None else None,
                    "clock_mhz": core.value if core is not None else None,
                    "memory_clock_mhz": memclk.value if memclk is not None else None,
                    "utilization_pct": util.gpu if util is not None else None,
                    "memory_used_mib": mem.used / mib if mem is not None else None,
                    "memory_total_mib": mem.total / mib if mem is not None else None,
                    "throttle": [n for bit, n in THROTTLE_BITS if reasons.value & bit]
                    if reasons is not None else None})
            return devices
        except (OSError, AttributeError):
            self.failed = True
            return None


def battery_health(p):
    "Cycle count and wear of one battery; Wh only from energy_* files (charge_* is Ah), None when missing."
    wh = [scaled(p / "energy_full", 1e6), scaled(p / "energy_full_design", 1e6)]
    full, design = wh if wh[0] is not None and wh[1] else (
        scaled(p / "charge_full"), scaled(p / "charge_full_design"))
    ok = full is not None and bool(design)
    energy = wh[0] is not None and bool(wh[1])
    return {"cycles": scaled(p / "cycle_count"), "full_wh": full if energy else None,
            "design_wh": design if energy else None, "health_percent": full / design * 100 if ok else None}


GPU_MODES = {("0", "0"): "dGPU only", ("1", "0"): "Hybrid", ("1", "1"): "iGPU only", ("1", "2"): "Hybrid (auto)"}


def gpu_mode(root):
    """Firmware GPU mode from legion_laptop's gsync and igpumode (read-only); None when unknown."""
    base = Path(root) / "devices/platform/legion"
    return GPU_MODES.get((read(base / "gsync"), read(base / "igpumode")))


class Sensors:
    def __init__(self, root=Path("/sys"), proc=Path("/proc"), dev=Path("/dev")):
        self.root, self.proc, self.dev = Path(root), Path(proc), Path(dev)
        self.nvml = NVML()
        self.previous_cpu = {}
        self.previous_energy = {}
        self.previous_throttle = None
        self.throttle_events = []  # (time, core delta, package delta) within SLOW_INTERVAL
        self.slow = None
        self.slow_at = None
        self.found = {}  # discovery cache: name -> (paths, monotonic time of the scan)

    def discover(self, name, scan):
        """Paths from scan(), re-scanned at most every DISCOVERY_INTERVAL (hot-plug, module load)."""
        cached = self.found.get(name)
        now = time.monotonic()
        if cached is None or now - cached[1] >= DISCOVERY_INTERVAL:
            cached = self.found[name] = (scan(), now)
        return cached[0]

    def cpu_usage(self):
        result = {}
        for line in (read(self.proc / "stat", "") or "").splitlines():
            parts = line.split()
            if not parts or not parts[0].startswith("cpu"):
                continue
            values = list(map(int, parts[1:9]))  # guest times already included in user/nice
            total, idle = sum(values), sum(values[3:5])
            previous = self.previous_cpu.get(parts[0])
            usage = None
            if previous and total > previous[0]:
                usage = max(0, min(100, 100 * (1 - (idle - previous[1]) / (total - previous[0]))))
            result[parts[0]] = usage
            self.previous_cpu[parts[0]] = (total, idle)
        return result

    def rapl_power(self):
        """Watts per RAPL domain, the package sum and a status line."""
        rapl, package, sources, denied = {}, [], [], False
        # AMD Zen exposes package energy through the same powercap intel-rapl driver
        # (amd_energy was removed in 5.13), so one glob covers both vendors.
        for path in sorted(self.root.glob("class/powercap/intel-rapl:*")):  # excludes intel-rapl-mmio duplicates
            name = read(path / "name", "") or ""
            if not name:
                continue
            parent = path.name.count(":") > 1
            if parent:
                parent_name = read(path.with_name(path.name.rsplit(":", 1)[0]) / "name", "") or ""
                key = f"{parent_name}/{name}"
            else:
                key = name
            top_package = not parent and name.startswith("package-")
            energy = scaled(path / "energy_uj")
            now = time.monotonic()
            if energy is None:
                denied = denied or top_package
                rapl[key] = None
                continue
            previous = self.previous_energy.get(str(path))
            self.previous_energy[str(path)] = (now, energy)
            rapl[key] = None
            if previous:
                delta = energy - previous[1]
                if delta < 0:
                    maximum = scaled(path / "max_energy_range_uj")
                    if maximum is None:
                        continue
                    delta += maximum
                elapsed = now - previous[0]
                if elapsed > 0 and delta >= 0:
                    rapl[key] = delta / 1_000_000 / elapsed
                    if top_package:
                        package.append(rapl[key])
                        sources.append(name)
        return (rapl, sum(package) if package else None,
                "RAPL package energy" if sources else
                "RAPL needs root: run sudo loqctl serve" if denied else "Waiting for RAPL samples or no package counter")

    def gpu_suspended(self):
        def scan():
            return [path for path in self.root.glob("bus/pci/devices/*")
                    if (read(path / "vendor", "") or "") == "0x10de"
                    and (read(path / "class", "") or "").startswith("0x03")]
        return any(read(path / "power/runtime_status") == "suspended"
                   for path in self.discover("nvidia_display", scan))

    def gpu(self):
        if self.gpu_suspended():  # querying would wake the dGPU
            return {"available": False, "suspended": True, "error": "GPU is asleep (runtime suspended)"}
        devices = self.nvml.read()
        if devices is not None:
            return {"available": bool(devices), "devices": devices, "source": "nvml"}
        smi = shutil.which("nvidia-smi")
        if not smi:
            return {"available": False, "error": "NVIDIA driver unavailable"}
        fields = "name,temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,utilization.gpu,memory.used,memory.total"
        try:
            result = subprocess.run([smi, "--query-gpu=" + fields,
                                     "--format=csv,noheader,nounits"],
                                    capture_output=True, text=True, timeout=2.5, check=False)
            if result.returncode:
                return {"available": False, "error": "NVIDIA driver unavailable"}
            devices = []
            for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
                if len(row) != 9:
                    continue
                devices.append(dict(zip(("name", "temperature_c", "power_w", "power_limit_w",
                                         "clock_mhz", "memory_clock_mhz", "utilization_pct",
                                         "memory_used_mib", "memory_total_mib"),
                                        [row[0]] + [number(v) for v in row[1:]])))
            return {"available": bool(devices), "devices": devices, "source": "nvidia-smi"}
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "error": "NVIDIA sensor query unavailable or timed out"}

    @staticmethod
    def cpu_temperature(hwmon_dirs):
        """Headline CPU temperature and the label it came from."""
        best = (None, None)
        for hw in hwmon_dirs:
            name = read(hw / "name", "")
            if name not in ("coretemp", "k10temp", "zenpower"):
                continue
            readings = []
            for p in hw.glob("temp*_input"):
                v = scaled(p, 1000)
                if v is not None:
                    readings.append((read(p.with_name(p.name.replace("_input", "_label")), "") or "", v))
            if not readings:
                continue
            if name == "coretemp":
                pick = [r for r in readings if r[0].startswith("Package id")]
                label = "Package id" if pick else "max of cores"
            else:
                pick, label = [r for r in readings if r[0] == "Tdie"], "Tdie"
                if not pick:
                    pick, label = [r for r in readings if r[0] == "Tctl"], "Tctl"
                if not pick:
                    label = "max of " + name
            value = max(v for _, v in (pick or readings))
            if best[0] is None or value > best[0]:
                best = (value, label)
        return best

    def cpu_vcore(self):
        """CPU core voltage from MSR 0x198 (IA32_PERF_STATUS); root and Intel only."""
        # AMD TBD: no stable MSR/sysfs source, so it stays None there.
        if os.geteuid() != 0:
            return None
        vendor = next((line.split(":", 1)[1].strip() for line in (read(self.proc / "cpuinfo", "") or "").splitlines()
                       if line.startswith("vendor_id")), None)
        msr = self.dev / "cpu/0/msr"
        if vendor != "GenuineIntel" or not msr.exists():
            return None
        try:
            fd = os.open(msr, os.O_RDONLY)
            try:
                raw = os.pread(fd, 8, 0x198)
            finally:
                os.close(fd)
            if len(raw) != 8:
                return None
            volts = ((int.from_bytes(raw, "little") >> 32) & 0xFFFF) / 8192
            return volts if 0.3 <= volts <= 2.0 else None  # implausible: N/A, not a guess
        except OSError:
            return None

    def throttle_files(self):
        """(core, package) counter files, one per physical core and package (SMT siblings share a counter)."""
        core_files, package_files, seen, cores = [], [], set(), set()
        for path in sorted(self.root.glob("devices/system/cpu/cpu[0-9]*/thermal_throttle")):
            cpu = path.parent
            core_id = read(cpu / "topology/core_id")
            core_key = (read(cpu / "topology/physical_package_id"), core_id) if core_id is not None else cpu
            if core_key not in cores:
                cores.add(core_key)
                core_files.append(path / "core_throttle_count")
            pkg = read(cpu / "topology/physical_package_id")
            if (pkg is None and cpu.name != "cpu0") or pkg in seen:
                continue
            seen.add(pkg)
            package_files.append(path / "package_throttle_count")
        return core_files, package_files

    def cpu_throttle(self, now):
        """Throttle events over the last SLOW_INTERVAL, from per-sample counter deltas."""
        def total(paths):
            values = [v for p in paths if (v := scaled(p)) is not None]
            return sum(values) if values else None
        core_files, package_files = self.discover("throttle", self.throttle_files)
        current = (total(core_files), total(package_files))
        previous, self.previous_throttle = self.previous_throttle, current
        if previous is not None:
            self.throttle_events.append((now, *(None if a is None or b is None else max(0, a - b)
                                                for a, b in zip(current, previous))))
        while self.throttle_events and now - self.throttle_events[0][0] >= SLOW_INTERVAL:
            self.throttle_events.pop(0)

        def window(i):
            values = [e[i] for e in self.throttle_events if e[i] is not None]
            return sum(values) if values else None
        events = [window(1), window(2)]
        known = [v for v in events if v is not None]
        return {"core_events": events[0], "package_events": events[1],
                "active": any(v > 0 for v in known) if known else None}

    def read_slow(self, hwmons):
        """Sources refreshed every SLOW_INTERVAL seconds."""
        battery_w, battery_status, battery_v, usbc = None, None, None, []
        for p in sorted(self.root.glob("class/power_supply/*")):
            kind = read(p / "type")
            if kind == "Battery" and battery_w is None and battery_status is None:
                volts, amps = scaled(p / "voltage_now", 1e6), scaled(p / "current_now", 1e6)
                battery_w = scaled(p / "power_now", 1e6)
                if battery_w is None and volts is not None and amps is not None:
                    battery_w = volts * amps
                battery_status, battery_v = read(p / "status"), volts
            elif p.name.startswith("ucsi"):
                volts, amps = scaled(p / "voltage_now", 1e6), scaled(p / "current_now", 1e6)
                online = read(p / "online")
                usbc.append({"name": p.name, "online": online == "1" if online is not None else None,
                             "volts": volts, "amps": amps,
                             "watts": volts * amps if volts is not None and amps is not None else None})
        temperatures, voltages, dimm = [], [], 0
        if battery_v is not None:
            voltages.append({"label": "Battery", "v": battery_v})
        for hw in hwmons:
            name = read(hw / "name", "") or ""
            group = "wifi" if name.startswith("iwlwifi") else TEMP_GROUPS.get(name)
            for p in sorted(hw.glob("temp*_input")):
                value = scaled(p, 1000)
                if group is None or value is None:
                    continue
                if group == "ec" and value == 0:
                    value = None  # legion_hwmon reports 0 for a sensor the model does not have
                num = p.name[4:].split("_")[0]
                label = read(p.with_name(f"temp{num}_label"))
                if group == "ram":
                    dimm += 1
                    label = f"DIMM {dimm}"
                temperatures.append({"group": group, "label": label or f"temp {num}", "c": value})
            for p in sorted(hw.glob("in*_input")):
                value = scaled(p, 1000)
                num = p.name[2:].split("_")[0]
                label = read(p.with_name(f"in{num}_label")) or f"in{num}"
                voltages.append({"label": f"{name} {label}", "v": value})
        voltages.append({"label": "CPU 0 Vcore", "v": self.cpu_vcore()})
        return {"battery_w": battery_w, "battery_status": battery_status, "usbc": usbc,
                "temperatures": temperatures, "voltages": voltages}

    def sample(self, now=None):
        now = time.monotonic() if now is None else now
        usage = self.cpu_usage()
        fans = []
        hwmons = self.discover("hwmon", lambda: sorted(self.root.glob("class/hwmon/hwmon*")))
        for hw in hwmons:
            name = read(hw / "name", "")
            for p in hw.glob("fan*_input"):
                fans.append({"name": read(p.with_name(p.name.replace("_input", "_label")), name + "/" + p.stem),
                             "rpm": scaled(p)})
        freq_paths = self.discover("cpufreq", lambda: sorted(
            self.root.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")))
        mhz = [v for p in freq_paths
               if (v := scaled(p, 1000)) is not None]
        rapl, power, power_source = self.rapl_power()
        temperature, temperature_source = self.cpu_temperature(hwmons)
        if self.slow is None or self.slow_at is None or now - self.slow_at >= SLOW_INTERVAL:
            self.slow, self.slow_at = self.read_slow(hwmons), now
        slow = self.slow
        mem = {}
        for line in (read(self.proc / "meminfo", "") or "").splitlines():
            key, value = line.split(":", 1)
            mem[key] = int(value.split()[0]) / 1024
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        battery = []
        ac = []
        conservation = next((v for f in sorted(self.root.glob("bus/platform/drivers/ideapad_acpi/*/conservation_mode"))
                             if (v := read(f)) in ("0", "1")), None)
        for p in self.root.glob("class/power_supply/*"):
            if read(p / "type") == "Battery":
                threshold = scaled(p / "charge_control_end_threshold")
                limit = (f"{threshold:.0f}%" if threshold is not None
                         else None if conservation is None else "on" if conservation == "1" else "off")
                battery.append({"charge_limit": limit, "name": p.name, "percent": scaled(p / "capacity"),
                                "status": read(p / "status"), "power_w": scaled(p / "power_now", 1e6),
                                **battery_health(p)})
            elif (online := read(p / "online")) is not None:
                ac.append(online == "1")
        gpu = self.gpu()
        devices = gpu.get("devices") or []
        temperatures = list(slow["temperatures"]) + [
            {"group": "gpu", "label": d.get("name") or "GPU", "c": d["temperature_c"]}
            for d in devices if d.get("temperature_c") is not None]
        temperatures.sort(key=lambda t: (GROUP_ORDER.index(t["group"]), [
            int(c) if c.isdigit() else c for c in re.split(r"(\d+)", t["label"])]))
        voltages = list(slow["voltages"]) + ([{"label": "GPU core", "v": None}] if devices else [])
        return {"timestamp": time.time(),
                "cpu": {"utilization_pct": usage.pop("cpu", None), "cores": usage,
                        "temperature_c": temperature, "temperature_source": temperature_source,
                        "power_w": power, "power_source": power_source,
                        "clock_mhz": sum(mhz) / len(mhz) if mhz else None,
                        "max_clock_mhz": max(mhz) if mhz else None},
                "gpu": gpu, "gpu_mode": gpu_mode(self.root), "memory": {"total_mib": total,
                    "used_mib": total - available if total is not None and available is not None else None},
                "battery": battery, "ac_online": any(ac) if ac else None, "fans": fans,
                "power": {"rapl": rapl, "battery_w": slow["battery_w"],
                          "battery_status": slow["battery_status"], "usbc": slow["usbc"]},
                "temperatures": temperatures, "voltages": voltages,
                "throttle": {"cpu": self.cpu_throttle(now),
                             "gpu": devices[0].get("throttle") if devices else None},
                "load": list(os.getloadavg()), "uptime_s": number((read(self.proc / "uptime", "0")).split()[0])}


class Sampler:
    FIRMWARE_INTERVAL = 5
    on_firmware = None  # serve hook: called with each good firmware read, on its own thread
    hook = None  # that thread, so a slow write never stalls the sampler

    def __init__(self, hardware=None):
        self.hardware = hardware or Hardware()
        self.sensors = Sensors(self.hardware.root)
        self.latest = {}
        self.caps = {}  # last good firmware read
        self.metrics = {}
        self.fw_error = None
        self.next_firmware = None
        self.stop = threading.Event()
        self.force = threading.Event()  # refresh() sets it: read firmware now and wake the wait
        self.thread = threading.Thread(target=self.run, daemon=True)

    def tick(self):
        now = time.monotonic()
        # no firmware read while the hook runs: it would wait on Hardware.lock, and the hook then
        # sees the state after its own write on the next read
        busy = self.busy()
        if not busy and (self.force.is_set() or self.next_firmware is None or now >= self.next_firmware):
            self.force.clear()
            self.next_firmware = now + self.FIRMWARE_INTERVAL
            try:
                self.caps = self.hardware.capabilities()
                self.fw_error = None
            except Exception as exc:
                self.fw_error = str(exc)
            if self.on_firmware and self.fw_error is None:
                self.hook = threading.Thread(target=self.call_hook, args=(self.caps,), daemon=True)
                self.hook.start()
        error = self.fw_error
        try:
            self.metrics = self.sensors.sample()
        except Exception as exc:
            error = str(exc)
        latest = {**self.caps, "metrics": self.metrics}
        if error:
            latest["error"] = error
        if busy:
            latest["stale"] = True  # firmware fields are from before the running hook
        self.latest = latest

    def call_hook(self, caps):
        try:
            self.on_firmware(caps)
        except Exception as exc:
            print(f"firmware hook failed: {exc}", flush=True)

    def busy(self):
        return self.hook is not None and self.hook.is_alive()

    def run(self):
        while not self.stop.is_set():
            self.tick()
            if self.busy():  # a pending refresh waits for the hook, not a busy loop
                self.stop.wait(1)
            else:
                self.force.wait(1)

    def refresh(self):
        self.force.set()

    def halt(self):
        self.stop.set()
        self.force.set()
        # a write in flight finishes, but quitting must not hang: join the sampler, then its hook
        if self.thread.is_alive() and self.thread is not threading.current_thread():
            self.thread.join(3)
        if self.busy() and self.hook is not threading.current_thread():
            self.hook.join(10)

    def start(self):
        self.thread.start()
        return self
