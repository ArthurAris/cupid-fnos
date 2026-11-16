#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Read-only FastRPC/CDSP presence inventory; no IOCTL, load, invoke or signing bypass."""
import json
import os
from pathlib import Path


def inventory(root):
    root = Path(root)
    rows = []
    for entry in (root / 'sys/class/remoteproc').glob('*'):
        try:
            rows.append({'name': (entry / 'name').read_text().strip(),
                         'state': (entry / 'state').read_text().strip()})
        except OSError as error:
            rows.append({'read_error': error.errno})
    return {'remoteproc': rows,
            'fastrpc_nodes': [p.name for p in (root / 'dev').glob('fastrpc*')],
            'cdsp_running': any(r.get('name') == 'cdsp' and r.get('state') == 'running' for r in rows),
            'dsp_rpc_tested': False, 'npu_inference_tested': False,
            'no_ioctl_or_driver_load': True}


def main():
    compatible = Path('/proc/device-tree/compatible').read_bytes().split(b'\0')
    if b'xiaomi,cupid' not in compatible:
        raise SystemExit('Not the tested cupid board')
    result = inventory('/')
    result['kernel'] = os.uname().release
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
