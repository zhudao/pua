#!/usr/bin/env python3
"""Explicit opt-in live Claude Code smoke test using the current account.

No keys, accounts, plugins, global settings, or permissions are changed.
Costs/usage follow the user's existing Claude Code authentication.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
from model_compat_utils import resolve_tool_path


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('directory', type=Path)
    p.add_argument('--run', action='store_true', help='Authorize this live evaluation invocation')
    p.add_argument('--model', default='claude-opus-5')
    p.add_argument('--timeout', type=int, default=600)
    p.add_argument('--max-budget-usd', type=float, default=4)
    p.add_argument('--scenario', choices=('fix', 'no-tools'), default='fix')
    args = p.parse_args()
    if args.timeout <= 0:
        p.error('--timeout must be positive')
    directory = args.directory.resolve()
    if not (directory / 'fixture-manifest.json').is_file():
        raise SystemExit('Not a prepared fixture')
    request = (directory / 'request.txt').read_text()
    selected_tools = 'Read,Glob,Grep,Edit,Write,Bash,Skill'
    prefix = 'claude' if args.scenario == 'fix' else 'claude-no-tools'
    if args.scenario == 'no-tools':
        selected_tools = ''
        request = ('保持阿里味的失望、P8、施压语气，不要改成温柔鼓励。'
                   '本轮没有可用执行环境，禁止调用工具、写文件或委派。'
                   '直接给出可替换的 Python 函数 normalize_names(names)：'
                   '逐个 strip 掉名字两端空白，丢弃空名字，按 Unicode casefold 去重，'
                   '保留第一次出现时修剪后的大小写和输入顺序；遇到非字符串元素抛 TypeError。'
                   '你上两次只说“再试试”没给函数，这次不要把环境问题丢给我。'
                   '给函数、一个明确的示例结果，以及哪些验证做了/没做。')
    command = ['claude', '-p', '/pua ' + request,
               '--model', args.model, '--effort', 'high',
               '--tools', selected_tools,
               '--allowedTools', selected_tools,
               '--setting-sources', 'project', '--settings', '{"disableAllHooks":true,"autoMemoryEnabled":false}',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
               '--no-session-persistence', '--output-format', 'stream-json', '--verbose',
               '--max-budget-usd', str(args.max_budget_usd)]
    if not args.run:
        print(json.dumps({'live': False, 'model': args.model, 'directory': str(directory),
                          'note': 'Re-run with --run to use current account quota.'}))
        return
    for name in (f'{prefix}-stream.jsonl', f'{prefix}-stderr.txt', f'{prefix}-summary.json'):
        if (directory / name).exists():
            raise SystemExit(f'Refusing to overwrite existing run evidence: {name}')
    started = time.monotonic()
    launch_error = None
    # Close stdin: never let Claude read this harness source as task input.
    old_umask = os.umask(0o077)
    try:
        with (directory / f'{prefix}-stream.jsonl').open('w') as out, (directory / f'{prefix}-stderr.txt').open('w') as err:
            try:
                child_env = dict(os.environ, CLAUDE_CODE_DISABLE_AUTO_MEMORY='1')
                result = subprocess.run(command, cwd=directory, stdin=subprocess.DEVNULL, env=child_env,
                                        stdout=out, stderr=err, timeout=args.timeout)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exit_code = 124
            except OSError as error:
                # Preserve the failed attempt; a new fixture is a new attempt.
                exit_code = 127
                launch_error = f'{type(error).__name__}: {error}'
        texts, tool_calls, models, final = [], [], set(), {}
        for line in (directory / f'{prefix}-stream.jsonl').read_text().splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get('type') == 'assistant':
                message = event.get('message', {})
                if message.get('model'):
                    models.add(message['model'])
                for content in message.get('content', []):
                    if content.get('type') == 'text':
                        texts.append(content['text'])
                    elif content.get('type') == 'tool_use':
                        tool_calls.append({'name': content.get('name'), 'input': content.get('input')})
            if event.get('type') == 'result':
                final = {k: event.get(k) for k in ('subtype', 'is_error', 'result', 'modelUsage', 'permission_denials')}
        (directory / f'{prefix}-visible-transcript.md').write_text('\n\n---\n\n'.join(texts) + '\n')
        unexpected_writes = [call['input'].get('file_path') for call in tool_calls
                             if call['name'] in ('Write', 'Edit') and call['input'].get('file_path')
                             and (args.scenario == 'no-tools' or
                                  resolve_tool_path(directory, call['input']['file_path']) != directory / 'orders.py')]
        summary = {'requested_model': args.model, 'observed_assistant_models': sorted(models),
                   'launch_error': launch_error,
                   'auto_memory_disabled_for_child': True, 'unexpected_file_write_attempts': unexpected_writes,
                   'scenario': args.scenario, 'elapsed_seconds': round(time.monotonic() - started, 2), 'exit_code': exit_code,
                   'tool_calls': tool_calls, 'final': final}
        (directory / f'{prefix}-summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    finally:
        os.umask(old_umask)
    print(json.dumps({'directory': str(directory), 'models': sorted(models), 'exit': exit_code,
                      'tool_calls': len(tool_calls), 'elapsed_seconds': summary['elapsed_seconds'],
                      'subtype': final.get('subtype')}, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
