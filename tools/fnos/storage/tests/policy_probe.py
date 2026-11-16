#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Opening only: run under a transient systemd unit on disposable test devices.
import errno
import os
import sys

parent, exported = sys.argv[1:]
fd = os.open(parent, os.O_RDONLY)
os.close(fd)
try:
    fd = os.open(parent, os.O_RDWR)
except OSError as error:
    if error.errno not in (errno.EPERM, errno.EACCES):
        raise
else:
    os.close(fd)
    raise RuntimeError('Device policy permitted raw parent write access')
fd = os.open(exported, os.O_RDWR)
os.close(fd)
print('PASS parent read permitted, raw parent write denied, SCSI write permitted')
