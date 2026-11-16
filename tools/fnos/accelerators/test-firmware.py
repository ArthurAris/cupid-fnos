#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic tests only: no vendor firmware, device or installed SDK needed."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('firmware_check', HERE / 'verify-firmware.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def cell(*values):
    return struct.pack('>' + 'I' * len(values), *values)


def fixture_dtb(handle=1, size=8192, duplicate_handle=False, firmware_node='remoteproc@32300000'):
    strings = bytearray()
    offsets = {}
    structure = bytearray()

    def begin(name):
        structure.extend(cell(1) + name.encode() + b'\0')
        structure.extend(b'\0' * (-len(structure) % 4))

    def end():
        structure.extend(cell(2))

    def prop(name, data):
        if name not in offsets:
            offsets[name] = len(strings)
            strings.extend(name.encode() + b'\0')
        structure.extend(cell(3, len(data), offsets[name]) + data)
        structure.extend(b'\0' * (-len(structure) % 4))

    begin('')
    prop('#address-cells', cell(2))
    prop('#size-cells', cell(2))
    begin('reserved-memory')
    prop('#address-cells', cell(2))
    prop('#size-cells', cell(2))
    prop('ranges', b'')
    begin('memory@90000000')
    prop('phandle', cell(1))
    prop('reg', cell(0, 0x90000000, 0, size))
    end()
    if duplicate_handle:
        begin('memory@91000000')
        prop('phandle', cell(1))
        prop('reg', cell(0, 0x91000000, 0, size))
        end()
    end()
    begin('soc@0')
    begin(firmware_node)
    prop('memory-region', cell(handle))
    end()
    end()
    end()
    structure.extend(cell(9))
    off_structure = 56
    off_strings = off_structure + len(structure)
    total = off_strings + len(strings)
    header = struct.pack('>10I', 0xd00dfeed, total, off_structure, off_strings, 40,
                         17, 16, 0, len(strings), len(structure))
    return header + b'\0' * 16 + structure + strings


def fixture_firmware(memsz=4100, flags=check.RELOCATABLE, paddr=0x80000000):
    header_size = 52 + 3 * 32
    header = bytearray(header_size)
    header[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HHIIIIIHHHHHH', header, 16,
                     2, 164, 1, paddr, 52, 0, 0, 52, 32, 3, 0, 0, 0)
    struct.pack_into('<8I', header, 52, 0, 0, 0, 0, header_size, 0, 0, 0)
    struct.pack_into('<8I', header, 84, 1, 4096, paddr, paddr, 4, memsz, flags | 5, 4096)
    struct.pack_into('<8I', header, 116, 0, 8192, 0, 0, 4, 4, check.TYPE_HASH, 4096)
    return {'cdsp.mdt': bytes(header) + b'hash', 'cdsp.b00': bytes(header),
            'cdsp.b01': b'data', 'cdsp.b02': b'hash'}


