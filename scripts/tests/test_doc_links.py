"""Tests for the Markdown link check on temporary directories."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'check_doc_links.py'
SPEC = importlib.util.spec_from_file_location('doc_links', SCRIPT)
links = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = links
SPEC.loader.exec_module(links)


class SlugTests(unittest.TestCase):
    def test_github_heading_slugs(self):
        self.assertEqual(links.slug('Get started on Linux'), 'get-started-on-linux')
        self.assertEqual(links.slug('Services, prompts and privacy'), 'services-prompts-and-privacy')
        self.assertEqual(links.slug('Prywatność'), 'prywatność')
        self.assertEqual(links.slug('`settings.json` and [the app](x.md)'), 'settingsjson-and-the-app')
        self.assertEqual(links.slug('G3. Backends — postponed'), 'g3-backends--postponed')

    def test_repeated_headings_get_numbered_anchors(self):
        self.assertEqual(links.anchors('# Tests\n## Tests\n### Tests\n'), {'tests', 'tests-1', 'tests-2'})

    def test_headings_in_code_blocks_are_not_anchors(self):
        self.assertEqual(links.anchors('# Real\n```sh\n# comment\n```\n'), {'real'})


class CheckTests(unittest.TestCase):
    def findings(self, files):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, text in files.items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text, encoding='utf-8')
            markdown = [name for name in files if name.endswith('.md')]
            return [(found.path, found.line, found.problem)
                    for found in links.check(root, markdown, set(files))]

    def test_valid_links_pass(self):
        self.assertEqual(self.findings({
            'README.md': '# Intro\n[guide](docs/guide.md#install) [self](#intro) [dir](docs/) '
                         '[web](https://example.com) [mail](mailto:info@example.com) ![logo](img/logo.png)\n',
            'docs/guide.md': '## Install\n[back](../README.md)\n',
            'img/logo.png': '',
        }), [])

    def test_missing_file_heading_and_escape_are_reported(self):
        self.assertEqual(self.findings({
            'README.md': '# Intro\n[a](missing.md)\n[b](docs/guide.md#nowhere)\n[c](#outro)\n[d](../outside.md)\n',
            'docs/guide.md': '## Install\n',
        }), [('README.md', 2, 'missing file'), ('README.md', 3, 'missing heading'),
             ('README.md', 4, 'missing heading'), ('README.md', 5, 'points outside the repository')])

    def test_links_in_code_are_ignored(self):
        self.assertEqual(self.findings({
            'README.md': '# Intro\n`[a](missing.md)`\n```md\n[b](missing.md)\n```\n',
        }), [])

    def test_reference_definitions_are_checked(self):
        self.assertEqual(self.findings({'README.md': '# Intro\n[ref]: gone.md\n'}),
                         [('README.md', 2, 'missing file')])


if __name__ == '__main__':
    unittest.main()
