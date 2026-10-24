# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
import copy
import errno
import json
import os
import sys
import tempfile
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
from tests import test_loqctl as base

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
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.hwmon = make_hwmon(self.root)
        self.pct = m_fan.Scale("percent")

    def fields(self, errors):
        return {(i, f) for i, f, _ in errors}

    def test_discovery(self):
        self.assertEqual(m_fan.find_legion_hwmon(self.root), self.hwmon)
        empty = tempfile.TemporaryDirectory()
        self.addCleanup(empty.cleanup)
        self.assertIsNone(m_fan.find_legion_hwmon(empty.name))
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

    def test_never_touches_fan_target_or_other_files(self):
        self.assertTrue(all("target" not in p and "points_size" not in p for p in m_fan.FILE_MAP.values()))
        with patch.object(m_fan.os, "open", wraps=os.open) as opened:
            m_fan.write_curve(self.hwmon, edit(CURVE, {(1, "fan2"): 100}), self.pct)
        self.assertEqual([Path(c.args[0]).name for c in opened.call_args_list], ["pwm2_auto_point2_pwm"])
        self.assertEqual((self.hwmon / "fan1_target").read_text(), "0")

    def test_invalid_target_refused_with_no_writes(self):
        with patch.object(m_fan, "_write_file") as write:
            for target in (edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}), CURVE[:2], edit(CURVE, {(0, "bogus"): 1})):
                with self.assertRaises(m_common.ControlError):
                    m_fan.write_curve(self.hwmon, target, self.pct)
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


class MoveTests(unittest.TestCase):
    def test_speed_sets_both_fans_and_keeps_order(self):
        points, moved = m_panel.move_speed(NECN_STOCK, 1, 100, RPM)
        self.assertTrue(moved)
        self.assertEqual((RPM.show(points[1]["fan1"]), RPM.show(points[1]["fan2"])), (2600, 2600))
        self.assertEqual(points[1]["cpu_up"], NECN_STOCK[1]["cpu_up"])  # temperatures never move
        points, _ = m_panel.move_speed(NECN_STOCK, 1, 5000, RPM)
        self.assertEqual((RPM.show(points[1]["fan1"]), RPM.show(points[1]["fan2"])), (3400, 3700))  # each fan held below its own next point
        points, moved = m_panel.move_speed(NECN_STOCK, 0, -100, RPM)
        self.assertFalse(moved)
        points, _ = m_panel.move_speed(NECN_STOCK, 9, 50000, RPM)
        self.assertEqual((RPM.show(points[9]["fan1"]), RPM.show(points[9]["fan2"])), (10000, 10000))
        self.assertEqual(m_fan.validate_curve(points, RPM), [])

    def test_speed_stays_above_the_floor(self):
        low = edit(NECN_STOCK, {(8, "fan1"): 0, (8, "fan2"): 0})
        points, _ = m_panel.move_speed(low, 9, -50000, RPM)
        self.assertEqual((RPM.show(points[9]["fan1"]), RPM.show(points[9]["fan2"])), (3000, 3000))


