#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["textual>=3.0", "psutil>=5.9"]
# ///
"""Regenerate the website screenshots from demo data: ./docs/make_shots.py"""
import asyncio
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from plexonator import Plexonator  # noqa: E402

OUT = Path(__file__).parent / "shots"
FONT = "ui-monospace,'Cascadia Code','JetBrains Mono','Fira Code',Menlo,Consolas,'DejaVu Sans Mono',monospace"
# name, demo scenario, theme, (columns, rows), key to press first
SHOTS = [
    ("hero", "busy", "tokyo-night", (150, 34), None),
    ("calm", "calm", "tokyo-night", (150, 34), None),
    ("settings", "busy", "tokyo-night", (150, 34), "s"),
    ("side", "busy", "tokyo-night", (76, 46), None),
    ("gruvbox", "busy", "gruvbox", (150, 34), None),
    ("dracula", "busy", "dracula", (150, 34), None),
    ("nord", "calm", "nord", (150, 34), None),
    ("catppuccin", "busy", "catppuccin-mocha", (150, 34), None),
    ("light", "calm", "solarized-light", (150, 34), None),
]


def crop(svg):
    """Drop Rich's window chrome and web font so the image is just the terminal grid."""
    x, y = re.search(r'<g transform="translate\((\d+(?:\.\d+)?), (\d+(?:\.\d+)?)\)" clip-path', svg).groups()
    w, h = re.search(r'clip-terminal">\s*<rect x="0" y="0" width="([\d.]+)" height="([\d.]+)"', svg).groups()
    svg = re.sub(r'<svg class="rich-terminal" viewBox="[^"]*"', f'<svg class="rich-terminal" viewBox="{x} {y} {w} {h}"', svg)
    svg = re.sub(r"@font-face\s*{.*?}", "", svg, flags=re.S)
    return re.sub(r"font-family: Fira Code, monospace", f"font-family: {FONT}", svg)


async def shoot(name, scenario, theme, size, key):
    random.seed(7)
    app = Plexonator(demo=scenario)
    app.cfg = {**app.cfg, "theme": theme, "logo": True, "refresh": 2}  # never saved: no Settings "Save" here
    async with app.run_test(size=size) as pilot:
        await pilot.pause(0.4)
        if key:
            await pilot.press(key)
            await pilot.pause(0.4)
        (OUT / f"{name}.svg").write_text(crop(app.export_screenshot()))
    print("wrote", name)


async def main():
    OUT.mkdir(exist_ok=True)
    for shot in SHOTS:
        await shoot(*shot)

asyncio.run(main())
