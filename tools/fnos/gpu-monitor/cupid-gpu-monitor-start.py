#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Load the tested fnOS GPU monitoring modules without changing GPU or NAS policy."""
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import time

KERNEL = '6.18.18.c1147-trim'
MODULE_DIR = '/usr/local/lib/cupid/gpu-monitor/' + KERNEL
MODULES = (
    {
        'name': 'cupid_gpu_monitor',
        'file': 'cupid-gpu-monitor.ko',
        'sha256': '10e827d144f3ae7670c4eee5cc1a09576bd56737fa4fc6528676db21bf30068d',
        'build_note': '040000001400000003000000474e55004ecc6303f0870c0ca216cee7948f0ed182c45644',
        'args': ('gpu_name=3d00000.gpu',),
    },
    {
        'name': 'cupid_gpu_thermal',
        'file': 'cupid-gpu-thermal.ko',
        'sha256': '69cba2d4988319534cf962a3cf9d1f81688cc653e868080408c3b47143828bcb',
        'build_note': '040000001400000003000000474e550083c06cc16dba720e13841030ba53665866baa2e4',
        'args': (),
    },
)


class Runtime:
    def __init__(self, root=Path('/')):
        self.root = Path(root)

    def path(self, name):
        return self.root / name.lstrip('/')

    def read(self, name, limit=4096):
        fd = os.open(self.path(name), os.O_RDONLY | os.O_CLOEXEC)
        try:
            data = os.read(fd, limit)
        finally:
            os.close(fd)
        if len(data) == limit:
            raise RuntimeError('Unexpectedly large runtime attribute: ' + name)
        return data

    def text(self, name):
        return self.read(name).decode('ascii').strip()

    def command(self, arguments, timeout=10):
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError('Command failed: ' + arguments[0] + ': ' +
                               (result.stdout + result.stderr).strip()[:500])
        return result.stdout.strip()


def secure_module_path(runtime, name):
    path = runtime.path(MODULE_DIR + '/' + name)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise RuntimeError('Module must be root-owned and not writable by others')
    for parent in path.parents:
        if parent == runtime.root:
            break
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('Unsafe module directory')
    return path


def validate_environment(runtime):
    if runtime.text('/proc/sys/kernel/osrelease') != KERNEL:
        raise RuntimeError('Untested kernel; GPU monitoring not loaded')
    if b'xiaomi,cupid' not in runtime.read('/proc/device-tree/compatible').split(b'\0'):
        raise RuntimeError('Not the tested cupid board')
    if runtime.text('/etc/cupid-fnos-release') != 'cupid-fnos-v1':
        raise RuntimeError('Unexpected system release')


def validate_files(runtime):
    files = {}
    for module in MODULES:
        path = secure_module_path(runtime, module['file'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != module['sha256']:
            raise RuntimeError('Untested module bytes: ' + module['name'])
        if runtime.command(['/usr/sbin/modinfo', '-F', 'name', str(path)]) != module['name']:
            raise RuntimeError('Module name mismatch')
        expected = KERNEL + ' SMP mod_unload modversions aarch64'
        if runtime.command(['/usr/sbin/modinfo', '-F', 'vermagic', str(path)]) != expected:
            raise RuntimeError('Module version mismatch')
        files[module['name']] = path
    return files


def thermal_zones(runtime):
    zones = {}
    for path in runtime.path('/sys/class/thermal').glob('thermal_zone*'):
        name = '/' + str(path.relative_to(runtime.root)) + '/type'
        try:
            kind = runtime.text(name)
        except OSError:
            continue
        zones.setdefault(kind, []).append(path)
    return zones


def providers_ready(runtime):
    drivers = (
        ('/sys/bus/platform/devices/3d00000.gpu/driver', 'adreno'),
        ('/sys/bus/platform/devices/c263000.thermal-sensor/driver', 'qcom-tsens'),
    )
    for name, wanted in drivers:
        path = runtime.path(name)
        if not path.is_symlink() or path.resolve().name != wanted:
            return False
    if not runtime.path('/sys/class/devfreq/3d00000.gpu').is_dir():
        return False
    zones = thermal_zones(runtime)
    return all(len(zones.get(name, [])) == 1 for name in ('gpu-top-thermal', 'gpu-bottom-thermal'))


def wait_for_providers(runtime, seconds=15):
    deadline = time.monotonic() + seconds
    while not providers_ready(runtime):
        if time.monotonic() >= deadline:
            raise RuntimeError('GPU/TSENS provider readiness deadline; no load retry')
        time.sleep(.2)


def check_loaded(runtime, module):
    directory = '/sys/module/' + module['name']
    if not runtime.path(directory).exists():
        return False
    if runtime.text(directory + '/initstate') != 'live':
        raise RuntimeError('Existing GPU module is not live')
    if runtime.read(directory + '/notes/.note.gnu.build-id').hex() != module['build_note']:
        raise RuntimeError('Another GPU module build is loaded; no replacement attempted')
    if module['name'] == 'cupid_gpu_monitor':
        if runtime.text(directory + '/parameters/gpu_name') != '3d00000.gpu':
            raise RuntimeError('Existing monitor targets a different GPU')
    return True


def verify_interfaces(runtime):
    base = '/sys/class/devfreq/3d00000.gpu/'
    if not runtime.path(base + 'gpu_busy_percentage').is_file():
        raise RuntimeError('GPU utilization interface absent')
    sample = runtime.text(base + 'sample_status')
    if not sample.startswith('valid='):
        raise RuntimeError('GPU sample status missing')
    # No valid sample at boot is legitimate; never wake GPU just to fabricate 0%.
    zones = thermal_zones(runtime).get('gpuss-cupid', [])
    if len(zones) != 1:
        raise RuntimeError('Expected one GPU temperature alias')
    path = '/' + str(zones[0].relative_to(runtime.root)) + '/temp'
    temperature = int(runtime.text(path))
    if not -273000 <= temperature <= 200000:
        raise RuntimeError('GPU temperature alias returned invalid units')
    return {'sample_status': sample, 'temperature_mC': temperature}


def activate(runtime):
    validate_environment(runtime)
    files = validate_files(runtime)
    wait_for_providers(runtime)
    # Check both existing identities before any new module load.
    existing = {module['name']: check_loaded(runtime, module) for module in MODULES}
    actions = []
    for module in MODULES:
        if not existing[module['name']]:
            runtime.command(['/usr/sbin/insmod', str(files[module['name']]), *module['args']])
            if not check_loaded(runtime, module):
                raise RuntimeError('Module load did not publish expected identity')
        actions.append({'module': module['name'], 'already_loaded': existing[module['name']]})
    result = verify_interfaces(runtime)
    return {'kernel': KERNEL, 'modules': actions, **result,
            'gpu_power_policy_changed': False, 'native_services_restarted': False}


def main():
    if os.geteuid() != 0:
        raise SystemExit('GPU monitoring startup requires root')
    try:
        result = activate(Runtime())
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('CUPID_GPU_MONITOR_START_FAILED=' + str(error), flush=True)
        raise SystemExit(1)
    print('CUPID_GPU_MONITOR_READY=' + json.dumps(result, sort_keys=True), flush=True)


if __name__ == '__main__':
    main()
