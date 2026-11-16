#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail-closed, offline ADSP loader. No unload/rebind and no locator handshake claim."""
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import stat
import struct
import subprocess
import sys
import time
import uuid

KERNEL = '6.18.18.c1147-trim'
BOARD = 'xiaomi,cupid'
RELEASE = 'cupid-fnos-v1'
VERMAGIC = KERNEL + ' SMP mod_unload modversions aarch64'
BASE = '/usr/local/lib/cupid/adsp'
DEPLOY_PENDING = '/usr/local/lib/cupid/adsp-deploy-pending.json'
STATE = '/var/lib/cupid-adsp'
MODULES = (
    'cupid_hwspinlock_core', 'cupid_hwspinlock_qcom', 'cupid_qcom_smem',
    'cupid_qcom_socinfo', 'cupid_qcom_smp2p', 'cupid_qcom_glink_smem',
    'cupid_qcom_pil_info', 'cupid_qcom_rproc_common', 'cupid_qcom_q6v5_pas',
    'cupid_qrtr_smd',
)
START_BUDGET = 90
FALLBACK_BUDGET = 10
MAX_MODULE_BYTES = 16 * 1024 * 1024


class Interrupted(RuntimeError):
    pass


def reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


class Runtime:
    """All host operations are injectable; offline tests override command/time/read."""
    def __init__(self, root=Path('/')):
        self.root = Path(root)

    def path(self, name):
        return self.root / name.lstrip('/')

    def now(self):
        return time.monotonic()

    def sleep(self, seconds):
        time.sleep(seconds)

    def read(self, name, limit=4096):
        fd = os.open(self.path(name), os.O_RDONLY | os.O_CLOEXEC)
        try:
            data = os.read(fd, limit + 1)
        finally:
            os.close(fd)
        if len(data) > limit:
            raise RuntimeError('Oversized attribute: ' + name)
        return data

    def text(self, name):
        return self.read(name).decode('ascii').strip()

    def command(self, arguments, timeout=6):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})
        if result.returncode:
            raise RuntimeError('Command failed: ' + arguments[0] + ': ' +
                               (result.stdout + result.stderr).strip()[:300])
        return result.stdout.strip()

    def sync(self, fd):
        os.fsync(fd)


ERRORS = (OSError, ValueError, RuntimeError, subprocess.SubprocessError)


def trusted_info(info, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Storage must be root-owned, non-symlink, not group/other writable')
    if not directory and info.st_nlink != 1:
        raise RuntimeError('Trusted files must not have hard-link aliases')


def directory_fd(runtime, name, create=False):
    """Walk with openat/O_NOFOLLOW, including /; never follow a symlink parent."""
    parts = Path(name).parts
    if not name.startswith('/') or '..' in parts:
        raise RuntimeError('Invalid trusted path')
    fd = os.open(runtime.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        trusted_info(os.fstat(fd), directory=True)
        for index, part in enumerate(parts[1:]):
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                # Only the final state directory may be created. /var/lib must be trusted.
                if not create or index != len(parts[1:]) - 1:
                    raise
                os.mkdir(part, 0o700, dir_fd=fd)
                runtime.sync(fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                                os.O_CLOEXEC, dir_fd=fd)
            try:
                trusted_info(os.fstat(child), directory=True)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def trusted_bytes(runtime, name, limit):
    parent = directory_fd(runtime, str(Path(name).parent))
    fd = None
    try:
        fd = os.open(Path(name).name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                     os.O_NONBLOCK, dir_fd=parent)
        trusted_info(os.fstat(fd))
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise RuntimeError('Oversized trusted file: ' + name)
        return data
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def build_note(data):
    """Extract the raw GNU note, not just its descriptor, from an AArch64 ELF64 .ko."""
    if len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01':
        raise RuntimeError('Module is not little-endian ELF64')
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    if header[1:4] != (1, 183, 1):
        raise RuntimeError('Module is not an AArch64 relocatable ELF')
    offset, entry_size, count, strings = header[6], header[11], header[12], header[13]
    if entry_size != 64 or not 0 < count <= 4096 or not 0 < strings < count:
        raise RuntimeError('Invalid ELF section table')
    if offset < 64 or offset + count * entry_size > len(data):
        raise RuntimeError('Truncated ELF section table')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * 64) for i in range(count)]

    def section_bytes(section):
        begin, size = section[4:6]
        if begin + size > len(data):
            raise RuntimeError('Truncated ELF section')
        return data[begin:begin + size]

    names = section_bytes(sections[strings])
    notes = []
    for section in sections:
        start = section[0]
        end = names.find(b'\0', start)
        if start >= len(names) or end < 0:
            raise RuntimeError('Invalid ELF section name')
        if names[start:end] == b'.note.gnu.build-id':
            if section[1] != 7:
                raise RuntimeError('GNU build note is not SHT_NOTE')
            notes.append(section_bytes(section))
    if len(notes) != 1:
        raise RuntimeError('Expected one GNU build note')
    validate_note(notes[0])
    return notes[0]


