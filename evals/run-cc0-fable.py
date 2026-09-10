#!/usr/bin/env python3
"""Run the user's explicitly supplied cc0 definition; preserve exact-model evidence.

This runner neither installs plugins nor changes global Claude settings. The cc0
definition is user-controlled and may itself contain wrapper behavior. Tool and
prompt restrictions here are not an operating-system sandbox.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import contextlib
import subprocess
import time
from cc0_fable_evidence import inspect_stream, loading_evidence


def stop_group(process):
    """Best-effort cleanup of this runner's owned process group only."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait()


def await_exact_model(process, stream, model, timeout):
    """Abort observable refusal/substitution rather than continue via fallback.

    The host may already have initiated a fallback before emitting its event.
    This monitor is not a provider-side switch or a permission boundary.
    """
    deadline, pending = time.monotonic() + timeout, ''
    with stream.open() as reader:
        while True:
            pending += reader.read()
            lines = pending.split('\n')
            pending = lines.pop()
            for line in lines:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                refusal = event.get('type') == 'system' and str(event.get('subtype', '')).startswith('model_refusal_')
                actual = event.get('message', {}).get('model') if event.get('type') == 'assistant' else None
                mismatch = isinstance(actual, str) and not actual.startswith('<') and actual != model
                if refusal or mismatch:
                    stop_group(process)
                    return 125, False, 'host_refusal' if refusal else f'unexpected_model:{actual}'
            if process.poll() is not None:
                return process.returncode, False, None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stop_group(process)
                return 124, True, None
            try:
                process.wait(timeout=min(0.25, remaining))
            except subprocess.TimeoutExpired:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cc0-definition', type=Path, required=True)
    parser.add_argument('--prompt-file', type=Path, required=True)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--plugin-dir', type=Path, action='append', default=[])
    parser.add_argument('--model', default='claude-fable-5')
    parser.add_argument('--tools', default='Read,Glob,Grep,Edit,Write,Bash,Skill')
    parser.add_argument('--effort', default='high')
    parser.add_argument('--timeout', type=int, default=600)
    parser.add_argument('--max-budget-usd', type=float, default=4)
    parser.add_argument('--require-skill', help='Fully qualified native skill, e.g. pua-check:pua')
    parser.add_argument('--require-read-file', type=Path, help='Exact source inside the matching plugin')
    parser.add_argument('--require-runtime-marker', action='store_true')
    parser.add_argument('--expected-cwd', type=Path, help='Verify wrapper-selected init cwd; does not override the wrapper')
    parser.add_argument('--enable-plugin-hooks-for-test', action='store_true',
                        help='Enable hooks only for a deliberately isolated plugin test; omit user/project/local settings. Hook I/O is NOT sandboxed.')
    parser.add_argument('--run', action='store_true')
    args = parser.parse_args()
    if args.timeout <= 0 or args.max_budget_usd <= 0:
        parser.error('timeout and max-budget-usd must be positive')
    if bool(args.require_skill) != bool(args.require_read_file):
        parser.error('--require-skill and --require-read-file are required together')
    if args.require_runtime_marker and not args.require_skill:
        parser.error('--require-runtime-marker needs a required skill/source')
    if args.enable_plugin_hooks_for_test and not args.plugin_dir:
        parser.error('--enable-plugin-hooks-for-test requires an explicitly supplied test plugin')
    definition = args.cc0_definition.resolve(strict=True)
    prompt = args.prompt_file.read_text()
    plugins = [path.resolve(strict=True) for path in args.plugin_dir]
    source = args.require_read_file.resolve(strict=True) if args.require_read_file else None
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest() if source else None
    invocation = {'requested_model': args.model, 'tools': args.tools, 'effort': args.effort,
                  'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'evidence_parser_sha256': hashlib.sha256(Path(__file__).with_name('cc0_fable_evidence.py').read_bytes()).hexdigest(),
                  'cc0_definition_sha256': hashlib.sha256(definition.read_bytes()).hexdigest(),
                  'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(),
                  'plugin_dirs': list(map(str, plugins)), 'timeout_seconds': args.timeout,
                  'max_budget_usd': args.max_budget_usd,
                  'native_auto_memory_disabled_for_child': True,
                  'external_hooks_disabled_for_child': not args.enable_plugin_hooks_for_test,
                  'unmanaged_setting_sources_for_child': '' if args.enable_plugin_hooks_for_test else 'wrapper default',
                  'global_configuration_changed_by_runner': False}
    invocation['loading_requirement'] = {'skill': args.require_skill, 'source': str(source) if source else None,
                                         'source_sha256': source_sha, 'runtime_marker': args.require_runtime_marker}
    invocation['expected_actual_cwd'] = str(args.expected_cwd.resolve(strict=True)) if args.expected_cwd else None
    if not args.run:
        print(json.dumps({'live': False, **invocation}, ensure_ascii=False))
        return 0
    destination = args.run_dir.absolute()
    # New evidence directories only; never overwrite an earlier attempt.
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    command = ['zsh', '-f', '-c', 'source "$1"; shift; cc0 "$@"', 'cc0-runner', str(definition),
               '-p', prompt, '--model', args.model, '--effort', args.effort,
               '--tools', args.tools, '--allowedTools', args.tools,
               '--settings', json.dumps({'disableAllHooks':not args.enable_plugin_hooks_for_test,'autoMemoryEnabled':False}),
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
               '--max-budget-usd', str(args.max_budget_usd)]
    if args.enable_plugin_hooks_for_test:
        # Lists of hooks merge across settings sources. An empty `hooks` object
        # would not erase a user's existing hooks; omit non-managed settings
        # sources for this explicitly requested test instead. Managed policy
        # still applies. Callers must independently isolate plugin hook writes.
        command += ['--setting-sources', '']
    for plugin in plugins:
        command += ['--plugin-dir', str(plugin)]
    started = time.monotonic()
    old_umask = os.umask(0o077)
    try:
        (destination / 'prompt.txt').write_text(prompt)
        (destination / 'invocation.json').write_text(json.dumps(invocation, ensure_ascii=False, indent=2) + '\n')
        timed_out, launch_error, identity_abort, code = False, None, None, 127
        with (destination / 'stream.jsonl').open('x') as out, (destination / 'stderr.txt').open('x') as err:
            try:
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                           start_new_session=True,
                                           env=dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY='1'))
                code, timed_out, identity_abort = await_exact_model(
                    process, destination / 'stream.jsonl', args.model, args.timeout)
            except OSError as error:
                launch_error = f'{type(error).__name__}: {error}'
        summary = inspect_stream(destination / 'stream.jsonl', args.model)
        visible = summary.pop('visible_text')
        summary.update({'process_exit': code, 'timed_out': timed_out,
                        'launch_error': launch_error,
                        'identity_abort': identity_abort,
                        'elapsed_seconds': round(time.monotonic() - started, 2),
                        'raw_stream_sha256': hashlib.sha256((destination / 'stream.jsonl').read_bytes()).hexdigest()})
        # This is transport/model identity, not the task's behavioral verdict.
        summary['invocation_passed'] = (code == 0 and summary['terminal_success']
                                        and summary['exact_model_confirmed'] and summary['expected_model_in_usage'])
        summary['loading'] = loading_evidence(summary, args.require_skill, source, source_sha,
                                              args.require_runtime_marker) if source else None
        summary['cwd_matches_expected'] = (bool(summary['actual_cwd']) and
            str(Path(summary['actual_cwd']).resolve()) == invocation['expected_actual_cwd']) if args.expected_cwd else None
        summary['run_passed'] = (summary['invocation_passed'] and
            (summary['loading'] is None or summary['loading']['passed']) and summary['cwd_matches_expected'] is not False)
        (destination / 'visible-transcript.md').write_text(visible + '\n')
        (destination / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    finally:
        os.umask(old_umask)
    print(json.dumps({key: summary[key] for key in
                      ('run_passed', 'invocation_passed', 'loading', 'observed_assistant_models', 'successful_skill_invocations',
                       'tool_call_count', 'elapsed_seconds', 'process_exit')}, ensure_ascii=False))
    return 0 if summary['run_passed'] else (code or 1)


if __name__ == '__main__':
    raise SystemExit(main())
