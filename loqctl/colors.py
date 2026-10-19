# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 loqctl contributors
"""Color conversion helpers and the ANSI palette."""
from __future__ import annotations

from .common import ControlError, _lazy, clamp

colorsys = _lazy("colorsys")
curses = _lazy("curses")

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
