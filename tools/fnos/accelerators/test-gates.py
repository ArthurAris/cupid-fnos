#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline material/enum gates; no vendor runtime or device program is executed."""
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


audit = load('material_audit', HERE / 'audit-materials.py')
fastrpc = load('fastrpc_inventory', HERE / 'npu/enumerate-fastrpc.py')
KERNEL = '6.18.18.c1147-trim'


class Gates(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='accel-gates-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manifest = {'schema': 1, 'board': 'xiaomi,cupid', 'kernel': KERNEL,
                         'accelerator': 'npu', 'files': []}

    def material(self, role, data):
        p = self.root / role
        p.write_bytes(data)
        return {'role': role, 'path': str(p), 'sha256': hashlib.sha256(data).hexdigest()}

    def elf(self, machine=183, kind=3):
        data = bytearray(64)
        data[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HH', data, 16, kind, machine)
        return bytes(data)

    def test_empty_npu_is_blocked(self):
        result = audit.audit(self.manifest, KERNEL)
        self.assertEqual(len(result['missing_roles']), 5)
        self.assertFalse(result['hardware_ready'])

    def test_empty_video_is_blocked(self):
        self.manifest['accelerator'] = 'video'
        self.assertEqual(len(audit.audit(self.manifest, KERNEL)['missing_roles']), 3)

    def test_wrong_kernel(self):
        with self.assertRaises(ValueError):
            audit.audit(self.manifest, '6.18.18.c1193-trim')

    def test_wrong_board(self):
        self.manifest['board'] = 'not,cupid'
        with self.assertRaises(ValueError):
            audit.audit(self.manifest, KERNEL)

    def test_duplicate_json_refused(self):
        with self.assertRaises(ValueError):
            json.loads('{"schema":1,"schema":1}', object_pairs_hook=audit.unique_json)

    def test_wrong_sha(self):
        row = self.material('cdsp-firmware', b'firmware')
        row['sha256'] = '0' * 64
        self.manifest['files'] = [row]
        self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_symlink_refused(self):
        row = self.material('cdsp-firmware', b'firmware')
        p = Path(row['path'])
        target = p.with_name('target')
        p.rename(target)
        p.symlink_to(target)
        self.manifest['files'] = [row]
        self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_host_x86_runtime_refused(self):
        self.manifest['files'] = [self.material('linux-runtime', self.elf(machine=62))]
        self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_android_runtime_refused(self):
        self.manifest['files'] = [self.material('linux-runtime', self.elf())]
        with mock.patch.object(audit, 'command', side_effect=['(NEEDED) [libc.so]', '/system/bin/linker64']):
            self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_dsp_wrong_isa_refused(self):
        self.manifest['files'] = [self.material('dsp-shell', self.elf())]
        self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_video_module_missing_sm8450_refused(self):
        self.manifest['accelerator'] = 'video'
        self.manifest['files'] = [self.material('iris-module', self.elf(kind=1))]
        with mock.patch.object(audit, 'command', side_effect=['qcom_iris', KERNEL + ' SMP mod_unload modversions aarch64', 'of:N*T*Cqcom,sm8550-iris']):
            self.assertTrue(audit.audit(self.manifest, KERNEL)['errors'])

    def test_adsp_running_not_npu_success(self):
        p = self.root / 'sys/class/remoteproc/remoteproc0'
        p.mkdir(parents=True)
        (p / 'name').write_text('adsp')
        (p / 'state').write_text('running')
        result = fastrpc.inventory(self.root)
        self.assertFalse(result['cdsp_running'])
        self.assertFalse(result['npu_inference_tested'])

    def test_cdsp_running_not_inference(self):
        p = self.root / 'sys/class/remoteproc/remoteproc1'
        p.mkdir(parents=True)
        (p / 'name').write_text('cdsp')
        (p / 'state').write_text('running')
        result = fastrpc.inventory(self.root)
        self.assertTrue(result['cdsp_running'])
        self.assertFalse(result['dsp_rpc_tested'])
        self.assertFalse(result['npu_inference_tested'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
