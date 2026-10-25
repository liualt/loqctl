# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from tests import helpers

from loqctl import backend as m_backend, capdata, common as m_common
from loqctl import hardware as m_hardware, ui as m_ui

FIXTURE = (REPO / "tests/fixtures/capdata-loq15irx9.txt").read_text()
MODES = ("low-power", "balanced", "performance", "max-power", "custom")
# Spec/user-confirmed real table for the LOQ 15IRX9: per key, values in MODES order.
EXPECTED = {
    "ppt_pl1_spl": (55, 60, 90, 95, 60), "ppt_pl2_sppt": (65, 119, 162, 162, 119),
    "ppt_cpu_cl": (35, 55, 55, 55, 55), "gpu_nv_ctgp": (55, 60, 90, 105, 60),
    "gpu_nv_ppab": (10, 20, 25, 10, 20), "gpu_nv_ac_offset": (20, 55, 55, 55, 55),
    "cpu_temp": (97,) * 5, "gpu_temp": (87,) * 5, "ppt_pl1_tau": (56,) * 5,
}


class ParseTests(unittest.TestCase):
    def test_parse_fixture(self):
        entries = capdata.parse_capdata(FIXTURE)
        self.assertGreater(len(entries), 0)
        self.assertEqual(entries[(1, 1, 255, 0)], {"default": 119, "step": 1, "min": 60, "max": 167})
        self.assertFalse(any(key[0] == 0 for key in entries))  # capdata00 rows carry no step/min/max

    def test_flags_and_garbage(self):
        row = ("  id:             0x01020200 [dev:  1, feat:  2, mode:  2, type:  0]\n"
               "  supported:      0x00000004 [ RW ]\n  default_value:  60\n  step:           1\n"
               "  min_value:      50\n  max_value:      100\n")
        self.assertEqual(capdata.parse_capdata(row), {})  # no V flag
        good = row.replace("[ RW ]", "[ RWV]")
        self.assertEqual(len(capdata.parse_capdata("junk\n== path\nx: y\n" + good + good)), 1)  # duplicates
        self.assertEqual(capdata.parse_capdata(""), {})

    def test_mode_table_exact_values(self):
        table = capdata.mode_table(capdata.parse_capdata(FIXTURE))
        self.assertEqual(tuple(table), MODES)
        for key, values in EXPECTED.items():
            for mode, value in zip(MODES, values):
                self.assertEqual(table[mode][key], value, (mode, key))
        self.assertTrue(capdata.valid_table(table))

    def test_read_mode_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(capdata.read_mode_table(tmp), (None, capdata.NOT_FOUND))
            guid = Path(tmp) / "kernel/debug/lenovo_wmi/G-19"
            guid.mkdir(parents=True)
            self.assertEqual(capdata.read_mode_table(tmp), (None, capdata.NOT_FOUND))  # no capdata01 rows
            (guid / "capdata").write_text(FIXTURE)
            table, reason = capdata.read_mode_table(tmp)
            self.assertIsNone(reason)
            self.assertEqual(table["performance"]["gpu_nv_ctgp"], 90)
            with patch.object(Path, "read_text", side_effect=PermissionError):
                self.assertEqual(capdata.read_mode_table(tmp), (None, "needs root"))
            with patch.object(os, "listdir", side_effect=PermissionError):
                self.assertEqual(capdata.read_mode_table(tmp), (None, "needs root"))


class Sim(unittest.TestCase):
    def setUp(self):
        temp = helpers.make_root(self)
        self.root = temp / "sys"
        self.cache = temp / "cache"
        patcher = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.cache)})
        patcher.start()
        self.addCleanup(patcher.stop)
        profile = self.root / "class/platform-profile/platform-profile-0"
        profile.mkdir(parents=True)
        self.profile = profile / "profile"
        (profile / "name").write_text("lenovo-wmi-gamezone")
        (profile / "choices").write_text(" ".join(MODES))
        self.set_mode("balanced")
        for key, current in (("ppt_pl1_spl", 50), ("ppt_pl2_sppt", 70), ("gpu_nv_ctgp", 40)):
            path = self.root / "class/firmware-attributes/lenovo-wmi-other-0/attributes" / key
            path.mkdir(parents=True)
            for name, value in {"current_value": current, "min_value": 20, "max_value": 170,
                                "default_value": 60, "scalar_increment": 1}.items():
                (path / name).write_text(str(value))
        dmi = self.root / "class/dmi/id"
        dmi.mkdir(parents=True)
        (dmi / "product_name").write_text("LOQ 15IRX9")
        (dmi / "bios_version").write_text("NECN44WW")

    def set_mode(self, mode):
        self.profile.write_text(mode)

    def add_table(self):
        guid = self.root / "kernel/debug/lenovo_wmi/G-19"
        guid.mkdir(parents=True)
        (guid / "capdata").write_text(FIXTURE)


