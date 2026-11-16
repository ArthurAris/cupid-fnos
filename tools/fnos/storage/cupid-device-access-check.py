#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail fnOS startup if its device cgroup permits raw UFS write access."""
import errno
import json
import os
from pathlib import Path
import stat
import sys


def write_open(path, permitted):
    try:
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC)
    except OSError as error:
        if permitted or error.errno not in (errno.EACCES, errno.EPERM):
            raise
    else:
        os.close(fd)
        if not permitted:
            raise RuntimeError('Unexpected raw UFS write permission: ' + str(path))


def main():
    state = json.loads(Path('/run/cupid-storage/state.json').read_text())
    export = Path(state['device']).resolve(strict=True)
    write_open(export, True)
    verified = 0
    for sysdev in Path('/sys/class/block').glob('sd*'):
        physical = sysdev.resolve()
        parent = physical.parent if (sysdev / 'partition').exists() else physical
        parent_name = parent.name
        if parent_name == export.name:
            continue
        # Only real UFS LUNs belong to the device being protected.
        device = parent / 'device'
        if not device.exists() or not any(
                ancestor.name.endswith('.ufshc') for ancestor in device.resolve().parents):
            continue
        node = Path('/dev') / sysdev.name
        if not stat.S_ISBLK(node.stat().st_mode):
            raise RuntimeError('Unexpected raw device node')
        write_open(node, False)
        verified += 1
    if verified == 0:
        raise RuntimeError('No UFS devices verified; refusing unsafe service startup')
    print('FNOS_RAW_UFS_WRITE_DENIED devices=' + str(verified))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError) as error:
        print('cupid-device-access-check: ' + str(error), file=sys.stderr)
        sys.exit(1)
