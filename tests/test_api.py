# SPDX-License-Identifier: GPL-3.0-or-later
import contextlib
import io
import os
import socket
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import helpers
from loqctl import api as m_api
from loqctl import backend as m_backend
from loqctl import common as m_common
from loqctl import sensors as m_sensors


class ApiTests(helpers.SysfsCase):
    def test_unix_api(self):
        sampler = type("Sampler", (), {"latest": {"mode": "custom", "metrics": {"cpu": {"power_w": 12}}}})()
        path = str(self.root / "test.sock")
        server = m_api.APIServer(path, self.hw, sampler, os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            self.assertEqual(m_api.request(path, "GET", "/v1/status")["mode"], "custom")
            self.assertEqual(m_api.request(path, "GET", "/v1/metrics")["cpu"]["power_w"], 12)
            self.assertTrue(m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 70})["verified"])
            for payload in ({"value": True}, {"value": 500}, {"value": 50, "path": "/tmp/x"}, []):
                with self.assertRaises(m_common.ControlError):
                    m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", payload)
            m_api.request(path, "POST", "/v1/mode", {"mode": "balanced"})
            with self.assertRaises(m_common.ControlError):
                m_api.request(path, "POST", "/v1/attributes/ppt_pl1_spl", {"value": 60})
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class HandlerErrorTests(helpers.SysfsCase):
    def test_unexpected_error_gets_500_without_message(self):
        sampler = type("Sampler", (), {"latest": {"metrics": {"x": float("nan")}}})()
        path = str(self.root / "err.sock")
        server = m_api.APIServer(path, self.hw, sampler, os.getuid())
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            err = io.StringIO()
            with patch.object(sys, "stderr", err), self.assertRaises(m_common.ControlError) as cm:
                m_api.request(path, "GET", "/v1/status")
            self.assertEqual(str(cm.exception), "ValueError")
            with patch.object(self.hw, "set_mode", side_effect=OSError("secret path")), \
                    patch.object(sys, "stderr", err), self.assertRaises(m_common.ControlError) as cm:
                m_api.request(path, "POST", "/v1/mode", {"mode": "custom"})
            self.assertEqual(str(cm.exception), "OSError")
            self.assertNotIn("secret", err.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


class ServerSamplerTests(unittest.TestCase):
    def setUp(self):
        self.root = helpers.make_root(self)

    def stale_socket(self, name="s.sock"):
        path = str(self.root / name)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(path)
        sock.close()
        return path

    def test_socket_alive(self):
        self.assertFalse(m_api.socket_alive(self.root / "missing"))
        regular = self.root / "file"
        regular.write_text("x")
        self.assertFalse(m_api.socket_alive(regular))
        self.assertFalse(m_api.socket_alive(self.stale_socket()))
        live = self.root / "live.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(live))
        server.listen(1)
        self.assertTrue(m_api.socket_alive(live))

    def test_socket_dir_is_traversable_under_any_umask(self):
        old = os.umask(0o077)
        self.addCleanup(os.umask, old)
        fresh = self.root / "fresh"
        m_api.prepare_socket_dir(fresh)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o711)
        fresh.chmod(0o700)
        with patch.object(Path, "lstat", return_value=Mock(st_uid=0, st_mode=0o40700)):
            m_api.prepare_socket_dir(fresh)
        self.assertEqual(fresh.stat().st_mode & 0o777, 0o711)

    def test_backend_notice_when_socket_dir_unreachable(self):
        directory = self.root / "run"
        directory.mkdir()
        directory.chmod(0o600)
        self.addCleanup(directory.chmod, 0o700)
        with patch.object(m_backend.os, "access", return_value=False):
            backend = m_backend.Backend(directory / "control.sock")
        self.assertFalse(backend.remote)
        self.assertIn("no access to its directory", backend.notice)
        self.assertEqual(m_backend.Backend(self.root / "none" / "control.sock").notice, "")

    def test_backend_ignores_stale_socket(self):
        path = self.stale_socket()
        backend = m_backend.Backend(path)
        self.assertFalse(backend.remote)
        self.assertIn("Ignored stale API socket", backend.notice)
        self.assertEqual(m_backend.Backend(str(self.root / "none")).notice, "")

    def test_serve_unlink_rules(self):
        regular = self.root / "file"
        regular.write_text("x")
        with self.assertRaises(m_common.ControlError):
            m_api.prepare_socket_path(regular)
        self.assertTrue(regular.exists())
        stale = self.stale_socket()
        link = self.root / "link"
        link.symlink_to(stale)
        with self.assertRaises(m_common.ControlError):
            m_api.prepare_socket_path(link)
        self.assertTrue(link.is_symlink())
        self.assertTrue(Path(stale).exists())
        m_api.prepare_socket_path(self.root / "absent")
        with contextlib.redirect_stdout(io.StringIO()) as out:
            m_api.prepare_socket_path(stale)
        self.assertFalse(Path(stale).exists())
        self.assertIn("Removed stale socket", out.getvalue())
        live = self.root / "live.sock"
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(server.close)
        server.bind(str(live))
        server.listen(1)
        with self.assertRaisesRegex(m_common.ControlError, "already running"):
            m_api.prepare_socket_path(live)
        self.assertTrue(live.exists())

    def sampler(self, hardware):
        sampler = m_sensors.Sampler.__new__(m_sensors.Sampler)
        sampler.hardware = hardware
        sampler.sensors = Mock()
        sampler.sensors.sample.return_value = {"cpu": {}}
        sampler.latest, sampler.caps, sampler.metrics = {}, {}, {}
        sampler.fw_error, sampler.next_firmware = None, None
        sampler.force = threading.Event()
        return sampler

    def test_sampler_keeps_last_good_caps(self):
        hardware = Mock()
        hardware.capabilities.side_effect = [{"mode": "custom"}, RuntimeError("gone")]
        sampler = self.sampler(hardware)
        sampler.tick()
        self.assertEqual(sampler.latest["mode"], "custom")
        self.assertNotIn("error", sampler.latest)
        sampler.refresh()
        first = sampler.latest
        sampler.tick()
        self.assertEqual(sampler.latest["mode"], "custom")
        self.assertEqual(sampler.latest["error"], "gone")
        self.assertIsNot(sampler.latest, first)
        self.assertEqual(sampler.latest["metrics"], {"cpu": {}})

    def test_sampler_polls_firmware_every_5s_and_on_refresh(self):
        hardware = Mock()
        hardware.capabilities.return_value = {"mode": "custom"}
        sampler = self.sampler(hardware)
        with patch.object(time, "monotonic") as clock:
            clock.return_value = 100.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 1)
            clock.return_value = 101.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 1)
            clock.return_value = 105.0
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 2)
            clock.return_value = 106.0
            sampler.refresh()
            sampler.tick()
            self.assertEqual(hardware.capabilities.call_count, 3)


if __name__ == "__main__":
    unittest.main()
