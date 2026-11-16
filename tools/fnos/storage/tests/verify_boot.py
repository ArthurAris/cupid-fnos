#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify a header-v2 boot image and its embedded gzip initramfs."""
import argparse
import hashlib
from pathlib import Path
import re
import struct
import zlib


def parse_cpio(data):
    files = {}
    offset = 0
    while offset + 110 <= len(data):
        if data[offset:offset+6] not in (b'070701', b'070702'):
            raise ValueError('Invalid newc header')
        fields = [int(data[offset+6+i*8:offset+14+i*8], 16) for i in range(13)]
        size, namesize = fields[6], fields[11]
        if namesize < 1 or offset+110+namesize > len(data):
            raise ValueError('Invalid newc name size')
        name = data[offset+110:offset+110+namesize-1].decode('utf-8')
        body = (offset+110+namesize+3) & ~3
        if body+size > len(data):
            raise ValueError('Truncated cpio entry')
        if name == 'TRAILER!!!':
            return files
        files[name.removeprefix('./')] = (fields[1], data[body:body+size])
        offset = (body+size+3) & ~3
    raise ValueError('No cpio trailer')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('boot_image', type=Path)
    parser.add_argument('kernel_image', type=Path)
    args = parser.parse_args()
    boot = args.boot_image.read_bytes()
    kernel = args.kernel_image.read_bytes()
    if boot[:8] != b'ANDROID!':
        raise ValueError('Invalid Android boot header')
    words = struct.unpack_from('<10I', boot, 8)
    kernel_size, kernel_address, ramdisk_size, _, _, _, tags, page, version, _ = words
    if (version, page, kernel_address, tags, ramdisk_size) != (2, 2048, 0x10008000, 0x10000100, 0):
        raise ValueError('Boot parameters differ from the approved baseline')
    if kernel_size != len(kernel) or boot[page:page+kernel_size] != kernel:
        raise ValueError('Packed kernel does not match candidate')
    dtb_size, dtb_address = struct.unpack_from('<IQ', boot, 1648)
    if dtb_size <= 0 or dtb_address != 0x11f00000:
        raise ValueError('DTB missing or address mismatch')
    if len(boot) != page+((kernel_size+page-1)//page)*page+((dtb_size+page-1)//page)*page:
        raise ValueError('Unexpected packed length')
    matched = False
    for stream in re.finditer(b'\x1f\x8b\x08', kernel):
        try:
            decoder = zlib.decompressobj(16+zlib.MAX_WBITS)
            payload = decoder.decompress(kernel[stream.start():], 128*1024**2)
            if not decoder.eof or not payload.startswith(b'07070'):
                continue
            files = parse_cpio(payload)
        except (ValueError, UnicodeError, zlib.error):
            continue
        if 'init' not in files:
            continue
        mode, init = files['init']
        if mode & 0o170000 != 0o100000 or not mode & 0o111 or init[:4] != b'\x7fELF':
            raise ValueError('init must be an executable ELF regular file')
        if struct.unpack_from('<H', init, 18)[0] != 183:
            raise ValueError('init is not AArch64')
        for marker in (b'xiaomi,cupid', b'/host/fnos/root.img', b'cupid-fnos-v1'):
            if marker not in init:
                raise ValueError('fnOS init marker missing')
        if 'bin/busybox' not in files:
            raise ValueError('Rescue busybox missing')
        print(f'PASS embedded ARM64 fnOS init, {len(files)} cpio entries, {len(payload)} unpacked bytes')
        matched = True
        break
    if not matched:
        raise ValueError('No complete embedded fnOS initramfs found')
    print('BOOT_SHA256=' + hashlib.sha256(boot).hexdigest())
    print('KERNEL_SHA256=' + hashlib.sha256(kernel).hexdigest())
    print('BOOT_IMAGE_VERIFIED_NOT_DEPLOYED')


if __name__ == '__main__':
    main()
