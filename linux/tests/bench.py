"""Quality benchmark on the real server, with the measures of macOS `GramaDrama --bench`:

    python3 tests/bench.py ../bench/cases.json [model ...] [--raw]
    python3 tests/bench.py ../bench/translate.json --mode=translate
    python3 tests/bench.py ../bench/cases.json --prompts DIR     # the prompt files of DIR instead of your own
    python3 tests/bench.py ../bench/cases.json --prompts default # the macOS prompts, whatever files you have

The prompts and the post-processing are the engine's (core), so the results are what the user sees.
A change of a prompt is measured by running the benchmark with it and without it (docs/prompts.md).
Only the texts of the bench file go to the server in settings.json."""
import dataclasses
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gramadrama import core  # noqa: E402

COMMENT = re.compile(r'(?i)\b(translation|translated|tłumaczenie|przetłumaczon)')


def check_translation(output, case):
    """macOS Bench.checkTranslation: in the target language, not the original, protected fragments kept."""
    target, detected, problems = core.translation_target(case['input']), core.detect_language(output), []
    if target == 'en' and detected == 'pl':
        problems.append('nie po angielsku')
    if target == 'pl' and detected == 'en':
        problems.append('nie po polsku')
    if output.strip() == case['input'].strip():
        problems.append('bez tłumaczenia')
    missing = [keep for keep in case.get('keep', []) if keep not in output]
    if missing:
        problems.append('zgubione: ' + ' '.join(missing))
    if COMMENT.search(output) and not re.search(r'(?i)(translat|tłumacz)', case['input']):
        problems.append('komentarz modelu')
    return not problems, problems


def run(path, models, mode, raw, prompts=None):
    cases = json.load(open(path))
    settings = core.Settings.load()
    prompts = prompts or core.Prompts.current()  # the user's own files, as the engine reads them
    print('prompty: %s' % ('domyślne (macOS)' if not prompts.values else 'własne pliki: ' + ', '.join(prompts.overridden)))
    for file, reason in prompts.problems:
        print('  ! pominięty %s: %s' % (file, reason))
    summary = []
    for model in models or [settings.model]:
        backend = core.Backend(dataclasses.replace(settings, model=model), prompts=prompts)
        print('\n### %s (%s, %s)' % (model, settings.llm_url, settings.llm_api))
        try:
            backend.warm_up()
        except core.BackendError as exc:
            print('  ! rozgrzewanie: %s' % exc)
        exact = false_alarms = clean_cases = errors = 0
        latencies, speeds = [], []
        for case in cases:
            started = time.monotonic()
            try:
                output = backend.correct(case['input'], mode, core.translation_target(case['input']))
            except core.BackendError as exc:
                errors += 1
                print('  ! %s' % (exc or type(exc).__name__))
                continue
            latencies.append(time.monotonic() - started)
            speeds.append(backend.tokens_per_second)
            expected, problems = case.get('expected', ''), []
            if mode == 'translate':
                ok, problems = check_translation(output, case)
            else:
                ok = output == expected
                if case['input'] == expected:
                    clean_cases += 1
                    false_alarms += output != case['input']
            exact += ok
            print('  %s [%s] %4.1fs  %s' % ('✓' if ok else '✗', case['lang'], latencies[-1], output.replace('\n', '⏎')))
            if not ok and expected:
                print('        oczekiwano: %s' % expected)
            if problems:
                print('        problem:    %s' % '; '.join(problems))
            if raw and backend.raw.strip() != output.strip():
                print('        surowo:     %r' % backend.raw.strip())
        latencies.sort()
        median = latencies[len(latencies) // 2] if latencies else 0
        speed = sum(speeds) / len(speeds) if speeds else 0
        line = '%s: dokładne %d/%d · fałszywe alarmy %d/%d · mediana %.1fs · max %.1fs · %.1f tok/s' % (
            model, exact, len(cases), false_alarms, clean_cases, median, latencies[-1] if latencies else 0, speed)
        if errors:
            line += ' · BŁĘDY: %d/%d (wynik niemiarodajny)' % (errors, len(cases))
        print('== ' + line)
        summary.append(line)
    print('\n=== PODSUMOWANIE\n' + '\n'.join(summary))


def main(argv):
    argv = list(argv)
    prompts = None
    if '--prompts' in argv:  # a directory of prompt files, or "default" for none of them
        at = argv.index('--prompts')
        if at + 1 >= len(argv):
            print('--prompts chce katalogu z plikami promptów albo słowa default')
            return 2
        where = argv[at + 1]
        del argv[at:at + 2]
        prompts = core.Prompts.defaults() if where == 'default' else core.Prompts.load(where)
    flags = [a for a in argv if a.startswith('--')]
    positional = [a for a in argv if not a.startswith('--')]
    modes = [f[len('--mode='):] for f in flags if f.startswith('--mode=')]
    if not positional or (modes and modes[0] not in core.MODES):
        print('Użycie: python3 tests/bench.py <cases.json> [model ...] [--mode=fix|improve|formal|casual|shorter|translate] '
              '[--raw] [--prompts KATALOG|default]')
        return 2
    run(positional[0], positional[1:], modes[0] if modes else 'fix', '--raw' in flags, prompts)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