class FanCurveSurfaceTests(unittest.TestCase):
    dashboard = base.FirmwareTests.dashboard

    def setUp(self):
        base.FirmwareTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.hwmon = make_hwmon(self.root)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
        thread = patch.object(m_panel, "threading", SimpleNamespace(Thread=ImmediateThread))
        thread.start()
        self.addCleanup(thread.stop)

    def set_mode(self, mode):
        self.hw.set_mode(mode)

    def test_hardware_fancurve_cache_and_gate(self):
        data = self.hw.fancurve()
        self.assertTrue(data["available"])
        self.assertEqual((data["unit"], data["max_rpm"], data["rpm"][0], data["rpm"][1]),
                         ("percent", 10000, 2100.0, None))
        self.assertEqual(len(data["points"]), 3)
        self.assertEqual(data["mode"], "custom")
        with patch.object(m_hardware, "read_curve", side_effect=AssertionError("read again")):
            data["points"][0]["fan1"] = 99  # callers get a copy
            self.assertEqual(self.hw.fancurve()["points"][0]["fan1"], 0)
        with patch.object(m_hardware, "read_curve", wraps=m_fan.read_curve) as read:
            self.hw.fancurve(fresh=True)
            self.set_mode("balanced")  # a mode change may reset the curve
            self.hw.fancurve()
            self.assertEqual(read.call_count, 2)
        with self.assertRaisesRegex(m_common.ControlError, "locked in balanced mode"):
            self.hw.write_fancurve(CURVE)
        self.set_mode("custom")
        self.hw.fancurve()
        self.hw.write_fancurve(edit(CURVE, {(1, "fan1"): 90}))
        self.assertEqual(self.hw.fancurve()["points"][1]["fan1"], 90)  # write drops the cache
        self.assertFalse(m_hardware.Hardware(self.root / "nowhere").fancurve()["available"])

    def test_internal_cli_prints_results(self):
        out = Mock()
        payload = json.dumps(edit(CURVE, {(1, "fan1"): 90}))
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("builtins.print", out):
            self.assertEqual(m_cli.main(["_fancurve", payload]), 0)
        self.assertEqual(json.loads(out.call_args.args[0])[0]["status"], "APPLIED")
        with patch.object(m_cli, "Hardware", return_value=self.hw), patch("sys.stderr"):
            self.assertEqual(m_cli.main(["_fancurve", json.dumps(CURVE[:1])]), 1)

    def test_fan_cli_json(self):
        out = Mock()
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

    def test_locked_notice_in_balanced(self):
        dash = self.dash("balanced")
        state = {"mode": "balanced"}
        self.keys(dash, state, "f", curses.KEY_UP, 10)
        self.assertEqual(dash.panel, "fans")
        self.assertIn("Fan curves are locked in balanced mode", dash.notice)
        self.assertIsNone(dash.fan_edit)
        self.assertEqual(m_fan.read_curve(self.hwmon)[0]["fan1"], 0)

    def test_select_move_and_apply(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", 9)
        self.assertEqual(dash.fan_sel, 2)
        self.keys(dash, state, "[", curses.KEY_BTAB, curses.KEY_BTAB)
        self.assertEqual(dash.fan_sel, 2)
        self.keys(dash, state, "[", curses.KEY_LEFT, curses.KEY_RIGHT, curses.KEY_UP)  # point 2; temperatures are fixed
        self.assertEqual((dash.fan_edit[1]["cpu_up"], dash.fan_edit[1]["gpu_down"]), (60, 35))
        self.assertEqual((dash.fan_edit[1]["fan1"], dash.fan_edit[1]["fan2"]), (92, 92))  # 31 % + 5 % = 36 %
        self.keys(dash, state, 10)
        point = m_fan.read_curve(self.hwmon)[1]
        self.assertEqual((point["cpu_up"], point["fan1"], point["fan2"]), (60, 92, 92))
        self.assertIsNone(dash.fan_edit)
        self.assertIn("Applied and verified", dash.notice)

    def test_held_point_and_discard(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", curses.KEY_DOWN)  # point 1 is already at 0
        self.assertIn("held", dash.notice)
        self.keys(dash, state, curses.KEY_UP, "r")
        self.assertIsNone(dash.fan_edit)

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
        self.assertEqual(run.call_args.args[0][1]["fan1"], 92)
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
        self.keys(dash, {"mode": "custom"}, "f", "]", "]")
        self.assertTrue(dash.fan["available"])
        dash.draw_fans({"mode": "custom"}, {"cpu": {"temperature_c": 67.4}, "fans": [{"rpm": 2000.0}]}, 1, 0, 30, 110)
        text = "\n".join(dash.puts)
        for needle in ("Point 3/10", "CPU 75°C (down 73°C)", "Fan 1 3400 RPM", "Fan 2 3700 RPM", "CPU 67°C",
                       "70°C", "4000", "◉", "●", "◆", "3700", "Curves"):
            self.assertIn(needle, text)
        self.assertIn("Unit: RPM", dash.box.call_args.args[6])
        dash.draw_fans({"mode": "balanced"}, {}, 1, 0, 17, 72)  # small: no table
        self.assertIn("locked in balanced mode", "\n".join(dash.puts))


class CurveStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)

    def test_config_dir_respects_xdg_and_serve_uid_home(self):
        self.assertEqual(m_store.config_dir(), self.config / "loqctl")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}):
            self.assertEqual(m_store.config_dir(), Path.home() / ".config/loqctl")
        home = Mock(pw_dir="/home/other")
        with patch.object(m_store.pwd, "getpwuid", return_value=home):
            self.assertEqual(m_store.config_dir(os.getuid() + 1), Path("/home/other/.config/loqctl"))
        self.assertEqual(m_store.config_dir(os.getuid()), self.config / "loqctl")

    def test_stock_is_recorded_once_per_mode_and_never_over_active(self):
        self.assertTrue(m_store.save_stock("custom", CURVE))
        self.assertFalse(m_store.save_stock("custom", edit(CURVE, {(1, "fan1"): 90})))  # first read wins
        self.assertEqual(m_store.read_stock("custom")[1]["fan1"], 80)
        self.assertIsNone(m_store.read_stock("balanced"))
        m_store.save_active(CURVE)
        self.assertFalse(m_store.save_stock("balanced", CURVE))  # the live curve may be custom
        self.assertIsNone(m_store.read_stock("balanced"))
        self.assertFalse(m_store.save_stock(None, CURVE))
        self.assertEqual(m_store.stock_path("../x y").parent, self.config / "loqctl/stock")
        m_store.clear_active()
        m_store.clear_active()  # already gone
        self.assertFalse((self.config / "loqctl/active.json").exists())

    def test_atomic_write_mode_and_round_trip(self):
        path = m_store.save_active(CURVE)
        self.assertEqual(path, self.config / "loqctl/active.json")
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
        saved = m_store.saved_curves(current)
        self.assertEqual([n for n, _ in saved], ["a-bad", "b-good", "c-junk"])
        self.assertEqual([p is None for _, p in saved], [True, False, True])
        self.assertEqual([n for n, _ in m_store.curve_options("custom", current)], ["a-bad", "b-good", "c-junk"])
        m_store.save_stock("custom", CURVE)
        self.assertEqual([n for n, _ in m_store.curve_options("custom", current)][:2], ["stock", "a-bad"])
        self.assertEqual([n for n, _ in m_store.curve_options("balanced", current)][:1], ["a-bad"])

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


