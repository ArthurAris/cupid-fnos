#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build a one-shot Btrfs trimacl observation initramfs and blank data image.

This builder never runs QEMU, mounts Btrfs, or touches a host block device.
It verifies pinned kernel/initrd hashes, streams the pinned virtio_blk.ko out
of the zstd-compressed newc initrd without writing the whole archive, builds a
static AArch64 PID1 from acl-init.c, creates a fresh 256 MiB regular file in a
new private workdir, and formats only that file with mkfs.btrfs.
"""
import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

KERNEL_SHA256 = "985ba91057b0025e62386fa380fb4a5fb9a70874ba55fc6907bf770c9a104b40"
IMAGE_SHA256 = "3e77a740a4f79712a7bd7ecb8c2748f90ef895a78212a41cb9e33836680f17fb"
STOCK_INITRD_SHA256 = "58f74e7939085094af42f373134f4216a8bf64ec5f71ace4a8fc60f9b18ef016"
MODULE_SHA256 = "a5f982ad312f73b47eac7f1d319e3aef5aad2c733b205a265e584b03590c8d13"
MODULE_NAME = (
    b"usr/lib/modules/6.18.18.c1147-trim/kernel/drivers/block/virtio_blk.ko"
)
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_INITRD_BYTES = 512 * 1024 * 1024
MAX_INITRD_ENTRIES = 100000
MAX_MODULE_BYTES = 1024 * 1024
MAX_NAME_BYTES = 65536
DATA_BYTES = 268435456
TEST_SERIAL = "FNOS_ORACLE_TEST"
CHUNK = 1 << 20
CROSS_GCC = "aarch64-linux-gnu-gcc"
ORACLE_TOKEN = "cupid.oracle=acl-v2"
SUCCESS_LINE = (
    "FNOS_ACL_ORACLE_OBSERVATIONS_COMPLETE NOT_COMPATIBILITY_ACCEPTANCE"
)
NEWC_MAGICS = (b"070701", b"070702")
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def load_build_oracle():
    path = Path(__file__).resolve().parent / "build-oracle.py"
    spec = importlib.util.spec_from_file_location("fnos_build_oracle", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load sibling build-oracle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILD_ORACLE = load_build_oracle()


def sha256_file(path):
    return BUILD_ORACLE.sha256_file(path)


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def sha256_fd(fd):
    digest = hashlib.sha256()
    size = 0
    os.lseek(fd, 0, os.SEEK_SET)
    while True:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            break
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def read_exact(stream, size, what):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise RuntimeError("truncated newc archive while reading %s" % what)
        data += chunk
    return bytes(data)


def skip_exact(stream, size):
    left = size
    while left:
        chunk = stream.read(min(left, CHUNK))
        if not chunk:
            raise RuntimeError("truncated newc archive while skipping member data")
        left -= len(chunk)


def read_newc_member(
    stream,
    target,
    max_decompressed=MAX_INITRD_BYTES,
    max_entries=MAX_INITRD_ENTRIES,
    max_member_bytes=MAX_MODULE_BYTES,
):
    """Return the unique regular non-hardlink member named target.

    The stream is consumed to EOF so a decompressor feeding it can finish
    cleanly. Any malformed header, truncation, oversized member, duplicate
    target, non-regular target, or hardlink target is rejected.
    """
    if isinstance(target, str):
        target = target.encode("ascii")
    consumed = 0
    entries = 0
    found = None
    while True:
        header = read_exact(stream, 110, "header")
        consumed += 110
        if consumed > max_decompressed:
            raise RuntimeError("decompressed initrd exceeds %d bytes" % max_decompressed)
        if header[:6] not in NEWC_MAGICS:
            raise RuntimeError("bad newc magic %r" % header[:6])
        try:
            fields = [int(header[6 + i * 8:14 + i * 8], 16) for i in range(13)]
        except ValueError as exc:
            raise RuntimeError("non-hex newc header field") from exc
        ino, mode, _uid, _gid, nlink, _mtime, filesize, _dmaj, _dmin, _rmaj, \
            _rmin, namesize, _check = fields
        del ino
        if namesize < 1 or namesize > MAX_NAME_BYTES:
            raise RuntimeError("newc name size out of bounds: %d" % namesize)
        if consumed + namesize > max_decompressed:
            raise RuntimeError("decompressed initrd exceeds %d bytes" % max_decompressed)
        raw_name = read_exact(stream, namesize, "member name")
        consumed += namesize
        if raw_name[-1] != 0 or b"\0" in raw_name[:-1] or len(raw_name) == 1:
            raise RuntimeError("malformed newc member name")
        name = raw_name[:-1]
        name_pad = -(110 + namesize) % 4
        read_exact(stream, name_pad, "name padding")
        consumed += name_pad
        entries += 1
        if entries > max_entries:
            raise RuntimeError("initrd has more than %d entries" % max_entries)
        if name == b"TRAILER!!!":
            if filesize != 0:
                raise RuntimeError("newc trailer has non-zero size")
            while True:
                chunk = stream.read(CHUNK)
                if not chunk:
                    break
                consumed += len(chunk)
                if consumed > max_decompressed:
                    raise RuntimeError(
                        "decompressed initrd exceeds %d bytes" % max_decompressed
                    )
            break
        if consumed + filesize > max_decompressed:
            raise RuntimeError("decompressed initrd exceeds %d bytes" % max_decompressed)
        data_pad = -filesize % 4
        if consumed + filesize + data_pad > max_decompressed:
            raise RuntimeError("decompressed initrd exceeds %d bytes" % max_decompressed)
        if name == target:
            if found is not None:
                raise RuntimeError("duplicate initrd member: %s" % target.decode("ascii"))
            if stat.S_IFMT(mode) != stat.S_IFREG:
                raise RuntimeError("target member is not REGULAR: %s" % target.decode())
            if nlink != 1:
                raise RuntimeError("target member has nlink=%d (hardlink)" % nlink)
            if filesize >= max_member_bytes:
                raise RuntimeError(
                    "target member size %d is not below %d bytes"
                    % (filesize, max_member_bytes)
                )
            found = read_exact(stream, filesize, "target member data")
            consumed += filesize
        else:
            skip_exact(stream, filesize)
            consumed += filesize
        read_exact(stream, data_pad, "member data padding")
        consumed += data_pad
    if found is None:
        raise RuntimeError("initrd member not found: %s" % target.decode("ascii"))
    return found


def extract_module_bytes(initrd_path):
    with open(initrd_path, "rb") as stream:
        magic = stream.read(4)
    if magic != ZSTD_MAGIC:
        raise RuntimeError("stock initrd is not zstd-compressed")
    zstd = shutil.which("zstd")
    if zstd is None:
        raise RuntimeError("zstd not found in PATH; this script installs nothing")
    proc = subprocess.Popen(
        [zstd, "-d", "-c", "--", str(initrd_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        data = read_newc_member(proc.stdout, MODULE_NAME)
    finally:
        proc.stdout.close()
        zstd_err = proc.stderr.read()
        returncode = proc.wait()
        proc.stderr.close()
    if returncode != 0:
        raise RuntimeError(
            "zstd failed with status %d: %s"
            % (returncode, zstd_err.decode("utf-8", "replace")[:400])
        )
    digest = sha256_bytes(data)
    if digest != MODULE_SHA256:
        raise RuntimeError(
            "virtio_blk.ko sha256 mismatch: expected %s, got %s"
            % (MODULE_SHA256, digest)
        )
    return data


def build_acl_cpio(init_data, module_data):
    if len(module_data) >= MAX_MODULE_BYTES:
        raise RuntimeError("module exceeds initramfs size limit")
    entries = []
    ino = 1

    def add(mode, name, data=b"", nlink=1, rdev=(0, 0)):
        nonlocal ino
        entries.append(
            BUILD_ORACLE.cpio_entry(
                ino, mode, name, data, nlink, rdev[0], rdev[1]
            )
        )
        ino += 1

    add(stat.S_IFREG | 0o755, "init", init_data)
    add(stat.S_IFDIR | 0o755, "proc", nlink=2)
    add(stat.S_IFDIR | 0o755, "sys", nlink=2)
    add(stat.S_IFDIR | 0o755, "dev", nlink=2)
    add(stat.S_IFCHR | 0o600, "dev/console", rdev=(5, 1))
    add(stat.S_IFDIR | 0o755, "tmp", nlink=2)
    add(stat.S_IFDIR | 0o755, "vol1", nlink=2)
    add(stat.S_IFDIR | 0o755, "modules", nlink=2)
    add(stat.S_IFREG | 0o644, "modules/virtio_blk.ko", module_data)
    entries.append(BUILD_ORACLE.cpio_entry(0, 0, "TRAILER!!!", b"", 1))
    return b"".join(entries)


def require_regular(path, label):
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RuntimeError("cannot stat %s: %s" % (label, exc)) from exc
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(
            "%s must be a regular file; symlinks, directories and devices "
            "are rejected" % label
        )
    return Path(path).resolve()


def build_runner_script(command, log_path):
    return "".join([
        "#!/bin/sh\n",
        "set -u\n",
        "LOG=" + shlex.quote(str(log_path)) + "\n",
        command + ' >"$LOG" 2>&1\n',
        "qemu_rc=$?\n",
        "token_complete=0\n",
        "token_fail=0\n",
        "tr -d '\\r' < \"$LOG\" | grep -qxF '" + SUCCESS_LINE + "' "
        "&& token_complete=1\n",
        "grep -qE 'FNOS_ACL_ORACLE_FAIL|Kernel panic| SETUP_FAIL | SKIPPED ' "
        '"$LOG" && token_fail=1\n',
        'cat "$LOG"\n',
        'printf "ACL_ORACLE_RUNNER qemu_rc=%s token_complete=%s token_fail=%s\\n" '
        '"$qemu_rc" "$token_complete" "$token_fail"\n',
        'if [ "$qemu_rc" -eq 0 ] && [ "$token_complete" -eq 1 ] '
        '&& [ "$token_fail" -eq 0 ]; then\n',
        "    exit 0\n",
        "fi\n",
        'printf "ACL_ORACLE_RUNNER_FAIL incomplete-or-failed-run\\n" >&2\n',
        "exit 1\n",
    ])


def require_linux():
    if not sys.platform.startswith("linux"):
        raise RuntimeError("ACL oracle image creation requires Linux or WSL")


def create_data_image(workdir):
    require_linux()
    data_path = workdir / "data.img"
    nofollow = os.O_NOFOLLOW
    try:
        fd = os.open(
            data_path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | nofollow,
            0o600,
        )
    except OSError as exc:
        raise RuntimeError("cannot create data.img exclusively: %s" % exc) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise RuntimeError("new data.img is not a regular file")
        os.ftruncate(fd, DATA_BYTES)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size != DATA_BYTES:
            raise RuntimeError("data.img is not the expected 256 MiB regular file")
        if os.path.realpath(data_path) != str(data_path):
            raise RuntimeError("data.img resolved outside the private workdir")
        if data_path.parent != workdir:
            raise RuntimeError("data.img path is not directly in the workdir")
    except (OSError, RuntimeError):
        os.close(fd)
        raise
    return data_path, fd, info.st_ino, info.st_dev


def build(args):
    require_linux()
    work_parent = Path(args.work_parent).expanduser()
    if not work_parent.is_dir():
        raise RuntimeError("--work-parent is not an existing directory: %s" % work_parent)
    work_parent = work_parent.resolve()

    kernel = require_regular(Path(args.kernel).expanduser(), "--kernel")
    stock = require_regular(Path(args.stock_initrd).expanduser(), "--stock-initrd")
    source = require_regular(
        Path(__file__).resolve().parent / "acl-init.c", "acl-init.c"
    )

    gcc = shutil.which(CROSS_GCC)
    if gcc is None:
        raise RuntimeError(
            "%s not found in PATH; a static AArch64 cross toolchain is required"
            % CROSS_GCC
        )

    workdir = Path(tempfile.mkdtemp(prefix="fnos-acl-oracle-", dir=str(work_parent)))
    try:
        kernel_sha, kernel_size = sha256_file(kernel)
        if kernel_sha != KERNEL_SHA256:
            raise RuntimeError(
                "kernel gzip sha256 mismatch: expected %s, got %s"
                % (KERNEL_SHA256, kernel_sha)
            )
        image_path = workdir / "Image"
        image_sha, image_size = BUILD_ORACLE.decompress_image(kernel, image_path)
        if image_sha != IMAGE_SHA256:
            raise RuntimeError(
                "decompressed Image sha256 mismatch: expected %s, got %s"
                % (IMAGE_SHA256, image_sha)
            )
        BUILD_ORACLE.verify_image_magic(image_path)

        stock_sha, stock_size = sha256_file(stock)
        if stock_sha != STOCK_INITRD_SHA256:
            raise RuntimeError(
                "stock initrd sha256 mismatch: expected %s, got %s"
                % (STOCK_INITRD_SHA256, stock_sha)
            )
        module_data = extract_module_bytes(stock)
        module_dir = workdir / "module"
        module_dir.mkdir(mode=0o700)
        module_path = module_dir / "virtio_blk.ko"
        fd = os.open(
            module_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(module_data)
        except OSError:
            raise
        module_sha = sha256_bytes(module_data)

        init_path = workdir / "init"
        completed = subprocess.run(
            [
                gcc,
                "-static",
                "-O2",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                str(init_path),
                str(source),
            ],
            cwd=str(workdir),
            stdout=sys.stderr,
            stderr=sys.stderr,
        )
        if completed.returncode != 0:
            raise RuntimeError("%s failed with exit status %d" % (CROSS_GCC, completed.returncode))
        if not init_path.is_file():
            raise RuntimeError("cross-compiler did not create init")
        BUILD_ORACLE.verify_static_aarch64(init_path)
        init_data = init_path.read_bytes()
        init_sha = sha256_bytes(init_data)

        initramfs_path = workdir / "initramfs.cpio.gz"
        initramfs_bytes = BUILD_ORACLE.gzip_bytes(
            build_acl_cpio(init_data, module_data)
        )
        initramfs_path.write_bytes(initramfs_bytes)
        initramfs_sha = sha256_bytes(initramfs_bytes)

        data_path, data_fd, data_ino, data_dev = create_data_image(workdir)
        try:
            mkfs = shutil.which("mkfs.btrfs")
            if mkfs is None:
                raise RuntimeError(
                    "mkfs.btrfs not found in PATH; install nothing here"
                )
            completed = subprocess.run(
                [
                    mkfs,
                    "-q",
                    "-L",
                    TEST_SERIAL,
                    "/proc/self/fd/%d" % data_fd,
                ],
                cwd=str(workdir),
                pass_fds=(data_fd,),
                stdout=sys.stderr,
                stderr=sys.stderr,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    "mkfs.btrfs failed with exit status %d"
                    % completed.returncode
                )
            data_info = os.fstat(data_fd)
            if (
                not stat.S_ISREG(data_info.st_mode)
                or data_info.st_size != DATA_BYTES
                or data_info.st_ino != data_ino
                or data_info.st_dev != data_dev
            ):
                raise RuntimeError(
                    "formatted data.img changed type, size, inode or device"
                )
            path_info = os.lstat(data_path)
            if (
                not stat.S_ISREG(path_info.st_mode)
                or path_info.st_size != DATA_BYTES
                or path_info.st_ino != data_ino
                or path_info.st_dev != data_dev
            ):
                raise RuntimeError(
                    "data.img path no longer names the formatted inode"
                )
            data_sha, data_size = sha256_fd(data_fd)
            if data_size != DATA_BYTES:
                raise RuntimeError("formatted data.img has an unexpected size")
            path_info = os.lstat(data_path)
            if (
                not stat.S_ISREG(path_info.st_mode)
                or path_info.st_ino != data_ino
                or path_info.st_dev != data_dev
            ):
                raise RuntimeError("data.img path changed while hashing")
        finally:
            os.close(data_fd)

        source_sha, source_size = sha256_file(source)
        builder_sha, builder_size = sha256_file(Path(__file__).resolve())
        oracle_builder = Path(__file__).resolve().parent / "build-oracle.py"
        oracle_sha, oracle_size = sha256_file(oracle_builder)

        qemu_argv = [
            "timeout",
            "-k",
            "5",
            "60",
            "qemu-system-aarch64",
            "-machine",
            "virt-10.2,gic-version=3",
            "-accel",
            "tcg,thread=single",
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
            str(image_path),
            "-initrd",
            str(initramfs_path),
            "-append",
            "console=ttyAMA0 rdinit=/init %s" % ORACLE_TOKEN,
            "-drive",
            "file=%s,if=none,id=vd0,format=raw" % data_path,
            "-device",
            "virtio-blk-pci,drive=vd0,serial=%s" % TEST_SERIAL,
        ]
        qemu_command = " ".join(shlex.quote(part) for part in qemu_argv)
        log_path = workdir / "qemu-boot.log"
        runner_script = build_runner_script(qemu_command, log_path)
        runner_path = workdir / "run-acl-oracle.sh"
        runner_fd = os.open(
            runner_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o755,
        )
        with os.fdopen(runner_fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(runner_script)
        os.chmod(runner_path, 0o755)
        runner_sha, runner_size = sha256_file(runner_path)

        manifest = {
            "format": 1,
            "purpose": (
                "one-shot Btrfs trimacl behavior observation; "
                "NOT a compatibility or acceptance result"
            ),
            "test_serial": TEST_SERIAL,
            "data_bytes": DATA_BYTES,
            "inputs": {
                "kernel_gzip": {
                    "path": str(kernel),
                    "sha256": kernel_sha,
                    "size": kernel_size,
                },
                "stock_initrd": {
                    "path": str(stock),
                    "sha256": stock_sha,
                    "size": stock_size,
                },
            },
            "sources": {
                "acl_init_c": {
                    "path": str(source),
                    "sha256": source_sha,
                    "size": source_size,
                },
                "build_acl_oracle_py": {
                    "path": str(Path(__file__).resolve()),
                    "sha256": builder_sha,
                    "size": builder_size,
                },
                "build_oracle_py": {
                    "path": str(oracle_builder),
                    "sha256": oracle_sha,
                    "size": oracle_size,
                },
            },
            "image": {
                "path": str(image_path),
                "sha256": image_sha,
                "size": image_size,
                "magic": "ARMd@0x38",
            },
            "module": {
                "member": MODULE_NAME.decode("ascii"),
                "path": str(module_path),
                "sha256": module_sha,
                "size": len(module_data),
                "regular": True,
                "nlink": 1,
            },
            "init": {
                "path": str(init_path),
                "sha256": init_sha,
                "size": len(init_data),
                "elf": "AArch64 64-bit little-endian static",
            },
            "initramfs": {
                "path": str(initramfs_path),
                "sha256": initramfs_sha,
                "size": len(initramfs_bytes),
                "format": "newc cpio inside gzip with mtime=0",
                "contents": [
                    "/init mode 0755",
                    "/proc /sys /dev /tmp /vol1 directories 0755",
                    "/dev/console character 5:1 mode 0600",
                    "/modules directory 0755 (parent of the module)",
                    "/modules/virtio_blk.ko regular 0644",
                    "TRAILER!!!",
                ],
            },
            "data_image": {
                "path": str(data_path),
                "sha256": data_sha,
                "size": data_size,
                "filesystem": "btrfs",
                "label": TEST_SERIAL,
                "created_by": (
                    "O_CREAT|O_EXCL|O_NOFOLLOW regular file, ftruncate, "
                    "mkfs.btrfs -q -L via /proc/self/fd/<fd>, pre/post "
                    "fstat and lstat inode check"
                ),
                "inode": data_ino,
                "device": data_dev,
                "path_inode_bound": True,
            },
            "expected": {
                "release": "6.18.18.c1147-trim",
                "machine": "aarch64",
                "cmdline_token": ORACLE_TOKEN,
                "case_set": "acl-v2",
                "success_token": SUCCESS_LINE,
                "failure_token": "FNOS_ACL_ORACLE_FAIL",
                "required_stdout_token": SUCCESS_LINE,
                "required_stdout_line": SUCCESS_LINE,
                "forbidden_stdout_token": "FNOS_ACL_ORACLE_FAIL",
                "compatibility_acceptance": False,
            },
            "runner": {
                "path": str(runner_path),
                "sha256": runner_sha,
                "size": runner_size,
                "log": str(log_path),
                "qemu_command": qemu_command,
                "stdout_gate": (
                    "exit 0 only when qemu_rc==0, the complete line "
                    "FNOS_ACL_ORACLE_OBSERVATIONS_COMPLETE "
                    "NOT_COMPATIBILITY_ACCEPTANCE is present, and neither "
                    "FNOS_ACL_ORACLE_FAIL nor Kernel panic is present"
                ),
            },
        }
        manifest_path = workdir / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError("%s [outputs kept in %s]" % (exc, workdir)) from exc

    return {
        "workdir": str(workdir),
        "kernel_image": str(image_path),
        "initramfs": str(initramfs_path),
        "data_image": str(data_path),
        "manifest": str(manifest_path),
        "runner": str(runner_path),
        "init": str(init_path),
        "module": str(module_path),
        "initramfs_sha256": initramfs_sha,
        "data_image_sha256": data_sha,
        "runner_sha256": runner_sha,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Build a one-shot FNOS Btrfs trimacl observation initramfs and "
            "a freshly formatted regular-file data image; never runs QEMU"
        )
    )
    parser.add_argument("--kernel", required=True, help="pinned gzipped ARM64 Image")
    parser.add_argument("--stock-initrd", required=True, help="pinned zstd newc initrd")
    parser.add_argument(
        "--work-parent",
        required=True,
        help="existing directory in which a new private workdir is created",
    )
    args = parser.parse_args(argv)
    try:
        result = build(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print("build-acl-oracle.py: error: %s" % exc, file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())