class HardwareTests(Sim):
    def test_effective_value(self):
        self.add_table()
        hw = m_hardware.Hardware(self.root)
        attrs = hw.capabilities()["attributes"]
        self.assertEqual(attrs["ppt_pl1_spl"]["effective_value"], 60)  # balanced: table, not current 50
        self.assertEqual(attrs["ppt_pl2_sppt"]["effective_value"], 119)
        self.set_mode("custom")
        caps = hw.capabilities()
        self.assertEqual(caps["attributes"]["ppt_pl1_spl"]["effective_value"], 50)
        self.assertEqual(caps["mode_limits"]["performance"]["ppt_pl1_spl"], 90)
        self.assertIsNone(caps["mode_limits_error"])

    def test_retry_only_as_root(self):
        hw = m_hardware.Hardware(self.root)
        with patch.object(os, "geteuid", return_value=1000):
            hw.capabilities()
            self.add_table()
            self.assertIsNone(hw.capabilities()["mode_limits"])
        with patch.object(os, "geteuid", return_value=0):
            self.assertIsNotNone(hw.capabilities()["mode_limits"])

    def test_limits_report(self):
        self.add_table()
        report = capdata.limits_report(m_hardware.Hardware(self.root).capabilities())
        self.assertEqual(report["balanced"]["ppt_pl1_spl"], 60)
        self.assertEqual(report["custom"]["ppt_pl1_spl"], 50)
        self.assertNotIn("error", report)
        report = capdata.limits_report(m_hardware.Hardware(self.root / "nope").capabilities())
        self.assertIn("error", report)


def sudo_out(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class SudoCacheTests(Sim):
    def table(self):
        return capdata.mode_table(capdata.parse_capdata(FIXTURE))

    def backend(self):
        backend = m_backend.Backend.__new__(m_backend.Backend)
        backend.remote = False
        backend.hardware = m_hardware.Hardware(self.root)
        return backend

    def load(self, backend, out):
        with patch.object(os, "geteuid", return_value=1000), \
             patch("loqctl.capdata.shutil.which", return_value="/usr/bin/sudo"), \
             patch.object(subprocess, "run", return_value=out) as run:
            backend.load_mode_limits()
        return run

    def test_fetch_success_injects_and_writes_cache(self):
        backend = self.backend()
        run = self.load(backend, sudo_out(json.dumps(self.table())))
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["sudo", "-n", "--", sys.executable, "-I", m_common.ENTRY, "_capdata"])
        caps = backend.hardware.capabilities()
        self.assertEqual(caps["attributes"]["ppt_pl1_spl"]["effective_value"], 60)
        path = self.cache / "loqctl/mode-limits.json"
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        data = json.loads(path.read_text())
        self.assertEqual((data["version"], data["product"], data["bios"]), (1, "LOQ 15IRX9", "NECN44WW"))

    def test_cache_reused_without_sudo(self):
        capdata.save_cache(self.table(), self.root)
        backend = self.backend()
        run = self.load(backend, sudo_out())
        run.assert_not_called()
        self.assertEqual(backend.hardware.capabilities()["mode_limits"]["max-power"]["gpu_nv_ctgp"], 105)

    def test_cache_ignored_on_mismatch_or_bad_shape(self):
        capdata.save_cache(self.table(), self.root)
        (self.root / "class/dmi/id/bios_version").write_text("NECN45WW")
        self.assertIsNone(capdata.load_cache(self.root))
        (self.root / "class/dmi/id/bios_version").write_text("NECN44WW")
        self.assertIsNotNone(capdata.load_cache(self.root))
        path = self.cache / "loqctl/mode-limits.json"
        data = json.loads(path.read_text())
        bad_tables = (None, [], {}, {"balanced": []}, {"balanced": {"nope": 1}},
                      {"balanced": {"ppt_pl1_spl": "x"}}, {"balanced": {"ppt_pl1_spl": True}})
        for bad in ({"version": 2}, {"product": "other"}, *({"table": t} for t in bad_tables)):
            path.write_text(json.dumps({**data, **bad}))
            self.assertIsNone(capdata.load_cache(self.root), bad)
        path.write_text("not json")
        self.assertIsNone(capdata.load_cache(self.root))
        backend = self.backend()
        run = self.load(backend, sudo_out(json.dumps(self.table())))  # mismatch -> refetch
        run.assert_called_once()

    def test_expired_and_bad_json(self):
        backend = self.backend()
        self.load(backend, sudo_out("", "sudo: a password is required", 1))
        caps = backend.hardware.capabilities()
        self.assertIsNone(caps["mode_limits"])
        self.assertTrue(caps["mode_limits_error"].startswith("needs root"))
        self.assertFalse((self.cache / "loqctl/mode-limits.json").exists())
        for bad in ("not json", json.dumps({"balanced": {"zzz": 1}})):
            backend = self.backend()
            self.load(backend, sudo_out(bad))
            self.assertIn("unreadable", backend.hardware.capabilities()["mode_limits_error"])

    def test_no_sudo_or_root_or_remote(self):
        backend = self.backend()
        with patch.object(os, "geteuid", return_value=1000), \
             patch("loqctl.capdata.shutil.which", return_value=None), patch.object(subprocess, "run") as run:
            backend.load_mode_limits()
        run.assert_not_called()
        self.assertEqual(backend.hardware.capabilities()["mode_limits_error"], "needs root")
        for euid, remote in ((0, False), (1000, True)):
            backend = self.backend()
            backend.remote = remote
            with patch.object(os, "geteuid", return_value=euid), patch.object(subprocess, "run") as run:
                backend.load_mode_limits()
            run.assert_not_called()

