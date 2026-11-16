#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise only mountinfo reads during bounded local process churn; no devices or exports."""
import argparse
import errno
import importlib.util
from pathlib import Path
import subprocess
import threading
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--adapter', type=Path, required=True)
parser.add_argument('--scans', type=int, default=40)
parser.add_argument('--zombies', action='store_true')
args = parser.parse_args()
if not 1 <= args.scans <= 100:
    raise SystemExit('scans must be between 1 and 100')
spec = importlib.util.spec_from_file_location('adapter', args.adapter)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)
stop = threading.Event()
created = [0]
errors = []

def churn():
    try:
        while not stop.is_set():
            children = [subprocess.Popen(['/usr/bin/true']) for _ in range(8)]
            for child in children:
                child.wait(timeout=2)
                created[0] += 1
    except Exception as error:
        errors.append(error)
        stop.set()

worker = threading.Thread(target=churn)
zombies = []
if args.zombies:
    zombies = [subprocess.Popen(['/usr/bin/true']) for _ in range(8)]
worker.start()
transient = 0
success = 0
started = time.monotonic()
try:
    if zombies:
        time.sleep(.05)
        for child in zombies:
            path = Path('/proc') / str(child.pid) / 'mountinfo'
            if not adapter.exited_mountinfo(path):
                raise RuntimeError('Expected unreaped process did not lose its mount namespace')
        print('MOUNTINFO_CONFIRMED_ZOMBIES=' + str(len(zombies)))
    for _ in range(args.scans):
        try:
            adapter.mountinfo_devices()
            success += 1
        except OSError as error:
            if error.errno != errno.EINVAL:
                raise
            transient += 1
        if time.monotonic() - started > 20:
            raise RuntimeError('Scan rehearsal exceeded deadline')
finally:
    stop.set()
    worker.join(timeout=3)
    for child in zombies:
        child.wait(timeout=2)
if worker.is_alive() or errors:
    raise RuntimeError('Process churn did not stop cleanly: ' + repr(errors))
print(f'MOUNTINFO_CHURN_SCANS={args.scans} SUCCESS={success} BOUNDED_EINVAL={transient} CHILDREN={created[0]}')
if not success:
    raise SystemExit('No complete mountinfo scan succeeded')
print('MOUNTINFO_CHURN_OK READONLY_NO_DEVICE_OPERATIONS')
