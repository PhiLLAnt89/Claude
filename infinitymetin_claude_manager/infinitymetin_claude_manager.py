#!/usr/bin/env python3
"""InfinityMetin Claude Manager.

A Game Boy Color style office where your Claude Code agents work on your project.
Type a task, pick a role, press Enter: an agent walks to a desk and a real Claude Code
session runs the task in your project folder.  When it finishes, read the answer in the
dialog box and type a reply; the same session continues.

    python infinitymetin_claude_manager.py --project D:/Metin2/Server

Without Claude Code installed (or with --sim) the office runs in simulation mode: the
agents pretend to work, so the game always starts.  Press F1 in the game for the keys.

Only pygame is needed:  pip install pygame
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from array import array
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import pygame

APP_NAME = "InfinityMetin Claude Manager"
VERSION = "1.0.0"

# ---------------------------------------------------------------------------------------------
# Palette: the only four colours ever drawn on the screen
# ---------------------------------------------------------------------------------------------

DARKEST = (15, 56, 15)       # darkest / text
SHADOW = (48, 98, 48)        # muted shadow
LIGHT = (139, 172, 15)       # light accent
BACKGROUND = (155, 188, 15)  # background / screen glow
PALETTE = (DARKEST, SHADOW, LIGHT, BACKGROUND)
COLORKEY = (255, 0, 255)     # transparency marker for sprites; never reaches the screen

# ---------------------------------------------------------------------------------------------
# Screen geometry (logical pixels; everything is scaled up as whole pixels)
# ---------------------------------------------------------------------------------------------

TILE = 16
MAP_W, MAP_H = 20, 14
MAP_PX_W, MAP_PX_H = MAP_W * TILE, MAP_H * TILE      # 320 x 224
PANEL_X, PANEL_W = MAP_PX_W, 160
DIALOG_Y, DIALOG_H = MAP_PX_H, 96
CANVAS_W, CANVAS_H = 480, 320
BEZEL_L, BEZEL_T, BEZEL_R, BEZEL_B = 8, 8, 8, 24
DEVICE_W = CANVAS_W + BEZEL_L + BEZEL_R
DEVICE_H = CANVAS_H + BEZEL_T + BEZEL_B
FPS = 60
CHAR_W, LINE_H = 6, 8
DIALOG_COLS = 77
PANEL_COLS = 26

# ---------------------------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------------------------

FINAL_ANSWER_RULE = (
    " Your final answer is shown inside a tiny 8-bit game window, so end with a plain-text "
    "summary: short lines under 70 characters, at most 30 lines, no markdown, no tables, "
    "no code fences."
)


@dataclass(frozen=True)
class Role:
    name: str
    short: str
    letter: str
    prompt: str


ROLES: Tuple[Role, ...] = (
    Role("DEVELOPER", "DEV", "D",
         "You are a software developer on this project. Implement the task fully: read the "
         "relevant code first, make minimal changes that fit the codebase, and check they work."
         + FINAL_ANSWER_RULE),
    Role("TESTER", "TST", "T",
         "You are the tester on this project. Do not implement features. Write or run tests, "
         "reproduce bugs and report what passed and what failed with file and line references."
         + FINAL_ANSWER_RULE),
    Role("DESIGNER", "DSN", "S",
         "You are the designer on this project: UI, UX, layout, text, assets and data files. "
         "Make the requested design change or produce the requested asset or spec."
         + FINAL_ANSWER_RULE),
    Role("MANAGER", "MGR", "M",
         "You are the project manager. Do not change source code. Investigate the codebase, "
         "plan work, review changes and write short status reports and task lists that the "
         "developer, tester and designer can pick up." + FINAL_ANSWER_RULE),
)
ROLE_BY_NAME = {role.name: role for role in ROLES}

AGENT_NAMES = [
    "KIRA", "TOMO", "REN", "YUKI", "SORA", "NAO", "MIKA", "KAI", "HARU", "RIN", "AKI", "EMI",
    "JUN", "KENJI", "MAYA", "NIKO", "OSKAR", "PIA", "QUIN", "ROSA", "SAM", "TESSA", "UMA", "VIC",
    "WREN", "XAN", "YAEL", "ZED", "ARI", "BO", "CLEO", "DAX", "ELIO", "FAY", "GUS", "HANA",
    "IVO", "JADE", "KOJI", "LEA",
]

# ---------------------------------------------------------------------------------------------
# 5x7 pixel font (uppercase only, like a Game Boy)
# ---------------------------------------------------------------------------------------------

FONT_ROWS = {
    " ": ".....|.....|.....|.....|.....|.....|.....",
    "A": ".###.|#...#|#...#|#####|#...#|#...#|#...#",
    "B": "####.|#...#|#...#|####.|#...#|#...#|####.",
    "C": ".###.|#...#|#....|#....|#....|#...#|.###.",
    "D": "###..|#..#.|#...#|#...#|#...#|#..#.|###..",
    "E": "#####|#....|#....|####.|#....|#....|#####",
    "F": "#####|#....|#....|####.|#....|#....|#....",
    "G": ".###.|#...#|#....|#.###|#...#|#...#|.####",
    "H": "#...#|#...#|#...#|#####|#...#|#...#|#...#",
    "I": ".###.|..#..|..#..|..#..|..#..|..#..|.###.",
    "J": "..###|...#.|...#.|...#.|...#.|#..#.|.##..",
    "K": "#...#|#..#.|#.#..|##...|#.#..|#..#.|#...#",
    "L": "#....|#....|#....|#....|#....|#....|#####",
    "M": "#...#|##.##|#.#.#|#.#.#|#...#|#...#|#...#",
    "N": "#...#|#...#|##..#|#.#.#|#..##|#...#|#...#",
    "O": ".###.|#...#|#...#|#...#|#...#|#...#|.###.",
    "P": "####.|#...#|#...#|####.|#....|#....|#....",
    "Q": ".###.|#...#|#...#|#...#|#.#.#|#..#.|.##.#",
    "R": "####.|#...#|#...#|####.|#.#..|#..#.|#...#",
    "S": ".####|#....|#....|.###.|....#|....#|####.",
    "T": "#####|..#..|..#..|..#..|..#..|..#..|..#..",
    "U": "#...#|#...#|#...#|#...#|#...#|#...#|.###.",
    "V": "#...#|#...#|#...#|#...#|#...#|.#.#.|..#..",
    "W": "#...#|#...#|#...#|#.#.#|#.#.#|##.##|#...#",
    "X": "#...#|#...#|.#.#.|..#..|.#.#.|#...#|#...#",
    "Y": "#...#|#...#|.#.#.|..#..|..#..|..#..|..#..",
    "Z": "#####|....#|...#.|..#..|.#...|#....|#####",
    "0": ".###.|#...#|#..##|#.#.#|##..#|#...#|.###.",
    "1": "..#..|.##..|..#..|..#..|..#..|..#..|.###.",
    "2": ".###.|#...#|....#|...#.|..#..|.#...|#####",
    "3": "#####|...#.|..#..|...#.|....#|#...#|.###.",
    "4": "...#.|..##.|.#.#.|#..#.|#####|...#.|...#.",
    "5": "#####|#....|####.|....#|....#|#...#|.###.",
    "6": "..##.|.#...|#....|####.|#...#|#...#|.###.",
    "7": "#####|....#|...#.|..#..|.#...|.#...|.#...",
    "8": ".###.|#...#|#...#|.###.|#...#|#...#|.###.",
    "9": ".###.|#...#|#...#|.####|....#|...#.|.##..",
    ".": ".....|.....|.....|.....|.....|.##..|.##..",
    ",": ".....|.....|.....|.....|.##..|..#..|.#...",
    ":": ".....|.##..|.##..|.....|.##..|.##..|.....",
    ";": ".....|.##..|.##..|.....|.##..|..#..|.#...",
    "!": "..#..|..#..|..#..|..#..|..#..|.....|..#..",
    "?": ".###.|#...#|....#|...#.|..#..|.....|..#..",
    "-": ".....|.....|.....|#####|.....|.....|.....",
    "_": ".....|.....|.....|.....|.....|.....|#####",
    "+": ".....|..#..|..#..|#####|..#..|..#..|.....",
    "=": ".....|.....|#####|.....|#####|.....|.....",
    "/": "....#|....#|...#.|..#..|.#...|#....|#....",
    "\\": "#....|#....|.#...|..#..|...#.|....#|....#",
    "(": "..#..|.#...|#....|#....|#....|.#...|..#..",
    ")": "..#..|...#.|....#|....#|....#|...#.|..#..",
    "[": ".###.|.#...|.#...|.#...|.#...|.#...|.###.",
    "]": ".###.|...#.|...#.|...#.|...#.|...#.|.###.",
    "<": "...#.|..#..|.#...|#....|.#...|..#..|...#.",
    ">": ".#...|..#..|...#.|....#|...#.|..#..|.#...",
    "#": ".#.#.|.#.#.|#####|.#.#.|#####|.#.#.|.#.#.",
    "$": "..#..|.####|#.#..|.###.|..#.#|####.|..#..",
    "%": "##...|##..#|...#.|..#..|.#...|#..##|...##",
    "&": ".##..|#..#.|#..#.|.##..|#.#.#|#..#.|.##.#",
    "*": ".....|#.#.#|.###.|#####|.###.|#.#.#|.....",
    "'": "..#..|..#..|.#...|.....|.....|.....|.....",
    '"': ".#.#.|.#.#.|.#.#.|.....|.....|.....|.....",
    "@": ".###.|#...#|#.###|#.#.#|#.###|#....|.###.",
    "~": ".....|.....|.##.#|#.##.|.....|.....|.....",
    "^": "..#..|.#.#.|#...#|.....|.....|.....|.....",
    "{": "..##.|.#...|.#...|##...|.#...|.#...|..##.",
    "}": ".##..|...#.|...#.|...##|...#.|...#.|.##..",
    "|": "..#..|..#..|..#..|..#..|..#..|..#..|..#..",
    "`": ".#...|..#..|.....|.....|.....|.....|.....",
}


class PixelFont:
    """Blocky 5x7 glyphs, pre-rendered in each palette colour. Lowercase is drawn as uppercase."""

    def __init__(self) -> None:
        self.glyphs: Dict[Tuple[Tuple[int, int, int], int], Dict[str, pygame.Surface]] = {}
        for char, rows in FONT_ROWS.items():
            parts = rows.split("|")
            assert len(parts) == 7 and all(len(p) == 5 for p in parts), f"bad glyph {char!r}"

    def _glyph(self, char: str, color: Tuple[int, int, int], scale: int) -> pygame.Surface:
        table = self.glyphs.setdefault((color, scale), {})
        surface = table.get(char)
        if surface is None:
            rows = FONT_ROWS.get(char, FONT_ROWS["?"]).split("|")
            surface = pygame.Surface((5 * scale, 7 * scale))
            surface.fill(COLORKEY)
            surface.set_colorkey(COLORKEY)
            for y, row in enumerate(rows):
                for x, bit in enumerate(row):
                    if bit == "#":
                        surface.fill(color, (x * scale, y * scale, scale, scale))
            table[char] = surface
        return surface

    @staticmethod
    def clean(text: str) -> str:
        return "".join(c if c in FONT_ROWS else "?" for c in str(text).upper().replace("\t", "  "))

    def draw(self, target: pygame.Surface, text: str, x: int, y: int,
             color: Tuple[int, int, int] = DARKEST, scale: int = 1) -> int:
        for char in self.clean(text):
            if char != " ":
                target.blit(self._glyph(char, color, scale), (x, y))
            x += CHAR_W * scale
        return x

    @staticmethod
    def width(text: str, scale: int = 1) -> int:
        return len(text) * CHAR_W * scale


def wrap_text(text: str, width: int) -> List[str]:
    """Word-wraps to `width` columns; long words are cut. Keeps explicit line breaks."""
    lines: List[str] = []
    for paragraph in str(text).replace("\r", "").split("\n"):
        words = paragraph.split(" ")
        line = ""
        for word in words:
            while len(word) > width:
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:width])
                word = word[width:]
            if not line:
                line = word
            elif len(line) + 1 + len(word) <= width:
                line += " " + word
            else:
                lines.append(line)
                line = word
        lines.append(line)
    return lines


# ---------------------------------------------------------------------------------------------
# Pixel art: tiles and sprites as text
# ---------------------------------------------------------------------------------------------

def _rows(art: str) -> List[str]:
    rows = [line for line in art.strip("\n").split("\n")]
    assert len(rows) == 16 and all(len(r) == 16 for r in rows), "tile art must be 16x16:\n" + art
    return rows


INK = {"0": DARKEST, "1": SHADOW, "2": LIGHT, "3": BACKGROUND}

FLOOR = _rows("""
2333333323333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
2333333323333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
3333333333333333
""")

RUG = _rows("""
2323232323232323
3333333333333333
2333333333333332
3333333333333333
2333333333333332
3333333333333333
2333333333333332
3333333333333333
2333333333333332
3333333333333333
2333333333333332
3333333333333333
2333333333333332
3333333333333333
2333333333333332
2323232323232323
""")

WALL = _rows("""
0000000000000000
0222222202222222
0222222202222222
0111111101111111
0000000000000000
2222202222222022
2222202222222022
1111101111111011
0000000000000000
0222222202222222
0222222202222222
0111111101111111
0000000000000000
2222202222222022
2222202222222022
1111101111111011
""")

WHITEBOARD = _rows("""
0000000000000000
0222222202222222
0033333333333300
0031111111111300
0033333333333300
0033111113333300
0033333333333300
0033111111133300
0033333333333300
0033111133333300
0033333333333300
0000000000000000
0000000000000000
2222202222222022
2222202222222022
1111101111111011
""")

DOOR = _rows("""
0000000000000000
0000000000000000
0033333333333300
0030000000000300
0030222222220300
0030222222220300
0030222222220300
0030222222220300
0030200000020300
0030222222220300
0030222222220300
0030222222220300
0030222222220300
0030000000000300
0033333333333300
0000000000000000
""")

DESK_L = _rows("""
................
.....0000000....
.....0222220....
.....0200220....
.....0222020....
.....0220220....
.....0222220....
.....0000000....
.......000......
0000000000000000
0333333333333330
0333333333333330
0333333333333330
0111111111111110
0000000000000000
.0............0.
""")

DESK_R = _rows("""
................
................
................
................
................
................
................
................
................
0000000000000000
0300000003300030
0302222203303030
0300000003300030
0111111111111110
0000000000000000
.0............0.
""")

CHAIR = _rows("""
................
................
....00000000....
....01111110....
....01111110....
....00000000....
......0..0......
....00000000....
....03333330....
....03333330....
....00000000....
.......00.......
.......00.......
....00000000....
....0......0....
................
""")

COUCH_L = _rows("""
................
................
..00000000000000
..01111111111111
..01111111111111
..00000000000000
..03333333333333
..03222222222222
..03222222222222
..03333333333333
..00000000000000
..0.............
..0.............
..00............
................
................
""")

PLANT = _rows("""
................
.....000000.....
....01221220....
...0122212210...
...0212122120...
...0122212210...
....01212120....
.....000000.....
......0000......
.....000000.....
.....011110.....
.....011110.....
......0110......
......0000......
................
................
""")

COFFEE = _rows("""
................
....00000000....
....03333330....
....03000030....
....03333330....
....00000000....
....01111110....
....01100110....
....01102110....
....01102110....
....01111110....
....00000000....
....03333330....
....00000000....
................
................
""")

COOLER = _rows("""
................
.....0000.......
....022220......
....023320......
....022220......
....022220......
....000000......
....011110......
....010010......
....011110......
....011110......
....011110......
....011110......
....000000......
................
................
""")

SERVER = _rows("""
................
...0000000000...
...0111111110...
...0122222210...
...0100000010...
...0111111110...
...0122222210...
...0100000010...
...0111111110...
...0122222210...
...0100000010...
...0111111110...
...0122222210...
...0000000000...
................
................
""")

# Sprite legend: k outline, H hat/top hair, h hair, f face, e eye, s shirt, x shirt accent,
# n hand, p trousers, b shoe, . transparent
SPRITE_DOWN_A = _rows("""
................
.....kkkkkk.....
....kHHHHHHk....
...kHHHHHHHHk...
...khhhhhhhhk...
...khffffffhk...
...kfeffffefk...
...kffffffffk...
....kffffffk....
...kssssssssk...
..kksssxxssskk..
..knsssxxsssnk..
...kssssssssk...
....kppppppk....
....kppkkppk....
....kbbkkbbk....
""")

SPRITE_DOWN_B = SPRITE_DOWN_A[:13] + _rows("""
................
................
................
................
................
................
................
................
................
................
................
................
................
....kppppppk....
....kppk.kbbk...
....kbbk........
""")[13:]

SPRITE_UP_A = _rows("""
................
.....kkkkkk.....
....kHHHHHHk....
...kHHHHHHHHk...
...khhhhhhhhk...
...khhhhhhhhk...
...khhhhhhhhk...
...khhhhhhhhk...
....khhhhhhk....
...kssssssssk...
..kksssssssskk..
..knssssssssnk..
...kssssssssk...
....kppppppk....
....kppkkppk....
....kbbkkbbk....
""")
SPRITE_UP_B = SPRITE_UP_A[:13] + SPRITE_DOWN_B[13:]

SPRITE_LEFT_A = _rows("""
................
.....kkkkkk.....
....kHHHHHHk....
...kHHHHHHHHk...
...khhhhhhhhk...
...kffffhhhhk...
...kfeffhhhhk...
...kffffhhhhk...
....kfffhhhk....
...kssssssssk...
...ksxssssssk...
...knxssssssk...
...kssssssssk...
....kppppppk....
....kppkkppk....
....kbbkkbbk....
""")
SPRITE_LEFT_B = SPRITE_LEFT_A[:13] + _rows("""
................
................
................
................
................
................
................
................
................
................
................
................
................
....kppppppk....
...kppk..kppk...
...kbbk..kbbk...
""")[13:]

# Per-role looks: hat colour, hair colour, shirt colour, accent colour ("checker" = pattern)
ROLE_LOOKS = {
    "DEVELOPER": {"H": DARKEST, "h": SHADOW, "s": LIGHT, "x": LIGHT},
    "TESTER": {"H": SHADOW, "h": SHADOW, "s": "checker", "x": "checker"},
    "DESIGNER": {"H": LIGHT, "h": DARKEST, "s": SHADOW, "x": LIGHT},
    "MANAGER": {"H": DARKEST, "h": DARKEST, "s": LIGHT, "x": DARKEST},
}
FIXED_INK = {"k": DARKEST, "f": LIGHT, "e": DARKEST, "n": LIGHT, "p": SHADOW, "b": DARKEST}


def make_surface(rows: List[str], ink: Callable[[str, int, int], Optional[Tuple[int, int, int]]],
                 flip: bool = False) -> pygame.Surface:
    surface = pygame.Surface((TILE, TILE))
    surface.fill(COLORKEY)
    surface.set_colorkey(COLORKEY)
    for y, row in enumerate(rows):
        for x, char in enumerate(row):
            color = ink(char, x, y)
            if color is not None:
                surface.set_at((x, y), color)
    return pygame.transform.flip(surface, True, False) if flip else surface


def tile_surface(rows: List[str]) -> pygame.Surface:
    return make_surface(rows, lambda c, x, y: INK.get(c))


def sprite_surface(rows: List[str], look: Dict[str, Any], flip: bool = False) -> pygame.Surface:
    def ink(char: str, x: int, y: int) -> Optional[Tuple[int, int, int]]:
        if char in FIXED_INK:
            return FIXED_INK[char]
        if char in look:
            color = look[char]
            if color == "checker":
                return LIGHT if (x + y) % 2 == 0 else SHADOW
            return color
        return None

    return make_surface(rows, ink, flip)


class Art:
    """All tiles and sprites, built once from the text art above."""

    def __init__(self) -> None:
        self.tiles = {
            ".": tile_surface(FLOOR), "L": tile_surface(RUG), "#": tile_surface(WALL),
            "W": tile_surface(WHITEBOARD), "~": tile_surface(DOOR), "D": tile_surface(DESK_L),
            "d": tile_surface(DESK_R), "c": tile_surface(CHAIR), "S": tile_surface(COUCH_L),
            "s": pygame.transform.flip(tile_surface(COUCH_L), True, False),
            "P": tile_surface(PLANT), "C": tile_surface(COFFEE), "K": tile_surface(COOLER),
            "R": tile_surface(SERVER),
        }
        self.sprites: Dict[str, Dict[str, List[pygame.Surface]]] = {}
        for role_name, look in ROLE_LOOKS.items():
            down_a, down_b = sprite_surface(SPRITE_DOWN_A, look), sprite_surface(SPRITE_DOWN_B, look)
            up_a, up_b = sprite_surface(SPRITE_UP_A, look), sprite_surface(SPRITE_UP_B, look)
            left_a, left_b = sprite_surface(SPRITE_LEFT_A, look), sprite_surface(SPRITE_LEFT_B, look)
            self.sprites[role_name] = {
                "down": [down_a, down_b, down_a, pygame.transform.flip(down_b, True, False)],
                "up": [up_a, up_b, up_a, pygame.transform.flip(up_b, True, False)],
                "left": [left_a, left_b, left_a, left_b],
                "right": [pygame.transform.flip(s, True, False) for s in (left_a, left_b, left_a, left_b)],
            }


# ---------------------------------------------------------------------------------------------
# Chiptune sound engine (square waves generated in code, no files)
# ---------------------------------------------------------------------------------------------

class Chiptune:
    """Generates Game Boy style square-wave effects into pygame.mixer.Sound buffers."""

    def __init__(self, enabled: bool = True) -> None:
        self.muted = False
        self.ready = False
        self.sounds: Dict[str, pygame.mixer.Sound] = {}
        if not enabled:
            return
        for attempt in range(2):
            try:
                if not pygame.mixer.get_init():
                    pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=512)
                self.ready = True
                break
            except pygame.error:
                os.environ["SDL_AUDIODRIVER"] = "dummy"  # no sound device: keep the game running
        if not self.ready:
            return
        self.sounds = {
            "spawn": self._synth([(700, 1500, 0.09, 0.5)]),
            "complete": self._synth([(659, 659, 0.09, 0.5), (0, 0, 0.02, 0.5), (988, 988, 0.22, 0.5)]),
            "assign": self._synth([(440, 440, 0.05, 0.25), (660, 660, 0.06, 0.25)]),
            "reply": self._synth([(880, 880, 0.05, 0.5), (1320, 1320, 0.08, 0.5)]),
            "error": self._synth([(300, 140, 0.25, 0.25)]),
            "select": self._synth([(1200, 1200, 0.02, 0.5)], volume=0.25),
            "cancel": self._synth([(500, 200, 0.15, 0.5)]),
        }

    def _synth(self, segments: List[Tuple[float, float, float, float]], volume: float = 0.4) -> pygame.mixer.Sound:
        """segments: (start Hz, end Hz, seconds, duty). 0 Hz is silence. Each segment decays."""
        rate, fmt, channels = pygame.mixer.get_init()
        samples: List[int] = []
        phase = 0.0
        for start, end, seconds, duty in segments:
            count = max(1, int(rate * seconds))
            for i in range(count):
                t = i / count
                freq = start + (end - start) * t
                if freq <= 0:
                    samples.append(0)
                    continue
                phase = (phase + freq / rate) % 1.0
                wave = 1.0 if phase < duty else -1.0
                envelope = min(1.0, i / (rate * 0.004)) * (1.0 - 0.6 * t)
                samples.append(int(32767 * volume * envelope * wave))
        bits = abs(fmt)
        if bits == 8:
            data = array("b" if fmt < 0 else "B", [(s >> 8) + (0 if fmt < 0 else 128) for s in samples for _ in range(channels)])
        else:
            data = array("h" if fmt < 0 else "H", [s + (0 if fmt < 0 else 32768) for s in samples for _ in range(channels)])
        return pygame.mixer.Sound(buffer=data.tobytes())

    def play(self, name: str) -> None:
        if self.ready and not self.muted and name in self.sounds:
            self.sounds[name].play()


# ---------------------------------------------------------------------------------------------
# Office map
# ---------------------------------------------------------------------------------------------

OFFICE_MAP = [
    "####W#####W#####W###",
    "#Dd.Dd.Dd.Dd.Dd.Dd.#",
    "#c..c..c..c..c..c..#",
    "#..................#",
    "#Dd.Dd.Dd.Dd.Dd.Dd.#",
    "#c..c..c..c..c..c..#",
    "#..................#",
    "#Dd.Dd.Dd.Dd.Dd.Dd.#",
    "#c..c..c..c..c..c..#",
    "#..................#",
    "#PLLLLLLLLLLLLLLLLR#",
    "#SsLLSsLLLLSsLLSsCK#",
    "#LLLLLLLLLLLLLLLLLP#",
    "########~~##########",
]
WALKABLE = {".", "c", "L"}
assert len(OFFICE_MAP) == MAP_H and all(len(r) == MAP_W for r in OFFICE_MAP)


@dataclass
class Desk:
    index: int
    tile: Tuple[int, int]       # left desk tile (monitor)
    seat: Tuple[int, int]       # where the agent sits
    agent: Optional["Agent"] = None  # reserved by / occupied by

    @property
    def free(self) -> bool:
        return self.agent is None


class Office:
    def __init__(self) -> None:
        self.desks: List[Desk] = []
        self.lounge: List[Tuple[int, int]] = []
        for y, row in enumerate(OFFICE_MAP):
            for x, char in enumerate(row):
                if char == "D":
                    self.desks.append(Desk(len(self.desks) + 1, (x, y), (x, y + 1)))
                elif char == "L":
                    self.lounge.append((x, y))
        self.seats = {desk.seat: desk for desk in self.desks}

    @staticmethod
    def tile(x: int, y: int) -> str:
        return OFFICE_MAP[y][x] if 0 <= x < MAP_W and 0 <= y < MAP_H else "#"

    def walkable(self, x: int, y: int) -> bool:
        return self.tile(x, y) in WALKABLE

    def find_path(self, start: Tuple[int, int], goal: Tuple[int, int], mover: "Agent") -> List[Tuple[int, int]]:
        """Breadth-first search on the tile grid; other agents' seats are blocked."""
        if start == goal:
            return []
        blocked = {seat for seat, desk in self.seats.items() if desk.agent is not None and desk.agent is not mover}
        blocked.discard(goal)
        previous: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {start: None}
        frontier = deque([start])
        while frontier:
            current = frontier.popleft()
            if current == goal:
                break
            cx, cy = current
            for nx, ny in ((cx + 1, cy), (cx - 1, cy), (cx, cy + 1), (cx, cy - 1)):
                nxt = (nx, ny)
                if nxt in previous or not self.walkable(nx, ny) or nxt in blocked:
                    continue
                previous[nxt] = current
                frontier.append(nxt)
        if goal not in previous:
            return []
        path = []
        node: Optional[Tuple[int, int]] = goal
        while node is not None and node != start:
            path.append(node)
            node = previous[node]
        path.reverse()
        return path


