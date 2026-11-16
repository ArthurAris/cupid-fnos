#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Present a verified partition as a local SCSI disk; never format it."""
import argparse
import contextlib
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time

CONFIGFS = Path('/sys/kernel/config/target')
SYS_BLOCK = Path('/sys/class/block')
RUN = Path('/run/cupid-storage')
KEYS = {
    'backing_partuuid', 'parent_ptuuid', 'start_sector_512',
    'size_sector_512', 'logical_block_size', 'virtual_serial', 'readonly',
}
UUID = re.compile(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}')


def fail(message):
    raise RuntimeError(message)


def command(*args):
    return subprocess.check_output(args, text=True, timeout=30,
                                   stderr=subprocess.PIPE).strip()


def load_config(path):
    with open(path, encoding='utf-8') as stream:
        config = json.load(stream)
    if not isinstance(config, dict) or set(config) != KEYS:
        fail('Configuration must contain exactly the documented keys.')
    for key in ('backing_partuuid', 'parent_ptuuid'):
        if not isinstance(config[key], str) or not UUID.fullmatch(config[key]):
            fail('Invalid GPT UUID: ' + key)
        config[key] = config[key].lower()
    for key in ('start_sector_512', 'size_sector_512', 'logical_block_size'):
        if type(config[key]) is not int or config[key] <= 0:
            fail('Expected positive integer: ' + key)
    if config['logical_block_size'] not in (512, 4096):
        fail('Unsupported logical block size.')
    if type(config['readonly']) is not bool:
        fail('readonly must be a JSON boolean.')
    if not isinstance(config['virtual_serial'], str) or not re.fullmatch(
            r'[0-9a-f]{16}', config['virtual_serial']):
        fail('virtual_serial must be 16 lowercase hexadecimal digits.')
    for key in ('start_sector_512', 'size_sector_512'):
        if config[key] * 512 % config['logical_block_size']:
            fail('Partition geometry is not logical-sector aligned.')
    return config


def read(path):
    return Path(path).read_text().strip()


def write(path, value):
    with open(path, 'w', encoding='ascii') as stream:
        stream.write(str(value) + '\n')


def metadata(device, field):
    return command('blkid', '-p', '-s', field, '-o', 'value', str(device)).lower()


def verify_backing(config):
    matches = command('blkid', '-t', 'PARTUUID=' + config['backing_partuuid'],
                      '-o', 'device').splitlines()
    devices = {Path(item).resolve() for item in matches}
    if len(devices) != 1:
        fail('Exactly one matching backing partition is required.')
    device = devices.pop()
    info = device.stat()
    if not stat.S_ISBLK(info.st_mode):
        fail('Backing path is not a block device.')
    sysdev = (SYS_BLOCK / device.name).resolve(strict=True)
    if not (sysdev / 'partition').exists():
        fail('Backing device must be a partition, never a whole disk.')
    parent = sysdev.parent
    if not (SYS_BLOCK / parent.name).exists():
        fail('Cannot verify backing parent.')
    parent_device = Path('/dev') / parent.name
    if metadata(device, 'PART_ENTRY_UUID') != config['backing_partuuid']:
        fail('Backing PARTUUID changed.')
    if metadata(parent_device, 'PTUUID') != config['parent_ptuuid']:
        fail('Backing parent GPT UUID mismatch.')
    start = int(read(sysdev / 'start'))
    size = int(read(sysdev / 'size'))
    if start != config['start_sector_512'] or size != config['size_sector_512']:
        fail('Backing partition geometry mismatch.')
    if start + size > int(read(parent / 'size')):
        fail('Backing range exceeds parent capacity.')
    if int(command('blockdev', '--getsize64', str(device))) != size * 512:
        fail('Backing capacity mismatch.')
    if int(command('blockdev', '--getss', str(device))) != config['logical_block_size']:
        fail('Backing logical sector size mismatch.')
    if read(sysdev / 'dev') != f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}':
        fail('Backing device node does not match sysfs.')
    return device, sysdev


