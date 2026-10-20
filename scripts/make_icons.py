"""Genererer PNG/ICO fra SVG-ene. Kjør: uv run --with resvg-py --with pillow scripts/make_icons.py"""
import io
from pathlib import Path

import resvg_py
from PIL import Image

static = Path(__file__).resolve().parent.parent / "app" / "static"


def render(svg: str, size: int) -> Image.Image:
    png = resvg_py.svg_to_bytes(svg_path=str(static / svg), width=size, height=size)
    return Image.open(io.BytesIO(bytes(png))).convert("RGBA")


for size, name in [(180, "apple-touch-icon.png"), (192, "icon-192.png"), (512, "icon-512.png")]:
    render("logo.svg", size).save(static / name)
icons = [render("favicon.svg", s) for s in (16, 32, 48)]
icons[-1].save(static / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)], append_images=icons[:-1])
print("ok")
