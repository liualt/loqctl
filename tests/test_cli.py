# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from tests.helpers import REPO
import loqctl
from loqctl import cli as m_cli
from loqctl import common as m_common
from loqctl import keyboard as m_keyboard
from loqctl.doctor import doctor


class CliTests(helpers.SysfsCase):
    def test_apply_subcommand_prints_json(self):
        out = io.StringIO()
        with patch.object(m_cli, "Hardware", return_value=self.hw), contextlib.redirect_stdout(out):
            self.assertEqual(m_cli.main(["_apply", json.dumps([["ppt_pl1_spl", 60]])]), 0)
        self.assertEqual(json.loads(out.getvalue())[0]["status"], "APPLIED")

    def test_toggle_lists_and_sets_through_the_backend(self):
        backend = unittest.mock.Mock(remote=True, notice="")
        backend.toggles.return_value = [{"name": "fn_lock", "value": 0, "max": 1}]
        backend.write_toggle.return_value = {"verified": True}
        out = io.StringIO()
        with patch.object(m_cli, "Backend", return_value=backend), contextlib.redirect_stdout(out):
            self.assertEqual(m_cli.main(["toggle"]), 0)
            self.assertEqual(m_cli.main(["toggle", "fn_lock", "on"]), 0)
        backend.write_toggle.assert_called_once_with("fn_lock", 1)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(m_cli.main(["toggle", "fn_lock", "\u00b2"]), 1)
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            m_cli.main(["toggle", "touchpad", "on"])
        backend = unittest.mock.Mock(remote=False, notice="")
        with patch.object(m_cli, "Backend", return_value=backend), patch.object(m_cli.os, "geteuid", return_value=1000), \
                patch.object(m_cli, "sudo_run", return_value=('{"verified": true}', "")) as run, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(m_cli.main(["toggle", "winkey", "off"]), 0)
        run.assert_called_once_with(["_toggle", "winkey", "0"])
        backend.write_toggle.assert_not_called()

    def run_watch(self, args, samples):
        """Run metrics --watch with mocked Sensors; returns (exit, stdout lines, stderr, sleeps, Sensors mock)."""
        sleeps, calls = [], []

        def sample():
            calls.append(1)
            if len(calls) > len(samples) + 1:  # first call primes
                raise KeyboardInterrupt
            return samples[len(calls) - 2] if len(calls) > 1 else None

        sensors = unittest.mock.Mock()
        sensors.return_value.sample.side_effect = sample
        backend = unittest.mock.Mock(remote=False, notice="")
        out, err = io.StringIO(), io.StringIO()
        with patch.object(m_cli, "Backend", return_value=backend), patch.object(m_cli, "Sensors", sensors), \
                patch.object(m_cli.time, "sleep", sleeps.append), \
                patch.object(m_cli.time, "monotonic", side_effect=[100, 100, 100.5, 101.2, 101.5, 102.0]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = m_cli.main(["metrics", "--watch"] + args)
        return code, out.getvalue().splitlines(), err.getvalue(), sleeps, sensors

    def test_metrics_watch_streams_with_one_sensors_and_deadline_sleeps(self):
        code, lines, _, sleeps, sensors = self.run_watch([], [{"n": 1}, {"n": 2}])
        self.assertEqual(code, 130)
        self.assertEqual([json.loads(x) for x in lines], [{"n": 1}, {"n": 2}])
        sensors.assert_called_once_with()
        self.assertEqual([round(x, 1) for x in sleeps], [1, 0.8, 1])

    def test_metrics_watch_interval_argument_and_minimum(self):
        _, _, _, sleeps, _ = self.run_watch(["3"], [{"n": 1}])
        self.assertEqual(sleeps[0], 3)
        for bad in ("0.5", "nan", "inf"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(m_cli.main(["metrics", "--watch", bad]), 1)
            self.assertIn("at least 1 second", err.getvalue())

    def test_metrics_watch_remote_reads_status_metrics(self):
        backend = unittest.mock.Mock(remote=True, notice="", path="/sock")
        out = io.StringIO()
        with patch.object(m_cli, "Backend", return_value=backend), patch.object(m_cli.time, "sleep"), \
                patch("loqctl.api.request", side_effect=[{"metrics": {"n": 1}}, KeyboardInterrupt]) as req, \
                contextlib.redirect_stdout(out):
            self.assertEqual(m_cli.main(["metrics", "--watch"]), 130)
        req.assert_called_with("/sock", "GET", "/v1/status")
        self.assertEqual(json.loads(out.getvalue()), {"n": 1})

    def test_metrics_watch_survives_a_failed_sample(self):
        sensors = unittest.mock.Mock()
        sensors.return_value.sample.side_effect = [None, OSError("boom"), {"n": 2}, KeyboardInterrupt]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(m_cli, "Backend", return_value=unittest.mock.Mock(remote=False, notice="")), \
                patch.object(m_cli, "Sensors", sensors), patch.object(m_cli.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(m_cli.main(["metrics", "--watch"]), 130)
        self.assertEqual(json.loads(out.getvalue()), {"n": 2})
        self.assertIn("boom", err.getvalue())

    def test_metrics_watch_stops_quietly_on_a_broken_pipe(self):
        sensors = unittest.mock.Mock()
        sensors.return_value.sample.return_value = {"n": 1}
        err = io.StringIO()
        with patch.object(m_cli, "Backend", return_value=unittest.mock.Mock(remote=False, notice="")), \
                patch.object(m_cli, "Sensors", sensors), patch.object(m_cli.time, "sleep"), \
                patch("builtins.print", side_effect=BrokenPipeError), contextlib.redirect_stderr(err):
            self.assertEqual(m_cli.main(["metrics", "--watch"]), 0)
        self.assertEqual(sensors.return_value.sample.call_count, 2)

    def test_metrics_watch_exits_cleanly_when_the_reader_closes(self):
        root = REPO
        proc = subprocess.Popen([sys.executable, str(root / "loqctl.py"), "metrics", "--watch", "1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            proc.stdout.readline()
            proc.stdout.close()
            err = proc.stderr.read()
            code = proc.wait(timeout=5)
        finally:
            proc.kill()
            proc.stderr.close()
        self.assertEqual(code, 0)
        self.assertNotIn("Exception ignored", err)

    def test_metrics_watch_reports_any_sample_exception_and_continues(self):
        sensors = unittest.mock.Mock()
        sensors.return_value.sample.side_effect = [None, IndexError("bad"), {"n": 2}, KeyboardInterrupt]
        out, err = io.StringIO(), io.StringIO()
        with patch.object(m_cli, "Backend", return_value=unittest.mock.Mock(remote=False, notice="")), \
                patch.object(m_cli, "Sensors", sensors), patch.object(m_cli.time, "sleep"), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(m_cli.main(["metrics", "--watch"]), 130)
        self.assertEqual(json.loads(out.getvalue()), {"n": 2})
        self.assertEqual(err.getvalue().strip(), "loqctl: bad")

    def test_doctor_reports_each_check_without_writes(self):
        proc = self.root / "proc"
        proc.mkdir()
        (proc / "cpuinfo").write_text("vendor_id\t: AuthenticAMD\n")
        (self.root / "class/dmi/id").mkdir(parents=True)
        (self.root / "class/dmi/id/product_name").write_text("LOQ 15IRX9")
        lines = doctor(self.root, proc=proc, dev=self.root / "dev")
        states = {}
        for line in lines:
            state, _, rest = line.partition(" ")
            if state in ("ok", "missing", "n/a"):
                states[rest.strip().split(" (")[0]] = state
        self.assertIn("product: LOQ 15IRX9", lines)
        self.assertEqual(states["firmware-attributes"], "ok")
        self.assertEqual(states["platform-profile custom"], "ok")
        self.assertEqual(states["legion_laptop hwmon"], "missing")
        self.assertEqual(states["debugfs capdata"], "missing")
        self.assertEqual(states["msr"], "n/a")
        self.assertEqual(len(states), 11)
        self.assertEqual(states["GPU mode"], "n/a")

    def test_doctor_msr_unreadable_is_missing(self):
        proc = self.root / "proc"
        proc.mkdir()
        (proc / "cpuinfo").write_text("vendor_id\t: GenuineIntel\n")
        msr = self.root / "dev/cpu/0/msr"
        msr.parent.mkdir(parents=True)
        msr.write_text("")
        with patch("loqctl.doctor.os.access", return_value=False):
            lines = doctor(self.root, proc=proc, dev=self.root / "dev")
        self.assertTrue(any(l.startswith("missing") and "msr needs root" in l for l in lines))

    def test_doctor_subcommand_prints_lines(self):
        out = io.StringIO()
        with patch("loqctl.doctor.doctor", return_value=["x ok"]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(m_cli.main(["doctor"]), 0)
        self.assertEqual(out.getvalue(), "x ok\n")

    def test_entry_script_runs_isolated(self):
        with tempfile.TemporaryDirectory() as cwd:
            ver = subprocess.run([sys.executable, "-I", str(REPO / "loqctl.py"), "--version"],
                                 capture_output=True, text=True, cwd=cwd)
        self.assertEqual(ver.returncode, 0, ver.stderr)
        self.assertEqual(ver.stdout.strip(), loqctl.VERSION)


class KbCliTests(unittest.TestCase):
    def run_cli(self, *argv):
        """kb_cli with a missing config; returns the legionaura argv it would run."""
        calls = []
        tmp = helpers.make_root(self)

        class KB(m_keyboard.Keyboard):
            def __init__(self):
                super().__init__(binary="legionaura", config=tmp / "none.json")

            def _run(self, args):
                calls.append(list(args))
                return "OK"
        with patch.object(m_keyboard, "Keyboard", KB), patch.object(shutil, "which", return_value=None):
            result = m_keyboard.kb_cli(list(argv))
        return calls[-1] if calls else None, result

    def test_effect_arguments(self):
        cases = {
            ("static", "#FF0000", "--brightness", "1"): ["static", *["ff0000"] * 4, "--brightness", "1"],
            ("breath", "ff0000", "00ff00", "--speed", "9"):
                ["breath", "ff0000", *["00ff00"] * 3, "--speed", "4", "--brightness", "2"],
            ("wave", "rtl", "--speed", "2"): ["wave", "rtl", "--speed", "2", "--brightness", "2"],
            ("hue", "--brightness", "5"): ["hue", "--speed", "1", "--brightness", "2"],
            ("off",): ["off"],
            ("brightness", "0"): ["--brightness", "1"],
        }
        for argv, expected in cases.items():
            self.assertEqual(self.run_cli(*argv)[0], expected, argv)
        args, result = self.run_cli()
        self.assertIsNone(args)
        self.assertIsNone(result["zones"])

    def test_status_without_config_lists_only_fields_sent(self):
        sent = {
            ("brightness", "1"): {"brightness": 1},
            ("off",): {"effect": "off"},
            ("wave", "rtl"): {"effect": "wave", "wave_dir": "rtl", "speed": 1, "brightness": 2},
        }
        for argv, fields in sent.items():
            result = self.run_cli(*argv)[1]
            got = {k: result[k] for k in ("effect", "wave_dir", "speed", "brightness", "zones")
                   if result[k] is not None}
            self.assertEqual(got, fields, argv)

    def test_bad_brightness_is_a_control_error(self):
        with self.assertRaises(m_common.ControlError):
            self.run_cli("brightness", "abc")

    def test_output_reports_settings_sent_not_config_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"effect": "breath", "zones": ["00ff00"] * 4}))

            class KB(m_keyboard.Keyboard):
                def __init__(self):
                    super().__init__(binary="legionaura", config=config)

                def _run(self, args):
                    return "OK"
            with patch.object(m_keyboard, "Keyboard", KB), patch.object(shutil, "which", return_value=None):
                result = m_keyboard.kb_cli(["static", "ff0000"])
        self.assertEqual(result["effect"], "static")
        self.assertEqual(result["zones"], ["ff0000"] * 4)

    def test_bad_arguments_never_run(self):
        for argv in (("static",), ("static", "--brightness", "1"), ("breath", "zzzzzz"),
                     ("wave", "up"), ("hue", "fast"), ("brightness",), ("bogus",)):
            with self.assertRaises(m_common.ControlError, msg=argv):
                self.run_cli(*argv)

    def test_timeout_exits_with_message(self):
        class KB(m_keyboard.Keyboard):
            def __init__(self):
                super().__init__(binary="legionaura", config="/nonexistent/loqctl.json")
        timeout = subprocess.TimeoutExpired(["legionaura"], 8)
        stderr = io.StringIO()
        with patch.object(m_keyboard, "Keyboard", KB), \
                patch.object(shutil, "which", return_value="/opt/bin/legionaura"), \
                patch.object(subprocess, "run", side_effect=timeout), contextlib.redirect_stderr(stderr):
            self.assertEqual(m_cli.main(["kb", "static", "ff0000"]), 1)
        self.assertEqual(stderr.getvalue(), "loqctl: legionaura did not answer within 8 s.\n")


class CompatibilityTests(unittest.TestCase):
    def test_sources_parse_as_python_310(self):
        import ast
        banned = {"tomllib", "ExceptionGroup", "itertools.batched", "typing.Self", "datetime.UTC"}
        for path in [REPO / "loqctl.py", *sorted((REPO / "loqctl").glob("*.py"))]:
            tree = ast.parse(path.read_text(), str(path), feature_version=(3, 10))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = {alias.name for alias in node.names}
                elif isinstance(node, ast.ImportFrom):
                    names = {node.module} | {f"{node.module}.{alias.name}" for alias in node.names}
                elif isinstance(node, ast.Name):
                    names = {node.id}
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    names = {f"{node.value.id}.{node.attr}"}
                else:
                    continue
                self.assertFalse(names & banned, f"{path.name} uses 3.11+ {names & banned}")


if __name__ == "__main__":
    unittest.main()
