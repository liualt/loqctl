# SPDX-License-Identifier: GPL-3.0-or-later
"""make install / uninstall into temp prefixes only."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@unittest.skipUnless(shutil.which("make"), "make not installed")
class MakefileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.prefix = os.path.join(self.tmp, "p")
        self.link = os.path.join(self.prefix, "bin", "loqctl")
        self.lib = os.path.join(self.prefix, "lib", "loqctl")

    def make(self, target, prefix=None):
        return subprocess.run(["make", "-C", ROOT, target, "PREFIX=" + (prefix or self.prefix)],
                              capture_output=True, text=True)

    def test_install_and_reinstall(self):
        self.assertEqual(self.make("install").returncode, 0)
        self.assertEqual(os.readlink(self.link), self.lib + "/loqctl.py")
        self.assertEqual(self.make("install").returncode, 0)
        self.assertFalse(os.path.exists(self.lib + ".new"))

    def test_relative_prefix_rejected(self):
        r = self.make("install", "rel")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("absolute", r.stderr)

    def test_uninstall_keeps_foreign_file(self):
        os.makedirs(os.path.dirname(self.link))
        with open(self.link, "w") as f:
            f.write("x")
        self.make("uninstall")
        self.assertTrue(os.path.exists(self.link))

    def test_uninstall_removes_own_symlink(self):
        self.make("install")
        self.make("uninstall")
        self.assertFalse(os.path.lexists(self.link))
        self.assertFalse(os.path.exists(self.lib))


if __name__ == "__main__":
    unittest.main()
