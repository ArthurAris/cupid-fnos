#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic tests for the complete shared-type ABI comparator."""
import importlib.util
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('abi_complete', HERE / 'abi-complete.py')
ABI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ABI)


def graph(records):
    return ABI.SIG.TypeGraph(records, {}, {})


def scalar(size=4, encoding='unsigned', bits=None):
    return {'kind': 'scalar', 'size': size, 'encoding': encoding,
            'bits': size * 8 if bits is None else bits}


class AbiComparatorTests(unittest.TestCase):
    def test_identical_nested_structs_match(self):
        local = graph({
            1: {'kind': 'struct', 'name': 'outer', 'size': 16, 'members': [
                {'name': 'a', 'type': 2, 'offset': 0, 'bits': 0},
                {'name': 'b', 'type': 3, 'offset': 64, 'bits': 0}]},
            2: scalar(),
            3: {'kind': 'pointer', 'type': 4, 'size': 8},
            4: {'kind': 'struct', 'name': 'inner', 'size': 4, 'members': [
                {'name': 'v', 'type': 2, 'offset': 0, 'bits': 0}]},
        })
        official = graph({
            10: {'kind': 'struct', 'name': 'outer', 'size': 16, 'members': [
                {'name': 'a', 'type': 11, 'offset': 0, 'bits': 0},
                {'name': 'b', 'type': 12, 'offset': 64, 'bits': 0}]},
            11: scalar(),
            12: {'kind': 'pointer', 'type': 13, 'size': 8},
            13: {'kind': 'struct', 'name': 'inner', 'size': 4, 'members': [
                {'name': 'v', 'type': 11, 'offset': 0, 'bits': 0}]},
        })
        self.assertEqual(ABI.compare_type(local, 1, official, 10)[0], True)

    def test_member_offset_mismatch(self):
        local = graph({1: {'kind': 'struct', 'name': 's', 'size': 8, 'members': [
            {'name': 'a', 'type': 2, 'offset': 32, 'bits': 0}]}, 2: scalar()})
        official = graph({1: {'kind': 'struct', 'name': 's', 'size': 8, 'members': [
            {'name': 'a', 'type': 2, 'offset': 0, 'bits': 0}]}, 2: scalar()})
        equal, reason = ABI.compare_type(local, 1, official, 1)
        self.assertFalse(equal)
        self.assertIn('offset', reason)

    def test_member_bit_width_mismatch(self):
        local = graph({1: {'kind': 'struct', 'name': 's', 'size': 4, 'members': [
            {'name': 'a', 'type': 2, 'offset': 0, 'bits': 3}]}, 2: scalar(bits=32)})
        official = graph({1: {'kind': 'struct', 'name': 's', 'size': 4, 'members': [
            {'name': 'a', 'type': 2, 'offset': 0, 'bits': 4}]}, 2: scalar(bits=32)})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_nested_pointer_target_mismatch(self):
        local = graph({
            1: {'kind': 'pointer', 'type': 2, 'size': 8},
            2: {'kind': 'struct', 'name': 't', 'size': 4, 'members': [
                {'name': 'a', 'type': 3, 'offset': 0, 'bits': 0}]},
            3: scalar()})
        official = graph({
            1: {'kind': 'pointer', 'type': 2, 'size': 8},
            2: {'kind': 'struct', 'name': 't', 'size': 8, 'members': [
                {'name': 'a', 'type': 3, 'offset': 0, 'bits': 0}]},
            3: scalar(size=8)})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_pointer_to_incomplete_target_is_opaque(self):
        local = graph({1: {'kind': 'pointer', 'type': 2, 'size': 8},
                       2: {'kind': 'struct', 'name': 'opaque', 'size': None, 'members': []}})
        official = graph({1: {'kind': 'pointer', 'type': 2, 'size': 8},
                          2: {'kind': 'struct', 'name': 'opaque', 'size': 64,
                              'members': [{'name': 'x', 'type': 3, 'offset': 0, 'bits': 0}]},
                          3: scalar()})
        self.assertIsNone(ABI.compare_type(local, 1, official, 1)[0])

    def test_enum_value_mismatch(self):
        local = graph({1: {'kind': 'enum', 'name': 'e', 'size': 4,
                           'values': [('A', 0), ('B', 2)]}})
        official = graph({1: {'kind': 'enum', 'name': 'e', 'size': 4,
                              'values': [('A', 0), ('B', 1)]}})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_array_element_count_mismatch(self):
        local = graph({1: {'kind': 'array', 'type': 2, 'dimensions': [3]}, 2: scalar()})
        official = graph({1: {'kind': 'array', 'type': 2, 'dimensions': [4]}, 2: scalar()})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_multidimensional_and_flat_arrays_match(self):
        local = graph({1: {'kind': 'array', 'type': 2, 'dimensions': [2, 7]}, 2: scalar()})
        official = graph({1: {'kind': 'array', 'type': 2, 'dimensions': [14]}, 2: scalar()})
        self.assertTrue(ABI.compare_type(local, 1, official, 1)[0])

    def test_function_argument_mismatch(self):
        local = graph({1: {'kind': 'function', 'return': 2, 'args': [3], 'variadic': False},
                       2: scalar(), 3: {'kind': 'pointer', 'type': 2, 'size': 8}})
        official = graph({1: {'kind': 'function', 'return': 2, 'args': [3, 2], 'variadic': False},
                          2: scalar(), 3: {'kind': 'pointer', 'type': 2, 'size': 8}})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_recursive_types_terminate_and_match(self):
        local = graph({
            1: {'kind': 'struct', 'name': 'node', 'size': 16, 'members': [
                {'name': 'next', 'type': 2, 'offset': 0, 'bits': 0}]},
            2: {'kind': 'pointer', 'type': 1, 'size': 8}})
        official = graph({
            10: {'kind': 'struct', 'name': 'node', 'size': 16, 'members': [
                {'name': 'next', 'type': 11, 'offset': 0, 'bits': 0}]},
            11: {'kind': 'pointer', 'type': 10, 'size': 8}})
        self.assertTrue(ABI.compare_type(local, 1, official, 10)[0])

    def test_kind_mismatch(self):
        local = graph({1: {'kind': 'struct', 'name': 's', 'size': 4, 'members': []}})
        official = graph({1: {'kind': 'union', 'name': 's', 'size': 4, 'members': []}})
        self.assertFalse(ABI.compare_type(local, 1, official, 1)[0])

    def test_missing_type_reference_fails(self):
        local = graph({1: {'kind': 'pointer', 'type': 99, 'size': 8}})
        official = graph({1: {'kind': 'pointer', 'type': 2, 'size': 8}, 2: scalar()})
        with self.assertRaises(ValueError):
            ABI.compare_type(local, 1, official, 1)

    def test_void_pointer_is_not_typed_pointer(self):
        local = graph({1: {'kind': 'pointer', 'size': 8, 'type': 0}})
        official = graph({1: {'kind': 'pointer', 'size': 8, 'type': 2}, 2: scalar()})
        self.assertIs(ABI.compare_type(local, 1, official, 1)[0], False)

    def test_pointer_qualifiers_are_preserved(self):
        for qualifier in ('const', 'volatile', 'restrict'):
            with self.subTest(qualifier=qualifier):
                local = graph({1: {'kind': 'pointer', 'size': 8, 'type': 2},
                               2: {'kind': qualifier, 'type': 3}, 3: scalar()})
                official = graph({1: {'kind': 'pointer', 'size': 8, 'type': 2}, 2: scalar()})
                self.assertIs(ABI.compare_type(local, 1, official, 1)[0], False)

    def test_incomplete_function_is_unknown(self):
        local = graph({1: {'kind': 'incomplete-function'}})
        self.assertIsNone(ABI.compare_type(local, 1, local, 1)[0])

    def test_different_opaque_names_do_not_match(self):
        local = graph({1: {'kind': 'struct', 'name': 'first', 'size': None}})
        official = graph({1: {'kind': 'struct', 'name': 'second', 'size': None}})
        self.assertIs(ABI.compare_type(local, 1, official, 1)[0], False)

    def test_missing_member_type_is_unknown(self):
        local = graph({1: {'kind': 'struct', 'name': 's', 'size': 4,
                           'members': [{'name': 'a', 'offset': 0, 'bits': 0, 'type': None}]}})
        self.assertIsNone(ABI.compare_type(local, 1, local, 1)[0])

    def test_bad_local_variant_cannot_hide_behind_good_one(self):
        local = graph({1: scalar(), 2: scalar(encoding='signed')})
        official = graph({1: scalar()})
        result = ABI.compare_variants([(local, 1), (local, 2)], [(official, 1)])
        self.assertEqual(result['status'], 'mismatch')

    def test_unknown_local_variant_cannot_hide_behind_good_one(self):
        local = graph({1: {'kind': 'function', 'return': 0, 'args': [], 'variadic': False},
                       2: {'kind': 'incomplete-function'}})
        result = ABI.compare_variants([(local, 1), (local, 2)], [(local, 1)])
        self.assertEqual(result['status'], 'unknown')

    def test_all_objects_are_taken_from_builder(self):
        self.assertEqual(len(ABI.BUILD.component_objects('iris')), 27)
        self.assertIn('iris_hfi_gen2_response.o', ABI.BUILD.component_objects('iris'))

    def test_kbuild_object_omission_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            name = ABI.BUILD.PINS['iris']['module']
            (work / (name + '.mod')).write_text('./iris_probe.o\n')
            with self.assertRaisesRegex(ValueError, 'object list'):
                ABI.checked_objects(work, 'iris')

    def test_checked_kbuild_object_list(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            name = ABI.BUILD.PINS['iris']['module']
            (work / (name + '.mod')).write_text(''.join('./' + value + '\n'
                for value in ABI.BUILD.component_objects('iris')))
            (work / ('.' + name + '.o.cmd')).write_text('savedcmd_' + name +
                '.o := /usr/bin/ld -EL -r -o ' + name + '.o @' + name + '.mod\n')
            objects, digest = ABI.checked_objects(work, 'iris')
            self.assertEqual(objects, ABI.BUILD.component_objects('iris'))
            self.assertEqual(len(digest), 64)

    def test_missing_local_signature_never_complete(self):
        local = graph({1: scalar()})
        local.source, local.sha256, local.object_list_sha256 = 'obj.o', 'a' * 64, 'b' * 64
        official = graph({1: scalar(), 2: {'kind': 'function', 'return': 0,
                                         'args': [], 'variadic': False}})
        official.functions = {'missing':[2]}
        symbol = {'name': 'missing', 'binding': 'GLOBAL', 'kind': 'NOTYPE', 'index': 'UND'}
        with mock.patch.object(ABI, 'graph_objects', return_value=[local]), \
             mock.patch.object(ABI, 'official_graphs', return_value=[official]), \
             mock.patch.object(ABI, 'private_type_names', return_value=set()), \
             mock.patch.object(ABI, 'import_symbols', return_value={'missing': symbol}):
            result = ABI.audit({}, Path('/test'), 'iris')
        self.assertFalse(result['all_import_signatures_verified'])
        self.assertFalse(result['complete_abi_verified'])
        self.assertEqual(result['imports_missing_local_signature'], ['missing'])
        self.assertEqual(result['imports_crc_only'], [])

    def test_symbol_table_truncation_refused(self):
        output = ("Symbol table '.symtab' contains 2 entries:\n"
                  'Num: Value Size Type Bind Vis Ndx Name\n'
                  '0: 0000000000000000 0 NOTYPE LOCAL DEFAULT UND\n')
        with self.assertRaisesRegex(RuntimeError, 'Incomplete'):
            ABI.BUILD.symbol_records(output)

    def test_ambiguous_official_variants_are_unknown(self):
        local = graph({1: scalar()})
        providers = graph({1: scalar(), 2: scalar(encoding='signed')})
        result = ABI.compare_variants([(local, 1)], [(providers, 1), (providers, 2)])
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('ambiguous-official-type', result['reason'])

    def test_graph_budget_is_unknown_not_verified(self):
        local = graph({1: {'kind': 'struct', 'name': 's', 'size': 4,
                           'members': [{'name': 'v', 'offset': 0, 'bits': 0, 'type': 2}]},
                       2: scalar()})
        with mock.patch.object(ABI, 'MAX_GRAPH_PAIRS', 0):
            self.assertIsNone(ABI.compare_type(local, 1, local, 1)[0])

    def test_incomplete_cli_returns_nonzero_and_writes_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'report.json'
            with mock.patch.object(ABI.SHAPE.materials, 'pinned_read', return_value=b'{}'), \
                 mock.patch.object(ABI, 'audit', return_value=ABI.empty_report('iris')), \
                 mock.patch('builtins.print'):
                result = ABI.main(['--layout-spec', 'unused', '--layout-spec-sha256', 'a' * 64,
                                  '--work', 'unused', '--component', 'iris', '--out', str(output)])
            self.assertNotEqual(result, 0)
            self.assertIn('complete_abi_verified', output.read_text())

    def test_nested_unknown_has_actionable_root_category(self):
        local = graph({1: {'kind': 'pointer', 'size': 8, 'type': 2},
                       2: {'kind': 'struct', 'name': 'opaque', 'size': None, 'members': []}})
        result = ABI.compare_variants([(local, 1)], [(local, 1)])
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('opaque-pointer', result['unknown_categories'])
        self.assertTrue(result['root_causes'])

    def test_conflicting_symbol_kinds_are_never_verified(self):
        local = graph({1: {'kind': 'function', 'return': 0, 'args': [], 'variadic': False},
                       2: scalar()})
        local.source, local.sha256, local.object_list_sha256 = 'obj.o', 'a' * 64, 'b' * 64
        local.functions = {'collision': [1]}
        local.variables, local.variable_types = {'collision': [2]}, {2: 2}
        official = graph(dict(local.records))
        official.functions = {'collision': [1]}
        symbol = {'name': 'collision', 'binding': 'GLOBAL', 'kind': 'NOTYPE', 'index': 'UND'}
        with mock.patch.object(ABI, 'graph_objects', return_value=[local]), \
             mock.patch.object(ABI, 'official_graphs', return_value=[official]), \
             mock.patch.object(ABI, 'private_type_names', return_value=set()), \
             mock.patch.object(ABI, 'import_symbols', return_value={'collision': symbol}):
            report = ABI.audit({}, Path('/test'), 'iris')
        self.assertEqual(report['symbols'][0]['symbol_kind'], 'conflicting')
        self.assertFalse(report['all_import_signatures_verified'])
        self.assertFalse(report['complete_abi_verified'])

    def test_notype_data_requires_independent_layout_evidence(self):
        local = graph({1: scalar()})
        local.source, local.sha256, local.object_list_sha256 = 'obj.o', 'a' * 64, 'b' * 64
        local.variables, local.variable_types = {'variable': [1]}, {1: 1}
        official = graph({1: scalar()})
        symbol = {'name': 'variable', 'binding': 'GLOBAL', 'kind': 'NOTYPE', 'index': 'UND'}
        with mock.patch.object(ABI, 'graph_objects', return_value=[local]), \
             mock.patch.object(ABI, 'official_graphs', return_value=[official]), \
             mock.patch.object(ABI, 'private_type_names', return_value=set()), \
             mock.patch.object(ABI, 'import_symbols', return_value={'variable': symbol}):
            report = ABI.audit({}, Path('/test'), 'iris')
        self.assertEqual(report['symbols'][0]['symbol_kind'], 'data')
        self.assertFalse(report['complete_abi_verified'])
        self.assertEqual(report['unknown_summary']['counts']['data-symbol'], 1)

    def test_split_function_provider_retains_split_origin(self):
        base = graph({1: {'kind': 'function', 'return': 0, 'args': [], 'variadic': False}})
        base.next_id = 2
        base.source = 'vmlinux'
        split = graph({1: dict(base.records[1]), 2: {'kind': 'function-alias', 'name': 'split_fn', 'type': 1}})
        split.next_id, split.base, split.source = 3, base, 'split.btf'
        rows = ABI.named_variants([base, split], 'split_fn', True)
        self.assertEqual(len(rows), 1)
        self.assertIs(rows[0][0], split)
        self.assertEqual(rows[0][1], 1)

    def test_unknown_counts_overlap_is_explicit(self):
        report = ABI.empty_report('iris')
        report['symbol_crc_verified'] = True
        report['symbols'] = [{'name': 'missing', **ABI.compare_variants([], [])}]
        ABI.summarize_unknowns(report)
        summary = report['unknown_summary']
        self.assertEqual(summary['unknown_items'], 1)
        self.assertEqual(summary['counts']['missing-signature'], 1)
        self.assertEqual(summary['counts']['missing-btf'], 1)
        self.assertIn('overlap', summary['count_unit'])

    def test_private_type_detection(self):
        with tempfile.TemporaryDirectory(prefix='abi-private-') as directory:
            work = Path(directory)
            (work / 'iris_private.h').write_text('struct iris_private { int x; };\n'
                                                 'enum iris_mode { IRIS_A, IRIS_B };\n')
            (work / 'other.c').write_text('union other_private { int y; };\n')
            with mock.patch.dict(ABI.BUILD.PINS['iris'], {'files': ['iris_private.h', 'other.c']}):
                names = ABI.private_type_names(work, 'iris')
            self.assertIn('iris_private', names)
            self.assertIn('iris_mode', names)
            self.assertIn('other_private', names)
            self.assertNotIn('kernel_type', names)


if __name__ == '__main__':
    unittest.main(verbosity=2)
