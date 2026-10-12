"""tests/prompt_profiles.json: the prompts of the language profiles other than Polish + English (prompts.for_languages),
which macOS builds the same way (PromptFiles.forLanguages; SharedFixturesTests compares). Polish + English is
bench/prompts.golden.json, written by macOS. Each entry is the start of a request without the user's text: the system
prompt and the examples, for every mode and translation target, and the dictation's cleanup.

From linux/: python3 tests/prompt_profiles.py — writes the file anew (test_prompts checks that it is current)."""
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from gramadrama import core, dictation  # noqa: E402
from gramadrama.prompts import Prompts  # noqa: E402

PATH = pathlib.Path(__file__).resolve().parents[2] / 'tests' / 'prompt_profiles.json'
PROFILES = (('en',), ('de',), ('pl',), ('de', 'en'), ('en', 'de'), ('en', 'pl'), ('pl', 'de'))
MODES = ('fix', 'improve', 'formal', 'casual', 'shorter')


def document():
    defaults = Prompts.defaults()
    entries = []
    for languages in PROFILES:
        for mode in MODES:
            entries.append({'languages': list(languages), 'mode': mode, 'target': None,
                            'messages': core.messages('x', mode, prompts=defaults, languages=languages)[:-1]})
        if len(languages) > 1:
            for target in languages:
                entries.append({'languages': list(languages), 'mode': 'translate', 'target': target,
                                'messages': core.messages('x', 'translate', target, prompts=defaults, languages=languages)[:-1]})
        cleanup = dictation.cleanup_messages('x', [], lambda text: core.detect_language(text, languages), defaults, languages)
        entries.append({'languages': list(languages), 'mode': 'cleanup', 'target': None, 'messages': cleanup[:-1]})
    return {'description': 'Początki zapytań profili języków innych niż polski + angielski (prompts.for_languages): prompt '
                           'systemowy i przykłady, bez tekstu użytkownika. Generowane: python3 tests/prompt_profiles.py '
                           '(z katalogu linux/). Nie edytować ręcznie.',
            'entries': entries}


if __name__ == '__main__':
    PATH.write_text(json.dumps(document(), ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print('%s: %d zestawów' % (PATH, len(document()['entries'])))
