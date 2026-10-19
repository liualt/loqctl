# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Local HTTP API over a Unix socket."""
from __future__ import annotations

import json
import os
from pathlib import Path
import pwd
import signal
import socket
import stat
import struct
import sys

from . import VERSION
from .common import ControlError, _lazy
from .sensors import Sampler

http_client = _lazy("http.client")
http_server = _lazy("http.server")
socketserver = _lazy("socketserver")

def socket_alive(path):
    """True only when path is a Unix socket that accepts a connection."""
    try:
        if not stat.S_ISSOCK(os.stat(path).st_mode):
            return False
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
        return True
    except OSError:
        return False


_API = None


def _api_classes():
    global _API
    if _API is None:
        class UnixHTTP(http_client.HTTPConnection):
            def __init__(self, path):
                super().__init__("localhost", timeout=4)
                self.path = path

            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                self.sock.connect(self.path)


        class APIServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

            def __init__(self, path, hardware, sampler, uid):
                self.hardware, self.sampler, self.uid = hardware, sampler, uid
                super().__init__(path, Handler)

            def verify_request(self, connection, address):
                _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                return uid in (0, self.uid)


        class Handler(http_server.BaseHTTPRequestHandler):
            server_version = "loqctl/" + VERSION

            def setup(self):
                super().setup()
                self.connection.settimeout(3)

            def log_message(self, *args):
                pass

            def reply(self, status, value):
                data = json.dumps(value, allow_nan=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/v1/status":
                    self.reply(200, self.server.sampler.latest)
                elif self.path == "/v1/metrics":
                    self.reply(200, self.server.sampler.latest.get("metrics", {}))
                elif self.path == "/v1/capabilities":
                    self.reply(200, self.server.hardware.capabilities())
                elif self.path == "/v1/fancurve":
                    self.reply(200, self.server.hardware.fancurve())
                else:
                    self.reply(404, {"error": "Unknown endpoint"})

            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.headers.get("Transfer-Encoding"):
                        raise ControlError("Provide a JSON body of at most 4096 bytes.")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ControlError("Expected a JSON object.")
                    if self.path == "/v1/mode" and set(body) == {"mode"}:
                        value = self.server.hardware.set_mode(body["mode"])
                    elif self.path.startswith("/v1/attributes/") and set(body) == {"value"}:
                        value = self.server.hardware.set_value(self.path[len("/v1/attributes/"):], body["value"])
                    elif self.path == "/v1/fancurve" and set(body) == {"points"}:
                        value = self.server.hardware.write_fancurve(body["points"])
                    else:
                        self.reply(404, {"error": "Unknown endpoint or unexpected fields"})
                        return
                    self.reply(200, value)
                    refresh = getattr(self.server.sampler, "refresh", None)
                    if refresh:
                        refresh()
                except (ControlError, ValueError, TypeError) as exc:
                    self.reply(400, {"error": str(exc)})
        _API = {"UnixHTTP": UnixHTTP, "APIServer": APIServer, "Handler": Handler}
    return _API


def __getattr__(name):
    if name in ("UnixHTTP", "APIServer", "Handler"):
        return _api_classes()[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def request(path, method, endpoint, payload=None):
    conn = _api_classes()["UnixHTTP"](path)
    try:
        body = json.dumps(payload) if payload is not None else None
        conn.request(method, endpoint, body, {"Content-Type": "application/json"} if body else {})
        response = conn.getresponse()
        result = json.loads(response.read())
        if response.status >= 400:
            raise ControlError(result.get("error", str(response.status)))
        return result
    finally:
        conn.close()


def serve_uid_warning(uid, environ):
    if "SUDO_UID" not in environ and uid == 0:
        return "Warning: serving for uid 0 because SUDO_UID is not set. Use --uid <your uid> so your user can connect."
    return None


def prepare_socket_path(path):
    """Refuse non-sockets and live servers; remove a stale socket left by a crash."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISSOCK(info.st_mode):
        raise ControlError(f"{path} exists and is not a socket. Remove it manually.")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            probe.connect(str(path))
    except (ConnectionRefusedError, FileNotFoundError):
        path.unlink(missing_ok=True)
        print(f"Removed stale socket {path}.", flush=True)
        return
    except OSError as exc:
        raise ControlError(f"Cannot check existing socket {path}: {exc}") from exc
    raise ControlError(f"A loqctl server is already running on {path}.")


def serve(uid):
    if os.geteuid() != 0:
        raise ControlError("Start the server with sudo so it can write firmware settings.")
    pwd.getpwuid(uid)
    directory = Path(f"/run/loqctl-{uid}")
    try:
        directory.mkdir(mode=0o711)
    except FileExistsError:
        info = directory.lstat()
        if directory.is_symlink() or not directory.is_dir() or info.st_uid != 0 or info.st_mode & 0o022:
            raise ControlError("Socket directory must be owned by root and not writable by other users.")
    path = directory / "control.sock"
    prepare_socket_path(path)
    warning = serve_uid_warning(uid, os.environ)
    if warning:
        print(warning, file=sys.stderr)
    sampler = Sampler().start()
    old_mask = os.umask(0o177)
    try:
        server = _api_classes()["APIServer"](str(path), sampler.hardware, sampler, uid)
    finally:
        os.umask(old_mask)
    os.chown(path, uid, -1)
    os.chmod(path, 0o600)
    print(f"LOQ API listening on {path}. Ctrl+C stops the server.", flush=True)
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.halt()
        server.server_close()
        path.unlink(missing_ok=True)
