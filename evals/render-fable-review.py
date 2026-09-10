#!/usr/bin/env python3
"""Render graded runs with the actual Anthropic creator scripts, without raw streams.

The creator aggregator currently emits placeholder model names and a fixed
three-runs count. Preserve its original output, then correct only provenance
metadata from the observed runs. Never change expectations or scores.
"""
import argparse
from collections import Counter
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def correct_metadata(benchmark):
    runs = benchmark['runs']
    counts = Counter((run['eval_id'], run['configuration']) for run in runs)
    unique_counts = set(counts.values())
    meta = benchmark['metadata']
    meta['skill_path'] = 'Per-run observed source paths: outputs/loading-and-model.json'
    meta['runs_per_configuration'] = next(iter(unique_counts)) if len(unique_counts) == 1 else 'varies; see sample_counts'
    meta['sample_counts'] = {f'{case}:{config}': n for (case, config), n in sorted(counts.items())}
    meta['executor_model'] = 'Per-run runtime evidence; model mismatches remain failed expectations'
    meta['analyzer_model'] = 'Visible-evidence review plus independent deterministic checks'
    configs = [key for key in benchmark['run_summary'] if key != 'delta']
    meta['delta_basis'] = ' minus '.join(configs[:2])
    benchmark['notes'] += [
        'Scores combine loading, model identity, scope, function and behavior checks. They are not a Fable-5 efficacy or win-rate estimate.',
        'One observed sample per case/version in each iteration; earlier failed attempts are retained separately. No statistical superiority claim.',
        'Native Skill invocation differs from the extra exact-source reread gate. Missing the latter does not prove the skill failed to load.',
        'Metadata corrected from actual run records; original creator-generated benchmark is preserved. Expectations, scores and deltas are unchanged.',
        'Delta orientation: ' + meta['delta_basis'] + '. Negative is not necessarily a regression of the current skill.'
    ]
    return benchmark


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--iteration', type=Path, required=True)
    parser.add_argument('--creator-skill', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    creator = args.creator_skill.resolve(strict=True)
    aggregator = creator / 'scripts/aggregate_benchmark.py'
    viewer = creator / 'eval-viewer/generate_review.py'
    for path in (aggregator, viewer):
        if not path.is_file():
            parser.error(f'Missing actual creator script: {path}')
    os.umask(0o077)
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    copied = []
    # Only graded output directories are staged: no eval-*.json glob collision,
    # raw provider streams, hidden thinking, credentials or mutable task files.
    for grading in sorted(args.iteration.glob('eval-*/*/run-*/grading.json')):
        run = grading.parent
        target = output / run.relative_to(args.iteration)
        shutil.copytree(run, target)
        copied.append(str(target.relative_to(output)))
    if not copied:
        raise ValueError('No graded runs; refusing to render an empty success report')
    command = [sys.executable, str(aggregator), str(output), '--skill-name', 'pua']
    subprocess.run(command, stdin=subprocess.DEVNULL, check=True)
    for suffix in ('json', 'md'):
        shutil.copyfile(output / f'benchmark.{suffix}', output / f'benchmark.generated.{suffix}')
    path = output / 'benchmark.json'
    before = json.loads(path.read_text())
    frozen_runs = json.dumps(before['runs'], sort_keys=True)
    frozen_scores = json.dumps(before['run_summary'], sort_keys=True)
    benchmark = correct_metadata(before)
    assert json.dumps(benchmark['runs'], sort_keys=True) == frozen_runs
    assert json.dumps(benchmark['run_summary'], sort_keys=True) == frozen_scores
    path.write_text(json.dumps(benchmark, ensure_ascii=False, indent=2))
    spec = importlib.util.spec_from_file_location('actual_creator_aggregate', aggregator)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    (output / 'benchmark.md').write_text(module.generate_markdown(benchmark))
    subprocess.run([sys.executable, str(viewer), str(output), '--skill-name', 'pua',
                    '--benchmark', str(path), '--static', str(output / 'review.html')],
                   stdin=subprocess.DEVNULL, check=True)
    (output / 'render-provenance.json').write_text(json.dumps({
        'source_iteration': str(args.iteration.resolve()), 'copied_graded_runs': copied,
        'aggregator_sha256': hashlib.sha256(aggregator.read_bytes()).hexdigest(),
        'viewer_sha256': hashlib.sha256(viewer.read_bytes()).hexdigest(),
        'raw_streams_included': False, 'expectations_and_scores_unchanged': True,
        'corrected_fields': ['metadata', 'notes']
    }, ensure_ascii=False, indent=2))
    print(json.dumps({'review': str(output / 'review.html'), 'graded_runs': len(copied)}))


if __name__ == '__main__':
    main()
