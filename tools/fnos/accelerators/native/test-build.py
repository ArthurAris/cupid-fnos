#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline gates for the native accelerator build tool. No compiler is needed
for the patch checks; the module build itself is exercised by build.py."""
import importlib.util
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('native_build', HERE / 'build.py')
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)

UPSTREAM = Path('/var/tmp/linux-src/linux-6.18.18')


class PinTests(unittest.TestCase):
    def test_accepts_matching_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'
            path.write_bytes(b'pinned\n')
            build.require_pin(path, build.sha256(path), 'input')

    def test_refuses_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'input'
            path.write_bytes(b'changed\n')
            with self.assertRaisesRegex(RuntimeError, 'Pinned input changed'):
                build.require_pin(path, '0' * 64, 'input')


class SymversTests(unittest.TestCase):
    def test_conflicting_crc_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Module.symvers'
            path.write_text('00000001\ta\tvmlinux\tEXPORT_SYMBOL\n'
                            '00000002\ta\tvmlinux\tEXPORT_SYMBOL\n')
            with self.assertRaisesRegex(RuntimeError, 'Conflicting'):
                build.official_symbols(Path(tmp))

    def test_malformed_line_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'Module.symvers').write_text('garbage\n')
            with self.assertRaisesRegex(RuntimeError, 'Malformed'):
                build.official_symbols(Path(tmp))


@unittest.skipUnless((UPSTREAM / 'drivers/media/platform/qcom/iris/iris_probe.c').is_file(),
                     'pinned upstream tree unavailable')
class IrisPatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-patch-gates-')
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name) / 'iris'
        self.work.mkdir()
        source = UPSTREAM / 'drivers/media/platform/qcom/iris'
        for path in source.iterdir():
            if path.is_file():
                shutil.copyfile(path, self.work / path.name)

    def apply(self):
        patch = HERE / build.PINS['iris']['patch']
        result = subprocess.run(['patch', '--batch', '--forward', '-p1', '-d', str(self.work),
                                 '-i', str(patch)], capture_output=True, text=True)
        return result

    def apply_followups(self):
        result = self.apply()
        if result.returncode:
            return result
        for row in build.PINS['iris'].get('followup_patches', []):
            result = subprocess.run(['patch', '--batch', '--forward', '--fuzz=0', '-p1',
                                     '-d', str(self.work), '-i', str(HERE / row['path'])],
                                    capture_output=True, text=True)
            if result.returncode:
                return result
        return result

    def test_followup_applies_and_retains_probe_refusal(self):
        self.assertEqual(self.apply_followups().returncode, 0)
        header = (self.work / 'iris_platform_common.h').read_text()
        platform = (self.work / 'iris_platform_gen2.c').read_text()
        self.assertIn('bool no_opb;', header)
        self.assertIn('bool no_tz_fifo_reset;', header)
        self.assertIn('.no_opb = true,', platform)
        self.assertIn('.no_tz_fifo_reset = true,', platform)
        self.assertIn('return dev_err_probe(dev, -EOPNOTSUPP,',
                      (self.work / 'iris_probe.c').read_text())
        buffer = (self.work / 'iris_vpu_buffer.c').read_text()
        self.assertIn('static u32 iris_vpu_enc_dpb_size', buffer)
        self.assertIn('{BUF_DPB,         iris_vpu_enc_dpb_size', buffer)
        self.assertIn('no_opb ? 2 : 0', buffer)
        self.assertIn('pixelformat != V4L2_PIX_FMT_NV12',
                      (self.work / 'iris_hfi_gen2_command.c').read_text())

    def test_followup_hash_drift_refused_before_copy(self):
        root = self.work.parent / 'tree'
        source = root / build.PINS['iris']['upstream_dir']
        source.parent.mkdir(parents=True)
        shutil.copytree(self.work, source)
        output = self.work.parent / 'out'
        output.mkdir()
        rows = [{'path': row['path'], 'sha256': '0' * 64}
                for row in build.PINS['iris']['followup_patches']]
        with mock.patch.dict(build.PINS['iris'], {'followup_patches': rows}):
            with self.assertRaisesRegex(RuntimeError, 'Pinned input changed: Iris patch'):
                build.prepare_iris(root, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_behavior_failure_stops_build_before_compilation(self):
        calls = []
        def fail(args, **kwargs):
            calls.append(args)
            raise RuntimeError('behavior failed')
        with mock.patch.object(build, 'prepare_iris'):
            with mock.patch.object(build, 'command', side_effect=fail):
                with mock.patch.object(build, 'compile_modules') as compiler:
                    with self.assertRaisesRegex(RuntimeError, 'behavior failed'):
                        build.build_iris(UPSTREAM, Path('headers'), self.work)
                    compiler.assert_not_called()
        self.assertIn('--work', calls[0])

    def test_patch_applies_to_pinned_sources(self):
        self.assertEqual(self.apply().returncode, 0)
        platform = (self.work / 'iris_platform_gen2.c').read_text()
        self.assertIn('struct iris_platform_data sm8450_data', platform)
        self.assertIn('"qcom/vpu/vpu20_4v.mbn"', platform)
        self.assertIn('qcom,sm8450-iris', (self.work / 'iris_probe.c').read_text())
        self.assertIn('extern struct iris_platform_data sm8450_data;',
                      (self.work / 'iris_platform_common.h').read_text())

    def test_shifted_source_refused(self):
        platform = self.work / 'iris_platform_gen2.c'
        platform.write_text(platform.read_text() + '\n/* drift */\n')
        result = self.apply()
        self.assertNotEqual(result.returncode, 0)

    def test_upstream_tree_not_modified(self):
        source = UPSTREAM / 'drivers/media/platform/qcom/iris/iris_platform_gen2.c'
        before = build.sha256(source)
        self.apply()
        self.assertEqual(build.sha256(source), before)

    def test_sm8450_probe_refused_before_hardware_access(self):
        self.assertEqual(self.apply().returncode, 0)
        text = (self.work / 'iris_probe.c').read_text()
        probe = text[text.index('static int iris_probe('):]
        guard = probe.index('of_device_get_match_data(dev) == &sm8450_data')
        allocation = probe.index('core = devm_kzalloc')
        mapping = probe.index('devm_platform_ioremap_resource')
        self.assertLess(guard, allocation)
        self.assertLess(guard, mapping)
        self.assertIn('return dev_err_probe(dev, -EOPNOTSUPP,', probe[:allocation])
        self.assertNotIn('module_param', probe[:allocation])

    def test_unlisted_source_refused_before_copy(self):
        root = self.work.parent / 'tree'
        source = root / build.PINS['iris']['upstream_dir']
        source.parent.mkdir(parents=True)
        shutil.copytree(self.work, source)
        (source / 'unexpected.h').write_text('extra\n')
        output = self.work.parent / 'out'
        output.mkdir()
        with self.assertRaisesRegex(RuntimeError, 'Extra or missing'):
            build.prepare_iris(root, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_patch_sha_drift_refused(self):
        root = self.work.parent / 'tree'
        source = root / build.PINS['iris']['upstream_dir']
        source.parent.mkdir(parents=True)
        shutil.copytree(self.work, source)
        output = self.work.parent / 'out'
        output.mkdir()
        with mock.patch.dict(build.PINS['iris'], {'patch_sha256': '0' * 64}):
            with self.assertRaisesRegex(RuntimeError, 'Pinned input changed: Iris patch'):
                build.prepare_iris(root, output)
        self.assertEqual(list(output.iterdir()), [])

    def test_double_apply_refused(self):
        self.assertEqual(self.apply().returncode, 0)
        self.assertNotEqual(self.apply().returncode, 0)


import struct
from unittest import mock


class ModuleGateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='native-module-gates-')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'module.ko'
        data = bytearray(64)
        data[:7] = b'\x7fELF\x02\x01\x01'
        struct.pack_into('<HHI', data, 16, 1, 183, 1)
        struct.pack_into('<H', data, 52, 64)
        self.path.write_bytes(data)
        self.outputs = {
            'name': 'cupid_qcom_iris\n',
            'vermagic': build.PINS['vermagic'] + '\n',
            'license': 'GPL\n',
            'alias': 'of:N*T*Cqcom,sm8450-irisC*\n',
            'symbols': ("Symbol table '.symtab' contains 3 entries:\n"
                        '   Num: Value Size Type Bind Vis Ndx Name\n'
                        '     0: 0000000000000000 0 NOTYPE LOCAL DEFAULT UND\n'
                        '     1: 0000000000000000 0 NOTYPE GLOBAL DEFAULT UND __needed\n'
                        '     2: 0000000000000000 0 NOTYPE WEAK DEFAULT UND weak_optional\n'),
            'versions': '0x00000001 __needed\n',
            'notes': 'Displaying notes found in: .note.gnu.build-id\n  Build ID: ' + 'a' * 40 + '\n',
        }
        self.failure = None
        self.providers = {'__needed': 1}

    def invoke(self):
        def command(args, **kwargs):
            if '-F' in args:
                key = args[args.index('-F') + 1]
            elif '-Ws' in args:
                key = 'symbols'
            elif '-n' in args:
                key = 'notes'
            else:
                key = 'versions'
            return subprocess.CompletedProcess(args, 1 if key == self.failure else 0,
                                               self.outputs[key], 'failure' if key == self.failure else '')
        with mock.patch.object(build, 'command', side_effect=command):
            return build.gate_module(self.path, 'cupid_qcom_iris', 'qcom,sm8450-iris', self.providers)

    def test_valid_static_result_not_hardware_acceptance(self):
        result = self.invoke()
        self.assertTrue(result['static_inspection_passed'])
        self.assertFalse(result['passed'])
        self.assertFalse(result['hardware_ready'])
        self.assertEqual(result['weak_undefined'], ['weak_optional'])

    def test_inspector_errors_refused_even_with_output(self):
        for key in self.outputs:
            with self.subTest(key=key):
                self.failure = key
                with self.assertRaises(RuntimeError):
                    self.invoke()

    def test_zero_exit_with_diagnostic_refused(self):
        with mock.patch.object(build, 'command', return_value=subprocess.CompletedProcess(['tool'], 0, 'partial', 'inspection warning')):
            with self.assertRaises(RuntimeError):
                build.inspect(['/usr/bin/tool'])

    def test_empty_inspector_output_refused(self):
        for key in self.outputs:
            with self.subTest(key=key):
                old = self.outputs[key]
                self.outputs[key] = ''
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.outputs[key] = old

    def test_missing_underscore_strong_symbol_refused(self):
        self.providers = {}
        with self.assertRaisesRegex(RuntimeError, 'Strong undefined'):
            self.invoke()

    def test_strong_import_missing_crc_refused(self):
        self.outputs['versions'] = '0x00000002 another\n'
        self.providers['another'] = 2
        with self.assertRaisesRegex(RuntimeError, 'lacks version CRC'):
            self.invoke()

    def test_duplicate_version_refused(self):
        self.outputs['versions'] *= 2
        with self.assertRaisesRegex(RuntimeError, 'Duplicate'):
            self.invoke()

    def test_malformed_version_refused(self):
        self.outputs['versions'] = 'not a version\n'
        with self.assertRaisesRegex(RuntimeError, 'Malformed'):
            self.invoke()

    def test_bad_crc_refused(self):
        self.providers['__needed'] = 2
        with self.assertRaisesRegex(RuntimeError, 'CRC'):
            self.invoke()

    def test_non_gpl_refused(self):
        self.outputs['license'] = 'Proprietary\n'
        with self.assertRaisesRegex(RuntimeError, 'GPL'):
            self.invoke()

    def test_similar_alias_refused(self):
        self.outputs['alias'] = 'of:N*T*Cqcom,sm8450-iris-fake\n'
        with self.assertRaisesRegex(RuntimeError, 'Exact OF'):
            self.invoke()

    def test_name_and_vermagic_refused(self):
        for key in ('name', 'vermagic'):
            with self.subTest(key=key):
                old = self.outputs[key]
                self.outputs[key] = 'wrong\n'
                with self.assertRaises(RuntimeError):
                    self.invoke()
                self.outputs[key] = old

    def test_missing_build_note_refused(self):
        self.outputs['notes'] = 'Other notes\n'
        with self.assertRaisesRegex(RuntimeError, 'build note'):
            self.invoke()

    def test_duplicate_build_id_refused(self):
        self.outputs['notes'] *= 2
        with self.assertRaisesRegex(RuntimeError, 'ambiguous'):
            self.invoke()

    def test_malformed_symbol_refused(self):
        self.outputs['symbols'] += 'partial unreadable row\n'
        with self.assertRaisesRegex(RuntimeError, 'Malformed ELF'):
            self.invoke()

    def test_export_conflict_refused(self):
        self.outputs['symbols'] = self.outputs['symbols'].replace('contains 3 entries:', 'contains 4 entries:')
        self.outputs['symbols'] += ' 3: 0000000000000000 8 OBJECT LOCAL DEFAULT 1 __ksymtab___needed\n'
        with self.assertRaisesRegex(RuntimeError, 'Export conflicts'):
            self.invoke()

    def test_wrong_elf_machine_refused(self):
        data = bytearray(self.path.read_bytes())
        struct.pack_into('<H', data, 18, 62)
        self.path.write_bytes(data)
        with self.assertRaisesRegex(RuntimeError, 'AArch64'):
            self.invoke()

    def test_command_nonzero_refused(self):
        with mock.patch.object(build.subprocess, 'run', return_value=subprocess.CompletedProcess(['tool'], 1, 'partial', 'failed')):
            with self.assertRaisesRegex(RuntimeError, 'Command failed'):
                build.command(['/usr/bin/tool'])

    def test_command_timeout_refused(self):
        with mock.patch.object(build.subprocess, 'run', side_effect=subprocess.TimeoutExpired('tool', 1)):
            with self.assertRaisesRegex(RuntimeError, 'timed out'):
                build.command(['/usr/bin/tool'])

    def test_symlink_pin_refused(self):
        link = self.path.with_name('link')
        try:
            link.symlink_to(self.path)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Symlink'):
            build.sha256(link)

    def test_json_duplicate_refused(self):
        with self.assertRaisesRegex(ValueError, 'Duplicate JSON'):
            build.unique_json([('a', 1), ('a', 2)])


if __name__ == '__main__':
    unittest.main(verbosity=2)