class FirmwareTests(unittest.TestCase):
    def setUp(self):
        self.files = fixture_firmware()
        self.region = {'base': 0x90000000, 'size': 8192, 'node': '/reserved-memory/memory@90000000'}

    def validate(self):
        return check.validate_mdt(self.files, 'cdsp', 164, self.region)

    def alter_header(self, offset, value, format='<I'):
        data = bytearray(self.files['cdsp.mdt'])
        struct.pack_into(format, data, offset, value)
        self.files['cdsp.mdt'] = bytes(data)
        self.files['cdsp.b00'] = bytes(data[:148])

    def test_valid_relocatable_firmware(self):
        report = self.validate()
        self.assertEqual(report['page_aligned_load_bytes'], 8192)
        self.assertEqual(report['aligned_slack_bytes'], 0)
        self.assertFalse(report['hardware_ready'])
        self.assertFalse(report['signature_verified'])

    def test_missing_segment(self):
        del self.files['cdsp.b01']
        with self.assertRaisesRegex(ValueError, 'Missing or wrong-length'):
            self.validate()

    def test_wrong_segment_size(self):
        self.files['cdsp.b01'] += b'x'
        with self.assertRaisesRegex(ValueError, 'wrong-length'):
            self.validate()

    def test_hash_metadata_mismatch(self):
        self.files['cdsp.b02'] = b'bad!'
        with self.assertRaisesRegex(ValueError, 'hash metadata'):
            self.validate()

    def test_header_mismatch(self):
        self.files['cdsp.b00'] = b'bad'
        with self.assertRaisesRegex(ValueError, 'header differs'):
            self.validate()

    def test_wrong_isa(self):
        self.alter_header(18, 183, '<H')
        with self.assertRaisesRegex(ValueError, 'ELF ABI'):
            self.validate()

    def test_wrong_endian(self):
        data = bytearray(self.files['cdsp.mdt'])
        data[5] = 2
        self.files['cdsp.mdt'] = bytes(data)
        with self.assertRaisesRegex(ValueError, 'little-endian'):
            self.validate()

    def test_truncated_program_table(self):
        self.files['cdsp.mdt'] = self.files['cdsp.mdt'][:90]
        with self.assertRaisesRegex(ValueError, 'program table'):
            self.validate()

    def test_bad_program_entry_size(self):
        self.alter_header(42, 31, '<H')
        with self.assertRaisesRegex(ValueError, 'program table'):
            self.validate()

    def test_filesz_exceeds_memory(self):
        self.alter_header(84 + 20, 3)
        with self.assertRaisesRegex(ValueError, 'filesz exceeds'):
            self.validate()

    def test_address_overflow(self):
        self.alter_header(84 + 12, 0xfffffffe)
        with self.assertRaisesRegex(ValueError, 'overflow'):
            self.validate()

    def test_invalid_alignment(self):
        self.alter_header(84 + 28, 3)
        with self.assertRaisesRegex(ValueError, 'alignment'):
            self.validate()

    def test_nonrelocatable_outside_memory(self):
        self.files = fixture_firmware(flags=0)
        with self.assertRaisesRegex(ValueError, 'outside resolved'):
            self.validate()

    def test_nonrelocatable_valid(self):
        self.files = fixture_firmware(flags=0, paddr=self.region['base'])
        self.assertFalse(self.validate()['relocatable'])

    def test_memory_too_small(self):
        self.region['size'] = 4096
        with self.assertRaisesRegex(ValueError, 'outside resolved'):
            self.validate()

    def test_page_aligned_span_required(self):
        self.region['size'] = 4100
        with self.assertRaisesRegex(ValueError, 'Page-aligned'):
            self.validate()

    def test_hash_segment_missing(self):
        self.alter_header(116 + 24, 0)
        with self.assertRaisesRegex(ValueError, 'hash metadata'):
            self.validate()

    def test_duplicate_hash_segment(self):
        self.alter_header(84 + 24, check.TYPE_HASH)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            self.validate()

    def test_full_image_mismatch(self):
        self.files['cdsp.mbn'] = self.files['cdsp.mdt'] + b'\0' * 9000
        with self.assertRaisesRegex(ValueError, 'Full MBN differs'):
            self.validate()


