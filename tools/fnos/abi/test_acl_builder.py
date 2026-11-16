#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Unit tests for the pure helpers in build-acl-oracle.py.

These tests never build an initramfs, run mkfs.btrfs, start QEMU, or touch a
real block device. Every fixture is synthesized in memory or a temporary file.
"""
import gzip
import hashlib
import importlib.util
import io
import os
import shlex
import stat
import subprocess
import struct
import tempfile
from pathlib import Path
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parent / "build-acl-oracle.py"
spec = importlib.util.spec_from_file_location("fnos_build_acl_oracle", SOURCE)
acl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acl)
BO = acl.BUILD_ORACLE
TARGET = acl.MODULE_NAME


def cpio_entry(name, data=b"", mode=stat.S_IFREG | 0o644, nlink=1):
    # build-oracle.cpio_entry encodes its name argument as ASCII str.
    if isinstance(name, bytes):
        name = name.decode("ascii")
    return BO.cpio_entry(1, mode, name, data, nlink)


def archive(*entries):
    return b"".join(entries) + BO.cpio_entry(0, 0, "TRAILER!!!", b"", 1)


def elf_header(machine=183, interp=False):
    header = bytearray(64)
    header[0:4] = b"\x7fELF"
    header[4] = 2
    header[5] = 1
    header[6] = 1
    struct.pack_into("<H", header, 16, 2)
    struct.pack_into("<H", header, 18, machine)
    struct.pack_into("<Q", header, 32, 64)
    struct.pack_into("<H", header, 54, 56)
    struct.pack_into("<H", header, 56, 1 if interp else 0)
    if interp:
        program = bytearray(56)
        struct.pack_into("<I", program, 0, 3)
        header += program
    return bytes(header)


class NewcReadTests(unittest.TestCase):
    def test_reads_unique_regular_member(self):
        data = archive(cpio_entry(b"plain/member", b"kernel-module-bytes"))
        self.assertEqual(
            acl.read_newc_member(io.BytesIO(data), b"plain/member"),
            b"kernel-module-bytes",
        )

    def test_parses_original_long_member_path(self):
        data = archive(cpio_entry(TARGET, b"long-path-bytes"))
        self.assertEqual(
            acl.read_newc_member(io.BytesIO(data), TARGET),
            b"long-path-bytes",
        )

    def test_rejects_truncated_header(self):
        with self.assertRaises(RuntimeError):
            acl.read_newc_member(io.BytesIO(b"070701" + b"0" * 20), TARGET)

    def test_rejects_truncated_member_data(self):
        data = cpio_entry(TARGET, b"0123456789")
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            acl.read_newc_member(io.BytesIO(data[:-5]), TARGET)

    def test_rejects_duplicate_target(self):
        data = archive(cpio_entry(TARGET, b"one"), cpio_entry(TARGET, b"two"))
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            acl.read_newc_member(io.BytesIO(data), TARGET)

    def test_rejects_hardlink_target(self):
        data = archive(cpio_entry(TARGET, b"x", nlink=2))
        with self.assertRaisesRegex(RuntimeError, "hardlink"):
            acl.read_newc_member(io.BytesIO(data), TARGET)

    def test_rejects_non_regular_target(self):
        data = archive(cpio_entry(TARGET, b"", mode=stat.S_IFDIR | 0o755))
        with self.assertRaisesRegex(RuntimeError, "REGULAR"):
            acl.read_newc_member(io.BytesIO(data), TARGET)

    def test_rejects_missing_target(self):
        data = archive(cpio_entry(b"other", b"x"))
        with self.assertRaisesRegex(RuntimeError, "not found"):
            acl.read_newc_member(io.BytesIO(data), TARGET)

    def test_rejects_oversized_target(self):
        data = archive(cpio_entry(TARGET, b"x" * 128))
        with self.assertRaisesRegex(RuntimeError, "not below"):
            acl.read_newc_member(
                io.BytesIO(data), TARGET, max_member_bytes=128
            )

    def test_rejects_entry_count_limit(self):
        data = archive(cpio_entry(b"one", b""), cpio_entry(b"two", b""))
        with self.assertRaisesRegex(RuntimeError, "more than"):
            acl.read_newc_member(io.BytesIO(data), TARGET, max_entries=2)


class CpioBuildTests(unittest.TestCase):
    def test_roundtrip_init_and_module(self):
        built = acl.build_acl_cpio(b"init-bytes", b"module-bytes")
        self.assertEqual(
            acl.read_newc_member(io.BytesIO(built), b"init"), b"init-bytes"
        )
        # build_acl_cpio deliberately writes the short /modules path.
        self.assertEqual(
            acl.read_newc_member(io.BytesIO(built), b"modules/virtio_blk.ko"),
            b"module-bytes",
        )
        for name in (b"proc", b"sys", b"dev", b"tmp", b"vol1", b"modules",
                     b"dev/console"):
            self.assertIn(name, built)

    def test_gzip_roundtrip(self):
        payload = b"newc cpio payload" * 8
        self.assertEqual(gzip.decompress(BO.gzip_bytes(payload)), payload)


class PlatformTests(unittest.TestCase):
    def test_non_linux_file_creation_refused_before_open(self):
        with mock.patch.object(acl.sys, "platform", "win32"):
            with mock.patch.object(acl.os, "open") as opened:
                with self.assertRaisesRegex(RuntimeError, "requires Linux or WSL"):
                    acl.create_data_image(Path("unused"))
                opened.assert_not_called()

    def test_non_linux_build_refused_before_inputs(self):
        with mock.patch.object(acl.sys, "platform", "win32"):
            with self.assertRaisesRegex(RuntimeError, "requires Linux or WSL"):
                acl.build(None)


@unittest.skipUnless(acl.sys.platform.startswith("linux"), "file creation requires Linux/WSL")
class CreateDataImageTests(unittest.TestCase):
    SIZE = 4096

    def test_creates_expected_size_and_sha256_fd(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory).resolve()
            with mock.patch.object(acl, "DATA_BYTES", self.SIZE):
                path, fd, inode, device = acl.create_data_image(workdir)
            try:
                info = os.fstat(fd)
                self.assertEqual(path, workdir / "data.img")
                self.assertTrue(stat.S_ISREG(info.st_mode))
                self.assertEqual(info.st_size, self.SIZE)
                self.assertEqual((info.st_ino, info.st_dev), (inode, device))
                os.write(fd, b"prefix")
                digest, size = acl.sha256_fd(fd)
                expected = hashlib.sha256(
                    b"prefix" + b"\0" * (self.SIZE - 6)
                ).hexdigest()
                self.assertEqual(size, self.SIZE)
                self.assertEqual(digest, expected)
            finally:
                os.close(fd)

    def test_rejects_duplicate_path(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory).resolve()
            (workdir / "data.img").write_bytes(b"existing")
            with mock.patch.object(acl, "DATA_BYTES", self.SIZE):
                with self.assertRaises(RuntimeError):
                    acl.create_data_image(workdir)

    def test_does_not_follow_preexisting_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory).resolve()
            target = workdir / "target"
            target.write_bytes(b"untouched")
            try:
                (workdir / "data.img").symlink_to(target)
            except OSError as exc:
                self.skipTest("symlinks unavailable: %s" % exc)
            with mock.patch.object(acl, "DATA_BYTES", self.SIZE):
                with self.assertRaises(RuntimeError):
                    acl.create_data_image(workdir)
            self.assertEqual(target.read_bytes(), b"untouched")
            self.assertTrue((workdir / "data.img").is_symlink())

    def test_does_not_follow_dangling_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            workdir = Path(directory).resolve()
            target = workdir / "missing-target"
            (workdir / "data.img").symlink_to(target)
            with mock.patch.object(acl, "DATA_BYTES", self.SIZE):
                with self.assertRaises(RuntimeError):
                    acl.create_data_image(workdir)
            self.assertFalse(target.exists())
            self.assertTrue((workdir / "data.img").is_symlink())


class ElfCheckTests(unittest.TestCase):
    def check(self, data, expected_error=None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "init"
            path.write_bytes(data)
            if expected_error is None:
                BO.verify_static_aarch64(path)
            else:
                with self.assertRaisesRegex(RuntimeError, expected_error):
                    BO.verify_static_aarch64(path)

    def test_accepts_static_aarch64(self):
        self.check(elf_header())

    def test_rejects_wrong_machine(self):
        self.check(elf_header(machine=62), "AArch64")

    def test_rejects_pt_interp(self):
        self.check(elf_header(interp=True), "PT_INTERP")


ACL_INIT_SOURCE = Path(__file__).resolve().parent / "acl-init.c"


class AclV2SourceStaticTests(unittest.TestCase):
    """Static source assertions only; no build, no QEMU, no device."""

    def test_acl_v2_token_and_magic_dfd_constants(self):
        text = ACL_INIT_SOURCE.read_text(encoding="utf-8")
        self.assertIn('#define ORACLE_TOKEN "cupid.oracle=acl-v2"', text)
        self.assertIn("case-set=acl-v2", text)
        self.assertNotIn("cupid.oracle=acl-v1", text)
        self.assertIn("#define TRIMACL_DFD (-21582)", text)
        self.assertIn("#define TRIMACL_QUERY_DFD (-21576)", text)

    def test_uapi_tag_group_and_inherit_constants(self):
        text = ACL_INIT_SOURCE.read_text(encoding="utf-8")
        self.assertIn("#define TRIM_ACL_TAG_GROUP 0x0008", text)
        self.assertIn("#define TRIM_ACL_INHERIT_SELF 0x0001", text)
        self.assertIn("#define TRIM_ACL_INHERIT_FILE 0x0002", text)
        self.assertIn("#define TRIM_ACL_INHERIT_DIR 0x0004", text)


class AclUapiSerializationTests(unittest.TestCase):
    """Pure length/field assertions for the packed 4/20-byte user layout."""

    def test_header_and_entry_lengths_match_measured_v1(self):
        # docs/fnos-permission-abi.md: measured 24/44 bytes for 1/2 entries.
        header = struct.pack("<HH", 0x0001, 0)
        entry = struct.pack("<HHIHHII", 0x0005, 0, 0x104, 0x0007, 0, 42001, 0)
        self.assertEqual(len(header), 4)
        self.assertEqual(len(entry), 20)
        self.assertEqual(len(header) + len(entry), 24)
        self.assertEqual(len(header) + 2 * len(entry), 44)

    def test_entry_field_offsets(self):
        # e_tag, e_unused1, e_perm, e_inherit, e_unused2, e_id, e_level.
        entry = struct.pack("<HHIHHII", 0x0005, 0, 0x104, 0x0007, 0, 42001, 3)
        self.assertEqual(struct.unpack_from("<H", entry, 0)[0], 0x0005)
        self.assertEqual(struct.unpack_from("<I", entry, 4)[0], 0x104)
        self.assertEqual(struct.unpack_from("<H", entry, 8)[0], 0x0007)
        self.assertEqual(struct.unpack_from("<I", entry, 12)[0], 42001)
        self.assertEqual(struct.unpack_from("<I", entry, 16)[0], 3)


@unittest.skipUnless(acl.sys.platform.startswith("linux"), "runner shell requires Linux/WSL")
class RunnerScriptTests(unittest.TestCase):
    def run_fixture(self, output, exit_code=0):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            fixture = work / "fixture.log"
            fixture.write_bytes(output)
            command = "(cat %s; exit %d)" % (shlex.quote(str(fixture)), exit_code)
            script = acl.build_runner_script(command, work / "result.log")
            return subprocess.run(["sh"], input=script, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE).returncode

    def test_lf_and_crlf_success(self):
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline):
                self.assertEqual(self.run_fixture(acl.SUCCESS_LINE.encode() + newline), 0)

    def test_nonzero_exit_rejected(self):
        self.assertNotEqual(self.run_fixture(acl.SUCCESS_LINE.encode() + b"\n", 124), 0)

    def test_partial_or_failed_run_rejected(self):
        for marker in (b"FNOS_ACL_ORACLE_FAIL", b"Kernel panic", b"CASE inheritance SETUP_FAIL note=failed",
                       b"CASE group SKIPPED note=failed"):
            with self.subTest(marker=marker):
                self.assertNotEqual(self.run_fixture(marker + b"\n" + acl.SUCCESS_LINE.encode() + b"\n"), 0)

    def test_incomplete_marker_rejected(self):
        for output in (b"", b"prefix " + acl.SUCCESS_LINE.encode(), acl.SUCCESS_LINE.encode() + b" suffix"):
            with self.subTest(output=output):
                self.assertNotEqual(self.run_fixture(output + b"\n"), 0)


if __name__ == "__main__":
    unittest.main()
