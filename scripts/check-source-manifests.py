#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only release Markdown links and source-manifest byte verification."""
import argparse
import hashlib
import json
from pathlib import Path
import re

p = argparse.ArgumentParser()
p.add_argument('--root', type=Path, required=True)
a = p.parse_args()
root = a.root.resolve()
links = []
broken = []
for path in sorted(root.rglob('*.md')):
    if '.git' in path.parts:
        continue
    text = path.read_text(encoding='utf-8')
    for target in re.findall(r'\]\(([^)]+)\)', text):
        if '://' in target or target.startswith('#') or not target:
            continue
        destination = path.parent / target.split('#', 1)[0]
        links.append((path.relative_to(root).as_posix(), target))
        if not destination.exists():
            broken.append({'file': path.relative_to(root).as_posix(), 'target': target})
count = 0
for role in ('verified', 'research', 'clients', 'pid1'):
    path = root / 'sources/iris' / role / 'source-manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    seen = set()
    for row in manifest['files']:
        assert row['path'] not in seen
        seen.add(row['path'])
        source = root / 'sources/iris' / row['path']
        raw = source.read_bytes()
        assert len(raw) == row['bytes'] and hashlib.sha256(raw).hexdigest() == row['sha256'], row['path']
        count += 1
print(json.dumps({'local_markdown_links_checked': len(links), 'broken_links': broken,
                  'source_manifest_files_checked': count, 'passed': not broken}))
raise SystemExit(1 if broken else 0)
