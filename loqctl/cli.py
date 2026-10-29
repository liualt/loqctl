# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Command line entry point."""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import time

from . import VERSION
from .api import http_client
from .backend import Backend
from .capdata import limits_report
from .common import FIELDS, ControlError, _lazy, owner_uid
from .hardware import Hardware, apply_batch
from .sensors import Sensors

argparse = _lazy("argparse")
curses = _lazy("curses")

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
    commands = parser.add_subparsers(dest="command", metavar="{tui,status,metrics,capabilities,limits,fan,mode,set,kb,doctor,serve}")
    commands.add_parser("tui", help="Open the dashboard (default)")
    for cmd in ("status", "metrics", "capabilities"):
        commands.add_parser(cmd, help="Print " + cmd + " as JSON")
    commands.choices["metrics"].add_argument("--watch", nargs="?", type=float, const=1, metavar="SECONDS", help="Print one JSON line every SECONDS (default 1, minimum 1) until interrupted")
    commands.add_parser("limits", help="Print per-mode firmware limits as JSON")
    commands.add_parser("fan", help="Print the fan curve and RPM as JSON")
    mode = commands.add_parser("mode", help="Read or set the platform mode")
    mode.add_argument("value", nargs="?", help="Mode to set; omit to print the current mode")
    setting = commands.add_parser("set", help="Set one firmware attribute, with readback verification")
    setting.add_argument("attribute", choices=FIELDS)
    setting.add_argument("value", type=int)
    kb_parser = commands.add_parser("kb", help="Keyboard RGB via LegionAura")
    kb_parser.add_argument("args", nargs=argparse.REMAINDER, help="status|apply|off|static|breath|wave|hue|brightness …")
    commands.add_parser("doctor", help="Check which features this laptop supports")
    server = commands.add_parser("serve", help="Serve the local HTTP API; run with sudo")
    server.add_argument("--uid", type=int, default=owner_uid(), help="UID permitted to connect; defaults to SUDO_UID")
    internal = commands.add_parser("_apply")
    internal.add_argument("changes", help="JSON list of [key, value] pairs")
    commands.add_parser("_capdata")
    fancurve = commands.add_parser("_fancurve")
    fancurve.add_argument("points", help="JSON list of fan curve points")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            from .api import serve
            serve(args.uid)
            return 0
        if args.command == "doctor":
            from .doctor import doctor
            print("\n".join(doctor()))
            return 0
        if args.command == "_apply":
            result = apply_batch(Hardware(), json.loads(args.changes))
        elif args.command == "_fancurve":
            result = Hardware().write_fancurve(json.loads(args.points))
        elif args.command == "_capdata":
            result, reason = Hardware().mode_limits()
            if result is None:
                raise ControlError(reason)
        elif args.command == "kb":
            # REMAINDER may include a leading "--" from argparse.
            kb_args = list(args.args or [])
            if kb_args and kb_args[0] == "--":
                kb_args = kb_args[1:]
            from .keyboard import kb_cli
            result = kb_cli(kb_args)
        else:
            backend = Backend(args.socket)
            if args.socket and not backend.remote:
                raise ControlError("The requested API socket is not accepting connections.")
            if backend.notice and args.command not in (None, "tui"):
                print(backend.notice, file=sys.stderr)
            if args.command in (None, "tui"):
                if not sys.stdin.isatty() or not sys.stdout.isatty():
                    raise ControlError("The dashboard needs a terminal. Use status for JSON output.")
                from .ui import Dashboard
                sudo_ok = preauth(backend)
                backend.load_mode_limits(allow_sudo=sudo_ok)
                curses.wrapper(lambda screen: Dashboard(screen, backend, sudo_ok=sudo_ok).run())
                return 0
            elif args.command == "metrics" and args.watch is not None:
                from .api import request
                if not (math.isfinite(args.watch) and args.watch >= 1):
                    raise ControlError("--watch interval must be at least 1 second.")
                sensors = None if backend.remote else Sensors()
                if sensors:
                    sensors.sample()  # prime so the first rates cover one interval
                deadline = time.monotonic()
                while True:
                    deadline += args.watch
                    time.sleep(max(0, deadline - time.monotonic()))
                    try:
                        metrics = sensors.sample() if sensors else request(backend.path, "GET", "/v1/status").get("metrics", {})
                        line = json.dumps(metrics, allow_nan=False)
                    except Exception as exc:
                        try:
                            print("loqctl: " + str(exc), file=sys.stderr)
                        except OSError:
                            pass
                        line = None
                    deadline = max(deadline, time.monotonic())
                    if line is not None:
                        try:
                            print(line, flush=True)
                        except BrokenPipeError:
                            try:  # keep the shutdown flush from failing on the closed pipe
                                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                            except OSError:
                                pass
                            return 0
            elif args.command in ("status", "metrics"):
                if args.command == "status":
                    backend.load_mode_limits()
                result = backend.snapshot()
                if args.command == "metrics":
                    result = result.get("metrics", {})
            elif args.command in ("capabilities", "limits") or (args.command == "mode" and args.value is None):
                from .api import request
                if args.command != "mode":
                    backend.load_mode_limits()
                result = request(backend.path, "GET", "/v1/capabilities") if backend.remote else backend.hardware.capabilities()
                if args.command == "mode":
                    result = {"mode": result["mode"], "modes": result["modes"]}
                elif args.command == "limits":
                    result = limits_report(result)
            elif args.command == "fan":
                result = backend.fancurve()
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