def validate_note(note):
    if len(note) < 16:
        raise RuntimeError('Invalid raw GNU build note')
    namesz, descsz, kind = struct.unpack_from('<III', note)
    if namesz != 4 or kind != 3 or not 1 <= descsz <= 64 or note[12:16] != b'GNU\0':
        raise RuntimeError('Invalid raw GNU build note')
    if len(note) != 16 + ((descsz + 3) & ~3):
        raise RuntimeError('Invalid raw GNU build note length')


def remaining(runtime, deadline, maximum=6):
    left = deadline - runtime.now()
    if left <= 0:
        raise RuntimeError('Startup deadline exhausted')
    return min(maximum, left)


def command(runtime, arguments, deadline):
    return runtime.command(arguments, timeout=remaining(runtime, deadline))


def validate_environment(runtime):
    if runtime.text('/proc/sys/kernel/osrelease') != KERNEL:
        raise RuntimeError('Wrong kernel')
    if BOARD.encode() not in runtime.read('/proc/device-tree/compatible').split(b'\0'):
        raise RuntimeError('Wrong board')
    if runtime.text('/etc/cupid-fnos-release') != RELEASE:
        raise RuntimeError('Wrong fnOS release')


def validate_files(runtime, deadline):
    manifest = json.loads(trusted_bytes(runtime, BASE + '/module-manifest.json', 65536),
                          object_pairs_hook=reject_duplicates)
    if (not isinstance(manifest, dict) or set(manifest) !=
            {'schema', 'kernel', 'board', 'release', 'modules'} or
            type(manifest['schema']) is not int or manifest['schema'] != 1 or
            (manifest['kernel'], manifest['board'], manifest['release']) != (KERNEL, BOARD, RELEASE)):
        raise RuntimeError('Invalid manifest environment/schema')
    rows = manifest['modules']
    if not isinstance(rows, list) or len(rows) != len(MODULES):
        raise RuntimeError('Manifest must contain the fixed ten modules')
    files = {}
    for expected, row in zip(MODULES, rows):
        if not isinstance(row, dict) or set(row) != {'name', 'file', 'sha256', 'build_note', 'vermagic'}:
            raise RuntimeError('Invalid module manifest entry')
        if row['name'] != expected or row['file'] != expected + '.ko' or row['vermagic'] != VERMAGIC:
            raise RuntimeError('Manifest module order/name/file/vermagic mismatch')
        if not isinstance(row['sha256'], str) or not re.fullmatch('[0-9a-fA-F]{64}', row['sha256']):
            raise RuntimeError('Invalid manifest SHA256')
        if not isinstance(row['build_note'], str) or not re.fullmatch('[0-9a-fA-F]{32,160}', row['build_note']):
            raise RuntimeError('Invalid manifest build note')
        note = bytes.fromhex(row['build_note'])
        validate_note(note)
        data = trusted_bytes(runtime, BASE + '/' + row['file'], MAX_MODULE_BYTES)
        if hashlib.sha256(data).hexdigest() != row['sha256'].lower():
            raise RuntimeError('Module SHA256 mismatch: ' + expected)
        if build_note(data) != note:
            raise RuntimeError('Module build note mismatch: ' + expected)
        path = runtime.path(BASE + '/' + row['file'])
        for field, wanted in (('name', expected), ('vermagic', VERMAGIC)):
            if command(runtime, ['/usr/sbin/modinfo', '-F', field, str(path)], deadline) != wanted:
                raise RuntimeError('Module ' + field + ' mismatch: ' + expected)
        files[expected] = path
    return rows, files


