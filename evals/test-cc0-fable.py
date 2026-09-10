#!/usr/bin/env python3
"""Offline tests for model identity and real skill-invocation evidence."""
import json
from pathlib import Path
import tempfile
import unittest
import os
import subprocess
import sys
import hashlib
from cc0_fable_evidence import inspect_stream, loading_evidence


def inspect(events):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'stream.jsonl'
        path.write_text('\n'.join(json.dumps(event) for event in events))
        return inspect_stream(path)


class EvidenceTests(unittest.TestCase):
    def test_cc0_hook_probe_is_explicit_and_omits_personal_setting_sources(self):
        """Hook probes opt in per process; default runs keep hooks disabled."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'plugin').mkdir()
            (root / 'prompt.txt').write_text('Offline stub; no model is called.')
            capture = root / 'capture.py'
            capture.write_text('''import json, sys
from pathlib import Path
Path(__file__).with_name('argv.json').write_text(json.dumps(sys.argv[1:]))
print(json.dumps({'type':'assistant','message':{'model':'claude-fable-5','content':[]}}))
print(json.dumps({'type':'result','subtype':'success','is_error':False,'modelUsage':{'claude-fable-5':{}}}))
''')
            # Model the real wrapper's earlier setting-sources argument. No
            # real credentials, settings, provider, or plugins are accessed.
            (root / 'wrapper.zsh').write_text(
                f'cc0() {{ {sys.executable!r} {str(capture)!r} --setting-sources user "$@"; }}\n')
            for hooks_enabled in (False, True):
                with self.subTest(hooks_enabled=hooks_enabled):
                    output = root / f'run-{hooks_enabled}'
                    command = [sys.executable, str(Path(__file__).with_name('run-cc0-fable.py')),
                        '--cc0-definition', str(root / 'wrapper.zsh'), '--prompt-file', str(root / 'prompt.txt'),
                        '--plugin-dir', str(root / 'plugin'), '--run-dir', str(output), '--run']
                    if hooks_enabled:
                        command.append('--enable-plugin-hooks-for-test')
                    result = subprocess.run(command, stdin=subprocess.DEVNULL,
                                            capture_output=True, text=True, timeout=12)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads((root / 'argv.json').read_text())
                    settings = json.loads(argv[argv.index('--settings') + 1])
                    self.assertIs(settings['disableAllHooks'], not hooks_enabled)
                    self.assertIs(settings['autoMemoryEnabled'], False)
                    sources = [argv[i + 1] for i, arg in enumerate(argv) if arg == '--setting-sources']
                    self.assertEqual(sources, ['user', ''] if hooks_enabled else ['user'])
                    invocation = json.loads((output / 'invocation.json').read_text())
                    self.assertIs(invocation['external_hooks_disabled_for_child'], not hooks_enabled)
                    self.assertFalse(invocation['global_configuration_changed_by_runner'])

    def test_cc0_hook_probe_requires_an_explicit_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Validation must reject this before reading a wrapper or starting
            # a child process, including in dry-run mode.
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('run-cc0-fable.py')),
                '--cc0-definition', str(root / 'missing-wrapper.zsh'), '--prompt-file', str(root / 'missing-prompt.txt'),
                '--run-dir', str(root / 'attempt'), '--enable-plugin-hooks-for-test'],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn('requires an explicitly supplied test plugin', result.stderr)
            self.assertFalse((root / 'attempt').exists())

    def test_grading_preserves_runner_cwd_failure(self):
        """A correct function and Fable identity cannot hide a runner/cwd failure."""
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); run=root/'eval-2-fixture/with_skill'
            execution=run/'execution'; execution.mkdir(parents=True)
            task=run/'task'; task.mkdir()
            source='''def compress_runs(values):
    result=[]
    for value in values:
        if isinstance(value,bool) or not isinstance(value,int): raise TypeError()
        if result and result[-1][0]==value:
            old,count=result[-1]; result[-1]=(old,count+1)
        else: result.append((value,1))
    return result
'''
            visible='```python\n'+source+'```'
            (execution/'visible-transcript.md').write_text(visible)
            (run/'prompt.txt').write_text('Offline grading fixture; no model call.')
            events=[{'type':'system','subtype':'init','cwd':str(task)},
                {'type':'assistant','message':{'model':'claude-fable-5','content':[
                    {'type':'tool_use','id':'s1','name':'Skill','input':{'skill':'pua-check:pua'}},
                    {'type':'text','text':visible}]}},
                {'type':'user','message':{'content':[{'type':'tool_result','tool_use_id':'s1','content':'Loaded'}]}},
                {'type':'result','subtype':'success','is_error':False,'modelUsage':{'claude-fable-5':{}}}]
            (execution/'stream.jsonl').write_text('\n'.join(map(json.dumps,events)))
            (execution/'summary.json').write_text(json.dumps({'process_exit':0,'elapsed_seconds':1,
                'loading':{'passed':True,'checks':{}},'run_passed':False,'cwd_matches_expected':False}))
            (root/'trusted-manifest.json').write_text(json.dumps({'runs':[
                {'run':str(run),'task':str(task),'id':2,'name':'fixture','configuration':'with_skill','protected':{}}]}))
            (root/'manual.json').write_text(json.dumps({'2:with_skill':[]}))
            result=subprocess.run([sys.executable,str(Path(__file__).with_name('grade-fable-evals.py')),
                str(root),'--manual-review',str(root/'manual.json')],stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=25)
            self.assertEqual(result.returncode,0,result.stderr)
            grading=json.loads((run/'run-1/grading.json').read_text())
            self.assertFalse(grading['expectations'][0]['passed'])
            self.assertTrue(all(item['passed'] for item in grading['expectations'][1:]))
            self.assertGreater(grading['summary']['failed'],0)
            self.assertLess(json.loads((root/'grading-index.json').read_text())[0]['pass_rate'],1)

    def test_cc0_stops_on_refusal_without_following_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = json.dumps({'type':'system','subtype':'model_refusal_fallback',
                                'original_model':'claude-fable-5','fallback_model':'claude-opus-5'})
            (root / 'wrapper.zsh').write_text(f"cc0() {{ printf '%s\\n' '{event}'; /bin/sleep 30; }}\n")
            (root / 'prompt.txt').write_text('Offline stub; no model is called.')
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('run-cc0-fable.py')),
                '--cc0-definition',str(root/'wrapper.zsh'),'--prompt-file',str(root/'prompt.txt'),
                '--run-dir',str(root/'attempt'),'--run'], stdin=subprocess.DEVNULL,
                capture_output=True,text=True,timeout=12)
            self.assertEqual(result.returncode,125,result.stderr)
            summary=json.loads((root/'attempt/summary.json').read_text())
            self.assertEqual(summary['identity_abort'],'host_refusal')
            self.assertTrue(summary['fallback_observed'])
            self.assertFalse(summary['run_passed'])

    def test_system_refusal_fallback_is_not_lost(self):
        output = inspect([
            {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': []}},
            {'type': 'assistant', 'message': {'model': '<synthetic>', 'content': []}},
            {'type': 'system', 'subtype': 'model_refusal_fallback', 'trigger': 'refusal',
             'original_model': 'claude-fable-5', 'fallback_model': 'claude-opus-5'}])
        self.assertEqual(output['observed_assistant_models'], ['claude-fable-5'])
        self.assertEqual(output['synthetic_message_models'], ['<synthetic>'])
        self.assertTrue(output['fallback_observed'])
        self.assertFalse(output['exact_model_confirmed'])
        self.assertEqual(len(output['model_fallback_events']), 1)

    def test_cc0_timeout_preserves_failed_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'wrapper.zsh').write_text('cc0() { /bin/sleep 30; }\n')
            (root / 'prompt.txt').write_text('Offline stub; no model is called.')
            cmd = [sys.executable, str(Path(__file__).with_name('run-cc0-fable.py')),
                   '--cc0-definition', str(root / 'wrapper.zsh'), '--prompt-file', str(root / 'prompt.txt'),
                   '--run-dir', str(root / 'attempt'), '--timeout', '1', '--run']
            result = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=12)
            self.assertEqual(result.returncode, 124, result.stderr)
            summary = json.loads((root / 'attempt/summary.json').read_text())
            self.assertTrue(summary['timed_out'])
            self.assertFalse(summary['run_passed'])
            repeat = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=5)
            self.assertNotEqual(repeat.returncode, 0)
            self.assertIn('FileExistsError', repeat.stderr)

    def test_source_bound_loading_and_cross_result_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'skills/pua/SKILL.md'
            source.parent.mkdir(parents=True)
            source.write_text('PUA-RUNTIME-CONTRACT:START')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            events = [
                {'type': 'system', 'subtype': 'init', 'cwd': directory,
                 'plugins': [{'name': 'pua-check', 'path': directory}]},
                {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': [
                    {'type': 'tool_use', 'id': 's1', 'name': 'Skill', 'input': {'skill': 'pua-check:pua'}},
                    {'type': 'tool_use', 'id': 'r1', 'name': 'Read', 'input': {'file_path': str(source)}}]}},
                {'type': 'user', 'message': {'content': [
                    {'type': 'tool_result', 'tool_use_id': 's1', 'content': 'Launching skill'},
                    {'type': 'tool_result', 'tool_use_id': 'r1', 'content': source.read_text()}]}},
                {'type': 'result', 'subtype': 'success', 'is_error': False,
                 'modelUsage': {'claude-fable-5': {'inputTokens': 20}}}]
            evidence = inspect(events)
            self.assertTrue(loading_evidence(evidence, 'pua-check:pua', source, digest, True)['passed'])
            # The other global pua invocation plus a local Read is not this plugin.
            events[1]['message']['content'][0]['input']['skill'] = 'pua'
            self.assertFalse(loading_evidence(inspect(events), 'pua-check:pua', source, digest, True)['passed'])
            events[1]['message']['content'][0]['input']['skill'] = 'pua-check:pua'
            events[2]['message']['content'][0]['content'] = source.read_text()
            events[2]['message']['content'][1]['content'] = 'different source without marker'
            self.assertFalse(loading_evidence(inspect(events), 'pua-check:pua', source, digest, True)['passed'])
            source.write_text('changed after run')
            self.assertFalse(loading_evidence(evidence, 'pua-check:pua', source, digest, True)['passed'])

    def test_auxiliary_usage_is_disclosed_not_primary_fallback(self):
        output = inspect([
            {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': []}},
            {'type': 'result', 'subtype': 'success', 'is_error': False,
             'modelUsage': {'claude-fable-5': {}, 'claude-haiku-4-5': {}}}])
        self.assertTrue(output['exact_model_confirmed'])
        self.assertTrue(output['expected_model_in_usage'])
        self.assertEqual(output['auxiliary_usage_models'], ['claude-haiku-4-5'])
        self.assertFalse(inspect([{'type': 'result', 'modelUsage': {'claude-opus-5': {}}}])['expected_model_in_usage'])

    def test_is_error_success_is_failure(self):
        self.assertFalse(inspect([{'type': 'result', 'subtype': 'success', 'is_error': True}])['terminal_success'])

    def test_injected_skill_words_are_not_visible_behavior(self):
        events = [
            {'type': 'system', 'subtype': 'init', 'skills': ['pua:pua']},
            {'type': 'user', 'message': {'content': [{'type': 'text', 'text': 'PUA生效 底层逻辑'}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'r1',
                'content': 'SKILL SOURCE: PUA生效 底层逻辑'}]}},
            {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': [
                {'type': 'text', 'text': 'An ordinary response.'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'stream.jsonl'
            path.write_text('\n'.join(json.dumps(event) for event in events))
            inspector = Path(__file__).with_name('inspect-claude-evidence.py')
            for arguments in (['--contains', 'PUA生效|底层逻辑'], ['--skill', 'pua']):
                result = subprocess.run([sys.executable, str(inspector), str(path), *arguments],
                                        stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 1)

    def test_trigger_runner_does_not_ignore_process_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            binary = Path(directory) / 'claude'
            binary.write_text('#!/bin/sh\nprintf \'%s\\n\' \'{"type":"result","subtype":"success","is_error":false}\'\nexit 7\n')
            binary.chmod(0o700)
            root = Path(__file__).resolve().parents[1]
            result = subprocess.run(['bash', str(root / 'evals/run-trigger-test.sh'), '--plugin-dir', str(root)],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
                                    env=dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH'], TMPDIR=directory))
            self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
            self.assertIn('Passed: 0', result.stdout)
            self.assertIn('Failed: 11', result.stdout)

    def test_discovery_is_not_loading_or_model_proof(self):
        output = inspect([{'type': 'system', 'subtype': 'init', 'model': 'claude-fable-5',
                           'skills': ['pua:pua']}])
        self.assertEqual(output['discovered_skills'], ['pua:pua'])
        self.assertFalse(output['exact_model_confirmed'])
        self.assertEqual(output['successful_skill_invocations'], [])

    def test_fallback_model_is_not_fable(self):
        output = inspect([{'type': 'assistant', 'message': {'model': 'claude-opus-5',
                            'content': [{'type': 'text', 'text': 'I am Fable-5.'}]}}])
        self.assertFalse(output['exact_model_confirmed'])

    def test_successful_invocation_needs_tool_result(self):
        use = {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': [
            {'type': 'tool_use', 'id': 's1', 'name': 'Skill', 'input': {'skill': 'pua:pua'}}]}}
        self.assertEqual(inspect([use])['successful_skill_invocations'], [])
        result = {'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'tool_use_id': 's1', 'content': 'PUA-RUNTIME-CONTRACT:START'}]}}
        output = inspect([use, result])
        self.assertTrue(output['exact_model_confirmed'])
        self.assertEqual(output['successful_skill_invocations'], ['pua:pua'])
        self.assertTrue(output['runtime_core_observed_in_tool_result'])

    def test_failed_skill_call_is_not_loaded(self):
        output = inspect([
            {'type': 'assistant', 'message': {'model': 'claude-fable-5', 'content': [
                {'type': 'tool_use', 'id': 's1', 'name': 'Skill', 'input': {'skill': 'pua:pua'}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 's1',
                                                     'is_error': True, 'content': 'Unknown skill'}]}}])
        self.assertEqual(output['successful_skill_invocations'], [])

    def test_usage_fallback_invalidates_identity(self):
        output = inspect([{'type': 'assistant', 'message': {'model': 'claude-fable-5',
                           'usage': {'iterations': [{'type': 'fallback_message'}]}, 'content': []}}])
        self.assertTrue(output['fallback_observed'])
        self.assertFalse(output['exact_model_confirmed'])

    def test_terminal_success_is_not_inferred_from_text(self):
        output = inspect([{'type': 'assistant', 'message': {'model': 'claude-fable-5',
                           'content': [{'type': 'text', 'text': 'All done, success.'}]}}])
        self.assertFalse(output['terminal_success'])
        output = inspect([{'type': 'result', 'subtype': 'error_max_budget_usd', 'is_error': False}])
        self.assertFalse(output['terminal_success'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
