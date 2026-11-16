#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify pinned private MDT firmware against an actual candidate device tree.

This is static consistency checking, not firmware signature authentication or
DSP runtime pairing. No firmware is executed and no device is accessed.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

MAX_FILE = 128 * 1024 * 1024
MAX_DTB = 2 * 1024 * 1024
TYPE_MASK = 7 << 24
TYPE_HASH = 2 << 24
RELOCATABLE = 1 << 27


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


def read_regular(path, limit=MAX_FILE):
    path = Path(path).absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise ValueError('Symlink material refused')
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError('Nonordinary or oversized material')
        with os.fdopen(descriptor, 'rb', closefd=False) as handle:
            data = handle.read(limit + 1)
        after = os.fstat(descriptor)
        fields = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if len(data) != before.st_size or any(getattr(before, key) != getattr(after, key) for key in fields):
            raise ValueError('Material changed during read')
        return data
    finally:
        os.close(descriptor)


def pinned_read(path, digest, limit=MAX_FILE):
    if not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('Malformed SHA256 pin')
    data = read_regular(path, limit)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError('Material SHA mismatch: ' + Path(path).name)
    return data


def words(data):
    if not data or len(data) % 4:
        raise ValueError('Malformed DT cells')
    return struct.unpack('>' + 'I' * (len(data) // 4), data)


def parse_dtb(data):
    if len(data) < 40:
        raise ValueError('Truncated FDT header')
    magic, total, structure, strings, reserved, version, compatible, _cpu, strings_size, struct_size = struct.unpack_from('>10I', data)
    if (magic != 0xd00dfeed or total != len(data) or total > MAX_DTB or
            version != 17 or compatible > 17 or compatible > version):
        raise ValueError('Unsupported FDT header')
    if (structure % 4 or reserved % 8 or min(structure, strings, reserved) < 40 or
            not struct_size or structure + struct_size > total or strings + strings_size > total):
        raise ValueError('FDT block outside bounds')
    if max(structure, strings) < min(structure + struct_size, strings + strings_size):
        raise ValueError('Overlapping FDT blocks')
    pos = reserved
    while True:
        if pos + 16 > total:
            raise ValueError('Truncated FDT reserve map')
        address, size = struct.unpack_from('>QQ', data, pos)
        pos += 16
        if not address and not size:
            break
        if address + size > 1 << 64:
            raise ValueError('FDT reserve range overflow')
    if (max(reserved, structure) < min(pos, structure + struct_size) or
            max(reserved, strings) < min(pos, strings + strings_size)):
        raise ValueError('Reserve map overlaps FDT blocks')
    end = structure + struct_size
    pos = structure
    stack, nodes = [], {}
    ended, root_seen = False, False
    while pos < end:
        if pos + 4 > end:
            raise ValueError('Truncated FDT token')
        token = struct.unpack_from('>I', data, pos)[0]
        pos += 4
        if token == 1:
            name_end = data.find(b'\0', pos, end)
            if name_end < 0:
                raise ValueError('Unterminated FDT node')
            try:
                name = data[pos:name_end].decode('ascii')
            except UnicodeError as error:
                raise ValueError('Invalid FDT node name') from error
            if not stack:
                if root_seen or name:
                    raise ValueError('Multiple or malformed FDT root')
                path = '/'
                root_seen = True
            else:
                if not name or '/' in name:
                    raise ValueError('Invalid FDT child name')
                path = stack[-1].rstrip('/') + '/' + name
            if path in nodes:
                raise ValueError('Duplicate FDT node')
            nodes[path] = {}
            stack.append(path)
            pos = (name_end + 4) & ~3
        elif token == 2:
            if not stack:
                raise ValueError('Unbalanced FDT node end')
            stack.pop()
        elif token == 3:
            if not stack or pos + 8 > end:
                raise ValueError('FDT property outside node')
            length, offset = struct.unpack_from('>II', data, pos)
            pos += 8
            if pos + length > end or offset >= strings_size:
                raise ValueError('FDT property outside bounds')
            name_end = data.find(b'\0', strings + offset, strings + strings_size)
            if name_end < 0:
                raise ValueError('Unterminated FDT property name')
            try:
                name = data[strings + offset:name_end].decode('ascii')
            except UnicodeError as error:
                raise ValueError('Invalid FDT property name') from error
            if not name or name in nodes[stack[-1]]:
                raise ValueError('Duplicate or empty FDT property')
            nodes[stack[-1]][name] = data[pos:pos + length]
            pos = (pos + length + 3) & ~3
        elif token == 4:
            pass
        elif token == 9:
            if stack or not root_seen or pos != end:
                raise ValueError('Incomplete FDT structure')
            ended = True
            break
        else:
            raise ValueError('Unknown FDT token')
        if pos > end:
            raise ValueError('FDT alignment outside structure')
    if not ended:
        raise ValueError('Missing FDT end token')
    return nodes


def memory_region(nodes, firmware_node):
    if firmware_node not in nodes:
        raise ValueError('Firmware node absent from candidate DTB')
    refs = words(nodes[firmware_node].get('memory-region', b''))
    if len(refs) != 1 or not refs[0]:
        raise ValueError('Expected one firmware memory-region phandle')
    providers = {}
    for path, props in nodes.items():
        handles = [words(props[key]) for key in ('phandle', 'linux,phandle') if key in props]
        if not handles:
            continue
        if any(len(value) != 1 or value[0] in (0, 0xffffffff) for value in handles) or len(set(handles)) != 1:
            raise ValueError('Invalid FDT phandle')
        handle = handles[0][0]
        if handle in providers:
            raise ValueError('Duplicate FDT phandle')
        providers[handle] = path
    path = providers.get(refs[0])
    if path is None or path.rsplit('/', 1)[0] != '/reserved-memory':
        raise ValueError('Firmware phandle does not reference reserved-memory')
    root = nodes.get('/reserved-memory', {})
    if root.get('ranges') != b'':
        raise ValueError('Reserved memory must have identity ranges')
    address_cells = words(root.get('#address-cells', nodes['/'].get('#address-cells', b'')))
    size_cells = words(root.get('#size-cells', nodes['/'].get('#size-cells', b'')))
    if len(address_cells) != 1 or len(size_cells) != 1 or address_cells[0] not in (1, 2) or size_cells[0] not in (1, 2):
        raise ValueError('Unsupported reserved memory cell widths')
    cells = words(nodes[path].get('reg', b''))
    ac, sc = address_cells[0], size_cells[0]
    if len(cells) != ac + sc:
        raise ValueError('Expected one reserved memory range')
    address = size = 0
    for cell in cells[:ac]:
        address = (address << 32) | cell
    for cell in cells[ac:]:
        size = (size << 32) | cell
    if not address or not size or address + size > 1 << 64 or address % 4096 or size % 4096:
        raise ValueError('Invalid aligned reserved memory range')
    if nodes[path].get('status', b'okay\0') not in (b'okay\0', b'ok\0'):
        raise ValueError('Firmware reserved memory disabled')
    return {'node': path, 'base': address, 'size': size}


def elf_programs(data, machine):
    if len(data) < 52 or data[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('Expected ELF32 little-endian firmware')
    header = struct.unpack_from('<HHIIIIIHHHHHH', data, 16)
    kind, arch, version, _entry, phoff, _shoff, _flags, ehsize, entsize, count, _shents, _shnum, _shstr = header
    if (kind not in (2, 3) or arch != machine or version != 1 or ehsize != 52 or
            entsize != 32 or not 2 <= count <= 64 or phoff < 52 or phoff + count * entsize > len(data)):
        raise ValueError('Invalid ELF ABI or program table')
    result = []
    for index in range(count):
        values = struct.unpack_from('<8I', data, phoff + index * entsize)
        names = ('type', 'offset', 'vaddr', 'paddr', 'filesz', 'memsz', 'flags', 'align')
        row = dict(zip(names, values))
        row['index'] = index
        if row['offset'] + row['filesz'] >= 1 << 32 or row['paddr'] + row['memsz'] >= 1 << 32:
            raise ValueError('Firmware segment address overflow')
        if row['type'] == 1 and row['flags'] & TYPE_MASK != TYPE_HASH and row['filesz'] > row['memsz']:
            raise ValueError('Firmware filesz exceeds memsz')
        if row['align'] and row['align'] & (row['align'] - 1):
            raise ValueError('Invalid ELF segment alignment')
        result.append(row)
    return result


def validate_mdt(files, prefix, machine, region):
    mdt_name = prefix + '.mdt'
    if mdt_name not in files:
        raise ValueError('MDT file missing')
    data = files[mdt_name]
    programs = elf_programs(data, machine)
    hashes = [row for row in programs if row['flags'] & TYPE_MASK == TYPE_HASH]
    if len(hashes) != 1 or not hashes[0]['filesz'] or programs[0]['type'] == 1:
        raise ValueError('Missing or ambiguous MDT hash metadata')
    header_size = programs[0]['filesz']
    hash_row = hashes[0]
    if header_size < 52 + len(programs) * 32 or header_size + hash_row['filesz'] != len(data):
        raise ValueError('MDT packed metadata length mismatch')
    if files.get(prefix + '.b00') != data[:header_size]:
        raise ValueError('MDT header differs from b00')
    hash_file = prefix + '.b%02d' % hash_row['index']
    if files.get(hash_file) != data[header_size:]:
        raise ValueError('MDT hash metadata differs from split hash segment')
    loadable = [row for row in programs if row['type'] == 1 and row['memsz'] and row['flags'] & TYPE_MASK != TYPE_HASH]
    if not loadable:
        raise ValueError('No MDT loadable segments')
    relocate = any(row['flags'] & RELOCATABLE for row in loadable)
    minimum = min(row['paddr'] for row in loadable)
    maximum = max((row['paddr'] + row['memsz'] + 4095) & ~4095 for row in loadable)
    reloc_base = minimum if relocate else region['base']
    for row in loadable:
        offset = row['paddr'] - reloc_base
        if offset < 0 or offset + row['memsz'] > region['size']:
            raise ValueError('Firmware segment outside resolved DT memory')
        if row['filesz']:
            name = prefix + '.b%02d' % row['index']
            if name not in files or len(files[name]) != row['filesz']:
                raise ValueError('Missing or wrong-length firmware segment: ' + name)
    needed = maximum - minimum
    if needed > region['size']:
        raise ValueError('Page-aligned firmware span exceeds reserved memory')
    mbn = files.get(prefix + '.mbn')
    if mbn is not None:
        if elf_programs(mbn, machine) != programs:
            raise ValueError('Full MBN and split MDT program tables differ')
        for row in programs:
            if row['filesz']:
                name = prefix + '.b%02d' % row['index']
                if (name not in files or row['offset'] + row['filesz'] > len(mbn) or
                        mbn[row['offset']:row['offset'] + row['filesz']] != files[name]):
                    raise ValueError('Full MBN differs from split segment: ' + name)
    return {'relocatable': relocate, 'load_base': minimum, 'page_aligned_load_bytes': needed,
            'reserved_region': region, 'aligned_slack_bytes': region['size'] - needed,
            'loadable_segments': len(loadable), 'metadata_and_segments_consistent': True,
            'signature_verified': False, 'runtime_pairing_verified': False, 'hardware_ready': False}


def verify(manifest_path, manifest_sha, dtb_path, dtb_sha, component):
    prefixes = {'cdsp': ('cdsp', 164, '/soc@0/remoteproc@32300000'),
                'video': ('vpu20_4v', 94, '/soc@0/video-codec@aa00000')}
    prefix, machine, node = prefixes[component]
    manifest = json.loads(pinned_read(manifest_path, manifest_sha, 65536), object_pairs_hook=unique_json)
    if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or
            manifest['schema'] != 1 or manifest.get('board') != 'xiaomi,cupid' or
            manifest.get('soc') != 'sm8450'):
        raise ValueError('Firmware manifest schema or board mismatch')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Invalid bounded firmware file list')
    directory = Path(manifest_path).absolute().parent
    files = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'size', 'sha256'}:
            raise ValueError('Malformed firmware file record')
        name = row['name']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or
                name in ('.', '..', 'manifest.json') or name in files or
                type(row['size']) is not int or not 0 <= row['size'] <= MAX_FILE):
            raise ValueError('Invalid or duplicate firmware file name/size')
        data = pinned_read(directory / name, row['sha256'])
        if len(data) != row['size']:
            raise ValueError('Firmware file size mismatch')
        files[name] = data
    if {p.name for p in directory.iterdir()} != set(files) | {Path(manifest_path).name}:
        raise ValueError('Extra or missing firmware directory entry')
    nodes = parse_dtb(pinned_read(dtb_path, dtb_sha, MAX_DTB))
    report = validate_mdt(files, prefix, machine, memory_region(nodes, node))
    report.update(component=component, manifest_sha256=manifest_sha, dtb_sha256=dtb_sha,
                  pinned_files=len(files), file_hashes_verified=True,
                  static_consistency_passed=True, phone_operated=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=('cdsp', 'video'))
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--dtb', required=True, type=Path)
    parser.add_argument('--dtb-sha256', required=True)
    args = parser.parse_args()
    try:
        report = verify(args.manifest, args.manifest_sha256, args.dtb, args.dtb_sha256, args.component)
    except (OSError, ValueError, struct.error) as error:
        print(json.dumps({'static_consistency_passed': False, 'error': str(error), 'hardware_ready': False}))
        raise SystemExit(2) from error
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
