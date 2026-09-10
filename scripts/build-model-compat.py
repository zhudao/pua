#!/usr/bin/env python3
"""Build portable PUA artifacts from two upstream-compatible entrypoints."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
START = '<!-- PUA-RUNTIME-CONTRACT:START -->'
END = '<!-- PUA-RUNTIME-CONTRACT:END -->'


def insert_core(path: Path, core: str, anchor: str) -> None:
    source = path.read_text()
    block = START + '\n' + core.rstrip() + '\n' + END + '\n\n'
    if START in source or END in source:
        if source.count(START) != 1 or source.count(END) != 1:
            raise ValueError(f'malformed generated block: {path}')
        left, rest = source.split(START, 1)
        _, right = rest.split(END, 1)
        source = left + block + right.lstrip('\n')
    else:
        if source.count(anchor) != 1:
            raise ValueError(f'expected one insertion anchor: {path}')
        source = source.replace(anchor, block + anchor, 1)
    path.write_text(source)


def package(folder: Path, target: Path) -> dict:
    members = {}
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob('*')):
            if path.is_symlink():
                raise ValueError(f'refuse symlink: {path}')
            if not path.is_file():
                continue
            name = 'pua/' + path.relative_to(folder).as_posix()
            data = path.read_bytes()
            info = zipfile.ZipInfo(name, (2026, 9, 9, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
            members[name] = hashlib.sha256(data).hexdigest()
    return {'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'members': members}


def build() -> None:
    core = (ROOT / 'compat/runtime-core.md').read_text()
    details = ROOT / 'compat/runtime-contract.md'
    claude = ROOT / 'skills/pua'
    codex = ROOT / 'codex/pua'
    insert_core(claude / 'SKILL.md', core, '**⚠️ 味道检测')
    insert_core(codex / 'SKILL.md', core, '## 三条铁律')
    for folder in (claude, codex):
        (folder / 'references').mkdir(exist_ok=True)
        shutil.copyfile(details, folder / 'references/runtime-contract.md')
    chatgpt = ROOT / 'chatgpt/pua'
    (chatgpt / 'references').mkdir(parents=True, exist_ok=True)
    shutil.copyfile(codex / 'SKILL.md', chatgpt / 'SKILL.md')
    shutil.copyfile(details, chatgpt / 'references/runtime-contract.md')
    # This entrypoint is self-contained: it requires no plugin hooks or commands.
    _, _, body = (chatgpt / 'SKILL.md').read_text().split('---', 2)
    paste = ('# PUA 对话版\n\n请在当前任务中使用以下 PUA 工作方式。'
             '保留原来的情绪和强度，直接执行任务，不要只总结这份规则。'
             '这只是当前对话指令，不代表已经安装技能、后台钩子或本地工具。\n\n'
             + body.strip() + '\n\n---\n\n' + details.read_text())
    (ROOT / 'chatgpt/PUA-Paste.md').write_text(paste)
    dist = ROOT / 'dist'
    dist.mkdir(exist_ok=True)
    manifest = {name: package(folder, dist / name) for name, folder in (
        ('pua-chatgpt.zip', chatgpt), ('pua-claude-code.zip', claude),
        ('pua-codex.zip', codex))}
    (dist / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'packages': list(manifest), 'manifest': str(dist / 'manifest.json')}, ensure_ascii=False))


if __name__ == '__main__':
    argparse.ArgumentParser(description=__doc__).parse_args()
    build()
