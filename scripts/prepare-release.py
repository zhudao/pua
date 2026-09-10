#!/usr/bin/env python3
"""Render one version's release notes; do not publish or invoke a model."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
from urllib.parse import quote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def render_notes(root: Path, tag: str, repository: str) -> str:
    """Bind a stable tag to the manifest version and keep all stated limits."""
    if not re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", tag):
        raise ValueError("expected a stable vMAJOR.MINOR.PATCH tag")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", repository):
        raise ValueError("expected an owner/repository name")
    version = json.loads((root / 'plugin.json').read_text(encoding='utf-8'))['version']
    if tag != f'v{version}':
        raise ValueError("release tag does not match plugin version")
    changelog = (root / 'CHANGELOG.md').read_text(encoding='utf-8')
    pattern = rf'(?ms)^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)'
    matches = re.findall(pattern, changelog)
    if len(matches) != 1 or not matches[0].strip():
        raise ValueError("expected exactly one nonempty changelog section for this version")
    base = f'https://github.com/{repository}/blob/{tag}/'

    def link(match: re.Match) -> str:
        label, target = match.groups()
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or target.startswith('#'):
            return match.group(0)
        path = Path(parts.path)
        resolved = (root / path).resolve()
        if path.is_absolute() or not resolved.is_relative_to(root.resolve()) or not resolved.is_file():
            raise ValueError("release note link must name an existing repository file")
        relative = path.as_posix()
        suffix = ('?' + parts.query if parts.query else '') + ('#' + parts.fragment if parts.fragment else '')
        return f'[{label}]({base}{quote(relative, safe="/")}{suffix})'

    body = re.sub(r'\[([^\]\n]*)\]\(([^\s)]+)\)', link, matches[0].strip())
    return f'# PUA {tag}\n\n{body}\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--repository', required=True)
    args = parser.parse_args()
    notes = render_notes(ROOT, args.tag, args.repository)
    output = ROOT / 'dist/release-notes.md'
    output.parent.mkdir(exist_ok=True)
    output.write_text(notes, encoding='utf-8')
    print(f'Release notes prepared for {args.tag}')


if __name__ == '__main__':
    main()
