# SPDX-License-Identifier: GPL-3.0-or-later
import json
import shutil
import subprocess
import tempfile
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import colors as m_colors
from loqctl import common as m_common
from loqctl import keyboard as m_keyboard


class KeyboardWorkerTests(unittest.TestCase):
    def kb(self, config=None):
        return m_keyboard.Keyboard(binary="/usr/bin/false", config=config or "/nonexistent/loqctl.json")

    def test_keyboard_command_matches_effects(self):
        kb = self.kb()
        kb.zones = ["ff0000", "00ff00", "0000ff", "ffffff"]
        kb.speed, kb.brightness, kb.wave_dir = 2, 1, "rtl"
        expected = {
            "off": ["off"],
            "static": ["static", *kb.zones, "--brightness", "1"],
            "breath": ["breath", *kb.zones, "--speed", "2", "--brightness", "1"],
            "wave": ["wave", "rtl", "--speed", "2", "--brightness", "1"],
            "hue": ["hue", "--speed", "2", "--brightness", "1"],
        }
        for effect, argv in expected.items():
            kb.effect = effect
            self.assertEqual(kb.command(), argv)
        kb.effect = "bogus"
        with self.assertRaises(m_common.ControlError):
            kb.command()

    def test_load_skipped_while_dirty(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"zones": ["aaaaaa"] * 4}))
            kb = self.kb(config)
            kb.zones = ["bbbbbb"] * 4
            kb.dirty = True
            kb.load()
            self.assertEqual(kb.zones, ["bbbbbb"] * 4)
            kb.load(force=True)
            self.assertEqual(kb.zones, ["aaaaaa"] * 4)

    def test_keyboard_worker_latest_wins_single_process(self):
        kb = self.kb()
        release, started = threading.Event(), threading.Event()
        calls, state = [], {"now": 0, "max": 0}

        def fake_run(args):
            state["now"] += 1
            state["max"] = max(state["max"], state["now"])
            calls.append(args[0])
            if len(calls) == 1:
                started.set()
                release.wait(2)
            state["now"] -= 1
            return "OK"

        kb._run = fake_run
        kb.dirty = True
        kb.submit([1], "one")
        self.assertTrue(started.wait(2))
        for n in (2, 3, 4):
            kb.submit([n], str(n))
        self.assertTrue(kb.busy)
        release.set()
        deadline = time.monotonic() + 2
        while kb.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(calls, [1, 4])
        self.assertEqual(state["max"], 1)
        self.assertIn("(4)", kb.last_message)
        self.assertEqual(kb.last_seq, 2)
        self.assertFalse(kb.dirty)
        kb.close()

    def test_dirty_kept_for_edits_after_submit(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({"zones": ["aaaaaa"] * 4}))
            kb = self.kb(config)
            release, started = threading.Event(), threading.Event()

            def fake_run(args):
                started.set()
                release.wait(2)
                return "OK"

            kb._run = fake_run
            kb.mark_dirty()
            kb.submit(["a"], "a")
            self.assertTrue(started.wait(2))
            kb.zones = ["bbbbbb"] * 4
            kb.mark_dirty()
            release.set()
            deadline = time.monotonic() + 2
            while kb.busy and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(kb.dirty)
            kb.load()
            self.assertEqual(kb.zones, ["bbbbbb"] * 4)
            kb.close()

    def test_worker_survives_oserror(self):
        kb = self.kb()
        results = [OSError("boom"), "OK"]

        def fake_run(args):
            r = results.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

        kb._run = fake_run
        kb.submit(["a"], "a")
        deadline = time.monotonic() + 2
        while kb.last_seq < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertIn("boom", kb.last_message)
        kb.submit(["b"], "b")
        while kb.last_seq < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(kb.last_message, "Keyboard applied (b). OK")
        kb.close()

    def test_close_stops_worker(self):
        kb = self.kb()
        kb._run = lambda args: "OK"
        kb.submit(["a"], "a")
        thread = kb._thread
        kb.close()
        thread.join(2)
        self.assertFalse(thread.is_alive())


