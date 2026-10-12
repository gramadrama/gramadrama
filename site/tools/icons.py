#!/usr/bin/env python3
"""The site's icons from the app's logo (linux/icon.svg): icon-192.png (the rounded square, transparent
corners), apple-touch-icon.png (180 px, full bleed: iOS rounds the corners itself) and favicon.ico
(16, 32 and 48 px). Needs GdkPixbuf with its SVG loader (librsvg) and Pillow. Safe to run again.

    python3 site/tools/icons.py
"""
import re
import tempfile
from pathlib import Path

import gi

gi.require_version('GdkPixbuf', '2.0')
from gi.repository import GdkPixbuf  # noqa: E402
from PIL import Image  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent.parent
LOGO = ROOT / 'linux' / 'icon.svg'
IMG = ROOT / 'site' / 'public' / 'img'
PLATE = re.compile(r'<rect x="4" y="4" width="120" height="120" rx="28" (fill="#[0-9A-Fa-f]{6}")/>')


def render(svg, size):
    """The SVG text at size x size px, as an RGBA image."""
    with tempfile.TemporaryDirectory() as tmp:
        source, png = Path(tmp) / 'logo.svg', Path(tmp) / 'logo.png'
        source.write_text(svg)
        GdkPixbuf.Pixbuf.new_from_file_at_size(str(source), size, size).savev(str(png), 'png', [], [])
        return Image.open(png).convert('RGBA')


def main():
    svg = LOGO.read_text()
    if not PLATE.search(svg):
        raise SystemExit('linux/icon.svg: the rounded square was not found')
    full_bleed = PLATE.sub(r'<rect width="128" height="128" \1/>', svg)
    render(svg, 192).save(IMG / 'icon-192.png', optimize=True)
    render(full_bleed, 180).convert('RGB').save(IMG / 'apple-touch-icon.png', optimize=True)
    sizes = [16, 32, 48]
    largest = render(svg, 48)
    largest.save(IMG / 'favicon.ico', sizes=[(s, s) for s in sizes],
                 append_images=[render(svg, s) for s in sizes[:-1]])
    print('icon-192.png, apple-touch-icon.png, favicon.ico ->', IMG.relative_to(ROOT))


if __name__ == '__main__':
    main()
