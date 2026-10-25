# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared fixtures for the test modules: temp-dir sysfs and a bare Dashboard."""
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from loqctl import hardware as m_hardware, ui as m_ui


def make_root(test):
    """A per-test temp directory, removed when the test finishes."""
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    return Path(temp.name)


class SysfsCase(unittest.TestCase):
    """A simulated sysfs with one custom-mode platform profile and one firmware attribute."""

    def setUp(self):
        self.root = make_root(self)
        profile = self.root / "class/platform-profile/platform-profile-0"
        profile.mkdir(parents=True)
        for key, value in {"name": "lenovo-wmi-gamezone", "profile": "custom",
                           "choices": "low-power balanced performance custom"}.items():
            (profile / key).write_text(value)
        self.attr = self.root / "class/firmware-attributes/lenovo-wmi-other-0/attributes/ppt_pl1_spl"
        self.attr.mkdir(parents=True)
        for key, value in {"current_value": 50, "min_value": 50, "max_value": 100,
                           "default_value": 60, "scalar_increment": 5}.items():
            (self.attr / key).write_text(str(value))
        self.hw = m_hardware.Hardware(self.root)

    def add_attr(self, key, current, lo, hi, step, default):
        path = self.attr.parent / key
        path.mkdir(exist_ok=True)
        for name, value in {"current_value": current, "min_value": lo, "max_value": hi,
                            "default_value": default, "scalar_increment": step}.items():
            (path / name).write_text(str(value))

    def dashboard(self):
        dashboard = object.__new__(m_ui.Dashboard)
        dashboard.help = False
        dashboard.selected = 1
        dashboard.pending = {}
        dashboard.fields = {}
        dashboard.invalid = set()
        dashboard.sudo_expired = False
        dashboard.apply_results = []
        dashboard.show_results = False
        dashboard.backend = type("Backend", (), {"remote": True})()
        dashboard.backend.write = self.hw.set_value
        dashboard.backend.refresh = lambda: None
        dashboard.backend.controls = self.hw.capabilities
        return dashboard
