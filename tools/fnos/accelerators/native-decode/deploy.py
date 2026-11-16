#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Video-only file transactions. No flash, block writes, systemctl or modprobe.

Independent fixed trust + pinned lifecycle/package/applicable ABI evidence gates
validated staging, not final native task acceptance. Live observer and supervised
receipts are read-only; main owns device commands, host receipts and source pins.
prepare-only validates inputs and emits an explicitly unverified boot/DT plan.
"""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
import sys
import uuid

try:
    import fcntl
except ImportError:
    fcntl = None

KERNEL = '6.18.18.c1147-trim'
BOARD = 'xiaomi,cupid'
VERMAGIC = KERNEL + ' SMP mod_unload modversions aarch64'
BOOT_SIZE = 201326592
BASE = '/usr/local/lib/cupid/native-decode'
CONTROL = '/var/lib/cupid-native-decode/deploy'
STATE = CONTROL + '/state.json'
PENDING = CONTROL + '/pending.json'
LOCK = '/run/cupid-native-decode.lock'
UNIT = '/etc/systemd/system/cupid-native-decode.service'
SERVICE = b'''[Unit]
Description=Cupid native decode (manual admission only)
[Service]
Type=oneshot
ExecStart=/usr/local/lib/cupid/native-decode/native-decode-runner --manual-only
NoNewPrivileges=yes
'''
FILES = {
    'cupid_qcom_iris.ko': (BASE + '/cupid_qcom_iris.ko', 0o644),
    'cupid_videocc_sm8450.ko': (BASE + '/cupid_videocc_sm8450.ko', 0o644),
    'native-decode-runner': (BASE + '/native-decode-runner', 0o755),
    'cupid-native-decode.service': (UNIT, 0o644),
}
TARGETS = frozenset(target for target, _ in FILES.values())
HEX = re.compile(r'[0-9a-f]{64}\Z')
TX = re.compile(re.escape(CONTROL) + r'/tx-[0-9a-f]{32}\Z')
TX_FILE = re.compile(re.escape(CONTROL) + r'/tx-[0-9a-f]{32}/(?:before.json|state-before|result.json|file-[0-3])\Z')
LIMIT = 32 * 1024 * 1024


class Refusal(RuntimeError):
    pass


def require(condition, message):
    if not condition:
        raise Refusal(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def parse_json(data):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            require(key not in value, 'Duplicate JSON key: ' + key)
            value[key] = item
        return value
    try:
        return json.loads(data, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refusal('Nonfinite JSON')))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal('Malformed JSON') from exc


def shape(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys), 'Unexpected JSON fields')


def identity(value):
    require(type(value.get('schema')) is int and value['schema'] == 1 and
            (value.get('kernel'), value.get('board')) == (KERNEL, BOARD), 'Wrong schema/kernel/board')


def digest(value):
    require(isinstance(value, str) and HEX.fullmatch(value), 'Invalid SHA256')
    return value


def stable(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_uid, st.st_gid,
            st.st_nlink, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def checked_dir(st, private=False):
    require(stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and
            not st.st_mode & 0o022, 'Unsafe directory owner/mode/symlink')
    if private:
        require(stat.S_IMODE(st.st_mode) == 0o700, 'Backup/control directory must be 0700')


def read_fd(fd, limit=LIMIT):
    before = os.fstat(fd)
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and
            before.st_uid == os.geteuid() and not before.st_mode & 0o7022,
            'Unsafe file owner/mode/symlink/hardlink')
    require(before.st_size <= limit, 'Oversize file')
    with os.fdopen(os.dup(fd), 'rb') as stream:
        data = stream.read(limit + 1)
    require(len(data) == before.st_size and stable(before) == stable(os.fstat(fd)),
            'File changed during read')
    return {'data': data, 'mode': stat.S_IMODE(before.st_mode)}


def source_read(path, limit=LIMIT, private=False):
    """Read arbitrary SOURCE files only; never execute/import from a package.

    All components are no-follow. Sticky shared ancestors are allowed, but the
    containing directory and the source itself must be owned and non-writable.
    """
    path = Path(os.path.abspath(path))
    for parent in [*reversed(path.parents)]:
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode), 'Symlink source parent')
        require(not st.st_mode & 0o022 or bool(st.st_mode & stat.S_ISVTX), 'Writable source parent')
    checked_dir(path.parent.lstat(), private=private)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        value = read_fd(fd, limit)
        require(stable(os.fstat(fd)) == stable(path.lstat()), 'Source replaced during read')
        if private:
            require(value['mode'] == 0o600, 'Private manifest must be 0600')
        return value['data']
    finally:
        os.close(fd)


def elf_identity(data, module):
    try:
        require(len(data) >= 64 and data[:7] == b'\x7fELF\x02\x01\x01', 'Not ELF64 LE')
        h = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        require(h[2] == 183 and h[3] == 1 and h[8] == 64, 'Not AArch64 ELF')
        if not module:
            require(h[1] in (2, 3) and h[4] != 0 and h[9] == 56 and h[10] > 0 and
                    h[5] >= 64 and h[5] + h[9] * h[10] <= len(data), 'Invalid executable headers')
            executable = False
            for index in range(h[10]):
                p = struct.unpack_from('<IIQQQQQQ', data, h[5] + index * 56)
                require(p[2] + p[5] <= len(data) and p[5] <= p[6], 'Executable segment outside file')
                if p[0] == 1 and p[1] & 1 and p[3] <= h[4] < p[3] + p[6]:
                    require(not p[1] & 2, 'Writable executable segment')
                    executable = True
            require(executable, 'Entry point not in executable load segment')
            return None
        require(h[1] == 1 and h[12] > 0 and h[11] == 64 and h[13] < h[12] and
                h[6] >= 64 and h[6] + h[11] * h[12] <= len(data), 'Invalid module sections')
        sections = [struct.unpack_from('<IIQQQQIIQQ', data, h[6] + i * 64) for i in range(h[12])]
        def payload(section):
            start, length = section[4:6]
            require(start + length <= len(data), 'Module section outside file')
            return data[start:start + length]
        names = payload(sections[h[13]])
        found = {}
        for section in sections:
            start = section[0]
            end = names.find(b'\0', start)
            require(start < len(names) and end >= 0, 'Invalid section name')
            name = names[start:end]
            if name in (b'.modinfo', b'.note.gnu.build-id'):
                require(name not in found, 'Duplicate ELF identity section')
                found[name] = payload(section)
        info = found[b'.modinfo'].split(b'\0')
        def field(key):
            entries = [v[len(key) + 1:].decode('ascii') for v in info if v.startswith(key + b'=')]
            require(len(entries) == 1, 'Missing/duplicate modinfo')
            return entries[0]
        note = found[b'.note.gnu.build-id']
        ns, ds, kind = struct.unpack_from('<III', note)
        require(ns == 4 and ds > 0 and kind == 3 and note[12:16] == b'GNU\0' and
                len(note) == 12 + ((ns + 3) & ~3) + ((ds + 3) & ~3), 'Invalid GNU note')
        return field(b'name'), field(b'vermagic'), note.hex()
    except (struct.error, KeyError, UnicodeError) as exc:
        raise Refusal('Malformed ELF') from exc


def validate_package(stage, pin):
    digest(pin)
    stage = Path(os.path.abspath(stage))
    checked_dir(stage.lstat())
    expected = set(FILES) | {'package-manifest.json'}
    require({p.name for p in stage.iterdir()} == expected, 'Package fixed file set mismatch')
    payloads = {name: source_read(stage / name) for name in expected}
    require(sha(payloads['package-manifest.json']) == pin, 'External package pin mismatch')
    manifest = parse_json(payloads['package-manifest.json'])
    shape(manifest, ('schema', 'kernel', 'board', 'files'))
    identity(manifest)
    require(isinstance(manifest['files'], list) and len(manifest['files']) == len(FILES), 'File count mismatch')
    seen = set()
    for record in manifest['files']:
        shape(record, ('file', 'target', 'mode', 'sha256', 'elf'))
        name = record['file']
        require(isinstance(name, str) and name in FILES and name not in seen, 'Unknown/duplicate asset')
        seen.add(name)
        target, mode = FILES[name]
        require(record['target'] == target and type(record['mode']) is int and record['mode'] == mode,
                'Forbidden target/mode')
        require(digest(record['sha256']) == sha(payloads[name]), 'Asset hash mismatch')
        if name.endswith('.ko'):
            actual = elf_identity(payloads[name], True)
            shape(record['elf'], ('name', 'vermagic', 'build_note'))
            require(actual == (name[:-3], VERMAGIC, record['elf']['build_note']) and
                    record['elf']['name'] == actual[0] and record['elf']['vermagic'] == VERMAGIC,
                    'Module ELF/vermagic/raw GNU note mismatch')
        elif name == 'native-decode-runner':
            require(record['elf'] == {'machine': 'aarch64', 'kind': 'executable'}, 'Executable record mismatch')
            elf_identity(payloads[name], False)
        else:
            require(record['elf'] is None and payloads[name] == SERVICE, 'Unaudited service/install side effects')
    return {target: {'data': payloads[name], 'mode': mode} for name, (target, mode) in FILES.items()}


def validate_firmware(path, pin):
    """Private source provenance only. Firmware is never a package/target asset."""
    digest(pin)
    path = Path(os.path.abspath(path))
    require('private' in path.parts, 'Firmware manifest must live under private/')
    raw = source_read(path, 65536, private=True)
    require(sha(raw) == pin, 'Private firmware manifest pin mismatch')
    value = parse_json(raw)
    shape(value, ('schema', 'kernel', 'board', 'firmware'))
    identity(value)
    shape(value['firmware'], ('path', 'sha256'))
    firmware = value['firmware']
    require(isinstance(firmware['path'], str) and Path(firmware['path']).is_absolute() and
            'private' in Path(firmware['path']).parts, 'Firmware source must be private absolute path')
    require(sha(source_read(firmware['path'])) == digest(firmware['sha256']), 'Firmware source hash mismatch')
    # Do not output path, hash or bytes to public plans/logs.


def signature(item):
    return None if item is None else {'sha256': sha(item['data']), 'mode': item['mode']}


def signatures(items):
    return {name: signature(item) for name, item in items.items()}


def validate_signature(value):
    if value is not None:
        shape(value, ('sha256', 'mode'))
        digest(value['sha256'])
        require(type(value['mode']) is int and value['mode'] >= 0 and
                not value['mode'] & ~0o777 and not value['mode'] & 0o022, 'Unsafe recorded mode')


def validate_map(value):
    shape(value, TARGETS)
    for item in value.values():
        validate_signature(item)


class Filesystem:
    """Linux dirfd/openat traversal: no absolute redirection or symlink races.

    Reads/writes are restricted to the four assets and private transaction files.
    No caller-provided block path can reach an open-for-write operation.
    """
    def __init__(self, root='/'):
        require(fcntl is not None and os.name == 'posix', 'Linux filesystem/flock required')
        self.root = Path(os.path.abspath(root))
        for component in [self.root, *self.root.parents]:
            require(not component.is_symlink(), 'Symlink fake/device root')
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            checked_dir(os.fstat(self.fd))
        except BaseException:
            os.close(self.fd)
            raise

    def close(self):
        os.close(self.fd)

    @staticmethod
    def allowed(name):
        require(isinstance(name, str) and str(PurePosixPath(name)) == name and
                name.startswith('/') and '..' not in PurePosixPath(name).parts and
                (name in TARGETS | {STATE, PENDING, LOCK} or TX_FILE.fullmatch(name)), 'Forbidden file path')

    @contextlib.contextmanager
    def parent(self, name, create=False):
        self.allowed(name)
        fd = os.dup(self.fd)
        current = ''
        try:
            checked_dir(os.fstat(fd))
            for part in PurePosixPath(name).parts[1:-1]:
                current += '/' + part
                try:
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                except FileNotFoundError:
                    if not create:
                        yield None
                        return
                    os.mkdir(part, 0o700 if current.startswith(CONTROL) else 0o755, dir_fd=fd)
                    os.fsync(fd)
                    next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    checked_dir(os.fstat(next_fd), private=current.startswith(CONTROL))
                except BaseException:
                    os.close(next_fd)
                    raise
                os.close(fd)
                fd = next_fd
            yield fd
        finally:
            os.close(fd)

    def read(self, name):
        with self.parent(name) as parent:
            if parent is None:
                return None
            leaf = PurePosixPath(name).name
            try:
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            except FileNotFoundError:
                return None
            try:
                value = read_fd(fd)
                require(stable(os.fstat(fd)) == stable(os.stat(leaf, dir_fd=parent, follow_symlinks=False)),
                        'Target replaced during read')
                if name not in TARGETS:
                    require(value['mode'] == 0o600, 'State/backup/lock file must be 0600')
                return value
            finally:
                os.close(fd)

    def put(self, name, item, expected):
        """CAS before rename/unlink, fsync file+directory, exact readback."""
        self.allowed(name)
        if item is not None:
            shape(item, ('data', 'mode'))
            require(isinstance(item['data'], bytes), 'Payload must be bytes')
            validate_signature(signature(item))
            require(name in TARGETS or item['mode'] == 0o600, 'Private state/backup mode required')
        require(self.read(name) == expected, 'Target conflict before write: ' + name)
        if item is None and expected is None:
            return
        with self.parent(name, create=True) as parent:
            leaf = PurePosixPath(name).name
            tmp = '.' + leaf + '.transaction-' + uuid.uuid4().hex
            made = False
            try:
                if item is not None:
                    validate_signature(signature(item))
                    require(len(item['data']) <= LIMIT, 'Oversize write')
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                    made = True
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(item['data'])
                        os.fchmod(stream.fileno(), item['mode'])
                        stream.flush()
                        os.fsync(stream.fileno())
                    fd = os.open(tmp, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        require(read_fd(fd) == item, 'Temporary readback mismatch')
                    finally:
                        os.close(fd)
                require(self.read(name) == expected, 'Target conflict immediately before replace: ' + name)
                if item is None:
                    os.unlink(leaf, dir_fd=parent)
                else:
                    os.replace(tmp, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                    made = False
                os.fsync(parent)
                require(self.read(name) == item, 'Installed readback mismatch')
            finally:
                if made:
                    os.unlink(tmp, dir_fd=parent)
                    os.fsync(parent)

    @contextlib.contextmanager
    def lock(self):
        with self.parent(LOCK, create=True) as parent:
            leaf = PurePosixPath(LOCK).name
            fd = os.open(leaf, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent)
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid() and
                        stat.S_IMODE(st.st_mode) == 0o600, 'Unsafe local lock')
                os.fsync(parent)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Refusal('Video startup/deployment lock held') from exc
                yield
            finally:
                os.close(fd)


PRODUCERS = {kind: '/usr/local/lib/cupid/native-decode-admission/' + kind + '-producer'
             for kind in ('lifecycle', 'package', 'abi')}
LIVE = '/run/cupid-native-decode-admission/live.json'
LIFECYCLE_CONTRACTS = frozenset(('remove', 'unbind', 'irq-work-drain', 'dma-lifetime',
                                  'provider-module-lifetime', 'normal-recovery'))


def private_path(path):
    require(isinstance(path, str) and Path(path).is_absolute() and
            str(Path(path)) == path and '..' not in Path(path).parts and
            'private' in Path(path).parts, 'Expected private absolute source path')
    return path


def pinned_private_json(path, pin):
    raw = source_read(private_path(path), 1024 * 1024, private=True)
    require(sha(raw) == digest(pin), 'Independent evidence pin mismatch')
    return parse_json(raw)


def stream_private_hash(record, exact_size=None):
    shape(record, ('path', 'sha256', 'size'))
    path = private_path(record['path'])
    digest(record['sha256'])
    require(type(record['size']) is int and record['size'] > 0 and
            (exact_size is None or record['size'] == exact_size), 'Backup length mismatch')
    # Validate ancestors and private permissions before bounded streaming; never
    # mmap/read a 192 MiB boot image into memory and never open a block device.
    for parent in reversed(Path(path).parents):
        st = parent.lstat()
        require(stat.S_ISDIR(st.st_mode) and
                (not st.st_mode & 0o022 or bool(st.st_mode & stat.S_ISVTX)), 'Unsafe backup parent')
    checked_dir(Path(path).parent.lstat(), private=True)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        require(stat.S_ISREG(st.st_mode) and st.st_nlink == 1 and st.st_uid == os.geteuid() and
                stat.S_IMODE(st.st_mode) == 0o600 and st.st_size == record['size'], 'Unsafe/incomplete backup')
        h = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
        require(total == record['size'] and stable(st) == stable(os.fstat(fd)) == stable(Path(path).lstat()) and
                h.hexdigest() == record['sha256'], 'Backup changed/hash mismatch')
        return h.hexdigest()
    finally:
        os.close(fd)


class AdmissionReader:
    """Independent pinned private reports; never execute/import a producer.

    Producer code hashes and accepted report hashes are separate operator inputs,
    NOT fields obtained from the package. A hash authenticates only when supplied
    through that independent reviewed channel; package authors cannot set them.
    Stage admission proves deployability contracts, not final native task success.
    """
    def __init__(self, path, pin, producer_pins, evidence_pins, producer_read=None):
        self.path, self.pin = path, pin
        shape(producer_pins, PRODUCERS)
        shape(evidence_pins, PRODUCERS)
        self.producer_pins = {k: digest(v) for k, v in producer_pins.items()}
        self.evidence_pins = {k: digest(v) for k, v in evidence_pins.items()}
        self.producer_read = producer_read or source_read

    def read(self, package_pin):
        value = pinned_private_json(self.path, self.pin)
        shape(value, ('schema', 'kernel', 'board', 'phase', 'package_sha256',
                      'firmware_manifest_sha256', 'evidence', 'backups', 'root_snapshots'))
        identity(value)
        require(value['phase'] == 'validated-staging' and value['package_sha256'] == digest(package_pin),
                'Wrong stage/package admission binding')
        digest(value['firmware_manifest_sha256'])
        shape(value['evidence'], PRODUCERS)
        paths = set()
        for kind, producer in PRODUCERS.items():
            require(sha(self.producer_read(producer)) == self.producer_pins[kind], 'Untrusted fixed producer: ' + kind)
            record = value['evidence'][kind]
            shape(record, ('path', 'sha256'))
            require(record['sha256'] == self.evidence_pins[kind] and record['path'] not in paths,
                    'Evidence must be independently pinned and distinct')
            paths.add(record['path'])
            report = pinned_private_json(record['path'], self.evidence_pins[kind])
            shape(report, ('schema', 'kernel', 'board', 'kind', 'producer_sha256',
                           'package_sha256', 'firmware_manifest_sha256', 'result', 'details'))
            identity(report)
            require(report['kind'] == kind and report['producer_sha256'] == self.producer_pins[kind] and
                    report['package_sha256'] == package_pin and
                    report['firmware_manifest_sha256'] == value['firmware_manifest_sha256'] and
                    report['result'] == 'verified-for-staging', 'Evidence provenance/result mismatch')
            if kind == 'lifecycle':
                shape(report['details'], LIFECYCLE_CONTRACTS)
                require(all(v == 'verified' for v in report['details'].values()), 'Incomplete lifecycle contracts')
            elif kind == 'abi':
                shape(report['details'], ('coverage', 'unknown', 'mismatches'))
                require(report['details']['coverage'] == 'complete-applicable' and
                        type(report['details']['unknown']) is int and report['details']['unknown'] == 0 and
                        type(report['details']['mismatches']) is int and report['details']['mismatches'] == 0,
                        'Applicable ABI incomplete/unknown/mismatched')
            else:
                shape(report['details'], ('assets', 'runner_audit', 'service_audit'))
                validate_map(report['details']['assets'])
                require(all(x is not None for x in report['details']['assets'].values()) and
                        report['details']['runner_audit'] == report['details']['service_audit'] == 'verified',
                        'Package executable/service audit missing')
                value['accepted_assets'] = report['details']['assets']
        shape(value['backups'], ('device_boot', 'host_receipt', 'recovery'))
        require(isinstance(value['root_snapshots'], list), 'Missing independent root snapshot inventory')
        return value


class ReadOnlyLiveObserver:
    """Device reads only. Main owns serial/mutex, bootloader and live producer.

    Fixed private /run report must be freshly regenerated by the trusted
    lifecycle producer on the current boot and external main nonce. No commands,
    partition opens, boolean self-attestation, or stale admission as live proof.
    """
    def __init__(self, fs, reader, nonce):
        self.fs, self.reader, self.nonce = fs, reader, nonce
        require(isinstance(nonce, str) and re.fullmatch(r'[0-9a-f]{32}', nonce), 'Independent live nonce required')

    def observe(self, package_pin):
        import time
        require(os.uname().release == KERNEL, 'Actual running kernel mismatch')
        def proc(path, limit):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                data = os.read(fd, limit + 1)
                require(len(data) <= limit, 'Oversize live identity')
                return data
            finally:
                os.close(fd)
        require(BOARD.encode() in proc('/proc/device-tree/compatible', 4096).split(b'\0'), 'Actual running board mismatch')
        boot_id = proc('/proc/sys/kernel/random/boot_id', 128).decode('ascii').strip()
        live = parse_json(source_read(LIVE, 65536, private=True))
        shape(live, ('schema', 'kernel', 'board', 'boot_id', 'nonce', 'issued_at', 'expires_at',
                     'producer_sha256', 'package_sha256', 'slot', 'boot_sha256', 'boot_size',
                     'battery_percent', 'runtime', 'authentication', 'iommu', 'recovery', 'assets'))
        identity(live)
        now = time.time()
        require(type(live['issued_at']) in (int, float) and type(live['expires_at']) in (int, float) and
                0 <= now - live['issued_at'] <= 15 and now < live['expires_at'] <= live['issued_at'] + 15,
                'Stale/future live observation')
        require(live['boot_id'] == boot_id and live['nonce'] == self.nonce and
                live['producer_sha256'] == self.reader.producer_pins['lifecycle'] and
                live['package_sha256'] == package_pin, 'Wrong live boot/nonce/producer/package')
        require(live['slot'] in ('boot_a', 'boot_b') and type(live['boot_size']) is int and
                live['boot_size'] == BOOT_SIZE and type(live['battery_percent']) is int and
                40 <= live['battery_percent'] <= 100, 'Live slot/boot/battery prerequisite failed')
        require(live['runtime'] == 'inactive-unbound-no-enablement-or-overrides' and
                live['authentication'] == 'secure-enforced' and live['iommu'] == 'translation-enforced' and
                live['recovery'] == 'independently-verified-usable', 'Runtime/security/recovery state unknown')
        digest(live['boot_sha256'])
        validate_map(live['assets'])
        actual = {name: self.fs.read(name) for name in sorted(TARGETS)}
        require(signatures(actual) == live['assets'], 'Live target hash observation stale/changed')
        return live


TRUST = '/etc/cupid-native-decode-admission/trust.json'


def read_trust():
    # Installed by the independent main coordinator; never read from a package
    # or accept arbitrary CLI producer/source pins as the trust anchor.
    value = parse_json(source_read(TRUST, 65536, private=True))
    shape(value, ('schema', 'kernel', 'board', 'observer_sha256', 'producer_pins',
                  'evidence_pins', 'admission_sha256', 'receipt_sha256', 'nonce'))
    identity(value)
    require(sha(source_read(str(Path(__file__).absolute()))) == digest(value['observer_sha256']),
            'Fixed observer source pin mismatch')
    shape(value['producer_pins'], PRODUCERS)
    shape(value['evidence_pins'], PRODUCERS)
    for key in ('producer_pins', 'evidence_pins'):
        for pin in value[key].values():
            digest(pin)
    digest(value['admission_sha256'])
    digest(value['receipt_sha256'])
    require(isinstance(value['nonce'], str) and re.fullmatch(r'[0-9a-f]{32}', value['nonce']), 'Trust nonce invalid')
    return value


def fresh_observation(value):
    import time
    now = time.time()
    require(type(value['issued_at']) in (int, float) and type(value['expires_at']) in (int, float) and
            0 <= now - value['issued_at'] <= 15 and now < value['expires_at'] <= value['issued_at'] + 15,
            'Stale/future supervised observation')


class ProductionAdmission:
    def __init__(self, reader=None, observer=None, trust=None):
        self.reader, self.observer, self.trust = reader, observer, trust
        self.value = None
        self.package_pin = None

    def require_final(self, package_pin):
        # Historical engine method name; deployability-for-staging only, never
        # final native task acceptance. Fixed source trust is checked every time.
        require(self.reader is not None and self.observer is not None and self.trust is not None,
                'Independent fixed trust/admission/observer required')
        require(read_trust() == self.trust, 'Installed trust changed during transaction')
        self.value = self.reader.read(digest(package_pin))
        self.package_pin = package_pin

    def verify_live(self):
        require(self.value is not None, 'Admission not read')
        self.require_final(self.package_pin)
        live = self.observer.observe(self.package_pin)
        backups = self.value['backups']
        shape(backups, ('device_boot', 'host_receipt', 'recovery'))
        shape(backups['host_receipt'], ('path', 'sha256'))
        require(backups['host_receipt']['sha256'] == self.trust['receipt_sha256'], 'Untrusted host receipt pin')
        receipt = pinned_private_json(backups['host_receipt']['path'], self.trust['receipt_sha256'])
        shape(receipt, ('schema', 'kernel', 'board', 'producer_sha256', 'package_sha256',
                        'boot_id', 'nonce', 'issued_at', 'expires_at', 'slot', 'boot_size',
                        'boot_sha256', 'device_before_sha256', 'device_after_sha256',
                        'host_boot_sha256', 'host_boot_size', 'recovery_sha256', 'recovery_size'))
        identity(receipt)
        fresh_observation(receipt)
        require(receipt['producer_sha256'] == self.reader.producer_pins['lifecycle'] and
                receipt['package_sha256'] == self.package_pin and receipt['boot_id'] == live['boot_id'] and
                receipt['nonce'] == live['nonce'] == self.trust['nonce'] and receipt['slot'] == live['slot'],
                'Receipt not from current supervised boot/slot/producer')
        for key in ('boot_size', 'host_boot_size'):
            require(type(receipt[key]) is int and receipt[key] == BOOT_SIZE, 'Full current-slot receipt length mismatch')
        current = digest(live['boot_sha256'])
        require(all(digest(receipt[key]) == current for key in
                    ('boot_sha256', 'device_before_sha256', 'device_after_sha256', 'host_boot_sha256')),
                'Supervised current/host/device-before-after hashes differ')
        # Only the local DEVICE backup is streamed. Host backup/recovery remain
        # on the computer, authenticated by independently pinned main receipt.
        require(stream_private_hash(backups['device_boot'], BOOT_SIZE) == current, 'Actual device full backup mismatch')
        shape(backups['recovery'], ('sha256', 'size'))
        require(digest(backups['recovery']['sha256']) == digest(receipt['recovery_sha256']) and
                type(backups['recovery']['size']) is int and backups['recovery']['size'] > 0 and
                type(receipt['recovery_size']) is int and backups['recovery']['size'] == receipt['recovery_size'],
                'Pinned recovery input hash/size mismatch')
        # Hashing the full local backup may consume the observation window;
        # never authorize a write using a receipt that expired during streaming.
        fresh_observation(receipt)
        require(self.observer.observe(self.package_pin) == live, 'Live observation changed during backup verification')

    def snapshot_record(self, before, state):
        for record in self.value['root_snapshots']:
            shape(record, ('before', 'state', 'files', 'state_file'))
            validate_map(record['before'])
            validate_signature(record['state'])
            if record['before'] == signatures(before) and record['state'] == signature(state):
                return record
        raise Refusal('Main must provide an independently pinned snapshot of CURRENT root assets/state')

    def save_host_snapshot(self, tx, before, state):
        # Main supplies copied small host snapshot assets; never synthesize an
        # independent endpoint from device bytes or access a computer path.
        require(TX.fullmatch(tx), 'Invalid transaction ID')
        self.verify_host_snapshot(tx, before, state)

    def verify_host_snapshot(self, tx, before, state):
        require(TX.fullmatch(tx), 'Invalid transaction ID')
        record = self.snapshot_record(before, state)
        shape(record['files'], TARGETS)
        for name in sorted(TARGETS):
            path = record['files'][name]
            if before[name] is None:
                require(path is None, 'Unexpected host snapshot asset')
            else:
                raw = source_read(private_path(path), private=True)
                require(sha(raw) == sha(before[name]['data']), 'Independent current root snapshot hash mismatch')
        if state is None:
            require(record['state_file'] is None, 'Unexpected host state snapshot')
        else:
            require(source_read(private_path(record['state_file']), private=True) == state['data'], 'Host state snapshot mismatch')


class Deployment:
    def __init__(self, fs, admission):
        self.fs, self.admission = fs, admission

    def snapshot(self):
        return {name: self.fs.read(name) for name in sorted(TARGETS)}

    def verify(self, expected):
        require(signatures(self.snapshot()) == expected, 'Current asset drift; no writes')

    def state(self):
        item = self.fs.read(STATE)
        if item is None:
            return None
        value = parse_json(item['data'])
        shape(value, ('schema', 'kernel', 'board', 'tx', 'package', 'installed'))
        identity(value)
        digest(value['package'])
        require(isinstance(value['tx'], str) and TX.fullmatch(value['tx']), 'Invalid snapshot location')
        validate_map(value['installed'])
        return value

    def save_backup(self, before, state):
        tx = CONTROL + '/tx-' + uuid.uuid4().hex
        # Host endpoint is the admitted observer's independently stored copy.
        # Python deployment only writes its local private device snapshot.
        self.admission.save_host_snapshot(tx, before, state)
        for i, name in enumerate(sorted(TARGETS)):
            if before[name] is not None:
                self.fs.put(tx + '/file-' + str(i), {'data': before[name]['data'], 'mode': 0o600}, None)
        if state is not None:
            self.fs.put(tx + '/state-before', state, None)
        metadata = {'schema': 1, 'before': signatures(before), 'state': signature(state)}
        self.fs.put(tx + '/before.json', {'data': json_bytes(metadata), 'mode': 0o600}, None)
        self.load_backup(tx)
        self.verify(signatures(before))
        require(self.fs.read(STATE) == state, 'State drift during snapshot')
        return tx

    def load_backup(self, tx, metadata_pin=None):
        require(isinstance(tx, str) and TX.fullmatch(tx), 'Forbidden backup path')
        item = self.fs.read(tx + '/before.json')
        require(item is not None and (metadata_pin is None or sha(item['data']) == metadata_pin), 'Backup metadata missing/changed')
        meta = parse_json(item['data'])
        shape(meta, ('schema', 'before', 'state'))
        require(type(meta['schema']) is int and meta['schema'] == 1, 'Backup schema mismatch')
        validate_map(meta['before'])
        validate_signature(meta['state'])
        before = {}
        for i, name in enumerate(sorted(TARGETS)):
            item = self.fs.read(tx + '/file-' + str(i))
            expected = meta['before'][name]
            if expected is None:
                require(item is None, 'Unexpected snapshot file')
                before[name] = None
            else:
                require(item is not None and sha(item['data']) == expected['sha256'], 'Snapshot hash mismatch')
                before[name] = {'data': item['data'], 'mode': expected['mode']}
        state = self.fs.read(tx + '/state-before')
        require(signature(state) == meta['state'], 'State snapshot mismatch')
        self.admission.verify_host_snapshot(tx, before, state)
        return before, state

    def pending(self):
        item = self.fs.read(PENDING)
        require(item is not None, 'No pending transaction')
        value = parse_json(item['data'])
        shape(value, ('schema', 'kernel', 'board', 'operation', 'package', 'tx', 'metadata_sha256', 'after', 'state_after'))
        identity(value)
        require(value['operation'] in ('apply', 'rollback'), 'Unknown operation')
        digest(value['package'])
        digest(value['metadata_sha256'])
        validate_map(value['after'])
        validate_signature(value['state_after'])
        before, state = self.load_backup(value['tx'], value['metadata_sha256'])
        return value, before, state

    def guarded_write(self, name, item, expected, tx, metadata_pin, working, state_expected, current_backup=None):
        # Live observer, never old manifest as evidence; both endpoints rehashed.
        self.admission.verify_live()
        self.load_backup(tx, metadata_pin)
        if current_backup is not None:
            self.load_backup(current_backup)
        self.verify(signatures(working))
        require(self.fs.read(STATE) == state_expected, 'Current state conflict')
        self.fs.put(name, item, expected)

    def recover_locked(self):
        pending, before, old_state = self.pending()
        self.admission.require_final(pending['package'])
        self.admission.verify_live()
        current = self.snapshot()
        for name in TARGETS:
            require(signature(current[name]) in (signature(before[name]), pending['after'][name]),
                    'Recovery conflict; foreign content retained: ' + name)
        state = self.fs.read(STATE)
        require(signature(state) in (signature(old_state), pending['state_after']), 'Recovery state conflict')
        # Snapshot the ACTUAL partially installed state at both endpoints before
        # the first recovery write; an old baseline is not its current backup.
        current_backup = self.save_backup(current, state)
        for name in sorted(TARGETS, reverse=True):
            if current[name] != before[name]:
                self.guarded_write(name, before[name], current[name], pending['tx'],
                                   pending['metadata_sha256'], current, state, current_backup)
                current[name] = before[name]
        self.guarded_write(STATE, old_state, state, pending['tx'], pending['metadata_sha256'], current, state, current_backup)
        self.verify(signatures(before))
        require(self.fs.read(STATE) == old_state, 'Recovery state readback mismatch')
        result = pending['tx'] + '/result.json'
        self.fs.put(result, {'data': json_bytes({'status': 'restored-before', 'operation': pending['operation']}), 'mode': 0o600}, self.fs.read(result))
        self.fs.put(PENDING, None, self.fs.read(PENDING))

    def recover(self):
        # Require admission before lock creation. No partial recovery when unknown.
        pending, _, _ = self.pending()
        self.admission.require_final(pending['package'])
        self.admission.verify_live()
        with self.fs.lock():
            self.recover_locked()

    def transaction(self, before, after, operation, package_pin, new_state_factory):
        old_state = self.fs.read(STATE)
        tx = self.save_backup(before, old_state)
        new_state = new_state_factory(tx)
        metadata_pin = sha(self.fs.read(tx + '/before.json')['data'])
        pending = {'schema': 1, 'kernel': KERNEL, 'board': BOARD, 'operation': operation,
                   'package': package_pin, 'tx': tx, 'metadata_sha256': metadata_pin,
                   'after': signatures(after), 'state_after': signature(new_state)}
        require(self.fs.read(PENDING) is None, 'Pending transaction already exists')
        working = dict(before)
        try:
            self.fs.put(PENDING, {'data': json_bytes(pending), 'mode': 0o600}, None)
            for name in sorted(TARGETS):
                if before[name] != after[name]:
                    self.guarded_write(name, after[name], before[name], tx, metadata_pin, working, old_state)
                    working[name] = after[name]
            self.guarded_write(STATE, new_state, old_state, tx, metadata_pin, working, old_state)
            self.verify(signatures(after))
            self.fs.put(tx + '/result.json', {'data': json_bytes({'status': 'file-transaction-committed', 'operation': operation}), 'mode': 0o600}, None)
            self.fs.put(PENDING, None, self.fs.read(PENDING))
        except Exception as exc:
            # SIGKILL/KeyboardInterrupt deliberately leave durable pending evidence.
            # Recovery never overwrites bytes other than verified before/after.
            if self.fs.read(PENDING) is not None:
                try:
                    self.recover_locked()
                except BaseException as recovery:
                    raise Refusal('Transaction failed; recovery incomplete; retain pending/backups: ' + str(recovery)) from exc
            raise Refusal('Transaction failed; before state restored where a journal existed; snapshots retained') from exc
        return tx

    def apply(self, payloads, package_pin):
        digest(package_pin)
        shape(payloads, TARGETS)
        self.admission.require_final(package_pin)
        self.admission.verify_live()
        # Reject unsafe/drifting targets before even creating the runtime lock.
        require(self.fs.read(PENDING) is None, 'Interrupted transaction; recover first')
        require(self.state() is None, 'Existing installation: explicitly rollback before another apply')
        self.snapshot()
        with self.fs.lock():
            self.admission.verify_live()
            require(self.fs.read(PENDING) is None, 'Interrupted transaction; recover first')
            require(self.state() is None, 'Existing installation: explicitly rollback before another apply')
            before = self.snapshot()
            def state_factory(tx):
                return {'data': json_bytes({'schema': 1, 'kernel': KERNEL, 'board': BOARD,
                                            'tx': tx, 'package': package_pin,
                                            'installed': signatures(payloads)}), 'mode': 0o600}
            return self.transaction(before, payloads, 'apply', package_pin, state_factory)

    def rollback(self, package_pin):
        digest(package_pin)
        self.admission.require_final(package_pin)
        self.admission.verify_live()
        with self.fs.lock():
            require(self.fs.read(PENDING) is None, 'Interrupted transaction; recover first')
            record = self.state()
            require(record is not None, 'No actual installed snapshot; unmanaged rollback refused')
            require(record['package'] == package_pin, 'Rollback package admission binding mismatch')
            self.verify(record['installed'])
            after, _ = self.load_backup(record['tx'])
            return self.transaction(self.snapshot(), after, 'rollback', package_pin, lambda _: None)


def prepare_only(stage, pin, firmware_manifest, firmware_pin):
    validate_package(stage, pin)
    validate_firmware(firmware_manifest, firmware_pin)
    return {'schema': 1, 'kernel': KERNEL, 'board': BOARD, 'mode': 'prepare-only',
            'package_sha256': pin, 'assets': sorted(TARGETS),
            'input_validation': 'hash/ELF/fixed-service verified; NOT runtime acceptance',
            'installation_admission': 'not evaluated by prepare-only; independent staging evidence required',
            'boot_dt_plan': {'operation': 'external authorized coordinator only; Python never flashes',
                             'candidate': 'reviewed boot image containing reviewed DT; no image generated here',
                             'allowed_slots': ['boot_a', 'boot_b'], 'slot_selection': 'live bootloader observation required',
                             'required_current_backup_bytes': BOOT_SIZE,
                             'prerequisites': ['live current boot + host/device full backup hashes match',
                                               'recovery image available and independently verified usable',
                                               'modified root assets backed up at both endpoints',
                                               'battery >=40%; single host serial/operator mutex',
                                               'independent lifecycle/package/complete applicable ABI staging admission'],
                             'prerequisites_status': 'NOT observed / NOT verified'},
            'activation': 'manual only after admission; never enable/start/modprobe; no auth/IOMMU bypass'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    for action in ('prepare-only', 'apply'):
        p = sub.add_parser(action)
        p.add_argument('stage')
        p.add_argument('package_sha256')
        p.add_argument('--firmware-manifest', required=True)
        p.add_argument('--firmware-manifest-sha256', required=True)
        if action == 'apply':
            p.add_argument('--admission', required=True)
    for action in ('rollback', 'recover'):
        p = sub.add_parser(action)
        p.add_argument('package_sha256')
        p.add_argument('--admission', required=True)
    args = parser.parse_args(argv)
    fs = None
    try:
        if args.action in ('prepare-only', 'apply'):
            plan = prepare_only(args.stage, args.package_sha256, args.firmware_manifest, args.firmware_manifest_sha256)
            if args.action == 'prepare-only':
                print(json.dumps(plan, indent=2, sort_keys=True))
                return 0
        require(os.name == 'posix' and os.geteuid() == 0, 'Production Linux root required')
        trust = read_trust()
        reader = AdmissionReader(args.admission, trust['admission_sha256'],
                                 trust['producer_pins'], trust['evidence_pins'])
        checked = reader.read(args.package_sha256)
        payloads = None
        if args.action == 'apply':
            require(checked['firmware_manifest_sha256'] == args.firmware_manifest_sha256,
                    'Independent firmware admission binding mismatch')
            payloads = validate_package(args.stage, args.package_sha256)
            require(signatures(payloads) == checked['accepted_assets'], 'Independent package asset hashes mismatch')
        fs = Filesystem()
        admission = ProductionAdmission(reader, ReadOnlyLiveObserver(fs, reader, trust['nonce']), trust)
        admission.require_final(args.package_sha256)
        admission.verify_live()
        deploy = Deployment(fs, admission)
        if args.action == 'apply':
            tx = deploy.apply(payloads, args.package_sha256)
        elif args.action == 'rollback':
            tx = deploy.rollback(args.package_sha256)
        else:
            deploy.recover()
            tx = 'retained pending snapshot'
        print('FILE_TRANSACTION_OK: ' + tx + '; no native task acceptance/start/enable/module/boot operations')
        return 0
    except (OSError, RuntimeError, TypeError, KeyError) as exc:
        print('REFUSED: ' + str(exc), file=sys.stderr)
        return 1
    finally:
        if fs is not None:
            fs.close()


if __name__ == '__main__':
    sys.exit(main())
