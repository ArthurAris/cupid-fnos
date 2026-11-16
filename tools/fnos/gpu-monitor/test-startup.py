#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline gates for exact-module startup; tests never load host or phone modules."""
import hashlib
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('gpu_start', SOURCE / 'cupid-gpu-monitor-start.py')
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)


class FakeRuntime(start.Runtime):
    def __init__(self, root):
        super().__init__(root)
        self.commands = []
        self.bad_modinfo = None
        self.fail_load = None

    def command(self, arguments, timeout=10):
        self.commands.append(arguments)
        if arguments[0] == '/usr/sbin/modinfo':
            module = next(row for row in start.MODULES if row['file'] == Path(arguments[-1]).name)
            if self.bad_modinfo:
                return 'wrong'
            return module['name'] if arguments[2] == 'name' else start.KERNEL + ' SMP mod_unload modversions aarch64'
        if arguments[0] == '/usr/sbin/insmod':
            module = next(row for row in start.MODULES if row['file'] == Path(arguments[1]).name)
            if module['name'] == self.fail_load:
                raise RuntimeError('simulated module failure')
            self.publish(module)
            return ''
        raise AssertionError('Unexpected command: ' + str(arguments))

    def write(self, path, data):
        target = self.path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data if isinstance(data, bytes) else data.encode())
        target.chmod(0o644)

    def publish(self, module):
        base = '/sys/module/' + module['name']
        self.write(base + '/initstate', 'live\n')
        self.write(base + '/notes/.note.gnu.build-id', bytes.fromhex(module['build_note']))
        if module['name'] == 'cupid_gpu_monitor':
            self.write(base + '/parameters/gpu_name', '3d00000.gpu\n')
            self.write('/sys/class/devfreq/3d00000.gpu/gpu_busy_percentage', '0\n')
            self.write('/sys/class/devfreq/3d00000.gpu/sample_status', 'valid=0 busy=0 total=0 age_ns=0 generation=0 missed=0\n')
        else:
            self.write('/sys/class/thermal/thermal_zone38/type', 'gpuss-cupid\n')
            self.write('/sys/class/thermal/thermal_zone38/temp', '48500\n')


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gpu-start-tests-')
        self.runtime = FakeRuntime(self.temp.name)
        self.module_rows = []
        for index, row in enumerate(start.MODULES):
            data = ('mock-module-' + str(index)).encode()
            new = dict(row, sha256=hashlib.sha256(data).hexdigest())
            self.module_rows.append(new)
            self.runtime.write(start.MODULE_DIR + '/' + row['file'], data)
        self.patch = mock.patch.object(start, 'MODULES', tuple(self.module_rows))
        self.patch.start()
        self.runtime.write('/proc/sys/kernel/osrelease', start.KERNEL + '\n')
        self.runtime.write('/proc/device-tree/compatible', b'qcom,waipio\0xiaomi,cupid\0qcom,sm8450\0')
        self.runtime.write('/etc/cupid-fnos-release', 'cupid-fnos-v1\n')
        for device, driver in (('3d00000.gpu', 'adreno'), ('c263000.thermal-sensor', 'qcom-tsens')):
            base = self.runtime.path('/sys/bus/platform/devices/' + device)
            base.mkdir(parents=True, exist_ok=True)
            target = self.runtime.path('/sys/bus/platform/drivers/' + driver)
            target.mkdir(parents=True, exist_ok=True)
            (base / 'driver').symlink_to(target, target_is_directory=True)
        self.runtime.path('/sys/class/devfreq/3d00000.gpu').mkdir(parents=True)
        self.runtime.write('/sys/class/thermal/thermal_zone14/type', 'gpu-top-thermal\n')
        self.runtime.write('/sys/class/thermal/thermal_zone15/type', 'gpu-bottom-thermal\n')

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def loads(self):
        return [command for command in self.runtime.commands if command[0] == '/usr/sbin/insmod']

    def test_clean_start_and_idempotent_repeat(self):
        first = start.activate(self.runtime)
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual(first['temperature_mC'], 48500)
        second = start.activate(self.runtime)
        self.assertEqual(len(self.loads()), 2)
        self.assertTrue(all(row['already_loaded'] for row in second['modules']))

    def test_wrong_kernel_no_load(self):
        self.runtime.write('/proc/sys/kernel/osrelease', '7.2.8-cupid+\n')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.runtime.commands, [])

    def test_wrong_board_no_load(self):
        self.runtime.write('/proc/device-tree/compatible', b'qcom,other\0')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.runtime.commands, [])

    def test_changed_module_refused_before_any_load(self):
        self.runtime.write(start.MODULE_DIR + '/' + start.MODULES[1]['file'], b'tampered')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_writable_module_rejected(self):
        self.runtime.path(start.MODULE_DIR + '/' + start.MODULES[0]['file']).chmod(0o666)
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_symlink_module_rejected(self):
        path = self.runtime.path(start.MODULE_DIR + '/' + start.MODULES[0]['file'])
        path.unlink()
        path.symlink_to(self.runtime.path(start.MODULE_DIR + '/' + start.MODULES[1]['file']))
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_wrong_vermagic_or_name_rejected(self):
        self.runtime.bad_modinfo = True
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_provider_missing_deadline(self):
        self.runtime.path('/sys/bus/platform/devices/3d00000.gpu/driver').unlink()
        with self.assertRaises(RuntimeError):
            start.wait_for_providers(self.runtime, seconds=0)
        self.assertEqual(self.loads(), [])

    def test_duplicate_source_not_ready(self):
        self.runtime.write('/sys/class/thermal/thermal_zone42/type', 'gpu-top-thermal\n')
        self.assertFalse(start.providers_ready(self.runtime))

    def test_existing_wrong_build_blocks_all_loads(self):
        module = start.MODULES[1]
        self.runtime.publish(module)
        self.runtime.write('/sys/module/' + module['name'] + '/notes/.note.gnu.build-id', b'wrong')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_existing_monitor_wrong_gpu(self):
        module = start.MODULES[0]
        self.runtime.publish(module)
        self.runtime.write('/sys/module/' + module['name'] + '/parameters/gpu_name', 'other.gpu')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(self.loads(), [])

    def test_load_failure_never_retried(self):
        self.runtime.fail_load = start.MODULES[1]['name']
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)
        self.assertEqual(len(self.loads()), 2)
        self.assertFalse(any('rmmod' in command[0] for command in self.runtime.commands))

    def test_missing_thermal_interface_rejected(self):
        for module in start.MODULES:
            self.runtime.publish(module)
        self.runtime.path('/sys/class/thermal/thermal_zone38/temp').unlink()
        with self.assertRaises(OSError):
            start.activate(self.runtime)

    def test_invalid_thermal_reading_rejected(self):
        for module in start.MODULES:
            self.runtime.publish(module)
        self.runtime.write('/sys/class/thermal/thermal_zone38/temp', '-274000')
        with self.assertRaises(RuntimeError):
            start.activate(self.runtime)

    def test_unit_is_optional_and_does_not_unload(self):
        unit = (SOURCE / 'cupid-gpu-monitor.service').read_text()
        dropin = (SOURCE / '40-cupid-gpu-monitor.conf').read_text()
        self.assertIn('TimeoutStartSec=45', unit)
        self.assertNotIn('ExecStop=', unit)
        self.assertNotIn('Requires=', dropin)
        self.assertIn('Wants=cupid-gpu-monitor.service', dropin)


if __name__ == '__main__':
    if sys.platform != 'linux':
        raise SystemExit('Run these permission/symlink tests in WSL Linux')
    unittest.main(verbosity=2)
