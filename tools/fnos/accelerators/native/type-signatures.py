#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compare scoped member types and selected imported function signatures.

Named aggregate pointer targets are compared by identity, not by recursively
claiming the whole kernel type graph is equivalent. Unknown/missing information
is a failed check. This never establishes hardware or firmware compatibility.
"""
import importlib.util
import json
from pathlib import Path
import re
import struct
import subprocess

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('shape_check', HERE / 'layout.py')
shape = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shape)
FUNCTIONS = {
    'videocc': ['qcom_cc_probe', '__platform_driver_register', 'platform_driver_unregister'],
    'fastrpc': ['dma_buf_attach', 'dma_buf_detach', 'dma_buf_map_attachment_unlocked',
                'dma_buf_unmap_attachment_unlocked', 'dma_buf_get', 'dma_buf_put', 'misc_register',
                'misc_deregister', 'rpmsg_send', '__register_rpmsg_driver', 'unregister_rpmsg_driver'],
    'iris': ['qcom_mdt_load', 'qcom_mdt_get_size', 'v4l2_device_register',
             'v4l2_device_unregister', 'v4l2_m2m_init', 'v4l2_m2m_ctx_init',
             'v4l2_m2m_ctx_release', 'v4l2_m2m_release', 'vb2_queue_init',
             'vb2_buffer_done', 'v4l2_fh_init', 'v4l2_fh_exit'],
}


class TypeGraph:
    def __init__(self, records, names, functions):
        self.records, self.names, self.functions = records, names, functions

    def type(self, ident, trail=()):
        if ident is None or ident == 0:
            return ('void',)
        if ident not in self.records:
            raise ValueError('Unresolved type reference')
        if len(trail) >= 80:
            raise ValueError('Type recursion limit')
        row = self.records[ident]
        kind = row['kind']
        if kind in ('struct', 'union') and row.get('name'):
            return (kind, row['name'])
        if ident in trail:
            raise ValueError('Recursive anonymous or alias type')
        trail = trail + (ident,)
        if kind == 'typedef':
            return self.type(row['type'], trail)
        if kind in ('const', 'volatile', 'restrict'):
            return (kind, self.type(row['type'], trail))
        if kind == 'pointer':
            return ('pointer', row.get('size', 8), self.type(row['type'], trail))
        if kind == 'scalar':
            return ('scalar', row['size'], row['encoding'], row.get('bits', row['size'] * 8))
        if kind == 'enum':
            return ('enum', row.get('name', ''), row['size'], tuple(row['values']))
        if kind == 'array':
            return ('array', tuple(row['dimensions']), self.type(row['type'], trail))
        if kind == 'function':
            return ('function', self.type(row['return'], trail),
                    tuple(self.type(arg, trail) for arg in row['args']), row['variadic'])
        if kind in ('struct', 'union'):
            return self.aggregate(ident, trail)
        raise ValueError('Unsupported type kind: ' + kind)

    def aggregate(self, ident, trail=()):
        row = self.records[ident]
        if row['kind'] not in ('struct', 'union') or row.get('size') is None:
            raise ValueError('Incomplete aggregate type')
        members = []
        for member in row['members']:
            if member['type'] is None or member['type'] == 0:
                raise ValueError('Missing aggregate member type')
            members.append((member['name'], member['offset'], member['bits'],
                            self.type(member['type'], trail)))
        return (row['kind'], row.get('name', ''), row['size'], tuple(members))

    def named_aggregate(self, name):
        return [self.aggregate(ident) for ident in self.names.get(name, [])
                if self.records[ident].get('size') is not None]

    def named_function(self, name):
        return [self.type(ident) for ident in self.functions.get(name, [])]


def btf_graph(blob, base=None):
    # Reuse bounded BTF parsing; keep the raw ids/types instead of dropping them.
    strings = base.strings if base is not None else b''
    first = base.next_id if base is not None else 1
    shape.btf_structs(blob, strings, first)
    _magic, _version, _flags, header, typeoff, typelen, stroff, strlen = struct.unpack_from('<HBBIIIII', blob)
    strings = strings + blob[header + stroff:header + stroff + strlen]
    types = blob[header + typeoff:header + typeoff + typelen]
    records = dict(base.records) if base is not None else {}
    names = {key: list(ids) for key, ids in base.names.items()} if base is not None else {}
    functions = {key: list(ids) for key, ids in base.functions.items()} if base is not None else {}

    def text(offset):
        if offset >= len(strings):
            raise ValueError('BTF graph name outside strings')
        end = strings.find(b'\0', offset)
        if end < 0:
            raise ValueError('Unterminated BTF graph string')
        return strings[offset:end].decode('utf-8')

    pos, ident = 0, first
    while pos < len(types):
        nameoff, info, value = struct.unpack_from('<III', types, pos)
        pos += 12
        kind, count, flag = (info >> 24) & 31, info & 65535, info >> 31
        name = text(nameoff)
        row = {'kind': 'unsupported-' + str(kind), 'name': name}
        if kind == 1:
            encoding = struct.unpack_from('<I', types, pos)[0]
            code = (encoding >> 24) & 15
            if code & ~7 or encoding >> 16 & 255:
                raise ValueError('Unsupported BTF integer encoding')
            row.update(kind='scalar', size=value, encoding='bool' if code & 4 else 'signed' if code & 1 else 'unsigned', bits=encoding & 255)
            pos += 4
        elif kind == 2:
            row.update(kind='pointer', type=value, size=8)
        elif kind == 3:
            item, _index, elements = struct.unpack_from('<III', types, pos)
            row.update(kind='array', type=item, dimensions=[elements])
            pos += 12
        elif kind in (4, 5):
            members = []
            for index in range(count):
                nm, ref, bits = struct.unpack_from('<III', types, pos + index * 12)
                members.append({'name': text(nm), 'type': ref,
                                'offset': bits & 0xffffff if flag else bits,
                                'bits': bits >> 24 if flag else 0})
            row.update(kind='struct' if kind == 4 else 'union', size=value, members=members)
            if name:
                names.setdefault(name, []).append(ident)
            pos += count * 12
        elif kind in (6, 19):
            values = []
            for index in range(count):
                if kind == 6:
                    nm, val = struct.unpack_from('<II', types, pos + index * 8)
                    if flag and val & (1 << 31):
                        val -= 1 << 32
                else:
                    nm, low, high = struct.unpack_from('<III', types, pos + index * 12)
                    val = high << 32 | low
                    if flag and val & (1 << 63):
                        val -= 1 << 64
                values.append((text(nm), val))
            row.update(kind='enum', size=value, values=values)
            pos += count * (8 if kind == 6 else 12)
        elif kind == 7:
            row.update(kind='union' if flag else 'struct', size=None, members=[])
        elif kind in (8, 9, 10, 11):
            row.update(kind={8: 'typedef', 9: 'volatile', 10: 'const', 11: 'restrict'}[kind], type=value)
        elif kind == 12:
            row.update(kind='function-alias', type=value)
            if name:
                functions.setdefault(name, []).append(value)
        elif kind == 13:
            args, variadic = [], False
            for index in range(count):
                nm, ref = struct.unpack_from('<II', types, pos + index * 8)
                if ref == 0:
                    if index != count - 1 or nm != 0:
                        raise ValueError('Invalid variadic BTF argument')
                    variadic = True
                else:
                    args.append(ref)
            row.update(kind='function', return_type=value, args=args, variadic=variadic)
            row['return'] = value
            pos += count * 8
        elif kind in (14, 17):
            pos += 4
        elif kind == 15:
            pos += count * 12
        elif kind == 16:
            row.update(kind='scalar', size=value, encoding='float', bits=value * 8)
        records[ident] = row
        ident += 1
    graph = TypeGraph(records, names, functions)
    graph.strings, graph.next_id = strings, ident
    return graph


def dwarf_graph(output):
    entries, stack, current = {}, [], None
    for line in output.splitlines():
        match = re.match(r'\s*<(\d+)><([0-9a-f]+)>: Abbrev Number: (\d+)(?: \((DW_TAG_\w+)\))?', line)
        if match:
            level, ident, abbrev, tag = int(match[1]), int(match[2], 16), int(match[3]), match[4] or ''
            while stack and entries[stack[-1]]['level'] >= level:
                stack.pop()
            current = None
            if not abbrev:
                continue
            if ident in entries:
                raise ValueError('Duplicate DWARF DIE')
            current = {'tag': tag, 'attrs': {}, 'children': [], 'level': level}
            entries[ident] = current
            if stack:
                entries[stack[-1]]['children'].append(ident)
            stack.append(ident)
        elif current is not None:
            match = re.match(r'\s*<[0-9a-f]+>\s+(DW_AT_\w+)\s*:\s*(.*)', line)
            if match:
                key, value = match.groups()
                if key in current['attrs']:
                    raise ValueError('Duplicate DWARF graph attribute')
                current['attrs'][key] = value
    if not entries:
        raise ValueError('No DWARF graph entries')

    def ref(attrs, key='DW_AT_type'):
        if key not in attrs:
            return None
        match = re.fullmatch(r'<0x([0-9a-f]+)>', attrs[key])
        if not match:
            raise ValueError('Unsupported DWARF reference')
        return int(match[1], 16)

    def number(value):
        match = re.fullmatch(r'(-?(?:0x[0-9a-fA-F]+|[0-9]+))(?:\s+\([A-Za-z_ ]+\))?', value.strip())
        if not match:
            raise ValueError('Unsupported DWARF graph number')
        return int(match[1], 16 if match[1].lstrip('-').startswith('0x') else 10)

    def name(attrs):
        return attrs.get('DW_AT_name', '').rsplit(': ', 1)[-1].strip()

    records, names, functions = {}, {}, {}
    for ident, entry in entries.items():
        tag, attrs = entry['tag'], entry['attrs']
        row = {'kind': 'unsupported-' + tag, 'name': name(attrs)}
        children = [entries[child] for child in entry['children']]
        if tag in ('DW_TAG_structure_type', 'DW_TAG_union_type'):
            union = tag == 'DW_TAG_union_type'
            members = []
            for child in children:
                if child['tag'] != 'DW_TAG_member':
                    continue
                ca = child['attrs']
                if 'DW_AT_data_bit_offset' in ca:
                    offset = number(ca['DW_AT_data_bit_offset'])
                elif 'DW_AT_data_member_location' in ca:
                    offset = number(ca['DW_AT_data_member_location']) * 8
                elif union:
                    offset = 0
                else:
                    raise ValueError('Missing DWARF graph member offset')
                members.append({'name': name(ca), 'type': ref(ca), 'offset': offset,
                                'bits': number(ca.get('DW_AT_bit_size', '0'))})
            row.update(kind='union' if union else 'struct', members=members,
                       size=number(attrs['DW_AT_byte_size']) if 'DW_AT_byte_size' in attrs else None)
            if row['name']:
                names.setdefault(row['name'], []).append(ident)
        elif tag == 'DW_TAG_base_type':
            code = number(attrs.get('DW_AT_encoding', '0'))
            encoding = {2: 'bool', 4: 'float', 5: 'signed', 6: 'signed', 7: 'unsigned', 8: 'unsigned'}.get(code)
            if encoding is None:
                raise ValueError('Unsupported DWARF scalar encoding')
            size = number(attrs['DW_AT_byte_size'])
            row.update(kind='scalar', size=size, encoding=encoding, bits=size * 8)
        elif tag in ('DW_TAG_pointer_type', 'DW_TAG_const_type', 'DW_TAG_volatile_type', 'DW_TAG_restrict_type', 'DW_TAG_typedef'):
            row.update(kind={'DW_TAG_pointer_type': 'pointer', 'DW_TAG_const_type': 'const',
                             'DW_TAG_volatile_type': 'volatile', 'DW_TAG_restrict_type': 'restrict',
                             'DW_TAG_typedef': 'typedef'}[tag], type=ref(attrs))
            if tag == 'DW_TAG_pointer_type':
                row['size'] = number(attrs.get('DW_AT_byte_size', '8'))
        elif tag == 'DW_TAG_array_type':
            dimensions = []
            for child in children:
                if child['tag'] != 'DW_TAG_subrange_type':
                    continue
                ca = child['attrs']
                lower = number(ca.get('DW_AT_lower_bound', '0'))
                if lower:
                    raise ValueError('Nonzero array lower bound unsupported')
                count = number(ca['DW_AT_count']) if 'DW_AT_count' in ca else number(ca['DW_AT_upper_bound']) + 1 if 'DW_AT_upper_bound' in ca else 0
                dimensions.append(count)
            if not dimensions:
                raise ValueError('Missing array dimensions')
            row.update(kind='array', type=ref(attrs), dimensions=dimensions)
        elif tag == 'DW_TAG_enumeration_type':
            values = [(name(child['attrs']), number(child['attrs']['DW_AT_const_value']))
                      for child in children if child['tag'] == 'DW_TAG_enumerator']
            row.update(kind='enum', size=number(attrs['DW_AT_byte_size']), values=values)
        elif tag in ('DW_TAG_subroutine_type', 'DW_TAG_subprogram'):
            args = [ref(child['attrs']) for child in children if child['tag'] == 'DW_TAG_formal_parameter']
            if (any(arg is None for arg in args) or
                    'DW_AT_specification' in attrs or 'DW_AT_abstract_origin' in attrs):
                # An inherited/abstract declaration cannot be mistaken for void(void).
                row.update(kind='incomplete-function')
            else:
                row.update(kind='function', args=args, variadic=any(child['tag'] == 'DW_TAG_unspecified_parameters' for child in children))
                row['return'] = ref(attrs)
            if row['name'] and tag == 'DW_TAG_subprogram':
                functions.setdefault(row['name'], []).append(ident)
        records[ident] = row
    return TypeGraph(records, names, functions)


def compare_graphs(official, objects, types, functions):
    def compare(name, method):
        local, providers, errors = [], [], []
        for graphs, output in ((objects, local), (official, providers)):
            for graph in graphs:
                try:
                    for value in getattr(graph, method)(name):
                        if value not in output:
                            output.append(value)
                except ValueError as error:
                    errors.append(str(error))
        if errors:
            status = 'incomplete-type-information'
        elif not local:
            status = 'missing-object-information'
        elif not providers:
            status = 'missing-official-information'
        elif len(local) != 1:
            status = 'conflicting-object-information'
        else:
            status = 'matched' if local[0] in providers else 'type-or-signature-mismatch'
        return {'name': name, 'status': status, 'errors': errors, 'object': local, 'official': providers}

    type_rows = [compare(name, 'named_aggregate') for name in sorted(types)]
    function_rows = [compare(name, 'named_function') for name in sorted(functions)]
    return {'member_types': type_rows, 'function_signatures': function_rows,
            'member_types_scope_verified': bool(type_rows) and all(row['status'] == 'matched' for row in type_rows),
            'function_signatures_scope_verified': bool(function_rows) and all(row['status'] == 'matched' for row in function_rows),
            'complete_type_graph_verified': False, 'complete_abi_verified': False,
            'hardware_ready': False}


def audit(specification, work, component):
    # Reuse the original layout spec checks and immutable object/type scope.
    layout_report = shape.audit(specification, work, component)
    base = specification['official']
    raw = shape.official_blob(Path(base['kernel']), base['kernel_sha256'], Path(base['map']),
                              base['map_sha256'], base['image_sha256'])
    official_base = btf_graph(raw)
    official = [official_base]
    for item in specification.get('split_btf', []):
        official.append(btf_graph(shape.materials.pinned_read(Path(item['path']), item['sha256']), official_base))
    objects = []
    for name in shape.SCOPES[component]['objects'] + (['iris_firmware.o'] if component == 'iris' else []):
        path = work / name
        before = shape.materials.read_regular(path)
        result = subprocess.run([shape.READ_ELF, '--debug-dump=info', str(path)], capture_output=True,
                                text=True, timeout=90, env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode or result.stderr.strip() or not result.stdout.strip():
            raise ValueError('DWARF type/signature inspection failed')
        if before != shape.materials.read_regular(path):
            raise ValueError('Object changed during type inspection')
        objects.append(dwarf_graph(result.stdout))
    report = compare_graphs(official, objects, shape.SCOPES[component]['types'], FUNCTIONS[component])
    report.update(component=component, layout_scope_verified=layout_report['layout_scope_verified'],
                  named_pointer_targets_only=True, all_imported_function_signatures_verified=False)
    return report
