# SPDX-License-Identifier: GPL-3.0-or-later
"""The shell completion files name every subcommand and settable attribute."""
import contextlib
import io
import os
import re
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


if __name__ == "__main__":
    unittest.main()