class ReapplyTests(unittest.TestCase):
    def setUp(self):
        base.FirmwareTests.setUp(self)
        self.addCleanup(self.temp.cleanup)
        self.hwmon = make_hwmon(self.root)
        self.config = self.root / "config"
        env = patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.config)})
        env.start()
        self.addCleanup(env.stop)
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
        self.assertIn("re-applied", self.log[-1])
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
        path = self.config / "loqctl/active.json"
        path.chmod(0o666)
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        path.chmod(0o600)
        other = m_store.Reapplier(self.hw, os.getuid() + 1, self.log.append)
        with patch.object(m_store, "config_dir", return_value=path.parent):
            other({"mode": "custom"})
        self.assertEqual(self.fan1(), 80)
        self.assertIn("skipped", self.log[-1])
        m_store.save_active(edit(CURVE, {(1, "cpu_up"): 30, (1, "fan1"): 90}))
        self.tick("balanced")
        self.tick("custom")
        self.assertEqual(self.fan1(), 80)
        self.assertEqual(len(self.log), 3)
        m_store.save_active(CURVE[:2])  # wrong size
        self.tick("balanced")
        self.tick("custom")
        self.assertIn("skipped", self.log[-1])

    def test_missing_file_is_silent_and_sampler_hook_survives_errors(self):
        (self.config / "loqctl/active.json").unlink()
        self.tick("custom")
        self.assertEqual(self.log, [])
        sampler = m_sensors.Sampler(self.hw)
        sampler.on_firmware = Mock(side_effect=RuntimeError("boom"))
        with patch.object(sampler.sensors, "sample", return_value={}), patch("builtins.print"):
            sampler.tick()
        sampler.on_firmware.assert_called_once()
        self.assertEqual(sampler.latest["mode"], "custom")


