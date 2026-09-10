"""Parse observable Claude Code evidence, not model self-identification."""
import json
import hashlib
from pathlib import Path


def inspect_stream(path: Path, expected_model: str = 'claude-fable-5') -> dict:
    models, tools, texts, skills, plugins = set(), [], [], [], []
    synthetic_models, fallback_events, refusal_events = set(), [], []
    result, cwd, fallback = {}, None, False
    successful_results = set()
    failed_results = set()
    runtime_core_in_result = False
    result_bodies = {}
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            cwd = event.get('cwd')
            skills = event.get('skills', [])
            plugins = event.get('plugins', [])
        elif kind == 'system' and str(event.get('subtype', '')).startswith('model_refusal_'):
            record = {key: event.get(key) for key in
                      ('subtype', 'original_model', 'fallback_model', 'trigger', 'scope', 'api_refusal_category')}
            refusal_events.append(record)
            if event.get('subtype') == 'model_refusal_fallback':
                fallback = True
                fallback_events.append(record)
        if kind == 'assistant':
            message = event.get('message', {})
            if message.get('model'):
                (synthetic_models if message['model'].startswith('<') else models).add(message['model'])
            fallback = fallback or bool(message.get('fallback'))
            iterations = (message.get('usage') or {}).get('iterations') or []
            fallback = fallback or any(item.get('type') == 'fallback_message' for item in iterations)
            for content in message.get('content', []):
                if content.get('type') == 'text':
                    texts.append(content.get('text', ''))
                elif content.get('type') == 'tool_use':
                    tools.append({'id': content.get('id'), 'name': content.get('name'),
                                  'input': content.get('input', {})})
        elif kind == 'user':
            content = event.get('message', {}).get('content', [])
            if not isinstance(content, list):
                continue
            for block in content:
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                identifier = block.get('tool_use_id')
                if not isinstance(identifier, str) or not identifier:
                    continue
                (failed_results if block.get('is_error') else successful_results).add(identifier)
                body = block.get('content', '')
                result_bodies[identifier] = body
                if not block.get('is_error') and 'PUA-RUNTIME-CONTRACT:START' in str(body):
                    runtime_core_in_result = True
        elif kind == 'result':
            result = {key: event.get(key) for key in
                      ('subtype', 'is_error', 'result', 'duration_ms', 'num_turns',
                       'total_cost_usd', 'usage', 'modelUsage', 'permission_denials')}
            fallback = fallback or bool(event.get('fallback'))
            iterations = (event.get('usage') or {}).get('iterations') or []
            fallback = fallback or any(item.get('type') == 'fallback_message' for item in iterations)
    invoked = [tool['input'].get('skill') for tool in tools if tool['name'] == 'Skill'
               and tool['id'] in successful_results and tool['id'] not in failed_results]
    reads = []
    for tool in tools:
        if tool['name'] != 'Read' or tool['id'] not in successful_results or tool['id'] in failed_results:
            continue
        raw = tool['input'].get('file_path')
        if not isinstance(raw, str):
            continue
        path = Path(raw)
        if not path.is_absolute() and not cwd:
            continue
        resolved = path if path.is_absolute() else Path(cwd) / path
        # Markers remain bound to this Read result, not to another tool call.
        body = str(result_bodies.get(tool['id'], ''))
        reads.append({'tool_use_id': tool['id'], 'path': str(resolved.resolve()),
                      'content_observed': bool(body),
                      'runtime_core_marker': 'PUA-RUNTIME-CONTRACT:START' in body})
    usage_models = sorted((result.get('modelUsage') or {}).keys())
    # The primary model comes from assistant messages. Other usage entries can
    # be auxiliary classifiers/titles: disclose them, never assume a fallback.
    auxiliary_usage = [name for name in usage_models if name not in models]
    return {'expected_model': expected_model, 'observed_assistant_models': sorted(models),
            'exact_model_confirmed': models == {expected_model} and not fallback,
            'model_substitution_observed': bool(models) and models != {expected_model},
            'synthetic_message_models': sorted(synthetic_models),
            'model_refusal_events': refusal_events, 'model_fallback_events': fallback_events,
            'usage_model_keys': usage_models,
            'expected_model_in_usage': expected_model in usage_models,
            'auxiliary_usage_models': auxiliary_usage,
            'fallback_observed': fallback, 'actual_cwd': cwd,
            'discovered_skills': skills, 'loaded_plugins': plugins,
            'successful_skill_invocations': invoked,
            'successful_source_reads': reads,
            'runtime_core_observed_in_tool_result': runtime_core_in_result,
            'tool_calls': tools, 'tool_call_count': len(tools),
            'visible_text': '\n\n---\n\n'.join(texts),
            'terminal_success': result.get('subtype') == 'success' and result.get('is_error') is False,
            'result': result}


def loading_evidence(evidence: dict, skill: str, source: Path,
                     before_sha256: str, require_runtime_marker: bool = False) -> dict:
    """Bind a native namespaced invocation to its actual, unchanged plugin source.

    This proves observable loading, not behavioral effectiveness or an OS sandbox.
    Discovery alone, a generic marker, and unqualified same-name skills fail.
    """
    source = source.resolve()
    namespace, separator, _ = skill.partition(':')
    matches = []
    for plugin in evidence['loaded_plugins']:
        if not isinstance(plugin, dict) or plugin.get('name') != namespace or not plugin.get('path'):
            continue
        root = Path(plugin['path']).resolve()
        if source.is_relative_to(root):
            matches.append(str(root))
    reads = [read for read in evidence['successful_source_reads']
             if read['path'] == str(source) and read['content_observed']
             and (not require_runtime_marker or read['runtime_core_marker'])]
    current = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
    checks = {'qualified_native_skill_invoked': bool(separator) and skill in evidence['successful_skill_invocations'],
              'source_inside_matching_loaded_plugin': len(set(matches)) == 1,
              'exact_source_read_succeeded': bool(reads),
              'source_unchanged': current == before_sha256}
    return {'passed': all(checks.values()), 'checks': checks, 'required_skill': skill,
            'source': str(source), 'before_sha256': before_sha256, 'after_sha256': current,
            'linked_read_ids': [read['tool_use_id'] for read in reads]}
