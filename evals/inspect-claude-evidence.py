#!/usr/bin/env python3
"""Assertions over actual visible output/tool results, never injected source text."""
import argparse
from pathlib import Path
import re
from cc0_fable_evidence import inspect_stream


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stream', type=Path)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--skill')
    group.add_argument('--contains')
    group.add_argument('--count')
    group.add_argument('--terminal-success', action='store_true')
    args = parser.parse_args()
    evidence = inspect_stream(args.stream)
    if args.count is not None:
        print(len(re.findall(args.count, evidence['visible_text'])))
        return 0
    if not evidence['terminal_success']:
        return 1
    if args.terminal_success:
        return 0
    if args.skill is not None:
        aliases = {args.skill}
        if args.skill == 'pua':
            aliases.add('pua:pua')
        return 0 if aliases.intersection(evidence['successful_skill_invocations']) else 1
    return 0 if re.search(args.contains, evidence['visible_text']) else 1


if __name__ == '__main__':
    raise SystemExit(main())
