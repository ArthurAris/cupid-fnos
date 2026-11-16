#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline pinned-source preparation gates; never invokes compilers or modules."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('adsp_build', HERE / 'prepare-build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='adsp-build-gates-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.roots = {name: self.root / name for name in ('headers', 'upstream', 'pas')}
        self.modules = self.root / 'modules'
        self.modules.mkdir()
        original = json.loads((HERE / 'source-inputs.json').read_text())
        self.pins = {name: {} for name in self.roots}
        for kind, names in original.items():
            for name in names:
                data = b'fixture\n'
                if name.endswith('qcom_common.c'):
                    data = b'ret = auxiliary_device_add(adev);\n'
                if name.endswith('qcom_q6v5_pas.c'):
                    data = b'if (stop_state || qcom_sysmon_shutdown_irq_state(pas->sysmon)) {\n'
                path = self.roots[kind] / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
                self.pins[kind][name] = build.digest(data)
        self.real_read = Path.read_text

    def prepare(self):
        def read(path, *args, **kwargs):
            if path == HERE / 'source-inputs.json':
                return json.dumps(self.pins)
            return self.real_read(path, *args, **kwargs)
        with mock.patch.object(Path, 'read_text', read):
            build.prepare(self.roots['headers'], self.roots['upstream'], self.roots['pas'], self.modules)

    def test_standard_pdm_name_only(self):
        self.prepare()
        text = (self.modules / 'qcom_common.c').read_text()
        self.assertIn('__auxiliary_device_add(adev, "qcom_common")', text)
        self.assertNotIn('#define KBUILD_MODNAME', text)

    def test_ten_module_qrtr_closure(self):
        self.prepare()
        makefile = (self.modules / 'Makefile').read_text()
        self.assertEqual(makefile.count('obj-m +='), 10)
        self.assertIn('cupid_qrtr_smd-y := smd.o', makefile)
        self.assertTrue((self.modules / 'qrtr.h').is_file())

    def test_drift_refuses_before_writes(self):
        (self.roots['upstream'] / 'net/qrtr/smd.c').write_bytes(b'drift')
        with self.assertRaisesRegex(RuntimeError, 'input changed'):
            self.prepare()
        self.assertEqual(list(self.modules.iterdir()), [])

    def test_nonempty_directory_refused(self):
        (self.modules / 'foreign').write_bytes(b'x')
        with self.assertRaisesRegex(RuntimeError, 'empty'):
            self.prepare()
        self.assertEqual((self.modules / 'foreign').read_bytes(), b'x')

    def test_patch_anchor_unique(self):
        path = self.roots['pas'] / 'drivers/remoteproc/qcom_common.c'
        path.write_bytes(path.read_bytes() * 2)
        self.pins['pas']['drivers/remoteproc/qcom_common.c'] = build.digest(path.read_bytes())
        with self.assertRaisesRegex(RuntimeError, 'occur once'):
            self.prepare()

    def test_crlf_normalization(self):
        path = self.roots['upstream'] / 'net/qrtr/smd.c'
        path.write_bytes(b'fixture\r\n')
        self.prepare()
        self.assertEqual((self.modules / 'smd.c').read_bytes(), b'fixture\n')


if __name__ == '__main__':
    unittest.main(verbosity=2)
