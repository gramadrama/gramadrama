#!/usr/bin/env python3
"""What the live check's layers find on bench/cases.json: errors found, alarms, and the layers' times.

An error is a stretch of the input that the expected text changes (words compared one by one); it is found
when some issue overlaps it. An alarm is an issue that overlaps no error, in any case. Layer 0 is the
dictionary on this machine, layer 1 is LanguageTool on the server (settings.json); Bielik is not asked.
Sends only the benchmark's sentences.

The measurement that decided to drop Harper (2026-10-01, 16 English and mixed cases, 23 errors):

                                found  alarms  layer 0   layer 1
    A  Harper + dictionary        17     1      3.0 ms    -        ("very good" -> "Excellent")
    B  no Harper                  19     1      0.4 ms   68 ms     (" and": COMMA_COMPOUND_SENTENCE)
    C  B without that rule        19     0      0.4 ms   71 ms     <- what the service does now

B and C also found "Your welcome" and "have went / it look", which Harper did not. The same direction as
the macOS measurement of 2026-09-29 (48 -> 51 of 57 errors, alarms 5 -> 4). Run this script to see the
numbers of the current build.

usage: python3 tests/lint_bench.py [--lang en,mix,pl] [-v]
"""
import argparse
import difflib
import json
import re
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from gramadrama import live as L, liveservice as S  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TOKEN = re.compile(r"\w+(?:['’]\w+)*|[^\w\s]")


def error_spans(original, expected):
    """[(start, end)] of the input that the expected text changes, per word and punctuation mark."""
    a, b = list(TOKEN.finditer(original)), list(TOKEN.finditer(expected))
    ops = difflib.SequenceMatcher(None, [m.group() for m in a], [m.group() for m in b], autojunk=False).get_opcodes()
    spans = []
    for tag, i1, i2, _j1, _j2 in ops:
        if tag == 'equal':
            continue
        if i1 < i2:
            spans.append((a[i1].start(), a[i2 - 1].end()))
        else:  # something added: the words around the gap
            near = [a[k] for k in (i1 - 1, i1) if 0 <= k < len(a)]
            if near:
                spans.append((near[0].start(), near[-1].end()))
    return spans


def overlaps(issue, span):
    return issue.start < span[1] and span[0] < issue.end


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--lang', default='en,mix', help='languages of bench/cases.json to run (default en,mix)')
    ap.add_argument('-v', '--verbose', action='store_true', help='every case')
    args = ap.parse_args()
    wanted = set(args.lang.split(','))
    cases = [c for c in json.loads((REPO / 'bench' / 'cases.json').read_text(encoding='utf-8')) if c['lang'] in wanted]

    server = S.backend(S.settings())
    if server is None:
        sys.exit('No LanguageTool server in settings.json.')
    checker = S.Checker()
    found = alarms = total = down = 0
    quick_ms, lt_ms, missed, alarm_list = [], [], [], []
    for case in cases:
        spans = error_spans(case['input'], case['expected'])
        total += len(spans)
        text = L.LiveText(case['input'])
        for _ in range(2):  # the first run loads the dictionaries and warms the connection; the second is measured
            t0 = time.monotonic()
            quick = checker.quick(text)
            t1 = time.monotonic()
            lt = checker.languagetool(text, server)
            t2 = time.monotonic()
        issues = L.merge(quick, lt or [])
        hit = [sp for sp in spans if any(overlaps(i, sp) for i in issues)]
        extra = [i for i in issues if not any(overlaps(i, sp) for sp in spans)]
        found += len(hit)
        alarms += len(extra)
        down += lt is None
        quick_ms.append((t1 - t0) * 1000)
        lt_ms.append((t2 - t1) * 1000)
        missed += [case['input'][a:b] for a, b in spans if (a, b) not in hit]
        alarm_list += ['%s[%s]' % (i.word, i.rule) for i in extra]
        if args.verbose:
            print('%-4s %-62s  %d/%d +%d' % (case['lang'], case['input'][:62], len(hit), len(spans), len(extra)))
    print('\n%d cases (%s): %d/%d errors found, %d alarms' % (len(cases), ','.join(sorted(wanted)), found, total, alarms))
    print('layer 0 %.1f ms, layer 1 %.1f ms (medians per case)%s' % (
        statistics.median(quick_ms), statistics.median(lt_ms),
        '; LanguageTool did not answer %d times' % down if down else ''))
    print('missed: %s\nalarms: %s' % (', '.join(missed) or '-', ', '.join(alarm_list) or '-'))


if __name__ == '__main__':
    main()
