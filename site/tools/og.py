#!/usr/bin/env python3
"""Render the PL/EN sharing cards with the site's existing icon and calm palette.

Requires Pillow and the DejaVu Sans font. The icon is generated from linux/icon.svg
by site/tools/icons.py. Run with ``python3 site/tools/og.py``; only img/og.png and
img/og-en.png are written.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

IMG = Path(__file__).resolve().parent.parent / 'public' / 'img'
SCALE = 2
SIZE = (1200, 630)
COLORS = {
    'background': '#F7F5F1',
    'ink': '#343337',
    'secondary': '#6B6870',
    'lavender': '#66579E',
    'lavender_soft': '#F0EDED',
    'sage': '#536B60',
    'sage_soft': '#E9F0E9',
}


def render(filename, first_line, second_line, subtitle):
    card = Image.new('RGB', tuple(value * SCALE for value in SIZE), COLORS['background'])
    draw = ImageDraw.Draw(card)

    def box(values):
        return tuple(value * SCALE for value in values)

    def text(position, content, size, color, bold=False):
        font = ImageFont.truetype('DejaVuSans-Bold.ttf' if bold else 'DejaVuSans.ttf', size * SCALE)
        draw.text(box(position), content, font=font, fill=COLORS[color], anchor='ls')

    draw.ellipse(box((850, -275, 1450, 325)), fill=COLORS['lavender_soft'])
    draw.ellipse(box((915, 450, 1335, 870)), fill=COLORS['sage_soft'])
    logo = Image.open(IMG / 'icon-192.png').convert('RGBA')
    logo = logo.resize((76 * SCALE, 76 * SCALE), Image.Resampling.LANCZOS)
    card.paste(logo, (74 * SCALE, 60 * SCALE), logo)
    text((170, 118), 'GramaDrama', 36, 'ink', bold=True)
    text((76, 278), first_line, 82, 'ink', bold=True)
    text((76, 378), second_line, 82, 'lavender', bold=True)
    text((80, 490), subtitle, 32, 'secondary')
    draw.rounded_rectangle(box((80, 538, 168, 544)), radius=3 * SCALE, fill=COLORS['sage'])
    card.resize(SIZE, Image.Resampling.LANCZOS).save(IMG / filename, optimize=True)
    print(f'Generated site/public/img/{filename} (1200 × 630).')


def main():
    render('og.png', 'Pisz bez', 'dramatu.', 'Korekta tekstu i dyktowanie. Także lokalnie.')
    render('og-en.png', 'Write without', 'the drama.', 'Proofreading and dictation. Locally, too.')


if __name__ == '__main__':
    main()
