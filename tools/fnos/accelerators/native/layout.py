#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare explicitly scoped type shapes in pinned BTF and fresh object DWARF.

Matching sizes/member offsets is a necessary ABI check, not complete type,
function-signature, semantics or hardware lifecycle equivalence.
"""
import argparse
import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import re
import struct
import subprocess

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('layout_materials', HERE.parent / 'verify-firmware.py')
materials = importlib.util.module_from_spec(spec)
spec.loader.exec_module(materials)
READ_ELF = '/usr/bin/aarch64-linux-gnu-readelf'
SCOPES = {
    'videocc': {
        'objects': ['videocc-sm8450.o'],
        'types': ['clk_regmap', 'clk_rcg2', 'clk_alpha_pll', 'clk_branch', 'qcom_cc_desc',
                  'gdsc', 'alpha_pll_config', 'parent_map', 'freq_tbl', 'pll_vco',
                  'qcom_reset_map', 'clk_regmap_div'],
    },
    'fastrpc': {
        'objects': ['fastrpc.o'],
        'types': ['device', 'device_driver', 'platform_device', 'file_operations', 'miscdevice',
                  'dma_buf', 'dma_buf_attachment', 'dma_buf_ops', 'sg_table', 'scatterlist',
                  'vm_area_struct', 'rpmsg_device', 'rpmsg_driver', 'rpmsg_endpoint'],
    },
    'iris': {
        'objects': ['iris_probe.o', 'iris_vb2.o', 'iris_vdec.o', 'iris_venc.o', 'iris_vidc.o'],
        'types': ['device', 'device_driver', 'platform_device', 'file_operations', 'sg_table',
                  'scatterlist', 'vb2_queue', 'vb2_buffer', 'vb2_v4l2_buffer', 'video_device',
                  'v4l2_device', 'v4l2_fh', 'v4l2_m2m_ctx', 'v4l2_m2m_ops'],
    },
}


def btf_structs(blob, base_strings=b'', first_id=1):
    if len(blob) < 24:
        raise ValueError('Truncated BTF header')
    magic, version, flags, header, typeoff, typelen, stroff, strlen = struct.unpack_from('<HBBIIIII', blob)
    if magic != 0xeb9f or version != 1 or flags or header < 24 or header > len(blob):
        raise ValueError('Unsupported BTF header')
    ts, ss = header + typeoff, header + stroff
    if ts + typelen > len(blob) or ss + strlen > len(blob):
        raise ValueError('BTF block outside bounds')
    if max(ts, ss) < min(ts + typelen, ss + strlen):
        raise ValueError('Overlapping BTF blocks')
    strings = base_strings + blob[ss:ss + strlen]
    if not strings or strings[-1] != 0 or not base_strings and strings[0] != 0:
        raise ValueError('Malformed BTF string table')
    types = blob[ts:ts + typelen]

    def text(offset):
        if offset >= len(strings):
            raise ValueError('BTF string offset outside bounds')
        end = strings.find(b'\0', offset)
        if end < 0:
            raise ValueError('Unterminated BTF string')
        try:
            return strings[offset:end].decode('utf-8')
        except UnicodeError as error:
            raise ValueError('Malformed BTF string') from error

    pos, number, layouts = 0, first_id, {}
    while pos < len(types):
        if pos + 12 > len(types):
            raise ValueError('Truncated BTF type header')
        nameoff, info, size = struct.unpack_from('<III', types, pos)
        pos += 12
        kind, count, kflag = (info >> 24) & 31, info & 65535, info >> 31
        name = text(nameoff)
        if kind in (4, 5):
            extra = count * 12
        elif kind in (1, 14, 17):
            extra = 4
        elif kind == 3:
            extra = 12
        elif kind in (6, 13):
            extra = count * 8
        elif kind in (15, 19):
            extra = count * 12
        elif kind in (2, 7, 8, 9, 10, 11, 12, 16, 18):
            extra = 0
        else:
            raise ValueError('Unknown BTF kind')
        if pos + extra > len(types):
            raise ValueError('Truncated BTF type body')
        if kind in (4, 5):
            members = []
            for index in range(count):
                nm, _type, bits = struct.unpack_from('<III', types, pos + index * 12)
                offset = bits & 0xffffff if kflag else bits
                bit_size = bits >> 24 if kflag else 0
                if offset > size * 8 or offset + bit_size > size * 8:
                    raise ValueError('BTF member outside type')
                members.append({'name': text(nm), 'bit_offset': offset, 'bit_size': bit_size})
            if name:
                layouts.setdefault(name, []).append({'kind': 'struct' if kind == 4 else 'union',
                                                     'size': size, 'members': members, 'id': number})
        pos += extra
        number += 1
    if not types:
        raise ValueError('Empty BTF type table')
    return layouts, strings, number


def official_blob(kernel, kernel_sha, map_path, map_sha, image_sha):
    compressed = materials.pinned_read(kernel, kernel_sha)
    # Bound decompression before allocating an untrusted image.
    with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as handle:
        image = handle.read(128 * 1024 * 1024 + 1)
    if len(image) > 128 * 1024 * 1024 or hashlib.sha256(image).hexdigest() != image_sha:
        raise ValueError('Official Image hash or size mismatch')
    map_data = materials.pinned_read(map_path, map_sha, 16 * 1024 * 1024).decode('ascii')

    def symbol(name):
        rows = re.findall(r'^([0-9a-f]+) \w ' + re.escape(name) + '$', map_data, re.M)
        if len(rows) != 1:
            raise ValueError('Missing or ambiguous official map symbol: ' + name)
        return int(rows[0], 16)

    base = symbol('_text')
    start, end = symbol('__start_BTF') - base, symbol('__stop_BTF') - base
    if not 0 <= start < end <= len(image):
        raise ValueError('Official BTF outside Image bounds')
    return image[start:end]


def official_btf(kernel, kernel_sha, map_path, map_sha, image_sha):
    return btf_structs(official_blob(kernel, kernel_sha, map_path, map_sha, image_sha))


def dwarf_structs(output, wanted):
    entries, current = [], None
    for line in output.splitlines():
        match = re.match(r'\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: (\d+)(?: \((DW_TAG_\w+)\))?', line)
        if match:
            if current is not None:
                entries.append(current)
            current = {'level': int(match[1]), 'tag': match[4] or '', 'attrs': {}}
        elif current is not None:
            attr = re.match(r'\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)', line)
            if attr:
                key, value = attr.groups()
                if key in current['attrs']:
                    raise ValueError('Duplicate DWARF attribute')
                current['attrs'][key] = value
    if current is not None:
        entries.append(current)
    if not entries:
        raise ValueError('No DWARF entries')

    def attrname(value):
        return value.rsplit(': ', 1)[-1].strip()

    def number(value):
        if not re.fullmatch(r'(?:0x[0-9a-fA-F]+|[0-9]+)', value.strip()):
            raise ValueError('Unsupported DWARF numeric expression')
        return int(value, 16 if value.strip().startswith('0x') else 10)

    layouts = {}
    for index, entry in enumerate(entries):
        if entry['tag'] not in ('DW_TAG_structure_type', 'DW_TAG_union_type'):
            continue
        attrs = entry['attrs']
        name = attrname(attrs.get('DW_AT_name', ''))
        if name not in wanted or 'DW_AT_byte_size' not in attrs:
            continue
        size, members = number(attrs['DW_AT_byte_size']), []
        union = entry['tag'] == 'DW_TAG_union_type'
        for child in entries[index + 1:]:
            if child['level'] <= entry['level']:
                break
            if child['level'] != entry['level'] + 1 or child['tag'] != 'DW_TAG_member':
                continue
            ca = child['attrs']
            if 'DW_AT_data_bit_offset' in ca:
                bits = number(ca['DW_AT_data_bit_offset'])
            elif 'DW_AT_data_member_location' in ca:
                bits = number(ca['DW_AT_data_member_location']) * 8
            elif union:
                bits = 0
            else:
                raise ValueError('Missing DWARF member offset: ' + name)
            bit_size = number(ca.get('DW_AT_bit_size', '0'))
            if bits > size * 8 or bits + bit_size > size * 8:
                raise ValueError('DWARF member outside type')
            members.append({'name': attrname(ca.get('DW_AT_name', '')),
                            'bit_offset': bits, 'bit_size': bit_size})
        row = {'kind': 'union' if union else 'struct', 'size': size, 'members': members}
        if row not in layouts.setdefault(name, []):
            layouts[name].append(row)
    return layouts


def compare_layouts(official, objects, wanted):
    if not wanted or not objects:
        raise ValueError('Nonempty layout scope and objects required')
    local = {}
    for layouts in objects:
        for name, rows in layouts.items():
            for row in rows:
                if row not in local.setdefault(name, []):
                    local[name].append(row)
    rows = []
    for name in sorted(wanted):
        candidates = local.get(name, [])
        providers = official.get(name, [])
        if not candidates:
            status = 'missing-object-type'
        elif not providers:
            status = 'missing-official-type'
        elif len(candidates) != 1:
            status = 'conflicting-object-layouts'
        else:
            normalized = [{key: row[key] for key in ('kind', 'size', 'members')} for row in providers]
            status = 'matched' if candidates[0] in normalized else 'layout-mismatch'
        rows.append({'type': name, 'status': status, 'object': candidates, 'official': providers})
    return {'types': rows, 'matched_types': sum(row['status'] == 'matched' for row in rows),
            'required_types': len(rows), 'layout_scope_verified': all(row['status'] == 'matched' for row in rows),
            'complete_abi_verified': False, 'function_signatures_verified': False,
            'hardware_ready': False}


def inspect_dwarf(path, wanted):
    materials.read_regular(path)
    try:
        result = subprocess.run([READ_ELF, '--debug-dump=info', str(path)], capture_output=True,
                                text=True, timeout=90,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError('DWARF inspector unavailable or timed out') from error
    if result.returncode or result.stderr.strip() or not result.stdout.strip():
        raise ValueError('DWARF inspection failed or incomplete')
    return dwarf_structs(result.stdout, wanted)


def validate_specification(specification, component):
    if (not isinstance(specification, dict) or type(specification.get('schema')) is not int or
            specification['schema'] != 1 or
            set(specification) - {'schema', 'official', 'split_btf', 'components'}):
        raise ValueError('Invalid private layout specification')
    base = specification.get('official')
    if not isinstance(base, dict) or set(base) != {'kernel', 'kernel_sha256', 'map', 'map_sha256', 'image_sha256'}:
        raise ValueError('Invalid official layout reference')
    split_inputs = specification.get('split_btf', [])
    if not isinstance(split_inputs, list) or len(split_inputs) > 20:
        raise ValueError('Invalid bounded split BTF list')
    for item in split_inputs:
        if not isinstance(item, dict) or set(item) != {'path', 'sha256'}:
            raise ValueError('Invalid split BTF record')
    if component not in SCOPES:
        raise ValueError('Unknown layout component')
    if 'components' in specification and specification['components'] != SCOPES:
        raise ValueError('Private specification changes required layout scope')
    return base, split_inputs


def audit(specification, work, component):
    base, split_inputs = validate_specification(specification, component)
    layouts, strings, number = official_btf(Path(base['kernel']), base['kernel_sha256'],
                                           Path(base['map']), base['map_sha256'], base['image_sha256'])
    for item in split_inputs:
        raw = materials.pinned_read(Path(item['path']), item['sha256'])
        split, _strings, _number = btf_structs(raw, strings, number)
        for name, rows in split.items():
            layouts.setdefault(name, []).extend(rows)
    scope = SCOPES[component]
    wanted = set(scope['types'])
    objects = []
    pins = {}
    for name in scope['objects']:
        path = work / name
        before = hashlib.sha256(materials.read_regular(path)).hexdigest()
        objects.append(inspect_dwarf(path, wanted))
        after = hashlib.sha256(materials.read_regular(path)).hexdigest()
        if before != after:
            raise ValueError('Object changed during layout inspection')
        pins[name] = after
    report = compare_layouts(layouts, objects, wanted)
    report.update(component=component, object_sha256=pins,
                  reference_kernel_sha256=base['kernel_sha256'], reference_map_sha256=base['map_sha256'])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=('fastrpc', 'videocc', 'iris'))
    parser.add_argument('--spec', required=True, type=Path, help='Private pinned BTF/object scope')
    parser.add_argument('--spec-sha256', required=True)
    parser.add_argument('--work', required=True, type=Path)
    args = parser.parse_args()
    try:
        specification = json.loads(materials.pinned_read(args.spec, args.spec_sha256, 65536),
                                   object_pairs_hook=materials.unique_json)
        report = audit(specification, args.work, args.component)
    except (ValueError, OSError, struct.error) as error:
        print(json.dumps({'layout_scope_verified': False, 'error': str(error), 'hardware_ready': False}))
        raise SystemExit(2) from error
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report['layout_scope_verified'] else 2)


if __name__ == '__main__':
    main()