class UiTests(Sim):
    def dash(self, h=30, w=100):
        dash = m_ui.Dashboard.__new__(m_ui.Dashboard)
        dash.theme = m_ui.Theme(False)
        dash.history = {k: m_ui.collections.deque(maxlen=240) for k in ("cpu", "gpu", "cpu_w", "gpu_w")}
        dash.pending, dash.fields, dash.invalid, dash.selected = {}, {}, set(), 1
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (h, w)
        dash.box = Mock()
        dash.fill = Mock()
        dash.calls = []
        dash.put = lambda y, x, text, color=0, bold=False, dim=False: dash.calls.append((str(text), bold, dim))
        return dash

    def text(self, dash):
        return "\n".join(c[0] for c in dash.calls)

    def test_stock_row_shows_table_value_and_footer(self):
        self.add_table()
        dash = self.dash()
        dash.draw_controls(m_hardware.Hardware(self.root).capabilities(), 0, 0, 20, 70)
        text = self.text(dash)
        self.assertIn("CPU sustained / PL1  60 W", text)
        self.assertIn("balanced limit from firmware table · custom default 60", text)

    def test_stock_row_without_table_is_na(self):
        dash = self.dash()
        state = m_hardware.Hardware(self.root).capabilities()
        self.assertIsNone(state["attributes"]["ppt_pl1_spl"]["effective_value"])  # not sysfs current 50
        self.assertIsNone(state["mode_limits"])
        self.assertEqual(state["mode_limits_error"], capdata.NOT_FOUND)
        state["mode_limits_error"] = "needs root"
        dash.draw_controls(state, 0, 0, 20, 70)
        text = self.text(dash)
        self.assertIn("CPU sustained / PL1  N/A", text)
        self.assertIn("Stock-mode limits need root: press s or run sudo loqctl serve", text)

    def test_limits_overlay(self):
        self.add_table()
        dash = self.dash()
        state = m_hardware.Hardware(self.root).capabilities()
        dash.draw_limits(state)
        text = self.text(dash)
        for mode in MODES:
            self.assertIn(mode, text)
        row = [c for c in dash.calls if c[0].strip() in ("60", "119", "162", "65")]
        self.assertTrue(row)
        bold = [c[0].strip() for c in dash.calls if c[1] and c[0].strip().isdigit()]
        self.assertIn("60", bold)  # active balanced column is bold
        dash.calls.clear()
        state["mode_limits"] = None
        dash.draw_limits(state)
        self.assertIn("N/A", self.text(dash))

    def test_chart_markers_use_effective_value(self):
        self.add_table()
        dash = self.dash(42, 100)
        dash.view = "summary"
        marks = []
        dash.chart_rows = lambda y, x, w, h, key, ceiling, color, markers=(): marks.append((key, [v for v, _ in markers]))
        dash.history["cpu_w"].extend([20, 45])
        dash.history["gpu_w"].extend([30, 80])
        metrics = {"cpu": {"cores": {}}, "gpu": {"devices": [{"power_limit_w": 140}]}}
        dash.draw_telemetry(m_hardware.Hardware(self.root).capabilities(), metrics, 1, 40, 42, 60)
        self.assertIn(("cpu_w", [60, 119]), marks)  # balanced PL1/PL2 from the table, not current 50/70
        self.assertIn(("gpu_w", [60, 140]), marks)


if __name__ == "__main__":
    unittest.main()
