#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Host-only refusal tests for archive unpacking; no build helpers or devices."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('headers_unpack_subject', HERE / 'unpack-build-headers.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
ROOT = HERE.parents[1]
ARCHIVE = ROOT / 'build-inputs/linux-headers-6.18.18.c1147-trim-complete.tar.xz'
MANIFEST = ROOT / 'build-inputs/linux-headers-6.18.18.c1147-trim-complete.manifest.json'
PIN = '3a19038f84738af3505f51571cfeaa1090c1c947e2abd90df48d5e09023e4b08'


class UnpackTests(unittest.TestCase):
    def test_wrong_pin_refused_before_output(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / 'unused'
            with self.assertRaises(ValueError):
                m.unpack(ARCHIVE, MANIFEST, out, '0' * 64)
            self.assertFalse(out.exists())

    def test_existing_output_refused(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError):
                m.unpack(ARCHIVE, MANIFEST, Path(d), PIN)

    def test_path_traversal_manifest_refused(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = json.loads(MANIFEST.read_bytes())
            manifest['files'][0]['path'] = '../outside'
            changed = Path(d) / 'manifest.json'
            changed.write_text(json.dumps(manifest))
            out = Path(d) / 'output'
            with self.assertRaises(ValueError):
                m.unpack(ARCHIVE, changed, out, PIN)
            self.assertFalse(out.exists())

    def test_duplicate_manifest_refused(self):
        with tempfile.TemporaryDirectory() as d:
            manifest = json.loads(MANIFEST.read_bytes())
            manifest['files'].append(manifest['files'][0])
            changed = Path(d) / 'manifest.json'
            changed.write_text(json.dumps(manifest))
            out = Path(d) / 'output'
            with self.assertRaises(ValueError):
                m.unpack(ARCHIVE, changed, out, PIN)
            self.assertFalse(out.exists())

    def test_relative_output_refused(self):
        with self.assertRaises(ValueError):
            m.unpack(ARCHIVE, MANIFEST, Path('relative-output'), PIN)


if __name__ == '__main__':
    unittest.main()
