#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Synthetic firmware/DT/source fixtures; no proprietary firmware or device."""
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK = load('runtime_check', HERE / 'verify-firmware-runtime.py')
FIXTURES = load('synthetic_firmware', HERE / 'test-firmware.py')
HASH = CHECK.CHECK.TYPE_HASH
RELOCATABLE = CHECK.CHECK.RELOCATABLE


def digest(data):
    return hashlib.sha256(data).hexdigest()


def elf32(data, machine=94):
    header = bytearray(148)
    header[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HHIIIIIHHHHHH', header, 16,
                     2, machine, 1, 0x80000000, 52, 0, 0, 52, 32, 3, 0, 0, 0)
    struct.pack_into('<8I', header, 52, 0, 0, 0, 0, 148, 0, 0, 0)
    struct.pack_into('<8I', header, 84, 1, 4096, 0x80000000, 0x80000000,
                     len(data), 4096, RELOCATABLE | 5, 4096)
    struct.pack_into('<8I', header, 116, 0, 8192, 0, 0, 4, 4, HASH, 4096)
    return bytes(header)


def fixture(directory):
    root = Path(directory)
    firmware = root / 'firmware'
    firmware.mkdir()
    data = b'QC_IMAGE_VERSION_STRING=video-firmware.2.4-test\0HFI_CMD_INIT\0'
    header = elf32(data)
    mbn = bytearray(8196)
    mbn[:len(header)] = header
    mbn[4096:4096 + len(data)] = data
    mbn[8192:] = b'hash'
    files = {'vpu20_4v.mdt': header + b'hash', 'vpu20_4v.mbn': bytes(mbn),
             'vpu20_4v.b00': header, 'vpu20_4v.b01': data, 'vpu20_4v.b02': b'hash'}
    for name, payload in files.items():
        (firmware / name).write_bytes(payload)
    manifest = firmware / 'manifest.json'
    manifest.write_text(json.dumps({'schema': 1, 'board': 'xiaomi,cupid', 'soc': 'sm8450',
        'firmware_name': 'qcom/vpu/vpu20_4v.mbn', 'expected_segments': ['vpu20_4v.b01'],
        'files': [{'name': name, 'size': len(payload), 'sha256': digest(payload)}
                  for name, payload in files.items()]}))
    dtb = root / 'candidate.dtb'
    dtb.write_bytes(FIXTURES.fixture_dtb(size=8192, firmware_node='video-codec@aa00000'))
    source = root / 'driver.c'
    source.write_text('struct iris_platform_data sm8450_data = {\n'
                      '\t.fwname = "qcom/vpu/vpu20_4v.mbn",\n\t.pas_id = IRIS_PAS_ID,\n};\n')
    platform = root / 'platform.h'
    platform.write_text('#define IRIS_PAS_ID 9\n')
    hfi = root / 'hfi.h'
    hfi.write_text(''.join('#define HFI_CMD_' + name + ' ' + hex(0x1000001 + index) + '\n'
                          for index, name in enumerate(sorted(CHECK.REQUIRED_COMMANDS))) +
                   ''.join('#define HFI_PROP_' + name + ' ' + hex(0x3000001 + index) + '\n'
                           for index, name in enumerate(sorted(CHECK.REQUIRED_PROPERTIES))))
    inputs = root / 'inputs.json'
    inputs.write_text(json.dumps({'schema': 1, 'component': 'video', 'files': {
        role: {'path': str(path), 'sha256': digest(path.read_bytes())}
        for role, path in [('driver', source), ('platform_header', platform), ('hfi_header', hfi)]}}))
    return manifest, digest(manifest.read_bytes()), dtb, digest(dtb.read_bytes()), inputs, digest(inputs.read_bytes())


class RuntimePairingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='runtime-repair-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.args = list(fixture(self.root))

    def audit(self):
        return CHECK.audit(*self.args)

    def repin_manifest(self):
        manifest = json.loads(self.args[0].read_text())
        files = sorted(path for path in self.args[0].parent.iterdir() if path.name != 'manifest.json')
        manifest['files'] = [{'name': path.name, 'size': path.stat().st_size,
                              'sha256': digest(path.read_bytes())} for path in files]
        self.args[0].write_text(json.dumps(manifest))
        self.args[1] = digest(self.args[0].read_bytes())

    def repin_inputs(self):
        spec = json.loads(self.args[4].read_text())
        for row in spec['files'].values():
            row['sha256'] = digest(Path(row['path']).read_bytes())
        self.args[4].write_text(json.dumps(spec))
        self.args[5] = digest(self.args[4].read_bytes())

    def test_valid_static_structure_does_not_claim_pairing(self):
        report = self.audit()
        self.assertTrue(report['static_consistency_passed'])
        self.assertEqual(report['required_segments'], ['vpu20_4v.b01'])
        self.assertFalse(report['static_pairing_consistent'])
        self.assertFalse(report['runtime_pairing_verified'])
        self.assertFalse(report['signature_verified'])
        self.assertFalse(report['hardware_ready'])
        self.assertEqual(report['loader_provenance_status'], 'unknown')

    def test_manifest_hash_mismatch_refused(self):
        self.args[1] = '0' * 64
        with self.assertRaises(ValueError):
            self.audit()

    def test_duplicate_manifest_key_refused(self):
        self.args[0].write_text('{"schema":1,"schema":1}')
        self.args[1] = digest(self.args[0].read_bytes())
        with self.assertRaisesRegex(ValueError, 'Duplicate'):
            self.audit()

    def test_extra_file_refused(self):
        (self.args[0].parent / 'extra.bin').write_bytes(b'x')
        with self.assertRaises(ValueError):
            self.audit()

    def test_expected_list_cannot_reduce_elf_requirements(self):
        manifest = json.loads(self.args[0].read_text())
        manifest['expected_segments'] = ['vpu20_4v.b00']
        self.args[0].write_text(json.dumps(manifest))
        (self.args[0].parent / 'vpu20_4v.b01').unlink()
        self.repin_manifest()
        with self.assertRaisesRegex(ValueError, 'firmware segment'):
            self.audit()

    def test_expected_list_drift_refused(self):
        manifest = json.loads(self.args[0].read_text())
        manifest['expected_segments'] = ['vpu20_4v.b99']
        self.args[0].write_text(json.dumps(manifest))
        self.args[1] = digest(self.args[0].read_bytes())
        with self.assertRaises(ValueError):
            self.audit()

    def test_complete_flag_is_not_required_or_trusted(self):
        manifest = json.loads(self.args[0].read_text())
        manifest['complete'] = False
        self.args[0].write_text(json.dumps(manifest))
        self.args[1] = digest(self.args[0].read_bytes())
        self.assertTrue(self.audit()['static_consistency_passed'])

    def test_wrong_firmware_name_refused(self):
        manifest = json.loads(self.args[0].read_text())
        manifest['firmware_name'] = 'qcom/vpu/other.mbn'
        self.args[0].write_text(json.dumps(manifest))
        self.args[1] = digest(self.args[0].read_bytes())
        with self.assertRaises(ValueError):
            self.audit()

    def test_mbn_same_headers_corrupt_segment_refused(self):
        path = self.args[0].parent / 'vpu20_4v.mbn'
        payload = bytearray(path.read_bytes())
        payload[4096] ^= 1
        path.write_bytes(payload)
        self.repin_manifest()
        with self.assertRaisesRegex(ValueError, 'Full MBN differs'):
            self.audit()

    def test_hash_metadata_corruption_refused(self):
        (self.args[0].parent / 'vpu20_4v.b02').write_bytes(b'bad!')
        self.repin_manifest()
        with self.assertRaisesRegex(ValueError, 'hash metadata differs'):
            self.audit()

    def test_missing_hfi_definitions_refused(self):
        (self.root / 'hfi.h').write_text('/* no protocol constants */')
        self.repin_inputs()
        with self.assertRaisesRegex(ValueError, 'HFI definitions'):
            self.audit()

    def test_header_hash_drift_refused(self):
        (self.root / 'hfi.h').write_text('changed')
        with self.assertRaisesRegex(ValueError, 'SHA mismatch'):
            self.audit()

    def test_wrong_pas_refused(self):
        (self.root / 'platform.h').write_text('#define IRIS_PAS_ID 18\n')
        self.repin_inputs()
        with self.assertRaisesRegex(ValueError, 'PAS ID mismatch'):
            self.audit()

    def test_wrong_driver_pas_selection_refused(self):
        source = self.root / 'driver.c'
        source.write_text(source.read_text().replace('IRIS_PAS_ID', 'OTHER_PAS'))
        self.repin_inputs()
        with self.assertRaisesRegex(ValueError, 'PAS constant'):
            self.audit()

    def test_conflicting_versions_refused(self):
        with self.assertRaisesRegex(ValueError, 'Conflicting firmware identity'):
            CHECK.identity(['QC_IMAGE_VERSION_STRING=first', 'QC_IMAGE_VERSION_STRING=second'])

    def test_short_mdt_refused_after_repin(self):
        path = self.args[0].parent / 'vpu20_4v.mdt'
        path.write_bytes(path.read_bytes()[:-1])
        self.repin_manifest()
        with self.assertRaises(ValueError):
            self.audit()

    def test_wrong_dtb_phandle_refused(self):
        self.args[2].write_bytes(FIXTURES.fixture_dtb(handle=5, firmware_node='video-codec@aa00000'))
        self.args[3] = digest(self.args[2].read_bytes())
        with self.assertRaises(ValueError):
            self.audit()

    def test_region_too_small_refused(self):
        self.args[2].write_bytes(FIXTURES.fixture_dtb(size=4096, firmware_node='video-codec@aa00000'))
        self.args[3] = digest(self.args[2].read_bytes())
        # This fixture requires exactly 4096; modify memsz while keeping all header copies consistent.
        for name in ('vpu20_4v.mdt', 'vpu20_4v.mbn', 'vpu20_4v.b00'):
            path = self.args[0].parent / name
            data = bytearray(path.read_bytes())
            struct.pack_into('<I', data, 84 + 20, 4097)
            path.write_bytes(data)
        self.repin_manifest()
        with self.assertRaises(ValueError):
            self.audit()

    def test_invalid_cli_writes_non_success_report(self):
        output = self.root / 'failed.json'
        with mock.patch('builtins.print'):
            status = CHECK.main(['--manifest', str(self.args[0]), '--manifest-sha256', '0' * 64,
                                 '--dtb', str(self.args[2]), '--dtb-sha256', self.args[3],
                                 '--inputs', str(self.args[4]), '--inputs-sha256', self.args[5],
                                 '--out', str(output)])
        self.assertNotEqual(status, 0)
        self.assertFalse(json.loads(output.read_text())['static_consistency_passed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