# ---------------------------------------------------------------------------------------------
# Tasks and agents
# ---------------------------------------------------------------------------------------------

STATUS_NEW, STATUS_GO, STATUS_RUN, STATUS_DONE, STATUS_FAIL = "NEW", "GO", "RUN", "DONE", "FAIL"


@dataclass
class Task:
    id: int
    name: str
    role: str
    status: str = STATUS_NEW
    transcript: List[Tuple[str, str]] = field(default_factory=list)  # (speaker, text)
    pending_prompt: str = ""      # what the next agent run should send
    session_id: str = ""          # Claude session to resume
    cost: float = 0.0
    agent_name: str = ""
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    unread: bool = False
    fail_reason: str = ""
    tool_calls: int = 0

    @property
    def role_obj(self) -> Role:
        return ROLE_BY_NAME[self.role]

    @property
    def active(self) -> bool:
        return self.status in (STATUS_GO, STATUS_RUN)

    def elapsed(self) -> float:
        if not self.started:
            return 0.0
        return (self.finished or time.time()) - self.started

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name, "role": self.role, "status": self.status,
            "transcript": self.transcript, "session_id": self.session_id, "cost": self.cost,
            "agent_name": self.agent_name, "created": self.created, "started": self.started,
            "finished": self.finished, "unread": self.unread, "fail_reason": self.fail_reason,
            "tool_calls": self.tool_calls,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Task":
        task = cls(int(data["id"]), str(data.get("name", ""))[:120], str(data.get("role", ROLES[0].name)))
        if task.role not in ROLE_BY_NAME:
            task.role = ROLES[0].name
        task.status = str(data.get("status", STATUS_NEW))
        task.transcript = [(str(a), str(b)) for a, b in data.get("transcript", [])]
        task.session_id = str(data.get("session_id", ""))
        task.cost = float(data.get("cost", 0.0))
        task.agent_name = str(data.get("agent_name", ""))
        task.created = float(data.get("created", time.time()))
        task.started = float(data.get("started", 0.0))
        task.finished = float(data.get("finished", 0.0))
        task.unread = bool(data.get("unread", False))
        task.fail_reason = str(data.get("fail_reason", ""))
        task.tool_calls = int(data.get("tool_calls", 0))
        if task.status in (STATUS_GO, STATUS_RUN):  # the game was closed while it ran
            task.status = STATUS_FAIL
            task.fail_reason = "INTERRUPTED - REPLY TO CONTINUE"
            task.finished = task.finished or time.time()
        return task


