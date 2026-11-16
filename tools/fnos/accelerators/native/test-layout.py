#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic BTF/DWARF tests; no proprietary type blobs or phone required."""
import importlib.util
from pathlib import Path
import struct
import subprocess
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('layout_under_test', HERE / 'layout.py')
layout = importlib.util.module_from_spec(spec)
spec.loader.exec_module(layout)


def btf_blob(size=8, member_offset=0):
    strings = b'\0example\0member\0'
    types = struct.pack('<III', 1, (4 << 24) | 1, size)
    types += struct.pack('<III', 9, 0, member_offset)
    header = struct.pack('<HBBIIIII', 0xeb9f, 1, 0, 24, 0, len(types), len(types), len(strings))
    return header + types + strings


def dwarf_output(size=8, offset=0):
    return (f' <1><10>: Abbrev Number: 1 (DW_TAG_structure_type)\n'
            f'    <11> DW_AT_name : example\n'
            f'    <12> DW_AT_byte_size : {size}\n'
            f' <2><20>: Abbrev Number: 2 (DW_TAG_member)\n'
            f'    <21> DW_AT_name : member\n'
            f'    <22> DW_AT_data_member_location : {offset}\n'
            f' <2><30>: Abbrev Number: 0\n')


class BtfTests(unittest.TestCase):
    def test_valid_struct(self):
        rows, strings, count = layout.btf_structs(btf_blob())
        self.assertEqual(rows['example'][0]['size'], 8)
        self.assertEqual(rows['example'][0]['members'][0]['bit_offset'], 0)
        self.assertEqual(count, 2)
        self.assertEqual(strings, b'\0example\0member\0')

    def test_truncated_header(self):
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            layout.btf_structs(btf_blob()[:12])

    def test_bad_magic(self):
        data = bytearray(btf_blob())
        data[0] ^= 1
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            layout.btf_structs(bytes(data))

    def test_truncated_type_body(self):
        data = bytearray(btf_blob())
        struct.pack_into('<I', data, 28, (4 << 24) | 5)
        with self.assertRaisesRegex(ValueError, 'Truncated'):
            layout.btf_structs(bytes(data))

    def test_bad_string_offset(self):
        data = bytearray(btf_blob())
        struct.pack_into('<I', data, 24, 10000)
        with self.assertRaisesRegex(ValueError, 'string offset'):
            layout.btf_structs(bytes(data))

    def test_unknown_type_kind(self):
        data = bytearray(btf_blob())
        struct.pack_into('<I', data, 28, 31 << 24)
        with self.assertRaisesRegex(ValueError, 'Unknown BTF'):
            layout.btf_structs(bytes(data))

    def test_overlapping_blocks(self):
        data = bytearray(btf_blob())
        struct.pack_into('<I', data, 16, 12)
        with self.assertRaisesRegex(ValueError, 'Overlapping'):
            layout.btf_structs(bytes(data))

    def test_member_offset_out_of_type(self):
        with self.assertRaisesRegex(ValueError, 'member outside'):
            layout.btf_structs(btf_blob(member_offset=80))

    def test_split_btf_uses_base_string_offsets(self):
        base = b'\0example\0member\0'
        types = struct.pack('<III', 1, (4 << 24) | 1, 8)
        types += struct.pack('<III', 9, 0, 0)
        data = struct.pack('<HBBIIIII', 0xeb9f, 1, 0, 24, 0, len(types), len(types), 0) + types
        rows, strings, count = layout.btf_structs(data, base, 11)
        self.assertEqual(rows['example'][0]['id'], 11)
        self.assertEqual(count, 12)
        self.assertEqual(strings, base)


