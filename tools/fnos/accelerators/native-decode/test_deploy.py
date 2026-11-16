#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Linux host-only tests; isolated roots, no host targets, services or network.

Run under Linux/WSL ext4 (DrvFS without metadata cannot enforce 0700/0600):
  python3 -I /absolute/trusted/test_deploy.py --host-test-base /private/fnos
A fresh iris-native-deploy-test-20261115-UUID directory holds every fixture.
The synthetic observer is NOT production admission or hardware acceptance.
"""
import argparse
import copy
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('video_deploy_subject', HERE / 'deploy.py')
d = importlib.util.module_from_spec(spec)
spec.loader.exec_module(d)
BASE = None


def module_elf(name):
    chunks = [b'', b'\0.shstrtab\0.modinfo\0.note.gnu.build-id\0',
              ('name=' + name + '\0vermagic=' + d.VERMAGIC + '\0').encode(),
              struct.pack('<III', 4, 20, 3) + b'GNU\0' + b'\x42' * 20]
    data = bytearray(64)
    offsets = []
    for chunk in chunks:
        offsets.append((len(data), len(chunk)))
        data.extend(chunk)
    table = len(data)
    for index, (offset, length) in enumerate(offsets):
        data.extend(struct.pack('<IIQQQQIIQQ', [0, 1, 11, 20][index], [0, 3, 1, 7][index],
                                0, 0, offset, length, 0, 0, 1, 0))
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, b'\x7fELF\x02\x01\x01' + b'\0' * 9,
                     1, 183, 1, 0, 0, table, 0, 64, 0, 0, 64, 4, 1)
    return bytes(data)


def executable():
    data = bytearray(128)
    struct.pack_into('<16sHHIQQQIHHHHHH', data, 0, b'\x7fELF\x02\x01\x01' + b'\0' * 9,
                     2, 183, 1, 0x400078, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 5, 0, 0x400000, 0, 128, 128, 4096)
    return bytes(data)


class SyntheticObserver:
    """Test-only live/dual-endpoint model; no full ABI or native claim."""
    def __init__(self, host):
        self.host = host
        self.pin = None
        self.calls = 0
        self.live = True
        self.boot = b'FAKE live slot: test only'
        self.host_boot = self.boot
        self.device_boot = self.boot
        self.recovery = b'FAKE recovery: test only'
        self.proofs = {}

    def require_final(self, pin):
        d.require(pin == self.pin and self.live, 'Synthetic admission refused')

    def verify_live(self):
        self.calls += 1
        d.require(self.live and self.recovery and
                  d.sha(self.boot) == d.sha(self.host_boot) == d.sha(self.device_boot),
                  'Synthetic live boot/recovery admission lost')

    def save_host_snapshot(self, tx, before, state):
        path = self.host / Path(tx).name
        path.mkdir(mode=0o700)
        for index, name in enumerate(sorted(d.TARGETS)):
            if before[name] is not None:
                p = path / ('file-' + str(index))
                p.write_bytes(before[name]['data'])
                p.chmod(0o600)
        if state is not None:
            p = path / 'state'
            p.write_bytes(state['data'])
            p.chmod(0o600)
        self.proofs[tx] = (d.signatures(before), d.signature(state))

    def verify_host_snapshot(self, tx, before, state):
        d.require(self.proofs.get(tx) == (d.signatures(before), d.signature(state)), 'Host snapshot record mismatch')
        path = self.host / Path(tx).name
        for index, name in enumerate(sorted(d.TARGETS)):
            p = path / ('file-' + str(index))
            if before[name] is not None:
                d.require(p.read_bytes() == before[name]['data'], 'Independent host backup hash mismatch')
            else:
                d.require(not p.exists(), 'Unexpected host backup file')
        if state is not None:
            d.require((path / 'state').read_bytes() == state['data'], 'Host state backup mismatch')


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix='case-', dir=BASE))
        self.addCleanup(shutil.rmtree, self.temp)
        self.root = self.temp / 'root'
        self.stage = self.temp / 'stage'
        self.private = self.temp / 'private'
        self.host = self.private / 'host'
        for p in (self.root, self.stage, self.private, self.host):
            p.mkdir(mode=0o700)
        self.fs = d.Filesystem(self.root)
        self.addCleanup(self.fs.close)
        self.observer = SyntheticObserver(self.host)
        self.deploy = d.Deployment(self.fs, self.observer)
        self.manifest = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'files': []}
        for name, (target, mode) in d.FILES.items():
            data = module_elf(name[:-3]) if name.endswith('.ko') else executable() if name == 'native-decode-runner' else d.SERVICE
            p = self.stage / name
            p.write_bytes(data)
            p.chmod(mode)
            elf = ({'name': name[:-3], 'vermagic': d.VERMAGIC, 'build_note': d.elf_identity(data, True)[2]}
                   if name.endswith('.ko') else {'machine': 'aarch64', 'kind': 'executable'}
                   if name == 'native-decode-runner' else None)
            self.manifest['files'].append({'file': name, 'target': target, 'mode': mode,
                                           'sha256': d.sha(data), 'elf': elf})
        self.repin()
        firmware = self.private / 'source-firmware.bin'
        firmware.write_bytes(b'SYNTHETIC firmware; not vendor data')
        firmware.chmod(0o600)
        self.firmware_manifest = self.private / 'firmware.json'
        self.firmware_manifest.write_bytes(d.json_bytes({'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD,
                                                        'firmware': {'path': str(firmware), 'sha256': d.sha(firmware.read_bytes())}}))
        self.firmware_manifest.chmod(0o600)
        self.firmware_pin = d.sha(self.firmware_manifest.read_bytes())
        # Canary paths are INSIDE fake root, never real /etc, /lib or /dev.
        self.canaries = {'etc/wifi.conf': b'wifi baseline', 'etc/systemd/system/cupid-usb.service': b'usb baseline',
                         'etc/nas.conf': b'nas baseline', 'lib/modules/untouched': b'module tree',
                         'dev/sda38': b'fake block marker', 'boot/current.img': b'fake boot marker'}
        for name, data in self.canaries.items():
            p = self.root / name
            p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            p.write_bytes(data)
            p.chmod(0o600)
        # Absolute filesystem opens are denied outside THIS test sandbox; all
        # deployment dirfd-relative opens remain confined by no-follow traversal.
        original_open = os.open
        def confined_open(path, flags, mode=0o777, *, dir_fd=None):
            if isinstance(path, (str, bytes, os.PathLike)) and os.path.isabs(path):
                d.require(Path(path).is_relative_to(self.temp), 'Offline attempted absolute host target')
            return original_open(path, flags, mode, dir_fd=dir_fd)
        for patcher in (mock.patch.object(os, 'open', side_effect=confined_open),
                        mock.patch.object(subprocess, 'run', side_effect=AssertionError('Host subprocess forbidden')),
                        mock.patch.object(os, 'system', side_effect=AssertionError('Host shell forbidden')),
                        mock.patch.object(socket, 'socket', side_effect=AssertionError('Network/ports forbidden'))):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.check_canaries)

    def check_canaries(self):
        for name, data in self.canaries.items():
            self.assertEqual((self.root / name).read_bytes(), data)

    def repin(self):
        p = self.stage / 'package-manifest.json'
        p.write_bytes(d.json_bytes(self.manifest))
        p.chmod(0o600)
        self.pin = d.sha(p.read_bytes())
        self.observer.pin = self.pin

    def payloads(self):
        return d.validate_package(self.stage, self.pin)

    def apply(self):
        return self.deploy.apply(self.payloads(), self.pin)

    def tree(self):
        return {str(p.relative_to(self.root)): (os.readlink(p) if p.is_symlink() else
                p.read_bytes() if p.is_file() else None, p.lstat().st_mode)
                for p in self.root.rglob('*')}

    def no_write_refusal(self, fn):
        before = self.tree()
        with self.assertRaises((d.Refusal, OSError)):
            fn()
        self.assertEqual(before, self.tree())

    def test_prepare_only_no_target_writes_no_acceptance(self):
        before = self.tree()
        plan = d.prepare_only(self.stage, self.pin, self.firmware_manifest, self.firmware_pin)
        self.assertEqual(before, self.tree())
        self.assertEqual(plan['boot_dt_plan']['required_current_backup_bytes'], 201326592)
        self.assertIn('NOT verified', plan['boot_dt_plan']['prerequisites_status'])
        self.assertNotIn(str(self.private), str(plan))
        self.assertNotIn('SYNTHETIC firmware', str(plan))

    def test_production_all_persistent_actions_block_before_open(self):
        for action in ('apply', 'rollback', 'recover'):
            args = [action, str(self.stage), self.pin, '--firmware-manifest', str(self.firmware_manifest),
                    '--firmware-manifest-sha256', self.firmware_pin] if action == 'apply' else [action, self.pin]
            args += ['--admission', str(self.private / 'missing-independent-admission.json')]
            with mock.patch.object(d, 'Filesystem', side_effect=AssertionError('Production filesystem must not open')), \
                 mock.patch.object(d, 'read_trust', side_effect=d.Refusal('Synthetic missing fixed trust; no real /etc reads')):
                self.assertEqual(d.main(args), 1)
        self.no_write_refusal(lambda: d.Deployment(self.fs, d.ProductionAdmission()).apply(self.payloads(), self.pin))

    def test_apply_then_actual_snapshot_rollback_modes(self):
        for i, name in enumerate(sorted(d.TARGETS)):
            self.fs.put(name, {'data': ('original-' + str(i)).encode(), 'mode': 0o640 if name == d.UNIT else 0o750}, None)
        original = self.deploy.snapshot()
        tx = self.apply()
        self.assertEqual(self.deploy.snapshot(), self.payloads())
        self.assertGreaterEqual(self.observer.calls, 6)
        self.assertEqual(self.fs.read(tx + '/before.json')['mode'], 0o600)
        self.assertEqual(self.fs.read(d.PENDING), None)
        self.deploy.rollback(self.pin)
        self.assertEqual(self.deploy.snapshot(), original)
        self.assertIsNone(self.fs.read(d.STATE))
        self.assertTrue((self.root / tx.lstrip('/')).exists())
        self.assertFalse((self.root / 'etc/systemd/system/multi-user.target.wants').exists())

    def test_absent_snapshot_rollback_not_delete_foreign(self):
        self.apply()
        self.deploy.rollback(self.pin)
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))

    def test_reinstall_and_wrong_rollback_pin_refused(self):
        self.apply()
        self.no_write_refusal(self.apply)
        self.no_write_refusal(lambda: self.deploy.rollback('0' * 64))

    def test_manifest_duplicate_json_nested_and_unknown_boolean(self):
        for raw in (b'{"schema":1,"schema":1}', b'{"a":{"x":1,"x":2}}', b'{"a":NaN}'):
            with self.assertRaises(d.Refusal):
                d.parse_json(raw)
        self.manifest['native_ready'] = True
        self.repin()
        self.no_write_refusal(self.apply)

    def test_forbidden_targets_boot_dt_wifi_usb_nas_module_tree(self):
        original = copy.deepcopy(self.manifest)
        for path in ('/boot/a.img', '/dev/sda38', '/etc/wifi.conf', '/etc/nas.conf',
                     '/etc/systemd/system/cupid-usb.service', '/lib/modules/x/iris.ko',
                     d.BASE + '/../evil', d.BASE + '//evil', '/etc/modprobe.d/native.conf'):
            with self.subTest(path=path):
                self.manifest = copy.deepcopy(original)
                self.manifest['files'][0]['target'] = path
                self.repin()
                self.no_write_refusal(self.apply)
                self.no_write_refusal(lambda: self.fs.put(path, {'data': b'bad', 'mode': 0o600}, None))

    def test_duplicate_record_and_bad_mode(self):
        original = copy.deepcopy(self.manifest)
        self.manifest['files'][1] = copy.deepcopy(self.manifest['files'][0])
        self.repin()
        self.no_write_refusal(self.apply)
        for mode in (True, 0o666, 0o777, 0o4755, -1):
            self.manifest = copy.deepcopy(original)
            self.manifest['files'][0]['mode'] = mode
            self.repin()
            self.no_write_refusal(self.apply)

    def test_wrong_kernel_board_schema_package_hash(self):
        original = copy.deepcopy(self.manifest)
        for key, value in (('kernel', 'other'), ('board', 'qcom,other'), ('schema', True)):
            self.manifest = copy.deepcopy(original)
            self.manifest[key] = value
            self.repin()
            self.no_write_refusal(self.apply)
        with self.assertRaises(d.Refusal):
            d.validate_package(self.stage, 'a' * 64)

    def test_source_symlink_hardlink_worldwrite_extra_file(self):
        p = self.stage / 'native-decode-runner'
        data = p.read_bytes()
        p.chmod(0o777)
        self.no_write_refusal(self.apply)
        p.chmod(0o755)
        extra = self.temp / 'linked'
        os.link(p, extra)
        self.no_write_refusal(self.apply)
        extra.unlink()
        p.unlink()
        p.symlink_to(self.root / 'etc/wifi.conf')
        self.no_write_refusal(self.apply)
        p.unlink()
        p.write_bytes(data)
        p.chmod(0o755)
        (self.stage / 'extra').write_bytes(b'bad')
        self.no_write_refusal(self.apply)

    def test_source_parent_symlink_and_worldwrite(self):
        alias = self.temp / 'alias'
        alias.symlink_to(self.stage, target_is_directory=True)
        with self.assertRaises(d.Refusal):
            d.validate_package(alias, self.pin)
        self.stage.chmod(0o777)
        self.no_write_refusal(self.apply)

    def test_module_wrong_elf_identity_and_executable_script(self):
        for name, data in (('cupid_qcom_iris.ko', module_elf('wrong_module')),
                           ('native-decode-runner', b'#!/bin/sh\nmodprobe iris\n')):
            p = self.stage / name
            original = p.read_bytes()
            p.write_bytes(data)
            record = next(x for x in self.manifest['files'] if x['file'] == name)
            record['sha256'] = d.sha(data)
            self.repin()
            self.no_write_refusal(self.apply)
            p.write_bytes(original)
            record['sha256'] = d.sha(original)
            self.repin()

    def test_service_enable_bypass_side_effects_rejected(self):
        p = self.stage / 'cupid-native-decode.service'
        original = p.read_bytes()
        record = next(x for x in self.manifest['files'] if x['file'] == p.name)
        for addition in (b'[Install]\nWantedBy=multi-user.target\n', b'ExecStartPost=/sbin/modprobe iris\n',
                         b'Environment=IOMMU_BYPASS=1\n'):
            p.write_bytes(original + addition)
            record['sha256'] = d.sha(p.read_bytes())
            self.repin()
            self.no_write_refusal(self.apply)

    def test_target_symlink_parent_and_worldwrite(self):
        target = self.root / d.BASE.lstrip('/')
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.symlink_to(self.stage, target_is_directory=True)
        self.no_write_refusal(self.apply)
        target.unlink()
        target.mkdir(mode=0o777)
        target.chmod(0o777)  # defeat host umask: actually test unsafe permissions
        self.no_write_refusal(self.apply)
        target.chmod(0o700)
        p = target / 'cupid_qcom_iris.ko'
        p.symlink_to(self.root / 'etc/wifi.conf')
        self.no_write_refusal(self.apply)
        p.unlink()
        p.write_bytes(b'bad')
        p.chmod(0o666)
        self.no_write_refusal(self.apply)

    def test_dual_boot_recovery_and_unknown_admission_refused(self):
        self.observer.host_boot = b'corrupt'
        self.no_write_refusal(self.apply)
        self.observer.host_boot = self.observer.boot
        self.observer.recovery = b''
        self.no_write_refusal(self.apply)
        self.observer.recovery = b'fixture'
        self.observer.live = False
        self.no_write_refusal(self.apply)

    def test_file_failure_before_and_after_replace_auto_restore(self):
        real = self.fs.put
        victim = sorted(d.TARGETS)[1]
        for after_replace in (False, True):
            fired = False
            def fail(name, item, expected):
                nonlocal fired
                if name == victim and not fired and item is not None:
                    fired = True
                    if after_replace:
                        real(name, item, expected)
                    raise OSError('injected actual file failure')
                return real(name, item, expected)
            with mock.patch.object(self.fs, 'put', side_effect=fail):
                with self.assertRaises(d.Refusal):
                    self.apply()
            self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
            self.assertIsNone(self.fs.read(d.PENDING))
            self.assertIsNone(self.fs.read(d.STATE))

    def interrupt(self, victim):
        real = self.fs.put
        def stop(name, item, expected):
            result = real(name, item, expected)
            if name == victim:
                raise KeyboardInterrupt('simulated abrupt interruption after durable write')
            return result
        with mock.patch.object(self.fs, 'put', side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                self.apply()
        self.assertIsNotNone(self.fs.read(d.PENDING))

    def test_interruption_recovery_at_each_file_state_and_commit_marker(self):
        for victim in [*sorted(d.TARGETS), d.STATE, d.PENDING]:
            with self.subTest(victim=victim):
                self.interrupt(victim)
                self.deploy.recover()
                self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
                self.assertIsNone(self.fs.read(d.STATE))
                self.assertIsNone(self.fs.read(d.PENDING))

    def test_fsync_failure_after_rename_restores(self):
        original_fsync = os.fsync
        victim = sorted(d.TARGETS)[0]
        original_replace = os.replace
        armed = False
        fired = False
        def replace(src, dst, **kwargs):
            nonlocal armed
            result = original_replace(src, dst, **kwargs)
            if dst == Path(victim).name:
                armed = True
            return result
        def sync(fd):
            nonlocal fired
            if armed and not fired:
                fired = True
                raise OSError('injected directory fsync failure')
            return original_fsync(fd)
        with mock.patch.object(os, 'replace', side_effect=replace), mock.patch.object(os, 'fsync', side_effect=sync):
            with self.assertRaises(d.Refusal):
                self.apply()
        self.assertTrue(fired)
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))
        self.assertIsNone(self.fs.read(d.PENDING))

    def test_conflict_rollback_and_recovery_zero_target_writes(self):
        self.apply()
        name = sorted(d.TARGETS)[0]
        self.fs.put(name, {'data': b'foreign edit', 'mode': 0o644}, self.fs.read(name))
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))
        # Restore exact admitted after bytes then create an interrupted rollback.
        self.fs.put(name, self.payloads()[name], self.fs.read(name))
        real = self.fs.put
        def interrupt(name_, item, expected):
            result = real(name_, item, expected)
            if name_ == sorted(d.TARGETS)[1]:
                raise KeyboardInterrupt('rollback interruption')
            return result
        with mock.patch.object(self.fs, 'put', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.deploy.rollback(self.pin)
        self.fs.put(name, {'data': b'foreign edit', 'mode': 0o644}, self.fs.read(name))
        self.no_write_refusal(self.deploy.recover)
        self.assertIsNotNone(self.fs.read(d.PENDING))

    def test_device_and_host_backup_corruption_refuses(self):
        tx = self.apply()
        p = self.root / (tx + '/file-0').lstrip('/')
        # Initially absent baseline: corrupt metadata instead of inventing backup.
        meta = self.fs.read(tx + '/before.json')
        p.write_bytes(b'not in actual baseline')
        p.chmod(0o600)
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))
        p.unlink()
        # Independently stored host backup absence also checked.
        host_file = self.host / Path(tx).name / 'file-0'
        host_file.write_bytes(b'foreign host file')
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))
        host_file.unlink()
        self.fs.put(tx + '/before.json', {'data': b'{"schema":1,"schema":1}', 'mode': 0o600}, meta)
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))

    def test_live_gate_loss_retains_pending_and_recovers_only_after_restored(self):
        real = self.fs.put
        victim = sorted(d.TARGETS)[0]
        def lose(name, item, expected):
            result = real(name, item, expected)
            if name == victim:
                self.observer.live = False
            return result
        with mock.patch.object(self.fs, 'put', side_effect=lose):
            with self.assertRaises(d.Refusal):
                self.apply()
        self.assertIsNotNone(self.fs.read(d.PENDING))
        self.no_write_refusal(self.deploy.recover)
        self.observer.live = True
        self.deploy.recover()
        self.assertTrue(all(item is None for item in self.deploy.snapshot().values()))

    def test_local_flock_contender_refused(self):
        with self.fs.lock():
            self.no_write_refusal(self.apply)

    def test_private_firmware_hash_location_and_boolean_refused(self):
        self.firmware_manifest.chmod(0o644)
        with self.assertRaises(d.Refusal):
            d.validate_firmware(self.firmware_manifest, self.firmware_pin)
        self.firmware_manifest.chmod(0o600)
        value = d.parse_json(self.firmware_manifest.read_bytes())
        value['trusted'] = True
        self.firmware_manifest.write_bytes(d.json_bytes(value))
        with self.assertRaises(d.Refusal):
            d.validate_firmware(self.firmware_manifest, d.sha(self.firmware_manifest.read_bytes()))

    def test_oversize_source_refused(self):
        p = self.stage / 'cupid_qcom_iris.ko'
        with p.open('wb') as stream:
            stream.truncate(d.LIMIT + 1)
        self.no_write_refusal(self.apply)

    def test_atomic_cas_conflict_retains_foreign_bytes(self):
        name = sorted(d.TARGETS)[0]
        self.fs.put(name, {'data': b'foreign', 'mode': 0o644}, None)
        self.no_write_refusal(lambda: self.fs.put(name, {'data': b'replace', 'mode': 0o644}, None))

    def test_recovery_pending_paths_duplicate_keys_and_state_drift(self):
        self.interrupt(sorted(d.TARGETS)[0])
        item = self.fs.read(d.PENDING)
        value = d.parse_json(item['data'])
        value['tx'] = '/etc/wifi.conf'
        self.fs.put(d.PENDING, {'data': d.json_bytes(value), 'mode': 0o600}, item)
        self.no_write_refusal(self.deploy.recover)
        self.fs.put(d.PENDING, {'data': b'{"schema":1,"schema":1}', 'mode': 0o600}, self.fs.read(d.PENDING))
        self.no_write_refusal(self.deploy.recover)
        self.fs.put(d.PENDING, item, self.fs.read(d.PENDING))
        self.fs.put(d.STATE, {'data': b'foreign state', 'mode': 0o600}, None)
        self.no_write_refusal(self.deploy.recover)

    def test_actual_nonempty_backup_hash_corruption(self):
        name = sorted(d.TARGETS)[0]
        self.fs.put(name, {'data': b'actual original asset', 'mode': 0o640}, None)
        tx = self.apply()
        backup = tx + '/file-0'
        original = self.fs.read(backup)
        self.fs.put(backup, {'data': b'corrupt device snapshot', 'mode': 0o600}, original)
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))
        self.fs.put(backup, original, self.fs.read(backup))
        (self.host / Path(tx).name / 'file-0').write_bytes(b'corrupt host snapshot')
        self.no_write_refusal(lambda: self.deploy.rollback(self.pin))

    def test_transaction_concurrent_foreign_edit_is_not_overwritten(self):
        real = self.fs.put
        victim = sorted(d.TARGETS)[0]
        def foreign(name, item, expected):
            result = real(name, item, expected)
            if name == victim and item is not None:
                real(name, {'data': b'concurrent foreign content', 'mode': 0o644}, item)
            return result
        with mock.patch.object(self.fs, 'put', side_effect=foreign):
            with self.assertRaises(d.Refusal):
                self.apply()
        self.assertEqual(self.fs.read(victim)['data'], b'concurrent foreign content')
        self.assertIsNotNone(self.fs.read(d.PENDING))
        self.no_write_refusal(self.deploy.recover)

    def test_fifo_and_state_mode_rejected_without_hang_or_write(self):
        p = self.stage / 'native-decode-runner'
        p.unlink()
        os.mkfifo(p, 0o600)
        self.no_write_refusal(self.apply)
        self.no_write_refusal(lambda: self.fs.put(d.STATE, {'data': b'bad mode', 'mode': 0o644}, None))

    def admission_fixture(self):
        # Actual AdmissionReader parses these independent files. All producers,
        # receipts and observations here are SYNTHETIC; NEVER production proof.
        import time
        producers = {kind: ('SYNTHETIC fixed producer ' + kind).encode() for kind in d.PRODUCERS}
        ppins = {kind: d.sha(raw) for kind, raw in producers.items()}
        records, epins, reports = {}, {}, {}
        for kind in d.PRODUCERS:
            details = ({key: 'verified' for key in d.LIFECYCLE_CONTRACTS} if kind == 'lifecycle' else
                       {'coverage': 'complete-applicable', 'unknown': 0, 'mismatches': 0} if kind == 'abi' else
                       {'assets': d.signatures(self.payloads()), 'runner_audit': 'verified', 'service_audit': 'verified'})
            report = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'kind': kind,
                      'producer_sha256': ppins[kind], 'package_sha256': self.pin,
                      'firmware_manifest_sha256': self.firmware_pin, 'result': 'verified-for-staging', 'details': details}
            path = self.private / (kind + '-synthetic-evidence.json')
            path.write_bytes(d.json_bytes(report)); path.chmod(0o600)
            epins[kind] = d.sha(path.read_bytes())
            records[kind] = {'path': str(path), 'sha256': epins[kind]}
            reports[kind] = report
        boot = self.private / 'device-boot-synthetic.bin'
        boot.write_bytes(b'\0' * 4096); boot.chmod(0o600)
        now = time.time()
        live = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'boot_id': 'synthetic-current-boot',
                'nonce': '9' * 32, 'issued_at': now, 'expires_at': now + 15,
                'producer_sha256': ppins['lifecycle'], 'package_sha256': self.pin, 'slot': 'boot_a',
                'boot_sha256': d.sha(boot.read_bytes()), 'boot_size': 4096, 'battery_percent': 80,
                'runtime': 'inactive-unbound-no-enablement-or-overrides', 'authentication': 'secure-enforced',
                'iommu': 'translation-enforced', 'recovery': 'independently-verified-usable',
                'assets': d.signatures(self.deploy.snapshot())}
        receipt = {key: live[key] for key in ('schema', 'kernel', 'board', 'producer_sha256', 'package_sha256',
                                            'boot_id', 'nonce', 'issued_at', 'expires_at', 'slot', 'boot_size', 'boot_sha256')}
        receipt.update({'device_before_sha256': live['boot_sha256'], 'device_after_sha256': live['boot_sha256'],
                        'host_boot_sha256': live['boot_sha256'], 'host_boot_size': 4096,
                        'recovery_sha256': d.sha(b'SYNTHETIC host recovery'), 'recovery_size': 23})
        rp = self.private / 'synthetic-host-receipt.json'
        rp.write_bytes(d.json_bytes(receipt)); rp.chmod(0o600)
        value = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'phase': 'validated-staging',
                 'package_sha256': self.pin, 'firmware_manifest_sha256': self.firmware_pin, 'evidence': records,
                 'backups': {'device_boot': {'path': str(boot), 'sha256': live['boot_sha256'], 'size': 4096},
                             'host_receipt': {'path': str(rp), 'sha256': d.sha(rp.read_bytes())},
                             'recovery': {'sha256': receipt['recovery_sha256'], 'size': receipt['recovery_size']}},
                 'root_snapshots': [{'before': d.signatures(self.deploy.snapshot()), 'state': None,
                                     'files': {name: None for name in d.TARGETS}, 'state_file': None}]}
        ap = self.private / 'synthetic-admission.json'
        ap.write_bytes(d.json_bytes(value)); ap.chmod(0o600)
        reader = d.AdmissionReader(str(ap), d.sha(ap.read_bytes()), ppins, epins,
                                   producer_read=lambda path: producers[next(k for k, p in d.PRODUCERS.items() if p == path)])
        trust = {'schema': 1, 'kernel': d.KERNEL, 'board': d.BOARD, 'observer_sha256': '1' * 64,
                 'producer_pins': ppins, 'evidence_pins': epins, 'admission_sha256': reader.pin,
                 'receipt_sha256': d.sha(rp.read_bytes()), 'nonce': live['nonce']}
        return reader, trust, value, reports, live, receipt

    def test_actual_independent_reader_valid_stage_no_native_acceptance_dependency(self):
        reader, trust, value, reports, live, receipt = self.admission_fixture()
        checked = reader.read(self.pin)
        self.assertEqual(checked['accepted_assets'], d.signatures(self.payloads()))
        self.assertEqual(checked['phase'], 'validated-staging')
        self.assertNotIn('native_ready', checked)
        observer = mock.Mock()
        observer.observe.return_value = live
        admission = d.ProductionAdmission(reader, observer, trust)
        before = self.tree()
        # Synthetic short image size is local to this test, NOT a CLI override.
        with mock.patch.object(d, 'read_trust', return_value=trust), mock.patch.object(d, 'BOOT_SIZE', 4096):
            admission.require_final(self.pin)
            admission.verify_live()
            admission.verify_host_snapshot(d.CONTROL + '/tx-' + '0' * 32, self.deploy.snapshot(), None)
        self.assertEqual(before, self.tree())
        self.assertFalse((self.root / 'run/cupid-native-decode.lock').exists())

    def test_reader_producer_report_pin_selfbool_unknown_and_binding_rejections(self):
        reader, trust, value, reports, live, receipt = self.admission_fixture()
        with mock.patch.object(reader, 'producer_read', return_value=b'wrong producer'):
            with self.assertRaises(d.Refusal): reader.read(self.pin)
        for kind, mutation in (('abi', lambda r: r['details'].update(unknown=1)),
                               ('lifecycle', lambda r: r['details'].update(remove='unknown')),
                               ('package', lambda r: r.update(package_sha256='0' * 64)),
                               ('package', lambda r: r.update(native_ready=True))):
            report = copy.deepcopy(reports[kind]); mutation(report)
            path = Path(value['evidence'][kind]['path'])
            original = path.read_bytes()
            path.write_bytes(d.json_bytes(report))
            # First reject fixed external report pin, then inspect semantics with
            # a deliberately re-pinned SYNTHETIC test trust.
            with self.assertRaises(d.Refusal): reader.read(self.pin)
            new_pin = d.sha(path.read_bytes())
            old_pin = reader.evidence_pins[kind]
            reader.evidence_pins[kind] = new_pin
            value['evidence'][kind]['sha256'] = new_pin
            Path(reader.path).write_bytes(d.json_bytes(value)); reader.pin = d.sha(Path(reader.path).read_bytes())
            with self.assertRaises(d.Refusal): reader.read(self.pin)
            path.write_bytes(original); reader.evidence_pins[kind] = old_pin
            value['evidence'][kind]['sha256'] = old_pin
            Path(reader.path).write_bytes(d.json_bytes(value)); reader.pin = d.sha(Path(reader.path).read_bytes())

    def test_actual_adapter_receipt_bootid_slot_beforeafter_and_recovery_rejections(self):
        reader, trust, value, reports, live, receipt = self.admission_fixture()
        observer = mock.Mock(); observer.observe.return_value = live
        admission = d.ProductionAdmission(reader, observer, trust)
        rp = Path(value['backups']['host_receipt']['path'])
        for key, bad in (('boot_id', 'old-boot'), ('slot', 'boot_b'), ('device_after_sha256', '0' * 64),
                         ('host_boot_size', 1024), ('issued_at', 0), ('recovery_sha256', '0' * 64),
                         ('producer_sha256', '0' * 64), ('nonce', '0' * 32)):
            with self.subTest(key=key):
                changed = copy.deepcopy(receipt); changed[key] = bad
                rp.write_bytes(d.json_bytes(changed)); pin = d.sha(rp.read_bytes())
                trust['receipt_sha256'] = pin; value['backups']['host_receipt']['sha256'] = pin
                Path(reader.path).write_bytes(d.json_bytes(value)); reader.pin = d.sha(Path(reader.path).read_bytes())
                with mock.patch.object(d, 'read_trust', return_value=trust), mock.patch.object(d, 'BOOT_SIZE', 4096):
                    admission.require_final(self.pin)
                    with self.assertRaises(d.Refusal): admission.verify_live()

    def test_actual_live_observer_identity_freshness_and_current_asset_hash(self):
        import io
        reader, trust, value, reports, live, receipt = self.admission_fixture()
        observer = d.ReadOnlyLiveObserver(self.fs, reader, live['nonce'])
        # Real observer code, fake proc file descriptors: no host /proc reads.
        kernel = mock.Mock(release=d.KERNEL)
        def proc_open(path, flags):
            raw = d.BOARD.encode() + b'\0' if path.endswith('compatible') else live['boot_id'].encode()
            p = self.private / ('fake-compatible' if path.endswith('compatible') else 'fake-bootid')
            p.write_bytes(raw); p.chmod(0o600)
            return original_open(p, flags)
        original_open = os.open
        original_source = d.source_read
        with mock.patch.object(os, 'uname', return_value=kernel), mock.patch.object(os, 'open', side_effect=proc_open), \
             mock.patch.object(d, 'source_read', side_effect=lambda *a, **k: d.json_bytes(live)), mock.patch.object(d, 'BOOT_SIZE', 4096):
            # fs snapshot reads are known absent; avoid recursively fake-opening
            # their dirfds while exercising the live observer proc path.
            with mock.patch.object(self.fs, 'read', return_value=None):
                self.assertEqual(observer.observe(self.pin)['slot'], 'boot_a')
                live['assets'][sorted(d.TARGETS)[0]] = {'sha256': '0' * 64, 'mode': 0o644}
                with self.assertRaises(d.Refusal): observer.observe(self.pin)
                live['assets'] = {name: None for name in d.TARGETS}
                live['boot_id'] = 'stale-other-id'
                # boot-id fd also follows mutable live, so use fixed actual id.
                with mock.patch.object(os, 'read', return_value=b'actual-different-id'):
                    with self.assertRaises(d.Refusal): observer.observe(self.pin)
                kernel.release = 'other-kernel'
                with self.assertRaises(d.Refusal): observer.observe(self.pin)

    def test_fixed_trust_observer_source_pin_and_no_cli_chosen_producers(self):
        reader, trust, value, reports, live, receipt = self.admission_fixture()
        source = b'SYNTHETIC observer source'
        trust['observer_sha256'] = d.sha(source)
        def trusted_read(path, *args, **kwargs):
            return d.json_bytes(trust) if path == d.TRUST else source
        with mock.patch.object(d, 'source_read', side_effect=trusted_read):
            self.assertEqual(d.read_trust(), trust)
            trust['observer_sha256'] = '0' * 64
            with self.assertRaises(d.Refusal): d.read_trust()

    def test_full_201326592_sparse_backup_streaming_without_host_copy(self):
        path = self.private / 'sparse-device-boot-test.bin'
        with path.open('wb') as stream: stream.truncate(201326592)
        path.chmod(0o600)
        import hashlib
        h = hashlib.sha256()
        with path.open('rb') as stream:
            while chunk := stream.read(1024 * 1024): h.update(chunk)
        record = {'path': str(path), 'size': 201326592, 'sha256': h.hexdigest()}
        self.assertEqual(d.stream_private_hash(record, d.BOOT_SIZE), h.hexdigest())
        record['size'] -= 1
        with self.assertRaises(d.Refusal): d.stream_private_hash(record, d.BOOT_SIZE)

    def test_backup_current_partial_state_before_recovery(self):
        self.interrupt(sorted(d.TARGETS)[1])
        current = self.deploy.snapshot()
        prior = set(self.observer.proofs)
        self.deploy.recover()
        new = set(self.observer.proofs) - prior
        self.assertEqual(len(new), 1)
        self.assertEqual(self.observer.proofs[new.pop()][0], d.signatures(current))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--host-test-base', required=True, help='existing private host directory on Linux ext4')
    args, remaining = parser.parse_known_args()
    require_base = Path(args.host_test_base).absolute()
    if os.name != 'posix' or d.fcntl is None:
        parser.error('Linux/WSL required; no permissive Windows fallback')
    for p in (require_base, *require_base.parents):
        if p.is_symlink():
            parser.error('host-test-base symlink refused')
    require_base.mkdir(parents=True, exist_ok=True, mode=0o700)
    BASE = require_base / ('iris-native-deploy-test-20261115-' + uuid.uuid4().hex)
    BASE.mkdir(mode=0o700)
    try:
        d.checked_dir(BASE.lstat(), private=True)
    except d.Refusal as exc:
        BASE.rmdir()
        parser.error(str(exc) + '; use ext4, not permissionless DrvFS')
    print('HOST TEST DIRECTORY: ' + str(BASE), flush=True)
    unittest.main(argv=[__file__, *remaining])
