#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail-closed source snapshot scan; never prints matched secret contents.

Run again on actual staged content and all reachable/unreachable Git blobs before
publishing. Optional --deny-values accepts a caller-owned private JSON string
list for exact sensitive-value checks; it is never copied into the repository.
This scanner is not a guarantee against every possible disclosure.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ALLOWED_TOP = {'README.md', 'NOTICE.md', 'LICENSE', '.gitignore', '.gitattributes',
               'LICENSES', 'docs', 'sources', 'tests', 'patches', 'tools', 'scripts'}
FORBIDDEN_SUFFIXES = {'.o', '.ko', '.exe', '.dll', '.pyc', '.pyo', '.elf', '.img',
                      '.dtb', '.dtbo', '.mbn', '.mdt', '.fde', '.fdg', '.zip',
                      '.png', '.jpg', '.jpeg', '.mp4', '.webm', '.pem', '.key'}
FORBIDDEN_DIRS = {'private', '.zcode', '__pycache__', '.pytest_cache', 'node_modules'}
PATTERNS = {
    'credential-token': r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{16})\b',
    'private-key': r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'url-credentials': r'https?://[^\s/]+:[^\s/@]+@',
    'host-home': r'(?:[A-Za-z]:[\\/]+Users[\\/]|/mnt/[a-z]/Users/|/home/(?!(?:user|linux)\b)[A-Za-z0-9_-]+/|/root/(?:fnos|linux|rootfs|cupid)[^\s]*)',
    'wifi-secret': r'(?im)^\s*(?:ssid|psk)\s*=\s*"[^"\n]+"',
    'concrete-secret': r'''(?im)\b(?:password|passwd|api_key|access_token|secret_key)\s*[:=]\s*['"](?!['"]|password|example|placeholder|synthetic|test|x['"]|secret['"]|host-|fixture|not-|none|null|unset|<|\$|\{)[^'"\n]{6,}['"]''',
}
# Protocol fixtures may use literal loopback addresses. Network addresses are
# still reviewed by a person; no generic MAC/UUID allowlist makes them harmless.
MAC = re.compile(r'(?i)(?<![0-9a-f:])(?:[0-9a-f]{2}:){5}[0-9a-f]{2}(?![0-9a-f:])')
DUMMY_MACS = {'00:00:00:00:00:00', 'ff:ff:ff:ff:ff:ff', '02:00:00:00:00:01',
              '02:00:00:00:00:02', 'aa:bb:cc:dd:ee:ff',
              '02:00:00:00:42:01', '02:00:00:00:42:02'}


def git(root, *args):
    result = subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                            cwd=str(root.parent), timeout=30)
    if result.returncode:
        raise ValueError('Git inventory failed')
    return result.stdout


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--deny-values', type=Path)
    parser.add_argument('--git-objects', action='store_true')
    args = parser.parse_args(argv)
    root = args.root.resolve()
    denied = []
    if args.deny_values:
        denied = json.loads(args.deny_values.read_text(encoding='utf-8'))
        if not isinstance(denied, list) or not all(isinstance(s, str) and len(s) >= 6 for s in denied):
            raise ValueError('Invalid private denial inventory')
    compiled = {name: re.compile(pattern) for name, pattern in PATTERNS.items()}
    issues, rows, warnings = [], [], []

    def inspect(label, raw):
        if len(raw) > 4 * 1024 * 1024:
            issues.append({'file': label, 'category': 'oversize'}); return
        if b'\0' in raw or raw.startswith((b'\x7fELF', b'MZ', b'PK\x03\x04', b'\x1f\x8b')):
            issues.append({'file': label, 'category': 'binary'}); return
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            issues.append({'file': label, 'category': 'non-UTF8'}); return
        for category, pattern in compiled.items():
            if pattern.search(text):
                issues.append({'file': label, 'category': category})
        if any(value in text for value in denied):
            issues.append({'file': label, 'category': 'private-exact-value'})
        if any(match.group().lower() not in DUMMY_MACS for match in MAC.finditer(text)):
            issues.append({'file': label, 'category': 'concrete-MAC-review'})
        if '/var/tmp/' in text or 'private/fnos/' in text:
            warnings.append({'file': label, 'category': 'external-material-reference'})

    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if '.git' in rel.parts:
            continue
        if path.is_symlink():
            issues.append({'file': rel.as_posix(), 'category': 'symlink'}); continue
        if not path.is_file():
            continue
        label = rel.as_posix()
        if rel.parts[0] not in ALLOWED_TOP or any(part in FORBIDDEN_DIRS for part in rel.parts):
            issues.append({'file': label, 'category': 'not-allowlisted-path'})
        if path.suffix.lower() in FORBIDDEN_SUFFIXES:
            issues.append({'file': label, 'category': 'forbidden-artifact'})
        raw = path.read_bytes()
        inspect(label, raw)
        rows.append({'path': label, 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
    objects = 0
    if args.git_objects:
        # cat-file --batch-all-objects includes dangling objects, not just HEAD.
        metadata = git(root, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname) %(objecttype) %(objectsize)').decode('ascii')
        for line in metadata.splitlines():
            oid, kind, size = line.split()
            if kind == 'blob':
                raw = git(root, 'cat-file', 'blob', oid)
                if len(raw) != int(size):
                    raise ValueError('Git object changed')
                inspect('git-blob:' + oid, raw)
                objects += 1
        staged = git(root, 'ls-files', '--stage', '-z').decode('utf-8').split('\0')
        for item in filter(None, staged):
            metadata, name = item.split('\t', 1)
            mode, oid, stage = metadata.split()
            if mode not in ('100644', '100755') or stage != '0':
                issues.append({'file': name, 'category': 'unexpected-index-mode'})
            if name not in {r['path'] for r in rows}:
                issues.append({'file': name, 'category': 'index-not-in-inventory'})
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    print(json.dumps({'source_files': len(rows), 'source_bytes': sum(r['bytes'] for r in rows),
                      'inventory_sha256': digest, 'git_blobs_scanned': objects,
                      'issues': issues, 'warnings': warnings,
                      'passed': not issues}, ensure_ascii=True))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
