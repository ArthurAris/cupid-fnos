#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify and unpack c1147 build headers into a NEW case-sensitive directory.
No archive script or helper is executed. Reject special entries and verify all
regular-file bytes against the separately supplied published manifest.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
import tarfile


def unique(pairs):
    out = {}
    for name, value in pairs:
        if name in out:
            raise ValueError('Duplicate manifest key')
        out[name] = value
    return out


def unpack(archive, manifest_path, output, archive_sha256):
    if len(archive_sha256) != 64 or any(c not in '0123456789abcdef' for c in archive_sha256):
        raise ValueError('Invalid external archive SHA256')
    if archive.is_symlink() or manifest_path.is_symlink():
        raise ValueError('Linked input refused')
    raw = archive.read_bytes()
    if len(raw) > 20 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != archive_sha256:
        raise ValueError('Archive SHA256/size mismatch')
    manifest_raw = manifest_path.read_bytes()
    if len(manifest_raw) > 4 * 1024 * 1024:
        raise ValueError('Manifest budget')
    manifest = json.loads(manifest_raw, object_pairs_hook=unique)
    if manifest.get('schema') != 'cupid-official-headers-archive-1' or manifest.get('kernel') != '6.18.18.c1147-trim':
        raise ValueError('Unexpected build-input schema')
    expected = {}
    for row in manifest['files']:
        name = row['path']
        path = PurePosixPath(name)
        if path.is_absolute() or '\\' in name or '..' in path.parts or name in expected:
            raise ValueError('Unsafe/duplicate manifest path')
        expected[name] = row
    root_name = manifest['root']
    if PurePosixPath(root_name).name != root_name:
        raise ValueError('Archive root invalid')
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise ValueError('Use a NEW absolute output directory')
    parent = output.parent
    if not parent.is_dir() or any(p.is_symlink() for p in (parent, *parent.parents)):
        raise ValueError('Unsafe output parent')
    # Windows/DrvFS case-insensitive directories silently lose UAPI headers.
    with tempfile.TemporaryDirectory(prefix='cupid-case-check-', dir=parent) as probe:
        p = Path(probe) / 'case'
        p.write_bytes(b'lower')
        if (Path(probe) / 'CASE').exists():
            raise ValueError('Output filesystem is case-insensitive; use Linux/ext4')
    seen = set()
    members = []
    with tarfile.open(archive, 'r:xz') as tar:
        total = 0
        for member in tar.getmembers():
            if not member.isfile() or not member.name.startswith(root_name + '/'):
                raise ValueError('Archive links/special entries refused')
            name = member.name[len(root_name) + 1:]
            path = PurePosixPath(name)
            if path.is_absolute() or '\\' in name or '..' in path.parts or name in seen:
                raise ValueError('Unsafe/duplicate archive path')
            if member.size < 0 or member.size > 4 * 1024 * 1024:
                raise ValueError('Member size budget')
            total += member.size
            if total > 80 * 1024 * 1024 or len(seen) >= 8000:
                raise ValueError('Expanded archive budget')
            seen.add(name)
            stream = tar.extractfile(member)
            data = stream.read(member.size + 1)
            if len(data) != member.size:
                raise ValueError('Incomplete archive member')
            if name == 'public-inputs-manifest.json':
                if data != manifest_raw:
                    raise ValueError('Embedded/external manifest differs')
            else:
                row = expected.get(name)
                if row is None or len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                    raise ValueError('Member bytes differ from manifest')
            members.append((name, data, member.mode))
    if seen != set(expected) | {'public-inputs-manifest.json'}:
        raise ValueError('Manifest member coverage differs')
    output.mkdir(mode=0o700)
    for name, data, mode in members:
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream:
            stream.write(data)
        target.chmod(0o755 if mode & 0o111 else 0o644)
    symvers = hashlib.sha256((output / 'Module.symvers').read_bytes()).hexdigest()
    if symvers != '121ac1b2269aa229ec044ff7bc3211ee3a7eea74829f89979603240299ab31a6':
        raise ValueError('Official Module.symvers mismatch')
    return {'output': str(output), 'files_verified': len(members),
            'archive_sha256': archive_sha256, 'case_sensitive_names_preserved': True,
            'module_symvers_verified': True, 'helpers_executed': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive', type=Path, required=True)
    p.add_argument('--manifest', type=Path, required=True)
    p.add_argument('--sha256', required=True)
    p.add_argument('--output', type=Path, required=True)
    a = p.parse_args()
    print(json.dumps(unpack(a.archive, a.manifest, a.output, a.sha256)))


if __name__ == '__main__':
    main()
