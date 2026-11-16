#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
import importlib.util
import errno
import json
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'cupid-storage.py'
spec = importlib.util.spec_from_file_location('cupid_storage', SOURCE)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


class ConfigTests(unittest.TestCase):
    def config(self):
        return {
            'backing_partuuid': '11111111-1111-1111-1111-111111111111',
            'parent_ptuuid': '22222222-2222-2222-2222-222222222222',
            'start_sector_512': 2048,
            'size_sector_512': 16384,
            'logical_block_size': 4096,
            'virtual_serial': '123456789abcdef0',
            'readonly': True,
        }

    def load(self, config):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            path.write_text(json.dumps(config))
            return adapter.load_config(path)

    def test_valid_readonly_config(self):
        self.assertTrue(self.load(self.config())['readonly'])

    def test_unknown_key_rejected(self):
        config = self.config()
        config['device'] = '/dev/sda'
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_unaligned_geometry_rejected(self):
        config = self.config()
        config['start_sector_512'] = 1
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_string_readonly_rejected(self):
        config = self.config()
        config['readonly'] = 'false'
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_boolean_sector_count_rejected(self):
        config = self.config()
        config['size_sector_512'] = True
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_virtual_identity_cannot_be_a_path(self):
        config = self.config()
        config['virtual_serial'] = '../disk'
        with self.assertRaises(RuntimeError):
            self.load(config)

    def test_ambiguous_partition_lookup_rejected(self):
        with mock.patch.object(adapter, 'command', return_value='/dev/one\n/dev/two'):
            with self.assertRaisesRegex(RuntimeError, 'Exactly one'):
                adapter.verify_backing(self.config())

    def test_mounted_backing_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'dev').write_text('8:1')
            (path / 'holders').mkdir()
            with mock.patch.object(adapter, 'mounted_devices', return_value={'8:1'}):
                with self.assertRaisesRegex(RuntimeError, 'mounted'):
                    adapter.check_unused(path)

    def test_unknown_holder_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'dev').write_text('8:1')
            (path / 'holders').mkdir()
            (path / 'holders/unknown').mkdir()
            with mock.patch.object(adapter, 'mounted_devices', return_value=set()):
                with self.assertRaisesRegex(RuntimeError, 'active holders'):
                    adapter.check_unused(path)


