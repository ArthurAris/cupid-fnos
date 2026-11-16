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
import io
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile

ALLOWED_TOP = {'README.md', 'NOTICE.md', 'LICENSE', '.gitignore', '.gitattributes',
               'LICENSES', 'docs', 'sources', 'tests', 'patches', 'tools', 'scripts',
               'build-inputs'}
HEADER_PREFIX = 'build-inputs/linux-headers-6.18.18.c1147-trim/'
# The official header package ships seven aarch64 build helpers. They are not
# firmware and are required by Kbuild; every other binary remains refused.
HEADER_HELPERS = {HEADER_PREFIX + name for name in (
    'scripts/asn1_compiler', 'scripts/kallsyms', 'scripts/sorttable',
    'scripts/basic/fixdep', 'scripts/genksyms/genksyms', 'scripts/mod/modpost',
    'scripts/selinux/mdp/mdp')}
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
              '02:00:00:00:42:01', '02:00:00:00:42:02',
              # Upstream DSA tag-format diagram (include/linux/dsa/ocelot.h).
              'fe:ff:ff:ff:ff:ff'}
# One upstream Rust test embeds a synthetic kernel source path in a test string.
HEADER_HELPER_PINS = {
    '5502d7eb6b14868158cbf142d83303b1895f4aaf145f6e7057748088455f836d',
    'ba2c955ba9cb9f88d786244b490fa0497ac4e91bbdc05f45896852c05922966f',
    '04d0fe61d97a9cd8fd5b5b123595b0d13b95e551aa63593eb07d06b14eb09454',
    'a1f7f222c76274902c02b71110ad861514ca313da2e1b9138bec6b6d169ce2c3',
    '24ad5ad1b71a1a5642e9d08331102e5b07168e1e5fe010c558f52ccf563af820',
    '65e92d1ed516816871f177ed32caa81f8b4f63a222cf22cb1288681f7c083c40',
    'e51400203ba829b3bec874a746cf9f7984c5b84005c9fd44a758b5223547065a',
}
HOST_PATH_ALLOWED = {HEADER_PREFIX + 'scripts/rust_is_available_test.py': {'host-home'}}
UPSTREAM_TEST_PIN = '491f01e47fc38cf439ee41128c0ceb3fb46e716fda093195d8890a1303accb9d'
HEADER_ARCHIVE_PATH = 'build-inputs/linux-headers-6.18.18.c1147-trim-complete.tar.xz'
HEADER_ARCHIVE_SHA256 = '3a19038f84738af3505f51571cfeaa1090c1c947e2abd90df48d5e09023e4b08'
HEADER_ARCHIVE_ROOT = 'linux-headers-6.18.18.c1147-trim'
HEADER_MANIFEST_PATH = 'build-inputs/linux-headers-6.18.18.c1147-trim-complete.manifest.json'


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
    archive_members = 0

    def inspect_archive(label, raw):
        nonlocal archive_members
        manifest_raw = (root / HEADER_MANIFEST_PATH).read_bytes()
        if len(manifest_raw) > 4 * 1024 * 1024:
            raise ValueError('Header manifest size budget')
        manifest = json.loads(manifest_raw)
        expected = {r['path']: r for r in manifest['files']}
        if len(expected) != len(manifest['files']):
            raise ValueError('Header manifest duplicates')
        seen, total = set(), 0
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:xz') as tar:
            for member in tar.getmembers():
                if not member.isfile() or not member.name.startswith(HEADER_ARCHIVE_ROOT + '/'):
                    raise ValueError('Header archive links/special entries')
                name = member.name[len(HEADER_ARCHIVE_ROOT) + 1:]
                path = PurePosixPath(name)
                if path.is_absolute() or '..' in path.parts or '\\' in name or name in seen:
                    raise ValueError('Unsafe/duplicate archive path')
                if member.size < 0 or member.size > 4 * 1024 * 1024:
                    raise ValueError('Header member size budget')
                total += member.size
                if total > 80 * 1024 * 1024 or len(seen) >= 8000:
                    raise ValueError('Header expanded budget')
                seen.add(name)
                data = tar.extractfile(member).read(member.size + 1)
                if name == 'public-inputs-manifest.json':
                    if data != manifest_raw:
                        raise ValueError('Header embedded manifest changed')
                else:
                    row = expected.get(name)
                    if row is None or len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                        raise ValueError('Header member hash mismatch')
                inspect(HEADER_PREFIX + name, data)
                archive_members += 1
        if seen != set(expected) | {'public-inputs-manifest.json'}:
            raise ValueError('Header archive incomplete')

    def inspect(label, raw):
        digest = hashlib.sha256(raw).hexdigest()
        if digest == HEADER_ARCHIVE_SHA256 and (label == HEADER_ARCHIVE_PATH or label.startswith('git-blob:')):
            if len(raw) > 20 * 1024 * 1024:
                raise ValueError('Header archive byte budget')
            inspect_archive(label, raw)
            return
        if raw.startswith(b'\x7fELF') and digest in HEADER_HELPER_PINS and (label in HEADER_HELPERS or label.startswith('git-blob:')):
            warnings.append({'file': label, 'category': 'official-header-helper'})
            return
        if len(raw) > 4 * 1024 * 1024:
            issues.append({'file': label, 'category': 'oversize'}); return
        if b'\0' in raw or raw.startswith((b'\x7fELF', b'MZ', b'PK\x03\x04', b'\x1f\x8b')):
            issues.append({'file': label, 'category': 'binary'}); return
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            issues.append({'file': label, 'category': 'non-UTF8'}); return
        for category, pattern in compiled.items():
            if category == 'host-home' and digest == UPSTREAM_TEST_PIN and (category in HOST_PATH_ALLOWED.get(label, ()) or label.startswith('git-blob:')):
                continue
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
                      'header_archive_members_scanned': archive_members,
                      'issues': issues, 'warnings': warnings,
                      'passed': not issues}, ensure_ascii=True))
    return 1 if issues else 0


if __name__ == '__main__':
    raise SystemExit(main())
