#!/usr/bin/env python3
"""Feedback is a voluntary user notice, not a model-facing Stop blocker."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class FeedbackTests(unittest.TestCase):
    def run_hook(self, config=None, event=None, source_only=False, counter=None):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            local = home / '.pua'
            local.mkdir()
            cfg = local / 'config.json'
            cfg.write_text(json.dumps(config or {'always_on': True, 'feedback_frequency': 1}))
            transcript = home / 'transcript.jsonl'
            transcript.write_text(json.dumps({'type': 'user' if source_only else 'assistant',
                'message': {'content': [{'type': 'tool_result' if source_only else 'text',
                    'text': '[PUA-DIAGNOSIS] real evidence', 'content': 'PUA生效'}]}}))
            if counter is not None:
                (local / '.stop_counter').write_text(counter)
            payload = {'hook_event_name': 'Stop', 'transcript_path': str(transcript), **(event or {})}
            result = subprocess.run(['bash', str(ROOT / 'hooks/stop-feedback.sh')],
                input=json.dumps(payload), capture_output=True, text=True, timeout=10,
                env=dict(os.environ, HOME=str(home), PUA_CONFIG=str(cfg)))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((local / 'feedback.jsonl').exists(), 'hook must not fabricate a user rating')
            return result.stdout

    def test_notice_is_valid_and_non_blocking(self):
        output = json.loads(self.run_hook())
        self.assertEqual(set(output), {'systemMessage'})
        self.assertIn('/pua:survey quick', output['systemMessage'])
        self.assertIn('跳过不记录', output['systemMessage'])

    def test_source_keywords_do_not_count_as_usage(self):
        self.assertEqual(self.run_hook(source_only=True), '')

    def test_suppression_gates(self):
        for config in ({'offline': True}, {'always_on': False}, {'feedback_frequency': 0}):
            with self.subTest(config=config):
                self.assertEqual(self.run_hook(config), '')
        for event in ({'hook_event_name': 'SubagentStop'}, {'parent_session_id': 'parent'},
                      {'stop_hook_active': True}):
            with self.subTest(event=event):
                self.assertEqual(self.run_hook(event=event), '')

    def test_corrupt_counter_does_not_execute_or_crash(self):
        self.assertIn('systemMessage', self.run_hook(counter='$(false); not a number'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
