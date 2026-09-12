"""Tray mark — high-contrast variant for small system-tray sizes."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

log = logging.getLogger("cfsmcp2.launcher")

ACCENT = (34, 197, 94, 255)
SLATE = (15, 23, 42, 255)
WHITE = (248, 250, 252, 255)

# Native draw size; upscaled with NEAREST so edges stay crisp in the tray.
TRAY_DRAW_SIZE = 32


def draw_mark(size: int) -> Image.Image:
    """Window / ICO mark — ring + graph (matches web UI)."""
    card = (30, 41, 59, 255)
    edge = (148, 163, 184, 255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2
    r = int(size * 0.40)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=ACCENT, width=max(1, size // 16))

    def pt(fx: float, fy: float) -> tuple[int, int]:
        return int(size * fx), int(size * fy)

    nodes = [pt(0.5, 0.25), pt(0.28, 0.34), pt(0.72, 0.38), pt(0.38, 0.78), pt(0.68, 0.80), (cx, cy)]
    edges = [(0, 5), (0, 1), (0, 2), (1, 3), (2, 4), (3, 4), (3, 5), (4, 5)]
    lw = max(1, size // 16)
    for a, b in edges:
        draw.line([nodes[a], nodes[b]], fill=edge, width=lw)
    for i, (x, y) in enumerate(nodes):
        nr = max(2, size // 12 if i == 5 else size // 14)
        fill = ACCENT if i == 5 else card
        draw.ellipse((x - nr, y - nr, x + nr, y + nr), fill=fill, outline=ACCENT, width=max(1, size // 24))
    return img


def draw_tray_mark(size: int = TRAY_DRAW_SIZE) -> Image.Image:
    """Tray monogram: white «C» on green disk (readable at 16px)."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    cx = cy = size // 2
    r = int(size * 0.47)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=ACCENT)
    ring = max(1, size // 16)
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), outline=SLATE, width=ring)

    # Bold «C» — arc + short serif at open ends (vector-friendly at small sizes)
    letter_r = int(size * 0.30)
    bbox = (cx - letter_r, cy - letter_r, cx + letter_r, cy + letter_r)
    stroke = max(3, size // 6)
    draw.arc(bbox, start=55, end=305, fill=WHITE, width=stroke)
    cap = max(2, stroke // 2)
    top_x = cx + int(letter_r * 0.55)
    bot_x = top_x
    top_y = cy - letter_r
    bot_y = cy + letter_r - max(1, size // 16)
    draw.line([(top_x, top_y), (top_x + cap, top_y)], fill=WHITE, width=stroke)
    draw.line([(bot_x, bot_y), (bot_x + cap, bot_y)], fill=WHITE, width=stroke)
    return img


def load_tray_image(ico_path: Path | None, size: int = 64) -> Image.Image:
    """Tray always uses the bold variant; ICO resize blurs at 16px."""
    del ico_path  # window icon still uses cfs-mark.ico
    tray = draw_tray_mark(TRAY_DRAW_SIZE)
    if size == TRAY_DRAW_SIZE:
        return tray
    return tray.resize((size, size), Image.Resampling.NEAREST)
