#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""LOQ control and telemetry. Python standard library only."""
from __future__ import annotations

import importlib.util
import collections
import errno
import json
import math
import os
from pathlib import Path
import pwd
import signal
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time



def _lazy(name):
    """Import a module on first attribute access to keep CLI startup cheap."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.find_spec(name)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader = importlib.util.LazyLoader(spec.loader)
    loader.exec_module(module)
    parent, _, child = name.rpartition(".")
    if parent:
        setattr(sys.modules[parent], child, module)
    return module


argparse = _lazy("argparse")
colorsys = _lazy("colorsys")
csv = _lazy("csv")
curses = _lazy("curses")
http_client = _lazy("http.client")
http_server = _lazy("http.server")
socketserver = _lazy("socketserver")
textwrap = _lazy("textwrap")

VERSION = "0.2.0"
ENTRY = str(Path(__file__).resolve())
LEGIONAURA = "/usr/bin/legionaura"
LEGIONAURA_CONFIG = Path.home() / ".config/legionaura/config.json"
# Explicit write allowlist. Other firmware attributes are never writable here.
FIELDS = {
    "ppt_pl1_spl": ("CPU sustained / PL1", "W"),
    "ppt_pl2_sppt": ("CPU boost / PL2", "W"),
    "ppt_cpu_cl": ("CPU cross-load", "W"),
    "ppt_pl1_tau": ("CPU boost duration", "s"),
    "cpu_temp": ("CPU temperature limit", "°C"),
    "gpu_nv_ctgp": ("GPU configurable TGP", "W"),
    "gpu_nv_ppab": ("GPU dynamic boost", "W"),
    "gpu_nv_ac_offset": ("GPU AC power offset", "W"),
    "gpu_temp": ("GPU temperature limit", "°C"),
}
SPARK = "▁▂▃▄▅▆▇█"
BAR_FILL = "█"
BAR_EMPTY = "░"
# Terminal ANSI palette only (never init_color / truecolor UI chrome).
# Keys 0..7 equal curses.COLOR_BLACK..COLOR_WHITE.
ANSI_RGB = {
    0: (0, 0, 0),
    1: (205, 49, 49),
    2: (13, 188, 121),
    3: (229, 229, 16),
    4: (36, 114, 200),
    5: (188, 63, 188),
    6: (17, 168, 205),
    7: (229, 229, 229),
}


class ControlError(Exception):
    pass


def read(path, default=None):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return default


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def scaled(path, divisor=1):
    value = number(read(path))
    return value / divisor if value is not None else None


def owner_uid():
    return int(os.environ.get("SUDO_UID", os.getuid()))


def socket_path():
    return f"/run/loqctl-{owner_uid()}/control.sock"


def fmt(value, unit="", decimals=0):
    return "N/A" if value is None else f"{value:.{decimals}f}{unit}"


def clamp(value, lo, hi):
    return max(lo, min(hi, value))


def strike(text):
    return "".join(c + "\u0336" for c in str(text))


def hex_to_rgb(value):
    text = (value or "").strip().lstrip("#").lower()
    if len(text) != 6 or any(c not in "0123456789abcdef" for c in text):
        raise ControlError(f"Color must be RRGGBB, got {value!r}.")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def rgb_to_hex(r, g, b):
    return f"{clamp(int(r), 0, 255):02x}{clamp(int(g), 0, 255):02x}{clamp(int(b), 0, 255):02x}"


def rgb_to_hsv(r, g, b):
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return h * 360, s * 100, v * 100


def hsv_to_rgb(h, s, v):
    r, g, b = colorsys.hsv_to_rgb((h % 360) / 360, clamp(s, 0, 100) / 100, clamp(v, 0, 100) / 100)
    return int(round(r * 255)), int(round(g * 255)), int(round(b * 255))


def nearest_ansi(r, g, b):
    best, distance = curses.COLOR_WHITE, None
    for color, (cr, cg, cb) in ANSI_RGB.items():
        if color == curses.COLOR_BLACK:
            continue
        dist = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if distance is None or dist < distance:
            best, distance = color, dist
    return best


class Theme:
    """UI colors from the terminal palette only. Never redefines RGB."""

    ACCENT = 1
    OK = 2
    WARN = 3
    BAD = 4
    GPU = 5
    INFO = 6
    TEXT = 7

    def __init__(self, enabled):
        self.enabled = enabled
        if not enabled:
            return
        curses.start_color()
        curses.use_default_colors()
        for pair, fg in (
            (self.ACCENT, curses.COLOR_CYAN),
            (self.OK, curses.COLOR_GREEN),
            (self.WARN, curses.COLOR_YELLOW),
            (self.BAD, curses.COLOR_RED),
            (self.GPU, curses.COLOR_MAGENTA),
            (self.INFO, curses.COLOR_BLUE),
            (self.TEXT, curses.COLOR_WHITE),
        ):
            curses.init_pair(pair, fg, -1)

    def attr(self, color=0, bold=False, dim=False):
        if not self.enabled or not color:
            base = 0
        else:
            base = curses.color_pair(color)
        if bold:
            base |= curses.A_BOLD
        if dim:
            base |= curses.A_DIM
        return base

    def heat(self, value, cool=60, hot=85):
        if value is None:
            return self.TEXT
        if value < cool:
            return self.OK
        if value < hot:
            return self.WARN
        return self.BAD

    def load(self, value):
        if value is None:
            return self.TEXT
        if value < 50:
            return self.OK
        if value < 80:
            return self.WARN
        return self.BAD

    def ansi_pair(self, ansi_color):
        """Map a raw curses.COLOR_* to our pair index for swatches."""
        return {
            curses.COLOR_RED: self.BAD,
            curses.COLOR_GREEN: self.OK,
            curses.COLOR_YELLOW: self.WARN,
            curses.COLOR_BLUE: self.INFO,
            curses.COLOR_MAGENTA: self.GPU,
            curses.COLOR_CYAN: self.ACCENT,
            curses.COLOR_WHITE: self.TEXT,
        }.get(ansi_color, self.TEXT)


class Hardware:
    def __init__(self, root=Path("/sys")):
        self.root = Path(root)
        self.lock = threading.RLock()
        bases = sorted(self.root.glob("class/firmware-attributes/lenovo-wmi-other*/attributes"))
        self.base = bases[0] if bases else None
        profiles = sorted(self.root.glob("class/platform-profile/*"))
        self.profile = next((p / "profile" for p in profiles
                             if read(p / "name") == "lenovo-wmi-gamezone"),
                            self.root / "firmware/acpi/platform_profile")
        self.choices = self.profile.parent / ("choices" if self.profile.name == "profile"
                                               else "platform_profile_choices")

    def capabilities(self):
        with self.lock:
            attrs = {}
            for key, (label, unit) in FIELDS.items():
                path = self.base / key if self.base else None
                if path is None or not path.is_dir():
                    continue
                item = {"label": label, "unit": unit, "description": read(path / "display_name", label)}
                for name in ("current_value", "min_value", "max_value", "default_value", "scalar_increment"):
                    value = read(path / name)
                    try:
                        item[name] = int(value)
                    except (ValueError, TypeError):
                        item[name] = None
                item["available"] = all(item[n] is not None for n in
                                        ("current_value", "min_value", "max_value", "scalar_increment"))
                attrs[key] = item
            return {"model": read(self.root / "class/dmi/id/product_name", "Lenovo LOQ"),
                    "mode": read(self.profile), "modes": (read(self.choices, "") or "").split(),
                    "attributes": attrs}

    def _write(self, path, value):
        try:
            path.write_text(str(value) + "\n")
        except PermissionError as exc:
            raise ControlError("Permission denied. Use sudo or start the privileged loqctl server.") from exc
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise ControlError("Limits are only editable in custom mode. Select custom first.") from exc
            raise ControlError(f"Firmware rejected the write: {exc.strerror}") from exc
        actual = read(path)
        if actual != str(value):
            raise ControlError(f"Write was sent, but readback is {actual!r}, expected {value}. Refresh before retrying.")
        return actual

    def set_mode(self, mode):
        with self.lock:
            caps = self.capabilities()
            if not isinstance(mode, str) or mode not in caps["modes"]:
                raise ControlError("Unsupported platform mode.")
            actual = self._write(self.profile, mode)
            return {"mode": mode, "previous": caps["mode"], "current": actual, "verified": True}

    def set_value(self, key, value):
        with self.lock:
            caps = self.capabilities()
            if key not in FIELDS or key not in caps["attributes"]:
                raise ControlError("Unknown or unavailable control.")
            if caps["mode"] != "custom":
                raise ControlError("Select custom mode before changing firmware limits.")
            item = caps["attributes"][key]
            if not item["available"]:
                raise ControlError("Firmware did not report usable limits for this control.")
            if type(value) is not int:
                raise ControlError("Value must be an integer.")
            lo, hi = item["min_value"], item["max_value"]
            if not lo <= value <= hi:
                raise ControlError(f"Value must be between {lo} and {hi} {item['unit']}.")
            # Zero means the firmware supplies no step constraint, not a read-only attribute.
            step = item["scalar_increment"]
            if step > 0 and (value - lo) % step:
                raise ControlError(f"Value must advance in steps of {step} from {lo}.")
            actual = self._write(self.base / key / "current_value", value)
            return {"attribute": key, "value": value, "previous": item["current_value"],
                    "current": int(actual), "verified": True}


class Sensors:
    def __init__(self, root=Path("/sys"), proc=Path("/proc")):
        self.root, self.proc = Path(root), Path(proc)
        self.previous_cpu = {}
        self.previous_energy = {}

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

    def cpu_power(self):
        watts, sources, denied = [], [], False
        for path in sorted(self.root.glob("class/powercap/intel-rapl:*")):
            if not (read(path / "name", "") or "").startswith("package-"):
                continue  # exclude nested domains and duplicate MMIO counters
            energy = scaled(path / "energy_uj")
            now = time.monotonic()
            if energy is None:
                denied = True
                continue
            previous = self.previous_energy.get(str(path))
            self.previous_energy[str(path)] = (now, energy)
            if previous:
                delta = energy - previous[1]
                if delta < 0:
                    maximum = scaled(path / "max_energy_range_uj")
                    if maximum is None:
                        continue
                    delta += maximum
                elapsed = now - previous[0]
                if elapsed > 0 and delta >= 0:
                    watts.append(delta / 1_000_000 / elapsed)
                    sources.append(read(path / "name"))
        return (sum(watts) if watts else None,
                "RAPL package energy" if sources else
                "RAPL needs root: run sudo loqctl serve" if denied else "Waiting for RAPL samples or no package counter")

    def gpu(self):
        fields = "name,temperature.gpu,power.draw,power.limit,clocks.gr,clocks.mem,utilization.gpu,memory.used,memory.total"
        try:
            result = subprocess.run(["/usr/bin/nvidia-smi", "--query-gpu=" + fields,
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
            return {"available": bool(devices), "devices": devices}
        except (OSError, subprocess.TimeoutExpired):
            return {"available": False, "error": "NVIDIA sensor query unavailable or timed out"}

    def sample(self):
        usage = self.cpu_usage()
        temperatures, fans = [], []
        for hw in self.root.glob("class/hwmon/hwmon*"):
            name = read(hw / "name", "")
            if name in ("coretemp", "k10temp", "zenpower"):
                temperatures += [v for p in hw.glob("temp*_input") if (v := scaled(p, 1000)) is not None]
            for p in hw.glob("fan*_input"):
                fans.append({"name": read(p.with_name(p.name.replace("_input", "_label")), name + "/" + p.stem),
                             "rpm": scaled(p)})
        mhz = [v for p in self.root.glob("devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq")
               if (v := scaled(p, 1000)) is not None]
        power, power_source = self.cpu_power()
        mem = {}
        for line in (read(self.proc / "meminfo", "") or "").splitlines():
            key, value = line.split(":", 1)
            mem[key] = int(value.split()[0]) / 1024
        total, available = mem.get("MemTotal"), mem.get("MemAvailable")
        battery = []
        ac = []
        for p in self.root.glob("class/power_supply/*"):
            if read(p / "type") == "Battery":
                battery.append({"name": p.name, "percent": scaled(p / "capacity"),
                                "status": read(p / "status"), "power_w": scaled(p / "power_now", 1e6)})
            elif (online := read(p / "online")) is not None:
                ac.append(online == "1")
        return {"timestamp": time.time(),
                "cpu": {"utilization_pct": usage.pop("cpu", None), "cores": usage,
                        "temperature_c": max(temperatures) if temperatures else None,
                        "power_w": power, "power_source": power_source,
                        "clock_mhz": sum(mhz) / len(mhz) if mhz else None,
                        "max_clock_mhz": max(mhz) if mhz else None},
                "gpu": self.gpu(), "memory": {"total_mib": total,
                    "used_mib": total - available if total is not None and available is not None else None},
                "battery": battery, "ac_online": any(ac) if ac else None, "fans": fans,
                "load": list(os.getloadavg()), "uptime_s": number((read(self.proc / "uptime", "0")).split()[0])}


class Sampler:
    def __init__(self, hardware=None):
        self.hardware = hardware or Hardware()
        self.sensors = Sensors(self.hardware.root)
        self.latest = {}
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def run(self):
        while not self.stop.is_set():
            try:
                self.latest = {**self.hardware.capabilities(), "metrics": self.sensors.sample()}
            except Exception as exc:
                self.latest = {"error": str(exc), "metrics": {}}
            self.stop.wait(1)

    def start(self):
        self.thread.start()
        return self


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
                    self.reply(200, {**self.server.sampler.latest, **self.server.hardware.capabilities()})
                elif self.path == "/v1/metrics":
                    self.reply(200, self.server.sampler.latest.get("metrics", {}))
                elif self.path == "/v1/capabilities":
                    self.reply(200, self.server.hardware.capabilities())
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
                    else:
                        self.reply(404, {"error": "Unknown endpoint or unexpected fields"})
                        return
                    self.reply(200, value)
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
    if path.exists() or path.is_symlink():
        raise ControlError(f"Socket already exists: {path}. Stop the existing server first; remove a stale socket manually.")
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
        sampler.stop.set()
        server.server_close()
        path.unlink(missing_ok=True)


class Backend:
    def __init__(self, path=None):
        self.path = path or socket_path()
        self.remote = Path(self.path).exists()
        self.hardware = Hardware()
        self.sampler = None
        self.latest = {}
        self.stop = threading.Event()

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
                        self.latest = {"error": "API connection failed: " + str(exc)}
                    self.stop.wait(1)
            threading.Thread(target=poll, daemon=True).start()

    def state(self):
        return self.sampler.latest if self.sampler else self.latest

    def controls(self):
        """Read firmware now, without using the telemetry cache."""
        return request(self.path, "GET", "/v1/capabilities") if self.remote else self.hardware.capabilities()

    def write(self, key, value):
        if self.remote:
            endpoint = "/v1/mode" if key == "mode" else "/v1/attributes/" + key
            return request(self.path, "POST", endpoint, {"mode" if key == "mode" else "value": value})
        return self.hardware.set_mode(value) if key == "mode" else self.hardware.set_value(key, value)

    def close(self):
        self.stop.set()
        if self.sampler:
            self.sampler.stop.set()


class Keyboard:
    """Lenovo 4-zone RGB via LegionAura. Config lives in ~/.config/legionaura/."""

    EFFECTS = ("static", "breath", "wave", "hue", "off")
    ZONES = 4

    def __init__(self, binary=LEGIONAURA, config=LEGIONAURA_CONFIG):
        self.binary = binary
        self.config = Path(config)
        self.effect = "static"
        self.speed = 1
        self.brightness = 2
        self.wave_dir = "ltr"
        self.zones = ["ffffff"] * self.ZONES
        self.dirty = False  # in-memory edits not yet confirmed applied; load() leaves them alone
        self.last_message = ""
        self.last_seq = 0
        self._cond = threading.Condition()
        self._job = None
        self._running = False
        self._stop = False
        self._thread = None
        self.load()

    @property
    def available(self):
        return bool(shutil.which(self.binary) or Path(self.binary).is_file())

    def load(self, force=False):
        if self.dirty and not force:
            return
        try:
            data = json.loads(self.config.read_text())
        except (OSError, json.JSONDecodeError, TypeError):
            return
        self.effect = data.get("effect", self.effect)
        self.speed = int(data.get("speed", self.speed) or 1)
        self.brightness = int(data.get("brightness", self.brightness) or 2)
        wave = data.get("waveDir", "ltr")
        self.wave_dir = "rtl" if wave == "rtl" else "ltr"
        zones = data.get("zones") or []
        cleaned = []
        for zone in zones[:self.ZONES]:
            try:
                cleaned.append(rgb_to_hex(*hex_to_rgb(zone)))
            except ControlError:
                cleaned.append("ffffff")
        while len(cleaned) < self.ZONES:
            cleaned.append(cleaned[-1] if cleaned else "ffffff")
        self.zones = cleaned

    def status(self):
        self.load()
        return {
            "available": self.available,
            "binary": self.binary,
            "config": str(self.config),
            "effect": self.effect,
            "speed": self.speed,
            "brightness": self.brightness,
            "wave_dir": self.wave_dir,
            "zones": list(self.zones),
        }

    def _run(self, args):
        if not self.available:
            raise ControlError("legionaura not found. Install the LegionAura package.")
        result = subprocess.run([self.binary, *args], capture_output=True, text=True, timeout=8, check=False)
        if result.returncode:
            detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
            raise ControlError("legionaura: " + detail)
        return (result.stdout or "").strip() or "OK"

    def command(self):
        effect = self.effect
        if effect == "off":
            return ["off"]
        if effect == "apply":
            return ["apply"]
        brightness = ["--brightness", str(clamp(self.brightness, 1, 2))]
        speed = ["--speed", str(clamp(self.speed, 1, 4))]
        if effect == "static":
            return ["static", *self.zones, *brightness]
        if effect == "breath":
            return ["breath", *self.zones, *speed, *brightness]
        if effect == "wave":
            return ["wave", self.wave_dir, *speed, *brightness]
        if effect == "hue":
            return ["hue", *speed, *brightness]
        raise ControlError(f"Unknown keyboard effect {effect!r}.")

    def apply_current(self):
        return self._run(self.command())

    @property
    def busy(self):
        with self._cond:
            return self._job is not None or self._running

    def submit(self, args, label):
        """Queue one legionaura run; a newer submit replaces one that has not started."""
        with self._cond:
            self._job = (list(args), label)
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, daemon=True)
                self._thread.start()
            self._cond.notify_all()

    def _worker(self):
        while True:
            with self._cond:
                while self._job is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    return
                args, label = self._job
                self._job = None
                self._running = True
            ok = False
            try:
                message = f"Keyboard applied ({label}). {self._run(args)}"
                ok = True
            except ControlError as exc:
                message = str(exc)
            except Exception as exc:
                message = f"legionaura: {exc}"
            with self._cond:
                self._running = False
                if ok and self._job is None:
                    self.dirty = False
                self.last_message = message
                self.last_seq += 1

    def close(self):
        with self._cond:
            self._stop = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(0.1)

    def set_brightness(self, value):
        self.brightness = clamp(int(value), 1, 2)
        return self._run(["--brightness", str(self.brightness)])


class Dashboard:
    MIN_W, MIN_H = 72, 20

    def __init__(self, screen, backend, sudo_ok=True):
        self.s, self.backend = screen, backend
        self.sudo_expired = not sudo_ok
        self.selected = 0
        self.pending = {}
        self.apply_results = []
        self.show_results = False
        self.notice = "Ready. Edit a row, then press Enter to apply.  l opens keyboard lighting."
        self.history = {k: collections.deque(maxlen=240) for k in ("cpu", "gpu", "cpu_w", "gpu_w")}
        self.last_timestamp = None
        self.fields = {}  # key -> raw text typed for that row
        self.invalid = set()  # keys whose field text is invalid
        self.help = False
        self.running = True
        self.paused = False
        self.panel = "main"  # main | keyboard
        self.theme = Theme(curses.has_colors())
        self.keyboard = Keyboard()
        self.kb_zone = 0
        self.kb_channel = 0  # 0=H 1=S 2=V
        self.kb_live = True
        self.kb_seen = 0
        self._set_escdelay()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        screen.keypad(True)
        screen.timeout(120)
        try:
            curses.meta(True)
        except curses.error:
            pass

    @staticmethod
    def _set_escdelay():
        if "ESCDELAY" not in os.environ:
            try:
                curses.set_escdelay(25)
            except (AttributeError, curses.error):
                pass

    def put(self, y, x, text, color=0, bold=False, dim=False):
        h, w = self.s.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w - 1:
            return
        try:
            self.s.addnstr(y, x, str(text), max(0, w - x - 1), self.theme.attr(color, bold, dim))
        except curses.error:
            pass

    def fill(self, y, x, height, width, char=" "):
        for row in range(y, y + height):
            self.put(row, x, char * max(0, width))

    def box(self, y, x, height, width, title="", color=1, right=""):
        if height < 2 or width < 4:
            return
        self.put(y, x, "╭" + "─" * (width - 2) + "╮", color)
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "│", color)
            self.put(row, x + width - 1, "│", color)
        self.put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", color)
        if title:
            label = f" {title} "
            self.put(y, x + 2, label, color, True)
        if right:
            label = f" {right} "
            self.put(y, x + width - len(label) - 2, label, color)

    def bar(self, y, x, width, value, color=None):
        width = max(0, width)
        if width == 0:
            return
        pct = clamp(value if value is not None else 0, 0, 100)
        filled = round(pct * width / 100)
        color = color if color is not None else self.theme.load(pct)
        self.put(y, x, BAR_FILL * filled, color, True)
        self.put(y, x + filled, BAR_EMPTY * (width - filled), self.theme.TEXT, dim=True)

    def chart(self, y, x, width, key, ceiling=100, color=1):
        width = max(0, width)
        values = list(self.history[key])[-width:]
        plot = []
        for value in values:
            if value is None:
                plot.append(" ")
            else:
                plot.append(SPARK[min(7, max(0, round(value / max(1, ceiling) * 7)))])
        self.put(y, x, "".join(plot).rjust(width), color)

    @staticmethod
    def locked(state):
        # Gate on the LIVE mode; a staged mode never unlocks limit rows.
        return state.get("mode") != "custom"

    def rows(self, state):
        attrs = state.get("attributes") or {}
        return ["mode"] + [k for k in FIELDS if k in attrs]

    def push_history(self, metrics):
        cpu = metrics.get("cpu", {})
        gpu = next(iter(metrics.get("gpu", {}).get("devices", [])), {})
        stamp = metrics.get("timestamp")
        if not stamp or stamp == self.last_timestamp:
            return
        for key, value in (("cpu", cpu.get("utilization_pct")), ("gpu", gpu.get("utilization_pct")),
                           ("cpu_w", cpu.get("power_w")), ("gpu_w", gpu.get("power_w"))):
            self.history[key].append(value)
        self.last_timestamp = stamp

    def draw(self, state):
        if self.keyboard.last_seq != self.kb_seen:
            self.kb_seen = self.keyboard.last_seq
            self.notice = self.keyboard.last_message
        self.s.erase()
        h, w = self.s.getmaxyx()
        if h < self.MIN_H or w < self.MIN_W:
            self.put(1, 2, "loqctl", self.theme.ACCENT, True)
            self.put(3, 2, f"Need at least {self.MIN_W}×{self.MIN_H}. Now {w}×{h}.")
            self.put(5, 2, "Press q to quit.", self.theme.TEXT, dim=True)
            self.s.refresh()
            return

        metrics = state.get("metrics") or {}
        if not self.paused:
            self.push_history(metrics)

        # Header
        model = state.get("model") or "…"
        mode = (state.get("mode") or "…").upper()
        access = "API" if self.backend.remote else "ROOT" if os.geteuid() == 0 else "LOCAL"
        if self.paused:
            access = "PAUSED"
        title = f" loqctl {VERSION} "
        self.put(0, 0, "━" * (w - 1), self.theme.ACCENT, dim=True)
        self.put(0, 1, title, self.theme.ACCENT, True)
        mid = f"{model}  ·  {mode}"
        self.put(0, max(len(title) + 2, (w - len(mid)) // 2), mid, self.theme.OK)
        self.put(0, w - len(access) - 3, f" {access} ", self.theme.GPU if access == "API" else self.theme.WARN)

        if self.panel == "keyboard":
            self.draw_keyboard(1, 0, h - 3, w)
        else:
            self.draw_main(state, metrics, 1, 0, h - 3, w)

        # Status + keys
        stamp = metrics.get("timestamp")
        stale = stamp is not None and time.time() - stamp > 5
        status = state.get("error") or (("Sensor data is stale. " if stale else "") + self.notice)
        warn = bool(state.get("error") or stale)
        if self.sudo_expired and not self.backend.remote and os.geteuid() != 0:
            status, warn = "sudo not authenticated · press s to authenticate · edits stay staged", True
        self.put(h - 2, 1, status[: w - 3], self.theme.WARN if warn else self.theme.TEXT)
        if self.panel == "keyboard":
            keys = "1–4 zone  ←→ value  ↑↓ HSV  e effect  b bright  c copy-all  Space live  Enter apply  Esc back  q quit"
        else:
            keys = "↑↓ select  0-9 type  ←→ adjust  Enter apply  Esc clear  d default  r discard  l keyboard  Space pause  v results  ? help  q"
        self.put(h - 1, 1, keys[: w - 3], self.theme.ACCENT, dim=True)

        if self.help:
            self.draw_help()
        if self.show_results:
            self.draw_results()
        self.s.refresh()

    def draw_main(self, state, metrics, y, x, height, width):
        left_w = clamp(width * 42 // 100, 34, 58)
        right_x = x + left_w + 1
        right_w = width - left_w - 1
        self.draw_controls(state, y, x, height, left_w)
        self.draw_telemetry(metrics, y, right_x, height, right_w)

    def draw_controls(self, state, y, x, height, width):
        pending_n = len(self.pending)
        right = f"{pending_n} staged" if pending_n else "live"
        if self.invalid:
            right = (f"{pending_n} staged" if pending_n else "") + f"{', ' if pending_n else ''}{len(self.invalid)} invalid"
        self.box(y, x, height, width, "firmware", self.theme.ACCENT, right)
        self.put(y + 1, x + 2, f"{'CONTROL':<22} LIVE", self.theme.TEXT, dim=True)
        rows = self.rows(state)
        if not rows:
            self.put(y + 3, x + 2, "No firmware attributes exposed.", self.theme.WARN)
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        list_top = y + 2
        detail_h = 4
        list_h = max(3, height - detail_h - 3)
        visible = list_h
        start = 0
        if self.selected >= visible:
            start = self.selected - visible + 1
        for i, key in enumerate(rows[start:start + visible]):
            row = start + i
            yy = list_top + i
            chosen = row == self.selected
            marker = "▶" if chosen else " "
            if key == "mode":
                label, current, unit = "Platform mode", state.get("mode", "N/A"), ""
            else:
                item = state["attributes"][key]
                label, current, unit = item["label"], item["current_value"], item["unit"]
            staged = self.pending.get(key)
            value = f"{current if current is not None else 'N/A'}{unit}"
            if key != "mode" and self.locked(state):
                line = f"{marker} {label:<20} {value}"
                self.put(yy, x + 1, line[: width - 3], self.theme.TEXT, bold=chosen, dim=True)
                continue
            if key != "mode":
                text = self.fields.get(key)
                bad = key in self.invalid
                if chosen:
                    line = (f"{marker} {label:<20} {strike(value)} -> "
                            + (f"{text}_" if text else "__"))
                    self.put(yy, x + 1, line[: width - 3], self.theme.BAD if bad else self.theme.ACCENT, bold=True)
                    continue
                if bad:
                    value += f" → {text}"
                    self.put(yy, x + 1, f"{marker} {label:<20} {value}"[: width - 3], self.theme.BAD)
                    continue
            if staged is not None:
                value += f" → {staged}"
            color = self.theme.WARN if staged is not None else (self.theme.ACCENT if chosen else self.theme.TEXT)
            line = f"{marker} {label:<20} {value}"
            self.put(yy, x + 1, line[: width - 3], color, bold=chosen, dim=not chosen and staged is None)

        # Detail footer inside the box
        detail_y = y + height - detail_h
        self.put(detail_y, x + 1, "─" * (width - 2), self.theme.ACCENT, dim=True)
        key = rows[self.selected]
        if key == "mode":
            modes = " ".join(state.get("modes") or []) or "none"
            self.put(detail_y + 1, x + 2, "← → cycles platform mode", self.theme.ACCENT)
            self.put(detail_y + 2, x + 2, f"Available: {modes}"[: width - 4], self.theme.TEXT, dim=True)
        elif self.locked(state):
            self.put(detail_y + 1, x + 2, f"Limits are locked in {state.get('mode') or 'N/A'} mode"[: width - 4],
                     self.theme.WARN)
            self.put(detail_y + 2, x + 2, "Select custom on the mode row and press Enter."[: width - 4],
                     self.theme.TEXT, dim=True)
        else:
            item = state["attributes"][key]
            step = item["scalar_increment"]
            step_txt = "any int" if not step else str(step)
            self.put(detail_y + 1, x + 2, key, self.theme.ACCENT)
            self.put(detail_y + 2, x + 2,
                     f"{item['min_value']}–{item['max_value']} {item['unit']}  ·  step {step_txt}  ·  default {item['default_value']}"
                     [: width - 4], self.theme.TEXT, dim=True)

    def draw_telemetry(self, metrics, y, x, height, width):
        cpu = metrics.get("cpu") or {}
        gpu = next(iter((metrics.get("gpu") or {}).get("devices") or []), {})
        gpu_meta = metrics.get("gpu") or {}
        memory = metrics.get("memory") or {}

        # Split right column into cpu / gpu / sys with flexible heights.
        cpu_h = max(7, height * 38 // 100)
        gpu_h = max(6, height * 32 // 100)
        if cpu_h + gpu_h > height - 6:
            gpu_h = max(6, height - cpu_h - 6)
        sys_h = height - cpu_h - gpu_h
        if sys_h < 5:
            sys_h = 5
            gpu_h = max(5, height - cpu_h - sys_h)

        # CPU
        cpu_right = " · ".join(filter(None, [
            fmt(cpu.get("utilization_pct"), "%"),
            fmt(cpu.get("temperature_c"), "°C"),
            fmt(cpu.get("power_w"), "W", 1),
        ]))
        self.box(y, x, cpu_h, width, "cpu", self.theme.OK, cpu_right)
        ix, iw = x + 2, width - 4
        self.bar(y + 1, ix, iw, cpu.get("utilization_pct"), self.theme.load(cpu.get("utilization_pct")))
        temp_c = cpu.get("temperature_c")
        self.put(y + 2, ix,
                 f"avg {fmt(cpu.get('clock_mhz'), ' MHz')}   max {fmt(cpu.get('max_clock_mhz'), ' MHz')}   "
                 f"{fmt(temp_c, '°C')}"[:iw],
                 self.theme.heat(temp_c))
        if cpu_h >= 7:
            self.chart(y + 3, ix, iw, "cpu", color=self.theme.OK)
        if cpu_h >= 8:
            self.put(y + 4, ix, "W ", self.theme.WARN, dim=True)
            self.chart(y + 4, ix + 2, iw - 2, "cpu_w", ceiling=170, color=self.theme.WARN)
        if cpu_h >= 9:
            cores = list((cpu.get("cores") or {}).values())
            spark = "".join("·" if c is None else SPARK[min(7, round((c or 0) * 7 / 100))] for c in cores)
            self.put(y + 5, ix, f"cores {spark}"[:iw], self.theme.OK)
        if cpu_h >= 10:
            self.put(y + 6, ix, (cpu.get("power_source") or "Sampling…")[:iw], self.theme.TEXT, dim=True)

        # GPU
        gy = y + cpu_h
        gpu_right = " · ".join(filter(None, [
            fmt(gpu.get("utilization_pct"), "%"),
            fmt(gpu.get("temperature_c"), "°C"),
            fmt(gpu.get("power_w"), "W", 1),
        ])) if gpu else "offline"
        self.box(gy, x, gpu_h, width, "gpu", self.theme.GPU, gpu_right)
        name = gpu.get("name") or gpu_meta.get("error") or "Sampling…"
        self.put(gy + 1, ix, name[:iw], self.theme.GPU, True)
        if gpu:
            self.bar(gy + 2, ix, iw, gpu.get("utilization_pct"), self.theme.load(gpu.get("utilization_pct")))
            self.put(gy + 3, ix,
                     f"core {fmt(gpu.get('clock_mhz'), ' MHz')}  mem {fmt(gpu.get('memory_clock_mhz'), ' MHz')}"[:iw],
                     self.theme.TEXT, dim=True)
            if gpu_h >= 7:
                self.put(gy + 4, ix,
                         f"vram {fmt(gpu.get('memory_used_mib'))}/{fmt(gpu.get('memory_total_mib'), ' MiB')}  "
                         f"limit {fmt(gpu.get('power_limit_w'), ' W')}"[:iw],
                         self.theme.TEXT, dim=True)
            if gpu_h >= 8:
                self.chart(gy + 5, ix, iw, "gpu", color=self.theme.GPU)
            if gpu_h >= 9:
                self.put(gy + 6, ix, "W ", self.theme.WARN, dim=True)
                self.chart(gy + 6, ix + 2, iw - 2, "gpu_w", ceiling=140, color=self.theme.WARN)

        # System
        sy = gy + gpu_h
        used, total = memory.get("used_mib"), memory.get("total_mib")
        mem_right = ""
        if used is not None and total:
            mem_right = f"{used / 1024:.1f}/{total / 1024:.1f} GiB"
        self.box(sy, x, sys_h, width, "system", self.theme.INFO, mem_right)
        if used is not None and total:
            self.bar(sy + 1, ix, iw, used / total * 100, self.theme.INFO)
        battery = next(iter(metrics.get("battery") or []), {})
        ac = metrics.get("ac_online")
        power = "AC" if ac else "BAT" if ac is False else "PWR?"
        bat = f"{fmt(battery.get('percent'), '%')} {battery.get('status') or ''}".strip()
        self.put(sy + 2, ix, f"{power}  {bat}"[:iw], self.theme.OK if ac else self.theme.WARN)
        if sys_h >= 6:
            fans = metrics.get("fans") or []
            fan_txt = "  ".join(f"{f['name']} {fmt(f['rpm'])}" for f in fans) if fans else "fans n/a"
            self.put(sy + 3, ix, fan_txt[:iw], self.theme.TEXT, dim=True)
        if sys_h >= 7:
            loads = metrics.get("load") or []
            uptime = metrics.get("uptime_s")
            up = f"  up {int(uptime // 3600)}h{int(uptime % 3600 // 60):02d}m" if uptime is not None else ""
            self.put(sy + 4, ix, ("load " + " ".join(fmt(v, decimals=2) for v in loads) + up)[:iw],
                     self.theme.TEXT, dim=True)

    def draw_keyboard(self, y, x, height, width):
        kb = self.keyboard
        status = "live" if self.kb_live else "preview off"
        if not kb.available:
            status = "missing"
        self.box(y, x, height, width, "keyboard / legionaura", self.theme.GPU, status)
        ix, iw = x + 2, width - 4
        if not kb.available:
            self.put(y + 2, ix, "Install LegionAura (AUR: legionaura) for 4-zone RGB control.", self.theme.WARN)
            self.put(y + 4, ix, "Esc returns to firmware controls.", self.theme.TEXT, dim=True)
            return

        # Effect row
        effects = []
        for name in kb.EFFECTS:
            tag = f"[{name}]" if name == kb.effect else f" {name} "
            effects.append(tag)
        self.put(y + 1, ix, "effect  " + " ".join(effects), self.theme.ACCENT)
        bright = "low" if kb.brightness == 1 else "high"
        wave = f"  wave {kb.wave_dir}" if kb.effect == "wave" else ""
        self.put(y + 2, ix, f"bright  {bright}   speed {kb.speed}/4{wave}", self.theme.TEXT, dim=True)

        # Zones
        self.put(y + 4, ix, "zones", self.theme.TEXT, dim=True)
        for zi, hexcolor in enumerate(kb.zones):
            try:
                r, g, b = hex_to_rgb(hexcolor)
            except ControlError:
                r, g, b = 255, 255, 255
            pair = self.theme.ansi_pair(nearest_ansi(r, g, b))
            chosen = zi == self.kb_zone
            zx = ix + zi * max(12, iw // 4)
            border = "▶" if chosen else " "
            self.put(y + 5, zx, f"{border}Z{zi + 1}", self.theme.GPU if chosen else self.theme.TEXT, bold=chosen)
            self.put(y + 6, zx, "████", pair, True)
            self.put(y + 7, zx, f"#{hexcolor}", self.theme.TEXT, dim=not chosen)

        # HSV channels for selected zone
        try:
            rgb = hex_to_rgb(kb.zones[self.kb_zone])
        except ControlError:
            rgb = (255, 255, 255)
        h_deg, s_pct, v_pct = rgb_to_hsv(*rgb)
        channels = (("H", h_deg, 360, "°"), ("S", s_pct, 100, "%"), ("V", v_pct, 100, "%"))
        cy = y + 9
        self.put(cy, ix, f"zone {self.kb_zone + 1}  HSV picker  (terminal swatch ≈ nearest ANSI)", self.theme.ACCENT)
        for i, (name, value, ceiling, unit) in enumerate(channels):
            yy = cy + 1 + i
            chosen = i == self.kb_channel
            marker = "▶" if chosen else " "
            bar_w = max(8, min(40, iw - 18))
            self.put(yy, ix, f"{marker}{name} ", self.theme.WARN if chosen else self.theme.TEXT, bold=chosen)
            self.bar(yy, ix + 4, bar_w, value / ceiling * 100,
                     self.theme.WARN if chosen else self.theme.ACCENT)
            self.put(yy, ix + 5 + bar_w, f" {value:5.1f}{unit}", self.theme.TEXT, dim=not chosen)

        tip_y = min(y + height - 3, cy + 5)
        self.put(tip_y, ix, "Hardware shows the true color. UI uses your terminal theme only.", self.theme.TEXT, dim=True)
        self.put(tip_y + 1, ix, f"config {kb.config}"[:iw], self.theme.TEXT, dim=True)

    def draw_help(self):
        h, w = self.s.getmaxyx()
        bw = min(86, w - 4)
        bh = min(19, h - 2)
        bx, by = (w - bw) // 2, max(1, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "help", self.theme.ACCENT)
        lines = [
            "Firmware  j/k or ↑↓ select   ←/→ or -/+ step the value",
            "          0-9 type a value   Backspace delete   Esc clear row",
            "          Enter apply all    d custom default   r discard all",
            "          v last apply results   Limits need custom mode (Enter on mode row)",
            "          s sudo authenticate (shown when needed)",
            "",
            "Keyboard  l open lighting panel   Esc back to firmware",
            "          1–4 select zone   ←/→ adjust HSV   ↑↓ channel",
            "          e cycle effect   b brightness   w wave dir",
            "          c copy color to all zones   Space toggle live apply",
            "          Enter apply via legionaura",
            "",
            "Global    Space pause telemetry   ? help   q quit",
            "Colors use your terminal ANSI palette only (no custom RGB UI).",
            "Press ? or Esc to close.",
        ]
        for i, line in enumerate(lines[: bh - 2]):
            self.put(by + 1 + i, bx + 2, line[: bw - 4], self.theme.TEXT, dim=not line)

    def draw_results(self):
        h, w = self.s.getmaxyx()
        bw = min(104, w - 4)
        bh = min(h - 2, len(self.apply_results) + 11)
        bx, by = (w - bw) // 2, max(0, (h - bh) // 2)
        self.fill(by, bx, bh, bw)
        self.box(by, bx, bh, bw, "apply results", self.theme.ACCENT)
        self.put(by + 1, bx + 2, "Firmware readback immediately after each attempted change.", self.theme.TEXT, dim=True)
        self.put(by + 3, bx + 2, f"{'CONTROL':<26}{'BEFORE':>12}{'REQUESTED':>12}{'READ BACK':>12}  RESULT",
                 self.theme.ACCENT, True)
        for i, result in enumerate(self.apply_results):
            key = result["key"]
            label, unit = ("Platform mode", "") if key == "mode" else FIELDS[key]
            def display(value):
                return "N/A" if value is None else str(value) + unit
            line = (f"{label:<26}{display(result['before']):>12}{display(result['requested']):>12}"
                    f"{display(result['current']):>12}  {result['status']}")
            color = self.theme.OK if result["status"] == "APPLIED" else (
                self.theme.WARN if result["status"] == "NOT SENT" else self.theme.BAD)
            self.put(by + 4 + i, bx + 2, line[: bw - 4], color)
        error = next((r["error"] for r in self.apply_results if r.get("error")),
                     "All requested values matched firmware readback.")
        for i, line in enumerate(textwrap.wrap(error, bw - 4)[:3]):
            self.put(by + bh - 5 + i, bx + 2, line, self.theme.WARN)
        self.put(by + bh - 2, bx + 2, "Enter / Esc closes · v reopens · failed edits remain staged", self.theme.ACCENT)

    def stage(self, state, key, value):
        if key == "mode":
            if value not in state.get("modes", []):
                raise ControlError("Unsupported mode.")
            current = state.get("mode")
            dropped = [k for k in self.pending if k != "mode"]
            if value != current:
                for k in dropped:
                    del self.pending[k]
        else:
            item = state["attributes"][key]
            if not item["available"]:
                raise ControlError("This control is unavailable.")
            lo, hi, step = item["min_value"], item["max_value"], max(1, item["scalar_increment"])
            if not lo <= value <= hi or (value - lo) % step:
                raise ControlError(f"Use {lo}–{hi} in steps of {step}.")
            current = item["current_value"]
            dropped_mode = self.pending.pop("mode", None) is not None
        if value == current:
            self.pending.pop(key, None)
        else:
            self.pending[key] = value
        self.notice = "Edit staged. Press Enter to apply."
        if key == "mode":
            if value != current and dropped:
                self.notice = "Staged limit edits dropped: limits apply only in custom mode."
        elif dropped_mode:
            self.notice = "Staged mode change dropped: apply mode and limits separately."

    def set_field(self, state, key, text):
        if not text:
            self.fields.pop(key, None)
            self.pending.pop(key, None)
            self.invalid.discard(key)
            return
        self.fields[key] = text
        try:
            if not text.isdigit():
                raise ControlError("Enter an integer.")
            self.stage(state, key, int(text))
        except ControlError:
            self.invalid.add(key)
            self.pending.pop(key, None)
            self.notice = "Value is not valid for this control."
        else:
            self.invalid.discard(key)

    def run_sudo_apply(self, changes):
        """Apply all changes in one `sudo -n` call; returns the per-item results."""
        command = ["sudo", "-n", "--", sys.executable, "-I", ENTRY, "_apply", json.dumps(changes)]
        try:
            result = subprocess.run(command, check=False, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired as exc:
            raise ControlError("sudo apply timed out; edits stay staged.") from exc
        stderr = (result.stderr or "").strip()
        if result.returncode:
            if "password is required" in stderr or "a terminal is required" in stderr:
                self.sudo_expired = True
                raise ControlError("sudo credentials expired. Press s to re-authenticate; edits stay staged.")
            raise ControlError(stderr or (result.stdout or "").strip() or f"sudo exited with code {result.returncode}")
        try:
            results = json.loads(result.stdout)
            if not isinstance(results, list) or not all(isinstance(r, dict) and "key" in r and "status" in r
                                                        for r in results):
                raise ValueError
        except ValueError as exc:
            raise ControlError(stderr or "sudo apply returned unreadable output.") from exc
        return results

    def reauth(self):
        curses.def_prog_mode()
        curses.endwin()
        try:
            ok = subprocess.run(["sudo", "-v"]).returncode == 0
        except KeyboardInterrupt:
            ok = False
        finally:
            curses.reset_prog_mode()
            self.s.refresh()
        if ok:
            self.sudo_expired = False
        self.notice = "sudo authenticated." if ok else "sudo authentication failed."

    def read_current(self, key):
        controls = self.backend.controls()
        if key == "mode":
            return controls.get("mode")
        return controls.get("attributes", {}).get(key, {}).get("current_value")

    def apply(self, state):
        if not self.pending:
            self.notice = "No pending changes."
            return
        changes = sorted(self.pending.items(), key=lambda pair: pair[0] != "mode")
        self.apply_results = []
        self.show_results = True
        completed = 0
        stopped = None
        if any(k != "mode" for k in self.pending) and self.locked(state):
            raise ControlError(f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter.")
        if not (self.backend.remote or os.geteuid() == 0):
            self.apply_results = self.run_sudo_apply([[k, v] for k, v in changes])
            self.show_results = True
            for result in self.apply_results:
                if result["status"] == "APPLIED":
                    self.pending.pop(result["key"], None)
                    self.fields.pop(result["key"], None)
                    completed += 1
            failed = next((r for r in self.apply_results if r["status"] != "APPLIED"), None)
            if failed:
                raise ControlError(f"Apply stopped after {completed} writes: {failed.get('error') or failed['status']}")
            self.notice = f"Applied and verified {completed} change(s)."
            return
        for key, value in changes:
            result = {"key": key, "requested": value, "before": None,
                      "current": None, "status": "NOT SENT", "error": None}
            try:
                result["before"] = self.read_current(key)
            except (ControlError, OSError, ValueError, http_client.HTTPException):
                pass
            if stopped:
                result["current"] = result["before"]
                self.apply_results.append(result)
                continue
            error = None
            try:
                self.backend.write(key, value)
            except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                error = str(exc)
            write_failed = error is not None
            try:
                result["current"] = self.read_current(key)
            except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                error = error or "Could not read firmware after the write: " + str(exc)
            if error or result["current"] != value:
                result["status"] = "UNVERIFIED" if result["current"] is None and not write_failed else "FAILED"
                result["error"] = error or f"Requested {value}, but firmware reports {result['current']!r}."
                stopped = result["error"]
            else:
                result["status"] = "APPLIED"
                self.pending.pop(key)
                completed += 1
            self.apply_results.append(result)
        if stopped:
            raise ControlError(f"Apply stopped after {completed} writes: {stopped}")
        self.notice = f"Applied and verified {completed} change(s)."

    def kb_set_hsv(self, h, s, v):
        kb = self.keyboard
        kb.zones[self.kb_zone] = rgb_to_hex(*hsv_to_rgb(h, s, v))
        kb.dirty = True
        if self.kb_live:
            # Live tweaking uses static so zone colors are visible regardless of effect.
            try:
                if kb.effect in ("static", "breath"):
                    kb.submit(kb.command(), kb.effect)
                else:
                    kb.submit(["static", *kb.zones, "--brightness", str(kb.brightness)], "static")
            except ControlError as exc:
                self.notice = str(exc)

    def kb_nudge(self, direction, fine=False):
        try:
            rgb = hex_to_rgb(self.keyboard.zones[self.kb_zone])
        except ControlError:
            rgb = (255, 255, 255)
        h, s, v = rgb_to_hsv(*rgb)
        step = 1 if fine else (5 if self.kb_channel else 10)
        if self.kb_channel == 0:
            h = (h + direction * step) % 360
        elif self.kb_channel == 1:
            s = clamp(s + direction * step, 0, 100)
        else:
            v = clamp(v + direction * step, 0, 100)
        self.kb_set_hsv(h, s, v)
        self.notice = f"Zone {self.kb_zone + 1} → #{self.keyboard.zones[self.kb_zone]}"

    def handle_keyboard(self, ch):
        kb = self.keyboard
        if ch in (27, ord("l")):
            self.panel = "main"
            self.notice = "Back to firmware controls."
            return
        if ch == ord("q"):
            self.running = False
            return
        if not kb.available:
            return
        if ch in (ord("1"), ord("2"), ord("3"), ord("4")):
            self.kb_zone = ch - ord("1")
            self.notice = f"Selected zone {self.kb_zone + 1}."
        elif ch in (curses.KEY_UP, ord("k")):
            self.kb_channel = (self.kb_channel - 1) % 3
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.kb_channel = (self.kb_channel + 1) % 3
        elif ch in (curses.KEY_LEFT, ord("-")):
            self.kb_nudge(-1)
        elif ch in (curses.KEY_RIGHT, ord("+"), ord("=")):
            self.kb_nudge(1)
        elif ch in (ord("H"),):  # fine adjust
            self.kb_nudge(-1, fine=True)
        elif ch in (ord("L"),):
            self.kb_nudge(1, fine=True)
        elif ch == ord("e"):
            idx = kb.EFFECTS.index(kb.effect) if kb.effect in kb.EFFECTS else 0
            kb.effect = kb.EFFECTS[(idx + 1) % len(kb.EFFECTS)]
            kb.dirty = True
            self.notice = f"Effect → {kb.effect}"
            if self.kb_live:
                try:
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("b"):
            kb.brightness = 1 if kb.brightness == 2 else 2
            kb.dirty = True
            self.notice = f"Brightness → {'low' if kb.brightness == 1 else 'high'}"
            if self.kb_live:
                try:
                    kb.submit(["--brightness", str(kb.brightness)], "brightness")
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("w"):
            kb.wave_dir = "rtl" if kb.wave_dir == "ltr" else "ltr"
            kb.dirty = True
            self.notice = f"Wave → {kb.wave_dir}"
            if self.kb_live and kb.effect == "wave":
                try:
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord("c"):
            color = kb.zones[self.kb_zone]
            kb.zones = [color] * kb.ZONES
            kb.dirty = True
            self.notice = f"Copied #{color} to all zones."
            if self.kb_live:
                try:
                    kb.effect = "static" if kb.effect == "off" else kb.effect
                    kb.submit(kb.command(), kb.effect)
                except ControlError as exc:
                    self.notice = str(exc)
        elif ch == ord(" "):
            self.kb_live = not self.kb_live
            self.notice = "Live keyboard apply on." if self.kb_live else "Live apply off — Enter to commit."
        elif ch in (10, 13, curses.KEY_ENTER):
            try:
                kb.submit(kb.command(), kb.effect)
                self.notice = "Applying keyboard…"
            except ControlError as exc:
                self.notice = str(exc)

    def handle(self, ch, state):
        if self.show_results:
            if ch in (10, 13, curses.KEY_ENTER, 27, ord("v")):
                self.show_results = False
            elif ch == ord("q"):
                self.running = False
            return
        if self.help:
            if ch in (ord("?"), 27):
                self.help = False
            elif ch == ord("q"):
                self.running = False
            return
        if getattr(self, "panel", "main") == "keyboard":
            self.handle_keyboard(ch)
            return

        rows = self.rows(state)
        if not rows:
            if ch == ord("q"):
                self.running = False
            elif ch == ord("l"):
                self.panel = "keyboard"
                self.keyboard.load()
                self.notice = "Keyboard lighting."
            return
        self.selected = clamp(self.selected, 0, len(rows) - 1)
        key = rows[self.selected]

        if self.locked(state) and (self.fields or self.invalid):
            # Limit fields only exist for custom mode; drop stale ones if the mode changed.
            self.fields.clear()
            self.invalid.clear()
        unlocked_limit = key != "mode" and not self.locked(state)
        digits = range(48, 58)

        if ch == ord("q"):
            self.running = False
        elif ch == ord("?"):
            self.help = True
        elif ch == ord("l"):
            self.panel = "keyboard"
            self.keyboard.load()
            self.notice = "Keyboard lighting. Esc returns."
        elif ch == ord(" "):
            self.paused = not self.paused
            self.notice = "Telemetry paused." if self.paused else "Telemetry resumed."
        elif ch == ord("s"):
            if self.backend.remote or os.geteuid() == 0:
                self.notice = "No sudo needed."
            else:
                self.reauth()
        elif ch == ord("v"):
            self.show_results = bool(self.apply_results)
            if not self.apply_results:
                self.notice = "No apply results yet. Edit a row and press Enter."
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.selected = (self.selected + 1) % len(rows)
        elif ch in (curses.KEY_UP, ord("k")):
            self.selected = (self.selected - 1) % len(rows)
        elif ch == ord("r"):
            self.pending.clear()
            self.fields.clear()
            self.invalid.clear()
            self.notice = "Pending edits discarded."
        elif ch in (10, 13, curses.KEY_ENTER):
            if self.invalid:
                first = next(k for k in rows if k in self.invalid)
                self.selected = rows.index(first)
                self.notice = "Fix the value marked red before applying."
            elif self.pending:
                before = set(self.pending)
                try:
                    self.apply(state)
                finally:
                    for done in before - set(self.pending):
                        self.fields.pop(done, None)
            elif key == "mode":
                self.notice = "Use ← → to choose a mode, then press Enter."
            elif unlocked_limit:
                self.notice = "Type a value or use ← →, then press Enter."
            else:
                self.notice = f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter."
        elif key != "mode" and self.locked(state) and (
                ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("+"), ord("-"), ord("="), ord("d"), *digits)):
            self.notice = f"Limits are locked in {state.get('mode')} mode. Select custom and press Enter."
        elif ch == ord("d"):
            if key == "mode":
                self.notice = "Platform mode has no default."
            else:
                default = state["attributes"][key]["default_value"]
                if default is None:
                    raise ControlError("Firmware default is unavailable.")
                self.set_field(state, key, str(default))
        elif unlocked_limit and ch == 27:
            self.set_field(state, key, "")
            self.notice = "Row cleared."
        elif unlocked_limit and ch in (curses.KEY_BACKSPACE, 127, 8):
            self.set_field(state, key, self.fields.get(key, "")[:-1])
        elif unlocked_limit and ch in digits:
            text = self.fields.get(key, "")
            if len(text) < 6:
                self.set_field(state, key, text + chr(ch))
        elif ch in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("+"), ord("-"), ord("=")):
            direction = 1 if ch in (curses.KEY_RIGHT, ord("+"), ord("=")) else -1
            if key == "mode":
                modes = state.get("modes", [])
                current = self.pending.get(key, state.get("mode"))
                if current in modes:
                    self.stage(state, key, modes[(modes.index(current) + direction) % len(modes)])
            else:
                item = state["attributes"][key]
                if not item["available"]:
                    raise ControlError("This control is unavailable.")
                step = max(1, item["scalar_increment"])
                current = self.pending.get(key, item["current_value"])
                value = clamp(current + direction * step, item["min_value"], item["max_value"])
                self.set_field(state, key, str(value))

    def run(self):
        self.backend.start()
        try:
            while self.running:
                state = self.backend.state() or {}
                self.draw(state)
                ch = self.s.getch()
                h, w = self.s.getmaxyx()
                if (h < self.MIN_H or w < self.MIN_W) and ch != ord("q"):
                    continue
                if ch != -1:
                    try:
                        self.handle(ch, state)
                    except (ControlError, OSError, ValueError, http_client.HTTPException) as exc:
                        self.notice = str(exc)
        finally:
            self.keyboard.close()
            self.backend.close()


def kb_cli(argv):
    """LegionAura keyboard subcommands."""
    kb = Keyboard()
    if not argv:
        return kb.status()
    action = argv[0]
    rest = argv[1:]
    if action == "status":
        return kb.status()
    if action == "apply":
        return {"result": kb.apply_current(), **kb.status()}
    if action == "off":
        kb.effect = "off"
        return {"result": kb.apply_current(), **kb.status()}
    if action == "brightness" and rest:
        return {"result": kb.set_brightness(rest[0]), **kb.status()}
    if action in ("static", "breath"):
        if not rest:
            raise ControlError(f"{action} needs at least one RRGGBB color.")
        colors, brightness, speed = [], kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            colors.append(rgb_to_hex(*hex_to_rgb(token)))
            i += 1
        if not colors:
            raise ControlError(f"{action} needs at least one RRGGBB color.")
        while len(colors) < Keyboard.ZONES:
            colors.append(colors[-1])
        kb.zones = colors[:Keyboard.ZONES]
        kb.effect = action
        kb.brightness = clamp(brightness, 1, 2)
        kb.speed = clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    if action == "wave":
        direction = "ltr"
        brightness, speed = kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token in ("ltr", "rtl"):
                direction = token; i += 1; continue
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            raise ControlError(f"Unexpected wave argument: {token}")
        kb.effect, kb.wave_dir = "wave", direction
        kb.brightness, kb.speed = clamp(brightness, 1, 2), clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    if action == "hue":
        brightness, speed = kb.brightness, kb.speed
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--brightness" and i + 1 < len(rest):
                brightness = int(rest[i + 1]); i += 2; continue
            if token == "--speed" and i + 1 < len(rest):
                speed = int(rest[i + 1]); i += 2; continue
            raise ControlError(f"Unexpected hue argument: {token}")
        kb.effect = "hue"
        kb.brightness, kb.speed = clamp(brightness, 1, 2), clamp(speed, 1, 4)
        return {"result": kb.apply_current(), **kb.status()}
    raise ControlError("kb usage: status|apply|off|brightness|static|breath|wave|hue …")


def apply_batch(hardware, changes):
    """Apply [key, value] pairs in order as root; stop at the first failure."""
    if not isinstance(changes, list) or not 1 <= len(changes) <= len(FIELDS) + 1:
        raise ControlError("Changes must be a list of 1 to %d [key, value] pairs." % (len(FIELDS) + 1))
    keys = []
    for item in changes:
        if not isinstance(item, list) or len(item) != 2:
            raise ControlError("Each change must be a [key, value] pair.")
        key, value = item
        if key == "mode":
            if not isinstance(value, str):
                raise ControlError("Mode must be a string.")
        elif isinstance(key, str) and key in FIELDS:
            if type(value) is not int:
                raise ControlError("Limit values must be integers.")
        else:
            raise ControlError("Unknown control.")
        if key in keys:
            raise ControlError("Duplicate control in changes.")
        keys.append(key)
    if "mode" in keys and len(keys) > 1:
        raise ControlError("Mode and limits are never applied together.")

    def current(key):
        caps = hardware.capabilities()
        if key == "mode":
            return caps.get("mode")
        return caps.get("attributes", {}).get(key, {}).get("current_value")

    results, stopped = [], False
    for key, value in changes:
        result = {"key": key, "requested": value, "before": None,
                  "current": None, "status": "NOT SENT", "error": None}
        try:
            result["before"] = current(key)
        except (ControlError, OSError, ValueError):
            pass
        if stopped:
            result["current"] = result["before"]
            results.append(result)
            continue
        error = None
        try:
            hardware.set_mode(value) if key == "mode" else hardware.set_value(key, value)
        except (ControlError, OSError, ValueError) as exc:
            error = str(exc)
        write_failed = error is not None
        try:
            result["current"] = current(key)
        except (ControlError, OSError, ValueError) as exc:
            error = error or "Could not read firmware after the write: " + str(exc)
        if error or result["current"] != value:
            result["status"] = "UNVERIFIED" if result["current"] is None and not write_failed else "FAILED"
            result["error"] = error or f"Requested {value}, but firmware reports {result['current']!r}."
            stopped = True
        else:
            result["status"] = "APPLIED"
        results.append(result)
    return results


def preauth(backend):
    """Authenticate sudo once before curses starts; True when writes can proceed."""
    if os.geteuid() == 0 or backend.remote:
        return True
    caps = backend.hardware.capabilities()
    if not caps.get("attributes") and not caps.get("modes"):
        return True
    if shutil.which("sudo") is None:
        return False
    if subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode == 0:
        return True
    print("loqctl: sudo is needed to change firmware settings. Ctrl+C skips (read-only until you press s).",
          file=sys.stderr)
    try:
        return subprocess.run(["sudo", "-v"]).returncode == 0
    except KeyboardInterrupt:
        return False


def main(argv=None):
    if sys.version_info < (3, 10):
        print(f"loqctl: Python 3.10 or newer is required (found {sys.version_info[0]}.{sys.version_info[1]}).", file=sys.stderr)
        return 1
    if (sys.argv[1:] if argv is None else list(argv)) == ["--version"]:
        print(VERSION)  # fast path: skip the argparse import
        return 0
    parser = argparse.ArgumentParser(description="Lenovo LOQ firmware controls and live terminal telemetry")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--socket", help="Connect to an existing loqctl Unix socket")
    commands = parser.add_subparsers(dest="command", metavar="{tui,status,metrics,capabilities,mode,set,kb,serve}")
    commands.add_parser("tui", help="Open the dashboard (default)")
    for cmd in ("status", "metrics", "capabilities"):
        commands.add_parser(cmd, help="Print " + cmd + " as JSON")
    mode = commands.add_parser("mode", help="Read or set the platform mode")
    mode.add_argument("value", nargs="?")
    setting = commands.add_parser("set", help="Set one firmware attribute, with readback verification")
    setting.add_argument("attribute", choices=FIELDS)
    setting.add_argument("value", type=int)
    kb_parser = commands.add_parser("kb", help="Keyboard RGB via LegionAura")
    kb_parser.add_argument("args", nargs=argparse.REMAINDER, help="status|apply|off|static|breath|wave|hue|brightness …")
    server = commands.add_parser("serve", help="Serve the local HTTP API; run with sudo")
    server.add_argument("--uid", type=int, default=owner_uid(), help="UID permitted to connect; defaults to SUDO_UID")
    internal = commands.add_parser("_apply")
    internal.add_argument("changes", help="JSON list of [key, value] pairs")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            serve(args.uid)
            return 0
        if args.command == "_apply":
            result = apply_batch(Hardware(), json.loads(args.changes))
        elif args.command == "kb":
            # REMAINDER may include a leading "--" from argparse.
            kb_args = list(args.args or [])
            if kb_args and kb_args[0] == "--":
                kb_args = kb_args[1:]
            result = kb_cli(kb_args)
        else:
            backend = Backend(args.socket)
            if args.socket and not backend.remote:
                raise ControlError("The requested API socket does not exist.")
            if args.command in (None, "tui"):
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ControlError("The dashboard needs a terminal. Use status for JSON output.")
                sudo_ok = preauth(backend)
                curses.wrapper(lambda screen: Dashboard(screen, backend, sudo_ok=sudo_ok).run())
                return 0
            elif args.command in ("status", "metrics"):
                result = backend.snapshot()
                if args.command == "metrics":
                    result = result.get("metrics", {})
            elif args.command == "capabilities" or (args.command == "mode" and args.value is None):
                result = request(backend.path, "GET", "/v1/capabilities") if backend.remote else backend.hardware.capabilities()
                if args.command == "mode":
                    result = {"mode": result["mode"], "modes": result["modes"]}
            elif args.command == "mode":
                result = backend.write("mode", args.value)
            elif args.command == "set":
                result = backend.write(args.attribute, args.value)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0
    except (ControlError, OSError, ValueError, http_client.HTTPException, curses.error) as exc:
        print("loqctl: " + str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
