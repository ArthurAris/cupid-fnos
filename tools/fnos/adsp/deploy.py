#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pinned, file-only ADSP deployment. Never starts/stops or unloads ADSP.

Production CLI requires root and the exact board/kernel. A running unit is a
hard refusal: authorize a separate normal systemctl stop before deploying.
Startup and this tool must hold /run/cupid-adsp.lock. Startup counters under
/var/lib/cupid-adsp are deliberately not deployment state and are never removed.
Backups and transaction journals are retained (0700/0600); no identities stored.
"""
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct
import subprocess
import sys
import uuid

KERNEL = '6.18.18.c1147-trim'
BOARD = 'xiaomi,cupid'
RELEASE = 'cupid-fnos-v1'
VERMAGIC = KERNEL + ' SMP mod_unload modversions aarch64'
BASE = '/usr/local/lib/cupid/adsp'
STATE = '/usr/local/lib/cupid/adsp-deploy-state.json'
PENDING = '/usr/local/lib/cupid/adsp-deploy-pending.json'
LOCK = '/run/cupid-adsp.lock'
UNIT = '/etc/systemd/system/cupid-adsp.service'
WANTS = '/etc/systemd/system/multi-user.target.wants/cupid-adsp.service'
SERVICE = 'cupid-adsp.service'
MODULES = (
    'cupid_hwspinlock_core', 'cupid_hwspinlock_qcom', 'cupid_qcom_smem',
    'cupid_qcom_socinfo', 'cupid_qcom_smp2p', 'cupid_qcom_glink_smem',
    'cupid_qcom_pil_info', 'cupid_qcom_rproc_common', 'cupid_qcom_q6v5_pas',
    'cupid_qrtr_smd',
)
FILES = {name + '.ko': (BASE + '/' + name + '.ko', 0o644) for name in MODULES}
FILES.update({
    'start.sh': (BASE + '/start.sh', 0o755),
    'adsp-start.py': (BASE + '/adsp-start.py', 0o755),
    'deploy.py': (BASE + '/deploy.py', 0o755),
    'module-manifest.json': (BASE + '/module-manifest.json', 0o644),
    'enabled': (BASE + '/enabled', 0o644),
    'cupid-adsp.service': (UNIT, 0o644),
    'cupid-adsp-blacklist.conf': ('/etc/modprobe.d/cupid-adsp-blacklist.conf', 0o644),
})
TARGETS = {target for target, _mode in FILES.values()}
HEX = re.compile(r'[0-9a-f]{64}\Z')
BACKUP = re.compile(r'/usr/local/lib/cupid/adsp-deploy-before-[0-9a-f]{32}\Z')


class Refusal(RuntimeError):
    pass


def sha(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, sort_keys=True, indent=2) + '\n').encode()


def parse_json(data):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise Refusal('Duplicate JSON key: ' + key)
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise Refusal('Invalid JSON') from exc


def identity(value):
    if not isinstance(value, dict) or value.get('schema') != 1 or type(value.get('schema')) is not int:
        raise Refusal('Manifest schema mismatch')
    if (value.get('kernel'), value.get('board'), value.get('release')) != (KERNEL, BOARD, RELEASE):
        raise Refusal('Wrong board/kernel/release')


def elf_module(data):
    """Read ELF64 LE AArch64 sections, exact modinfo and complete GNU raw note."""
    try:
        if len(data) < 64 or data[:7] != b'\x7fELF\x02\x01\x01':
            raise Refusal('Not ELF64 little-endian')
        header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
        if header[1] != 1 or header[2] != 183:
            raise Refusal('Not an AArch64 relocatable module')
        offset, size, count, names_index = header[6], header[11], header[12], header[13]
        if size != 64 or not count or names_index >= count or offset + size * count > len(data):
            raise Refusal('Invalid ELF section table')
        sections = [struct.unpack_from('<IIQQQQIIQQ', data, offset + i * size) for i in range(count)]
        def payload(section):
            start, length = section[4], section[5]
            if start + length > len(data):
                raise Refusal('ELF section outside file')
            return data[start:start + length]
        strings = payload(sections[names_index])
        found = {}
        for section in sections:
            start = section[0]
            end = strings.find(b'\0', start)
            if start >= len(strings) or end < 0:
                raise Refusal('Bad ELF section name')
            name = strings[start:end]
            if name in (b'.modinfo', b'.note.gnu.build-id'):
                if name in found:
                    raise Refusal('Duplicate ELF identity section')
                found[name] = payload(section)
        notes = found[b'.note.gnu.build-id']
        namesz, descsz, kind = struct.unpack_from('<III', notes)
        expected_len = 12 + ((namesz + 3) & ~3) + ((descsz + 3) & ~3)
        if namesz != 4 or not descsz or kind != 3 or notes[12:16] != b'GNU\0' or expected_len != len(notes):
            raise Refusal('Invalid GNU raw build-id note')
        info = found[b'.modinfo'].split(b'\0')
        def field(key):
            entries = [item[len(key) + 1:].decode('ascii') for item in info if item.startswith(key + b'=')]
            if len(entries) != 1:
                raise Refusal('Missing/duplicate module identity field')
            return entries[0]
        return field(b'name'), field(b'vermagic'), notes.hex()
    except (struct.error, KeyError, UnicodeError) as exc:
        raise Refusal('Malformed module ELF identity') from exc


def validate_modules(data, payloads):
    manifest = parse_json(data)
    identity(manifest)
    if set(manifest) != {'schema', 'kernel', 'board', 'release', 'modules'}:
        raise Refusal('Unexpected module manifest fields')
    records = manifest['modules']
    if not isinstance(records, list) or len(records) != len(MODULES):
        raise Refusal('Module count mismatch')
    seen = set()
    for expected_name, item in zip(MODULES, records):
        if not isinstance(item, dict) or set(item) != {'name', 'file', 'sha256', 'build_note', 'vermagic'}:
            raise Refusal('Module record schema mismatch')
        name = item['name']
        if not isinstance(name, str) or name != expected_name or name in seen or item['file'] != name + '.ko':
            raise Refusal('Module set mismatch')
        seen.add(name)
        value = payloads[item['file']]
        if item['sha256'] != sha(value) or item['vermagic'] != VERMAGIC:
            raise Refusal('Module SHA/vermagic mismatch: ' + name)
        if elf_module(value) != (name, VERMAGIC, item['build_note']):
            raise Refusal('Module ELF name/vermagic/GNU raw note mismatch: ' + name)


def stable_stat(info):
    # Reading may legitimately advance atime; identity/content/permissions must not drift.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid,
            info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def staging_file(path):
    name = path.name
    limit = 16 * 1024 * 1024 if name.endswith('.ko') else 64 * 1024 if name.endswith('.json') else 1024 * 1024
    st = path.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
        raise Refusal('Non-regular/symlink/hardlink staging file: ' + name)
    if st.st_size > limit:
        raise Refusal('Oversize staging file: ' + name)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if stable_stat(opened) != stable_stat(st) or opened.st_size > limit:
            raise Refusal('Staging file changed during validation')
        data = stream.read(limit + 1)
        if (len(data) > limit or stable_stat(os.fstat(stream.fileno())) != stable_stat(st) or
                stable_stat(path.lstat()) != stable_stat(st) or len(data) != st.st_size):
            raise Refusal('Staging file changed/oversize during validation')
        return data


def safe_stage(stage):
    stage = Path(os.path.abspath(stage))
    # Staging may live in /tmp, but no path component may redirect through a link.
    for path in [stage, *stage.parents]:
        if path.is_symlink():
            raise Refusal('Symlink staging path')
    if not stage.is_dir():
        raise Refusal('Staging directory missing')
    expected = set(FILES) | {'package-manifest.json'}
    if {path.name for path in stage.iterdir()} != expected:
        raise Refusal('Staging fixed file set/count mismatch')
    payloads = {}
    for name in expected:
        payloads[name] = staging_file(stage / name)
    return payloads


def validate_unit(data):
    # Bound systemctl enable to the one audited standard wants link: no Also,
    # Alias, extra WantedBy/RequiredBy, or other install-time side effects.
    try:
        text = data.decode('utf-8')
    except UnicodeError as exc:
        raise Refusal('Invalid unit encoding') from exc
    install = False
    entries = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(('#', ';')):
            continue
        if line.startswith('['):
            if line == '[Install]':
                if entries or install:
                    raise Refusal('Duplicate unit Install section')
                install = True
            else:
                install = False
            continue
        if install:
            entries.append(line)
    if entries != ['WantedBy=multi-user.target']:
        raise Refusal('Unit Install section must have only WantedBy=multi-user.target')


def validate_package(stage, accepted):
    if not isinstance(accepted, str) or not HEX.fullmatch(accepted):
        raise Refusal('An external accepted package-manifest SHA256 is required')
    payloads = safe_stage(stage)
    if sha(payloads['package-manifest.json']) != accepted:
        raise Refusal('Package manifest does not match external accepted SHA256')
    manifest = parse_json(payloads['package-manifest.json'])
    identity(manifest)
    if set(manifest) != {'schema', 'kernel', 'board', 'release', 'files'}:
        raise Refusal('Unexpected package manifest fields')
    records = manifest['files']
    if not isinstance(records, list) or len(records) != len(FILES):
        raise Refusal('Package count mismatch')
    seen = set()
    for item in records:
        if not isinstance(item, dict) or set(item) != {'file', 'target', 'mode', 'sha256'}:
            raise Refusal('Package record schema mismatch')
        name = item['file']
        if not isinstance(name, str) or name not in FILES or name in seen:
            raise Refusal('Package fixed file set mismatch')
        seen.add(name)
        target, mode = FILES[name]
        if item['target'] != target or type(item['mode']) is not int or item['mode'] != mode:
            raise Refusal('Package fixed target/mode mismatch')
        if item['sha256'] != sha(payloads[name]):
            raise Refusal('Package file hash mismatch: ' + name)
    if payloads['enabled'] != b'':
        raise Refusal('Enabled sentinel must be empty')
    validate_modules(payloads['module-manifest.json'], payloads)
    validate_unit(payloads['cupid-adsp.service'])
    return {target: {'data': payloads[name], 'mode': mode} for name, (target, mode) in FILES.items()}


class Filesystem:
    def __init__(self, root='/'):
        self.root = Path(os.path.abspath(root))
        self.uid = os.geteuid()
        self.created = []
        self.check_dir(self.root)

    def path(self, name):
        if not name.startswith('/') or '..' in Path(name).parts or str(Path(name)) != name:
            raise Refusal('Unsafe target path')
        return self.root / name.lstrip('/')

    def check_dir(self, path):
        st = path.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != self.uid or st.st_mode & 0o022:
            raise Refusal('Unsafe symlink/owner/writable parent: ' + str(path))

    def parents(self, name):
        path = self.path(name).parent
        ancestors = []
        while path != self.root:
            ancestors.append(path)
            path = path.parent
        self.check_dir(self.root)
        for parent in reversed(ancestors):
            if parent.exists() or parent.is_symlink():
                self.check_dir(parent)

    def read(self, name):
        self.parents(name)
        path = self.path(name)
        try:
            st = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_uid != self.uid or st.st_nlink != 1 or st.st_mode & 0o7022:
            raise Refusal('Unsafe target symlink/owner/mode/hardlink: ' + name)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            if os.fstat(stream.fileno()) != st:
                raise Refusal('Target changed during validation')
            return {'data': stream.read(), 'mode': stat.S_IMODE(st.st_mode)}

    def mkdirs(self, name):
        self.parents(name)
        parent = self.path(name).parent
        missing = []
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for path in reversed(missing):
            path.mkdir(mode=0o755)
            self.created.append(str('/' + str(path.relative_to(self.root))))
            self.sync(path.parent)

    @staticmethod
    def sync(path):
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def put(self, name, item):
        self.read(name)  # Refuse a link even during recovery.
        path = self.path(name)
        if item is None:
            if path.exists():
                path.unlink()
                self.sync(path.parent)
            return
        self.mkdirs(name)
        tmp = path.with_name('.' + path.name + '.transaction-' + uuid.uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item['mode'])
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(item['data'])
                os.fchmod(stream.fileno(), item['mode'])
                stream.flush()
                os.fsync(stream.fileno())
            if sha(tmp.read_bytes()) != sha(item['data']):
                raise Refusal('Atomic temporary readback mismatch')
            os.replace(tmp, path)
            self.sync(path.parent)
            if self.read(name) != item:
                raise Refusal('Atomic installed readback mismatch')
        finally:
            if tmp.exists():
                tmp.unlink()
                self.sync(path.parent)

    def remove_dirs(self, names):
        for name in sorted(set(names), key=lambda value: len(Path(value).parts), reverse=True):
            path = self.path(name)
            if path.exists() or path.is_symlink():
                self.check_dir(path)
                try:
                    path.rmdir()  # Never recursively remove; foreign contents are retained.
                    self.sync(path.parent)
                except OSError as exc:
                    if exc.errno not in (39, 17):
                        raise

    def wants(self):
        self.parents(WANTS)
        path = self.path(WANTS)
        if not path.exists() and not path.is_symlink():
            return False
        st = path.lstat()
        if not stat.S_ISLNK(st.st_mode) or st.st_uid != self.uid:
            raise Refusal('Foreign service enable target')
        if os.readlink(path) not in (UNIT, '../cupid-adsp.service'):
            raise Refusal('Foreign service enable symlink')
        return True

    @contextlib.contextmanager
    def lock(self):
        self.read(LOCK)
        self.mkdirs(LOCK)
        fd = os.open(self.path(LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != self.uid or st.st_mode & 0o022:
                raise Refusal('Unsafe deployment lock')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Refusal('Startup/deployment transaction holds cupid-adsp.lock') from exc
            yield
        finally:
            os.close(fd)


def signature(item):
    return None if item is None else {'sha256': sha(item['data']), 'mode': item['mode']}


def signatures(items):
    return {name: signature(item) for name, item in items.items()}


class System:
    def run(self, *args, timeout=20, codes=(0,)):
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode not in codes:
            raise Refusal('Command failed: ' + ' '.join(args) + '\n' + result.stdout + result.stderr)
        return result.stdout.strip()

    def identity(self):
        if os.uname().release != KERNEL:
            raise Refusal('Running kernel mismatch; no writes')
        fd = os.open('/proc/device-tree/compatible', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if BOARD.encode() not in os.read(fd, 4096).split(b'\0'):
                raise Refusal('Running board mismatch; no writes')
        finally:
            os.close(fd)
        fd = os.open('/etc/cupid-fnos-release', os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if os.read(fd, 128).decode('ascii').strip() != RELEASE:
                raise Refusal('Running fnOS release mismatch; no writes')
        finally:
            os.close(fd)

    def inactive(self):
        state = self.run('/usr/bin/systemctl', 'is-active', SERVICE, codes=(0, 3, 4))
        if state not in ('inactive', 'failed', 'unknown'):
            raise Refusal('Service is running/transitioning. Separately authorize normal systemctl stop first; ADSP is not unloaded.')

    def enabled(self):
        value = self.run('/usr/bin/systemctl', 'is-enabled', SERVICE, codes=(0, 1, 4))
        if value not in ('enabled', 'disabled', 'not-found', 'static'):
            raise Refusal('Unsupported unit enabled state: ' + value)
        return value

    def reload(self):
        self.run('/usr/bin/systemctl', 'daemon-reload')

    def change(self, enabled):
        if enabled == 'enabled':
            self.run('/usr/bin/systemctl', 'enable', SERVICE)
        else:
            self.run('/usr/bin/systemctl', 'disable', SERVICE)

    def fallback(self):
        if not Path('/sys/module/pmic_glink').is_dir():
            self.run('/sbin/modprobe', 'pmic_glink', timeout=10)
        try:
            fd = os.open('/sys/module/pmic_glink/initstate', os.O_RDONLY | os.O_CLOEXEC)
            try:
                state = os.read(fd, 32).strip()
            finally:
                os.close(fd)
        except OSError as error:
            raise Refusal('Fallback pmic_glink live state unavailable') from error
        if state != b'live':
            raise Refusal('Fallback pmic_glink not live')


class Deployment:
    def __init__(self, fs, system):
        self.fs, self.system = fs, system

    def snapshot(self):
        return {name: self.fs.read(name) for name in sorted(TARGETS)}

    def check_enabled(self, enabled):
        link = self.fs.wants()
        if link != (enabled == 'enabled'):
            raise Refusal('Unit enable link/state mismatch')

    def verify(self, expected):
        if signatures(self.snapshot()) != expected:
            raise Refusal('Installed target changed; refusing every write')

    def record(self):
        item = self.fs.read(STATE)
        if item is None:
            return None
        if item['mode'] != 0o600:
            raise Refusal('Deployment state must be private mode 0600')
        record = parse_json(item['data'])
        identity(record)
        if set(record) != {'schema', 'kernel', 'board', 'release', 'status', 'installed', 'baseline', 'created_dirs', 'transactions'}:
            raise Refusal('Invalid deployment state schema')
        if record['status'] not in ('installed', 'uninstalled') or set(record['installed']) != TARGETS:
            raise Refusal('Invalid deployment state targets')
        self.verify(record['installed'])
        self.load_backup(record['baseline'])
        if not isinstance(record['transactions'], list) or not record['transactions']:
            raise Refusal('Missing deployment transaction history')
        for name in record['transactions']:
            self.load_backup(name)
        if not isinstance(record['created_dirs'], list):
            raise Refusal('Invalid directory record')
        allowed_dirs = {str(p) for target in TARGETS | {WANTS} for p in Path(target).parents if str(p) != '/'}
        if any(name not in allowed_dirs for name in record['created_dirs']):
            raise Refusal('Foreign created directory record')
        return record

    def load_backup(self, name):
        if not isinstance(name, str) or not BACKUP.fullmatch(name):
            raise Refusal('Unsafe backup location')
        path = self.fs.path(name)
        self.fs.check_dir(path)
        for ancestor in path.parents:
            if ancestor == self.fs.root:
                break
            if ancestor.name.startswith('adsp-deploy-before-'):
                if stat.S_IMODE(ancestor.stat().st_mode) != 0o700:
                    raise Refusal('Unsafe backup ancestor')
        if stat.S_IMODE(path.stat().st_mode) != 0o700:
            raise Refusal('Backup directory not private')
        metadata = self.fs.read(name + '/before.json')
        if metadata is None or metadata['mode'] != 0o600:
            raise Refusal('Missing/private backup metadata')
        value = parse_json(metadata['data'])
        if set(value) != {'schema', 'before', 'enabled', 'state'} or value['schema'] != 1 or set(value['before']) != TARGETS:
            raise Refusal('Backup target schema mismatch')
        if value['enabled'] not in ('enabled', 'disabled', 'not-found', 'static'):
            raise Refusal('Backup enabled state mismatch')
        result = {}
        for index, target in enumerate(sorted(TARGETS)):
            expected = value['before'][target]
            entry = self.fs.read(name + '/file-' + str(index))
            if expected is None:
                if entry is not None:
                    raise Refusal('Unexpected backup file')
                result[target] = None
            else:
                if not isinstance(expected, dict) or set(expected) != {'sha256', 'mode'} or entry is None or entry['mode'] != 0o600:
                    raise Refusal('Invalid backup file record')
                if type(expected['mode']) is not int or expected['mode'] & ~0o755 or expected['mode'] & 0o022:
                    raise Refusal('Unsafe backed-up mode')
                if expected['sha256'] != sha(entry['data']):
                    raise Refusal('Backup hash mismatch')
                result[target] = {'data': entry['data'], 'mode': expected['mode']}
        state = self.fs.read(name + '/state-before.json')
        if signature(state) != value['state']:
            raise Refusal('Before-state backup hash/mode mismatch')
        return result, value['enabled'], state

    def backup(self, items, enabled):
        name = '/usr/local/lib/cupid/adsp-deploy-before-' + uuid.uuid4().hex
        self.fs.mkdirs(name + '/before.json')
        path = self.fs.path(name)
        path.chmod(0o700)
        self.fs.sync(path.parent)
        state = self.fs.read(STATE)
        for index, target in enumerate(sorted(TARGETS)):
            if items[target] is not None:
                self.fs.put(name + '/file-' + str(index), {'data': items[target]['data'], 'mode': 0o600})
        if state is not None:
            self.fs.put(name + '/state-before.json', state)
        value = {'schema': 1, 'before': signatures(items), 'enabled': enabled, 'state': signature(state)}
        self.fs.put(name + '/before.json', {'data': json_bytes(value), 'mode': 0o600})
        self.load_backup(name)
        return name

    def apply_enabled(self, enabled):
        # systemctl disable on a just-removed unit returns nonzero. For the
        # verified original not-found state, remove only our exact owned link.
        if enabled == 'not-found':
            if self.fs.wants():
                self.fs.path(WANTS).unlink()
                self.fs.sync(self.fs.path(WANTS).parent)
            self.system.reload()
        else:
            self.system.reload()
            self.system.change(enabled)
        if self.system.enabled() != enabled:
            raise Refusal('Systemctl enabled state readback mismatch')
        self.check_enabled(enabled)

    def transaction(self, before, after, old_enabled, new_enabled, record, operation, backup):
        old_state = self.fs.read(STATE)
        changed = []
        starting_dirs = set(self.fs.created)
        journal = backup + '/' + operation + '-result.json'
        allowed_dirs = {str(p) for target in TARGETS | {WANTS} for p in Path(target).parents if str(p) != '/'}
        future_dirs = {name for name in allowed_dirs if not self.fs.path(name).exists()}
        record['installed'] = signatures(after)
        record['created_dirs'] = sorted(set(record['created_dirs']) | future_dirs | {
            name for name in self.fs.created if name in allowed_dirs
        })
        new_state = {'data': json_bytes(record), 'mode': 0o600}
        pending = {'schema': 1, 'operation': operation, 'backup': backup,
                   'after': signatures(after), 'state_after': signature(new_state),
                   'created_dirs': sorted(future_dirs), 'new_enabled': new_enabled}
        if self.fs.read(PENDING) is not None:
            raise Refusal('Unfinished transaction; use deploy.py recover first')
        try:
            self.fs.put(PENDING, {'data': json_bytes(pending), 'mode': 0o600})
            self.fs.put(journal, {'data': json_bytes({'status': 'in-progress', 'operation': operation}), 'mode': 0o600})
            for name in sorted(TARGETS):
                if self.fs.read(name) != before[name]:
                    raise Refusal('Target drift during transaction')
                if before[name] != after[name]:
                    changed.append(name)  # Include failure after os.replace/readback.
                    self.fs.put(name, after[name])
            # systemctl may create this single standard directory.
            self.fs.mkdirs(WANTS)
            self.apply_enabled(new_enabled)
            self.verify(signatures(after))
            if operation == 'uninstall':
                self.fs.remove_dirs(record['created_dirs'])
            record['installed'] = signatures(after)
            allowed_dirs = {str(p) for target in TARGETS | {WANTS} for p in Path(target).parents if str(p) != '/'}
            record['created_dirs'] = sorted(set(record['created_dirs']) | {
                name for name in self.fs.created if name in allowed_dirs
            })
            self.fs.put(STATE, new_state)
            self.fs.put(journal, {'data': json_bytes({'status': 'committed', 'operation': operation}), 'mode': 0o600})
            self.fs.put(PENDING, None)
        except BaseException as failure:
            errors = []
            for name in reversed(changed):
                try:
                    self.fs.put(name, before[name])
                except BaseException as exc:
                    errors.append('file rollback ' + name + ': ' + str(exc))
            try:
                self.fs.put(STATE, old_state)
                self.apply_enabled(old_enabled)
                self.verify(signatures(before))
                if self.fs.read(STATE) != old_state:
                    raise Refusal('State rollback readback mismatch')
            except BaseException as exc:
                errors.append('state/enable rollback: ' + str(exc))
            try:
                self.fs.remove_dirs(set(self.fs.created) - starting_dirs)
            except BaseException as exc:
                errors.append('directory rollback: ' + str(exc))
            try:
                self.fs.put(journal, {'data': json_bytes({'status': 'rollback-incomplete' if errors else 'rolled-back', 'operation': operation, 'errors': errors}), 'mode': 0o600})
            except BaseException as exc:
                errors.append('journal: ' + str(exc))
            if not errors:
                try:
                    self.fs.put(PENDING, None)
                except BaseException as exc:
                    errors.append('pending journal cleanup: ' + str(exc))
            if errors:
                raise Refusal(str(failure) + '; ROLLBACK INCOMPLETE; retained backup ' + backup + '; ' + '; '.join(errors)) from failure
            raise Refusal(str(failure) + '; rollback verified; retained backup ' + backup) from failure

    def preflight(self):
        self.system.identity()
        self.system.inactive()
        if self.fs.read(PENDING) is not None:
            raise Refusal('Unfinished transaction; inspect retained backup and run deploy.py recover first; no writes')
        before = self.snapshot()
        state = self.record()
        enabled = self.system.enabled()
        self.check_enabled(enabled)
        if before[UNIT] is not None:
            validate_unit(before[UNIT]['data'])
        self.fs.read(LOCK)
        # Drop-ins could override startup/enable semantics beyond the fixed package.
        for directory in ('/etc/systemd/system/cupid-adsp.service.d',
                          '/run/systemd/system/cupid-adsp.service.d',
                          '/usr/lib/systemd/system/cupid-adsp.service.d'):
            self.fs.parents(directory + '/placeholder')
            path = self.fs.path(directory)
            if path.exists() or path.is_symlink():
                raise Refusal('Unmanaged unit drop-in directory; refusing every write')
        for directory in ('/run/systemd/system', '/usr/lib/systemd/system'):
            if self.fs.read(directory + '/' + SERVICE) is not None:
                raise Refusal('Foreign runtime/vendor unit; refusing every write')
        return before, state, enabled

    def recovery_preflight(self):
        self.system.identity()
        self.system.inactive()
        item = self.fs.read(PENDING)
        if item is None or item['mode'] != 0o600:
            raise Refusal('No private pending transaction to recover')
        pending = parse_json(item['data'])
        if set(pending) != {'schema', 'operation', 'backup', 'after', 'state_after', 'created_dirs', 'new_enabled'} or pending['schema'] != 1:
            raise Refusal('Invalid pending transaction schema')
        if pending['operation'] not in ('install', 'uninstall') or set(pending['after']) != TARGETS:
            raise Refusal('Invalid pending transaction targets')
        if pending['new_enabled'] not in ('enabled', 'disabled', 'not-found', 'static'):
            raise Refusal('Invalid pending enabled state')
        allowed_dirs = {str(p) for target in TARGETS | {WANTS} for p in Path(target).parents if str(p) != '/'}
        if not isinstance(pending['created_dirs'], list) or any(name not in allowed_dirs for name in pending['created_dirs']):
            raise Refusal('Invalid pending created directories')
        before, enabled, state = self.load_backup(pending['backup'])
        current = self.snapshot()
        for name in TARGETS:
            if signature(current[name]) not in (signature(before[name]), pending['after'][name]):
                raise Refusal('Recovery target drift; refusing every write: ' + name)
        if signature(self.fs.read(STATE)) not in (signature(state), pending['state_after']):
            raise Refusal('Recovery deployment-state drift; refusing every write')
        self.check_enabled(self.system.enabled())
        return pending, before, enabled, state

    def recover(self):
        self.recovery_preflight()
        with self.fs.lock():
            pending, before, enabled, state = self.recovery_preflight()
            errors = []
            for name in sorted(TARGETS, reverse=True):
                try:
                    self.fs.put(name, before[name])
                except BaseException as exc:
                    errors.append(name + ': ' + str(exc))
            try:
                self.fs.put(STATE, state)
                self.fs.mkdirs(WANTS)
                self.apply_enabled(enabled)
                self.verify(signatures(before))
                if self.fs.read(STATE) != state:
                    raise Refusal('Recovered deployment-state readback mismatch')
                self.fs.remove_dirs(pending['created_dirs'])
            except BaseException as exc:
                errors.append('recovery state/enable/directories: ' + str(exc))
            journal = pending['backup'] + '/' + pending['operation'] + '-result.json'
            self.fs.put(journal, {'data': json_bytes({'operation': pending['operation'], 'status': 'recovery-incomplete' if errors else 'recovered', 'errors': errors}), 'mode': 0o600})
            if errors:
                raise Refusal('RECOVERY INCOMPLETE; backup and pending retained: ' + '; '.join(errors))
            self.fs.put(PENDING, None)
            print('RECOVER_OK: before files/modes/enable state verified; backups retained; no start/stop/module changes')

    def install(self, stage, accepted):
        files = validate_package(stage, accepted)  # Complete all trust checks before lock/write.
        self.preflight()
        with self.fs.lock():
            before, state, enabled = self.preflight()
            keep = state is not None and state['status'] == 'installed'
            previous_unit = before[UNIT] is not None
            after = dict(files)
            if keep or previous_unit:
                after[BASE + '/enabled'] = before[BASE + '/enabled']
            desired = enabled if keep or previous_unit else 'enabled'
            if desired == 'not-found':
                desired = 'disabled'
            backup = self.backup(before, enabled)
            record = {
                'schema': 1, 'kernel': KERNEL, 'board': BOARD, 'release': RELEASE,
                'status': 'installed', 'installed': {},
                'baseline': state['baseline'] if keep else backup,
                'created_dirs': state['created_dirs'] if keep else [],
                'transactions': (state['transactions'] if state else []) + [backup],
            }
            self.transaction(before, after, enabled, desired, record, 'install', backup)
            print('INSTALL_OK: files verified; unit ' + desired + '; no start/stop/module changes. Backup: ' + backup)

    def uninstall(self, fallback=False):
        _before, initial_state, _enabled = self.preflight()
        if initial_state is None:
            raise Refusal('No deployment record; refusing unmanaged uninstall')
        with self.fs.lock():
            before, state, enabled = self.preflight()
            if state is None:
                raise Refusal('No deployment record; refusing unmanaged uninstall')
            if state['status'] == 'uninstalled':
                print('UNINSTALL_OK: already restored; retained backups; loaded modules unchanged until reboot')
                return
            after, desired, _old_state = self.load_backup(state['baseline'])
            backup = self.backup(before, enabled)
            record = dict(state)
            record['status'] = 'uninstalled'
            record['transactions'] = state['transactions'] + [backup]
            self.transaction(before, after, enabled, desired, record, 'uninstall', backup)
            print('UNINSTALL_OK: before files/modes/enable state restored; backups retained; loaded ADSP modules left unchanged until reboot')
            if fallback:
                try:
                    self.system.fallback()
                except BaseException as exc:
                    raise Refusal('File uninstall committed, but bounded pmic_glink fallback FAILED; ADSP not unloaded: ' + str(exc)) from exc
                print('Fallback: standard pmic_glink present; ADSP not unloaded')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    install = sub.add_parser('install')
    install.add_argument('stage')
    install.add_argument('accepted_package_sha256', help='externally reviewed/accepted package-manifest SHA256, not calculated here as authentication')
    sub.add_parser('recover', help='restore before-files for a retained interrupted transaction; drift refuses all writes')
    uninstall = sub.add_parser('uninstall')
    uninstall.add_argument('--pmic-fallback', action='store_true', help='explicit bounded standard modprobe pmic_glink; never unload ADSP')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root required')
    try:
        deploy = Deployment(Filesystem(), System())
        if args.action == 'install':
            deploy.install(args.stage, args.accepted_package_sha256)
        elif args.action == 'uninstall':
            deploy.uninstall(args.pmic_fallback)
        else:
            deploy.recover()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
