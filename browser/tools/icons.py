#!/usr/bin/env python3
"""The browser extension's icons from the app's logo (linux/icon.svg), in browser/extension/icons:
icon{16,32,48,128}.png (GramaDrama works on the page) and icon{16,32,48,128}-off.png (the site is
off or the app is not running: the same masks on a grey square). 16 and 32 px fill the whole icon so
that the masks stay readable in the toolbar; 128 px keeps Chrome's 16 px transparent margin. Needs
GdkPixbuf with its SVG loader (librsvg) and Pillow. Safe to run again.

    python3 browser/tools/icons.py
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
ICONS = ROOT / 'browser' / 'extension' / 'icons'
PLATE = re.compile(r'<rect x="4" y="4" width="120" height="120" rx="28" fill="(#[0-9A-Fa-f]{6})"/>')
# The off icon: BRAND.md's grey of a switched-off switch (3.4:1 with the light masks).
OFF = '#8E8992'


def render(svg, size):
    """The SVG text at size x size px, as an RGBA image."""
    with tempfile.TemporaryDirectory() as tmp:
        source, png = Path(tmp) / 'logo.svg', Path(tmp) / 'logo.png'
        source.write_text(svg)
        GdkPixbuf.Pixbuf.new_from_file_at_size(str(source), size, size).savev(str(png), 'png', [], [])
        return Image.open(png).convert('RGBA')


def icon(svg, size):
    """The icon of one size: 16/32 full bleed, 48 as the logo, 128 drawn at 96 px in the middle."""
    if size <= 32:
        return render(PLATE.sub(r'<rect width="128" height="128" rx="28" fill="\1"/>', svg), size)
    if size == 128:
        image = Image.new('RGBA', (128, 128))
        image.alpha_composite(render(svg, 96), (16, 16))
        return image
    return render(svg, size)


def main():
    svg = LOGO.read_text()
    if not PLATE.search(svg):
        raise SystemExit('linux/icon.svg: the rounded square was not found')
    off = PLATE.sub(lambda match: match.group(0).replace(match.group(1), OFF), svg)
    for size in (16, 32, 48, 128):
        icon(svg, size).save(ICONS / f'icon{size}.png', optimize=True)
        icon(off, size).save(ICONS / f'icon{size}-off.png', optimize=True)
    print('icon{16,32,48,128}.png and -off.png ->', ICONS.relative_to(ROOT))


if __name__ == '__main__':
    main()
