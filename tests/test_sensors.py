# SPDX-License-Identifier: GPL-3.0-or-later
import os
import shutil
import subprocess
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import common as m_common
from loqctl import hardware as m_hardware
from loqctl import sensors as m_sensors


class SensorsTests(helpers.SysfsCase):
    def test_rapl_wrap_and_duplicate_exclusion(self):
        rapl = self.root / "class/powercap/intel-rapl:0"
        rapl.mkdir(parents=True)
        (rapl / "name").write_text("package-0")
        (rapl / "energy_uj").write_text("9000000")
        (rapl / "max_energy_range_uj").write_text("10000000")
        duplicate = self.root / "class/powercap/intel-rapl-mmio:0"
        duplicate.mkdir()
        (duplicate / "name").write_text("package-0")
        (duplicate / "energy_uj").write_text("99999999999")
        sensors = m_sensors.Sensors(self.root)
        with patch.object(time, "monotonic", return_value=1):
            self.assertIsNone(sensors.rapl_power()[1])
        (rapl / "energy_uj").write_text("3000000")
        with patch.object(time, "monotonic", return_value=3):
            self.assertEqual(sensors.rapl_power()[1], 2)

    def test_unsupported_sensor_values_are_null(self):
        for value in ("[N/A]", "nan", "inf", None, "Not Supported"):
            self.assertIsNone(m_common.number(value))

    def test_gpu_mode_decodes_gsync_and_igpumode(self):
        base = self.root / "devices/platform/legion"
        self.assertIsNone(m_sensors.gpu_mode(self.root))
        base.mkdir(parents=True)
        for gsync, igpu, expected in (("0", "0", "dGPU only"), ("1", "0", "Hybrid"), ("1", "1", "iGPU only"),
                                      ("1", "2", "Hybrid (auto)"), ("0", "1", None), ("1", "7", None)):
            (base / "gsync").write_text(gsync)
            (base / "igpumode").write_text(igpu)
            self.assertEqual(m_sensors.gpu_mode(self.root), expected)

    def test_smt_siblings_counted_once(self):
        root = helpers.make_root(self)
        s = m_sensors.Sensors(root, root / "proc", root / "dev")
        def counts(core):
            for i, core_id in ((0, "0"), (1, "0"), (2, "1")):
                base = root / f"devices/system/cpu/cpu{i}"
                (base / "thermal_throttle").mkdir(parents=True, exist_ok=True)
                (base / "topology").mkdir(parents=True, exist_ok=True)
                (base / "thermal_throttle/core_throttle_count").write_text(str(core))
                (base / "topology/core_id").write_text(core_id)
                (base / "topology/physical_package_id").write_text("0")
        counts(1)
        s.cpu_throttle(0)
        counts(3)
        self.assertEqual(s.cpu_throttle(1)["core_events"], 4)


class FakeNVML:
    """Plain-Python stand-in for libnvidia-ml; bad names return nonzero."""
    def __init__(self, bad=()):
        self.bad = set(bad)
        f = self
        def out(name, setter):
            def call(*args):
                if name in f.bad:
                    return 3
                setter(args[-1]._obj)
                return 0
            return call
        self.nvmlInit_v2 = lambda: 0
        self.nvmlDeviceGetCount_v2 = out("count", lambda o: setattr(o, "value", 1))
        self.nvmlDeviceGetHandleByIndex_v2 = lambda i, h: 0
        self.nvmlDeviceGetName = lambda h, buf, n: (setattr(buf, "value", b"RTX Test"), 0)[1]
        self.nvmlDeviceGetTemperature = out("temp", lambda o: setattr(o, "value", 51))
        self.nvmlDeviceGetPowerUsage = out("power", lambda o: setattr(o, "value", 45000))
        self.nvmlDeviceGetEnforcedPowerLimit = out("limit", lambda o: setattr(o, "value", 80000))
        self.nvmlDeviceGetClockInfo = out("clock", lambda o: setattr(o, "value", 1500))
        def util(o):
            o.gpu, o.memory = 33, 10
        self.nvmlDeviceGetUtilizationRates = out("util", util)
        def mem(o):
            o.total, o.free, o.used = 8192 * 2**20, 6144 * 2**20, 2048 * 2**20
        self.nvmlDeviceGetMemoryInfo = out("mem", mem)


class PortabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def pci(self, status):
        self.put("bus/pci/devices/0000:01:00.0/vendor", "0x10de\n")
        self.put("bus/pci/devices/0000:01:00.0/class", "0x030000\n")
        self.put("bus/pci/devices/0000:01:00.0/power/runtime_status", status + "\n")

    def test_nvml_reads_devices(self):
        devices = m_sensors.NVML(FakeNVML()).read()
        self.assertEqual(len(devices), 1)
        d = devices[0]
        self.assertEqual((d["name"], d["temperature_c"], d["power_w"], d["power_limit_w"]),
                         ("RTX Test", 51, 45.0, 80.0))
        self.assertEqual((d["clock_mhz"], d["memory_clock_mhz"], d["utilization_pct"]), (1500, 1500, 33))
        self.assertEqual((d["memory_used_mib"], d["memory_total_mib"]), (2048, 8192))

    def test_nvml_unsupported_field_is_none(self):
        d = m_sensors.NVML(FakeNVML(bad={"power"})).read()[0]
        self.assertIsNone(d["power_w"])
        self.assertEqual(d["temperature_c"], 51)
        self.assertEqual(d["power_limit_w"], 80.0)

    def test_nvml_failure_falls_back_to_nvidia_smi(self):
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        csv_out = "RTX Test, 50, 20.5, 80, 300, 405, 7, 100, 8192\n"
        run = Mock(return_value=Mock(returncode=0, stdout=csv_out))
        with patch("ctypes.CDLL", side_effect=OSError("no lib")), \
                patch.object(shutil, "which", return_value="/x/nvidia-smi"), \
                patch.object(subprocess, "run", run):
            gpu = sensors.gpu()
            self.assertTrue(sensors.nvml.failed)
        self.assertEqual(gpu["source"], "nvidia-smi")
        self.assertEqual(gpu["devices"][0]["power_w"], 20.5)
        self.assertEqual(run.call_args[0][0][0], "/x/nvidia-smi")
        with patch.object(shutil, "which", return_value=None):
            self.assertEqual(sensors.gpu(), {"available": False, "error": "NVIDIA driver unavailable"})

    def test_gpu_skipped_while_suspended(self):
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        sensors.nvml = Mock()
        sensors.nvml.read.return_value = []
        self.pci("suspended")
        with patch.object(subprocess, "run") as run:
            gpu = sensors.gpu()
        self.assertTrue(gpu["suspended"])
        self.assertFalse(gpu["available"])
        sensors.nvml.read.assert_not_called()
        run.assert_not_called()
        self.pci("active")
        sensors.nvml.read.return_value = [{"name": "x"}]
        self.assertEqual(sensors.gpu()["source"], "nvml")
        sensors.nvml.read.assert_called_once()

    def test_gpu_switching_attributes_never_writable(self):
        forbidden = ("dgpu_enable", "gpu_mode", "dgpu_didvid", "dgpu_boost_clk")
        for key in forbidden:
            self.assertNotIn(key, m_common.FIELDS)
        attr = self.root / "class/firmware-attributes/lenovo-wmi-other-0/attributes/dgpu_enable"
        attr.mkdir(parents=True)
        (attr / "current_value").write_text("0")
        with self.assertRaises(m_common.ControlError):
            m_hardware.Hardware(self.root).set_value("dgpu_enable", 1)

    def hwmon(self, n, name, temps):
        self.put(f"class/hwmon/hwmon{n}/name", name)
        for i, (label, value) in enumerate(temps, 1):
            self.put(f"class/hwmon/hwmon{n}/temp{i}_input", str(value * 1000))
            if label:
                self.put(f"class/hwmon/hwmon{n}/temp{i}_label", label)

    def temp_of(self):
        return m_sensors.Sensors.cpu_temperature(sorted((self.root / "class/hwmon").glob("hwmon*")))

    def test_cpu_temperature_labels(self):
        self.assertEqual(self.temp_of(), (None, None))
        self.hwmon(0, "coretemp", [("Package id 0", 60), ("Core 0", 70)])
        self.assertEqual(self.temp_of(), (60, "Package id"))
        shutil.rmtree(self.root / "class")
        self.hwmon(0, "k10temp", [("Tctl", 80), ("Tdie", 65), ("Tccd1", 90)])
        self.assertEqual(self.temp_of(), (65, "Tdie"))
        shutil.rmtree(self.root / "class")
        self.hwmon(0, "k10temp", [("Tctl", 80), ("Tccd1", 90)])
        self.assertEqual(self.temp_of(), (80, "Tctl"))
        shutil.rmtree(self.root / "class")
        self.hwmon(0, "zenpower", [("Tdie", 55), ("Tctl", 75)])
        self.assertEqual(self.temp_of(), (55, "Tdie"))

    def test_amd_rapl_package_power(self):
        self.hwmon(0, "k10temp", [("Tctl", 50)])
        self.put("class/powercap/intel-rapl:0/name", "package-0")
        self.put("class/powercap/intel-rapl:0/energy_uj", "1000000")
        self.put("class/powercap/intel-rapl:0/max_energy_range_uj", "262143328850")
        self.put("proc/uptime", "10 5")
        self.put("proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 kB")
        sensors = m_sensors.Sensors(self.root, self.root / "proc")
        sensors.gpu = lambda: {"available": False}
        clock = [100.0]
        with patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            first = sensors.sample()
            self.put("class/powercap/intel-rapl:0/energy_uj", "21000000")
            clock[0] = 102.0
            second = sensors.sample()
        self.assertIsNone(first["cpu"]["power_w"])
        self.assertAlmostEqual(second["cpu"]["power_w"], 10.0)
        self.assertEqual(second["cpu"]["temperature_source"], "Tctl")


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)
        self.put("proc/uptime", "10 5")
        self.put("proc/meminfo", "MemTotal: 1024 kB\nMemAvailable: 512 kB")

    def put(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def sensors(self):
        s = m_sensors.Sensors(self.root, self.root / "proc", self.root / "dev")
        s.gpu = lambda: {"available": False}
        return s

    def rapl(self, d, name, uj):
        self.put(f"class/powercap/{d}/name", name)
        self.put(f"class/powercap/{d}/energy_uj", str(uj))
        self.put(f"class/powercap/{d}/max_energy_range_uj", "1000000000")

    def test_rapl_all_domains(self):
        for d, n in (("intel-rapl:0", "package-0"), ("intel-rapl:0:0", "core"), ("intel-rapl:1", "psys"),
                     ("intel-rapl-mmio:0", "package-0")):
            self.rapl(d, n, 1000000)
        s = self.sensors()
        with patch.object(time, "monotonic", side_effect=[0, 0, 0, 2, 2, 2]):
            s.rapl_power()
            for d, uj in (("intel-rapl:0", 11000000), ("intel-rapl:0:0", 5000000), ("intel-rapl:1", 21000000),
                          ("intel-rapl-mmio:0", 99000000)):
                self.put(f"class/powercap/{d}/energy_uj", str(uj))
            rapl, package, _ = s.rapl_power()
        self.assertEqual(set(rapl), {"package-0", "package-0/core", "psys"})
        self.assertAlmostEqual(rapl["package-0"], 5.0)
        self.assertAlmostEqual(rapl["package-0/core"], 2.0)
        self.assertAlmostEqual(rapl["psys"], 10.0)
        self.assertAlmostEqual(package, 5.0)

    def test_battery_and_usbc_power(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        self.put("class/power_supply/BAT1/status", "Discharging")
        self.put("class/power_supply/BAT1/power_now", "15000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/online", "1")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/voltage_now", "20000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:001/current_now", "3000000")
        self.put("class/power_supply/ucsi-source-psy-USBC000:002/online", "0")
        self.put("class/power_supply/ucsi-source-psy-USBC000:002/voltage_now", "5000000")
        out = self.sensors().sample()["power"]
        self.assertEqual((out["battery_w"], out["battery_status"]), (15.0, "Discharging"))
        by = {u["name"][-3:]: u for u in out["usbc"]}
        self.assertEqual((by["001"]["online"], by["001"]["watts"]), (True, 60.0))
        self.assertEqual((by["002"]["online"], by["002"]["volts"], by["002"]["amps"], by["002"]["watts"]),
                         (False, 5.0, None, None))

    def test_battery_charge_limit(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        bat = lambda: self.sensors().sample()["battery"][0]["charge_limit"]
        self.assertIsNone(bat())
        self.put("bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode", "1")
        self.assertEqual(bat(), "on")
        self.put("class/power_supply/BAT1/charge_control_end_threshold", "80")
        self.assertEqual(bat(), "80%")

    def test_battery_health(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        bat = lambda: self.sensors().sample()["battery"][0]
        self.assertEqual([bat()[k] for k in ("cycles", "health_percent", "full_wh", "design_wh")], [None] * 4)
        self.put("class/power_supply/BAT1/cycle_count", "77")
        self.put("class/power_supply/BAT1/energy_full", "58880000")
        self.put("class/power_supply/BAT1/energy_full_design", "60000000")
        b = bat()
        self.assertEqual((b["cycles"], b["full_wh"], b["design_wh"]), (77, 58.88, 60.0))
        self.assertAlmostEqual(b["health_percent"], 98.13, 2)
        self.put("class/power_supply/BAT1/energy_full_design", "0")
        self.assertIsNone(bat()["health_percent"])
        self.put("class/power_supply/BAT1/charge_full", "4000000")
        self.put("class/power_supply/BAT1/charge_full_design", "5000000")
        self.assertEqual(bat()["health_percent"], 80.0)
        self.assertIsNone(bat()["full_wh"])

    def test_temperature_groups(self):
        def hw(n, name, temps):
            self.put(f"class/hwmon/hwmon{n}/name", name)
            for i, (label, v) in enumerate(temps, 1):
                self.put(f"class/hwmon/hwmon{n}/temp{i}_input", str(v * 1000))
                if label:
                    self.put(f"class/hwmon/hwmon{n}/temp{i}_label", label)
        hw(0, "spd5118", [(None, 30)])
        hw(1, "spd5118", [(None, 31)])
        hw(2, "nvme", [("Composite", 40), ("Sensor 1", 41)])
        hw(3, "coretemp", [("Package id 0", 60), ("Core 10", 55), ("Core 2", 54)])
        hw(4, "acpitz", [(None, 50)])
        hw(5, "iwlwifi_1", [(None, 45)])
        hw(6, "legion_hwmon", [("CPU Temperature", 48), ("IC Temperature", 0)])
        hw(7, "mystery", [(None, 99)])
        temps = self.sensors().sample()["temperatures"]
        self.assertEqual([(t["group"], t["label"]) for t in temps], [
            ("cpu", "Core 2"), ("cpu", "Core 10"), ("cpu", "Package id 0"), ("ec", "CPU Temperature"),
            ("ec", "IC Temperature"), ("acpi", "temp 1"),
            ("nvme", "Composite"), ("nvme", "Sensor 1"), ("ram", "DIMM 1"), ("ram", "DIMM 2"), ("wifi", "temp 1")])
        ec = {t["label"]: t["c"] for t in temps if t["group"] == "ec"}
        self.assertEqual(ec, {"CPU Temperature": 48.0, "IC Temperature": None})  # 0 means no sensor

    def test_voltages(self):
        self.put("class/power_supply/BAT1/type", "Battery")
        self.put("class/power_supply/BAT1/voltage_now", "16415000")
        self.put("class/hwmon/hwmon0/name", "BAT1")
        self.put("class/hwmon/hwmon0/in0_input", "16415")
        s = self.sensors()
        s.gpu = lambda: {"available": True, "devices": [{"name": "G", "temperature_c": 50}]}
        with patch.object(os, "geteuid", return_value=1000):
            m = s.sample()
        volts = {v["label"]: v["v"] for v in m["voltages"]}
        self.assertEqual(volts["Battery"], 16.415)
        self.assertEqual(volts["BAT1 in0"], 16.415)
        self.assertIsNone(volts["GPU core"])
        self.assertIsNone(volts["CPU 0 Vcore"])
        self.assertIn({"group": "gpu", "label": "G", "c": 50}, m["temperatures"])

    def test_cpu_vcore_msr(self):
        s = self.sensors()
        self.put("proc/cpuinfo", "processor : 0\nvendor_id : GenuineIntel\n")
        msr = self.root / "dev/cpu/0/msr"
        with patch.object(os, "geteuid", return_value=0):
            self.assertIsNone(s.cpu_vcore())  # no msr node
            msr.parent.mkdir(parents=True)
            msr.write_bytes(bytes(0x198) + (0x2000 << 32).to_bytes(8, "little"))
            self.assertEqual(s.cpu_vcore(), 1.0)
            for field, volts in ((2458, 2458 / 8192), (0x4000, 2.0)):  # lowest raw value >= 0.3 V, 2.0 V
                msr.write_bytes(bytes(0x198) + (field << 32).to_bytes(8, "little"))
                self.assertEqual(s.cpu_vcore(), volts)
            for field in (0, 0x0800, 0x5000, 0xFFFF):  # 0 V, 0.25 V, 2.5 V, 8 V
                msr.write_bytes(bytes(0x198) + (field << 32).to_bytes(8, "little"))
                self.assertIsNone(s.cpu_vcore())
            self.put("proc/cpuinfo", "vendor_id : AuthenticAMD\n")
            self.assertIsNone(s.cpu_vcore())
        self.put("proc/cpuinfo", "vendor_id : GenuineIntel\n")
        with patch.object(os, "geteuid", return_value=1000):
            self.assertIsNone(s.cpu_vcore())

    def test_cpu_throttle_deltas(self):
        def counts(core, pkg):
            for i in (0, 1):
                self.put(f"devices/system/cpu/cpu{i}/thermal_throttle/core_throttle_count", str(core))
                self.put(f"devices/system/cpu/cpu{i}/thermal_throttle/package_throttle_count", str(pkg))
                self.put(f"devices/system/cpu/cpu{i}/topology/physical_package_id", "0")
        s = self.sensors()
        counts(1, 5)
        first = s.cpu_throttle(0)
        self.assertEqual(first, {"core_events": None, "package_events": None, "active": None})
        counts(3, 6)
        second = s.cpu_throttle(1)
        self.assertEqual((second["core_events"], second["package_events"], second["active"]), (4, 1, True))
        self.assertIs(s.cpu_throttle(2)["active"], True)
        self.assertIs(s.cpu_throttle(2 + m_sensors.SLOW_INTERVAL)["active"], False)

    def test_sample_throttle_delta_and_window(self):
        def counts(n):
            for name in ("core", "package"):
                self.put(f"devices/system/cpu/cpu0/thermal_throttle/{name}_throttle_count", str(n))
        counts(2)
        s = self.sensors()
        self.assertIsNone(s.sample(now=0)["throttle"]["cpu"]["core_events"])
        counts(5)
        cpu = s.sample(now=1)["throttle"]["cpu"]
        self.assertEqual((cpu["core_events"], cpu["package_events"], cpu["active"]), (3, 3, True))
        cpu = s.sample(now=2)["throttle"]["cpu"]  # no new events, still inside the window
        self.assertEqual((cpu["core_events"], cpu["active"]), (3, True))
        cpu = s.sample(now=1 + m_sensors.SLOW_INTERVAL)["throttle"]["cpu"]
        self.assertEqual((cpu["core_events"], cpu["active"]), (0, False))

    def test_nvml_throttle_reasons(self):
        lib = FakeNVML()
        lib.nvmlDeviceGetCurrentClocksEventReasons = lambda h, box: (setattr(box._obj, "value", 0x1 | 0x4 | 0x40), 0)[1]
        self.assertEqual(m_sensors.NVML(lib).read()[0]["throttle"], ["sw_power_cap", "hw_thermal"])
        self.assertIsNone(m_sensors.NVML(FakeNVML()).read()[0]["throttle"])


if __name__ == "__main__":
    unittest.main()
