# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Local or remote data source for the CLI and dashboard."""
from __future__ import annotations

import os
import threading
import time

from .api import http_client, request, socket_alive
from .capdata import fetch_via_sudo, load_cache, save_cache
from .common import ControlError, socket_path
from .hardware import Hardware
from .sensors import Sampler, Sensors

class Backend:
    def __init__(self, path=None):
        self.path = path or socket_path()
        self.notice = ""
        self.remote = socket_alive(self.path)
        if not self.remote and os.path.lexists(self.path):
            self.notice = f"Ignored stale API socket {self.path}; using local access."
        elif not self.remote and os.path.isdir(directory := os.path.dirname(self.path)) and not os.access(directory, os.X_OK):
            self.notice = f"Cannot reach {self.path}: no access to its directory; using local access."
        self.hardware = Hardware()
        self.hardware.on_new_base = lambda: self.load_mode_limits(allow_sudo=False)
        self.sampler = None
        self.latest = {}
        self.stop = threading.Event()
        self.wake = threading.Event()

    def load_mode_limits(self, allow_sudo=True):
        """Local non-root reads: fill the stock-mode table from the cache, else one sudo fetch."""
        hardware = self.hardware
        if self.remote or os.geteuid() == 0 or hardware.base is None or hardware.mode_limits()[0]:
            return
        table, reason = load_cache(hardware.root), "needs root"
        if table is None and allow_sudo:
            table, reason = fetch_via_sudo()
            if table:
                save_cache(table, hardware.root)
        hardware.set_mode_limits(table, reason)

    def snapshot(self):
        if self.remote:
            return request(self.path, "GET", "/v1/status")
        sensors = Sensors()
        sensors.sample()
        time.sleep(0.25)
        return {**self.hardware.capabilities(), "metrics": sensors.sample()}

    def start(self):
        if not self.remote:
            self.sampler = Sampler(self.hardware).start()
        else:
            def poll():
                while not self.stop.is_set():
                    try:
                        self.latest = request(self.path, "GET", "/v1/status")
                    except (OSError, ControlError, ValueError, http_client.HTTPException) as exc:
                        self.latest = {**{k: v for k, v in self.latest.items() if k != "metrics"}, "error": "API connection failed: " + str(exc)}
                    self.wake.wait(1)
                    self.wake.clear()
            threading.Thread(target=poll, daemon=True).start()

    def state(self):
        return self.sampler.latest if self.sampler else self.latest

    def refresh(self):
        """Ask for a fresh firmware read soon, e.g. right after a write."""
        if self.sampler:
            self.sampler.refresh()
        else:
            self.wake.set()

    def controls(self):
        """Read firmware now, without using the telemetry cache."""
        return request(self.path, "GET", "/v1/capabilities") if self.remote else self.hardware.capabilities()

    def write(self, key, value):
        if self.remote:
            endpoint = "/v1/mode" if key == "mode" else "/v1/attributes/" + key
            return request(self.path, "POST", endpoint, {"mode" if key == "mode" else "value": value})
        return self.hardware.set_mode(value) if key == "mode" else self.hardware.set_value(key, value)

    def fancurve(self):
        return request(self.path, "GET", "/v1/fancurve") if self.remote else self.hardware.fancurve()

    def write_fancurve(self, points):
        if self.remote:
            return request(self.path, "POST", "/v1/fancurve", {"points": points})
        return self.hardware.write_fancurve(points)

    def close(self):
        self.stop.set()
        self.wake.set()
        if self.sampler:
            self.sampler.halt()