class KeyboardConfigTests(helpers.SysfsCase):
    def test_keyboard_color_helpers(self):
        self.assertEqual(m_colors.rgb_to_hex(242, 118, 10), "f2760a")
        self.assertEqual(m_colors.hex_to_rgb("f2760a"), (242, 118, 10))
        with self.assertRaises(m_common.ControlError):
            m_colors.hex_to_rgb("gg0000")
        h, s, v = m_colors.rgb_to_hsv(255, 0, 0)
        self.assertAlmostEqual(h, 0, delta=0.1)
        self.assertGreater(s, 99)
        self.assertGreater(v, 99)
        self.assertEqual(m_colors.rgb_to_hex(*m_colors.hsv_to_rgb(120, 100, 100)), "00ff00")

    def test_keyboard_status_loads_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text(json.dumps({
                "effect": "breath", "speed": 3, "brightness": 1,
                "waveDir": "rtl", "zones": ["ff0000", "00ff00", "0000ff", "ffffff"],
            }))
            kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
            status = kb.status()
            self.assertEqual(status["effect"], "breath")
            self.assertEqual(status["speed"], 3)
            self.assertEqual(status["brightness"], 1)
            self.assertEqual(status["wave_dir"], "rtl")
            self.assertEqual(status["zones"], ["ff0000", "00ff00", "0000ff", "ffffff"])
            self.assertEqual(status["problem"], "")

    def test_keyboard_load_tolerates_bad_config(self):
        config = self.root / "kb.json"
        for text in ('[1, 2]', '{"speed": "fast", "brightness": [], "zones": [5, "zzz", "ff0000"], "effect": "nope"}',
                     '{"zones": 7}'):
            config.write_text(text)
            kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
            kb.load(force=True)
            self.assertEqual((kb.effect, kb.speed, kb.brightness), ("static", 1, 2))
            self.assertEqual(len(kb.zones), 4)
        config.write_text('{"zones": [5, "ff0000", "00ff00", "0000ff"]}')
        kb.load(force=True)
        self.assertEqual(kb.zones, ["ffffff"] * 4)
        self.assertNotIn("zones", kb.from_config)

    def test_status_is_null_without_usable_config(self):
        config = self.root / "kb.json"
        for text in (None, "{bad", "[1]"):
            if text is not None:
                config.write_text(text)
            kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
            status = kb.status()
            for key in ("effect", "speed", "brightness", "wave_dir", "zones"):
                self.assertIsNone(status[key])
            self.assertTrue(status["problem"])
        kb.mark_dirty()
        self.assertEqual(kb.status()["effect"], "static")
        self.assertTrue(kb.status()["problem"])

    def test_status_reports_only_fields_the_config_holds(self):
        config = self.root / "kb.json"
        config.write_text("{}")
        kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
        self.assertEqual(kb.from_config, set())
        self.assertEqual(kb.status()["problem"], "config is missing: effect, speed, brightness, wave_dir, zones")
        config.write_text('{"effect": "nope", "zones": [], "brightness": 1}')
        kb.load()
        status = kb.status()
        self.assertEqual(status["brightness"], 1)
        for key in ("effect", "speed", "wave_dir", "zones"):
            self.assertIsNone(status[key])

    def test_status_problem_is_short_and_follows_the_latest_load(self):
        config = self.root / "kb.json"
        kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
        self.assertEqual(kb.status()["problem"], "no config yet")
        config.write_text('{"effect": "wave"}')
        kb.load()
        self.assertEqual(kb.status()["effect"], "wave")
        self.assertIn("speed", kb.status()["problem"])
        config.unlink()
        kb.load()
        status = kb.status()
        self.assertIsNone(status["effect"])
        self.assertEqual(status["problem"], "no config yet")
        self.assertNotIn(str(self.root), status["problem"])

    def test_invalid_values_are_not_known(self):
        config = self.root / "kb.json"
        config.write_text(json.dumps({
            "effect": "static", "speed": 0, "brightness": None, "waveDir": "up",
            "zones": ["ff0000", "00ff00"],
        }))
        kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=config)
        self.assertEqual(kb.from_config, {"effect"})
        self.assertEqual(kb.status()["problem"], "config is missing: speed, brightness, wave_dir, zones")
        config.write_text('{"speed": 9, "zones": ["ff0000", "x", "00ff00", "0000ff"]}')
        kb.load()
        self.assertEqual(kb.from_config, set())

    def test_edited_fields_survive_a_missing_config(self):
        kb = m_keyboard.Keyboard(binary="/usr/bin/false", config=self.root / "none.json")
        kb.brightness = 1
        kb.mark_dirty("brightness")
        kb.dirty = False  # as after a successful apply
        kb.load()
        status = kb.status()
        self.assertEqual(status["brightness"], 1)
        self.assertIsNone(status["effect"])
        self.assertEqual(status["problem"], "no config yet")

    def test_kb_apply_runs_legionaura_apply(self):
        kb = m_keyboard.Keyboard(binary="legionaura", config=self.root / "none.json")
        with patch.object(m_keyboard, "Keyboard", return_value=kb), patch.object(kb, "_run", return_value="OK") as run:
            result = m_keyboard.kb_cli(["apply"])
        run.assert_called_once_with(["apply"])
        self.assertEqual(result["result"], "OK")

    def test_legionaura_resolved_via_path(self):
        kb = m_keyboard.Keyboard(binary="legionaura", config="/nonexistent/loqctl.json")
        run = Mock(return_value=Mock(returncode=0, stdout="ok", stderr=""))
        with patch.object(shutil, "which", return_value="/opt/bin/legionaura"), \
                patch.object(subprocess, "run", run):
            self.assertEqual(kb._run(["off"]), "ok")
            self.assertEqual(kb.status()["binary"], "/opt/bin/legionaura")
        self.assertEqual(run.call_args[0][0], ["/opt/bin/legionaura", "off"])
        with patch.object(shutil, "which", return_value=None):
            with self.assertRaisesRegex(m_common.ControlError, "not found"):
                kb._run(["off"])

    def test_legionaura_timeout_is_control_error(self):
        kb = m_keyboard.Keyboard(binary="legionaura", config="/nonexistent/loqctl.json")
        timeout = subprocess.TimeoutExpired(["legionaura", "off"], 8)
        with patch.object(shutil, "which", return_value="/opt/bin/legionaura"), \
                patch.object(subprocess, "run", side_effect=timeout):
            with self.assertRaisesRegex(m_common.ControlError, "did not answer within 8 s"):
                kb._run(["off"])


if __name__ == "__main__":
    unittest.main()