IDLE, TO_DESK, WORKING, CELEBRATE, RETURNING = "IDLE", "TO_DESK", "WORKING", "CELEBRATE", "RETURNING"


class Agent:
    def __init__(self, name: str, role: Role, tile: Tuple[int, int]) -> None:
        self.name = name
        self.role = role
        self.tile = tile
        self.px = tile[0] * TILE
        self.py = tile[1] * TILE
        self.facing = random.choice(["down", "left", "right"])
        self.path: List[Tuple[int, int]] = []
        self.state = IDLE
        self.desk: Optional[Desk] = None
        self.task: Optional[Task] = None
        self.job: Optional["Job"] = None
        self.progress = 0.0
        self.bubble = ""
        self.bubble_until = 0.0
        self.steps = 0
        self.idle_timer = random.randint(30, 240)
        self.celebrate_timer = 0
        self.result_pending: Optional[Dict[str, Any]] = None

    @property
    def moving(self) -> bool:
        return bool(self.path) or (self.px, self.py) != (self.tile[0] * TILE, self.tile[1] * TILE)

    def say(self, text: str, seconds: float = 2.5) -> None:
        self.bubble = text[:6]
        self.bubble_until = time.time() + seconds

    def walk_to(self, office: Office, goal: Tuple[int, int]) -> bool:
        """Plans a path; an agent in the middle of a step finishes that step first."""
        start, prefix = self.tile, []
        if self.path and (self.px, self.py) != (self.tile[0] * TILE, self.tile[1] * TILE):
            prefix = [self.path[0]]
            start = self.path[0]
        path = office.find_path(start, goal, self)
        if start != goal and not path:
            return False
        self.path = prefix + path
        return True

    def update_movement(self) -> None:
        """Moves one pixel per frame along the path; a tile takes 16 frames like a Game Boy step."""
        target = self.tile
        if self.path:
            target = self.path[0]
        tx, ty = target[0] * TILE, target[1] * TILE
        if (self.px, self.py) == (tx, ty):
            if self.path:
                self.tile = self.path.pop(0)
                if self.path:
                    self._face_towards(self.path[0])
            return
        dx = (tx > self.px) - (tx < self.px)
        dy = (ty > self.py) - (ty < self.py)
        self.px += dx
        self.py += dy
        self.steps += 1
        if dx:
            self.facing = "right" if dx > 0 else "left"
        elif dy:
            self.facing = "down" if dy > 0 else "up"
        if (self.px, self.py) == (tx, ty) and self.path:
            self.tile = self.path.pop(0)
            if self.path:
                self._face_towards(self.path[0])

    def _face_towards(self, nxt: Tuple[int, int]) -> None:
        dx, dy = nxt[0] - self.tile[0], nxt[1] - self.tile[1]
        self.facing = "right" if dx > 0 else "left" if dx < 0 else "down" if dy > 0 else "up"

    @property
    def anim_frame(self) -> int:
        return (self.steps // 8) % 4 if self.moving else 0


# ---------------------------------------------------------------------------------------------
# Backends: real Claude Code sessions, or a simulation
# ---------------------------------------------------------------------------------------------

TOOL_WORDS = {
    "Read": "READ", "Edit": "EDIT", "Write": "WRITE", "MultiEdit": "EDIT", "NotebookEdit": "EDIT",
    "Bash": "BASH", "Grep": "GREP", "Glob": "FIND", "LS": "FIND", "WebSearch": "WEB", "WebFetch": "WEB",
    "Task": "AGENT", "Agent": "AGENT", "TodoWrite": "PLAN", "AskUserQuestion": "ASK",
}


@dataclass
class Job:
    task_id: int
    cancel: threading.Event = field(default_factory=threading.Event)
    process: Optional[subprocess.Popen] = None
    thread: Optional[threading.Thread] = None


def find_claude(explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit if os.path.exists(explicit) else shutil.which(explicit)
    for name in ("claude", "claude.cmd", "claude.exe"):
        found = shutil.which(name)
        if found:
            return found
    folders = [Path.home() / ".local" / "bin", Path.home() / ".claude" / "local"]
    if os.environ.get("APPDATA"):
        folders.append(Path(os.environ["APPDATA"]) / "npm")
    for folder in folders:
        for name in ("claude", "claude.exe", "claude.cmd"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return None


def command_for(executable: str, args: List[str]) -> List[str]:
    if os.name == "nt" and executable.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", executable] + args
    return [executable] + args


class ClaudeBackend:
    """Runs each task as `claude -p` in the project folder and streams its JSON events."""

    name = "CLAUDE"

    def __init__(self, executable: str, project_dir: str, events: "queue.Queue[Dict[str, Any]]",
                 permission_mode: str = "acceptEdits", model: str = "", max_turns: int = 0,
                 bypass_permissions: bool = False, log: Callable[[str], None] = print,
                 allowed_tools: str = "", max_budget_usd: float = 0.0) -> None:
        self.executable = executable
        self.project_dir = project_dir
        self.events = events
        self.permission_mode = permission_mode
        self.model = model
        self.max_turns = max_turns
        self.bypass = bypass_permissions
        self.log = log
        self.allowed_tools = allowed_tools
        self.max_budget_usd = max_budget_usd

    def describe(self) -> str:
        return "BYPASS" if self.bypass else self.permission_mode.upper()

    def start(self, task: Task, prompt: str, resume_id: str) -> Job:
        job = Job(task.id)
        args = ["-p", prompt, "--output-format", "stream-json", "--verbose",
                "--append-system-prompt", task.role_obj.prompt]
        if self.bypass:
            args.append("--dangerously-skip-permissions")
        else:
            args += ["--permission-mode", self.permission_mode]
        if self.model:
            args += ["--model", self.model]
        if self.max_turns:
            args += ["--max-turns", str(self.max_turns)]
        if self.allowed_tools:
            args += ["--allowedTools", self.allowed_tools]
        if self.max_budget_usd:
            args += ["--max-budget-usd", f"{self.max_budget_usd:.2f}"]
        if resume_id:
            args += ["--resume", resume_id]
        job.thread = threading.Thread(target=self._run, args=(job, args), daemon=True)
        job.thread.start()
        return job

    def cancel(self, job: Job) -> None:
        job.cancel.set()
        process = job.process
        if process and process.poll() is None:
            if os.name == "nt":
                # claude.cmd starts node as a child; kill the whole tree or it keeps running
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                process.kill()
            except OSError:
                pass

    def _run(self, job: Job, args: List[str]) -> None:
        env = dict(os.environ)
        for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):  # allow launching from inside Claude Code
            env.pop(key, None)
        popen_kwargs: Dict[str, Any] = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            job.process = subprocess.Popen(
                command_for(self.executable, args), cwd=self.project_dir, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **popen_kwargs)
        except OSError as exc:
            self.events.put({"type": "done", "task_id": job.task_id, "ok": False,
                             "text": f"COULD NOT START CLAUDE: {exc}"})
            return
        process = job.process
        stderr_lines: List[str] = []
        stderr_thread = threading.Thread(
            target=lambda: stderr_lines.extend(line.rstrip() for line in process.stderr), daemon=True)
        stderr_thread.start()
        result: Optional[Dict[str, Any]] = None
        last_text = ""
        session_id = ""
        denied: List[str] = []
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = str(event.get("session_id") or session_id)
            kind = event.get("type")
            if kind == "permission_denied" or kind == "system" and event.get("subtype") == "permission_denied":
                tool = str(event.get("tool_name") or event.get("tool") or "TOOL")
                denied.append(tool)
                self.events.put({"type": "status", "task_id": job.task_id, "word": "DENY", "detail": tool[:40]})
            elif kind == "assistant":
                message = event.get("message") or {}
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text" and block.get("text"):
                        last_text = str(block["text"])
                    elif block.get("type") == "tool_use":
                        tool = str(block.get("name", "TOOL"))
                        inputs = block.get("input") or {}
                        target = ""
                        if isinstance(inputs, dict):
                            for key in ("file_path", "path", "pattern", "command", "url", "description"):
                                if inputs.get(key):
                                    target = str(inputs[key])
                                    break
                        self.events.put({"type": "status", "task_id": job.task_id,
                                         "word": TOOL_WORDS.get(tool, tool[:5].upper()),
                                         "detail": os.path.basename(target)[:40] if target else ""})
            elif kind == "result":
                result = event
        process.wait()
        stderr_thread.join(timeout=2)
        if job.cancel.is_set():
            self.events.put({"type": "done", "task_id": job.task_id, "ok": False, "text": "CANCELLED",
                             "session_id": session_id, "cancelled": True})
            return
        if result is not None:
            text = str(result.get("result") or last_text or "").strip()
            ok = not result.get("is_error") and result.get("subtype", "success") == "success"
            if not ok and not text:
                text = f"CLAUDE STOPPED: {result.get('subtype', 'error')}"
            if denied:
                text += (f"\n[{len(denied)} TOOL CALL(S) WERE DENIED ({', '.join(sorted(set(denied)))}). "
                         "ALLOW THEM WITH --PERMISSION-MODE, --ALLOWED-TOOLS OR --BYPASS-PERMISSIONS.]")
            self.events.put({"type": "done", "task_id": job.task_id, "ok": ok, "text": text or "(NO ANSWER)",
                             "session_id": str(result.get("session_id") or session_id),
                             "cost": float(result.get("total_cost_usd") or 0.0)})
        else:
            tail = " ".join(stderr_lines[-6:]).strip() or "NO RESULT FROM CLAUDE"
            self.events.put({"type": "done", "task_id": job.task_id, "ok": False,
                             "text": f"CLAUDE EXITED ({process.returncode}): {tail}"[:600],
                             "session_id": session_id})
        self.log(f"task {job.task_id}: claude exited {process.returncode}")


SIM_REPLIES = {
    "DEVELOPER": ["IMPLEMENTED: {task}\nCHANGED 2 FILES, BUILD OK.\nREADY FOR TESTING.",
                  "DONE: {task}\nADDED THE MISSING CHECK AND A SMALL REFACTOR.\nNOTHING ELSE TOUCHED."],
    "TESTER": ["TEST REPORT: {task}\n12 TESTS RUN, 11 PASSED, 1 FAILED.\nFAILURE: EDGE CASE WITH EMPTY INPUT.",
               "TESTED: {task}\nALL 8 CHECKS PASSED. NO REGRESSIONS FOUND."],
    "DESIGNER": ["DESIGN DONE: {task}\nNEW LAYOUT MOCKUP SAVED, ICONS UPDATED,\nCOPY SHORTENED TO FIT.",
                 "DELIVERED: {task}\nPALETTE AND SPACING ALIGNED WITH THE STYLE GUIDE."],
    "MANAGER": ["STATUS: {task}\nPLAN: 1) INVESTIGATE 2) FIX 3) TEST.\nSUGGESTED NEXT TASK FOR DEV: SEE ABOVE.",
                "REVIEW: {task}\nSCOPE IS CLEAR. RISK: LOW. ESTIMATE: 1 DAY."],
}


class SimBackend:
    """Stands in for Claude: pretends to work for a few seconds and answers with canned text."""

    name = "SIM"

    def __init__(self, events: "queue.Queue[Dict[str, Any]]", seconds: Tuple[float, float] = (6.0, 18.0)) -> None:
        self.events = events
        self.seconds = seconds

    def describe(self) -> str:
        return "NO CLAUDE FOUND" if not getattr(self, "forced", False) else "FORCED"

    def start(self, task: Task, prompt: str, resume_id: str) -> Job:
        job = Job(task.id)
        job.thread = threading.Thread(target=self._run, args=(job, task, prompt, bool(resume_id)), daemon=True)
        job.thread.start()
        return job

    def cancel(self, job: Job) -> None:
        job.cancel.set()

    def _run(self, job: Job, task: Task, prompt: str, is_reply: bool) -> None:
        total = random.uniform(*self.seconds)
        words = ["READ", "GREP", "EDIT", "BASH", "READ", "PLAN", "EDIT", "BASH"]
        deadline = time.time() + total
        index = 0
        while time.time() < deadline:
            if job.cancel.wait(random.uniform(0.8, 2.2)):
                self.events.put({"type": "done", "task_id": job.task_id, "ok": False, "text": "CANCELLED",
                                 "cancelled": True})
                return
            self.events.put({"type": "status", "task_id": job.task_id, "word": words[index % len(words)],
                             "detail": random.choice(["MAIN.PY", "ITEM_PROTO.TXT", "CHAR.CPP", "QUEST.LUA", "UI.PY"])})
            index += 1
        if is_reply:
            text = f"(SIMULATION) NOTED: {prompt[:60]}\nUPDATED ACCORDINGLY. ANYTHING ELSE?"
        else:
            text = "(SIMULATION) " + random.choice(SIM_REPLIES[task.role]).format(task=task.name[:50])
        self.events.put({"type": "done", "task_id": job.task_id, "ok": random.random() > 0.08, "text": text,
                         "session_id": task.session_id or f"sim-{task.id}-{random.randint(1000, 9999)}",
                         "cost": 0.0})


# ---------------------------------------------------------------------------------------------
# Text input box
# ---------------------------------------------------------------------------------------------

class InputBox:
    def __init__(self, max_length: int = 400) -> None:
        self.text = ""
        self.max_length = max_length

    def handle(self, event: pygame.event.Event) -> Optional[str]:
        """Returns the submitted text on Enter, else None."""
        if event.type == pygame.TEXTINPUT:
            self.text = (self.text + event.text)[: self.max_length]
        elif event.type == pygame.KEYDOWN:
            mods = pygame.key.get_mods()
            ctrl = mods & (pygame.KMOD_CTRL | pygame.KMOD_META)
            if event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
                text, self.text = self.text.strip(), ""
                return text
            if event.key == pygame.K_BACKSPACE:
                if ctrl:
                    self.text = self.text.rstrip()
                    self.text = self.text[: self.text.rfind(" ") + 1] if " " in self.text else ""
                else:
                    self.text = self.text[:-1]
            elif event.key == pygame.K_v and ctrl:
                self.text = (self.text + clipboard_get())[: self.max_length]
            elif event.key == pygame.K_u and ctrl:
                self.text = ""
        return None

    def visible(self, columns: int, blink_on: bool) -> str:
        cursor = "_" if blink_on else " "
        text = self.text[-(columns - 1):] if len(self.text) > columns - 1 else self.text
        return text + cursor


def clipboard_get() -> str:
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        text = pygame.scrap.get_text() if hasattr(pygame.scrap, "get_text") else ""
        return re.sub(r"\s+", " ", text or "").strip()
    except Exception:
        return ""


def clipboard_put(text: str) -> bool:
    try:
        if not pygame.scrap.get_init():
            pygame.scrap.init()
        if hasattr(pygame.scrap, "put_text"):
            pygame.scrap.put_text(text)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------------------------
# The game
# ---------------------------------------------------------------------------------------------

HELP_LINES = [
    "TYPE A TASK AND PRESS ENTER. TAB PICKS THE ROLE (DEV/TST/DSN/MGR).",
    "UP/DOWN SELECT A TASK. READ THE ANSWER HERE, TYPE A REPLY, ENTER SENDS IT.",
    "ESC BACK TO NEW TASK.  PGUP/PGDN OR MOUSE WHEEL SCROLL THE ANSWER.",
    "DEL CANCELS A RUNNING TASK OR REMOVES A FINISHED ONE. CTRL+C COPIES THE ANSWER.",
    "CTRL+V PASTES.  CTRL+M MUTE.  CTRL+G LCD GRID.  CTRL+Q QUIT.  F1 THIS HELP.",
    "DROP A PROJECT FOLDER ON THE WINDOW TO SWITCH PROJECT.",
    "AGENTS WALK TO A DESK AND RUN CLAUDE CODE IN YOUR PROJECT. WATCH THE BUBBLES.",
]


class SimulationEngine:
    def __init__(self, project_dir: str, backend: Any, sound: Chiptune, agent_count: int = 24,
                 max_jobs: int = 4, log: Callable[[str], None] = print, state_path: Optional[Path] = None) -> None:
        self.project_dir = project_dir
        self.backend = backend
        self.sound = sound
        self.max_jobs = max_jobs
        self.log = log
        self.state_path = state_path
        self.font = PixelFont()
        self.art = Art()
        self.office = Office()
        self.events: "queue.Queue[Dict[str, Any]]" = backend.events
        self.canvas = pygame.Surface((CANVAS_W, CANVAS_H))
        self.tasks: List[Task] = []
        self.next_task_id = 1
        self.agents: List[Agent] = []
        self.input = InputBox()
        self.role_index = 0
        self.selected: Optional[int] = None  # task id
        self.scroll = 0
        self.panel_scroll = 0
        self.show_help = False
        self.frame = 0
        self.total_cost = 0.0
        self.event_log: deque = deque(maxlen=200)
        self.wrapped_cache: Dict[Tuple[int, int], List[str]] = {}
        self.dirty = False
        self.last_save = 0.0
        self.agent_status_time: Dict[str, float] = {}
        self._spawn_agents(max(4, agent_count))
        self._load_state()
        self.note(f"WELCOME TO {APP_NAME.upper()}. PRESS F1 FOR HELP.")

    # -- setup --------------------------------------------------------------------------------

    def _spawn_agents(self, count: int) -> None:
        spots = list(self.office.lounge)
        random.shuffle(spots)
        names = list(AGENT_NAMES)
        for i in range(count):
            name = names[i] if i < len(names) else f"A{i + 1}"
            role = ROLES[i % len(ROLES)]
            tile = spots[i % len(spots)]
            self.agents.append(Agent(name, role, tile))

    # -- persistence --------------------------------------------------------------------------

    def _load_state(self) -> None:
        if not self.state_path or not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self.tasks = [Task.from_dict(item) for item in data.get("tasks", [])][-200:]
            self.next_task_id = max([t.id for t in self.tasks] + [0]) + 1
            self.total_cost = float(data.get("total_cost", 0.0))
            self.role_index = int(data.get("role_index", 0)) % len(ROLES)
            self.note(f"LOADED {len(self.tasks)} TASKS FROM LAST SESSION.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self.log(f"could not load state: {exc}")

    def save_state(self, force: bool = False) -> None:
        if not self.state_path or (not self.dirty and not force):
            return
        if not force and time.time() - self.last_save < 2.0:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"app": APP_NAME, "version": VERSION, "project": self.project_dir,
                       "total_cost": self.total_cost, "role_index": self.role_index,
                       "tasks": [t.to_dict() for t in self.tasks]}
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_path)
            self.dirty = False
            self.last_save = time.time()
        except OSError as exc:
            self.log(f"could not save state: {exc}")

    # -- helpers ------------------------------------------------------------------------------

    def note(self, text: str) -> None:
        stamp = time.strftime("%H:%M")
        self.event_log.append(f"{stamp} {text}")
        self.log(text)

    def task_by_id(self, task_id: Optional[int]) -> Optional[Task]:
        for task in self.tasks:
            if task.id == task_id:
                return task
        return None

    def agent_for_task(self, task: Task) -> Optional[Agent]:
        """The agent working on the task; a job holder wins over one still celebrating it."""
        owners = [agent for agent in self.agents if agent.task is task]
        for agent in owners:
            if agent.job is not None:
                return agent
        return owners[0] if owners else None

    def agent_for_job(self, task_id: int) -> Optional[Agent]:
        for agent in self.agents:
            if agent.job is not None and agent.job.task_id == task_id:
                return agent
        return None

    @property
    def running_jobs(self) -> int:
        return sum(1 for agent in self.agents if agent.job is not None)

    @property
    def current_role(self) -> Role:
        return ROLES[self.role_index]

    # -- task flow ----------------------------------------------------------------------------

    def add_task(self, name: str, role: Role) -> Task:
        task = Task(self.next_task_id, name[:120], role.name)
        task.pending_prompt = name
        task.transcript.append(("YOU", name))
        self.next_task_id += 1
        self.tasks.append(task)
        self.note(f"#{task.id} ADDED ({role.name})")
        self.sound.play("spawn")
        self.dirty = True
        return task

    def reply(self, task: Task, text: str) -> None:
        task.pending_prompt = text
        task.transcript.append(("YOU", text))
        task.status = STATUS_NEW
        task.fail_reason = ""
        task.unread = False
        self.note(f"REPLY QUEUED FOR #{task.id}")
        self.sound.play("reply")
        self.wrapped_cache.pop((task.id, DIALOG_COLS), None)
        self.scroll = 0
        self.dirty = True

    def dispatch(self) -> None:
        """Gives backlog tasks to idle agents of the matching role when a desk is free."""
        for task in self.tasks:
            if task.status != STATUS_NEW:
                continue
            if self.running_jobs >= self.max_jobs:
                return
            free_desks = [desk for desk in self.office.desks if desk.free]
            if not free_desks:
                return
            candidates = [a for a in self.agents if a.state == IDLE and a.role.name == task.role]
            if not candidates:
                continue
            candidates.sort(key=lambda a: (a.name != task.agent_name, a.moving))
            agent = candidates[0]
            desk = min(free_desks, key=lambda d: abs(d.seat[0] - agent.tile[0]) + abs(d.seat[1] - agent.tile[1]))
            desk.agent = agent
            agent.desk = desk
            agent.task = task
            agent.state = TO_DESK
            agent.progress = 0.0
            agent.say("!", 1.5)
            if not agent.walk_to(self.office, desk.seat):
                agent.state = IDLE
                agent.desk = None
                agent.task = None
                desk.agent = None
                continue
            task.status = STATUS_GO
            task.agent_name = agent.name
            task.started = time.time()
            task.finished = 0.0
            prompt, task.pending_prompt = task.pending_prompt, ""
            agent.job = self.backend.start(task, prompt, task.session_id)
            self.note(f"{agent.name} TOOK #{task.id} -> DESK {desk.index}")
            self.sound.play("assign")
            self.dirty = True

    def cancel_task(self, task: Task) -> None:
        agent = self.agent_for_task(task)
        if agent and agent.job:
            self.backend.cancel(agent.job)
            self.note(f"CANCELLING #{task.id}...")
            self.sound.play("cancel")
        elif task.status == STATUS_NEW:
            self.tasks.remove(task)
            self.note(f"#{task.id} REMOVED")
            self.sound.play("cancel")
            self.dirty = True

    def remove_task(self, task: Task) -> None:
        if task.active:
            self.cancel_task(task)
            return
        if task.status == STATUS_NEW:
            self.cancel_task(task)
            return
        self.tasks.remove(task)
        self.note(f"#{task.id} REMOVED FROM THE LIST")
        self.sound.play("select")
        self.dirty = True

    def _apply_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            task = self.task_by_id(event.get("task_id"))
            agent = self.agent_for_job(event.get("task_id"))
            if task is None or agent is None or agent.task is not task:
                continue
            if event["type"] == "status":
                task.tool_calls += 1
                agent.say(event["word"], 4.0)
                agent.progress = min(0.95, agent.progress + 0.02)
                now = time.time()
                if now - self.agent_status_time.get(agent.name, 0.0) > 1.5:
                    self.agent_status_time[agent.name] = now
                    detail = f" {event['detail']}" if event.get("detail") else ""
                    self.note(f"{agent.name}: {event['word']}{detail}")
            elif event["type"] == "done":
                agent.result_pending = event
                if agent.state == WORKING or agent.state == TO_DESK and not agent.moving:
                    self._finish(agent)

    def _finish(self, agent: Agent) -> None:
        event = agent.result_pending
        task = agent.task
        if event is None or task is None:
            return
        agent.result_pending = None
        agent.job = None
        task.finished = time.time()
        task.session_id = str(event.get("session_id") or task.session_id)
        cost = float(event.get("cost") or 0.0)
        task.cost += cost
        self.total_cost += cost
        text = str(event.get("text") or "")
        if event.get("ok"):
            task.status = STATUS_DONE
            task.transcript.append((agent.name, text))
            task.unread = True
            agent.progress = 1.0
            agent.say("DONE!", 3.0)
            self.sound.play("complete")
            money = f" (${cost:.2f})" if cost else ""
            self.note(f"#{task.id} DONE BY {agent.name}{money} - SELECT IT TO READ")
        else:
            task.status = STATUS_FAIL
            task.fail_reason = text[:80]
            task.transcript.append((agent.name, text))
            task.unread = not event.get("cancelled")
            agent.say("X_X", 3.0)
            self.sound.play("error")
            self.note(f"#{task.id} {'CANCELLED' if event.get('cancelled') else 'FAILED'}: {text[:60]}")
        self.wrapped_cache.pop((task.id, DIALOG_COLS), None)
        agent.state = CELEBRATE
        agent.celebrate_timer = 75
        self.dirty = True

    # -- per-frame update ---------------------------------------------------------------------

    def update(self) -> None:
        self.frame += 1
        self._apply_events()
        self.dispatch()
        now = time.time()
        for agent in self.agents:
            if agent.state == IDLE:
                agent.update_movement()
                if not agent.moving:
                    agent.idle_timer -= 1
                    if agent.idle_timer <= 0:
                        agent.idle_timer = random.randint(90, 420)
                        if random.random() < 0.6:
                            spot = random.choice(self.office.lounge)
                            agent.walk_to(self.office, spot)
                        else:
                            agent.facing = random.choice(["down", "left", "right", "up"])
                            if random.random() < 0.15:
                                agent.say(random.choice(["ZZZ", "?", "...", "HI!"]), 2.0)
            elif agent.state == TO_DESK:
                agent.update_movement()
                if not agent.moving:
                    agent.state = WORKING
                    agent.facing = "up"
                    if agent.task and agent.task.status == STATUS_GO:
                        agent.task.status = STATUS_RUN
                    if agent.result_pending:
                        self._finish(agent)
            elif agent.state == WORKING:
                if agent.task and agent.task.status == STATUS_GO:
                    agent.task.status = STATUS_RUN
                if agent.task:
                    elapsed = max(0.0, now - agent.task.started)
                    estimate = 1.0 - 0.5 ** (elapsed / 40.0)
                    agent.progress = max(agent.progress, min(0.95, estimate))
                if not agent.bubble or agent.bubble_until < now:
                    agent.say("...", 60.0)
            elif agent.state == CELEBRATE:
                agent.celebrate_timer -= 1
                if agent.celebrate_timer <= 0:
                    if agent.desk:
                        agent.desk.agent = None
                    agent.desk = None
                    agent.task = None
                    agent.progress = 0.0
                    agent.bubble = ""
                    agent.state = RETURNING
                    agent.walk_to(self.office, random.choice(self.office.lounge))
            elif agent.state == RETURNING:
                agent.update_movement()
                if not agent.moving:
                    agent.state = IDLE
                    agent.idle_timer = random.randint(60, 300)
        self.save_state()

    # -- input --------------------------------------------------------------------------------

    def handle_event(self, event: pygame.event.Event) -> bool:
        """Returns False when the game should quit."""
        if event.type == pygame.QUIT:
            return False
        if event.type == pygame.DROPFILE:
            self._drop(event.file)
            return True
        if event.type == pygame.MOUSEWHEEL:
            self.scroll = max(0, self.scroll - event.y * 2)
            return True
        if event.type == pygame.KEYDOWN:
            mods = pygame.key.get_mods()
            ctrl = mods & (pygame.KMOD_CTRL | pygame.KMOD_META)
            if event.key == pygame.K_q and ctrl:
                return False
            if event.key == pygame.K_F1:
                self.show_help = not self.show_help
                self.sound.play("select")
                return True
            if event.key == pygame.K_ESCAPE:
                self.show_help = False
                self.select(None)
                return True
            if event.key == pygame.K_TAB:
                self.role_index = (self.role_index + (-1 if mods & pygame.KMOD_SHIFT else 1)) % len(ROLES)
                self.sound.play("select")
                self.dirty = True
                return True
            if event.key in (pygame.K_UP, pygame.K_DOWN):
                self._move_selection(-1 if event.key == pygame.K_UP else 1)
                return True
            if event.key == pygame.K_PAGEUP:
                self.scroll += 8
                return True
            if event.key == pygame.K_PAGEDOWN:
                self.scroll = max(0, self.scroll - 8)
                return True
            if event.key == pygame.K_HOME and ctrl:
                self.scroll = 10 ** 6
                return True
            if event.key == pygame.K_END and ctrl:
                self.scroll = 0
                return True
            if event.key == pygame.K_DELETE:
                task = self.task_by_id(self.selected)
                if task:
                    self.remove_task(task)
                    if task not in self.tasks:
                        self.select(None)
                return True
            if event.key == pygame.K_m and ctrl:
                self.sound.muted = not self.sound.muted
                self.note("SOUND OFF" if self.sound.muted else "SOUND ON")
                return True
            if event.key == pygame.K_c and ctrl:
                task = self.task_by_id(self.selected)
                if task and task.transcript:
                    ok = clipboard_put(task.transcript[-1][1])
                    self.note("ANSWER COPIED TO CLIPBOARD" if ok else "CLIPBOARD NOT AVAILABLE")
                return True
        if event.type in (pygame.KEYDOWN, pygame.TEXTINPUT):
            submitted = self.input.handle(event)
            if submitted:
                self._submit(submitted)
        if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and hasattr(event, "canvas_pos"):
            self._click(event.canvas_pos)
        return True

    def _drop(self, path: str) -> None:
        if os.path.isdir(path):
            if self.running_jobs:
                self.note("FINISH RUNNING TASKS BEFORE SWITCHING PROJECT")
                return
            self.project_dir = path
            if hasattr(self.backend, "project_dir"):
                self.backend.project_dir = path
            self.note(f"PROJECT: {os.path.basename(path.rstrip(os.sep)) or path}")
            self.sound.play("assign")
        else:
            self.input.text = (self.input.text + " " + path)[: self.input.max_length]

    def _submit(self, text: str) -> None:
        task = self.task_by_id(self.selected)
        if task is None:
            self.add_task(text, self.current_role)
        elif task.active:
            self.note(f"#{task.id} IS STILL RUNNING - WAIT FOR THE ANSWER")
            self.input.text = text
            self.sound.play("error")
        elif task.status == STATUS_NEW:
            task.pending_prompt = (task.pending_prompt + "\n" + text).strip()
            task.transcript.append(("YOU", text))
            self.note(f"ADDED TO #{task.id} BEFORE IT STARTS")
            self.wrapped_cache.pop((task.id, DIALOG_COLS), None)
        else:
            self.reply(task, text)

    def select(self, task_id: Optional[int]) -> None:
        self.selected = task_id
        self.scroll = 0
        task = self.task_by_id(task_id)
        if task and task.unread:
            task.unread = False
            self.dirty = True
        self.sound.play("select")

    def _move_selection(self, delta: int) -> None:
        ids: List[Optional[int]] = [None] + [t.id for t in self.tasks]
        index = ids.index(self.selected) if self.selected in ids else 0
        self.select(ids[max(0, min(len(ids) - 1, index + delta))])

    def _click(self, pos: Tuple[int, int]) -> None:
        x, y = pos
        if x >= PANEL_X and 16 <= y < 16 + 20 * LINE_H:
            row = (y - 16) // LINE_H + self.panel_scroll
            if 0 <= row < len(self.tasks):
                self.select(self.tasks[row].id)
        elif x >= PANEL_X and 188 <= y < 196:
            self.role_index = (self.role_index + 1) % len(ROLES)
            self.sound.play("select")
        elif x < PANEL_X:
            tx, ty = x // TILE, y // TILE
            for agent in self.agents:
                if agent.tile == (tx, ty) and agent.task:
                    self.select(agent.task.id)
                    break

    # -- drawing ------------------------------------------------------------------------------

    def draw(self) -> pygame.Surface:
        canvas = self.canvas
        canvas.fill(BACKGROUND)
        self._draw_office(canvas)
        self._draw_panel(canvas)
        self._draw_dialog(canvas)
        return canvas

    def _draw_office(self, canvas: pygame.Surface) -> None:
        tiles = self.art.tiles
        for y, row in enumerate(OFFICE_MAP):
            for x, char in enumerate(row):
                base = tiles["L"] if char in "LPSsCKR" and y >= 10 else tiles["."]
                if char in ("#", "W", "~"):
                    canvas.blit(tiles[char], (x * TILE, y * TILE))
                    continue
                canvas.blit(base, (x * TILE, y * TILE))
                if char in tiles and char not in (".", "L"):
                    canvas.blit(tiles[char], (x * TILE, y * TILE))
        # blinking server LED (palette colours only)
        led_on = (self.frame // 30) % 2 == 0 or self.running_jobs > 0 and (self.frame // 8) % 2 == 0
        canvas.fill(LIGHT if led_on else DARKEST, (18 * TILE + 5, 10 * TILE + 4, 2, 1))
        for agent in sorted(self.agents, key=lambda a: (a.py, a.px)):
            frame = self.art.sprites[agent.role.name][agent.facing][agent.anim_frame]
            canvas.blit(frame, (agent.px, agent.py))
        for desk in self.office.desks:
            agent = desk.agent
            if agent and agent.state in (WORKING, CELEBRATE):
                x, y = desk.tile[0] * TILE + 1, desk.tile[1] * TILE - 5
                canvas.fill(DARKEST, (x, y, 30, 5))
                canvas.fill(BACKGROUND, (x + 1, y + 1, 28, 3))
                fill = int(28 * max(0.0, min(1.0, agent.progress)))
                if agent.state == WORKING and agent.progress < 0.95:
                    fill = max(fill, (self.frame // 6) % 6 if fill < 3 else fill)
                canvas.fill(DARKEST, (x + 1, y + 1, fill, 3))
        now = time.time()
        for agent in self.agents:
            if agent.bubble and agent.bubble_until > now:
                text = agent.bubble
                width = PixelFont.width(text) + 3
                bx = max(0, min(MAP_PX_W - width, agent.px + TILE // 2 - width // 2))
                by = agent.py - 9
                if by < 0:
                    by = agent.py + TILE
                canvas.fill(DARKEST, (bx, by, width, 9))
                canvas.fill(BACKGROUND, (bx + 1, by + 1, width - 2, 7))
                self.font.draw(canvas, text, bx + 2, by + 1, DARKEST)

    def _draw_panel(self, canvas: pygame.Surface) -> None:
        font = self.font
        canvas.fill(BACKGROUND, (PANEL_X, 0, PANEL_W, MAP_PX_H))
        canvas.fill(DARKEST, (PANEL_X, 0, 2, MAP_PX_H))
        canvas.fill(DARKEST, (PANEL_X, 0, PANEL_W, 12))
        new = sum(1 for t in self.tasks if t.status == STATUS_NEW)
        running = sum(1 for t in self.tasks if t.active)
        font.draw(canvas, f"BACKLOG {new} NEW {running} RUN", PANEL_X + 4, 2, BACKGROUND)
        rows_visible = 20
        ids: List[Optional[int]] = [None] + [t.id for t in self.tasks]
        selected_index = ids.index(self.selected) if self.selected in ids else 0
        if selected_index - 1 < self.panel_scroll:
            self.panel_scroll = max(0, selected_index - 1)
        if selected_index - 1 >= self.panel_scroll + rows_visible:
            self.panel_scroll = selected_index - rows_visible
        self.panel_scroll = max(0, min(self.panel_scroll, max(0, len(self.tasks) - rows_visible)))
        y = 16
        for task in self.tasks[self.panel_scroll: self.panel_scroll + rows_visible]:
            status = task.status + ("*" if task.unread else "")
            if task.status == STATUS_NEW and task.session_id:
                status = "RPLY"
            name = task.name[:14]
            line = f"{task.role_obj.letter} {name:<14} {status:>5}"[:PANEL_COLS]
            if task.id == self.selected:
                canvas.fill(DARKEST, (PANEL_X + 2, y - 1, PANEL_W - 2, LINE_H))
                font.draw(canvas, line, PANEL_X + 4, y, BACKGROUND)
            else:
                font.draw(canvas, line, PANEL_X + 4, y, DARKEST if task.status != STATUS_FAIL else SHADOW)
            y += LINE_H
        if not self.tasks:
            font.draw(canvas, "NO TASKS YET.", PANEL_X + 4, 16, SHADOW)
            font.draw(canvas, "TYPE ONE BELOW.", PANEL_X + 4, 24, SHADOW)
        canvas.fill(DARKEST, (PANEL_X + 2, 184, PANEL_W - 2, 1))
        x = font.draw(canvas, "ROLE ", PANEL_X + 4, 188, DARKEST)
        for index, role in enumerate(ROLES):
            if index == self.role_index and self.selected is None:
                canvas.fill(DARKEST, (x - 1, 187, PixelFont.width(role.short) + 2, LINE_H + 1))
                x = font.draw(canvas, role.short, x, 188, BACKGROUND)
            else:
                x = font.draw(canvas, role.short, x, 188, SHADOW if self.selected is not None else DARKEST)
            x += CHAR_W
        mode = f"{self.backend.name} {self.backend.describe()}"[:19]
        font.draw(canvas, f"MODE {mode}", PANEL_X + 4, 198, DARKEST)
        desks_used = sum(1 for d in self.office.desks if not d.free)
        font.draw(canvas, f"AGENTS {len(self.agents)} DESKS {desks_used}/{len(self.office.desks)}", PANEL_X + 4, 206, DARKEST)
        jobs = f"JOBS {self.running_jobs}/{self.max_jobs}"
        cost = f"${self.total_cost:.2f}" if self.total_cost else ""
        font.draw(canvas, f"{jobs} {cost}".strip(), PANEL_X + 4, 214, DARKEST)

    def _transcript_lines(self, task: Task) -> List[str]:
        key = (task.id, DIALOG_COLS)
        lines = self.wrapped_cache.get(key)
        if lines is None:
            lines = []
            for speaker, text in task.transcript:
                lines.extend(wrap_text(f"{speaker}: {text}", DIALOG_COLS))
                lines.append("")
            if task.status == STATUS_FAIL and task.fail_reason:
                lines.append(f"[FAILED: {task.fail_reason}]")
            self.wrapped_cache[key] = lines
        return lines

    def _draw_dialog(self, canvas: pygame.Surface) -> None:
        font = self.font
        canvas.fill(BACKGROUND, (0, DIALOG_Y, CANVAS_W, DIALOG_H))
        pygame.draw.rect(canvas, DARKEST, (1, DIALOG_Y + 1, CANVAS_W - 2, DIALOG_H - 2), 2)
        pygame.draw.rect(canvas, SHADOW, (4, DIALOG_Y + 4, CANVAS_W - 8, DIALOG_H - 8), 1)
        text_x, first_y = 8, DIALOG_Y + 8
        body_rows = 8                      # header + 7 lines, then the input line
        input_y = first_y + LINE_H * 9
        canvas.fill(SHADOW, (6, input_y - 4, CANVAS_W - 12, 1))
        task = self.task_by_id(self.selected)
        blink = (self.frame // 30) % 2 == 0
        if self.show_help:
            font.draw(canvas, f"{APP_NAME.upper()} V{VERSION} - HELP", text_x, first_y, DARKEST)
            for i, line in enumerate(HELP_LINES):
                font.draw(canvas, line, text_x, first_y + LINE_H * (i + 1), DARKEST)
            font.draw(canvas, "F1 OR ESC TO CLOSE HELP", text_x, input_y, SHADOW)
            return
        if task is None:
            project = os.path.basename(self.project_dir.rstrip(os.sep)) or self.project_dir
            header = f"PROJECT: {project}   MODE: {self.backend.name}"
            if self.backend.name == "SIM":
                header += "  (INSTALL CLAUDE CODE FOR REAL AGENTS)"
            font.draw(canvas, header[:DIALOG_COLS], text_x, first_y, DARKEST)
            recent = list(self.event_log)[-(body_rows - 1):]
            for i, line in enumerate(recent):
                font.draw(canvas, line[:DIALOG_COLS], text_x, first_y + LINE_H * (i + 1), DARKEST if i == len(recent) - 1 else SHADOW)
            prompt = f"NEW {self.current_role.short}> "
        else:
            elapsed = task.elapsed()
            clock = f"{int(elapsed // 60)}M{int(elapsed % 60):02d}S" if elapsed else ""
            cost = f"${task.cost:.2f}" if task.cost else ""
            who = f"AGENT {task.agent_name}" if task.agent_name else "WAITING FOR AN AGENT"
            header = f"#{task.id} {task.role} - {who} - {task.status} {clock} {cost}".strip()
            font.draw(canvas, header[:DIALOG_COLS], text_x, first_y, DARKEST)
            lines = self._transcript_lines(task)
            max_scroll = max(0, len(lines) - (body_rows - 1))
            self.scroll = max(0, min(self.scroll, max_scroll))
            start = max_scroll - self.scroll
            for i, line in enumerate(lines[start: start + body_rows - 1]):
                color = DARKEST if not line.startswith("YOU:") else SHADOW
                font.draw(canvas, line, text_x, first_y + LINE_H * (i + 1), color)
            if max_scroll:
                font.draw(canvas, f"{'^' if self.scroll < max_scroll else ' '}{'V' if self.scroll else ' '}", CANVAS_W - 20, first_y, SHADOW)
            if task.active:
                prompt = "RUNNING (DEL = CANCEL)> "
            elif task.status == STATUS_NEW:
                prompt = "QUEUED - ADD NOTE> "
            else:
                prompt = "REPLY> "
        columns = DIALOG_COLS - len(prompt)
        font.draw(canvas, prompt, text_x, input_y, DARKEST)
        font.draw(canvas, self.input.visible(columns, blink), text_x + PixelFont.width(prompt), input_y, DARKEST)


# ---------------------------------------------------------------------------------------------
# Handheld device frame, scaling and the main loop
# ---------------------------------------------------------------------------------------------

class Device:
    """Draws the canvas inside a handheld shell and scales everything up in whole pixels."""

    def __init__(self, scale: int, grid: bool = True) -> None:
        self.scale = scale
        self.grid = grid
        self.font = PixelFont()
        self.shell = pygame.Surface((DEVICE_W, DEVICE_H))
        self.window = pygame.display.set_mode((DEVICE_W * scale, DEVICE_H * scale))
        self.overlay = self._make_overlay()

    def _make_overlay(self) -> pygame.Surface:
        overlay = pygame.Surface((CANVAS_W * self.scale, CANVAS_H * self.scale), pygame.SRCALPHA)
        if self.scale >= 2:
            for y in range(0, CANVAS_H * self.scale, self.scale):
                overlay.fill((*DARKEST, 46), (0, y + self.scale - 1, CANVAS_W * self.scale, 1))
        return overlay

    def to_canvas(self, pos: Tuple[int, int]) -> Optional[Tuple[int, int]]:
        x = pos[0] // self.scale - BEZEL_L
        y = pos[1] // self.scale - BEZEL_T
        if 0 <= x < CANVAS_W and 0 <= y < CANVAS_H:
            return x, y
        return None

    def present(self, canvas: pygame.Surface, busy: bool, frame: int) -> None:
        shell = self.shell
        shell.fill(DARKEST)
        shell.fill(SHADOW, (BEZEL_L - 3, BEZEL_T - 3, CANVAS_W + 6, CANVAS_H + 6))
        shell.fill(DARKEST, (BEZEL_L - 1, BEZEL_T - 1, CANVAS_W + 2, CANVAS_H + 2))
        shell.blit(canvas, (BEZEL_L, BEZEL_T))
        label = APP_NAME.upper()
        self.font.draw(shell, label, (DEVICE_W - PixelFont.width(label)) // 2, CANVAS_H + BEZEL_T + 8, LIGHT)
        led_on = busy and (frame // 15) % 2 == 0 or not busy
        shell.fill(LIGHT if led_on else SHADOW, (BEZEL_L, CANVAS_H + BEZEL_T + 9, 4, 4))
        self.font.draw(shell, "POWER", BEZEL_L + 7, CANVAS_H + BEZEL_T + 8, SHADOW)
        scaled = pygame.transform.scale(shell, self.window.get_size())
        self.window.blit(scaled, (0, 0))
        if self.grid and self.scale >= 2:
            self.window.blit(self.overlay, (BEZEL_L * self.scale, BEZEL_T * self.scale))
        pygame.display.flip()


def make_icon() -> pygame.Surface:
    icon = pygame.Surface((32, 32))
    icon.fill(DARKEST)
    icon.fill(BACKGROUND, (2, 2, 28, 28))
    font = PixelFont()
    font.draw(icon, "IM", 4, 4, DARKEST, 2)
    icon.fill(SHADOW, (4, 22, 24, 4))
    return icon


def auto_scale() -> int:
    try:
        info = pygame.display.Info()
        width, height = info.current_w, info.current_h
    except pygame.error:
        return 2
    if width <= 0 or height <= 0:
        return 2
    scale = 1
    while (DEVICE_W * (scale + 1) <= width * 0.94) and (DEVICE_H * (scale + 1) <= height * 0.88):
        scale += 1
    return max(1, scale)


def state_file_for(project_dir: str) -> Path:
    digest = hashlib.sha1(os.path.abspath(project_dir).encode("utf-8")).hexdigest()[:10]
    slug = re.sub(r"[^A-Za-z0-9]+", "-", os.path.basename(project_dir.rstrip(os.sep)) or "project").strip("-")[:30]
    return Path.home() / ".infinitymetin_claude_manager" / f"{slug}-{digest}.json"


def make_logger() -> Callable[[str], None]:
    log_dir = Path.home() / ".infinitymetin_claude_manager"
    handle = None
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / "manager.log", "a", encoding="utf-8")
    except OSError:
        pass

    def log(text: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}"
        if handle:
            try:
                handle.write(line + "\n")
                handle.flush()
            except OSError:
                pass
        if sys.stdout is not None:  # a windowed .exe has no console
            try:
                print(line)
            except (OSError, ValueError):
                pass

    return log


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--project", default=os.getcwd(), help="project folder the agents work in (default: current folder)")
    parser.add_argument("--sim", action="store_true", help="simulation only, never start Claude Code")
    parser.add_argument("--claude", default="", help="path to the claude executable")
    parser.add_argument("--permission-mode", default="acceptEdits",
                        help="Claude Code permission mode for the agents (default: acceptEdits)")
    parser.add_argument("--bypass-permissions", action="store_true",
                        help="run agents with --dangerously-skip-permissions (they can run any command)")
    parser.add_argument("--model", default="", help="Claude model for the agents (default: Claude Code's default)")
    parser.add_argument("--max-turns", type=int, default=0, help="limit agent turns per task (0 = no limit)")
    parser.add_argument("--allowed-tools", default="",
                        help='tools the agents may use without asking, e.g. "Read,Edit,Bash(git *)"')
    parser.add_argument("--max-budget-usd", type=float, default=0.0, help="spending cap per task in dollars")
    parser.add_argument("--max-jobs", type=int, default=0,
                        help="Claude sessions running at the same time (default 4, or 12 in simulation)")
    parser.add_argument("--agents", type=int, default=24, help="number of agents in the office (default 24)")
    parser.add_argument("--scale", type=int, default=0, help="pixel scale (default: fit the screen)")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--no-grid", action="store_true", help="start without the LCD grid overlay")
    parser.add_argument("--fresh", action="store_true", help="ignore the saved task list")
    return parser.parse_args(argv)


def build_backend(args: argparse.Namespace, events: "queue.Queue[Dict[str, Any]]", log: Callable[[str], None]) -> Any:
    if args.sim:
        backend = SimBackend(events)
        backend.forced = True
        return backend
    executable = find_claude(args.claude or None)
    if executable:
        log(f"using claude at {executable}")
        return ClaudeBackend(executable, args.project, events, args.permission_mode, args.model,
                             args.max_turns, args.bypass_permissions, log, args.allowed_tools, args.max_budget_usd)
    log("claude executable not found; simulation mode")
    return SimBackend(events)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    log = make_logger()
    project = os.path.abspath(args.project)
    if not os.path.isdir(project):
        log(f"project folder not found: {project}; using the current folder")
        project = os.getcwd()
    args.project = project
    pygame.mixer.pre_init(22050, -16, 1, 512)
    pygame.init()
    pygame.display.set_caption(f"{APP_NAME} - {os.path.basename(project) or project}")
    pygame.display.set_icon(make_icon())
    scale = args.scale if args.scale > 0 else auto_scale()
    device = Device(scale, grid=not args.no_grid)
    sound = Chiptune(enabled=not args.no_sound)
    events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    backend = build_backend(args, events, log)
    state_path = None if args.fresh else state_file_for(project)
    max_jobs = args.max_jobs if args.max_jobs > 0 else (12 if backend.name == "SIM" else 4)
    engine = SimulationEngine(project, backend, sound, agent_count=args.agents, max_jobs=max_jobs,
                              log=log, state_path=state_path)
    if backend.name == "SIM" and not args.sim:
        engine.note("CLAUDE CODE NOT FOUND: SIMULATION MODE. INSTALL IT OR USE --CLAUDE PATH.")
    pygame.key.set_repeat(400, 40)
    clock = pygame.time.Clock()
    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.MOUSEBUTTONDOWN:
                canvas_pos = device.to_canvas(event.pos)
                if canvas_pos is None:
                    continue
                event = pygame.event.Event(pygame.MOUSEBUTTONDOWN, button=event.button, pos=event.pos, canvas_pos=canvas_pos)
            if event.type == pygame.KEYDOWN and event.key == pygame.K_g and pygame.key.get_mods() & (pygame.KMOD_CTRL | pygame.KMOD_META):
                device.grid = not device.grid
                continue
            if not engine.handle_event(event):
                running = False
        engine.update()
        device.present(engine.draw(), engine.running_jobs > 0, engine.frame)
        clock.tick(FPS)
    for agent in engine.agents:
        if agent.job:
            backend.cancel(agent.job)
    engine.save_state(force=True)
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