class DeviceTreeTests(unittest.TestCase):
    def test_resolve_actual_phandle(self):
        nodes = check.parse_dtb(fixture_dtb())
        region = check.memory_region(nodes, '/soc@0/remoteproc@32300000')
        self.assertEqual(region['base'], 0x90000000)
        self.assertEqual(region['size'], 8192)

    def test_missing_phandle(self):
        nodes = check.parse_dtb(fixture_dtb(handle=2))
        with self.assertRaisesRegex(ValueError, 'does not reference'):
            check.memory_region(nodes, '/soc@0/remoteproc@32300000')

    def test_duplicate_phandle(self):
        nodes = check.parse_dtb(fixture_dtb(duplicate_handle=True))
        with self.assertRaisesRegex(ValueError, 'Duplicate FDT phandle'):
            check.memory_region(nodes, '/soc@0/remoteproc@32300000')

    def test_unaligned_memory(self):
        nodes = check.parse_dtb(fixture_dtb(size=8191))
        with self.assertRaisesRegex(ValueError, 'aligned'):
            check.memory_region(nodes, '/soc@0/remoteproc@32300000')

    def test_truncated_fdt(self):
        with self.assertRaises(ValueError):
            check.parse_dtb(fixture_dtb()[:-1])

    def test_fdt_bad_offsets(self):
        data = bytearray(fixture_dtb())
        struct.pack_into('>I', data, 8, 0xfffffff0)
        with self.assertRaisesRegex(ValueError, 'bounds'):
            check.parse_dtb(bytes(data))

    def test_unknown_fdt_token(self):
        data = bytearray(fixture_dtb())
        struct.pack_into('>I', data, 56, 6)
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            check.parse_dtb(bytes(data))

    def test_nonidentity_ranges_refused(self):
        nodes = check.parse_dtb(fixture_dtb())
        nodes['/reserved-memory']['ranges'] = cell(0)
        with self.assertRaisesRegex(ValueError, 'identity'):
            check.memory_region(nodes, '/soc@0/remoteproc@32300000')

    def test_disabled_carveout_refused(self):
        nodes = check.parse_dtb(fixture_dtb())
        nodes['/reserved-memory/memory@90000000']['status'] = b'disabled\0'
        with self.assertRaisesRegex(ValueError, 'disabled'):
            check.memory_region(nodes, '/soc@0/remoteproc@32300000')


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='firmware-pins-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / 'firmware'
        self.directory.mkdir()
        self.dtb = self.root / 'board.dtb'
        self.dtb.write_bytes(fixture_dtb())
        self.dtb_sha = digest(self.dtb.read_bytes())
        self.manifest = self.directory / 'manifest.json'
        self.rows = []
        for name, data in fixture_firmware().items():
            (self.directory / name).write_bytes(data)
            self.rows.append({'name': name, 'size': len(data), 'sha256': digest(data)})
        self.doc = {'schema': 1, 'board': 'xiaomi,cupid', 'soc': 'sm8450', 'files': self.rows}
        self.save()

    def save(self):
        self.manifest.write_text(json.dumps(self.doc))
        self.manifest_sha = digest(self.manifest.read_bytes())

    def verify(self):
        return check.verify(self.manifest, self.manifest_sha, self.dtb, self.dtb_sha, 'cdsp')

    def test_valid_fixture(self):
        self.assertTrue(self.verify()['static_consistency_passed'])

    def test_equal_size_corruption_refused(self):
        (self.directory / 'cdsp.b01').write_bytes(b'evil')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.verify()

    def test_modified_manifest_refused(self):
        self.manifest.write_text('{}')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.verify()

    def test_modified_dtb_refused(self):
        self.dtb.write_bytes(fixture_dtb(size=4096))
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.verify()

    def test_extra_file_refused(self):
        (self.directory / 'extra').write_bytes(b'extra')
        with self.assertRaisesRegex(ValueError, 'Extra or missing'):
            self.verify()

    def test_manifest_nonobject_refused(self):
        self.doc = []
        self.save()
        with self.assertRaisesRegex(ValueError, 'schema or board'):
            self.verify()

    def test_wrong_board_refused(self):
        self.doc['board'] = 'other,board'
        self.save()
        with self.assertRaisesRegex(ValueError, 'board mismatch'):
            self.verify()

    def test_path_traversal_refused(self):
        self.rows[0]['name'] = '../escaped'
        self.save()
        with self.assertRaisesRegex(ValueError, 'file name'):
            self.verify()

    def test_duplicate_row_refused(self):
        self.rows.append(self.rows[0])
        self.save()
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.verify()

    def test_duplicate_json_key_refused(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            json.loads('{"files":[],"files":[]}', object_pairs_hook=check.unique_json)

    def test_fifo_refused_without_blocking(self):
        import os
        if not hasattr(os, 'mkfifo'):
            self.skipTest('FIFO unavailable')
        target = self.directory / 'cdsp.b01'
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(ValueError, 'Nonordinary'):
            self.verify()

    def test_symlink_refused(self):
        target = self.directory / 'cdsp.b01'
        copy = self.root / 'external'
        copy.write_bytes(target.read_bytes())
        target.unlink()
        try:
            target.symlink_to(copy)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            self.verify()


if __name__ == '__main__':
    unittest.main(verbosity=2)
