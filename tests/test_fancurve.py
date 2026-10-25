# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
import copy
import errno
import importlib.util
import json
import os
import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import curses
from loqctl import api as m_api, backend as m_backend, cli as m_cli, common as m_common
from loqctl import curvestore as m_store, fancurve as m_fan, hardware as m_hardware, sensors as m_sensors, ui as m_ui
from loqctl import ui_fans as m_panel
from tests import helpers

CURVE = [
    dict(cpu_up=40, cpu_down=0, gpu_up=40, gpu_down=0, ic_up=40, ic_down=0, fan1=0, fan2=0, accel=1, decel=1),
    dict(cpu_up=60, cpu_down=35, gpu_up=60, gpu_down=35, ic_up=60, ic_down=35, fan1=80, fan2=80, accel=1, decel=1),
    dict(cpu_up=90, cpu_down=55, gpu_up=90, gpu_down=55, ic_up=90, ic_down=55, fan1=180, fan2=180, accel=1, decel=1),
]


def stock_point(cpu, gpu, ic, fan1, fan2):
    return dict(cpu_up=cpu[0], cpu_down=cpu[1], gpu_up=gpu[0], gpu_down=gpu[1], ic_up=ic[0], ic_down=ic[1],
                fan1=fan1, fan2=fan2)


# Read from a LOQ 15IRX9 (BIOS NECN44WW, LenovoLegionLinux 0.1.3) in balanced mode, unit rpm,
# fan1_max 10000: fan 1 0..3700 RPM, fan 2 0..4000 RPM. Down temperatures sit above the
# previous point's up temperature, and there are no accel/decel files.
NECN_STOCK = [
    stock_point((69, 0), (69, 0), (42, 0), 0, 0),
    stock_point((71, 70), (71, 70), (63, 40), 63, 71),
    stock_point((75, 73), (75, 73), (65, 59), 86, 94),
    *[stock_point((75, 73), (75, 73), (ic, 60), 94, 102) for ic in (68, 76, 77, 87, 97)],
    stock_point((99, 73), (99, 73), (105, 89), 94, 102),
    stock_point((100, 94), (100, 94), (120, 90), 94, 102),
]
RPM = m_fan.Scale("rpm", 10000)


def make_hwmon(root, curve=CURVE, unit="percent", name="legion_hwmon", max_rpm=None):
    hwmon = Path(root) / "class/hwmon/hwmon5"
    (hwmon / "device").mkdir(parents=True)
    (hwmon / "name").write_text(name)
    if unit:
        (hwmon / "device/fancurve_speed_unit").write_text(unit)
    if max_rpm:
        (hwmon / "fan1_max").write_text(str(max_rpm))
    (hwmon / "fan1_input").write_text("2100")
    (hwmon / "fan1_target").write_text("0")
    for n, point in enumerate(curve, 1):
        for field, value in point.items():
            (hwmon / m_fan.FILE_MAP[field].format(n=n)).write_text(str(value))
    return hwmon


def edit(curve, changes):
    """Copy of curve with {(point, field): value} changes."""
    out = copy.deepcopy(curve)
    for (i, field), value in changes.items():
        out[i][field] = value
    return out


class ImmediateThread:
    """threading.Thread stand-in that runs the target on start()."""

    def __init__(self, target, daemon=None):
        self.target = target

    def start(self):
        self.target()


class FanCurveTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)
        self.hwmon = make_hwmon(self.root)
        self.pct = m_fan.Scale("percent")

    def fields(self, errors):
        return {(i, f) for i, f, _ in errors}

    def test_discovery(self):
        self.assertEqual(m_fan.find_legion_hwmon(self.root), self.hwmon)
        self.assertIsNone(m_fan.find_legion_hwmon(helpers.make_root(self)))
        (self.hwmon / "name").write_text("k10temp")
        self.assertIsNone(m_fan.find_legion_hwmon(self.root))

    def test_read_curve_with_missing_files(self):
        (self.hwmon / "pwm3_auto_point2_temp").unlink()
        (self.hwmon / "pwm1_auto_point1_accel").unlink()
        points = m_fan.read_curve(self.hwmon)
        self.assertEqual(len(points), 3)
        self.assertIsNone(points[1]["ic_up"])
        self.assertIsNone(points[0]["accel"])
        self.assertEqual(points[2]["fan2"], 180)
        self.assertEqual(m_fan.speed_unit(self.hwmon), "percent")

    def test_stock_loq_curve_is_valid(self):
        self.assertEqual(m_fan.validate_curve(NECN_STOCK, RPM), [])
        self.assertEqual([RPM.show(p["fan1"]) for p in NECN_STOCK],
                         [0, 2500, 3400, 3700, 3700, 3700, 3700, 3700, 3700, 3700])
        self.assertEqual(RPM.show(NECN_STOCK[-1]["fan2"]), 4000)

    def test_validate_ok_and_each_rule(self):
        self.assertEqual(m_fan.validate_curve(copy.deepcopy(CURVE)), [])
        cases = {
            "temps": (edit(CURVE, {(1, "cpu_up"): 30}), (1, "cpu_up")),
            "speeds": (edit(CURVE, {(2, "fan1"): 70}), (2, "fan1")),
            "down_above_up": (edit(CURVE, {(1, "gpu_down"): 61}), (1, "gpu_down")),
            "range_temp": (edit(CURVE, {(2, "cpu_up"): 128}), (2, "cpu_up")),
            "range_pwm": (edit(CURVE, {(2, "fan2"): 256}), (2, "fan2")),
            "type": (edit(CURVE, {(0, "cpu_up"): "40"}), (0, "cpu_up")),
            "bool": (edit(CURVE, {(0, "fan1"): True}), (0, "fan1")),
            "last_point": (edit(CURVE, {(2, "fan1"): 140}), (2, "fan1")),
        }
        for name, (curve, cell) in cases.items():
            self.assertIn(cell, self.fields(m_fan.validate_curve(curve)), name)
        # Down at or above the previous up temperature is what the firmware ships.
        self.assertEqual(m_fan.validate_curve(edit(CURVE, {(1, "ic_down"): 45})), [])

    def test_floors_per_unit(self):
        hot = edit(CURVE, {(0, "cpu_up"): 80, (1, "cpu_up"): 85, (2, "cpu_up"): 90,
                           (1, "fan1"): 0, (1, "fan2"): 0, (2, "fan1"): 160, (2, "fan2"): 160})
        self.assertEqual(self.fields(m_fan.validate_curve(hot)), {(1, "fan1"), (1, "fan2")})
        hot[1]["fan1"] = hot[1]["fan2"] = 102  # 40 %
        self.assertEqual(m_fan.validate_curve(hot), [])
        # rpm: 2000 RPM after a hot step, 3000 RPM on the last point.
        low = edit(NECN_STOCK, {(9, "fan1"): RPM.pwm(2900), (9, "fan2"): RPM.pwm(3000)})
        floors = [(i, f) for i, f, m in m_fan.validate_curve(low, RPM) if "needs at least 3000 RPM" in m]
        self.assertEqual(floors, [(9, "fan1")])

    def test_scale_matches_module_rounding(self):
        # The module stores ceil(pwm * max_rpm / 25500) and reads back floor(byte * 25500 / max_rpm).
        for max_rpm in (10000, 5400, 4400):
            scale = m_fan.Scale("rpm", max_rpm)
            for rpm in range(0, max_rpm + 1, 100):
                pwm = scale.pwm(rpm)
                stored = -(-pwm * max_rpm // 25500)
                self.assertEqual(stored * 100, rpm, (max_rpm, rpm))
                self.assertEqual(stored * 25500 // max_rpm, pwm)  # readback is the same pwm
        level = m_fan.Scale("level")
        self.assertEqual([level.show(level.pwm(n)) for n in range(11)], list(range(11)))
        self.assertTrue(self.pct.same(128, 127))  # percent drifts by one on readback
        self.assertFalse(RPM.same(94, 102))
        self.assertEqual(m_fan.Scale("rpm", 0).max_rpm, 10000)
        self.assertEqual((RPM.label, RPM.text(94), self.pct.text(128), level.text(128)), ("RPM", "3700 RPM", "50%", "5 level"))

    def test_speed_steps_come_from_the_module(self):
        # Each step is the next speed the module stores.
        self.assertEqual(RPM.speeds(), list(range(0, 10100, 100)))
        self.assertEqual(m_fan.Scale("rpm", 5450).speeds()[-2:], [5300, 5400])  # top is fan1_max rounded down
        self.assertEqual(m_fan.Scale("level").speeds(), list(range(11)))
        self.assertEqual((RPM.next_speed(94, 1), RPM.next_speed(94, -5), RPM.next_speed(255, 1)), (3800, 3200, 10000))
        self.assertEqual([self.pct.next_speed(self.pct.pwm(50), n) for n in (1, 2, -1)], [51, 52, 49])
        self.assertEqual((self.pct.next_speed(0, -1), self.pct.next_speed(255, 1)), (0, 100))
        self.assertEqual((self.pct.next_speed(self.pct.pwm(99), 1), self.pct.next_speed(self.pct.pwm(1), -1)), (100, 0))

    def test_write_only_changed_files_in_order(self):
        writes = []
        real = m_fan._write_file
        with patch.object(m_fan, "_write_file", side_effect=lambda p, v: (writes.append(p.name), real(p, v))):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90, (1, "cpu_up"): 61,
                                                                    (2, "gpu_up"): 91}), self.pct)
        self.assertEqual(writes, ["pwm1_auto_point2_temp", "pwm2_auto_point3_temp", "pwm1_auto_point2_pwm"])
        self.assertEqual({r["status"] for r in results}, {"APPLIED"})
        self.assertEqual(m_fan.read_curve(self.hwmon)[1]["fan1"], 90)
        self.assertEqual(m_fan.write_curve(self.hwmon, m_fan.read_curve(self.hwmon), self.pct), [])

    def test_one_percent_change_is_written(self):
        results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 82}), self.pct)  # 31 % to 32 %
        self.assertEqual([(r["field"], r["status"]) for r in results], [("fan1", "APPLIED")])
        self.assertEqual(m_fan.read_curve(self.hwmon)[1]["fan1"], 82)
        self.assertEqual(m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 81}), self.pct), [])  # still 32 %

    def test_one_percent_write_the_module_ignores_is_not_applied(self):
        with patch.object(m_fan, "_write_file"):  # the module accepts the write and keeps the old value
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 82}), self.pct)
        self.assertEqual([r["status"] for r in results], ["FAILED"])
        self.assertEqual(m_fan.read_curve(self.hwmon)[1]["fan1"], 80)

    def test_never_touches_fan_target_or_other_files(self):
        self.assertTrue(all("target" not in p and "points_size" not in p for p in m_fan.FILE_MAP.values()))
        with patch.object(m_fan.os, "open", wraps=os.open) as opened:
            m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan2"): 100}), self.pct)
        self.assertEqual([Path(c.args[0]).name for c in opened.call_args_list], ["pwm2_auto_point2_pwm"])
        self.assertEqual((self.hwmon / "fan1_target").read_text(), "0")

    def test_invalid_target_refused_with_no_writes(self):
        with patch.object(m_fan, "_write_file") as write:
            for target in (edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}), CURVE[:2], edit(CURVE, {(0, "bogus"): 1}),
                           edit(CURVE, {(0, "cpu_up"): None})):
                with self.assertRaises(m_common.ControlError):
                    m_fan.write_curve(self.hwmon, target, self.pct)
        write.assert_not_called()

    def test_null_ignored_for_missing_or_unreadable_file(self):
        (self.hwmon / "pwm3_auto_point2_temp").unlink()
        (self.hwmon / "pwm1_auto_point1_temp").write_text("junk\n")
        with patch.object(m_fan, "_write_file") as write:
            target = edit(CURVE, {(1, "ic_up"): None, (0, "cpu_up"): None})
            self.assertEqual(m_fan.write_curve(self.hwmon, target, self.pct), [])
            with self.assertRaisesRegex(m_common.ControlError, "Cannot read point 1"):
                m_fan.write_curve(self.hwmon, edit(CURVE, {(0, "cpu_up"): 50}), self.pct)
        write.assert_not_called()

    def test_symlink_and_foreign_path_rejected(self):
        outside = self.root / "outside"
        outside.write_text("1")
        link = self.hwmon / "pwm1_auto_point2_pwm"
        link.unlink()
        link.symlink_to(outside)
        results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90}), self.pct)
        self.assertEqual(results[0]["status"], "FAILED")
        self.assertEqual(outside.read_text(), "1")
        with self.assertRaises(m_common.ControlError):
            m_fan._path(self.hwmon, "fan1_target", 1)

    def test_readback_verification_per_unit(self):
        with patch.object(m_fan, "_write_file"):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan1"): 90}), self.pct)
        self.assertEqual((results[0]["status"], results[0]["current"]), ("FAILED", 80))
        self.assertIn("firmware reports", results[0]["error"])

        def module(path, value):  # the rpm unit as legion_laptop stores and reads it back
            stored = -(-int(value) * 10000 // 25500)
            path.write_text(str(stored * 25500 // 10000))
        rpm_hwmon = make_hwmon(self.root / "rpm", NECN_STOCK, "rpm", max_rpm=10000)
        target = edit(NECN_STOCK, {(9, "fan1"): RPM.pwm(4500), (9, "fan2"): RPM.pwm(4800)})
        with patch.object(m_fan, "_write_file", side_effect=module):
            results = m_fan.write_curve(rpm_hwmon, target, RPM)
        self.assertEqual([r["status"] for r in results], ["APPLIED", "APPLIED"])

    def test_stops_at_first_failure(self):
        calls = []

        def failing(path, value):
            calls.append(path.name)
            raise m_common.ControlError("boom")
        with patch.object(m_fan, "_write_file", side_effect=failing):
            results = m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "cpu_up"): 61, (1, "fan1"): 90}), self.pct)
        self.assertEqual([r["status"] for r in results], ["FAILED", "NOT SENT"])
        self.assertEqual(len(calls), 1)

    def test_errno_messages(self):
        path = self.hwmon / "pwm1_auto_point2_pwm"
        for code, text in ((errno.EPERM, "refuses unvalidated"), (errno.EBUSY, "locked"),
                           (errno.EOPNOTSUPP, "rejected")):
            with patch.object(m_fan.os, "open", side_effect=OSError(code, "x")):
                with self.assertRaisesRegex(m_common.ControlError, text):
                    m_fan._write_file(path, 1)
        with self.assertRaises(m_common.ControlError):
            m_fan._write_file(self.hwmon / "missing", 1)  # never created
        self.assertFalse((self.hwmon / "missing").exists())


class CheckScriptTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)
        self.hwmon = make_hwmon(self.root)
        path = Path(__file__).resolve().parent.parent / "scripts/check_fancurve_percent.py"
        spec = importlib.util.spec_from_file_location("check_fancurve_percent", path)
        self.script = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.script)
        self.lines = []
        self.pct = m_fan.Scale("percent")

    def test_report_lists_each_pwm_with_its_percent(self):
        self.script.report(self.hwmon, self.pct, self.lines.append)
        self.assertIn("pwm1_auto_point2_pwm: 80 -> 31%", self.lines)
        self.assertIn("pwm2_auto_point3_pwm: 180 -> 71%", self.lines)

    def test_write_check_tries_both_directions_and_restores(self):
        (self.hwmon / "pwm1_auto_point1_pwm").write_text("40")
        self.script.write_check(self.hwmon, self.pct, self.lines.append)
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["fan1"], 40)
        self.assertEqual(sum(line.startswith("restored:") for line in self.lines), 2)
        self.assertTrue(any(line.startswith("+1 %: wrote") for line in self.lines))
        self.assertTrue(any(line.startswith("-1 %: wrote") for line in self.lines))

    def test_failed_restore_stops_the_check(self):
        (self.hwmon / "pwm1_auto_point1_pwm").write_text("40")
        real, calls = m_fan._write_file, []

        def write_once(path, value):  # the first write lands, the restore is refused
            calls.append(value)
            if len(calls) > 1:
                raise m_common.ControlError("busy")
            real(path, value)

        with patch.object(m_fan, "_write_file", side_effect=write_once):
            self.script.write_check(self.hwmon, self.pct, self.lines.append)
        self.assertTrue(any("RESTORE FAILED" in line for line in self.lines))
        self.assertFalse(any(line.startswith("-1 %") for line in self.lines))

    def test_log_goes_to_the_sudo_users_home(self):
        home = self.root
        with patch.dict(os.environ, {"SUDO_UID": "1000"}), \
                patch.object(self.script.pwd, "getpwuid", return_value=SimpleNamespace(pw_dir=str(home))):
            self.assertEqual(self.script.log_path(), home / "loqctl-fancurve-check.log")


class MoveTests(unittest.TestCase):
    def test_speeds_are_computed_once(self):
        scale = m_fan.Scale("percent")
        self.assertIs(scale.speeds(), scale.speeds())

    def test_speed_sets_both_fans_and_keeps_order(self):
        points, moved = m_panel.move_speed(NECN_STOCK, 1, 1, RPM)
        self.assertTrue(moved)
        self.assertEqual((RPM.show(points[1]["fan1"]), RPM.show(points[1]["fan2"])), (2600, 2600))
        self.assertEqual(points[1]["cpu_up"], NECN_STOCK[1]["cpu_up"])  # temperatures never move
        points, _ = m_panel.move_speed(NECN_STOCK, 1, 50, RPM)
        self.assertEqual((RPM.show(points[1]["fan1"]), RPM.show(points[1]["fan2"])), (3400, 3700))  # each fan held below its own next point
        points, moved = m_panel.move_speed(NECN_STOCK, 0, -1, RPM)
        self.assertFalse(moved)
        points, _ = m_panel.move_speed(NECN_STOCK, 9, 500, RPM)
        self.assertEqual((RPM.show(points[9]["fan1"]), RPM.show(points[9]["fan2"])), (10000, 10000))
        self.assertEqual(m_fan.validate_curve(points, RPM), [])

    def test_speed_stays_above_the_floor(self):
        low = edit(NECN_STOCK, {(8, "fan1"): 0, (8, "fan2"): 0})
        points, _ = m_panel.move_speed(low, 9, -500, RPM)
        self.assertEqual((RPM.show(points[9]["fan1"]), RPM.show(points[9]["fan2"])), (3000, 3000))

    def test_move_clamped_to_its_neighbour_is_held(self):
        close = edit(CURVE, {(2, "fan1"): 80, (2, "fan2"): 80})  # point 3 equals point 2
        self.assertEqual(m_panel.move_speed(close, 1, 1, m_fan.Scale("percent")), (close, False))


class FanPanelFixture(helpers.SysfsCase):
    def setUp(self):
        super().setUp()
        self.hwmon = make_hwmon(self.root)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.home = self.root / "home"
        self.active = self.home / ".config/loqctl/active.json"
        pw = patch.object(m_store.pwd, "getpwuid",
                          side_effect=lambda uid: Mock(pw_dir=str(self.home if uid == os.getuid() else self.root / f"home{uid}")))
        pw.start()
        self.addCleanup(pw.stop)
        thread = patch.object(m_panel, "threading", SimpleNamespace(Thread=ImmediateThread))
        thread.start()
        self.addCleanup(thread.stop)

    def set_mode(self, mode):
        self.hw.set_mode(mode)

    def dash(self, mode="custom"):
        dash = self.dashboard()
        dash.notice = ""
        dash.panel = "main"
        dash.show_limits = dash.show_results = dash.help = False
        dash.view = "summary"
        dash.running = True
        dash.keyboard = Mock()
        dash.backend.fancurve = self.hw.fancurve
        dash.backend.write_fancurve = self.hw.write_fancurve
        dash.theme = m_ui.Theme(False)
        dash.s = Mock()
        dash.s.getmaxyx.return_value = (30, 110)
        dash.puts = []
        dash.put = lambda y, x, text, *a, **k: dash.puts.append(str(text))
        dash.box = Mock()
        dash.fan_init()
        self.set_mode(mode)
        return dash

    def keys(self, dash, state, *keys):
        for key in keys:
            dash.handle(ord(key) if isinstance(key, str) else key, state)


