#!/usr/bin/env python3
"""Offline release-note gates; no network, model, tags or GitHub mutations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('prepare_release', ROOT / 'scripts/prepare-release.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class ReleasePreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'plugin.json').write_text(json.dumps({'version': '3.5.1'}))
        (self.root / 'docs').mkdir()
        (self.root / 'docs/limits.md').write_text('Known limits')
        self.changelog = self.root / 'CHANGELOG.md'
        self.changelog.write_text(
            '# Changes\n\n## [3.5.1] — 2026-09-09\n\n'
            'Fable behavior failed; GLM untested.\n'
            '[Limits](docs/limits.md#details)\n'
            '[External](https://example.com/unchanged)\n'
            '\n## [3.5.0] — old\nOld release only.\n')

    def render(self, tag='v3.5.1', repository='tanweai/pua'):
        return MODULE.render_notes(self.root, tag, repository)

    def test_current_section_keeps_failures_and_binds_links_to_tag(self):
        notes = self.render()
        self.assertIn('Fable behavior failed; GLM untested.', notes)
        self.assertNotIn('Old release only', notes)
        self.assertIn('https://github.com/tanweai/pua/blob/v3.5.1/docs/limits.md#details', notes)
        self.assertIn('[External](https://example.com/unchanged)', notes)

    def test_tag_must_match_manifest_and_be_stable(self):
        for tag in ['v3.5.0', '3.5.1', 'v3.5.1-rc.1', 'v03.5.1', 'v3.5.1\nextra']:
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                self.render(tag=tag)

    def test_repository_name_cannot_inject_a_url_or_path(self):
        for repository in ['https://github.com/tanweai/pua', '../pua', 'tanweai/pua/extra', 'bad\nname/pua']:
            with self.subTest(repository=repository), self.assertRaises(ValueError):
                self.render(repository=repository)

    def test_missing_duplicate_or_empty_version_section_is_rejected(self):
        for text in ['## [3.5.0]\nOld only.\n', '## [3.5.1]\n\n',
                     '## [3.5.1]\nFirst.\n## [3.5.1]\nSecond.\n']:
            self.changelog.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.render()

    def test_local_links_must_exist_inside_repository(self):
        for target in ['docs/missing.md', '../outside.md', '/absolute/private.md']:
            self.changelog.write_text(f'## [3.5.1]\n[Bad]({target})\n')
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.render()

    def test_actual_release_keeps_documented_limits(self):
        version = json.loads((ROOT / 'plugin.json').read_text())['version']
        notes = MODULE.render_notes(ROOT, f'v{version}', 'tanweai/pua')
        self.assertIn('不是“全模型完全通过”认证', notes)
        self.assertIn('仍未解决', notes)
        self.assertIn(f'/blob/v{version}/docs/MODEL-MATRIX-20260909.md', notes)


if __name__ == '__main__':
    unittest.main(verbosity=2)