class MountinfoTests(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.object(adapter, 'exited_mountinfo', return_value=False)
        self.exited = patch.start()
        self.addCleanup(patch.stop)

    def mount(self, device, source='/not-a-device', filesystem='ext4'):
        return f'20 1 {device} / /mnt rw - {filesystem} {source} rw\n'

    def scan(self, snapshots):
        root = mock.Mock()
        root.glob.side_effect = snapshots
        return mock.patch.object(adapter, 'Path', return_value=root), root

    def test_confirmed_exit_skipped_without_retry(self):
        self.exited.return_value = True
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
        patch, root = self.scan([[stale]])
        with patch, mock.patch.object(adapter.time, 'sleep') as sleep:
            self.assertEqual(adapter.mountinfo_devices(), set())
        self.assertEqual(root.glob.call_count, 1)
        sleep.assert_not_called()

    def test_exit_check_error_not_swallowed(self):
        self.exited.side_effect = PermissionError(errno.EACCES, 'cannot verify exit')
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
        patch, _ = self.scan([[stale]])
        with patch:
            with self.assertRaises(PermissionError):
                adapter.mountinfo_devices()

    def test_exited_process_einval_reenumerates(self):
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
        live = mock.Mock()
        live.read_text.return_value = self.mount('8:1')
        patch, root = self.scan([[stale], [live]])
        with patch, mock.patch.object(adapter.time, 'sleep') as sleep:
            self.assertEqual(adapter.mountinfo_devices(), {'8:1'})
        self.assertEqual(root.glob.call_count, 2)
        sleep.assert_called_once_with(.05)

    def test_two_transient_errors_then_success(self):
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
        patch, root = self.scan([[stale], [stale], []])
        with patch, mock.patch.object(adapter.time, 'sleep') as sleep:
            self.assertEqual(adapter.mountinfo_devices(), set())
        self.assertEqual(root.glob.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_persistent_einval_rejected_after_three_scans(self):
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'not readable')
        patch, root = self.scan([[stale]] * 3)
        with patch, mock.patch.object(adapter.time, 'sleep') as sleep:
            with self.assertRaises(OSError) as caught:
                adapter.mountinfo_devices()
        self.assertEqual(caught.exception.errno, errno.EINVAL)
        self.assertEqual(root.glob.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    def test_other_errors_fail_without_retry(self):
        for code in (errno.EACCES, errno.EPERM, errno.EIO, errno.ENOMEM):
            with self.subTest(errno=code):
                unreadable = mock.Mock()
                unreadable.read_text.side_effect = OSError(code, 'cannot scan')
                patch, root = self.scan([[unreadable]])
                with patch, mock.patch.object(adapter.time, 'sleep') as sleep:
                    with self.assertRaises(OSError) as caught:
                        adapter.mountinfo_devices()
                self.assertEqual(caught.exception.errno, code)
                self.assertEqual(root.glob.call_count, 1)
                sleep.assert_not_called()

    def test_disappeared_processes_skipped(self):
        for code in (errno.ENOENT, errno.ESRCH):
            with self.subTest(errno=code):
                stale = mock.Mock()
                stale.read_text.side_effect = OSError(code, 'gone')
                patch, _ = self.scan([[stale]])
                with patch:
                    self.assertEqual(adapter.mountinfo_devices(), set())

    def test_mount_records_retained_across_retry(self):
        mounted = mock.Mock()
        mounted.read_text.return_value = self.mount('8:1')
        stale = mock.Mock()
        stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
        patch, _ = self.scan([[mounted, stale], []])
        with patch, mock.patch.object(adapter.time, 'sleep'):
            self.assertEqual(adapter.mountinfo_devices(), {'8:1'})

    def test_block_source_resolves_anonymous_mount_id(self):
        mounted = mock.Mock()
        mounted.read_text.return_value = self.mount('0:41', '/dev/test', 'btrfs')
        root = mock.Mock()
        root.glob.return_value = [mounted]
        device = mock.Mock()
        device.stat.return_value = mock.Mock(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 17))
        with mock.patch.object(adapter, 'Path', side_effect=lambda p: root if p == '/proc' else device):
            self.assertEqual(adapter.mountinfo_devices(), {'0:41', '8:17'})

    def test_busy_device_found_during_retry_still_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / 'dev').write_text('8:1')
            (path / 'holders').mkdir()
            mounted = mock.Mock()
            mounted.read_text.return_value = self.mount('8:1')
            stale = mock.Mock()
            stale.read_text.side_effect = OSError(errno.EINVAL, 'task exited')
            patch, _ = self.scan([[stale], [mounted]])
            with patch, mock.patch.object(adapter.time, 'sleep'):
                busy = adapter.mountinfo_devices()
            with mock.patch.object(adapter, 'mounted_devices', return_value=busy):
                with self.assertRaisesRegex(RuntimeError, 'mounted'):
                    adapter.check_unused(path)


class ExitedMountinfoTests(unittest.TestCase):
    def path(self, state):
        path = mock.MagicMock()
        status = mock.Mock()
        status.read_text.return_value = 'Name:\ttest\nState:\t' + state + ' (test)\n'
        path.parent.__truediv__.return_value = status
        return path, status

    def test_missing_process_confirmed(self):
        path, status = self.path('S')
        status.read_text.side_effect = FileNotFoundError(errno.ENOENT, 'gone')
        self.assertTrue(adapter.exited_mountinfo(path))

    def test_live_process_not_confirmed(self):
        path, _ = self.path('S')
        with mock.patch.object(adapter.os, 'readlink') as link:
            self.assertFalse(adapter.exited_mountinfo(path))
        link.assert_not_called()

    def test_missing_state_not_confirmed(self):
        path, status = self.path('S')
        for value in ('Name:\ttest\n', 'State:\n'):
            with self.subTest(status=value):
                status.read_text.return_value = value
                self.assertFalse(adapter.exited_mountinfo(path))

    def test_zombie_without_namespace_confirmed(self):
        for state in ('Z', 'X'):
            with self.subTest(state=state):
                path, _ = self.path(state)
                with mock.patch.object(adapter.os, 'readlink', side_effect=FileNotFoundError(errno.ENOENT, 'gone')):
                    self.assertTrue(adapter.exited_mountinfo(path))

    def test_zombie_with_namespace_not_confirmed(self):
        path, _ = self.path('Z')
        with mock.patch.object(adapter.os, 'readlink', return_value='mnt:[123]'):
            self.assertFalse(adapter.exited_mountinfo(path))

    def test_permission_error_not_swallowed(self):
        path, _ = self.path('Z')
        with mock.patch.object(adapter.os, 'readlink', side_effect=PermissionError(errno.EACCES, 'denied')):
            with self.assertRaises(PermissionError):
                adapter.exited_mountinfo(path)


if __name__ == '__main__':
    unittest.main()