def exited_mountinfo(path):
    try:
        status = (path.parent / 'status').read_text().splitlines()
    except (FileNotFoundError, ProcessLookupError):
        return True
    states = [fields[1] for line in status
              if (fields := line.split()) and fields[0] == 'State:' and len(fields) > 1]
    if states not in (['Z'], ['X']):
        return False
    try:
        os.readlink(path.parent / 'ns/mnt')
    except (FileNotFoundError, ProcessLookupError):
        return True
    return False


def mountinfo_devices():
    result = set()
    for attempt in range(3):
        retry = None
        for path in Path('/proc').glob('[0-9]*/mountinfo'):
            try:
                lines = path.read_text().splitlines()
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as error:
                # proc returns EINVAL when a task loses its mount namespace.
                if error.errno != errno.EINVAL:
                    raise
                if exited_mountinfo(path):
                    continue
                retry = error
                continue
            for line in lines:
                fields = line.split()
                if len(fields) < 3:
                    continue
                result.add(fields[2])
                if '-' in fields:
                    index = fields.index('-')
                    if len(fields) > index + 2:
                        source = fields[index + 2]
                        if source.startswith('/dev/'):
                            try:
                                info = Path(source).stat()
                            except FileNotFoundError:
                                continue
                            if stat.S_ISBLK(info.st_mode):
                                result.add(f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}')
        if retry is None:
            return result
        if attempt == 2:
            raise retry
        time.sleep(.05)
    fail('Cannot complete mountinfo scan.')


def mounted_devices():
    result = mountinfo_devices()
    # Btrfs uses anonymous mount IDs; include every registered member device.
    for device in Path('/sys/fs/btrfs').glob('*/devices/*'):
        if (device / 'dev').exists():
            result.add(read(device / 'dev'))
    for line in Path('/proc/swaps').read_text().splitlines()[1:]:
        path = line.split()[0]
        try:
            info = Path(path).stat()
            if stat.S_ISBLK(info.st_mode):
                result.add(f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}')
        except FileNotFoundError:
            fail('Cannot verify a swap device.')
    return result


def check_unused(sysdev):
    if read(sysdev / 'dev') in mounted_devices():
        fail('Device is mounted or used for swap: ' + sysdev.name)
    holders = list((sysdev / 'holders').iterdir())
    if holders:
        fail('Device has active holders: ' + ', '.join(item.name for item in holders))


def paths(config):
    serial = config['virtual_serial']
    core = CONFIGFS / 'core' / ('iblock_' + str(int(serial[-8:], 16)))
    target = CONFIGFS / 'loopback' / ('naa.6' + serial[:15])
    return core, core / 'cupid-data', target, target / 'tpgt_1'


def disk_for_tpg(tpg, config):
    address = read(tpg / 'address')
    directory = Path('/sys/class/scsi_device') / (address + ':1') / 'device/block'
    for _ in range(100):
        disks = list(directory.iterdir()) if directory.exists() else []
        if len(disks) == 1:
            device = Path('/dev') / disks[0].name
            try:
                size = int(command('blockdev', '--getsize64', str(device)))
                if size == config['size_sector_512'] * 512:
                    return device
            except (OSError, subprocess.SubprocessError):
                pass
        time.sleep(.1)
    fail('Expected SCSI disk did not become ready.')


def verify_export(config, backing):
    core, storage, target, tpg = paths(config)
    if not storage.exists() or not tpg.exists():
        fail('Expected export is absent or incomplete.')
    expected_major_minor = read(SYS_BLOCK / backing.name / 'dev')
    info = read(storage / 'info')
    major, minor = expected_major_minor.split(':')
    if not re.search(r'Major:\s*' + re.escape(major) +
                     r'\s+Minor:\s*' + re.escape(minor) + r'\s', info):
        fail('Existing export uses another backing device.')
    if not re.search(r'exclusive:\s*1\b', info):
        fail('Existing export does not hold its backing exclusively.')
    actual_serial = read(storage / 'wwn/vpd_unit_serial')
    prefix = 'T10 VPD Unit Serial Number: '
    if actual_serial.startswith(prefix):
        actual_serial = actual_serial[len(prefix):]
    if actual_serial != config['virtual_serial']:
        fail('Existing export has another VPD identity: ' + repr(actual_serial))
    link = tpg / 'lun/lun_1/data'
    if not link.is_symlink() or link.resolve() != storage.resolve():
        fail('Existing LUN link is not owned by this adapter.')
    device = disk_for_tpg(tpg, config)
    if int(command('blockdev', '--getro', str(device))) != int(config['readonly']):
        fail('Existing export has another read-only mode.')
    return device


