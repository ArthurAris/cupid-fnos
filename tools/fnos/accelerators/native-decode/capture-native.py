#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Future on-device READ-ONLY metadata capture; never starts a media job/FFmpeg.

Default mode only reports capabilities. --collect-read-only explicitly captures
bounded /proc and executable metadata into a NEW directory beneath --private-root.
No network, port, ioctl, device open, driver load, service changes, signals, shell
or subprocess. A capture is NOT a session evidence envelope or trusted receipt.
--write-fixture generates a fixed synthetic template/FFmpeg argv, never executes.
Actual media-task request/API creation is unknown from the 20261111 saved logs.
Use python3 -I -B with this script; never run it from an extracted archive.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time

FFMPEG = "/usr/trim/lib/mediasrv/ffmpeg"
MEDIASRV = "/usr/trim/bin/mediasrv"


def ffmpeg_command(source, frames, duration=600, device="/dev/video0"):
    """Forced INPUT decoder, local source, no encoder/hardware-listing substitute.

Output NV12 rawvideo is the oracle input; external instrumentation must associate
its frames/PTS and ioctl trace with the exact process starttime/job identity.
The command is only constructed, never executed by this collector.
"""
    for value in (source, frames):
        if not isinstance(value, str) or not value.startswith("/") or any(x in value for x in ("\x00", "\n", "\r")):
            raise ValueError("absolute local paths required")
        if any(x in value.split("/") for x in ("..", ".")) or ":" in value or "\\" in value:
            raise ValueError("unsafe/nonlocal media path")
    if source == frames or type(duration) is not int or not 1 <= duration <= 3600:
        raise ValueError("invalid output/duration")
    if not isinstance(device, str) or not re.fullmatch(r"/dev/video[0-9]+", device):
        raise ValueError("explicit V4L2 node required")
    return [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "verbose", "-n",
            "-protocol_whitelist", "file", "-c:v", "h264_v4l2m2m", "-device", device,
            "-memory_type", "mmap", "-capture_pix_fmt", "nv12",
            "-track_timestamps", "1", "-export_drm", "0", "-i", source,
            "-t", str(duration + 1), "-map", "0:v:0", "-an", "-sn", "-dn",
            "-fps_mode", "passthrough", "-pix_fmt", "nv12", "-c:v", "rawvideo",
            "-f", "rawvideo", frames]


def process_starttime(data):
    # /proc/PID/stat comm may include spaces and ')' characters.
    end = data.rfind(b")")
    fields = data[end + 2:].split()
    if end < 0 or len(fields) < 20 or not fields[19].isdigit():
        raise ValueError("invalid process stat")
    return int(fields[19])


def checked_directory(path):
    p = Path(path)
    if not p.is_absolute():
        raise ValueError("absolute private root required")
    for ancestor in list(reversed(p.parents)) + [p]:
        st = ancestor.lstat()
        if stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ValueError("private root cannot use links/reparse points")
    if "private" not in [part.lower() for part in p.parts] or not p.is_dir():
        raise ValueError("existing private directory required")
    return p


class Reader:
    def __init__(self, seconds, byte_limit):
        self.deadline = time.monotonic() + seconds
        self.remaining = byte_limit
        self.truncated = False

    def check(self):
        if time.monotonic() >= self.deadline or self.remaining <= 0:
            raise TimeoutError("capture deadline/byte budget reached")

    def read(self, path, maximum=65536):
        self.check()
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise ValueError("read limited to regular proc/files")
            out = bytearray()
            cap = min(maximum, self.remaining)
            while len(out) <= cap:
                self.check()
                block = os.read(fd, min(65536, cap + 1 - len(out)))
                if not block:
                    break
                out.extend(block)
            self.remaining -= len(out)
            if len(out) > cap:
                self.truncated = True
                return bytes(out[:cap]), False
            return bytes(out), True
        finally:
            os.close(fd)


