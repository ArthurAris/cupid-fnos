#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Generate narrowly scoped fnOS block-device policy from the verified export."""
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

RUN = Path('/run/cupid-storage')
POLICY = Path('/run/systemd/system/trim_main.service.d/80-cupid-device-policy.conf')


def probe(device):
    path = Path(device).resolve(strict=True)
    info = path.stat()
    if not stat.S_ISBLK(info.st_mode):
        raise RuntimeError('Export is not a block device')
    sysdev = Path('/sys/class/block') / path.name
    if not (sysdev / 'device').exists() or (sysdev / 'partition').exists():
        raise RuntimeError('Export must be a complete SCSI disk')
    major, minor = map(int, (sysdev / 'dev').read_text().strip().split(':'))
    size = int((sysdev / 'range').read_text())
    if major != 8 or size != 16 or minor % 16:
        raise RuntimeError('Unsupported SCSI device range; refusing broad permissions')
    return path, major, minor, size


def main():
    if os.geteuid() != 0:
        raise RuntimeError('Root required')
    state = json.loads((RUN / 'state.json').read_text())
    device = subprocess.check_output(
        ['/usr/bin/python3', '-I', '/usr/local/lib/cupid/cupid-storage.py', 'status'],
        text=True).strip()
    if device != state['device']:
        raise RuntimeError('Adapter state and live export disagree')
    path, major, minor, size = probe(device)
    nodes = Path('/dev/cupid-storage-policy')
    nodes.mkdir(mode=0o700, exist_ok=True)
    rules = [
        '[Service]',
        'DevicePolicy=closed',
        'DeviceAllow=',
        'DeviceAllow=block-sd r',
        'DeviceAllow=block-blkext r',
        'DeviceAllow=block-md rwm',
        'DeviceAllow=block-device-mapper rwm',
        'DeviceAllow=/dev/mapper/control rw',
    ]
    for offset in range(size):
        node = nodes / str(offset)
        expected = os.makedev(major, minor + offset)
        if node.exists():
            info = node.stat()
            if not stat.S_ISBLK(info.st_mode) or info.st_rdev != expected:
                raise RuntimeError('Policy alias has unexpected identity')
        else:
            os.mknod(node, stat.S_IFBLK | 0o600, expected)
        rules.append('DeviceAllow=' + str(node) + ' rw')
    rules += [
        'ExecStartPre=/usr/bin/python3 -I /usr/local/lib/cupid/cupid-device-access-check.py',
        '',
    ]
    POLICY.parent.mkdir(parents=True, exist_ok=True)
    temporary = POLICY.with_suffix('.tmp')
    temporary.write_text('\n'.join(rules))
    os.chmod(temporary, 0o600)
    os.replace(temporary, POLICY)
    subprocess.run(['systemctl', 'daemon-reload'], check=True, timeout=30)
    print('POLICY_EXPORT=' + str(path))
    print(f'POLICY_SCSI_RANGE={major}:{minor}-{minor+size-1}')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('cupid-storage-policy: ' + str(error), file=sys.stderr)
        sys.exit(1)
