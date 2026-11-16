#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline Linux/WSL filesystem + systemctl simulation. NEVER calls host modprobe.
Run: python3 -I /trusted/path/test-deployment.py
All roots/stages are new TemporaryDirectory children; System is replaced by fake.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import unittest
from unittest import mock

HERE = Path(__file__).resolve().parent


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, HERE / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


d = load('deployment_test_subject', 'deploy.py')
p = load('package_test_subject', 'prepare-package.py')


def module_elf(name):
    sections = [b'', b'\0.shstrtab\0.modinfo\0.note.gnu.build-id\0',
                ('name=' + name + '\0vermagic=' + d.VERMAGIC + '\0').encode(),
                struct.pack('<III', 4, 20, 3) + b'GNU\0' + b'\x17' * 20]
    locations, data = [], bytearray(b'\0' * 64)
    for item in sections:
        locations.append((len(data), len(item)))
        data.extend(item)
    table = len(data)
    for index, (offset, length) in enumerate(locations):
        data.extend(struct.pack('<IIQQQQIIQQ', [0, 1, 11, 20][index], [0, 3, 1, 7][index], 0, 0, offset, length, 0, 0, 1, 0))
    ident = b'\x7fELF\x02\x01\x01' + b'\0' * 9
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, ident, 1, 183, 1, 0, 0, table, 0, 64, 0, 0, 64, 4, 1)
    return bytes(data)


class FakeSystem:
    def __init__(self, fs):
        self.fs = fs
        self.calls = []
        self.board, self.kernel = d.BOARD, d.KERNEL
        self.active = 'inactive'
        self.fail_reload = 0
        self.fail_change = 0
        self.fail_modprobe = False
        self.pmic = False

    def identity(self):
        self.calls.append('identity')
        if (self.board, self.kernel) != (d.BOARD, d.KERNEL):
            raise d.Refusal('Wrong board/kernel')

    def inactive(self):
        self.calls.append('inactive')
        if self.active not in ('inactive', 'failed', 'unknown'):
            raise d.Refusal('Service running: authorize separate stop')

    def enabled(self):
        if self.fs.wants():
            return 'enabled'
        return 'disabled' if self.fs.read(d.UNIT) is not None else 'not-found'

    def reload(self):
        self.calls.append('daemon-reload')
        if self.fail_reload:
            self.fail_reload -= 1
            raise d.Refusal('Injected daemon-reload failure')

    def change(self, value):
        self.calls.append('enable' if value == 'enabled' else 'disable')
        path = self.fs.path(d.WANTS)
        if value == 'enabled':
            self.fs.mkdirs(d.WANTS)
            if not path.is_symlink():
                path.symlink_to(d.UNIT)
        elif path.is_symlink():
            path.unlink()
        if self.fail_change:
            self.fail_change -= 1
            raise d.Refusal('Injected systemctl failure after change')

    def fallback(self):
        self.calls.append('modprobe pmic_glink')
        if self.fail_modprobe:
            raise d.Refusal('Injected modprobe failure')
        self.pmic = True


class DeploymentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cupid-offline-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'root'
        self.root.mkdir(mode=0o700)
        self.stage = Path(self.temp.name) / 'stage'
        self.stage.mkdir(mode=0o700)
        self.fs = d.Filesystem(self.root)
        self.system = FakeSystem(self.fs)
        self.deploy = d.Deployment(self.fs, self.system)
        # Absolutely prohibit real subprocess commands, including host modprobe.
        self.guard = mock.patch.object(subprocess, 'run', side_effect=AssertionError('Host command prohibited in offline tests'))
        self.guard.start()
        self.addCleanup(self.guard.stop)
        for name in d.FILES:
            if name != 'enabled':
                (self.stage / name).write_bytes(module_elf(name[:-3]) if name.endswith('.ko') else b'fixture\n')
        (self.stage / 'cupid-adsp.service').write_bytes(b'[Service]\nType=oneshot\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n')
        records = []
        for name in d.MODULES:
            value = (self.stage / (name + '.ko')).read_bytes()
            records.append({'name': name, 'file': name + '.ko', 'sha256': d.sha(value), 'build_note': d.elf_module(value)[2], 'vermagic': d.VERMAGIC})
        manifest = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'release': d.RELEASE, 'modules': records}
        (self.stage / 'module-manifest.json').write_bytes(d.json_bytes(manifest))
        self.pin = self.prepare()
        self.fs.created.clear()

    def prepare(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return p.prepare(self.stage)

    def install(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.deploy.install(self.stage, self.pin)

    def uninstall(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            self.deploy.uninstall(**kwargs)

    def tree(self):
        result = {}
        for path in self.root.rglob('*'):
            name = str(path.relative_to(self.root))
            if path.is_symlink():
                result[name] = ('link', os.readlink(path))
            elif path.is_file():
                result[name] = (path.read_bytes(), path.stat().st_mode & 0o777)
            else:
                result[name] = ('dir', path.stat().st_mode & 0o777)
        return result

    def refusal_no_writes(self, fn):
        before = self.tree()
        with self.assertRaises((d.Refusal, p.deploy.Refusal, OSError)):
            fn()
        self.assertEqual(self.tree(), before)

    def original(self, enabled=False, sentinel=False):
        self.fs.put(d.UNIT, {'data': b'# original unit\n[Service]\nType=oneshot\nExecStart=/bin/true\n[Install]\nWantedBy=multi-user.target\n', 'mode': 0o640})
        self.fs.put(d.BASE + '/start.sh', {'data': b'original start', 'mode': 0o750})
        if sentinel:
            self.fs.put(d.BASE + '/enabled', {'data': b'original switch', 'mode': 0o640})
        if enabled:
            self.fs.mkdirs(d.WANTS)
            self.system.change('enabled')
        self.fs.created.clear()
        return self.deploy.snapshot()

    def test_new_install_restore_and_repeat(self):
        self.install()
        self.assertEqual(self.system.enabled(), 'enabled')
        self.assertEqual(self.fs.read(d.BASE + '/enabled'), {'data': b'', 'mode': 0o644})
        record = self.deploy.record()
        self.assertEqual(len(record['installed']), 17)
        self.assertEqual(self.fs.path(record['baseline']).stat().st_mode & 0o777, 0o700)
        self.uninstall()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertEqual(self.system.enabled(), 'not-found')
        self.assertFalse(self.fs.path(d.BASE).exists())
        self.uninstall()
        self.assertEqual(self.deploy.record()['status'], 'uninstalled')
        self.assertNotIn('modprobe pmic_glink', self.system.calls)
        self.assertNotIn('start', self.system.calls)
        self.assertNotIn('stop', self.system.calls)

    def test_original_disabled_sentinel_absent(self):
        old = self.original()
        self.install()
        self.assertEqual(self.system.enabled(), 'disabled')
        self.assertIsNone(self.fs.read(d.BASE + '/enabled'))
        self.uninstall()
        self.assertEqual(self.deploy.snapshot(), old)
        self.assertEqual(self.system.enabled(), 'disabled')

    def test_original_enabled_sentinel_preserved(self):
        old = self.original(enabled=True, sentinel=True)
        self.install()
        self.assertEqual(self.fs.read(d.BASE + '/enabled'), old[d.BASE + '/enabled'])
        self.uninstall()
        self.assertEqual(self.deploy.snapshot(), old)
        self.assertEqual(self.system.enabled(), 'enabled')

    def test_upgrade_preserves_admin_disabled_state(self):
        self.install()
        self.system.change('disabled')
        self.fs.put(d.BASE + '/enabled', None)
        # Sentinel changes are administrative, but record must be updated explicitly.
        # Current strict drift gate intentionally refuses unrecorded changes.
        self.refusal_no_writes(self.install)

    def test_upgrade_retains_first_baseline(self):
        original = self.original(enabled=True)
        self.install()
        baseline = self.deploy.record()['baseline']
        (self.stage / 'start.sh').write_bytes(b'updated start\n')
        self.pin = self.prepare()
        self.install()
        self.assertEqual(self.deploy.record()['baseline'], baseline)
        self.assertEqual(len(self.deploy.record()['transactions']), 2)
        self.uninstall()
        self.assertEqual(self.deploy.snapshot(), original)

    def test_upgrade_preserves_systemd_disable(self):
        self.install()
        self.system.change('disabled')
        self.install()
        self.assertEqual(self.system.enabled(), 'disabled')

    def test_install_reload_failure_rolls_back(self):
        old = self.original()
        self.system.fail_reload = 1
        with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
            self.install()
        self.assertEqual(self.deploy.snapshot(), old)
        self.assertEqual(self.system.enabled(), 'disabled')
        self.assertIsNone(self.fs.read(d.STATE))

    def test_enable_failure_after_change_rolls_back(self):
        self.system.fail_change = 1
        with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
            self.install()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertEqual(self.system.enabled(), 'not-found')

    def test_replacement_failure_preserves_installed_transaction(self):
        self.install()
        before, state = self.deploy.snapshot(), self.fs.read(d.STATE)
        (self.stage / 'start.sh').write_bytes(b'updated\n')
        self.pin = self.prepare()
        self.system.fail_change = 1
        with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
            self.install()
        self.assertEqual(self.deploy.snapshot(), before)
        self.assertEqual(self.fs.read(d.STATE), state)
        self.assertEqual(self.system.enabled(), 'enabled')

    def test_partial_file_failure_install_restores(self):
        put = self.fs.put
        failed = False
        def inject(name, item):
            nonlocal failed
            put(name, item)
            if name == d.BASE + '/cupid_hwspinlock_qcom.ko' and not failed:
                failed = True
                raise OSError('after replacement failure')
        with mock.patch.object(self.fs, 'put', side_effect=inject):
            with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
                self.install()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertFalse(self.fs.path(d.BASE).exists())

    def test_partial_uninstall_failure_restores_current(self):
        self.install()
        before, state = self.deploy.snapshot(), self.fs.read(d.STATE)
        put = self.fs.put
        failed = False
        def inject(name, item):
            nonlocal failed
            put(name, item)
            if name == d.BASE + '/cupid_hwspinlock_qcom.ko' and not failed:
                failed = True
                raise OSError('partial uninstall failure')
        with mock.patch.object(self.fs, 'put', side_effect=inject):
            with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
                self.uninstall()
        self.assertEqual(self.deploy.snapshot(), before)
        self.assertEqual(self.fs.read(d.STATE), state)
        self.uninstall()

    def test_uninstall_disable_failure_restores(self):
        self.original(enabled=False)
        self.install()
        self.system.change('enabled')
        before, state = self.deploy.snapshot(), self.fs.read(d.STATE)
        self.system.fail_change = 1
        with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
            self.uninstall()
        self.assertEqual(self.deploy.snapshot(), before)
        self.assertEqual(self.fs.read(d.STATE), state)
        self.assertEqual(self.system.enabled(), 'enabled')

    def test_missing_stage(self):
        self.refusal_no_writes(lambda: self.deploy.install(self.stage / 'missing', self.pin))

    def test_missing_payload(self):
        (self.stage / 'start.sh').unlink()
        self.refusal_no_writes(self.install)

    def test_extra_payload(self):
        (self.stage / 'foreign').write_bytes(b'x')
        self.refusal_no_writes(self.install)

    def test_hash_pin_mismatch(self):
        self.pin = '0' * 64
        self.refusal_no_writes(self.install)

    def test_hash_pin_required(self):
        self.pin = ''
        self.refusal_no_writes(self.install)

    def test_payload_hash_mismatch(self):
        (self.stage / 'start.sh').write_bytes(b'tampered')
        self.refusal_no_writes(self.install)

    def test_target_manifest_tamper(self):
        path = self.stage / 'package-manifest.json'
        value = d.parse_json(path.read_bytes())
        value['files'][0]['target'] = '/etc/foreign'
        path.write_bytes(d.json_bytes(value))
        self.pin = d.sha(path.read_bytes())
        self.refusal_no_writes(self.install)

    def test_manifest_mode_tamper(self):
        path = self.stage / 'package-manifest.json'
        value = d.parse_json(path.read_bytes())
        value['files'][0]['mode'] = 0o777
        path.write_bytes(d.json_bytes(value))
        self.pin = d.sha(path.read_bytes())
        self.refusal_no_writes(self.install)

    def test_duplicate_json_keys(self):
        path = self.stage / 'package-manifest.json'
        path.write_bytes(path.read_bytes().replace(b'"schema": 1', b'"schema": 1, "schema": 1'))
        self.pin = d.sha(path.read_bytes())
        self.refusal_no_writes(self.install)

    def test_wrong_manifest_identities(self):
        path = self.stage / 'package-manifest.json'
        original = path.read_bytes()
        for key in ('board', 'kernel', 'release'):
            value = d.parse_json(original)
            value[key] = 'wrong'
            path.write_bytes(d.json_bytes(value))
            self.pin = d.sha(path.read_bytes())
            self.refusal_no_writes(self.install)

    def test_wrong_machine_identities(self):
        self.system.board = 'wrong'
        self.refusal_no_writes(self.install)
        self.system.board = d.BOARD
        self.system.kernel = 'wrong'
        self.refusal_no_writes(self.install)

    def test_module_note_mismatch(self):
        path = self.stage / 'module-manifest.json'
        value = d.parse_json(path.read_bytes())
        value['modules'][0]['build_note'] = '00'
        path.write_bytes(d.json_bytes(value))
        with self.assertRaises(p.deploy.Refusal):
            self.prepare()

    def test_module_elf_mismatch(self):
        path = self.stage / (d.MODULES[0] + '.ko')
        path.write_bytes(b'not elf')
        value = d.parse_json((self.stage / 'module-manifest.json').read_bytes())
        value['modules'][0]['sha256'] = d.sha(path.read_bytes())
        (self.stage / 'module-manifest.json').write_bytes(d.json_bytes(value))
        with self.assertRaises(p.deploy.Refusal):
            self.prepare()

    def test_staging_file_symlink(self):
        path = self.stage / 'start.sh'
        path.unlink()
        path.symlink_to('/etc/passwd')
        self.refusal_no_writes(self.install)

    def test_staging_directory_symlink(self):
        link = Path(self.temp.name) / 'stage-link'
        link.symlink_to(self.stage, target_is_directory=True)
        self.refusal_no_writes(lambda: self.deploy.install(link, self.pin))

    def test_target_symlink(self):
        self.fs.mkdirs(d.UNIT)
        self.fs.path(d.UNIT).symlink_to('/etc/passwd')
        self.refusal_no_writes(self.install)

    def test_parent_symlink(self):
        self.fs.path('/usr').symlink_to('/tmp', target_is_directory=True)
        self.refusal_no_writes(self.install)

    def test_parent_writable(self):
        self.fs.mkdirs(d.UNIT)
        self.fs.path('/etc/systemd').chmod(0o777)
        self.refusal_no_writes(self.install)

    def test_foreign_enable_symlink(self):
        self.fs.mkdirs(d.WANTS)
        self.fs.path(d.WANTS).symlink_to('/etc/foreign')
        self.refusal_no_writes(self.install)

    def test_changed_file_uninstall_refuses_all_writes(self):
        self.install()
        self.fs.put(d.BASE + '/start.sh', {'data': b'changed', 'mode': 0o755})
        self.refusal_no_writes(self.uninstall)

    def test_changed_mode_uninstall_refuses_all_writes(self):
        self.install()
        self.fs.path(d.BASE + '/start.sh').chmod(0o700)
        self.refusal_no_writes(self.uninstall)

    def test_uninstall_symlink_refuses_all_writes(self):
        self.install()
        path = self.fs.path(d.BASE + '/start.sh')
        path.unlink()
        path.symlink_to('/etc/passwd')
        self.refusal_no_writes(self.uninstall)

    def test_backup_tamper_refuses_all_writes(self):
        self.original()
        self.install()
        backup = self.deploy.record()['baseline']
        self.fs.put(backup + '/file-' + str(sorted(d.TARGETS).index(d.UNIT)), {'data': b'tampered', 'mode': 0o600})
        self.refusal_no_writes(self.uninstall)

    def test_active_service_refuses(self):
        for active in ('active', 'activating', 'deactivating'):
            self.system.active = active
            self.refusal_no_writes(self.install)
        self.system.active = 'inactive'
        self.install()
        self.system.active = 'active'
        self.refusal_no_writes(self.uninstall)

    def test_startup_lock_refuses(self):
        import fcntl
        self.fs.mkdirs(d.LOCK)
        fd = os.open(self.fs.path(d.LOCK), os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.refusal_no_writes(self.install)

    def test_unmanaged_uninstall_refuses(self):
        # preflight refusal must happen before creating even the lock file.
        self.refusal_no_writes(self.uninstall)

    def test_foreign_base_file_retained(self):
        self.install()
        self.fs.put(d.BASE + '/foreign', {'data': b'keep', 'mode': 0o644})
        self.uninstall()
        self.assertEqual(self.fs.read(d.BASE + '/foreign')['data'], b'keep')

    def test_startup_state_retained(self):
        self.fs.put('/var/lib/cupid-adsp/attempts', {'data': b'2\n', 'mode': 0o600})
        self.install()
        self.uninstall()
        self.assertEqual(self.fs.read('/var/lib/cupid-adsp/attempts')['data'], b'2\n')

    def test_fallback_success_is_explicit(self):
        self.install()
        self.uninstall(fallback=True)
        self.assertTrue(self.system.pmic)
        self.assertEqual(self.system.calls.count('modprobe pmic_glink'), 1)

    def test_fallback_failure_is_reported_not_masked(self):
        self.install()
        self.system.fail_modprobe = True
        with self.assertRaisesRegex(d.Refusal, 'uninstall committed.*fallback FAILED'):
            self.uninstall(fallback=True)
        self.assertEqual(self.deploy.record()['status'], 'uninstalled')
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))

    def test_pending_blocks_install_and_uninstall(self):
        self.system.fail_reload = 2
        with self.assertRaises(d.Refusal):
            self.install()
        self.assertIsNotNone(self.fs.read(d.PENDING))
        self.refusal_no_writes(self.install)
        self.refusal_no_writes(self.uninstall)

    def test_recovery_after_failed_rollback(self):
        self.system.fail_reload = 2
        with self.assertRaises(d.Refusal):
            self.install()
        with contextlib.redirect_stdout(io.StringIO()):
            self.deploy.recover()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertIsNone(self.fs.read(d.PENDING))
        self.assertIsNone(self.fs.read(d.STATE))
        self.assertEqual(self.system.enabled(), 'not-found')
        self.install()

    def test_recovery_mixed_files_after_interruption(self):
        before = self.deploy.snapshot()
        files = d.validate_package(self.stage, self.pin)
        backup = self.deploy.backup(before, 'not-found')
        future_dirs = [d.BASE, '/etc/modprobe.d', '/etc/systemd/system/multi-user.target.wants']
        pending = {'schema': 1, 'operation': 'install', 'backup': backup,
                   'after': d.signatures(files), 'state_after': {'sha256': '0' * 64, 'mode': 0o600},
                   'created_dirs': future_dirs, 'new_enabled': 'enabled'}
        self.fs.put(d.PENDING, {'data': d.json_bytes(pending), 'mode': 0o600})
        for name in sorted(d.TARGETS)[:6]:
            self.fs.put(name, files[name])
        self.refusal_no_writes(self.install)
        with contextlib.redirect_stdout(io.StringIO()):
            self.deploy.recover()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertIsNone(self.fs.read(d.PENDING))

    def test_recovery_drift_refuses(self):
        self.system.fail_reload = 2
        with self.assertRaises(d.Refusal):
            self.install()
        self.fs.put(d.BASE + '/start.sh', {'data': b'foreign after interruption', 'mode': 0o755})
        self.refusal_no_writes(self.deploy.recover)

    def test_recovery_failure_retains_pending(self):
        self.system.fail_reload = 3
        with self.assertRaises(d.Refusal):
            self.install()
        with self.assertRaisesRegex(d.Refusal, 'RECOVERY INCOMPLETE'):
            self.deploy.recover()
        self.assertIsNotNone(self.fs.read(d.PENDING))
        with contextlib.redirect_stdout(io.StringIO()):
            self.deploy.recover()
        self.assertIsNone(self.fs.read(d.PENDING))

    def test_unit_install_side_effects_refused(self):
        path = self.stage / 'cupid-adsp.service'
        path.write_bytes(b'[Install]\nWantedBy=multi-user.target\nAlso=foreign.service\n')
        with self.assertRaises(p.deploy.Refusal):
            self.prepare()

    def test_target_hardlink_refused(self):
        self.fs.mkdirs(d.UNIT)
        outside = Path(self.temp.name) / 'foreign-unit'
        outside.write_bytes(b'outside')
        os.link(outside, self.fs.path(d.UNIT))
        self.refusal_no_writes(self.install)

    def test_uninstall_directory_failure_restores_current(self):
        self.install()
        before, state = self.deploy.snapshot(), self.fs.read(d.STATE)
        remove = self.fs.remove_dirs
        failed = False
        def inject(names):
            nonlocal failed
            remove(names)
            if not failed:
                failed = True
                raise OSError('directory cleanup failure after removal')
        with mock.patch.object(self.fs, 'remove_dirs', side_effect=inject):
            with self.assertRaisesRegex(d.Refusal, 'rollback verified'):
                self.uninstall()
        self.assertEqual(self.deploy.snapshot(), before)
        self.assertEqual(self.fs.read(d.STATE), state)
        self.assertEqual(self.system.enabled(), 'enabled')

    def test_reinstall_after_complete_uninstall(self):
        self.install()
        self.uninstall()
        self.install()
        self.assertEqual(self.deploy.record()['status'], 'installed')
        self.uninstall()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))

    def test_prepare_reproducible_and_rejects_extras(self):
        self.assertEqual(self.prepare(), self.pin)
        (self.stage / 'foreign.txt').write_bytes(b'x')
        with self.assertRaises(p.deploy.Refusal):
            self.prepare()

    def test_foreign_dropin_refuses(self):
        self.fs.mkdirs('/etc/systemd/system/cupid-adsp.service.d/override.conf')
        self.refusal_no_writes(self.install)

    def test_foreign_vendor_unit_refuses(self):
        self.fs.put('/usr/lib/systemd/system/cupid-adsp.service', {'data': b'foreign', 'mode': 0o644})
        self.refusal_no_writes(self.install)

    def test_command_failure_and_fallback_timeout_contract(self):
        system = d.System()
        failed = subprocess.CompletedProcess(['/usr/bin/systemctl'], 1, '', 'injected command error')
        with mock.patch.object(subprocess, 'run', return_value=failed):
            with self.assertRaisesRegex(d.Refusal, 'injected command error'):
                system.reload()
        with mock.patch.object(Path, 'is_dir', return_value=False):
            with mock.patch.object(subprocess, 'run', side_effect=subprocess.TimeoutExpired('/sbin/modprobe', 10)) as runner:
                with self.assertRaises(subprocess.TimeoutExpired):
                    system.fallback()
                self.assertEqual(runner.call_args.args[0], ('/sbin/modprobe', 'pmic_glink'))
                self.assertEqual(runner.call_args.kwargs['timeout'], 10)

    def test_module_order_refused_even_with_valid_package_pin(self):
        path = self.stage / 'module-manifest.json'
        value = d.parse_json(path.read_bytes())
        value['modules'][0], value['modules'][1] = value['modules'][1], value['modules'][0]
        path.write_bytes(d.json_bytes(value))
        manifest_path = self.stage / 'package-manifest.json'
        package = d.parse_json(manifest_path.read_bytes())
        next(row for row in package['files'] if row['file'] == path.name)['sha256'] = d.sha(path.read_bytes())
        manifest_path.write_bytes(d.json_bytes(package))
        self.pin = d.sha(manifest_path.read_bytes())
        self.refusal_no_writes(self.install)

    def test_oversize_payload_refused_before_read_or_prepare(self):
        path = self.stage / (d.MODULES[0] + '.ko')
        with path.open('r+b') as stream:
            stream.truncate(16 * 1024 * 1024 + 1)
        self.refusal_no_writes(self.install)
        with self.assertRaises(p.deploy.Refusal):
            self.prepare()

    def test_production_fallback_requires_live_state(self):
        system = d.System()
        with mock.patch.object(Path, 'is_dir', return_value=True), \
                mock.patch.object(d.os, 'open', return_value=999), \
                mock.patch.object(d.os, 'close') as close, \
                mock.patch.object(d.os, 'read', return_value=b'coming\n'):
            with self.assertRaisesRegex(d.Refusal, 'not live'):
                system.fallback()
            close.assert_called_once_with(999)
        with mock.patch.object(Path, 'is_dir', return_value=True), \
                mock.patch.object(d.os, 'open', return_value=999), \
                mock.patch.object(d.os, 'close'), \
                mock.patch.object(d.os, 'read', return_value=b'live\n'):
            system.fallback()

    def test_atime_change_is_not_content_drift(self):
        path = self.stage / 'start.sh'
        before = path.stat()
        os.utime(path, ns=(1, before.st_mtime_ns))
        self.assertEqual(d.staging_file(path), path.read_bytes())
        self.install()

    def test_production_release_gate(self):
        system = d.System()
        with mock.patch.object(d.os, 'uname', return_value=type('Uname', (), {'release': d.KERNEL})()), \
                mock.patch.object(d.os, 'open', return_value=999), \
                mock.patch.object(d.os, 'close') as close, \
                mock.patch.object(d.os, 'read', side_effect=[b'xiaomi,cupid\0', b'wrong-release\n']):
            with self.assertRaisesRegex(d.Refusal, 'release mismatch'):
                system.identity()
            self.assertEqual(close.call_count, 2)

    def test_rollback_failure_reported_and_backup_retained(self):
        self.system.fail_reload = 2
        with self.assertRaisesRegex(d.Refusal, 'ROLLBACK INCOMPLETE.*retained backup'):
            self.install()
        backups = list(self.fs.path('/usr/local/lib/cupid').glob('adsp-deploy-before-*'))
        self.assertEqual(len(backups), 1)
        result = d.parse_json((backups[0] / 'install-result.json').read_bytes())
        self.assertEqual(result['status'], 'rollback-incomplete')


if __name__ == '__main__':
    unittest.main(verbosity=2)
