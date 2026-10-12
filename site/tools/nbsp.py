#!/usr/bin/env python3
"""Polish typography: a one-letter word (a, i, o, u, w, z) must not end a line, so glue it to the next
word with a non-breaking space. Edits only the text between tags in the Polish pages, never attributes,
<style> or <script>. Safe to run again: replaced spaces no longer match.

    python3 site/tools/nbsp.py
"""
import re
import sys
from pathlib import Path

PAGES = [Path(__file__).resolve().parent.parent / "public" / "index.html"]
ONE_LETTER = re.compile(r"(?<=[\s>])([aiouwzAIOUWZ]) (?=\S)")
TAG_OR_BLOCK = re.compile(r"(<(?:style|script)\b.*?</(?:style|script)>|<[^>]*>)", re.S | re.I)


def fix(html: str) -> str:
    out = []
    for part in TAG_OR_BLOCK.split(html):
        if part.startswith("<"):
            out.append(part)
        else:
            out.append(ONE_LETTER.sub(lambda m: m.group(1) + "&nbsp;", part))
    return "".join(out)


def main() -> int:
    changed = 0
    for page in PAGES:
        before = page.read_text(encoding="utf-8")
        after = fix(before)
        if after != before:
            page.write_text(after, encoding="utf-8")
            changed += 1
            print(f"{page.relative_to(page.parent.parent.parent)}: {before.count(' ') - after.count(' ')} spaces glued")
    if not changed:
        print("nothing to change")
    return 0


if __name__ == "__main__":
    sys.exit(main())