class FanCurveSurfaceTests(FanPanelFixture):
    def test_hardware_fancurve_reads_firmware_and_gate(self):
        data = self.hw.fancurve()
        self.assertTrue(data["available"])
        self.assertEqual((data["unit"], data["max_rpm"], data["rpm"][0], data["rpm"][1]),
                         ("percent", 10000, 2100.0, None))
        self.assertEqual(len(data["points"]), 3)
        self.assertEqual(data["mode"], "custom")
        (self.hwmon / m_fan.FILE_MAP["fan1"].format(n=1)).write_text("33")  # a resume or another program
        self.assertEqual(self.hw.fancurve()["points"][0]["fan1"], 33)
        self.set_mode("balanced")
        with self.assertRaisesRegex(m_common.ControlError, "locked in balanced mode"):
            self.hw.write_fancurve(CURVE)
        self.set_mode("custom")
        self.hw.write_fancurve(edit(CURVE, {(1, "fan1"): 90}))
        self.assertEqual(self.hw.fancurve()["points"][1]["fan1"], 90)
        self.assertFalse(m_hardware.Hardware(self.root / "nowhere").fancurve()["available"])

    def test_cli_write_and_fan_json(self):
        out = Mock()
        payload = json.dumps(edit(CURVE, {(1, "fan1"): 90}))
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("builtins.print", out):
            self.assertEqual(m_cli.main(["_fancurve", payload]), 0)
        self.assertEqual(json.loads(out.call_args.args[0])[0]["status"], "APPLIED")
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("sys.stderr"):
            self.assertEqual(m_cli.main(["_fancurve", json.dumps(CURVE[:1])]), 1)
        backend = m_backend.Backend(str(self.root / "none.sock"))
        for root, expected in ((self.root, True), (self.root / "nowhere", False)):
            backend.hardware = m_hardware.Hardware(root)
            with patch.object(m_cli, "Backend", return_value=backend), patch("builtins.print", out):
                self.assertEqual(m_cli.main(["fan"]), 0)
            self.assertEqual(json.loads(out.call_args.args[0])["available"], expected)
        self.assertIn("reason", json.loads(out.call_args.args[0]))

    def test_api_get_and_post(self):
        sampler = type("Sampler", (), {"latest": {}})()
        path = str(self.root / "t.sock")
        server = m_api.APIServer(path, self.hw, sampler, os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(len(m_api.request(path, "GET", "/v1/fancurve")["points"]), 3)
            results = m_api.request(path, "POST", "/v1/fancurve", {"points": edit(CURVE, {(1, "fan1"): 90})})
            self.assertEqual(results[0]["status"], "APPLIED")
            for payload in ({"points": edit(CURVE, {(1, "cpu_up"): 30})}, {"points": [], "x": 1}):
                with self.assertRaises(m_common.ControlError):
                    m_api.request(path, "POST", "/v1/fancurve", payload)
            self.set_mode("balanced")
            with self.assertRaisesRegex(m_common.ControlError, "locked"):
                m_api.request(path, "POST", "/v1/fancurve", {"points": CURVE})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_open_loads_in_background(self):
        dash = self.dash()
        started = []
        with patch.object(m_panel, "threading", SimpleNamespace(
                Thread=lambda target, daemon=None: Mock(start=lambda: started.append(target)))):
            self.keys(dash, {"mode": "custom"}, "f")
        self.assertTrue(dash.fan_loading)
        dash.draw_fans({"mode": "custom"}, {}, 1, 0, 25, 100)
        self.assertIn("Reading the fan curve", "\n".join(dash.puts))
        started[0]()
        dash.draw_fans({"mode": "custom"}, {}, 1, 0, 25, 100)
        self.assertTrue(dash.fan["available"])

    def test_fresh_read_beats_inflight_load(self):
        dash = self.dash()
        started = []
        with patch.object(m_panel, "threading", SimpleNamespace(
                Thread=lambda target, daemon=None: Mock(start=lambda: started.append(target)))):
            dash.open_fans()
        old = {"available": True, "mode": "custom", "points": CURVE[:2]}
        new = {"available": True, "mode": "custom", "points": CURVE}
        self.assertTrue(dash.fan_loading)  # the background load is still running
        with patch.object(dash.backend, "fancurve", return_value=new):
            dash.fan_request(wait=True)
        with patch.object(dash.backend, "fancurve", return_value=old):
            started[0]()  # the old load finishes late and must be dropped
        dash.fan_take()
        self.assertEqual(dash.fan["points"], CURVE)
        self.assertFalse(dash.fan_loading)

    def test_fresh_read_discards_untaken_result(self):
        dash = self.dash()
        dash.fan_init()
        dash.fan_result = {"available": True, "mode": "custom", "points": CURVE[:2]}
        new = {"available": True, "mode": "custom", "points": CURVE}
        with patch.object(dash.backend, "fancurve", return_value=new):
            dash.fan_request(wait=True)
        self.assertEqual(dash.fan["points"], CURVE)

    def test_stale_load_resuming_after_fresh_read_is_dropped(self):
        dash = self.dash()
        dash.fan_init()
        new = {"available": True, "mode": "custom", "points": CURVE}
        old = {"available": True, "mode": "custom", "points": CURVE[:2]}
        started = []

        def stale_read():
            # the UI thread applies and reads fresh while this load is mid-read
            with patch.object(dash.backend, "fancurve", return_value=new):
                dash.fan_request(wait=True)
            return old
        with patch.object(m_panel, "threading", SimpleNamespace(
                Thread=lambda target, daemon=None: Mock(start=lambda: started.append(target)))), patch.object(dash.backend, "fancurve", side_effect=stale_read):
            dash.fan_request()
            started[0]()
        dash.fan_take()
        self.assertEqual(dash.fan["points"], CURVE)

    def test_reopen_shows_outside_curve_change(self):
        dash = self.dash()
        self.keys(dash, {"mode": "custom"}, "f")
        dash.fan_take()
        self.assertEqual(dash.fan_points()[0]["fan1"], 0)
        dash.panel = "main"
        (self.hwmon / m_fan.FILE_MAP["fan1"].format(n=1)).write_text("33")
        self.keys(dash, {"mode": "custom"}, "f")
        dash.fan_take()
        self.assertEqual(dash.fan_points()[0]["fan1"], 33)

    def test_select_move_and_apply(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", 9)
        self.assertEqual(dash.fan_sel, 2)
        self.keys(dash, state, "[", curses.KEY_BTAB, curses.KEY_BTAB)
        self.assertEqual(dash.fan_sel, 2)
        self.keys(dash, state, "[", curses.KEY_LEFT, curses.KEY_LEFT, curses.KEY_LEFT)  # arrows stop at point 1
        self.assertEqual(dash.fan_sel, 0)
        self.keys(dash, state, curses.KEY_RIGHT, curses.KEY_UP)  # point 2; temperatures are fixed
        self.assertEqual((dash.fan_edit[1]["cpu_up"], dash.fan_edit[1]["gpu_down"]), (60, 35))
        self.assertEqual((dash.fan_edit[1]["fan1"], dash.fan_edit[1]["fan2"]), (82, 82))  # 31 % + 1 % = 32 %
        self.keys(dash, state, 10)
        point = m_fan.read_curve(self.hwmon)[1]
        self.assertEqual((point["cpu_up"], point["fan1"], point["fan2"]), (60, 82, 82))
        self.assertIsNone(dash.fan_edit)
        self.assertIn("Applied and verified", dash.notice)

    def test_held_point_and_discard(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", curses.KEY_DOWN)  # point 1 is already at 0
        self.assertIn("at its 0% floor", dash.notice)
        self.keys(dash, state, curses.KEY_UP, "r")
        self.assertIsNone(dash.fan_edit)

    def test_held_reason_without_fan1_and_at_neighbour(self):
        dash = self.dash()
        dash.fan = {"available": True, "points": edit(CURVE, {(1, "fan1"): None, (1, "fan2"): 50, (2, "fan2"): 50})}
        self.assertIn("point 3 has the same speed", dash.fan_held_reason(dash.fan_points(), 1, 1))

    def test_fans_key_hint_keeps_esc_and_quit_at_80_columns(self):
        dash = self.dash()
        dash.panel = "fans"
        dash.s.getmaxyx.return_value = (30, 80)
        dash.kb_seen, dash.paused, dash.sudo_expired = dash.keyboard.last_seq, False, False
        calls = []
        dash.put = lambda y, x, text, *a, **k: calls.append(str(text))
        dash.draw_fans = dash.box = Mock()
        dash.draw({"mode": "custom"})
        hint = next(c for c in calls if "Esc back" in c)
        self.assertIn("q quit", hint[:77])

    def test_invalid_staged_curve_is_not_written(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f")
        dash.fan_edit = edit(CURVE, {(2, "fan1"): 100})  # below the last-point floor
        self.keys(dash, state, 10)
        self.assertEqual(dash.fan_sel, 2)
        self.assertIn("needs at least", dash.notice)
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 180)

    def test_sudo_path_for_non_root_local(self):
        dash = self.dash()
        dash.backend.remote = False
        dash.backend.refresh = lambda: None
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", curses.KEY_UP)
        with patch.object(os, "geteuid", return_value=1000), \
                patch.object(m_ui.Dashboard, "run_sudo_fancurve", return_value=[{"status": "APPLIED"}]) as run:
            self.keys(dash, state, 10)
        self.assertEqual(run.call_args.args[0][1]["fan1"], 82)
        with patch.object(m_ui, "sudo_run", side_effect=m_common.SudoExpired("x")):
            with self.assertRaisesRegex(m_common.ControlError, "expired"):
                dash.sudo_call(["_fancurve", "[]"])
        self.assertTrue(dash.sudo_expired)

    def test_panel_draws_without_module(self):
        dash = self.dash()
        dash.fan = {"available": False, "reason": "legion_laptop hwmon not found"}
        dash.draw_fans({"mode": "custom"}, {"fans": [{"rpm": 1800.0}, {"rpm": None}]}, 1, 0, 25, 100)
        text = "\n".join(dash.puts)
        self.assertIn("LenovoLegionLinux legion_laptop", text)
        self.assertIn("F1 1800 RPM", dash.box.call_args.args[4])
        self.assertIn("F2 N/A", dash.box.call_args.args[4])
        self.assertIn("enable_platformprofile=0", text)
        dash.draw_fans({"mode": "custom"}, {}, 1, 0, 25, 100)

    def test_graph_draws_rpm_curve_with_cursor(self):
        rpm_root = self.root / "rpm"
        make_hwmon(rpm_root, NECN_STOCK, "rpm", max_rpm=10000)
        dash = self.dash()
        dash.backend.fancurve = m_hardware.Hardware(rpm_root).fancurve
        self.keys(dash, {"mode": "custom"}, "f", "]", "]", curses.KEY_UP)
        self.assertTrue(dash.fan["available"])
        cells = {}
        dash.put = lambda y, x, text, color=None, *a, **k: cells.update({(y, x): (str(text), color)})
        dash.draw_fans({"mode": "custom"}, {"cpu": {"temperature_c": 67.4}, "fans": [{"rpm": 2000.0}]}, 1, 0, 30, 110)
        text = {t for t, _ in cells.values()}
        self.assertIn("Point 3/10 · 75°C · 3500 RPM (was 3400 RPM) (fan 2 was 3700 RPM)", text)  # fan 2 is compared too
        self.assertIn("▲ CPU 67°C", text)
        self.assertTrue({"70°C", " 4000", "RPM"} <= text)
        self.assertFalse(any("GPU" in t or "Curves" in t for t in text))  # details and the curve list stay hidden
        axis = next(y for (y, x), (t, _) in cells.items() if t.startswith("└"))
        self.assertTrue(any(y == axis and t == "▲" for (y, x), (t, _) in cells.items()))  # CPU tick on the axis
        marks = sorted(x for (y, x), (t, c) in cells.items() if t == "●")
        selected = [k for k, (t, c) in cells.items() if t == "●" and c == dash.theme.WARN]
        self.assertEqual(len(selected), 1)
        lines = {x for (y, x), (t, _) in cells.items() if "\u2800" < t <= "\u28ff"}
        self.assertTrue(set(range(marks[0] + 1, marks[-1])) <= lines | set(marks))  # segments leave no gaps
        self.keys(dash, {"mode": "custom"}, "d")
        dash.draw_fans({"mode": "custom"}, {}, 1, 0, 30, 110)
        text = {t for t, _ in cells.values()}
        self.assertIn("Point 3/10 · CPU 75/73°C · GPU 75/73°C · IC 65/59°C · Fan 1 3500 RPM · Fan 2 3500 RPM", text)
        self.assertIn("IC", text)  # the per-point table
        self.assertIn("Unit: RPM", dash.box.call_args.args[6])
        dash.draw_fans({"mode": "balanced"}, {}, 1, 0, 17, 72)  # small: no table
        self.assertIn("Fan curves are locked in balanced mode. Select custom to edit.", {t for t, _ in cells.values()})


class CurveStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.home = self.root / "home"
        self.active = self.home / ".config/loqctl/active.json"
        pw = patch.object(m_store.pwd, "getpwuid",
                          side_effect=lambda uid: Mock(pw_dir=str(self.home if uid == os.getuid() else self.root / f"home{uid}")))
        pw.start()
        self.addCleanup(pw.stop)

    def test_config_dir_respects_xdg(self):
        self.assertEqual(m_store.config_dir(), self.config / "loqctl")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(m_store.config_dir(), Path.home() / ".config/loqctl")

    def test_active_path_ignores_xdg_and_home_for_own_and_other_uid(self):
        with patch.dict(os.environ, {"HOME": "/elsewhere"}):
            self.assertEqual(m_store.active_path(), self.active)
            self.assertEqual(m_store.active_path(os.getuid() + 1),
                             self.root / f"home{os.getuid() + 1}/.config/loqctl/active.json")
        self.assertEqual(m_store.save_active(CURVE), self.active)
        self.assertFalse((self.config / "loqctl/active.json").exists())

    def test_active_path_needs_an_absolute_home(self):
        for entry in (KeyError("uid"), Mock(pw_dir=""), Mock(pw_dir="relative")):
            effect = {"side_effect": entry} if isinstance(entry, KeyError) else {"return_value": entry}
            with patch.object(m_store.pwd, "getpwuid", **effect):
                with self.assertRaises(OSError):
                    m_store.active_path()

    def test_legacy_xdg_active_json_blocks_recording_stock(self):
        m_store.write_atomic(self.config / "loqctl/active.json", CURVE)
        self.assertFalse(m_store.save_stock("custom", CURVE))
        self.assertFalse(m_store.stock_path("custom").exists())

    def test_clear_active_removes_the_legacy_file_too(self):
        legacy = self.config / "loqctl/active.json"
        m_store.write_atomic(legacy, CURVE)
        m_store.save_active(CURVE)
        m_store.clear_active()
        self.assertFalse(legacy.exists())
        self.assertFalse(self.active.exists())
        self.assertTrue(m_store.save_stock("custom", CURVE))

    def test_non_utf8_file_is_a_control_error(self):
        path = self.root / "bad.json"
        path.write_bytes(b"\xff\xfe{")
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path)

    def test_stock_is_recorded_once_per_mode_and_never_over_active(self):
        self.assertTrue(m_store.save_stock("custom", CURVE))
        self.assertFalse(m_store.save_stock("custom", edit(CURVE, {(1, "fan1"): 90})))  # first read wins
        self.assertEqual(m_store.read_stock("custom")[1]["fan1"], 80)
        self.assertIsNone(m_store.read_stock("balanced"))
        m_store.save_active(CURVE)
        self.assertFalse(m_store.save_stock("balanced", CURVE))  # the live curve may be custom
        self.assertIsNone(m_store.read_stock("balanced"))
        self.assertFalse(m_store.save_stock(None, CURVE))
        m_store.clear_active()
        m_store.mark_written("balanced")
        self.assertFalse(m_store.save_stock("balanced", CURVE))  # loqctl already wrote in this mode
        m_store.save_active(CURVE)
        self.assertEqual(m_store.stock_path("../x y").parent, self.config / "loqctl/stock")
        m_store.clear_active()
        m_store.clear_active()  # already gone
        self.assertFalse(self.active.exists())

    def test_atomic_write_mode_and_round_trip(self):
        path = m_store.save_active(CURVE)
        self.assertEqual(path, self.active)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(list(path.parent.glob(".*tmp")), [])
        points = m_store.read_curve_file(path, os.getuid())
        self.assertNotIn("accel", points[0])
        self.assertEqual(points[1]["fan1"], 80)
        with patch.object(m_store.os, "replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                m_store.save_active(edit(CURVE, {(1, "fan1"): 90}))
        self.assertEqual(m_store.read_curve_file(path)[1]["fan1"], 80)
        self.assertEqual(list(path.parent.glob(".*tmp")), [])

    def test_saved_curves_listed_with_invalid(self):
        current = m_fan.read_curve(make_hwmon(self.root))
        m_store.write_atomic(self.config / "loqctl/curves/b-good.json", edit(CURVE, {(1, "fan1"): 90}))
        m_store.write_atomic(self.config / "loqctl/curves/a-bad.json", edit(CURVE, {(1, "cpu_up"): 30}))
        (self.config / "loqctl/curves/c-junk.json").write_text("{")
        null = {"version": 1, "points": m_store.writable_points(CURVE)}
        null["points"][0]["cpu_up"] = None
        (self.config / "loqctl/curves/d-null.json").write_text(json.dumps(null))
        saved = m_store.saved_curves(current)
        self.assertEqual([n for n, _ in saved], ["a-bad", "b-good", "c-junk", "d-null"])
        self.assertEqual([p is None for _, p in saved], [True, False, True, True])
        self.assertEqual([n for n, _ in m_store.curve_options("custom", current)], ["a-bad", "b-good", "c-junk", "d-null"])
        m_store.save_stock("custom", CURVE)
        self.assertEqual([n for n, _ in m_store.curve_options("custom", current)][:2], ["stock", "a-bad"])
        self.assertEqual([n for n, _ in m_store.curve_options("balanced", current)][:1], ["a-bad"])
        m_store.write_atomic(self.config / "loqctl/curves/stock.json", CURVE)
        self.assertEqual([n for n, _ in m_store.curve_options("custom", current)].count("stock"), 1)

    def test_untrusted_files_rejected(self):
        path = m_store.save_active(CURVE)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path, os.getuid() + 1)
        path.chmod(0o666)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path, os.getuid())
        link = path.with_name("link.json")
        link.symlink_to(path)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(link)
        path.write_text("{}")
        junk = path.with_name("junk.json")
        junk.write_text("{")
        fifo = path.with_name("fifo.json")
        os.mkfifo(fifo)
        cases = [(path, None), (path, os.getuid() + 1), (junk, None), (fifo, None), (link, None),
                 (path.with_name("missing.json"), None)]
        for bad, uid in cases:
            with self.assertRaises(m_common.ControlError) as caught:
                m_store.read_curve_file(bad, uid)
            self.assertNotIn(str(path.parent), str(caught.exception))
            self.assertIn(bad.name, str(caught.exception))

    def test_fifo_rejected_without_blocking(self):
        path = m_store.save_active(CURVE)
        path.unlink()
        os.mkfifo(path)
        with self.assertRaises(m_common.ControlError):
            m_store.read_curve_file(path, os.getuid())


class ReapplyTests(helpers.SysfsCase):
    def setUp(self):
        super().setUp()
        self.hwmon = make_hwmon(self.root)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        self.home = self.root / "home"
        self.active = self.home / ".config/loqctl/active.json"
        pw = patch.object(m_store.pwd, "getpwuid",
                          side_effect=lambda uid: Mock(pw_dir=str(self.home if uid == os.getuid() else self.root / f"home{uid}")))
        pw.start()
        self.addCleanup(pw.stop)
        m_store.save_active(edit(CURVE, {(1, "fan1"): 90}))
        self.log = []
        self.reapply = m_store.Reapplier(self.hw, os.getuid(), self.log.append)
        self.clock = {"boot": 1000.0, "mono": 500.0}
        patcher = patch.object(m_store.time, "clock_gettime",
                               side_effect=lambda c: self.clock["boot" if c == m_store.time.CLOCK_BOOTTIME else "mono"])
        patcher.start()
        self.addCleanup(patcher.stop)

    def fan1(self):
        return m_fan.read_curve(self.hwmon)[1]["fan1"]

    def tick(self, mode):
        self.reapply({"mode": mode})

    def test_mode_change_into_custom_writes_once(self):
        self.tick("balanced")
        self.assertEqual(self.fan1(), 80)
        self.tick("custom")
        self.assertEqual(self.fan1(), 90)
        self.assertIn("re-applied active.json", self.log[-1])
        self.assertNotIn(str(self.config), self.log[-1])
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.tick("custom")  # no change of mode: no write
        self.assertEqual(self.fan1(), 80)
        self.assertEqual(len(self.log), 1)

    def test_other_modes_never_write(self):
        self.tick("custom")
        self.hw.set_mode("balanced")
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.tick("balanced")
        self.tick("performance")
        self.assertEqual(self.fan1(), 80)

    def test_resume_detection(self):
        self.tick("custom")
        (self.hwmon / "pwm1_auto_point2_pwm").write_text("80")
        self.clock["boot"] += 5.0
        self.clock["mono"] += 5.0
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.clock["boot"] += 3600.0 + 5
        self.clock["mono"] += 5.0
        self.tick("custom")
        self.assertEqual(self.fan1(), 90)
        self.assertIn("after resume", self.log[-1])

    def test_untrusted_or_invalid_file_skipped_with_log(self):
        path = self.active
        path.chmod(0o666)
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        path.chmod(0o600)
        with patch.object(m_store.os.path, "lexists", side_effect=OSError(13, "Permission denied", str(path))):
            self.reapply.apply("custom mode")
        self.assertEqual(self.log[-1], "fancurve: skipped re-apply after custom mode: Permission denied")
        with patch.object(self.hw, "write_fancurve", side_effect=ValueError(str(path))):
            self.reapply.apply("custom mode")
        self.assertEqual(self.log[-1], "fancurve: skipped re-apply after custom mode: ValueError")
        other = m_store.Reapplier(self.hw, os.getuid() + 1, self.log.append)
        with patch.object(m_store, "active_path", return_value=path):
            other({"mode": "custom"})
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        m_store.save_active(edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}))
        self.tick("balanced")
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertEqual(len(self.log), 5)
        m_store.save_active(CURVE[:2])  # wrong size
        self.tick("balanced")
        self.tick("custom")
        self.assertIn("skipped", self.log[-1])

    def test_xdg_elsewhere_still_reapplies(self):
        self.assertFalse((self.config / "loqctl/active.json").exists())
        self.tick("custom")
        self.assertEqual(self.fan1(), 90)

    def test_rejected_file_then_deleted_logs_again(self):
        self.active.chmod(0o666)
        self.tick("custom")
        self.assertIn("skipped", self.log[-1])
        self.active.unlink()
        self.tick("balanced")
        self.tick("custom")
        self.assertIn("nothing to re-apply", self.log[-1])

    def test_oversized_file_is_rejected(self):
        self.active.write_text("é" * (m_store.MAX_FILE // 2 + 1))  # under the limit in characters, over in bytes
        with patch.object(m_store.os, "fstat", return_value=Mock(st_mode=0o100600, st_size=0, st_uid=os.getuid())):
            with self.assertRaises(m_common.ControlError):
                m_store.read_curve_file(self.active)

    def test_missing_file_logs_once_and_sampler_hook_survives_errors(self):
        self.active.unlink()
        self.tick("custom")
        self.assertEqual(self.log, ["fancurve: nothing to re-apply after custom mode: no ~/.config/loqctl/active.json"])
        self.tick("balanced")
        self.tick("custom")
        self.assertEqual(len(self.log), 1)
        m_store.save_active(edit(CURVE, {(1, "fan1"): 90}))
        self.tick("balanced")
        self.tick("custom")
        self.assertIn("re-applied", self.log[-1])
        self.active.unlink()
        self.tick("balanced")
        self.tick("custom")
        self.assertEqual(len(self.log), 3)
        self.assertIn("nothing to re-apply", self.log[-1])
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = Mock(side_effect=RuntimeError("boom"))
        with patch.object(sampler.sensors, "sample", return_value={}), patch("builtins.print"):
            sampler.tick()
            sampler.hook.join(5)
        sampler.on_firmware.assert_called_once()
        self.assertEqual(sampler.latest["mode"], "custom")

    def finish(self, sampler):
        sampler.hook.join(5)
        self.assertFalse(sampler.hook.is_alive())

    def test_slow_hook_does_not_delay_the_snapshot(self):
        release = threading.Event()
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = Mock(side_effect=lambda caps: release.wait(5))
        with patch.object(sampler.sensors, "sample", return_value={}):
            sampler.tick()
            self.assertEqual(sampler.latest["mode"], "custom")
            sampler.force.set()
            sampler.tick()  # hook still running: not started twice
            release.set()
            self.finish(sampler)
        sampler.on_firmware.assert_called_once()

    def test_reapplier_on_hook_thread_sees_every_mode_flip(self):
        release, written = threading.Event(), threading.Event()
        applied = []
        real_apply = self.reapply.apply

        def slow_apply(reason):
            applied.append(reason)
            real_apply(reason)
            if len(applied) == 1:
                written.set()
                release.wait(5)
        self.reapply.apply = slow_apply
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = self.reapply
        with patch.object(sampler.sensors, "sample", return_value={}):
            sampler.tick()  # enters custom, hook blocks mid-write
            self.assertTrue(written.wait(5))
            self.hw.profile.write_text("balanced\n")
            sampler.force.set()
            sampler.tick()  # no firmware read while the hook runs
            self.assertEqual(sampler.latest["mode"], "custom")
            release.set()
            self.finish(sampler)
            self.assertEqual(self.fan1(), 90)
            sampler.force.set()
            sampler.tick()  # sees the flip once the hook is done
            self.finish(sampler)
            self.assertEqual(sampler.latest["mode"], "balanced")
            self.hw.profile.write_text("custom\n")
            sampler.force.set()
            sampler.tick()
            self.finish(sampler)
        self.assertEqual(len(applied), 2)

    def test_halt_waits_for_the_running_hook(self):
        release = threading.Event()
        done = []
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = lambda caps: (release.wait(5), done.append(1))
        with patch.object(sampler.sensors, "sample", return_value={}):
            sampler.tick()
        threading.Timer(0.2, release.set).start()
        sampler.halt()
        self.assertEqual(done, [1])


class FanPanelPresetTests(FanPanelFixture):
    def test_stock_cycle_stage_save_and_reset_keys(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f")
        dash.fan_take()
        self.assertIsNone(m_store.read_stock("custom"))  # reading never records it
        self.keys(dash, state, "]", "]", curses.KEY_SF)
        self.assertEqual(dash.fan_edit[2]["fan1"], 169)  # five 1 % steps: 71 % to 66 %
        self.keys(dash, state, curses.KEY_SF)
        self.assertEqual(dash.fan_edit[2]["fan1"], 156)  # 61 %
        self.keys(dash, state, 10)
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 156)
        self.assertEqual(m_store.read_curve_file(self.active)[2]["fan1"], 156)
        self.assertEqual(m_store.read_stock("custom")[2]["fan1"], 180)  # recorded just before the first write
        self.keys(dash, state, curses.KEY_UP, 10)
        self.assertEqual(m_store.read_stock("custom")[2]["fan1"], 180)  # later applies do not overwrite it
        self.keys(dash, state, "W")
        self.assertTrue(dash.notice.startswith("Saved "))
        self.assertEqual(len(list((self.config / "loqctl/curves").glob("saved-*.json"))), 1)
        self.keys(dash, state, "p")
        self.assertEqual(dash.fan_preset, "stock")
        self.assertEqual(dash.fan_edit[2]["fan1"], 180)
        dash.draw_fans(state, {}, 1, 0, 25, 100)
        self.assertIn("Staged stock", dash.box.call_args.args[6])
        dash.fan_preset = "n" * 80
        dash.draw_fans(state, {}, 1, 0, 25, 40)
        label = dash.box.call_args.args[6]
        self.assertTrue(label.endswith("Unit: %"))  # the curve name is cut, the unit stays
        self.assertLess(len(label), 40 - 8)
        self.assertIs(dash.fan_scale(), dash.fan_scale())
        dash.fan_preset = "stock"
        m_store.write_atomic(self.config / "loqctl/curves/aa.json", edit(CURVE, {(1, "fan1"): 90}))
        self.keys(dash, state, "p")  # a new saved curve sorts before the staged one; the name still decides
        self.assertEqual(dash.fan_preset, dash.fan_opts[1][0])
        m_store.write_atomic(self.config / "loqctl/curves/zz.json", edit(CURVE, {(0, "cpu_up"): 99}))
        self.keys(dash, state, "r", "p", "p", "p", "p")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("Curve zz (4/4) is invalid and cannot be staged", dash.notice)
        self.assertNotEqual(dash.fan_preset, "zz")
        self.keys(dash, state, "p")  # the next press moves past the invalid curve instead of retrying it
        self.assertEqual(dash.fan_preset, dash.fan_opts[0][0])
        self.keys(dash, state, "p", "p")
        self.assertEqual(dash.fan_preset, dash.fan_opts[2][0])  # a valid curve after the invalid one is reachable by cycling

    def test_reset_writes_stock_and_stops_reapply(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        self.assertTrue(self.active.exists())
        self.keys(dash, state, "R")
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 180)
        self.assertFalse(self.active.exists())
        self.assertIn("Reset to the stock curve", dash.notice)

    def test_reset_without_recorded_stock_writes_nothing(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        (self.config / "loqctl/stock/custom.json").unlink()
        (self.config / "loqctl/stock/custom.written").unlink()
        with patch.object(self.hw, "write_fancurve") as write:
            self.keys(dash, state, "R")
        write.assert_not_called()
        self.assertFalse(self.active.exists())
        self.assertIn("never recorded", dash.notice)
        self.assertNotIn("Reset to", dash.notice)
        self.keys(dash, state, "f")
        dash.fan_take()
        self.assertIsNone(m_store.read_stock("custom"))  # the custom curve is never taken for stock
        self.assertTrue((self.config / "loqctl/stock/custom.written").exists())

    def test_apply_with_failed_pre_write_read_still_applies(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF)
        real = self.hw.fancurve
        dash.backend.fancurve = Mock(return_value={"available": False, "reason": "gone"})
        self.keys(dash, state, 10)
        dash.backend.fancurve = real
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 169)
        self.assertIn("stock curve was not recorded", dash.notice)
        self.assertIsNone(m_store.read_stock("custom"))
        self.assertTrue((self.config / "loqctl/stock/custom.written").exists())

    def test_reset_refuses_stale_stock_and_root(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        m_store.write_atomic(self.config / "loqctl/stock/custom.json", CURVE[:2])  # point count changed
        self.active.write_text("{}")
        with patch.object(self.hw, "write_fancurve") as write:
            self.keys(dash, state, "R")
            write.assert_not_called()
            self.assertIn("no longer fits", dash.notice)
            self.assertTrue(self.active.exists())
            with patch.object(os, "geteuid", return_value=0):
                self.keys(dash, state, "R")
            write.assert_not_called()
        self.assertIn("not root", dash.notice)
        self.assertTrue(self.active.exists())

    def test_marker_failure_and_active_json_are_reported_not_fatal(self):
        dash = self.dash()
        state = {"mode": "custom"}
        m_store.save_active(CURVE)  # save_stock refuses while active.json exists
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF)
        with patch.object(m_panel, "mark_written", side_effect=PermissionError(13, "Permission denied")):
            self.keys(dash, state, 10)
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 169)
        self.assertIn("stock curve was not recorded", dash.notice)
        self.assertIn("Could not save the stock marker", dash.notice)
        self.assertIsNone(m_store.read_stock("custom"))

    def test_reload_dropping_staged_edit_clears_preset_name(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF)
        dash.fan_preset = "x"
        dash.fan_result = {"available": True, "mode": "custom", "points": CURVE[:2]}
        dash.fan_take()
        self.assertEqual((dash.fan_edit, dash.fan_preset), (None, None))

    def test_reset_reports_file_errors(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        with patch.object(m_panel, "clear_active", side_effect=PermissionError(13, "Permission denied")):
            self.keys(dash, state, "R")
        self.assertIn("Reset failed: Permission denied", dash.notice)

    def test_preset_with_null_point_sets_notice(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f")
        with patch.object(m_panel, "curve_options", return_value=[("bad", edit(CURVE, {(0, "cpu_up"): None}))]):
            self.keys(dash, state, "p")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("Curve bad (1/1) cannot be staged: Point 1", dash.notice)

    def test_locked_mode_blocks_presets(self):
        dash = self.dash("balanced")
        self.keys(dash, {"mode": "balanced"}, "f", curses.KEY_UP, 10)
        self.assertEqual(dash.panel, "fans")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("Fan curves are locked in balanced mode", dash.notice)
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["fan1"], 0)
        self.keys(dash, {"mode": "balanced"}, "p")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("locked in balanced mode", dash.notice)
        self.keys(dash, {"mode": "balanced"}, "R")
        self.assertIn("locked in balanced mode", dash.notice)
