# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Named profiles: platform mode, custom-mode limits and an optional fan curve in ~/.config/loqctl/profiles."""
from __future__ import annotations

import json
import os
import re

from .api import http_client
from .common import FIELDS, ControlError, sudo_run
from .curvestore import check_curve, config_dir, read_json_file, write_json_atomic, writable_points
from .fancurve import MAX_POINTS, POINT_FIELDS, Scale
from .hardware import run_changes

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
KEYS = {"version", "mode", "limits", "fancurve"}
ERRORS = (ControlError, OSError, ValueError, http_client.HTTPException)


def profiles_dir():
    return config_dir() / "profiles"


def profile_path(name):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ControlError("Profile names use letters, digits, - and _ (up to 64 characters).")
    return profiles_dir() / (name + ".json")


def validate_profile(data):
    """Raise ControlError unless `data` is a well-formed profile; needs no hardware."""
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ControlError("Not a version 1 profile.")
    if set(data) - KEYS:
        raise ControlError("Profile has unknown keys.")
    if not isinstance(data.get("mode"), str):
        raise ControlError("Profile mode must be a string.")
    limits, curve = data.get("limits", {}), data.get("fancurve")
    if not isinstance(limits, dict):
        raise ControlError("Profile limits must be an object.")
    for key, value in limits.items():
        if key not in FIELDS:
            raise ControlError(f"Unknown limit {key[:40]!r} in profile.")
        if type(value) is not int:
            raise ControlError(f"Limit {key} must be an integer.")
    if curve is not None:
        if not isinstance(curve, list) or not 1 <= len(curve) <= MAX_POINTS:
            raise ControlError(f"Fan curve must be a list of 1 to {MAX_POINTS} points.")
        for point in curve:
            if not isinstance(point, dict) or any(k not in POINT_FIELDS or type(v) is not int for k, v in point.items()):
                raise ControlError("Each fan curve point must be an object of integer fan curve fields.")
    if (limits or curve is not None) and data["mode"] != "custom":
        raise ControlError("Limits and a fan curve need mode custom.")


def load_profile(name):
    data = read_json_file(profile_path(name))
    validate_profile(data)
    return data


def list_profiles():
    found = []
    for path in sorted(profiles_dir().glob("*.json")):
        if not NAME.fullmatch(path.stem):
            continue
        try:
            data = load_profile(path.stem)
        except ControlError as exc:
            found.append({"name": path.stem, "error": str(exc)})
        else:
            found.append({"name": path.stem, "mode": data["mode"], "limits": len(data.get("limits", {})),
                          "fancurve": "fancurve" in data})
    return found


def save_profile(backend, name):
    """Write the current mode, and in custom mode the limits and fan curve, as profile `name`."""
    path = profile_path(name)
    caps = backend.controls()
    mode = caps.get("mode")
    if not mode:
        raise ControlError("The platform mode is unreadable; nothing to save.")
    data = {"version": 1, "mode": mode}
    if mode == "custom":
        data["limits"] = {k: a["current_value"] for k, a in caps.get("attributes", {}).items()
                          if k in FIELDS and type(a.get("current_value")) is int}
        curve = backend.fancurve()
        if curve.get("available"):
            data["fancurve"] = writable_points(curve["points"])
    validate_profile(data)
    return write_json_atomic(path, data)


def check_against_hardware(data, caps, curve):
    """Raise ControlError when the firmware would refuse any part of the profile."""
    modes = caps.get("modes", [])
    if data["mode"] not in modes:
        raise ControlError(f"Mode {data['mode']} is not available. Choose one of {', '.join(modes)}.")
    for key, value in data.get("limits", {}).items():
        item = caps.get("attributes", {}).get(key)
        if not item or not item.get("available"):
            raise ControlError(f"{key} is not available on this machine.")
        lo, hi, step = item["min_value"], item["max_value"], item["scalar_increment"]
        if not lo <= value <= hi:
            raise ControlError(f"{key} must be between {lo} and {hi} {item['unit']}.")
        if step > 0 and (value - lo) % step:
            raise ControlError(f"{key} must advance in steps of {step} from {lo}.")
    if "fancurve" in data:
        if not curve.get("available"):
            raise ControlError(f"The profile has a fan curve but {curve.get('reason', 'none is available')}.")
        reason = check_curve(curve["points"], data["fancurve"], Scale(curve.get("unit"), curve.get("max_rpm")))
        if reason:
            raise ControlError("Fan curve: " + reason)


def run_sudo_json(args):
    stdout, stderr = sudo_run(args)
    try:
        results = json.loads(stdout)
    except ValueError as exc:
        raise ControlError(stderr or "sudo returned unreadable output.") from exc
    if not isinstance(results, list) or not all(isinstance(r, dict) and "status" in r for r in results):
        raise ControlError(stderr or "sudo returned unreadable output.")
    return results


def current_value(caps, key):
    return caps.get("mode") if key == "mode" else caps.get("attributes", {}).get(key, {}).get("current_value")


def write_changes(backend, changes):
    """Mode or limit writes as the right user; the firmware read-back verifies each one."""
    if not (backend.remote or os.geteuid() == 0):
        return run_sudo_json(["_apply", json.dumps(changes)])

    def write(key, value):
        backend.write(key, value)

    return run_changes(changes, lambda key: current_value(backend.controls(), key), write, ERRORS)


def write_points(backend, points):
    if backend.remote or os.geteuid() == 0:
        return backend.write_fancurve(points)
    return run_sudo_json(["_fancurve", json.dumps(points)])


def apply_profile(backend, name):
    """Validate the whole profile, then write mode, limits and curve in that order, stopping at
    the first stage that fails. Returns {"profile", "applied", "results", "fancurve"}."""
    data = load_profile(name)
    caps = backend.controls()
    curve = backend.fancurve() if "fancurve" in data else {}
    check_against_hardware(data, caps, curve)
    report = {"profile": name, "applied": False, "previous_mode": caps.get("mode"), "results": [], "fancurve": []}
    stages = []
    if caps.get("mode") != data["mode"]:
        stages.append([["mode", data["mode"]]])
    if data.get("limits"):
        stages.append([[k, v] for k, v in data["limits"].items()])
    for changes in stages:
        try:
            results = write_changes(backend, changes)
        except ERRORS as exc:
            results = [{"key": k, "requested": v, "status": "FAILED", "error": str(exc)} for k, v in changes]
        report["results"] += results
        if any(r["status"] != "APPLIED" for r in results):
            return report
    if "fancurve" in data:
        try:
            report["fancurve"] = write_points(backend, data["fancurve"])
        except ERRORS as exc:
            report["fancurve"] = [{"key": "fancurve", "status": "FAILED", "error": str(exc)}]
        if any(r["status"] != "APPLIED" for r in report["fancurve"]):
            return report
    report["applied"] = True
    return report
