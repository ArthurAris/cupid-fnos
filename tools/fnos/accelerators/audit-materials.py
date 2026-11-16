#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Inspect pinned local accelerator materials; never load ELF files or run vendor tools."""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import struct
import subprocess

ROLES = {
    'npu': {'cdsp-firmware', 'dsp-shell', 'dsp-skeleton', 'linux-runtime', 'fastrpc-client'},
    'video': {'video-firmware', 'iris-module', 'videocc-module'},
}


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate manifest key')
        result[key] = value
    return result


def inspect_file(path, expected):
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Symlink material path')
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 128 * 1024 * 1024:
        raise ValueError('Nonordinary/oversized material file')
    data = path.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError('Material SHA mismatch')
    elf = None
    if data.startswith(b'\x7fELF'):
        if len(data) < 20 or data[5] not in (1, 2):
            raise ValueError('Malformed ELF header')
        order = '<' if data[5] == 1 else '>'
        elf = {'class': data[4], 'type': struct.unpack_from(order + 'H', data, 16)[0],
               'machine': struct.unpack_from(order + 'H', data, 18)[0], 'little_endian': data[5] == 1}
    return data, elf


def command(args):
    result = subprocess.run(args, capture_output=True, text=True, timeout=10,
                            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise ValueError('Static inspector failed: ' + args[0])
    return result.stdout


def audit(manifest, kernel):
    errors, records, found = [], [], set()
    kind = manifest.get('accelerator') if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or set(manifest) != {'schema', 'accelerator', 'board', 'kernel', 'files'} or
            type(manifest.get('schema')) is not int or manifest.get('schema') != 1 or kind not in ROLES or
            manifest.get('board') != 'xiaomi,cupid' or manifest.get('kernel') != kernel):
        raise ValueError('Manifest schema/board/kernel mismatch')
    files = manifest.get('files')
    if not isinstance(files, list) or len(files) > 100:
        raise ValueError('Invalid bounded file list')
    for item in files:
        try:
            if not isinstance(item, dict) or set(item) != {'role', 'path', 'sha256'}:
                raise ValueError('Invalid file record')
            role = item['role']
            if role not in ROLES[kind]:
                raise ValueError('Unknown material role')
            path = Path(item['path'])
            if not path.is_absolute() or '..' in path.parts:
                raise ValueError('Material paths must be absolute')
            if (not isinstance(item['sha256'], str) or len(item['sha256']) != 64 or
                    any(c not in '0123456789abcdef' for c in item['sha256'])):
                raise ValueError('An externally pinned SHA256 is required')
            data, elf = inspect_file(path, item['sha256'])
            row = {'role': role, 'path': str(path), 'bytes': len(data), 'elf': elf}
            if role in ('linux-runtime', 'fastrpc-client'):
                if not elf or elf['class'] != 2 or elf['machine'] != 183 or not elf['little_endian'] or elf['type'] != 3:
                    raise ValueError('Host runtime must be AArch64 ELF64 shared object')
                dynamic = command(['readelf', '-dW', str(path)])
                program = command(['readelf', '-lW', str(path)])
                if any(name in dynamic for name in ('[libc.so]', '[liblog.so]', '[libandroid.so]')) or '/system/bin/linker' in program:
                    raise ValueError('Android bionic runtime is not a Linux glibc runtime')
                # A marker in a filename is not Linux ABI evidence.
                versions = command(['readelf', '-VW', str(path)])
                if 'GLIBC_' not in versions:
                    raise ValueError('Linux glibc ABI not established; needs manual review')
                row['needed'] = [line.strip() for line in dynamic.splitlines() if '(NEEDED)' in line]
            elif role in ('dsp-shell', 'dsp-skeleton'):
                if not elf or elf['machine'] != 164:
                    raise ValueError('DSP shell/skeleton must be Hexagon ELF; signature/firmware pairing still unverified')
            elif role.endswith('-module'):
                if not elf or elf['machine'] != 183 or elf['type'] != 1:
                    raise ValueError('Module must be AArch64 relocatable ELF')
                name = command(['modinfo', '-F', 'name', str(path)]).strip()
                version = command(['modinfo', '-F', 'vermagic', str(path)]).strip()
                aliases = command(['modinfo', '-F', 'alias', str(path)]).splitlines()
                wanted = 'qcom,sm8450-iris' if role == 'iris-module' else 'qcom,sm8450-videocc'
                if version != kernel + ' SMP mod_unload modversions aarch64' or not any(wanted in alias for alias in aliases):
                    raise ValueError('Official module version/SM8450 match missing')
                row.update(module=name, vermagic=version, aliases=aliases)
            records.append(row)
            found.add(role)
        except (ValueError, OSError, subprocess.SubprocessError, struct.error) as error:
            errors.append(str(error))
    missing = sorted(ROLES[kind] - found)
    return {'accelerator': kind, 'kernel': kernel, 'records': records,
            'missing_roles': missing, 'errors': errors,
            'material_static_checks_passed': not errors and not missing,
            'firmware_signature_and_pairing_verified': False,
            'hardware_ready': False, 'driver_loaded': False, 'inference_or_codec_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest', type=Path, help='Private pinned material manifest')
    parser.add_argument('--kernel', required=True, help='Actually running, independently verified kernel')
    args = parser.parse_args()
    if args.manifest.stat().st_size > 65536:
        parser.error('Manifest size limit')
    try:
        result = audit(json.loads(args.manifest.read_bytes(), object_pairs_hook=unique_json), args.kernel)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['material_static_checks_passed'] else 2)


if __name__ == '__main__':
    main()