def save_state(config, device):
    RUN.mkdir(mode=0o700, exist_ok=True)
    state = {'config': config, 'device': str(device),
             'major_minor': read(SYS_BLOCK / device.name / 'dev')}
    temporary = RUN / 'state.json.tmp'
    with open(temporary, 'w', encoding='ascii') as stream:
        json.dump(state, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, RUN / 'state.json')


def start(config):
    backing, sysdev = verify_backing(config)
    core, storage, target, tpg = paths(config)
    if core.exists() or target.exists():
        device = verify_export(config, backing)
        save_state(config, device)
        return device
    check_unused(sysdev)
    for module in ('target_core_mod', 'target_core_iblock', 'tcm_loop'):
        command('modprobe', module)
    if not CONFIGFS.exists():
        fail('Target configfs is absent; mount configfs first.')
    loopback = CONFIGFS / 'loopback'
    if not loopback.exists():
        loopback.mkdir()
    created = []
    try:
        for directory in (core, storage):
            directory.mkdir()
            created.append(directory)
        write(storage / 'control', 'udev_path=' + str(backing) +
              ',readonly=' + str(int(config['readonly'])))
        write(storage / 'enable', '1')
        write(storage / 'wwn/vpd_unit_serial', config['virtual_serial'])
        for directory in (target, tpg):
            directory.mkdir()
            created.append(directory)
        write(tpg / 'nexus', target.name)
        lun = tpg / 'lun/lun_1'
        lun.mkdir()
        created.append(lun)
        (lun / 'data').symlink_to(storage)
        device = verify_export(config, backing)
        save_state(config, device)
        return device
    except Exception:
        with contextlib.suppress(OSError):
            (tpg / 'lun/lun_1/data').unlink()
        if tpg.exists():
            with contextlib.suppress(OSError):
                write(tpg / 'nexus', 'NULL')
        for directory in reversed(created):
            with contextlib.suppress(OSError):
                directory.rmdir()
        raise


def stop(config):
    backing, _ = verify_backing(config)
    core, storage, target, tpg = paths(config)
    if not core.exists() and not target.exists():
        return
    device = verify_export(config, backing)
    sysdev = (SYS_BLOCK / device.name).resolve()
    descendants = [sysdev] + [item for item in sysdev.iterdir()
                              if item.is_dir() and (item / 'partition').exists()]
    for item in descendants:
        check_unused(item)
    command('blockdev', '--flushbufs', str(device))
    (tpg / 'lun/lun_1/data').unlink()
    write(tpg / 'nexus', 'NULL')
    for directory in (tpg / 'lun/lun_1', tpg, target, storage, core):
        directory.rmdir()
    with contextlib.suppress(FileNotFoundError):
        (RUN / 'state.json').unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('verify', 'start', 'status', 'stop'))
    parser.add_argument('--config', default='/etc/cupid/storage.json')
    args = parser.parse_args()
    config = load_config(args.config)
    if os.geteuid() != 0:
        fail('Root is required.')
    RUN.mkdir(mode=0o700, exist_ok=True)
    with open(RUN / 'lock', 'a', encoding='ascii') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.action == 'verify':
            backing, _ = verify_backing(config)
            print(json.dumps({'backing': str(backing), 'readonly': config['readonly']}))
        elif args.action == 'start':
            print(start(config))
        elif args.action == 'stop':
            stop(config)
        else:
            backing, _ = verify_backing(config)
            print(verify_export(config, backing))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('cupid-storage: ' + str(error), file=sys.stderr)
        sys.exit(1)
