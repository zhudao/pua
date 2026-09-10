#!/usr/bin/env python3
"""Offline artifact tests, NOT evidence of model effectiveness."""
import hashlib
import json
import os
import runpy
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile
import yaml
from model_compat_utils import resolve_tool_path

ROOT = Path(__file__).resolve().parents[1]
BASE = 'ac5026791845b730a18eb4ff07512a3b6f2f06f5'


def original(path):
    return subprocess.check_output(['git', 'show', f'{BASE}:{path}'], cwd=ROOT, text=True)


class ModelCompatArtifacts(unittest.TestCase):
    def test_no_tools_trace_writes_are_audited(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            call = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Write',
                    'input': {'file_path': 'orders.py'}}]}}
            (directory / 'claude-no-tools-stream.jsonl').write_text(json.dumps(call))
            audit = runpy.run_path(str(ROOT / 'evals/check-model-compat-fix.py'))['trace_write_deviations']
            self.assertEqual(audit(directory), ['orders.py'])

    def test_launch_failure_has_durable_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / 'fixture-manifest.json').write_text('{}')
            (directory / 'request.txt').write_text('Offline launch failure test, no model.')
            command = [sys.executable, str(ROOT / 'evals/run-claude-model-compat.py'), str(directory), '--run']
            result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    env=dict(os.environ, PATH=tmp), timeout=10)
            self.assertEqual(result.returncode, 127, result.stderr)
            summary = json.loads((directory / 'claude-summary.json').read_text())
            self.assertIn('FileNotFoundError', summary['launch_error'])
            repeat = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                    env=dict(os.environ, PATH=tmp), timeout=10)
            self.assertIn('Refusing to overwrite', repeat.stderr)

    def test_text_checker_accepts_benign_builtins(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / 'reply.md'
            transcript.write_text('''```python
def normalize_names(names):
    result, seen = [], set()
    for name in names:
        if not isinstance(name, str):
            raise TypeError(repr(name))
        value = name.strip()
        if len(value) and value.casefold() not in seen:
            seen.add(value.casefold())
            result.append(value)
    return result
```
''')
            result = subprocess.run([sys.executable, str(ROOT / 'evals/check-model-compat-text.py'), str(transcript)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_tool_paths_use_actor_working_directory(self):
        directory = Path('/tmp/model-compat-actor').resolve()
        self.assertEqual(resolve_tool_path(directory, 'orders.py'), directory / 'orders.py')
        self.assertEqual(resolve_tool_path(directory, './orders.py'), directory / 'orders.py')
        self.assertEqual(resolve_tool_path(directory, str(directory / 'orders.py')), directory / 'orders.py')
        self.assertNotEqual(resolve_tool_path(directory, '../orders.py'), directory / 'orders.py')

    def test_checker_missing_external_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            run = subprocess.run([sys.executable, str(ROOT / 'evals/check-model-compat-fix.py'),
                                  str(directory), '--trusted-manifest', str(directory / 'missing.json')],
                                 stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
            summary = json.loads(run.stdout)
            self.assertEqual(run.returncode, 2)
            self.assertFalse(summary['passed'])
            self.assertEqual(summary['scope_validation'], 'not_verified')

    def test_checker_legacy_is_partial_and_relative_write_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'actor'
            directory.mkdir()
            (directory / 'orders.py').write_text('''import csv, io
from decimal import Decimal, ROUND_HALF_UP, localcontext, InvalidOperation
def load_orders(text):
    result = []
    for row in csv.DictReader(io.StringIO(text.lstrip('\\ufeff'), newline='')):
        try:
            amount = Decimal(row['price'])
            if not amount.is_finite():
                raise ValueError('non-finite')
            with localcontext() as context:
                context.prec = max(28, len(amount.as_tuple().digits) + abs(amount.as_tuple().exponent) + 5)
                cents = int((amount * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
        except InvalidOperation as error:
            raise ValueError('invalid amount') from error
        result.append(dict(sku=row['sku'], note=row['note'], cents=cents, quantity=int(row['quantity'])))
    return result
''')
            (directory / 'tests_public.py').write_text(
                'import unittest\nfrom orders import load_orders\n'
                'class PublicTest(unittest.TestCase):\n'
                '    def test_empty(self):\n        self.assertEqual(load_orders(""), [])\n')
            hashes = {'tests_public.py': hashlib.sha256((directory / 'tests_public.py').read_bytes()).hexdigest()}
            manifest = {'fixed_files': hashes, 'protected_files': hashes}
            (directory / 'fixture-manifest.json').write_text(json.dumps(manifest))
            (directory / 'claude-stream.jsonl').write_text(json.dumps({
                'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'name': 'Write',
                                                             'input': {'file_path': 'orders.py'}}]}}) + '\n')
            trusted = Path(tmp) / 'external.json'
            command = [sys.executable, str(ROOT / 'evals/check-model-compat-fix.py'),
                       str(directory), '--trusted-manifest', str(trusted)]
            legacy = subprocess.run(command + ['--legacy-manifest'], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=40)
            partial = json.loads(legacy.stdout)
            self.assertTrue(partial['artifact_contract_passed'], legacy.stderr + legacy.stdout)
            self.assertFalse(partial['passed'])
            self.assertEqual(partial['scope_validation'], 'legacy_partial_only')
            trusted.write_text(json.dumps(manifest))
            run = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=40)
            summary = json.loads(run.stdout)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertTrue(summary['passed'])
            self.assertEqual(summary['unexpected_file_write_attempts'], [])

    def test_metadata_parse(self):
        for folder in ('skills/pua', 'codex/pua', 'chatgpt/pua'):
            source = (ROOT / folder / 'SKILL.md').read_text()
            self.assertTrue(source.startswith('---\n'))
            metadata = yaml.safe_load(source.split('---', 2)[1])
            self.assertEqual(metadata['name'], 'pua')
            self.assertIsInstance(metadata['description'], str)
            self.assertTrue(metadata['description'].strip())
            self.assertNotIn('allowed-tools', metadata)

    def test_original_quoted_tone_not_removed(self):
        for path in ('skills/pua/SKILL.md', 'codex/pua/SKILL.md'):
            current = (ROOT / path).read_text()
            quotes = [s for s in original(path).splitlines() if s.startswith('>')]
            self.assertGreater(len(quotes), 0)
            for line in quotes:
                self.assertIn(line, current, (path, line))

    def test_original_codex_pressure_dialogue_intact(self):
        path = 'codex/pua/SKILL.md'
        current = (ROOT / path).read_text()
        for line in original(path).splitlines():
            if line.startswith('| 第 ') and '**L' in line:
                self.assertIn(line, current)

    def test_flavor_library_byte_identical(self):
        path = 'skills/pua/references/flavors.md'
        self.assertEqual(subprocess.check_output(['git', 'show', f'{BASE}:{path}'], cwd=ROOT),
                         (ROOT / path).read_bytes())

    def test_core_early_and_identical(self):
        core = (ROOT / 'compat/runtime-core.md').read_text().strip()
        for path in ('skills/pua/SKILL.md', 'codex/pua/SKILL.md', 'chatgpt/pua/SKILL.md'):
            text = (ROOT / path).read_text()
            self.assertEqual(text.count(core), 1)
            # Character placement, not a claim about model tokenizer behavior.
            self.assertLess(text.index(core), 1800)
            self.assertEqual(text.count('PUA-RUNTIME-CONTRACT:START'), 1)
            self.assertEqual(text.count('PUA-RUNTIME-CONTRACT:END'), 1)

    def test_contract_copies_current(self):
        source = (ROOT / 'compat/runtime-contract.md').read_bytes()
        for folder in ('skills/pua', 'codex/pua', 'chatgpt/pua'):
            self.assertEqual(source, (ROOT / folder / 'references/runtime-contract.md').read_bytes())

    def test_archives_match_actual_files_and_have_safe_members(self):
        manifest = json.loads((ROOT / 'dist/manifest.json').read_text())
        sources = {'pua-chatgpt.zip': 'chatgpt/pua', 'pua-claude-code.zip': 'skills/pua',
                   'pua-codex.zip': 'codex/pua'}
        for name, entry in manifest.items():
            path = ROOT / 'dist' / name
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry['sha256'])
            with zipfile.ZipFile(path) as archive:
                self.assertIsNone(archive.testzip())
                members = archive.namelist()
                self.assertEqual(len(members), len(set(members)))
                self.assertIn('pua/SKILL.md', members)
                self.assertEqual(set(members), set(entry['members']))
                for member in members:
                    self.assertFalse(member.startswith('/'))
                    self.assertNotIn('..', Path(member).parts)
                    self.assertTrue(member.startswith('pua/'))
                    content = archive.read(member)
                    self.assertEqual(hashlib.sha256(content).hexdigest(), entry['members'][member])
                    relative = Path(member).relative_to('pua')
                    self.assertEqual(content, (ROOT / sources[name] / relative).read_bytes())

    def test_paste_contains_portable_body_and_contract(self):
        paste = (ROOT / 'chatgpt/PUA-Paste.md').read_text()
        _, _, body = (ROOT / 'chatgpt/pua/SKILL.md').read_text().split('---', 2)
        self.assertIn(body.strip(), paste)
        self.assertIn((ROOT / 'compat/runtime-contract.md').read_text(), paste)

    def test_builder_idempotent(self):
        names = ('skills/pua/SKILL.md', 'codex/pua/SKILL.md', 'chatgpt/pua/SKILL.md',
                 'chatgpt/PUA-Paste.md', 'dist/manifest.json', 'dist/pua-chatgpt.zip',
                 'dist/pua-claude-code.zip', 'dist/pua-codex.zip')
        before = {name: (ROOT / name).read_bytes() for name in names}
        subprocess.run(['python3', 'scripts/build-model-compat.py'], cwd=ROOT,
                       stdin=subprocess.DEVNULL, check=True, capture_output=True)
        self.assertEqual(before, {name: (ROOT / name).read_bytes() for name in names})


if __name__ == '__main__':
    unittest.main(verbosity=2)
