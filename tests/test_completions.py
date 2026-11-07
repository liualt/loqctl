# SPDX-License-Identifier: GPL-3.0-or-later
"""The shell completion files name every subcommand and settable attribute."""
import contextlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest

from loqctl.cli import main
from loqctl.common import FIELDS

DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contrib", "completions")


class CompletionTests(unittest.TestCase):
    def test_files_name_every_subcommand_and_field(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            main(["--help"])
        commands = re.search(r"\{([a-z,]+)\}", out.getvalue()).group(1).split(",")
        self.assertIn("kb", commands)
        kb_out = io.StringIO()
        with contextlib.redirect_stdout(kb_out), self.assertRaises(SystemExit):
            main(["kb", "--help"])
        kb_actions = re.search(r"status(?:\|\w+)+", kb_out.getvalue()).group(0).split("|")
        for name in ("loqctl.bash", "_loqctl", "loqctl.fish"):
            with open(os.path.join(DIR, name)) as f:
                text = f.read()
            words = set(re.findall(r"-{0,2}[\w-]+", text))
            for word in (*commands, *FIELDS, *kb_actions):
                self.assertIn(word, words, f"{name} lacks {word}")
            for opt in ("--watch", "--socket", "--version", "--uid"):
                # fish spells long options as `-l name`
                self.assertTrue(opt in words or f"-l {opt[2:]}" in text, f"{name} lacks {opt}")

    def _modes(self, shell, root):
        """Run the mode lookup of one completion file with /sys redirected to root."""
        with open(os.path.join(DIR, "loqctl.bash" if shell == "bash" else "loqctl.fish")) as f:
            text = f.read().replace("/sys/", root + "/")
        if shell == "bash":
            script = text + '\nCOMP_WORDS=(loqctl mode ""); COMP_CWORD=2; _loqctl; echo "${COMPREPLY[*]}"'
        else:
            script = text + "\n__loqctl_modes | string join ' '"
        return subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=20).stdout.split()

    def test_mode_choices_prefer_gamezone_class_device(self):
        for shell in ("bash", "fish"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as root:
                for name, dev, modes in (("other", "platform-profile/platform-profile-0", "low balanced"),
                                         ("lenovo-wmi-gamezone", "platform-profile/platform-profile-1", "quiet performance")):
                    d = os.path.join(root, "class", dev)
                    os.makedirs(d)
                    for fn, val in (("name", name), ("choices", modes)):
                        with open(os.path.join(d, fn), "w") as f:
                            f.write(val + "\n")
                os.makedirs(os.path.join(root, "firmware", "acpi"))
                with open(os.path.join(root, "firmware", "acpi", "platform_profile_choices"), "w") as f:
                    f.write("balanced custom\n")
                self.assertEqual(self._modes(shell, root), ["quiet", "performance"])
                shutil.rmtree(os.path.join(root, "class"))
                self.assertEqual(self._modes(shell, root), ["balanced", "custom"])


if __name__ == "__main__":
    unittest.main()
