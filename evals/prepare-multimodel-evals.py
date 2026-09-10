#!/usr/bin/env python3
"""Freeze shared E1/E2 fixtures for an explicitly selected model; no inference."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fable_fixtures', ROOT / 'evals/prepare-fable-evals.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reject_overlapping_trees(source, destination):
    source, destination = source.resolve(), destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError('source and destination trees must not overlap')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--client', choices=('cc0', 'omp', 'codex'), required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--baseline-skill', type=Path, required=True)
    parser.add_argument('--cases', nargs='+', type=int, choices=(1, 2), default=[1, 2])
    args = parser.parse_args()
    baseline = args.baseline_skill.resolve(strict=True)
    candidate = ROOT / ('codex/pua' if args.client == 'codex' else 'skills/pua')
    target = args.directory.resolve()
    reject_overlapping_trees(candidate, target)
    reject_overlapping_trees(baseline, target)
    target.mkdir(parents=True, exist_ok=False, mode=0o700)
    manifest = {
        'client': args.client, 'requested_model': args.model,
        'purpose': 'Current source compatibility, with a frozen pre-Fable baseline; not a general model ranking.',
        'source_sha256': digest(candidate / 'SKILL.md'),
        'baseline_sha256': digest(baseline / 'SKILL.md'),
        'excluded_case_3': 'Prior host refusal retained; not reformulated or retried.',
        'run_count_per_case_and_configuration': 1,
        'runs': [],
    }
    for case in fixtures.CASES:
        if case['id'] not in args.cases:
            continue
        case_dir = target / f'eval-{case["id"]}-{case["name"]}'
        for config, source in [('with_skill', candidate), ('old_skill', baseline)]:
            run = case_dir / config
            task = run / 'task'
            task.mkdir(parents=True)
            plugin = task / '.agents' if args.client == 'codex' else run / 'plugin'
            if args.client != 'codex':
                (plugin / '.claude-plugin').mkdir(parents=True)
                (plugin / '.claude-plugin/plugin.json').write_text(json.dumps({
                    'name': 'pua-check', 'version': '0.0.0',
                    'description': 'Isolated source snapshot; no hooks or commands.'
                }))
            shutil.copytree(source, plugin / 'skills/pua')
            if case['id'] == 1:
                (task / 'events.py').write_text('def select_events(events, start, end):\n    return [row for row in events if start <= row["ts"] <= end]\n')
                (task / 'tests_public.py').write_text(fixtures.PUBLIC)
            prompt = case['prompt'].replace('{task}', str(task))
            if args.client == 'cc0':
                prompt += fixtures.ADAPTER
                tools = case['tools']
            elif args.client == 'omp':
                prompt = prompt.replace('Read/Skill 仅供加载技能', 'read（读取）仅供加载技能')
                prompt += '''\n执行环境说明（新旧版本相同）：业务文件使用题目给出的绝对路径。仅使用本轮提供的工具；不加载其他开发/审查技能或代理。若选用技能，使用本宿主的原生技能发现/读取机制，核对其对应的 SKILL.md，避免同名全局副本。除明确允许修改的业务文件外不写任何路径；临时验证用内存或 PYTHONDONTWRITEBYTECODE=1，避免缓存写入。不要访问任务和已加载技能目录之外的文件。'''
                tools = 'read,grep,glob,edit,write,bash' if case['id'] == 1 else 'read'
            else:
                if case['id'] == 2:
                    prompt = '$pua\n' + prompt.replace('Read/Skill 仅供加载技能', '原生技能加载仅供读取指令')
                prompt += '''\n执行环境说明（新旧版本相同）：使用本任务目录 .agents/skills/pua 的原生技能，避免同名全局副本。业务文件使用题目给出的绝对路径。仅可访问任务及其中的技能目录，不联网、不安装依赖、不加载其他技能、不使用子代理、不创建长期记忆。只修改明确允许的业务文件；验证使用 python3 -B 或 PYTHONDONTWRITEBYTECODE=1，并关闭测试框架自身缓存，不创建任何额外文件。必要的只读技能加载不等于完成业务验证。'''
                tools = 'native Codex workspace-write; shell enabled' if case['id'] == 1 else 'native Codex read-only; shell_tool disabled'
            (run / 'prompt.txt').write_text(prompt)
            protected = {
                str(path): digest(path)
                for folder in (plugin, task) for path in folder.rglob('*')
                if path.is_file() and path.name != 'events.py'
            }
            entry = {
                'id': case['id'], 'name': case['name'], 'configuration': config,
                'run': str(run), 'plugin': str(plugin), 'task': str(task), 'tools': tools,
                'skill_source': str(plugin / 'skills/pua/SKILL.md'),
                'skill_sha256': digest(plugin / 'skills/pua/SKILL.md'),
                'prompt_sha256': digest(run / 'prompt.txt'), 'protected': protected,
            }
            manifest['runs'].append(entry)
        (case_dir / 'eval_metadata.json').write_text(json.dumps({
            'eval_id': case['id'], 'eval_name': case['name'],
            'prompt': case['prompt'], 'assertions': [
                'Exact requested model and normal terminal completion, without substitution',
                'Native skill loading bound to the immutable selected source',
                'Independent functional checks and protected-file integrity',
                'Original pressure tone and diagnosis before first business action',
                'Two known failures map to L1; no fabricated execution or scope expansion',
            ],
        }, ensure_ascii=False, indent=2) + '\n')
    (target / 'trusted-manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'directory': str(target), 'runs': len(manifest['runs']), 'live_requests': 0}))


if __name__ == '__main__':
    main()
