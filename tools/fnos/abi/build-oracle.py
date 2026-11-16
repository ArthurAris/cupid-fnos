#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build the read-only FNOS boot-oracle initramfs from a gzipped ARM64 Image.

The script never executes the produced init or QEMU. It verifies pinned input
and decompressed-image hashes, cross-compiles oracle-init.c statically, writes
a deterministic newc cpio/gzip initramfs plus manifest.json, and prints exactly
one JSON object on stdout. A failed run returns non-zero and keeps its workdir.
"""
import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

KERNEL_SHA256 = "985ba91057b0025e62386fa380fb4a5fb9a70874ba55fc6907bf770c9a104b40"
IMAGE_SHA256 = "3e77a740a4f79712a7bd7ecb8c2748f90ef895a78212a41cb9e33836680f17fb"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
CHUNK = 1 << 20
CROSS_GCC = "aarch64-linux-gnu-gcc"
ORACLE_RELEASE = "6.18.18.c1147-trim"
ORACLE_TOKEN = "cupid.oracle=boot-v1"


def sha256_file(path):
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            chunk = stream.read(CHUNK)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def decompress_image(source, target):
    digest = hashlib.sha256()
    size = 0
    try:
        with gzip.open(source, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise RuntimeError(
                        "decompressed Image exceeds %d bytes" % MAX_IMAGE_BYTES
                    )
                digest.update(chunk)
                dst.write(chunk)
    except (OSError, EOFError) as exc:
        raise RuntimeError("gzip decompression failed: %s" % exc) from exc
    return digest.hexdigest(), size


def verify_image_magic(path):
    with open(path, "rb") as stream:
        stream.seek(0x38)
        magic = stream.read(4)
    if magic != b"ARMd":
        raise RuntimeError(
            "not an ARM64 Image: expected b'ARMd' at 0x38, got %r" % magic
        )


def verify_static_aarch64(path):
    with open(path, "rb") as stream:
        header = stream.read(64)
        if len(header) < 64 or header[:4] != b"\x7fELF":
            raise RuntimeError("init is not an ELF file")
        if header[4] != 2 or header[5] != 1:
            raise RuntimeError("init is not a 64-bit little-endian ELF")
        e_type = struct.unpack_from("<H", header, 16)[0]
        e_machine = struct.unpack_from("<H", header, 18)[0]
        if e_machine != 183:
            raise RuntimeError(
                "init ELF machine is %d, not AArch64 (183)" % e_machine
            )
        if e_type not in (2, 3):
            raise RuntimeError("unexpected init ELF type %d" % e_type)
        phoff = struct.unpack_from("<Q", header, 32)[0]
        phentsize = struct.unpack_from("<H", header, 54)[0]
        phnum = struct.unpack_from("<H", header, 56)[0]
        if phentsize < 56:
            raise RuntimeError("init has an invalid program header size")
        for index in range(phnum):
            stream.seek(phoff + index * phentsize)
            program_type = stream.read(4)
            if len(program_type) != 4:
                raise RuntimeError("init has a truncated program header")
            if struct.unpack("<I", program_type)[0] == 3:
                raise RuntimeError("init contains PT_INTERP; not statically linked")


def cpio_entry(ino, mode, name, data=b"", nlink=1, rdev_major=0, rdev_minor=0):
    encoded_name = name.encode("ascii") + b"\0"
    fields = (
        ino,
        mode,
        0,
        0,
        nlink,
        0,
        len(data),
        0,
        0,
        rdev_major,
        rdev_minor,
        len(encoded_name),
        0,
    )
    entry = b"070701" + b"".join(b"%08x" % value for value in fields)
    entry += encoded_name
    entry += b"\0" * (-len(entry) % 4)
    entry += data
    entry += b"\0" * (-len(data) % 4)
    return entry


def build_cpio(init_data):
    entries = []
    ino = 1

    def add(mode, name, data=b"", nlink=1, rdev=(0, 0)):
        nonlocal ino
        entries.append(
            cpio_entry(ino, mode, name, data, nlink, rdev[0], rdev[1])
        )
        ino += 1

    add(stat.S_IFREG | 0o755, "init", init_data)
    add(stat.S_IFDIR | 0o755, "proc", nlink=2)
    add(stat.S_IFDIR | 0o755, "sys", nlink=2)
    add(stat.S_IFDIR | 0o755, "dev", nlink=2)
    add(stat.S_IFCHR | 0o600, "dev/console", rdev=(5, 1))
    add(stat.S_IFDIR | 0o755, "tmp", nlink=2)
    entries.append(cpio_entry(0, 0, "TRAILER!!!", b"", 1))
    return b"".join(entries)


def gzip_bytes(data):
    buffer = io.BytesIO()
    with gzip.GzipFile(
        filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0
    ) as stream:
        stream.write(data)
    return buffer.getvalue()


def build(args):
    work_parent = Path(args.work_parent).expanduser()
    if not work_parent.is_dir():
        raise RuntimeError(
            "--work-parent is not an existing directory: %s" % work_parent
        )
    work_parent = work_parent.resolve()

    kernel = Path(args.kernel).expanduser()
    try:
        kernel_stat = os.lstat(kernel)
    except OSError as exc:
        raise RuntimeError("cannot stat --kernel: %s" % exc) from exc
    if not stat.S_ISREG(kernel_stat.st_mode):
        raise RuntimeError(
            "--kernel must be a regular file; devices, directories, "
            "and symlinks are rejected"
        )
    kernel = kernel.resolve()

    input_sha256, input_size = sha256_file(kernel)
    if input_sha256 != KERNEL_SHA256:
        raise RuntimeError(
            "kernel gzip sha256 mismatch: expected %s, got %s"
            % (KERNEL_SHA256, input_sha256)
        )

    source = Path(__file__).resolve().parent / "oracle-init.c"
    try:
        source_stat = os.lstat(source)
    except OSError as exc:
        raise RuntimeError(
            "oracle-init.c is missing next to this script: %s" % exc
        ) from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise RuntimeError("oracle-init.c is not a regular file: %s" % source)

    gcc = shutil.which(CROSS_GCC)
    if gcc is None:
        raise RuntimeError(
            "%s not found in PATH; a static AArch64 cross toolchain is "
            "required (this script installs nothing)" % CROSS_GCC
        )

    workdir = Path(tempfile.mkdtemp(prefix="fnos-oracle-", dir=str(work_parent)))

    try:
        image = workdir / "Image"
        image_sha256, image_size = decompress_image(kernel, image)
        if image_sha256 != IMAGE_SHA256:
            raise RuntimeError(
                "decompressed Image sha256 mismatch: expected %s, got %s"
                % (IMAGE_SHA256, image_sha256)
            )
        verify_image_magic(image)

        init = workdir / "init"
        completed = subprocess.run(
            [
                gcc,
                "-static",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                str(init),
                str(source),
            ],
            cwd=str(workdir),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "%s failed with exit status %d"
                % (CROSS_GCC, completed.returncode)
            )
        if not init.is_file():
            raise RuntimeError("cross-compiler did not create init")
        verify_static_aarch64(init)

        source_sha256, source_size = sha256_file(source)
        init_sha256, init_size = sha256_file(init)
        initramfs = workdir / "initramfs.cpio.gz"
        initramfs.write_bytes(gzip_bytes(build_cpio(init.read_bytes())))
        initramfs_sha256, initramfs_size = sha256_file(initramfs)

        qemu_command = [
            "timeout",
            "-k",
            "5",
            "60",
            "qemu-system-aarch64",
            "-machine",
            "virt",
            "-accel",
            "tcg",
            "-cpu",
            "max",
            "-smp",
            "2",
            "-m",
            "1024",
            "-nodefaults",
            "-monitor",
            "none",
            "-serial",
            "stdio",
            "-display",
            "none",
            "-nic",
            "none",
            "-no-reboot",
            "-kernel",
            str(image),
            "-initrd",
            str(initramfs),
            "-append",
            "console=ttyAMA0 rdinit=/init %s" % ORACLE_TOKEN,
        ]

        manifest = {
            "format": 1,
            "purpose": (
                "read-only no-disk boot baseline oracle; NOT an ACL/trimacl "
                "acceptance"
            ),
            "input": {
                "path": str(kernel),
                "sha256": input_sha256,
                "size": input_size,
            },
            "source": {
                "path": str(source),
                "sha256": source_sha256,
                "size": source_size,
            },
            "init": {
                "path": str(init),
                "sha256": init_sha256,
                "size": init_size,
                "elf": "AArch64 64-bit little-endian",
                "static_pt_interp_absent": True,
                "mode": "0755",
            },
            "image": {
                "path": str(image),
                "sha256": image_sha256,
                "size": image_size,
                "magic": "ARMd@0x38",
            },
            "initramfs": {
                "path": str(initramfs),
                "sha256": initramfs_sha256,
                "size": initramfs_size,
                "format": "newc cpio inside gzip with mtime=0",
                "contents": [
                    "/init mode 0755",
                    "/proc empty directory 0755",
                    "/sys empty directory 0755",
                    "/dev empty directory 0755",
                    "/dev/console character 5:1 mode 0600 (cpio only)",
                    "/tmp empty directory 0755",
                    "TRAILER!!!",
                ],
            },
            "expected": {
                "uname_release": ORACLE_RELEASE,
                "independent_cmdline_token": ORACLE_TOKEN,
                "success_token": "FNOS_ORACLE_BOOT_OK",
                "failure_token": "FNOS_ORACLE_BOOT_FAIL",
                "acl_acceptance": False,
                "block_device_io": False,
                "network_initialization": False,
                "btrfs_mount": False,
            },
            "future_commands": {
                "run_isolated_qemu": qemu_command,
                "timeout": (
                    "external fixed 60s timeout; QEMU has no wall-clock "
                    "timeout of its own"
                ),
                "isolation": (
                    "QEMU virt only; no host directory sharing, disks, USB "
                    "passthrough, or networking"
                ),
            },
        }
        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("%s [outputs kept in %s]" % (exc, workdir)) from exc

    return {
        "workdir": str(workdir),
        "kernel_image": str(image),
        "initramfs": str(initramfs),
        "manifest": str(manifest_path),
        "init": str(init),
        "source": str(source),
        "image_sha256": image_sha256,
        "initramfs_sha256": initramfs_sha256,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Build the isolated no-disk FNOS boot-oracle initramfs; "
            "never runs init or QEMU"
        )
    )
    parser.add_argument("--kernel", required=True, help="gzipped ARM64 Image")
    parser.add_argument(
        "--work-parent",
        required=True,
        help="existing directory in which a new workdir is created",
    )
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print("build-oracle.py: error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())