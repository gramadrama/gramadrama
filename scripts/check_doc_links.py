#!/usr/bin/env python3
"""Check relative links in Markdown files: the target file exists and its #anchor is a heading there.

Scans the repository's tracked and new (not ignored) Markdown files, or every Markdown file under --root
(for a directory that is not a Git checkout, such as a release export). External links
(http, https, mailto) are not fetched. Anchors follow GitHub's heading slugs.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
from urllib.parse import unquote


LINK = re.compile(r'(?<!!)\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
IMAGE = re.compile(r'!\[(?:[^\]\\]|\\.)*\]\(\s*<?([^)\s>]+)>?(?:\s+"[^"]*")?\s*\)')
REFERENCE = re.compile(r'^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$')
HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$')
FENCE = re.compile(r'^\s{0,3}(`{3,}|~{3,})')
EXTERNAL = re.compile(r'^[a-zA-Z][a-zA-Z0-9+.-]*:')
HTML_ANCHOR = re.compile(r'<a\s+(?:name|id)="([^"]+)"', re.I)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    target: str
    problem: str


def slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, inline markup dropped, punctuation removed, spaces to hyphens."""
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', heading)  # links and images keep their text
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('`', '').replace('*', '')
    text = text.strip().lower()
    text = ''.join(char for char in text if char.isalnum() or char in ' -_')
    return text.replace(' ', '-')


def lines_outside_code(text: str):
    """(number, line) of the lines that are not inside fenced code blocks."""
    fence = None
    for number, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match:
            marker = match.group(1)
            if fence is None:
                fence = marker[0] * 3
            elif marker.startswith(fence):
                fence = None
            continue
        if fence is None:
            yield number, line


def anchors(text: str) -> set[str]:
    found, seen = set(), {}
    for _, line in lines_outside_code(text):
        found.update(match.group(1) for match in HTML_ANCHOR.finditer(line))
        match = HEADING.match(line)
        if not match:
            continue
        base = slug(match.group(2))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else '%s-%d' % (base, count))
    return found


def strip_inline_code(line: str) -> str:
    return re.sub(r'(`+)(?:(?!\1).)*\1', '', line)


def check(root: Path, markdown: list[str], existing: set[str]) -> list[Finding]:
    """Findings for the given Markdown paths (relative, POSIX) under root; existing = every file path in the set."""
    directories = {str(parent) for path in existing for parent in PurePosixPath(path).parents}
    cache: dict[str, set[str]] = {}

    def anchors_of(path: str) -> set[str]:
        if path not in cache:
            cache[path] = anchors((root / path).read_text(encoding='utf-8', errors='replace'))
        return cache[path]

    findings = []
    for path in markdown:
        text = (root / path).read_text(encoding='utf-8', errors='replace')
        for number, line in lines_outside_code(text):
            line = strip_inline_code(line)
            targets = [match.group(1) for pattern in (LINK, IMAGE) for match in pattern.finditer(line)]
            reference = REFERENCE.match(line)
            if reference:
                targets.append(reference.group(1))
            for target in targets:
                if EXTERNAL.match(target) or target.startswith('//'):
                    continue
                file_part, _, anchor = target.partition('#')
                file_part = unquote(file_part)
                if file_part:
                    resolved = os.path.normpath(str(PurePosixPath(path).parent / file_part)).replace(os.sep, '/')
                    if file_part.startswith('/') or resolved.startswith('..'):
                        findings.append(Finding(path, number, target, 'points outside the repository'))
                        continue
                    if resolved not in existing and resolved.rstrip('/') not in directories:
                        findings.append(Finding(path, number, target, 'missing file'))
                        continue
                else:
                    resolved = path
                if anchor and resolved.endswith('.md') and unquote(anchor) not in anchors_of(resolved):
                    findings.append(Finding(path, number, target, 'missing heading'))
    return findings


def git_files(root: Path) -> list[str]:
    output = subprocess.run(['git', '-C', str(root), 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], check=True,
                            stdout=subprocess.PIPE).stdout
    return [os.fsdecode(name) for name in output.split(b'\0') if name and (root / os.fsdecode(name)).exists()]


def tree_files(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)).replace(os.sep, '/') for path in root.rglob('*')
                  if path.is_file() and '.git' not in path.relative_to(root).parts)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, help='check every file under this directory instead of the Git checkout')
    args = parser.parse_args(argv)
    if args.root:
        root, files = args.root.resolve(), tree_files(args.root.resolve())
    else:
        root = Path(subprocess.run(['git', 'rev-parse', '--show-toplevel'], check=True,
                                   stdout=subprocess.PIPE, text=True).stdout.strip())
        files = git_files(root)
    markdown = [path for path in files if path.endswith('.md')]
    findings = check(root, markdown, set(files))
    for finding in findings:
        print('%s:%d: %s: %s' % (finding.path, finding.line, finding.problem, finding.target))
    print('Link check: %d Markdown file(s), %d broken link(s).' % (len(markdown), len(findings)))
    return 1 if findings else 0


if __name__ == '__main__':
    sys.exit(main())
