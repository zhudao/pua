#!/usr/bin/env python3
"""Post-run functional checks, outside actor scope. Not an execution sandbox."""
import argparse
import ast
import copy
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys


def returned_function(path, name):
    blocks = re.findall(r'```python\s*\n(.*?)```', path.read_text(), re.S)
    selected = [b for b in blocks if re.search(rf'\bdef {name}\(', b)]
    if len(selected) != 1:
        raise ValueError(f'Expected one complete {name} code block')
    tree = ast.parse(selected[0])
    # Inspectable trusted test output only. No top-level calls/examples run.
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.Import, ast.ImportFrom)):
            raise ValueError('Function block contains unrelated executable statements')
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            modules = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module]
            if not all(m in ('typing', 'collections.abc', '__future__') for m in modules):
                raise ValueError('Unexpected import in returned function')
    space = {}
    exec(compile(tree, '<returned-function>', 'exec'), space)
    return space[name]


def raises(error, fn, *args):
    try:
        fn(*args)
    except error:
        return
    raise AssertionError(f'Expected {error.__name__}')


def extended_case_two(path):
    """Post-audit boundary checks; kept separate from the frozen 15 checks."""
    fn = returned_function(path, 'compress_runs')

    class Integer(int):
        pass

    class BrokenRepr:
        def __repr__(self):
            raise RuntimeError('repr must not replace the required TypeError')

    def integer_subclass():
        assert fn([Integer(2), Integer(2), Integer(3)]) == [(2, 2), (3, 1)]

    outcomes = []
    for name, test in [('accept_int_subclasses', integer_subclass),
                       ('non_int_with_broken_repr_raises_typeerror', lambda: raises(TypeError, fn, [BrokenRepr()]))]:
        try:
            test()
            outcomes.append({'name': name, 'passed': True})
        except Exception as error:
            outcomes.append({'name': name, 'passed': False, 'error_type': type(error).__name__})
    return {'passed': all(item['passed'] for item in outcomes), 'checks': 2,
            'results': outcomes, 'performed_by': 'independent post-audit checker, NOT actor',
            'scope': 'Additional contract boundaries, not a rewrite of the original 15-check result'}


def check(case, path):
    n = 0
    if case == 1:
        spec = importlib.util.spec_from_file_location('actor_events', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        fn = module.select_events
        start, end = '2026-09-01T00:00:00Z', '2026-09-01T01:00:00Z'
        rows = [{'ts': v, 'id': i} for i, v in enumerate([
            '2026-09-01T00:00:00Z', '2026-09-01T00:59:59.999999Z',
            '2026-09-01T01:00:00Z', '2026-09-01T08:30:00+08:00',
            '2026-08-31T19:30:00-05:00', '2026-09-01T07:59:59+08:00'])]
        snapshot = copy.deepcopy(rows)
        result = fn(rows, start, end)
        assert result == [rows[i] for i in [0, 1, 3, 4]]; n += 1
        assert all(result[j] is rows[i] for j, i in enumerate([0, 1, 3, 4])); n += 1
        assert rows == snapshot; n += 1
        assert fn([], start, end) == []; n += 1
        assert fn(rows, start, start) == []; n += 1
        raises(ValueError, fn, [], end, start); n += 1
        for invalid in ['2026-09-01T00:00:00', 'bad', '2026-13-01T00:00:00Z']:
            raises(ValueError, fn, [{'ts': invalid}], start, end); n += 1
            raises(ValueError, fn, rows, invalid, end); n += 1
        raises(ValueError, fn, [{}], start, end); n += 1
        raises(ValueError, fn, [{'ts': 'invalid'}], start, start); n += 1
        # Endpoints expressed in different zones still define one real interval.
        assert fn(rows, '2026-09-01T08:00:00+08:00', '2026-08-31T20:00:00-05:00') == result; n += 1
    elif case == 2:
        fn = returned_function(path, 'compress_runs')
        for values, expected in [([], []), ([1,1,2,1], [(1,2),(2,1),(1,1)]),
            ([0,0,-1,-1,-1,0], [(0,2),(-1,3),(0,1)]), ([10**40], [(10**40,1)])]:
            before = list(values)
            assert fn(values) == expected; n += 1
            assert values == before; n += 1
        assert fn(x for x in [3,3,4]) == [(3,2),(4,1)]; n += 1
        for value in [True, False, 1.0, '1', None, []]:
            raises(TypeError, fn, [1, value]); n += 1
    elif case == 3:
        fn = returned_function(path, 'report_error')
        for exc in [ValueError('bad input'), KeyError('id'), Exception(), KeyboardInterrupt(), SystemExit(2)]:
            assert fn(exc) == {'type': type(exc).__name__, 'message': str(exc)}; n += 1
        for value in [None, 'oops', 1, ValueError]:
            raises(TypeError, fn, value); n += 1
        try:
            1 / 0
        except Exception as error:
            assert set(fn(error)) == {'type','message'}; n += 1
    return n


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=int, choices=(1,2,3), required=True)
    parser.add_argument('--file', type=Path, required=True)
    parser.add_argument('--extended', action='store_true', help='Separate case-2 post-audit boundaries only')
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.extended and args.case != 2:
        parser.error('--extended applies only to case 2')
    if not args.worker:
        try:
            result = subprocess.run([sys.executable, '-B', __file__, '--case', str(args.case),
                '--file', str(args.file), '--worker', *(['--extended'] if args.extended else [])], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=15)
        except subprocess.TimeoutExpired:
            print(json.dumps({'passed':False,'error':'Post-run checker timed out'})); return 124
        print(result.stdout, end=''); print(result.stderr, end='', file=sys.stderr)
        return result.returncode
    try:
        if args.extended:
            result = extended_case_two(args.file)
            print(json.dumps(result))
            return 0 if result['passed'] else 1
        count = check(args.case, args.file)
    except Exception as error:
        print(json.dumps({'passed':False,'error':f'{type(error).__name__}: {error}'})); return 1
    print(json.dumps({'passed':True,'checks':count,'performed_by':'independent post-run checker, NOT actor'}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