def collect(seconds=15, byte_limit=32 * 1024 * 1024):
    if os.name != "posix" or not Path("/proc/self/stat").is_file():
        raise ValueError("Linux device only; host collection refused")
    reader = Reader(seconds, byte_limit)
    records, errors = [], []
    boot, complete = reader.read("/proc/sys/kernel/random/boot_id", 128)
    if not complete:
        raise ValueError("incomplete boot identity")
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        reader.check()
        try:
            comm, ok = reader.read(entry / "comm", 128)
            if not ok or comm.strip() not in (b"mediasrv", b"ffmpeg", b"trim-media"):
                continue
            before, ok = reader.read(entry / "stat", 8192)
            if not ok:
                raise ValueError("incomplete process identity")
            started = process_starttime(before)
            exe = os.readlink(entry / "exe")
            if not exe.startswith("/") or exe.endswith(" (deleted)"):
                raise ValueError("unresolved live executable")
            if not (exe.startswith("/usr/trim/") or exe.startswith("/usr/local/apps/") or exe.startswith("/var/apps/")):
                raise ValueError("unexpected executable prefix")
            image, image_complete = reader.read(Path(exe), 256 * 1024 * 1024)
            cmdline, cmd_ok = reader.read(entry / "cmdline", 16384)
            maps, maps_ok = reader.read(entry / "maps", 1024 * 1024)
            fds = []
            for fd in (entry / "fd").iterdir():
                reader.check()
                target = os.readlink(fd)
                if re.fullmatch(r"/dev/video[0-9]+", target):
                    # stat follows an already-open FD; never open the video node.
                    st = fd.stat()
                    if stat.S_ISCHR(st.st_mode):
                        fds.append({"fd": int(fd.name), "path": target,
                                    "dev_t": [os.major(st.st_rdev), os.minor(st.st_rdev)]})
            after, after_ok = reader.read(entry / "stat", 8192)
            if not after_ok or process_starttime(after) != started or os.readlink(entry / "exe") != exe:
                raise ValueError("process exited/reused during capture")
            records.append({"pid": int(entry.name), "starttime": started, "executable": exe,
                            "executable_sha256": hashlib.sha256(image).hexdigest() if image_complete else None,
                            "hash_complete": image_complete, "cmdline_hex": cmdline.hex(), "cmdline_complete": cmd_ok,
                            "maps": maps.decode(errors="replace"), "maps_complete": maps_ok, "video_fds": fds,
                            "jobid": None, "ioctl": None, "frames": None, "backend": "unknown"})
        except (FileNotFoundError, ProcessLookupError):
            errors.append("process vanished during capture")
        except (OSError, ValueError) as exc:
            errors.append(type(exc).__name__)
    return {"schema": "native-decode-readonly-capture-v1", "origin": "unattested-readonly-observation",
            "hardware": False, "native": False, "native_decode_verified": False,
            "producer_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "boot_id": boot.decode().strip(), "processes": records, "errors": errors,
            "truncated": reader.truncated, "limits": {"seconds": seconds, "bytes": byte_limit},
            "real_task_entry": "unknown; no request is issued", "acceptance_ready": False}


def fixed_fixture():
    return {"schema": "native-decode-evidence-v1", "origin": "synthetic", "run_id": "fixed-host-fixture",
            "versions": {}, "sessions": [], "hardware": False, "native": False,
            "native_decode_verified": False, "acceptance_ready": False,
            "ffmpeg_argv_not_executed": ffmpeg_command("/private/native-fixture/input.h264",
                                                       "/private/native-fixture/frames.nv12"),
            "real_task_entry": "unknown"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--collect-read-only", action="store_true")
    modes.add_argument("--write-fixture", action="store_true")
    parser.add_argument("--private-root")
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--bytes", type=int, default=32 * 1024 * 1024)
    args = parser.parse_args()
    if not args.collect_read_only and not args.write_fixture:
        print(json.dumps({"mode": "plan-only", "execution": "never", "real_task_entry": "unknown",
                          "writes": "require explicit mode and private root", "hardware": False}))
        return 0
    try:
        if not 1 <= args.seconds <= 60 or not 4096 <= args.bytes <= 128 * 1024 * 1024:
            raise ValueError("capture bounds exceeded")
        if not args.private_root:
            raise ValueError("private root required")
        root = checked_directory(args.private_root)
        result = fixed_fixture() if args.write_fixture else collect(args.seconds, args.bytes)
        # Unique new directory, restrictive mode, no overwrites or secret stdout.
        import tempfile
        output = Path(tempfile.mkdtemp(prefix="native-capture-", dir=root))
        os.chmod(output, 0o700)
        with (output / "capture.json").open("x", encoding="utf-8") as stream:
            json.dump(result, stream, sort_keys=True)
        os.chmod(output / "capture.json", 0o600)
        print(json.dumps({"stored_under_private": True, "mode": "fixture" if args.write_fixture else "read-only",
                          "hardware": False, "native_decode_verified": False, "acceptance_ready": False}))
        return 0
    except (OSError, ValueError, TimeoutError):
        # No PID, path, cmdline, boot identity or exception contents to stdout.
        print(json.dumps({"stored_under_private": False, "error": "capture refused/incomplete", "hardware": False}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