def check_loaded(runtime, name, note=None):
    base = '/sys/module/' + name
    if not runtime.path(base).exists():
        return False
    if runtime.text(base + '/initstate') != 'live':
        raise RuntimeError('Loaded module is not live: ' + name)
    # Standard distribution modules have no pinned build identity. Custom modules do.
    if note is not None and runtime.read(base + '/notes/.note.gnu.build-id').hex() != note.lower():
        raise RuntimeError('Loaded custom module build mismatch: ' + name)
    return True


def wait_for(runtime, check, deadline, seconds, description):
    end = min(deadline, runtime.now() + seconds)
    while True:
        value = check()
        if value:
            return value
        if runtime.now() >= end:
            raise RuntimeError(description + ' deadline')
        runtime.sleep(min(.25, end - runtime.now()))


def standard_module(runtime, name, deadline):
    if check_loaded(runtime, name):
        return
    command(runtime, ['/usr/sbin/modprobe', name], deadline)
    wait_for(runtime, lambda: check_loaded(runtime, name), deadline, 2, name + ' live')


def fallback(runtime):
    """Best effort only: a kernel-stuck modprobe/SIGKILL cannot guarantee recovery."""
    end = runtime.now() + FALLBACK_BUDGET
    try:
        standard_module(runtime, 'pmic_glink', end)
        return {'status': 'live', 'module': 'pmic_glink'}
    except ERRORS as error:
        return {'status': 'failed', 'module': 'pmic_glink', 'reason': str(error)[:500]}


def sysfs_resolve(runtime, path):
    target = path.resolve(strict=True)
    if not target.is_relative_to(runtime.path('/sys/devices')):
        raise RuntimeError('Unexpected sysfs device ancestry')
    return target


def locate_adsp(runtime):
    matches = []
    for entry in runtime.path('/sys/class/remoteproc').glob('remoteproc*'):
        try:
            if runtime.text('/' + str(entry.relative_to(runtime.root)) + '/name') != 'adsp':
                continue
            target = sysfs_resolve(runtime, entry)
            # Linux class path resolves to <platform>/remoteproc/remoteprocN.
            parent = target.parent.parent if target.parent.name == 'remoteproc' else target.parent
            if parent.name != '3000000.remoteproc':
                continue
            platform = sysfs_resolve(runtime, runtime.path('/sys/bus/platform/devices/3000000.remoteproc'))
            if parent != platform:
                continue
            matches.append((entry, target, parent))
        except FileNotFoundError:
            continue
    if len(matches) > 1:
        raise RuntimeError('Ambiguous ADSP remoteproc')
    if not matches:
        return None
    entry, target, parent = matches[0]
    if runtime.text('/' + str(entry.relative_to(runtime.root)) + '/state') != 'running':
        return None
    return target, parent


def belongs_to(runtime, device, parent):
    return sysfs_resolve(runtime, device).is_relative_to(parent)


def driver_module(runtime, device, bus, driver_name, module):
    link = device / 'driver'
    if not link.is_symlink():
        return False
    target = link.resolve(strict=True)
    if target.parent != runtime.path('/sys/bus/' + bus + '/drivers').resolve():
        return False
    if driver_name is not None and target.name != driver_name:
        return False
    owner = target / 'module'
    return (owner.is_symlink() and owner.resolve(strict=True) ==
            runtime.path('/sys/module/' + module).resolve(strict=True) and check_loaded(runtime, module))


def qrtr_ready(runtime, parent):
    matches = []
    for device in runtime.path('/sys/bus/rpmsg/devices').glob('*'):
        try:
            attr = '/' + str(device.relative_to(runtime.root)) + '/name'
            if runtime.text(attr) == 'IPCRTR' and belongs_to(runtime, device, parent):
                matches.append(device)
        except FileNotFoundError:
            continue
    if len(matches) > 1:
        raise RuntimeError('Ambiguous ADSP IPCRTR channel')
    return bool(matches) and driver_module(runtime, matches[0], 'rpmsg', 'qcom_smd_qrtr', 'cupid_qrtr_smd')


def pdm_ready(runtime, parent):
    matches = []
    for device in runtime.path('/sys/bus/auxiliary/devices').glob('*'):
        if not re.fullmatch(r'qcom_common\.pd-mapper\.[0-9]+', device.name):
            continue
        try:
            if belongs_to(runtime, device, parent) and sysfs_resolve(runtime, device).name == device.name:
                matches.append(device)
        except FileNotFoundError:
            continue
    if len(matches) > 1:
        raise RuntimeError('Ambiguous ADSP PDM device')
    return bool(matches) and driver_module(runtime, matches[0], 'auxiliary', None, 'qcom_pd_mapper')


