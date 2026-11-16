#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline Linux/root tests. Host modprobe/insmod/subprocess are prohibited."""
import errno
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import signal
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('adsp_start', SOURCE / 'adsp-start.py')
start = importlib.util.module_from_spec(spec)
spec.loader.exec_module(start)
BOOT1 = '11111111-1111-4111-8111-111111111111'
BOOT2 = '22222222-2222-4222-8222-222222222222'
BOOT3 = '33333333-3333-4333-8333-333333333333'


def module_bytes(index):
    note = struct.pack('<III', 4, 20, 3) + b'GNU\0' + bytes([index + 1]) * 20
    names = b'\0.shstrtab\0.note.gnu.build-id\0'
    header = struct.pack('<16sHHIQQQIHHHHHH', b'\x7fELF\x02\x01\x01' + bytes(9),
                         1, 183, 1, 0, 0, 64, 0, 64, 0, 0, 64, 3, 1)
    strings = struct.pack('<IIQQQQIIQQ', 1, 3, 0, 0, 256, len(names), 0, 0, 1, 0)
    section = struct.pack('<IIQQQQIIQQ', 11, 7, 0, 0, 256 + len(names), len(note), 0, 0, 4, 0)
    return header + bytes(64) + strings + section + names + note, note


class FakeRuntime(start.Runtime):
    def __init__(self, root):
        super().__init__(root)
        self.commands = []
        self.clock = 0
        self.rows = []
        self.bad_modinfo = None
        self.fail_module = None
        self.no_publish = None
        self.sync_fail = False
        self.eagain = 0
        self.raise_term = False
        self.command_seconds = 0

    def now(self):
        return self.clock

    def sleep(self, seconds):
        self.clock += seconds

    def sync(self, fd):
        if self.sync_fail:
            raise OSError(errno.EIO, 'simulated state fsync failure')
        super().sync(fd)

    def read(self, name, limit=4096):
        if name.endswith('/capacity') and self.eagain:
            if self.eagain > 0:
                self.eagain -= 1
            raise BlockingIOError(errno.EAGAIN, 'simulated battery EAGAIN')
        return super().read(name, limit)

    def command(self, arguments, timeout=6):
        self.commands.append(tuple(arguments))
        if self.command_seconds > timeout:
            self.clock += timeout
            raise subprocess.TimeoutExpired(arguments, timeout)
        self.clock += self.command_seconds
        if arguments[0] == '/usr/sbin/modinfo':
            row = next(row for row in self.rows if row['file'] == Path(arguments[-1]).name)
            return 'wrong' if self.bad_modinfo == arguments[2] else row[arguments[2]]
        if arguments[0] not in ('/usr/sbin/modprobe', '/usr/sbin/insmod'):
            raise AssertionError('Forbidden command: ' + str(arguments))
        module = Path(arguments[1]).stem if arguments[0].endswith('insmod') else arguments[1]
        if self.raise_term and module in start.MODULES:
            self.raise_term = False
            raise start.Interrupted('simulated SIGTERM')
        if module == self.fail_module:
            raise RuntimeError('simulated failed module: ' + module)
        if module != self.no_publish:
            self.publish(module)
        return ''

    def write(self, name, data):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode('ascii'))
        path.chmod(0o644)

    def link(self, name, target):
        path = self.path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink():
            path.unlink()
        path.symlink_to(self.path(target), target_is_directory=True)

    def publish(self, name):
        self.write('/sys/module/' + name + '/initstate', 'live\n')
        if name in start.MODULES:
            row = next(row for row in self.rows if row['name'] == name)
            self.write('/sys/module/' + name + '/notes/.note.gnu.build-id', bytes.fromhex(row['build_note']))

    def driver(self, device, bus, driver, module):
        base = '/sys/bus/' + bus + '/drivers/' + driver
        self.path(base).mkdir(parents=True, exist_ok=True)
        self.link(base + '/module', '/sys/module/' + module)
        self.link(device + '/driver', base)


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.guard = mock.patch.object(subprocess, 'run', side_effect=AssertionError('Host subprocess forbidden'))
        self.guard.start()
        self.temp = tempfile.TemporaryDirectory(prefix='adsp-offline-')
        self.r = FakeRuntime(self.temp.name)
        for name in ('/run', '/var/lib'):
            self.r.path(name).mkdir(parents=True)
        self.r.write('/proc/sys/kernel/osrelease', start.KERNEL)
        self.r.write('/proc/device-tree/compatible', b'qcom,waipio\0xiaomi,cupid\0')
        self.r.write('/etc/cupid-fnos-release', start.RELEASE)
        self.r.write('/proc/sys/kernel/random/boot_id', BOOT1)
        self.manifest = {'schema': 1, 'kernel': start.KERNEL, 'board': start.BOARD,
                         'release': start.RELEASE, 'modules': []}
        for index, name in enumerate(start.MODULES):
            data, note = module_bytes(index)
            row = {'name': name, 'file': name + '.ko', 'sha256': hashlib.sha256(data).hexdigest(),
                   'build_note': note.hex(), 'vermagic': start.VERMAGIC}
            self.manifest['modules'].append(row)
            self.r.write(start.BASE + '/' + row['file'], data)
        self.r.rows = self.manifest['modules']
        self.save_manifest()
        self.r.write(start.BASE + '/enabled', b'')
        self.parent = '/sys/devices/platform/soc/3000000.remoteproc'
        self.adsp = self.parent + '/remoteproc/remoteproc7'
        self.r.write(self.adsp + '/name', 'adsp')
        self.r.write(self.adsp + '/state', 'running')
        self.r.link('/sys/class/remoteproc/remoteproc7', self.adsp)
        self.r.link('/sys/bus/platform/devices/3000000.remoteproc', self.parent)
        self.ipc = self.parent + '/remoteproc/remoteproc7/glink-edge/rpmsg0'
        self.r.write(self.ipc + '/name', 'IPCRTR')
        self.r.link('/sys/bus/rpmsg/devices/rpmsg0', self.ipc)
        self.r.driver(self.ipc, 'rpmsg', 'qcom_smd_qrtr', 'cupid_qrtr_smd')
        self.pdm = self.parent + '/qcom_common.pd-mapper.7'
        self.r.path(self.pdm).mkdir()
        self.r.link('/sys/bus/auxiliary/devices/qcom_common.pd-mapper.7', self.pdm)
        self.r.driver(self.pdm, 'auxiliary', 'qcom_pd_mapper.qcom-pdm-mapper', 'qcom_pd_mapper')
        self.battery = '/sys/class/power_supply/qcom-battmgr-bat/'
        for name, value in (('capacity', '72'), ('present', '1'), ('status', 'Discharging'), ('type', 'Battery')):
            self.r.write(self.battery + name, value)

    def tearDown(self):
        self.guard.stop()
        self.temp.cleanup()

    def save_manifest(self):
        self.r.write(start.BASE + '/module-manifest.json', json.dumps(self.manifest))

    def state(self):
        return json.loads(self.r.path(start.STATE + '/state.json').read_bytes())

    def write_state(self, **changes):
        value = {'schema': 1, 'failures': 0, 'boot_id': '', 'outcome': 'skipped'}
        value.update(changes)
        self.r.write(start.STATE + '/state.json', json.dumps(value))

    def loads(self):
        return [cmd for cmd in self.r.commands if cmd[0] == '/usr/sbin/insmod']

    def run_start(self, expected='degraded'):
        result = start.activate(self.r)
        self.assertEqual(result['status'], expected, result)
        self.assertEqual(start.exit_status(result), {'ready': 0, 'degraded': 2, 'failed': 1}[expected])
        return result

    def no_load(self, expected='degraded'):
        result = self.run_start(expected)
        self.assertEqual(self.loads(), [])
        self.assertTrue(any(cmd[:2] == ('/usr/sbin/modprobe', 'pmic_glink') for cmd in self.r.commands))
        return result

    def test_ready_and_same_boot_idempotence(self):
        result = self.run_start('ready')
        self.assertFalse(result['locator_handshake_verified'])
        self.assertEqual([Path(cmd[1]).stem for cmd in self.loads()], list(start.MODULES))
        count = len(self.r.commands)
        self.run_start('ready')
        self.assertEqual(len(self.loads()), 10)
        self.assertFalse(any(cmd[0].endswith(('insmod', 'modprobe')) for cmd in self.r.commands[count:]))
        self.assertEqual(self.state()['failures'], 0)

    def test_wrong_environments(self):
        for path, value in (('/proc/sys/kernel/osrelease', 'other'),
                            ('/proc/device-tree/compatible', b'not,cupid\0'),
                            ('/etc/cupid-fnos-release', 'other')):
            with self.subTest(path=path):
                old = self.r.read(path)
                self.r.write(path, value)
                self.no_load()
                self.r.write(path, old)
                self.assertEqual(self.state()['failures'], 1)

    def test_disabled(self):
        self.r.path(start.BASE + '/enabled').unlink()
        self.no_load()

    def test_nonempty_enabled_refused(self):
        self.r.write(start.BASE + '/enabled', '1')
        self.no_load()

    def test_manifest_sha_corrupt(self):
        self.manifest['modules'][-1]['sha256'] = '0' * 64
        self.save_manifest()
        self.no_load()

    def test_module_bytes_corrupt(self):
        self.r.write(start.BASE + '/' + start.MODULES[-1] + '.ko', b'corrupt')
        self.no_load()

    def test_manifest_schema_and_environment_rejected(self):
        for field, bad in (('schema', True), ('schema', 2), ('kernel', 'wrong'),
                           ('board', 'wrong'), ('release', 'wrong'), ('modules', [])):
            with self.subTest(field=field, bad=bad):
                value = dict(self.manifest, **{field: bad})
                self.r.write(start.BASE + '/module-manifest.json', json.dumps(value))
                self.no_load()

    def test_manifest_arbitrary_names_order_and_path_rejected(self):
        row = self.manifest['modules'][0]
        for field, bad in (('name', 'evil'), ('file', '../evil.ko'), ('vermagic', start.KERNEL),
                           ('sha256', 'g' * 64), ('build_note', '00' * 20)):
            with self.subTest(field=field):
                original = row[field]
                row[field] = bad
                self.save_manifest()
                self.no_load()
                row[field] = original
        self.manifest['modules'].reverse()
        self.save_manifest()
        self.no_load()

    def test_duplicate_manifest_key(self):
        data = json.dumps(self.manifest).replace('"schema": 1', '"schema": 1, "schema": 1')
        self.r.write(start.BASE + '/module-manifest.json', data)
        self.no_load()

    def test_wrong_build_note_file(self):
        self.manifest['modules'][-1]['build_note'] = self.manifest['modules'][0]['build_note']
        self.save_manifest()
        self.no_load()

    def test_modinfo_name_and_vermagic(self):
        for field in ('name', 'vermagic'):
            with self.subTest(field=field):
                self.r.bad_modinfo = field
                self.no_load()

    def test_writable_manifest_and_module(self):
        for name in ('module-manifest.json', start.MODULES[-1] + '.ko', 'enabled'):
            with self.subTest(name=name):
                path = self.r.path(start.BASE + '/' + name)
                path.chmod(0o666)
                self.no_load()
                path.chmod(0o644)

    def test_symlink_manifest_module_switch(self):
        for name in ('module-manifest.json', start.MODULES[-1] + '.ko', 'enabled'):
            with self.subTest(name=name):
                path = self.r.path(start.BASE + '/' + name)
                data = path.read_bytes()
                target = path.with_name(name + '.target')
                target.write_bytes(data)
                path.unlink()
                path.symlink_to(target)
                self.no_load()
                path.unlink()
                path.write_bytes(data)

    def test_parent_writable(self):
        path = self.r.path('/usr/local/lib/cupid')
        path.chmod(0o777)
        self.no_load()

    def test_parent_symlink(self):
        path = self.r.path('/usr/local/lib/cupid')
        target = path.with_name('cupid-real')
        path.rename(target)
        path.symlink_to(target)
        self.no_load()

    def test_root_anchor_writable(self):
        self.r.root.chmod(0o777)
        self.run_start('failed')
        self.assertEqual(self.r.commands, [])

    def test_nonroot_owner_rejected(self):
        path = self.r.path(start.BASE + '/' + start.MODULES[-1] + '.ko')
        os.chown(path, 65534, 65534)
        self.no_load()

    def test_hardlink_module_rejected(self):
        path = self.r.path(start.BASE + '/' + start.MODULES[-1] + '.ko')
        os.link(path, path.with_name('alias.ko'))
        self.no_load()

    def test_existing_wrong_custom_identity(self):
        name = start.MODULES[-1]
        self.r.publish(name)
        self.r.write('/sys/module/' + name + '/notes/.note.gnu.build-id', b'wrong')
        self.no_load()

    def test_existing_custom_not_live(self):
        name = start.MODULES[-1]
        self.r.publish(name)
        self.r.write('/sys/module/' + name + '/initstate', 'coming')
        self.no_load()

    def test_standard_loaded_without_pinned_identity(self):
        for name in ('pmic_glink', 'qcom_glink', 'qcom_sysmon', 'mdt_loader', 'qcom_pd_mapper'):
            self.r.publish(name)
        self.run_start('ready')
        self.assertFalse(any(cmd[0].endswith('modprobe') for cmd in self.r.commands))

    def test_standard_not_live(self):
        self.r.publish('qcom_glink')
        self.r.write('/sys/module/qcom_glink/initstate', 'coming')
        self.no_load()

    def test_unique_resolved_adsp_not_remoteproc0(self):
        other = '/sys/devices/platform/soc/other/remoteproc/remoteproc0'
        self.r.write(other + '/name', 'cdsp')
        self.r.write(other + '/state', 'running')
        self.r.link('/sys/class/remoteproc/remoteproc0', other)
        self.run_start('ready')

    def test_duplicate_adsp(self):
        other = self.parent + '/remoteproc/remoteproc8'
        self.r.write(other + '/name', 'adsp')
        self.r.write(other + '/state', 'running')
        self.r.link('/sys/class/remoteproc/remoteproc8', other)
        self.run_start()

    def test_adsp_wrong_parent(self):
        other = '/sys/devices/platform/soc/4000000.remoteproc/remoteproc/remoteproc9'
        self.r.write(other + '/name', 'adsp')
        self.r.write(other + '/state', 'running')
        self.r.link('/sys/class/remoteproc/remoteproc7', other)
        self.run_start()
        self.assertLessEqual(self.r.clock, start.START_BUDGET + start.FALLBACK_BUDGET)

    def test_adsp_not_running(self):
        self.r.write(self.adsp + '/state', 'offline')
        self.run_start()

    def test_qrtr_not_bound(self):
        self.r.path(self.ipc + '/driver').unlink()
        self.run_start()

    def test_qrtr_wrong_driver(self):
        self.r.driver(self.ipc, 'rpmsg', 'other', 'cupid_qrtr_smd')
        self.run_start()

    def test_qrtr_wrong_module_owner(self):
        self.r.publish('other')
        self.r.driver(self.ipc, 'rpmsg', 'qcom_smd_qrtr', 'other')
        self.run_start()

    def test_ipcrtr_other_remoteproc_not_accepted(self):
        other = '/sys/devices/platform/soc/4000000.remoteproc/rpmsg0'
        self.r.write(other + '/name', 'IPCRTR')
        self.r.driver(other, 'rpmsg', 'qcom_smd_qrtr', 'cupid_qrtr_smd')
        self.r.link('/sys/bus/rpmsg/devices/rpmsg0', other)
        self.run_start()

    def test_ipcrtr_requires_exact_channel_name(self):
        self.r.write(self.ipc + '/name', 'NOT_IPCRTR')
        self.run_start()

    def test_pdm_nonstandard_name(self):
        self.r.path('/sys/bus/auxiliary/devices/qcom_common.pd-mapper.7').unlink()
        self.r.link('/sys/bus/auxiliary/devices/cupid_qcom_rproc_common.pd-mapper.7', self.pdm)
        self.run_start()

    def test_pdm_not_bound(self):
        self.r.path(self.pdm + '/driver').unlink()
        self.run_start()

    def test_pdm_wrong_module(self):
        self.r.publish('other')
        self.r.driver(self.pdm, 'auxiliary', 'other', 'other')
        self.run_start()

    def test_pdm_other_adsp_parent(self):
        other = '/sys/devices/platform/soc/4000000.remoteproc/qcom_common.pd-mapper.7'
        self.r.path(other).mkdir(parents=True)
        self.r.driver(other, 'auxiliary', 'qcom_pd_mapper.qcom-pdm-mapper', 'qcom_pd_mapper')
        self.r.link('/sys/bus/auxiliary/devices/qcom_common.pd-mapper.7', other)
        self.run_start()

    def test_eagain_recovery(self):
        self.r.eagain = 4
        self.run_start('ready')
        self.assertEqual(self.r.clock, 1)

    def test_eagain_deadline(self):
        self.r.eagain = -1
        self.run_start()
        self.assertEqual(self.r.clock, 25)
        self.assertEqual(self.state()['failures'], 1)

    def test_missing_battery_attr(self):
        self.r.path(self.battery + 'status').unlink()
        self.run_start()
        self.assertEqual(self.r.clock, 25)

    def test_illegal_battery_capacity(self):
        for value in ('101', '-1', 'nan', '', '1000'):
            with self.subTest(value=value):
                self.r.write(self.battery + 'capacity', value)
                with self.assertRaises(RuntimeError):
                    start.battery_sample(self.r)

    def test_battery_boundary_capacity(self):
        for value in ('0', '100'):
            self.r.write(self.battery + 'capacity', value)
            self.assertEqual(start.battery_sample(self.r)['capacity'], int(value))

    def test_battery_invalid_status_present_type(self):
        for key, bad in (('present', '0'), ('present', '2'), ('status', 'Unknown'), ('type', 'USB')):
            with self.subTest(key=key, value=bad):
                old = self.r.read(self.battery + key)
                self.r.write(self.battery + key, bad)
                with self.assertRaises(RuntimeError):
                    start.battery_sample(self.r)
                self.r.write(self.battery + key, old)

    def test_failed_fallback(self):
        self.r.write('/proc/sys/kernel/osrelease', 'wrong')
        self.r.fail_module = 'pmic_glink'
        self.no_load('failed')

    def test_fallback_checks_live_after_command(self):
        self.r.write('/proc/sys/kernel/osrelease', 'wrong')
        self.r.no_publish = 'pmic_glink'
        self.no_load('failed')
        self.assertEqual(self.r.clock, 2)

    def test_sameboot_failure_no_retry_or_increment(self):
        self.r.fail_module = start.MODULES[0]
        self.run_start()
        count = len(self.loads())
        self.run_start()
        self.assertEqual(len(self.loads()), count)
        self.assertEqual(self.state()['failures'], 1)

    def test_two_distinct_failure_boots_then_safe_mode(self):
        self.r.fail_module = start.MODULES[0]
        self.run_start()
        self.r.write('/proc/sys/kernel/random/boot_id', BOOT2)
        self.run_start()
        self.assertEqual(self.state()['failures'], 2)
        self.assertEqual(len(self.loads()), 2)
        self.r.write('/proc/sys/kernel/random/boot_id', BOOT3)
        result = self.run_start()
        self.assertIn('Safe mode', result['reason'])
        self.assertEqual(len(self.loads()), 2)
        self.assertEqual(self.state()['outcome'], 'safe')

    def test_ready_on_second_boot_clears_failure(self):
        self.r.fail_module = start.MODULES[0]
        self.run_start()
        self.r.fail_module = None
        self.r.write('/proc/sys/kernel/random/boot_id', BOOT2)
        self.run_start('ready')
        self.assertEqual(self.state()['failures'], 0)

    def test_counter_corruption_rejected(self):
        for value in (-1, 3, True, '1', 1000000000):
            with self.subTest(value=value):
                self.write_state(failures=value)
                self.no_load()

    def test_state_invalid_json(self):
        self.r.write(start.STATE + '/state.json', '{')
        self.no_load()

    def test_legacy_counter_corruption(self):
        for value in ('-1', '3', 'garbage', '999999999999999999999999'):
            with self.subTest(value=value):
                self.r.write(start.STATE + '/attempts', value)
                self.no_load()

    def test_legacy_safety_and_ok_cleanup(self):
        self.r.write(start.STATE + '/attempts', '2')
        self.r.write(start.STATE + '/ok', '')
        self.no_load()
        self.assertFalse(self.r.path(start.STATE + '/ok').exists())
        self.assertEqual(self.state()['failures'], 2)

    def test_stale_success_missing_module(self):
        self.run_start('ready')
        self.r.path('/sys/module/' + start.MODULES[-1] + '/initstate').unlink()
        self.run_start()
        self.assertEqual(len(self.loads()), 10)
        self.assertEqual(self.state()['outcome'], 'failed')

    def test_stale_success_missing_battery(self):
        self.run_start('ready')
        self.r.path(self.battery + 'capacity').unlink()
        self.run_start()
        self.assertEqual(self.state()['outcome'], 'failed')
        self.assertEqual(len(self.loads()), 10)

    def test_state_fsync_failure_forbids_adsp_load(self):
        self.r.sync_fail = True
        self.no_load()

    def test_state_parent_writable(self):
        self.r.path('/var/lib').chmod(0o777)
        self.no_load()

    def test_state_symlink(self):
        self.write_state()
        path = self.r.path(start.STATE + '/state.json')
        target = path.with_name('state-target')
        path.rename(target)
        path.symlink_to(target)
        self.no_load()

    def test_state_parent_symlink(self):
        self.write_state()
        path = self.r.path(start.STATE)
        target = path.with_name('state-target')
        path.rename(target)
        path.symlink_to(target)
        self.no_load()

    def test_shared_lock_blocks_load(self):
        path = self.r.path('/run/cupid-adsp.lock')
        with path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.run_start('failed')
            self.assertEqual(self.r.commands, [])

    def test_shared_lock_symlink(self):
        self.r.write('/run/lock-target', '')
        self.r.link('/run/cupid-adsp.lock', '/run/lock-target')
        self.run_start('failed')
        self.assertEqual(self.r.commands, [])

    def test_pending_same_boot_no_retry(self):
        self.write_state(boot_id=BOOT1, failures=1, outcome='pending')
        self.no_load()
        self.assertEqual(self.state()['failures'], 1)

    def test_sigterm_runs_fallback_and_counts_once(self):
        self.r.raise_term = True
        self.run_start()
        self.assertTrue(start.check_loaded(self.r, 'pmic_glink'))
        self.assertEqual(self.state()['failures'], 1)
        self.assertEqual(self.state()['outcome'], 'failed')

    def test_timeout_failure_hook(self):
        self.write_state(boot_id=BOOT1, failures=1, outcome='pending')
        result = start.failure_hook(self.r, 'timeout')
        self.assertEqual(result['status'], 'degraded')
        self.assertTrue(start.check_loaded(self.r, 'pmic_glink'))
        self.assertEqual(self.loads(), [])
        self.assertEqual(self.state()['outcome'], 'failed')
        self.assertEqual(self.state()['failures'], 1)
        start.failure_hook(self.r, 'exit-code')
        self.assertEqual(self.state()['failures'], 1)

    def test_success_stop_hook_is_noop(self):
        result = start.failure_hook(self.r, 'success')
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(self.r.commands, [])
        self.assertFalse(self.r.path(start.STATE).exists())

    def test_command_timeout_and_fallback_budget(self):
        self.r.command_seconds = 100
        self.run_start('failed')
        self.assertLessEqual(self.r.clock, start.START_BUDGET + start.FALLBACK_BUDGET)
        self.assertEqual(self.loads(), [])

    def test_total_deadline(self):
        # Successful commands consume 4 seconds each: shared 90s deadline must stop
        # additional commands/waits and leave a separate 10s standard fallback.
        self.r.command_seconds = 4
        result = self.run_start()
        self.assertLessEqual(self.r.clock, start.START_BUDGET + start.FALLBACK_BUDGET)
        self.assertTrue('deadline' in result['reason'].lower() or 'timed out' in result['reason'])

    def test_deployment_pending_blocks_custom_and_leaves_record(self):
        self.r.write(start.DEPLOY_PENDING, '{"deployment": "incomplete"}')
        path = self.r.path(start.DEPLOY_PENDING)
        path.chmod(0o600)
        before = path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns
        result = self.no_load()
        self.assertIn('explicit recover required', result['reason'])
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns))
        self.assertFalse(self.r.path(start.STATE + '/state.json').exists())

    def test_deployment_pending_corrupt_or_dangling_still_blocks(self):
        self.r.write(start.DEPLOY_PENDING, 'not JSON')
        self.no_load()
        path = self.r.path(start.DEPLOY_PENDING)
        path.unlink()
        path.symlink_to(self.r.path('/nonexistent'))
        result = self.no_load()
        self.assertIn('explicit recover required', result['reason'])
        self.assertTrue(path.is_symlink())

    def test_deployment_pending_blocks_sameboot_ready(self):
        self.run_start('ready')
        before = self.state()
        self.r.write(start.DEPLOY_PENDING, '{}')
        count = len(self.r.commands)
        result = self.run_start()
        self.assertIn('explicit recover required', result['reason'])
        self.assertEqual(self.state(), before)
        self.assertFalse(any(cmd[0].endswith(('insmod', 'modprobe')) for cmd in self.r.commands[count:]))

    def test_inconsistent_state_rejected(self):
        for changes in ({'outcome': 'ready'}, {'outcome': 'ready', 'failures': 1, 'boot_id': BOOT1},
                        {'outcome': 'pending', 'failures': 0, 'boot_id': BOOT1},
                        {'outcome': 'safe', 'failures': 1, 'boot_id': BOOT1},
                        {'outcome': 'skipped', 'boot_id': BOOT1}, {'schema': True}):
            with self.subTest(changes=changes):
                self.write_state(**changes)
                self.no_load()

    def test_loaded_standard_pdm_not_live_blocks_entire_custom_group(self):
        self.r.publish('qcom_pd_mapper')
        self.r.write('/sys/module/qcom_pd_mapper/initstate', 'coming')
        self.no_load()

    def test_invalid_capacity_start_never_ready(self):
        self.r.write(self.battery + 'capacity', '101')
        self.run_start()
        self.assertEqual(self.state()['outcome'], 'failed')
        self.assertEqual(self.state()['failures'], 1)

    def test_explicit_fallback_main_is_runnable(self):
        with mock.patch.object(start, 'Runtime', return_value=self.r), \
                mock.patch.object(sys, 'argv', ['adsp-start.py', '--fallback']), \
                mock.patch('builtins.print') as output:
            self.assertEqual(start.main(), 2)
        result = json.loads(output.call_args.args[0])
        self.assertEqual(result['status'], 'degraded')
        self.assertEqual(self.loads(), [])
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_failure_hook_main_is_runnable(self):
        with mock.patch.object(start, 'Runtime', return_value=self.r), \
                mock.patch.object(sys, 'argv', ['adsp-start.py', '--failure-fallback']), \
                mock.patch.dict(os.environ, {'SERVICE_RESULT': 'timeout'}), \
                mock.patch('builtins.print') as output:
            self.assertEqual(start.main(), 2)
        result = json.loads(output.call_args.args[0])
        self.assertIn('timeout', result['reason'])
        self.assertEqual(self.loads(), [])
        signal.signal(signal.SIGTERM, signal.SIG_DFL)

    def test_raw_read_eagain_and_descriptor_cleanup(self):
        runtime = start.Runtime(self.r.root)
        with mock.patch.object(start.os, 'read', side_effect=BlockingIOError(errno.EAGAIN, 'raw EAGAIN')), \
                mock.patch.object(start.os, 'close', wraps=os.close) as close:
            self.assertIsNone(start.battery_sample(runtime))
            self.assertEqual(close.call_count, 1)
        self.assertEqual(start.battery_sample(runtime)['capacity'], 72)

    def test_every_fallback_entry_respects_busy_lock(self):
        path = self.r.path('/run/cupid-adsp.lock')
        with path.open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(start.failure_hook(self.r, 'timeout')['status'], 'failed')
            self.assertEqual(start.explicit_fallback(self.r)['status'], 'failed')
        self.assertEqual(self.r.commands, [])

    def test_fallback_holds_shared_lock(self):
        original = self.r.command
        def check(arguments, timeout=6):
            if arguments[:2] == ['/usr/sbin/modprobe', 'pmic_glink']:
                with self.r.path('/run/cupid-adsp.lock').open('r+') as lock:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return original(arguments, timeout)
        with mock.patch.object(self.r, 'command', side_effect=check):
            self.assertEqual(start.explicit_fallback(self.r)['status'], 'degraded')
            self.assertEqual(start.failure_hook(self.r, 'timeout')['status'], 'degraded')

    def test_unit_contract_and_python_isolation(self):
        unit = (SOURCE / 'cupid-adsp.service').read_text()
        shell = (SOURCE / 'start.sh').read_text()
        self.assertIn('ExecStopPost=/bin/sh /usr/local/lib/cupid/adsp/start.sh --failure-fallback', unit)
        self.assertIn('TimeoutStartSec=120', unit)
        self.assertIn('TimeoutStopSec=20', unit)
        self.assertNotIn('Requires=', unit)
        self.assertNotIn('SuccessExitStatus=', unit)
        self.assertIn('/usr/bin/python3 -I -B', shell)
        self.assertLess(start.START_BUDGET + start.FALLBACK_BUDGET, 120)


if __name__ == '__main__':
    if sys.platform != 'linux' or os.geteuid() != 0:
        raise SystemExit('Run offline permission tests with WSL Linux root, python3 -I -B')
    unittest.main(verbosity=2)
