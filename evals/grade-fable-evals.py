#!/usr/bin/env python3
"""Create skill-creator grading records from real outputs and explicit human/model review."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from cc0_fable_evidence import inspect_stream

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('iteration',type=Path)
    parser.add_argument('--manual-review',type=Path,required=True)
    args=parser.parse_args()
    manifest=json.loads((args.iteration/'trusted-manifest.json').read_text())
    manual=json.loads(args.manual_review.read_text())
    results=[]
    for entry in manifest['runs']:
        run=Path(entry['run']); execution=run/'execution'
        if not (execution/'summary.json').exists():
            continue  # Unrun/refused scenarios are reported separately, never counted as passes.
        summary=json.loads((execution/'summary.json').read_text())
        evidence=inspect_stream(execution/'stream.jsonl')
        key=f"{entry['id']}:{entry['configuration']}"
        if key not in manual:
            raise ValueError(f'Missing qualitative review: {key}')
        checks=[]
        def add(text,passed,detail): checks.append({'text':text,'passed':bool(passed),'evidence':detail})
        add('执行器总门与显式工作目录检查通过',
            summary.get('run_passed') is True and summary.get('cwd_matches_expected') is True,
            json.dumps({'run_passed':summary.get('run_passed'),
                        'cwd_matches_expected':summary.get('cwd_matches_expected'),
                        'observed_cwd':evidence['actual_cwd']},ensure_ascii=False))
        exact=(summary['process_exit']==0 and evidence['terminal_success'] and evidence['exact_model_confirmed']
               and evidence['expected_model_in_usage'])
        add('实际主回复与用量均为 Fable-5，无宿主回退',exact,
            f"models={evidence['observed_assistant_models']}; usage={evidence['usage_model_keys']}; fallback={evidence['fallback_observed']}")
        add('原生 namespaced Skill（命名空间技能）调用成功','pua-check:pua' in evidence['successful_skill_invocations'],
            str(evidence['successful_skill_invocations']))
        add('额外来源复核：精确插件内 SKILL.md 读取与前后指纹一致',summary['loading']['passed'],json.dumps(summary['loading']['checks']))
        modified=[name for name,digest in entry['protected'].items() if not Path(name).is_file() or
                  Path(name).is_symlink() or hashlib.sha256(Path(name).read_bytes()).hexdigest()!=digest]
        allowed=str((Path(entry['task'])/'events.py').resolve()) if entry['id']==1 else None
        unexpected=[]
        for call in evidence['tool_calls']:
            if call['name'] not in ('Write','Edit','MultiEdit'): continue
            path=call['input'].get('file_path')
            if not path: unexpected.append('missing write path'); continue
            resolved=Path(path) if Path(path).is_absolute() else Path(evidence['actual_cwd'])/path
            if str(resolved.resolve())!=allowed:unexpected.append(str(resolved))
        extras=[str(path) for path in Path(entry['task']).rglob('*') if path.is_file() and
                str(path) not in entry['protected'] and str(path.resolve())!=allowed]
        add('验收资产不变且无额外业务文件写入（非操作系统沙箱证明）',not modified and not unexpected and not extras,
            json.dumps({'modified_protected':modified,'unexpected_write_tools':unexpected,'extra_task_files':extras}))
        target=Path(entry['task'])/'events.py' if entry['id']==1 else execution/'visible-transcript.md'
        checked=subprocess.run([sys.executable,str(ROOT/'evals/check-fable-artifacts.py'),'--case',str(entry['id']),
                                '--file',str(target)],stdin=subprocess.DEVNULL,capture_output=True,text=True,timeout=25)
        add('独立事后功能检查通过（不算模型自己执行）',checked.returncode==0,checked.stdout.strip()+checked.stderr.strip())
        if entry['id']!=1:
            execution_calls=[c['name'] for c in evidence['tool_calls'] if c['name'] not in ('Read','Skill')]
            add('无执行工具场景只使用 Skill/Read',not execution_calls,str(execution_calls))
        for item in manual[key]:
            if set(item)!={'text','passed','evidence'} or not isinstance(item['passed'],bool):
                raise ValueError('Manual review must use exact skill-creator expectation fields')
            checks.append(item)
        destination=run/'run-1'
        destination.mkdir(exist_ok=False)
        outputs=destination/'outputs';outputs.mkdir()
        shutil.copyfile(execution/'visible-transcript.md',outputs/'response.md')
        if entry['id']==1:shutil.copyfile(target,outputs/'events.py')
        (outputs/'loading-and-model.json').write_text(json.dumps({k:evidence[k] for k in
            ['observed_assistant_models','usage_model_keys','model_fallback_events','successful_skill_invocations',
             'successful_source_reads','actual_cwd']},ensure_ascii=False,indent=2))
        (outputs/'artifact-check.json').write_text(checked.stdout)
        passed=sum(c['passed'] for c in checks)
        usage=evidence['result'].get('modelUsage') or {}
        tokens=sum(sum(v.get(k,0) or 0 for k in ['inputTokens','outputTokens','cacheReadInputTokens','cacheCreationInputTokens'])
                   for v in usage.values())
        timing={'total_tokens':tokens,'duration_ms':evidence['result'].get('duration_ms'),
                'total_duration_seconds':summary['elapsed_seconds'],'token_basis':'Sum of reported modelUsage input/output/cache tokens; not unique context size.'}
        grading={'grading_schema_revision':2,'expectations':checks,'summary':{'passed':passed,'failed':len(checks)-passed,'total':len(checks),'pass_rate':passed/len(checks)},
                 'execution_metrics':{'total_tool_calls':evidence['tool_call_count']},
                 'user_notes_summary':{'uncertainties':['One sample per scenario/version; no general efficacy or benchmark significance claim.',
                     'Native invocation and extra source reread are distinct checks; missing reread is not proof no native loading occurred.']}}
        (destination/'grading.json').write_text(json.dumps(grading,ensure_ascii=False,indent=2))
        (destination/'timing.json').write_text(json.dumps(timing,ensure_ascii=False,indent=2))
        (destination/'eval_metadata.json').write_text(json.dumps({'eval_id':entry['id'],'eval_name':entry['name'],
            'prompt':(run/'prompt.txt').read_text(),'assertions':[c['text'] for c in checks]},ensure_ascii=False,indent=2))
        (destination/'provenance.json').write_text(json.dumps({'source_execution':str(execution),
            'stream_sha256':hashlib.sha256((execution/'stream.jsonl').read_bytes()).hexdigest(),
            'qualitative_review_sha256':hashlib.sha256(args.manual_review.read_bytes()).hexdigest()},indent=2))
        results.append({'case':entry['id'],'configuration':entry['configuration'],**grading['summary']})
    (args.iteration/'grading-index.json').write_text(json.dumps(results,ensure_ascii=False,indent=2));print(json.dumps(results))


if __name__=='__main__':main()
