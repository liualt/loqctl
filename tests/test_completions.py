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

    def _modes(self, shell, root, stderr=False):
        """Run the mode lookup of one completion file with /sys redirected to root."""
        with open(os.path.join(DIR, "loqctl.bash" if shell == "bash" else "loqctl.fish")) as f:
            text = f.read().replace("/sys/", root + "/")
        if shell == "bash":
            script = text + '\nCOMP_WORDS=(loqctl mode ""); COMP_CWORD=2; _loqctl; echo "${COMPREPLY[*]}"'
        else:
            script = text + "\n__loqctl_modes | string join ' '"
        done = subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=20)
        return (done.stdout.split(), done.stderr) if stderr else done.stdout.split()

    def test_mode_choices_without_name_file_print_no_error(self):
        for shell in ("bash", "fish"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as root:
                d = os.path.join(root, "class", "platform-profile", "p0")
                os.makedirs(d)
                with open(os.path.join(d, "choices"), "w") as f:
                    f.write("quiet performance\n")
                open(os.path.join(d, "profile"), "w").close()
                self.assertEqual(self._modes(shell, root, stderr=True), (["quiet", "performance"], ""))

    def test_mode_choices_need_a_profile_file(self):
        for shell in ("bash", "fish"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as root:
                d = os.path.join(root, "class", "platform-profile", "p0")
                os.makedirs(d)
                with open(os.path.join(d, "choices"), "w") as f:
                    f.write("quiet performance\n")
                self.assertEqual(self._modes(shell, root), ["low-power", "balanced", "performance", "max-power", "custom"])

    def test_zsh_file_parses(self):
        if shutil.which("zsh"):
            done = subprocess.run(["zsh", "-n", os.path.join(DIR, "_loqctl")], capture_output=True, text=True, timeout=20)
            self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_kb_effect_arguments_complete(self):
        flags = {"--speed", "--brightness"}
        cases = {
            # fish offers options only once the word starts with a dash
            "bash": (("kb wave ", {"ltr", "rtl"} | flags), ("kb hue ", flags), ("kb wave --speed ", set()), ("kb off ", set())),
            "fish": (("kb wave ", {"ltr", "rtl"}), ("kb wave -", flags), ("kb hue -", flags), ("kb off -", set())),
        }
        for shell, lines in cases.items():
            if not shutil.which(shell):
                continue
            for line, want in lines:
                with self.subTest(shell=shell, line=line):
                    self.assertEqual(self._complete(shell, line), want)

    def _complete(self, shell, line):
        """Words offered for `loqctl <line>` by one completion file."""
        with open(os.path.join(DIR, "loqctl.bash" if shell == "bash" else "loqctl.fish")) as f:
            text = f.read()
        if shell == "bash":
            words = " ".join(f'"{w}"' for w in ("loqctl", *line.split(), ""))
            script = text + f'\nCOMP_WORDS=({words}); COMP_CWORD=$((${{#COMP_WORDS[@]}} - 1)); _loqctl; echo "${{COMPREPLY[*]}}"'
        else:
            script = text + f'\ncomplete -C "loqctl {line}" | string replace -r "\\t.*" ""'
        out = subprocess.run([shell, "-c", script], capture_output=True, text=True, timeout=20).stdout
        return set(out.split()) - {"-h", "--help", "--socket", "--version"}

    def test_mode_choices_prefer_gamezone_class_device(self):
        for shell in ("bash", "fish"):
            if not shutil.which(shell):
                continue
            with self.subTest(shell=shell), tempfile.TemporaryDirectory() as root:
                for name, dev, modes in (("other", "platform-profile/platform-profile-0", "low balanced"),
                                         ("lenovo-wmi-gamezone", "platform-profile/platform-profile-1", "quiet performance")):
                    d = os.path.join(root, "class", dev)
                    os.makedirs(d)
                    for fn, val in (("name", name), ("choices", modes), ("profile", "balanced")):
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
