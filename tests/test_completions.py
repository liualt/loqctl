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
        for name in ("loqctl.bash", "_loqctl", "loqctl.fish"):
            with open(os.path.join(DIR, name)) as f:
                words = set(re.findall(r"[\w-]+", f.read()))
            for word in (*commands, *FIELDS, "--watch", "--socket", "--version"):
                self.assertIn(word.lstrip("-"), {w.lstrip("-") for w in words}, f"{name} lacks {word}")


if __name__ == "__main__":
    unittest.main()
