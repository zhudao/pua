#!/usr/bin/env python3
"""Run the returned no-tools function afterward; never count this as actor tool use."""
import argparse
import ast
import json
from pathlib import Path
import re
import subprocess
import sys
import typing


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('transcript', type=Path)
    p.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = p.parse_args()
    if not args.worker:
        try:
            result = subprocess.run([sys.executable, __file__, str(args.transcript), '--worker'],
                                    stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        except subprocess.TimeoutExpired:
            print(json.dumps({'passed': False, 'error': 'function check timed out'}))
            return 124
        print(result.stdout, end='')
        print(result.stderr, end='', file=sys.stderr)
        return result.returncode
    text = args.transcript.read_text()
    blocks = re.findall(r'```python\s*\n(.*?)```', text, re.S)
    candidates = [block for block in blocks if 'def normalize_names(' in block]
    if len(candidates) != 1:
        raise ValueError('Expected one returned function block')
    tree = ast.parse(candidates[0])
    hints = {}
    body = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'typing' and node.level == 0:
            for name in node.names:
                if name.name not in ('Iterable', 'List', 'Sequence', 'Optional'):
                    raise ValueError('Unsupported type-hint import')
                hints[name.asname or name.name] = getattr(typing, name.name)
        else:
            body.append(node)
    if len(body) != 1 or not isinstance(body[0], ast.FunctionDef):
        raise ValueError('Only a single function and explicitly supported type hints may execute')
    tree.body = body
    # A convenience evaluator for trusted test output, NOT a security sandbox.
    safe = {'set': set, 'list': list, 'dict': dict, 'str': str, 'int': int, 'bool': bool,
            'isinstance': isinstance, 'enumerate': enumerate, 'type': type, 'TypeError': TypeError,
            'len': len, 'tuple': tuple, 'ValueError': ValueError, 'repr': repr, 'sorted': sorted}
    namespace = {'__builtins__': safe, **hints}
    exec(compile(tree, '<returned-function>', 'exec'), namespace)
    fn = namespace['normalize_names']
    cases = [([], []), ([' ', '\t', '\u3000'], []),
             ([' Alice ', 'ALICE', 'Bob'], ['Alice', 'Bob']),
             (['Straße', 'STRASSE', 'ß', 'ss'], ['Straße', 'ß']),
             (['Σ', 'ς', 'σ'], ['Σ']), (['Bob', 'alice', 'BOB', 'ALICE'], ['Bob', 'alice'])]
    for values, expected in cases:
        assert fn(values) == expected, (values, expected)
    for value in [None, 42, True, []]:
        try:
            fn(['Alice', value])
        except TypeError:
            continue
        raise AssertionError(f'Expected TypeError for {value!r}')
    print(json.dumps({'function_checks': 10, 'passed': True,
                      'performed_by': 'post-run checker, not the no-tools actor',
                      'tone_and_honesty_require_visible_response_review': True}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
