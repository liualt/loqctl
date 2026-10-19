#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Single-file entry point for the loqctl package."""
import sys
from pathlib import Path

# sudo runs this as `python3 -I`, which drops the script directory from sys.path.
HERE = str(Path(__file__).resolve().parent)
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from loqctl.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