def battery_sample(runtime):
    base = '/sys/class/power_supply/qcom-battmgr-bat/'
    try:
        values = {key: runtime.text(base + key) for key in ('capacity', 'present', 'status', 'type')}
    except OSError as error:
        if error.errno in (errno.EAGAIN, errno.ENOENT):
            return None
        raise
    if not re.fullmatch('[0-9]{1,3}', values['capacity']) or not 0 <= int(values['capacity']) <= 100:
        raise RuntimeError('Invalid battery capacity')
    if values['present'] != '1' or values['type'] != 'Battery':
        raise RuntimeError('Battery absent or wrong supply type')
    if values['status'] not in ('Charging', 'Discharging', 'Not charging', 'Full'):
        raise RuntimeError('Invalid/unknown battery status')
    values['capacity'] = int(values['capacity'])
    return values


class SharedLock:
    """Serialize every load, including standard fallback, with deployment."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.fd = None

    def __enter__(self):
        runfd = directory_fd(self.runtime, '/run')
        try:
            self.fd = os.open('cupid-adsp.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW |
                              os.O_CLOEXEC | os.O_NONBLOCK, 0o600, dir_fd=runfd)
        finally:
            os.close(runfd)
        try:
            trusted_info(os.fstat(self.fd))
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return self
        except BaseException:
            os.close(self.fd)
            self.fd = None
            raise

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class StateStore:
    """Durable write-before-load record; caller must hold SharedLock throughout."""
    def __init__(self, runtime):
        self.runtime = runtime
        self.fd = None
        self.data = None

    def __enter__(self):
        try:
            self.fd = directory_fd(self.runtime, STATE, create=True)
            # Old "ok" is never evidence of success, even when state is corrupt.
            try:
                os.unlink('ok', dir_fd=self.fd)
                self.runtime.sync(self.fd)
            except FileNotFoundError:
                pass
            self.data = self.load()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *unused):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def load(self):
        try:
            data = json.loads(trusted_bytes(self.runtime, STATE + '/state.json', 4096),
                              object_pairs_hook=reject_duplicates)
        except FileNotFoundError:
            failures = 0
            try:
                legacy = trusted_bytes(self.runtime, STATE + '/attempts', 32).decode('ascii').strip()
                if not re.fullmatch('[0-2]', legacy):
                    raise RuntimeError('Corrupt legacy attempt counter')
                failures = int(legacy)
            except FileNotFoundError:
                pass
            data = {'schema': 1, 'failures': failures, 'boot_id': '', 'outcome': 'skipped'}
        if (not isinstance(data, dict) or set(data) != {'schema', 'failures', 'boot_id', 'outcome'} or
                type(data['schema']) is not int or data['schema'] != 1 or
                type(data['failures']) is not int or not 0 <= data['failures'] <= 2 or
                not isinstance(data['boot_id'], str) or
                (data['boot_id'] and not valid_boot_id(data['boot_id'])) or
                data['outcome'] not in ('ready', 'pending', 'failed', 'skipped', 'safe')):
            raise RuntimeError('Corrupt startup state/counter')
        if ((data['outcome'] == 'ready' and (data['failures'] != 0 or not data['boot_id'])) or
                (data['outcome'] in ('pending', 'failed') and (not data['boot_id'] or not data['failures'])) or
                (data['outcome'] == 'safe' and (data['failures'] != 2 or not data['boot_id'])) or
                (data['outcome'] == 'skipped' and data['boot_id'])):
            raise RuntimeError('Inconsistent startup state/counter')
        return data

    def save(self, data):
        # Refuse an unsafe existing state file instead of silently replacing it.
        try:
            trusted_info(os.stat('state.json', dir_fd=self.fd, follow_symlinks=False))
        except FileNotFoundError:
            pass
        name = '.state-' + uuid.uuid4().hex
        temp = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                       os.O_CLOEXEC, 0o600, dir_fd=self.fd)
        try:
            trusted_info(os.fstat(temp))
            with os.fdopen(temp, 'wb', closefd=False) as stream:
                stream.write((json.dumps(data, sort_keys=True) + '\n').encode('ascii'))
                stream.flush()
            self.runtime.sync(temp)
            os.replace(name, 'state.json', src_dir_fd=self.fd, dst_dir_fd=self.fd)
            self.runtime.sync(self.fd)
            self.data = data
            # Migration only after a durable canonical state exists.
            try:
                os.unlink('attempts', dir_fd=self.fd)
                self.runtime.sync(self.fd)
            except FileNotFoundError:
                pass
        finally:
            os.close(temp)
            try:
                os.unlink(name, dir_fd=self.fd)
            except FileNotFoundError:
                pass

    def update(self, **changes):
        self.save(dict(self.data, **changes))


def valid_boot_id(value):
    return bool(re.fullmatch('[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', value))


def current_boot(runtime):
    value = runtime.text('/proc/sys/kernel/random/boot_id')
    if not valid_boot_id(value):
        raise RuntimeError('Invalid boot ID')
    return value


def enable_switch(runtime):
    try:
        data = trusted_bytes(runtime, BASE + '/enabled', 1)
    except FileNotFoundError:
        return False
    if data != b'':
        raise RuntimeError('Enabled switch must be a trusted empty file')
    return True


def deployment_pending(runtime):
    # Same shared flock as deployment. Presence (even corrupt/dangling) blocks all
    # custom loads; do not parse, rewrite or delete the worker's recovery record.
    parent = directory_fd(runtime, str(Path(DEPLOY_PENDING).parent))
    try:
        try:
            os.stat(Path(DEPLOY_PENDING).name, dir_fd=parent, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False
    finally:
        os.close(parent)


def verify_chain(runtime, deadline):
    adsp, parent = wait_for(runtime, lambda: locate_adsp(runtime), deadline, 20, 'ADSP running')
    wait_for(runtime, lambda: qrtr_ready(runtime, parent), deadline, 15, 'ADSP QRTR bound')
    wait_for(runtime, lambda: pdm_ready(runtime, parent), deadline, 10, 'ADSP PDM bound')
    return adsp, parent


def result_with_fallback(runtime, reason):
    recovery = fallback(runtime)
    return {'status': 'degraded' if recovery['status'] == 'live' else 'failed',
            'reason': reason[:500], 'fallback': recovery, 'locator_handshake_verified': False}


def lock_failure(error):
    return {'status': 'failed', 'reason': 'Shared lock unavailable: ' + str(error)[:400],
            'fallback': {'status': 'not_attempted', 'reason': 'No module changes without shared lock'},
            'locator_handshake_verified': False}


def activate(runtime):
    try:
        with SharedLock(runtime):
            return activate_locked(runtime)
    except ERRORS as error:
        return lock_failure(error)


def activate_locked(runtime):
    deadline = runtime.now() + START_BUDGET
    try:
        with StateStore(runtime) as store:
            if deployment_pending(runtime):
                return result_with_fallback(runtime, 'Deployment pending; explicit recover required; record untouched')
            boot = current_boot(runtime)
            same_boot = store.data['boot_id'] == boot
            try:
                # Environment and the entire file/live-identity group precede any custom load.
                validate_environment(runtime)
                if not enable_switch(runtime):
                    return result_with_fallback(runtime, 'Administratively disabled')
                if store.data['failures'] >= 2:
                    if not same_boot:
                        store.update(boot_id=boot, outcome='safe')
                    return result_with_fallback(runtime, 'Safe mode: two distinct boot failures')
                if same_boot and store.data['outcome'] != 'ready':
                    return result_with_fallback(runtime, 'Already attempted this boot; no retry')
                rows, files = validate_files(runtime, deadline)
                existing = {row['name']: check_loaded(runtime, row['name'], row['build_note']) for row in rows}
                for name in ('qcom_glink', 'qcom_sysmon', 'mdt_loader', 'qcom_pd_mapper', 'pmic_glink'):
                    check_loaded(runtime, name)
                if same_boot:
                    if not all(existing.values()):
                        raise RuntimeError('Same-boot success is stale: custom module missing')
                    verify_chain(runtime, deadline)
                    if not check_loaded(runtime, 'pmic_glink'):
                        raise RuntimeError('Same-boot success is stale: pmic_glink missing')
                else:
                    # fsync failure here forbids every ADSP load. Pending counts this boot once,
                    # including an uncatchable crash; subsequent same-boot calls cannot retry.
                    store.update(boot_id=boot, failures=min(2, store.data['failures'] + 1), outcome='pending')
                    for name in ('qcom_glink', 'qcom_sysmon', 'mdt_loader'):
                        standard_module(runtime, name, deadline)
                    for row in rows:
                        name = row['name']
                        if not existing[name]:
                            command(runtime, ['/usr/sbin/insmod', str(files[name])], deadline)
                            if not check_loaded(runtime, name, row['build_note']):
                                raise RuntimeError('Custom load did not publish expected identity: ' + name)
                        if name == 'cupid_qcom_q6v5_pas':
                            wait_for(runtime, lambda: locate_adsp(runtime), deadline, 20, 'ADSP running')
                    _, parent = wait_for(runtime, lambda: locate_adsp(runtime), deadline, 20, 'ADSP running')
                    wait_for(runtime, lambda: qrtr_ready(runtime, parent), deadline, 15, 'ADSP QRTR bound')
                    standard_module(runtime, 'qcom_pd_mapper', deadline)
                    wait_for(runtime, lambda: pdm_ready(runtime, parent), deadline, 10, 'ADSP PDM bound')
                    standard_module(runtime, 'pmic_glink', deadline)
                sample = wait_for(runtime, lambda: battery_sample(runtime), deadline, 25, 'Battery telemetry')
                remaining(runtime, deadline)
                store.update(boot_id=boot, failures=0, outcome='ready')
                return {'status': 'ready', 'kernel': KERNEL, 'battery': sample,
                        'same_boot': same_boot, 'locator_handshake_verified': False,
                        'evidence': 'ADSP running, QRTR/PDM bound, valid battery attributes only'}
            except ERRORS as error:
                reason = str(error)
                try:
                    if store.data['boot_id'] != boot or store.data['outcome'] == 'ready':
                        store.update(boot_id=boot, failures=min(2, store.data['failures'] + 1), outcome='failed')
                    elif store.data['outcome'] == 'pending':
                        store.update(outcome='failed')
                except ERRORS as state_error:
                    reason += '; state save failed: ' + str(state_error)
                return result_with_fallback(runtime, reason)
    except ERRORS as error:
        # Lock/storage/boot-id failures never reach custom insmod.
        return result_with_fallback(runtime, str(error))


def failure_hook(runtime, service_result):
    """ExecStopPost: act only on a failed start, including systemd timeout/SIGKILL."""
    if service_result == 'success':
        return {'status': 'skipped', 'reason': 'Normal service stop; no failure fallback'}
    try:
        with SharedLock(runtime):
            reason = 'Failure hook: ' + service_result
            try:
                with StateStore(runtime) as store:
                    boot = current_boot(runtime)
                    if store.data['boot_id'] == boot and store.data['outcome'] in ('pending', 'ready'):
                        store.update(outcome='failed', failures=max(1, store.data['failures']))
            except ERRORS as error:
                reason += '; state unavailable: ' + str(error)
            return result_with_fallback(runtime, reason)
    except ERRORS as error:
        return lock_failure(error)


def explicit_fallback(runtime):
    try:
        with SharedLock(runtime):
            return result_with_fallback(runtime, 'Explicit standard-module fallback')
    except ERRORS as error:
        return lock_failure(error)


def exit_status(result):
    return 0 if result['status'] in ('ready', 'skipped') else (2 if result['status'] == 'degraded' else 1)


def main():
    if os.geteuid() != 0:
        print(json.dumps({'status': 'failed', 'reason': 'Startup requires root'}), flush=True)
        return 1

    def terminate(signum, frame):
        # Ignore repeated TERM while executing the bounded userspace recovery path.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise Interrupted('SIGTERM interrupted startup')

    signal.signal(signal.SIGTERM, terminate)
    runtime = Runtime()
    if sys.argv[1:] == ['--failure-fallback']:
        result = failure_hook(runtime, os.environ.get('SERVICE_RESULT', 'unknown'))
    elif sys.argv[1:] == ['--fallback']:
        result = explicit_fallback(runtime)
    elif not sys.argv[1:]:
        result = activate(runtime)
    else:
        result = {'status': 'failed', 'reason': 'Usage: adsp-start.py [--fallback|--failure-fallback]'}
    print(json.dumps(result, sort_keys=True), flush=True)
    return exit_status(result)


if __name__ == '__main__':
    raise SystemExit(main())
