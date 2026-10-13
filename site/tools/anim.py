#!/usr/bin/env python3
"""The site's animations from the frames shot by linux/tests/integration_panel.py --site: each loop is cropped,
the keys pressed are shown as keycaps, and the loop is written as an animated WebP (with a still picture for
reduced motion when the loop names one).

    python3 site/tools/anim.py FRAMES_DIR [OUT_DIR]

FRAMES_DIR/<name>.json: {"crop": [x, y, w, h], "variant": "light" | "dark", "still": index | null,
"frames": [{"file": "<png>", "ms": <duration>, "keys": ["Ctrl", "Alt", "G"] | null}]}.
OUT_DIR (default: site/public/img) gets anim-<name>.webp and, with a still, still-<name>.webp.
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT = Path('/usr/share/fonts/truetype/ubuntu/UbuntuSans[wdth,wght].ttf')
# the site's colours (site/public/css/site.css): the card, its frame, a keycap, its edge, the text
COLOURS = {'light': {'card': '#fffefc', 'line': '#ddd8d0', 'key': '#f0ede7', 'edge': '#cfc9c0', 'ink': '#343337',
                     'dim': '#6b6870', 'shadow': (28, 27, 31, 70)},
           'dark': {'card': '#242827', 'line': '#505852', 'key': '#303631', 'edge': '#505852', 'ink': '#f1eee8',
                    'dim': '#b9bdb8', 'shadow': (0, 0, 0, 150)}}
SIZE = 15  # px, the keycaps' text
MARGIN = 14  # from the picture's bottom left corner (the test leaves a band there under a correction's panel)


def font():
    if FONT.exists():
        f = ImageFont.truetype(str(FONT), SIZE)
        try:
            f.set_variation_by_name('Bold')
        except (OSError, ValueError):
            pass
        return f
    return ImageFont.load_default(SIZE)


def keycaps(frame, keys, colours, typeface):
    """The keys as keycaps on a small card at the bottom left corner, with a soft shadow."""
    draw = ImageDraw.Draw(frame)
    pad, gap, height = 10, 8, 30
    widths = [draw.textlength(k, font=typeface) + 2 * pad for k in keys]
    plus = draw.textlength('+', font=typeface)
    inner = sum(widths) + (len(keys) - 1) * (plus + 2 * gap)
    card_w, card_h = int(inner + 2 * 10), height + 2 * 9
    x0, y0 = MARGIN, frame.height - MARGIN - card_h
    shadow = Image.new('RGBA', frame.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle([x0, y0 + 4, x0 + card_w, y0 + card_h + 4], 12, fill=colours['shadow'])
    frame.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(8)))
    draw = ImageDraw.Draw(frame)
    draw.rounded_rectangle([x0, y0, x0 + card_w, y0 + card_h], 12, fill=colours['card'], outline=colours['line'])
    x, y = x0 + 10, y0 + 9
    for i, (key, w) in enumerate(zip(keys, widths)):
        if i:
            draw.text((x + gap, y + height / 2), '+', font=typeface, fill=colours['dim'], anchor='lm')
            x += plus + 2 * gap
        draw.rounded_rectangle([x, y, x + w, y + height], 7, fill=colours['edge'])  # the deeper bottom edge
        draw.rounded_rectangle([x, y, x + w, y + height - 2], 7, fill=colours['key'], outline=colours['edge'])
        draw.text((x + w / 2, y + (height - 2) / 2), key, font=typeface, fill=colours['ink'], anchor='mm')
        x += w


def build(spec_path, out_dir, typeface):
    spec = json.loads(spec_path.read_text(encoding='utf-8'))
    x, y, w, h = spec['crop']
    colours = COLOURS[spec['variant']]
    frames = []
    for f in spec['frames']:
        with Image.open(spec_path.parent / f['file']) as shot:
            frame = shot.convert('RGBA').crop((x, y, x + w, y + h))
        if f.get('keys'):
            keycaps(frame, f['keys'], colours, typeface)
        frames.append(frame.convert('RGB'))
    name = spec_path.stem
    anim = out_dir / ('anim-%s.webp' % name)
    frames[0].save(anim, 'WEBP', save_all=True, append_images=frames[1:], duration=[f['ms'] for f in spec['frames']],
                   loop=0, lossless=True, method=6)
    made = ['%s (%d frames, %.1f s, %d KB)' % (anim.name, len(frames), sum(f['ms'] for f in spec['frames']) / 1000,
                                              anim.stat().st_size // 1024)]
    if spec.get('still') is not None:
        still = out_dir / ('still-%s.webp' % name)
        frames[spec['still']].save(still, 'WEBP', lossless=True, method=6)
        made.append('%s (%d KB)' % (still.name, still.stat().st_size // 1024))
    return made, (w, h)


def main():
    if len(sys.argv) not in (2, 3):
        sys.exit(__doc__)
    frames_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2]) if len(sys.argv) == 3 else Path(__file__).resolve().parent.parent / 'public' / 'img'
    typeface = font()
    specs = sorted(frames_dir.glob('*.json'))
    if not specs:
        sys.exit('no loops in %s' % frames_dir)
    for spec in specs:
        made, size = build(spec, out_dir, typeface)
        print('%s %dx%d: %s' % (spec.stem, size[0], size[1], ', '.join(made)))


if __name__ == '__main__':
    main()