class FanPanelPresetTests(FanCurveSurfaceTests):
    def test_stock_cycle_stage_save_and_reset_keys(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f")
        dash.fan_take()
        self.assertEqual(m_store.read_stock("custom")[2]["fan1"], 180)  # recorded on the first read
        self.keys(dash, state, "]", "]", curses.KEY_SF)
        self.assertEqual(dash.fan_edit[2]["fan1"], 153)  # held at the 60 % last-point floor
        self.keys(dash, state, 10)
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 153)
        self.assertEqual(m_store.read_curve_file(self.config / "loqctl/active.json")[2]["fan1"], 153)
        self.assertEqual(m_store.read_stock("custom")[2]["fan1"], 180)  # the apply did not overwrite it
        self.keys(dash, state, "W")
        self.assertTrue(dash.notice.startswith("Saved "))
        self.assertEqual(len(list((self.config / "loqctl/curves").glob("saved-*.json"))), 1)
        self.keys(dash, state, "p")
        self.assertEqual(dash.fan_preset, "stock")
        self.assertEqual(dash.fan_edit[2]["fan1"], 180)
        dash.draw_fans(state, {}, 1, 0, 25, 100)
        self.assertTrue(any(t == "stock" for t in dash.puts))
        m_store.write_atomic(self.config / "loqctl/curves/aa.json", edit(CURVE, {(1, "fan1"): 90}))
        self.keys(dash, state, "p")  # a new saved curve sorts before the staged one; the name still decides
        self.assertEqual(dash.fan_preset, dash.fan_opts[1][0])
        m_store.write_atomic(self.config / "loqctl/curves/zz.json", edit(CURVE, {(0, "cpu_up"): 99}))
        self.keys(dash, state, "r", "p", "p", "p", "p")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("invalid and cannot be staged", dash.notice)
        dash.draw_fans(state, {}, 1, 0, 25, 100)
        self.assertTrue(any(t == "zz (invalid)" for t in dash.puts))

    def test_reset_writes_stock_and_stops_reapply(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        self.assertTrue((self.config / "loqctl/active.json").exists())
        self.keys(dash, state, "R")
        self.assertEqual(m_fan.read_curve(self.hwmon)[2]["fan1"], 180)
        self.assertFalse((self.config / "loqctl/active.json").exists())
        self.assertIn("Reset to the stock curve", dash.notice)

    def test_reset_without_recorded_stock_writes_nothing(self):
        dash = self.dash()
        state = {"mode": "custom"}
        self.keys(dash, state, "f", "]", "]", curses.KEY_SF, 10)
        (self.config / "loqctl/stock/custom.json").unlink()
        with patch.object(self.hw, "write_fancurve") as write:
            self.keys(dash, state, "R")
        write.assert_not_called()
        self.assertFalse((self.config / "loqctl/active.json").exists())
        self.assertIn("never recorded", dash.notice)
        self.assertNotIn("Reset to", dash.notice)

    def test_locked_mode_blocks_presets(self):
        dash = self.dash("balanced")
        self.keys(dash, {"mode": "balanced"}, "f", "p")
        self.assertIsNone(dash.fan_edit)
        self.assertIn("locked in balanced mode", dash.notice)
        self.keys(dash, {"mode": "balanced"}, "R")
        self.assertIn("locked in balanced mode", dash.notice)
