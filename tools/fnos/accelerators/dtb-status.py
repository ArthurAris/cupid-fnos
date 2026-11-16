#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Change exact disabled status properties with fixed-size FDT NOP padding.

The caller supplies a trusted whole-image pin. This module validates complete
FDT structures and independently checks the resulting property/byte scope.
"""
import importlib.util
from pathlib import Path
import struct

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('dtb_validator', HERE / 'verify-firmware.py')
fdt = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fdt)


def property_spans(data):
    fdt.parse_dtb(data)
    start = struct.unpack_from('>I', data, 8)[0]
    end = start + struct.unpack_from('>I', data, 36)[0]
    strings = struct.unpack_from('>I', data, 12)[0]
    pos = start
    stack, spans = [], {}
    while pos < end:
        token = struct.unpack_from('>I', data, pos)[0]
        pos += 4
        if token == 1:
            name_end = data.index(b'\0', pos, end)
            name = data[pos:name_end].decode('ascii')
            path = '/' if not stack else stack[-1].rstrip('/') + '/' + name
            stack.append(path)
            pos = (name_end + 4) & ~3
        elif token == 2:
            stack.pop()
        elif token == 3:
            length, name_offset = struct.unpack_from('>II', data, pos)
            header = pos
            pos += 8
            name_end = data.index(b'\0', strings + name_offset)
            name = data[strings + name_offset:name_end].decode('ascii')
            spans[(stack[-1], name)] = (header, length, pos)
            pos = (pos + length + 3) & ~3
        elif token == 9:
            break
    return spans


def enable_status(data, paths):
    if not paths or len(set(paths)) != len(paths) or any(not path.startswith('/') for path in paths):
        raise ValueError('Unique absolute DT node paths required')
    before = fdt.parse_dtb(data)
    spans = property_spans(data)
    result = bytearray(data)
    allowed_bytes = set()
    for path in paths:
        if before.get(path, {}).get('status') != b'disabled\0':
            raise ValueError('Target node is not explicitly disabled: ' + path)
        header, length, offset = spans[(path, 'status')]
        if length != 9 or data[offset + 9:offset + 12] != b'\0' * 3:
            raise ValueError('Unexpected status property padding')
        struct.pack_into('>I', result, header, 5)
        result[offset:offset + 8] = b'okay\0\0\0\0'
        struct.pack_into('>I', result, offset + 8, 4)
        allowed_bytes.update(range(header, header + 4))
        allowed_bytes.update(range(offset, offset + 12))
    after = fdt.parse_dtb(bytes(result))
    expected = {path: dict(values) for path, values in before.items()}
    for path in paths:
        expected[path]['status'] = b'okay\0'
    if after != expected or len(result) != len(data):
        raise ValueError('Unexpected DT node/property change')
    changed = {i for i, (old, new) in enumerate(zip(data, result)) if old != new}
    if not changed <= allowed_bytes:
        raise ValueError('DT bytes changed outside status allowlist')
    return bytes(result), sorted(changed)


def derive_image(source, dtb, targets):
    if len(source) < 48 or source[:8] != b'ANDROID!':
        raise ValueError('Missing Android boot header')
    kernel_size = struct.unpack_from('<I', source, 8)[0]
    page = struct.unpack_from('<I', source, 36)[0]
    header_version = struct.unpack_from('<I', source, 40)[0]
    if header_version != 2 or page != 2048 or not kernel_size or page + kernel_size > len(source):
        raise ValueError('Unsupported or truncated boot image')
    fdt.parse_dtb(dtb)
    kernel = source[page:page + kernel_size]
    if kernel.count(dtb) != 1:
        raise ValueError('Expected one exact pinned board DTB in the kernel')
    offset = kernel.index(dtb)
    modified, changes = enable_status(dtb, targets)
    base = page + offset
    result = source[:base] + modified + source[base + len(dtb):]
    changed = [base + index for index in changes]
    if (len(result) != len(source) or result[:base] != source[:base] or
            result[base + len(dtb):] != source[base + len(dtb):]):
        raise ValueError('Boot image changed outside pinned DTB')
    return result, {'dtb_offset': base, 'dtb_size': len(dtb), 'changed_offsets': changed,
                    'changed_nodes': list(targets), 'scope_verified': True,
                    'system_readonly': False, 'contains_new_accelerator_modules': False,
                    'contains_new_accelerator_firmware': False, 'hardware_ready': False}
