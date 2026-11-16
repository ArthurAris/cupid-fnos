#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Type and signature refusal tests using small synthetic graphs only."""
import importlib.util
from pathlib import Path
import struct
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('signature_check', HERE / 'type-signatures.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def graph(encoding='signed', qualifier=None, pointer=False, args=None, variadic=False):
    records = {
        1: {'kind': 'scalar', 'size': 4, 'encoding': encoding, 'bits': 32},
        2: {'kind': 'struct', 'name': 'sample', 'size': 4,
            'members': [{'name': 'value', 'offset': 0, 'bits': 0, 'type': 1}]},
        3: {'kind': 'function', 'return': 1, 'args': [1] if args is None else args,
            'variadic': variadic},
    }
    if qualifier:
        records[4] = {'kind': qualifier, 'type': 1}
        records[2]['members'][0]['type'] = 4
    if pointer:
        records[4] = {'kind': 'pointer', 'size': 8, 'type': 1}
        records[2]['members'][0]['type'] = 4
    return check.TypeGraph(records, {'sample': [2]}, {'invoke': [3]})


class SignatureTests(unittest.TestCase):
    def compare(self, official, local):
        return check.compare_graphs([official], [local], ['sample'], ['invoke'])

    def test_equivalent_scopes_not_complete_abi(self):
        result = self.compare(graph(), graph())
        self.assertTrue(result['member_types_scope_verified'])
        self.assertTrue(result['function_signatures_scope_verified'])
        self.assertFalse(result['complete_abi_verified'])
        self.assertFalse(result['complete_type_graph_verified'])
        self.assertFalse(result['hardware_ready'])

    def test_same_shape_different_signedness(self):
        result = self.compare(graph(), graph(encoding='unsigned'))
        self.assertFalse(result['member_types_scope_verified'])
        self.assertFalse(result['function_signatures_scope_verified'])

    def test_same_shape_different_const_type(self):
        result = self.compare(graph(), graph(qualifier='const'))
        self.assertFalse(result['member_types_scope_verified'])

    def test_pointer_is_not_scalar(self):
        self.assertFalse(self.compare(graph(), graph(pointer=True))['member_types_scope_verified'])

    def test_function_argument_count(self):
        self.assertFalse(self.compare(graph(), graph(args=[1, 1]))['function_signatures_scope_verified'])

    def test_function_argument_type(self):
        local = graph()
        local.records[4] = {'kind': 'pointer', 'size': 8, 'type': 1}
        local.records[3]['args'] = [4]
        self.assertFalse(self.compare(graph(), local)['function_signatures_scope_verified'])

    def test_function_return_type(self):
        local = graph()
        local.records[3]['return'] = 0
        self.assertFalse(self.compare(graph(), local)['function_signatures_scope_verified'])

    def test_variadic_difference(self):
        self.assertFalse(self.compare(graph(), graph(variadic=True))['function_signatures_scope_verified'])

    def test_typedef_resolved(self):
        self.assertTrue(self.compare(graph(), graph(qualifier='typedef'))['member_types_scope_verified'])

    def test_void_member_type_refused(self):
        local = graph()
        local.records[2]['members'][0]['type'] = None
        self.assertFalse(self.compare(graph(), local)['member_types_scope_verified'])

    def test_missing_type_reference(self):
        local = graph()
        local.records[2]['members'][0]['type'] = 99
        result = self.compare(graph(), local)
        self.assertFalse(result['member_types_scope_verified'])
        self.assertEqual(result['member_types'][0]['status'], 'incomplete-type-information')

    def test_missing_official_function(self):
        official = graph()
        official.functions = {}
        result = self.compare(official, graph())
        self.assertFalse(result['function_signatures_scope_verified'])
        self.assertEqual(result['function_signatures'][0]['status'], 'missing-official-information')

    def test_missing_object_function(self):
        local = graph()
        local.functions = {}
        self.assertFalse(self.compare(graph(), local)['function_signatures_scope_verified'])

    def test_conflicting_object_types(self):
        result = check.compare_graphs([graph()], [graph(), graph(encoding='unsigned')], ['sample'], ['invoke'])
        self.assertEqual(result['member_types'][0]['status'], 'conflicting-object-information')

    def test_recursive_alias_refused(self):
        local = graph()
        local.records[4] = {'kind': 'typedef', 'type': 4}
        local.records[2]['members'][0]['type'] = 4
        self.assertFalse(self.compare(graph(), local)['member_types_scope_verified'])

    def test_named_pointer_target_identity(self):
        local = graph()
        local.records[4] = {'kind': 'pointer', 'size': 8, 'type': 2}
        self.assertEqual(local.type(4), ('pointer', 8, ('struct', 'sample')))

    def test_array_dimensions_compared(self):
        a, b = graph(), graph()
        for item, count in [(a, 2), (b, 3)]:
            item.records[4] = {'kind': 'array', 'type': 1, 'dimensions': [count]}
            item.records[2]['members'][0]['type'] = 4
        self.assertFalse(self.compare(a, b)['member_types_scope_verified'])

    def test_enum_values_compared(self):
        a, b = graph(), graph()
        for item, value in [(a, 1), (b, 2)]:
            item.records[4] = {'kind': 'enum', 'name': 'values', 'size': 4, 'values': [('KEY', value)]}
            item.records[2]['members'][0]['type'] = 4
        self.assertFalse(self.compare(a, b)['member_types_scope_verified'])

    def test_empty_scopes_not_pass(self):
        result = check.compare_graphs([graph()], [graph()], [], [])
        self.assertFalse(result['member_types_scope_verified'])
        self.assertFalse(result['function_signatures_scope_verified'])


class DwarfGraphTests(unittest.TestCase):
    def fixture(self):
        return (' <1><10>: Abbrev Number: 1 (DW_TAG_base_type)\n'
                ' <11> DW_AT_name : int\n <12> DW_AT_byte_size : 4\n <13> DW_AT_encoding : 5 (signed)\n'
                ' <1><20>: Abbrev Number: 2 (DW_TAG_subprogram)\n'
                ' <21> DW_AT_name : invoke\n <22> DW_AT_type : <0x10>\n'
                ' <2><30>: Abbrev Number: 3 (DW_TAG_formal_parameter)\n'
                ' <31> DW_AT_type : <0x10>\n <2><40>: Abbrev Number: 0\n')

    def test_declaration_signature(self):
        g = check.dwarf_graph(self.fixture())
        self.assertEqual(g.named_function('invoke'), [('function', ('scalar', 4, 'signed', 32),
                                                      (('scalar', 4, 'signed', 32),), False)])

    def test_inherited_signature_not_treated_as_void(self):
        text = self.fixture().replace(' <22> DW_AT_type : <0x10>\n',
                                      ' <22> DW_AT_specification : <0x99>\n')
        g = check.dwarf_graph(text)
        with self.assertRaises(ValueError):
            g.named_function('invoke')

    def test_missing_argument_type_refused(self):
        text = self.fixture().replace(' <31> DW_AT_type : <0x10>\n', '')
        g = check.dwarf_graph(text)
        with self.assertRaises(ValueError):
            g.named_function('invoke')

    def test_numeric_expression_not_partially_accepted(self):
        text = self.fixture().replace('DW_AT_byte_size : 4', 'DW_AT_byte_size : 4 invalid-expression')
        with self.assertRaisesRegex(ValueError, 'graph number'):
            check.dwarf_graph(text)

    def test_bad_reference_refused(self):
        with self.assertRaisesRegex(ValueError, 'reference'):
            check.dwarf_graph(self.fixture().replace('<0x10>', '<external>'))

    def test_duplicate_die_refused(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate DWARF DIE'):
            check.dwarf_graph(self.fixture() + self.fixture())

    def test_empty_dump_refused(self):
        with self.assertRaisesRegex(ValueError, 'No DWARF'):
            check.dwarf_graph('empty')

    def test_duplicate_attribute_refused(self):
        text = self.fixture().replace(' <12> DW_AT_byte_size : 4\n', ' <12> DW_AT_byte_size : 4\n <14> DW_AT_byte_size : 8\n')
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            check.dwarf_graph(text)


class BtfGraphTests(unittest.TestCase):
    def fixture(self):
        strings = b'\0int\0sample\0value\0invoke\0arg\0'
        records = struct.pack('<III', 1, 1 << 24, 4) + struct.pack('<I', (1 << 24) | 32)
        records += struct.pack('<III', 5, (4 << 24) | 1, 4) + struct.pack('<III', 12, 1, 0)
        records += struct.pack('<III', 0, (13 << 24) | 1, 1) + struct.pack('<II', 25, 1)
        records += struct.pack('<III', 18, 12 << 24, 3)
        return struct.pack('<HBBIIIII', 0xeb9f, 1, 0, 24, 0, len(records), len(records), len(strings)) + records + strings

    def test_parses_member_type_and_function(self):
        g = check.btf_graph(self.fixture())
        self.assertEqual(g.named_aggregate('sample')[0][3][0][3], ('scalar', 4, 'signed', 32))
        self.assertEqual(g.named_function('invoke')[0][0], 'function')

    def test_bad_member_type_refused(self):
        data = bytearray(self.fixture())
        struct.pack_into('<I', data, 24 + 16 + 12 + 4, 99)
        g = check.btf_graph(bytes(data))
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            g.named_aggregate('sample')

    def test_truncation_refused(self):
        with self.assertRaises(ValueError):
            check.btf_graph(self.fixture()[:35])


if __name__ == '__main__':
    unittest.main(verbosity=2)
