"""The English texts of the macOS interface (Resources/locales/en/*.json, read by Sources/GramaCore/UI.swift).

Every tr("…") literal in the Swift sources needs an English entry with the same {placeholders}; an entry nobody
uses any more is reported too, so the catalogue does not rot."""
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
CATALOGUE = ROOT / 'Resources' / 'locales' / 'en'
# tr("…") and UI.tr("…"): the first argument, a plain Swift string literal (no interpolation — values go in {name}).
CALL = re.compile(r'(?<![\w.])(?:UI\.)?tr\(\s*"((?:[^"\\\n]|\\.)*)"')
PLACEHOLDER = re.compile(r'\{([a-zA-Z_][a-zA-Z0-9_]*)\}')
LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
# Texts kept in Polish as data (logged, compared, shared with Linux) and translated where they are shown —
# tr(change.reason), tr(scope), tr(issue.message). Their literals need an entry in dynamic.json.
DYNAMIC = {
    'Sources/GramaCore/Diff.swift': r'static func reason\(.*?\n    }\n',
    'Sources/GramaCore/GlobalShortcut.swift': r'static let reserved.*?\]\n',
    'Sources/GramaCore/LiveText.swift': r'case spelling = "[^"]*"',
    'Sources/GramaBackend/EventLog.swift': r'static func reasonText.*?\n    }\n',
    'Sources/GramaDrama/TextBridge.swift': r'var label: String \{.*?\n        }\n',
    'Sources/GramaDrama/LiveCheck.swift': r'message: "[^"]*"',
}


def unescape(literal):
    return re.sub(r'\\u\{([0-9a-fA-F]+)\}', lambda m: chr(int(m.group(1), 16)), literal) \
        .replace('\\n', '\n').replace('\\t', '\t').replace('\\"', '"').replace('\\\\', '\\')


def used():
    found = {}
    for path in sorted((ROOT / 'Sources').rglob('*.swift')):
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            if line.lstrip().startswith('//'):
                continue  # examples in comments
            for match in CALL.finditer(line):
                found.setdefault(unescape(match.group(1)), f'{path.relative_to(ROOT)}:{number}')
    return found


def catalogue():
    entries = {}
    for path in sorted(CATALOGUE.glob('*.json')):
        for key, value in json.loads(path.read_text(encoding='utf-8')).items():
            entries.setdefault(key, (value, path.name))
    return entries


def dynamic_texts():
    found = {}
    for name, pattern in DYNAMIC.items():
        regions = re.findall(pattern, (ROOT / name).read_text(encoding='utf-8'), re.S)
        assert regions, f'{name}: {pattern} not found'
        for region in regions:
            for match in LITERAL.finditer(region):
                if region[:match.start()].endswith('case '):
                    continue  # a log code being matched, not a text
                text = unescape(match.group(1))
                # Words only: not rule ids (MORFOLOGIK) or shortcuts (⌥⌘D).
                if re.search(r'[^\W\d_]{2}', text) and not re.fullmatch(r'[A-Z0-9_]+', text):
                    found.setdefault(text, name)
    return found


class CatalogueTests(unittest.TestCase):
    def test_every_text_has_an_english_entry(self):
        missing = {text: where for text, where in used().items() if text not in catalogue()}
        self.assertEqual(missing, {}, 'tr("…") without an English entry in Resources/locales/en')

    def test_placeholders_match(self):
        wrong = {key: value for key, (value, _) in catalogue().items()
                 if set(PLACEHOLDER.findall(key)) != set(PLACEHOLDER.findall(value)) or not value.strip()}
        self.assertEqual(wrong, {})

    def test_no_unused_entries(self):
        texts = used()
        # Texts translated through a variable (tr(outcome)) are listed in dynamic.json and kept.
        unused = [key for key, (_, file) in catalogue().items() if key not in texts and file != 'dynamic.json']
        self.assertEqual(unused, [])

    def test_texts_translated_through_a_variable(self):
        entries = catalogue()
        missing = {text: where for text, where in dynamic_texts().items() if text not in entries}
        self.assertEqual(missing, {}, 'add these to Resources/locales/en/dynamic.json')

    def test_dynamic_entries_still_exist(self):
        sources = '\n'.join(path.read_text(encoding='utf-8') for path in (ROOT / 'Sources').rglob('*.swift'))
        literals = {unescape(literal) for literal in LITERAL.findall(sources)}
        stale = [key for key, (_, file) in catalogue().items() if file == 'dynamic.json' and key not in literals]
        self.assertEqual(stale, [])

    def test_each_text_in_one_file(self):
        seen, duplicates = {}, []
        for path in sorted(CATALOGUE.glob('*.json')):
            for key in json.loads(path.read_text(encoding='utf-8')):
                if key in seen:
                    duplicates.append((key, seen[key], path.name))
                seen.setdefault(key, path.name)
        self.assertEqual(duplicates, [])


if __name__ == '__main__':
    unittest.main()