class DwarfTests(unittest.TestCase):
    def test_valid_members(self):
        rows = layout.dwarf_structs(dwarf_output(), {'example'})
        self.assertEqual(rows['example'][0]['size'], 8)
        self.assertEqual(rows['example'][0]['members'][0]['name'], 'member')

    def test_missing_offset_refused(self):
        text = dwarf_output().replace('    <22> DW_AT_data_member_location : 0\n', '')
        with self.assertRaisesRegex(ValueError, 'Missing DWARF member'):
            layout.dwarf_structs(text, {'example'})

    def test_expression_offset_refused(self):
        text = dwarf_output().replace('DW_AT_data_member_location : 0', 'DW_AT_data_member_location : DW_OP_plus_uconst: 0')
        with self.assertRaisesRegex(ValueError, 'Unsupported DWARF'):
            layout.dwarf_structs(text, {'example'})

    def test_member_outside_type(self):
        with self.assertRaisesRegex(ValueError, 'outside type'):
            layout.dwarf_structs(dwarf_output(offset=9), {'example'})

    def test_duplicate_attributes(self):
        text = dwarf_output().replace('    <12> DW_AT_byte_size : 8\n', '    <12> DW_AT_byte_size : 8\n    <13> DW_AT_byte_size : 16\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            layout.dwarf_structs(text, {'example'})

    def test_empty_dump_refused(self):
        with self.assertRaisesRegex(ValueError, 'No DWARF'):
            layout.dwarf_structs('not a dwarf dump', {'example'})

    def test_incomplete_type_not_match(self):
        text = dwarf_output().replace('    <12> DW_AT_byte_size : 8\n', '')
        self.assertEqual(layout.dwarf_structs(text, {'example'}), {})

    def test_inspector_nonzero_refused(self):
        with mock.patch.object(layout.materials, 'read_regular', return_value=b'object'):
            with mock.patch.object(layout.subprocess, 'run', return_value=subprocess.CompletedProcess(['readelf'], 1, dwarf_output(), 'partial')):
                with self.assertRaisesRegex(ValueError, 'failed'):
                    layout.inspect_dwarf(Path('fixture.o'), {'example'})

    def test_inspector_stderr_refused(self):
        with mock.patch.object(layout.materials, 'read_regular', return_value=b'object'):
            with mock.patch.object(layout.subprocess, 'run', return_value=subprocess.CompletedProcess(['readelf'], 0, dwarf_output(), 'warning')):
                with self.assertRaisesRegex(ValueError, 'failed'):
                    layout.inspect_dwarf(Path('fixture.o'), {'example'})


class ComparisonTests(unittest.TestCase):
    def setUp(self):
        self.official, _strings, _count = layout.btf_structs(btf_blob())
        self.object = layout.dwarf_structs(dwarf_output(), {'example'})

    def compare(self, obj=None, wanted=None, official=None):
        return layout.compare_layouts(self.official if official is None else official,
                                      [self.object if obj is None else obj],
                                      {'example'} if wanted is None else wanted)

    def test_scoped_match_not_complete_abi(self):
        result = self.compare()
        self.assertTrue(result['layout_scope_verified'])
        self.assertFalse(result['complete_abi_verified'])
        self.assertFalse(result['hardware_ready'])
        self.assertFalse(result['function_signatures_verified'])

    def test_missing_object_type(self):
        result = self.compare(obj={})
        self.assertEqual(result['types'][0]['status'], 'missing-object-type')
        self.assertFalse(result['layout_scope_verified'])

    def test_missing_official_type(self):
        result = self.compare(official={})
        self.assertEqual(result['types'][0]['status'], 'missing-official-type')

    def test_wrong_size(self):
        obj = layout.dwarf_structs(dwarf_output(size=16), {'example'})
        self.assertEqual(self.compare(obj)['types'][0]['status'], 'layout-mismatch')

    def test_wrong_member_offset(self):
        obj = layout.dwarf_structs(dwarf_output(offset=4), {'example'})
        self.assertEqual(self.compare(obj)['types'][0]['status'], 'layout-mismatch')

    def test_conflicting_object_shapes(self):
        other = layout.dwarf_structs(dwarf_output(size=16), {'example'})
        result = layout.compare_layouts(self.official, [self.object, other], {'example'})
        self.assertEqual(result['types'][0]['status'], 'conflicting-object-layouts')

    def test_unknown_member_name_not_equivalent(self):
        obj = layout.dwarf_structs(dwarf_output().replace('DW_AT_name : member', 'DW_AT_name : different'), {'example'})
        self.assertEqual(self.compare(obj)['types'][0]['status'], 'layout-mismatch')

    def test_empty_scope_refused(self):
        with self.assertRaisesRegex(ValueError, 'Nonempty'):
            self.compare(wanted=set())

    def test_boolean_schema_refused(self):
        with self.assertRaisesRegex(ValueError, 'Invalid private'):
            layout.audit({'schema': True}, Path('fixture'), 'fastrpc')

    def test_caller_cannot_weaken_scope(self):
        specification = {'schema': 1, 'official': {'kernel': 'x', 'kernel_sha256': 'x',
                         'map': 'x', 'map_sha256': 'x', 'image_sha256': 'x'},
                         'components': {'fastrpc': {'types': [], 'objects': []}}}
        with mock.patch.object(layout, 'official_btf', return_value=(self.official, b'\0', 2)):
            with self.assertRaisesRegex(ValueError, 'changes required layout scope'):
                layout.audit(specification, Path('fixture'), 'fastrpc')


if __name__ == '__main__':
    unittest.main(verbosity=2)
