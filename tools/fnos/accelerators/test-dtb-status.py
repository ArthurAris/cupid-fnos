#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Test DT-only image scope using synthetic firmware-independent fixtures."""
import importlib.util
from pathlib import Path
import struct
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


status = load('status_check', HERE / 'dtb-status.py')
fixtures = load('dt_fixtures', HERE / 'test-firmware.py')
TARGET = '/soc@0/remoteproc@32300000'


def disabled_dtb():
    data = fixtures.fixture_dtb()
    # Add the status property at the end of the target's existing memory-region property.
    spans = status.property_spans(data)
    _header, length, value = spans[(TARGET, 'memory-region')]
    insert_at = (value + length + 3) & ~3
    off_strings = struct.unpack_from('>I', data, 12)[0]
    strings_size = struct.unpack_from('>I', data, 32)[0]
    extra = fixtures.cell(3, 9, strings_size) + b'disabled\0\0\0\0'
    result = bytearray(data[:insert_at] + extra + data[insert_at:] + b'status\0')
    struct.pack_into('>I', result, 4, len(result))
    struct.pack_into('>I', result, 12, off_strings + len(extra))
    struct.pack_into('>I', result, 32, strings_size + 7)
    struct.pack_into('>I', result, 36, struct.unpack_from('>I', data, 36)[0] + len(extra))
    status.fdt.parse_dtb(bytes(result))
    return bytes(result)


def boot_image(dtb):
    header = bytearray(2048)
    header[:8] = b'ANDROID!'
    kernel = b'prefix' + dtb + b'payload'
    struct.pack_into('<I', header, 8, len(kernel))
    struct.pack_into('<I', header, 36, 2048)
    struct.pack_into('<I', header, 40, 2)
    return bytes(header) + kernel + b'trailer'


class ScopeTests(unittest.TestCase):
    def test_status_scope(self):
        data = disabled_dtb()
        after, changed = status.enable_status(data, [TARGET])
        self.assertEqual(len(data), len(after))
        self.assertEqual(status.fdt.parse_dtb(after)[TARGET]['status'], b'okay\0')
        self.assertTrue(changed)

    def test_missing_target(self):
        with self.assertRaisesRegex(ValueError, 'explicitly disabled'):
            status.enable_status(disabled_dtb(), ['/other/' + TARGET.split('/')[-1]])

    def test_repeated_target(self):
        with self.assertRaisesRegex(ValueError, 'Unique'):
            status.enable_status(disabled_dtb(), [TARGET, TARGET])

    def test_already_enabled_refused(self):
        data, _changed = status.enable_status(disabled_dtb(), [TARGET])
        with self.assertRaisesRegex(ValueError, 'explicitly disabled'):
            status.enable_status(data, [TARGET])

    def test_whole_image_scope(self):
        dtb = disabled_dtb()
        source = boot_image(dtb)
        image, report = status.derive_image(source, dtb, [TARGET])
        base, size = report['dtb_offset'], report['dtb_size']
        self.assertEqual(source[:base], image[:base])
        self.assertEqual(source[base + size:], image[base + size:])
        self.assertEqual(len(source), len(image))
        self.assertFalse(report['system_readonly'])
        self.assertFalse(report['hardware_ready'])

    def test_wrong_image_header(self):
        with self.assertRaises(ValueError):
            status.derive_image(b'wrong', disabled_dtb(), [TARGET])

    def test_wrong_boot_version(self):
        dtb = disabled_dtb()
        source = bytearray(boot_image(dtb))
        struct.pack_into('<I', source, 40, 4)
        with self.assertRaisesRegex(ValueError, 'Unsupported'):
            status.derive_image(bytes(source), dtb, [TARGET])

    def test_duplicate_pinned_dtb(self):
        dtb = disabled_dtb()
        source = bytearray(boot_image(dtb))
        source.extend(dtb)
        struct.pack_into('<I', source, 8, len(source) - 2048)
        with self.assertRaisesRegex(ValueError, 'one exact'):
            status.derive_image(bytes(source), dtb, [TARGET])

    def test_missing_pinned_dtb(self):
        with self.assertRaisesRegex(ValueError, 'one exact'):
            status.derive_image(boot_image(disabled_dtb()), fixtures.fixture_dtb(), [TARGET])

    def test_fdt_corruption_refused(self):
        data = bytearray(disabled_dtb())
        struct.pack_into('>I', data, 56, 8)
        with self.assertRaises(ValueError):
            status.enable_status(bytes(data), [TARGET])


if __name__ == '__main__':
    unittest.main(verbosity=2)
