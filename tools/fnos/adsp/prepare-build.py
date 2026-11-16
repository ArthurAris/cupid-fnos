#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prepare pinned ADSP sources and verify the complete ten-module build."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess

KERNEL = '6.18.18.c1147-trim'
VERMAGIC = KERNEL + ' SMP mod_unload modversions aarch64'
NAMES = ('cupid_hwspinlock_core', 'cupid_hwspinlock_qcom', 'cupid_qcom_smem',
         'cupid_qcom_socinfo', 'cupid_qcom_smp2p', 'cupid_qcom_glink_smem',
         'cupid_qcom_pil_info', 'cupid_qcom_rproc_common', 'cupid_qcom_q6v5_pas',
         'cupid_qrtr_smd')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def prepare(headers, upstream, pas, modules):
    pins = json.loads(Path(__file__).with_name('source-inputs.json').read_text())
    inputs = {}
    for kind, root in (('headers', headers), ('upstream', upstream), ('pas', pas)):
        for name, expected in pins[kind].items():
            data = (root / name).read_bytes()
            if kind != 'headers':
                data = data.replace(b'\r\n', b'\n')
            if digest(data) != expected:
                raise RuntimeError('Source input changed: ' + kind + '/' + name)
            inputs[kind + '/' + name] = data
    if any(modules.iterdir()):
        raise RuntimeError('Build module directory must be empty')
    for name, data in inputs.items():
        if not name.startswith('headers/'):
            target = modules / Path(name).name
            if target.exists():
                raise RuntimeError('Duplicate source basename')
            target.write_bytes(data)
    patches = (
        ('qcom_q6v5_pas.c',
         'if (stop_state || qcom_sysmon_shutdown_irq_state(pas->sysmon)) {',
         'if (stop_state) {'),
        ('qcom_common.c', 'ret = auxiliary_device_add(adev);',
         'ret = __auxiliary_device_add(adev, "qcom_common");'),
    )
    patched = {}
    for name, before, after in patches:
        path = modules / name
        text = path.read_text()
        if text.count(before) != 1:
            raise RuntimeError('Patch anchor must occur once: ' + name)
        path.write_text(text.replace(before, after, 1))
        patched[name] = digest(path.read_bytes())
    (modules / 'cupid_hwspinlock_license.c').write_text(
        '// SPDX-License-Identifier: GPL-2.0-only\n#include <linux/module.h>\n'
        'MODULE_LICENSE("GPL v2");\n')
    sources = ('hwspinlock_core.o cupid_hwspinlock_license.o', 'qcom_hwspinlock.o',
               'smem.o smem_state.o', 'socinfo.o', 'smp2p.o', 'qcom_glink_smem.o',
               'qcom_pil_info.o', 'qcom_common.o', 'qcom_q6v5.o qcom_q6v5_pas.o', 'smd.o')
    makefile = ''.join('obj-m += ' + name + '.o\n' + name + '-y := ' + objects + '\n\n'
                       for name, objects in zip(NAMES, sources))
    makefile += 'ccflags-y += -I$(src) -g\n'
    for option in ('HWSPINLOCK', 'HWSPINLOCK_QCOM', 'QCOM_SMEM', 'QCOM_SMEM_STATE',
                   'QCOM_SMP2P', 'RPMSG_QCOM_GLINK_SMEM_MODULE', 'QCOM_PIL_INFO_MODULE',
                   'QCOM_RPROC_COMMON_MODULE', 'QCOM_Q6V5_COMMON',
                   'QCOM_Q6V5_PAS_MODULE', 'QCOM_SOCINFO'):
        makefile += 'ccflags-y += -DCONFIG_' + option + '=1\n'
    (modules / 'Makefile').write_text(makefile)
    (modules.parent / 'source-report.json').write_text(json.dumps(
        {'inputs': pins, 'patched': patched,
         'pdm_match_name': 'qcom_common.pd-mapper',
         'pas_shutdown_check_removed': True,
         'pas_attach_equivalence_proven': False}, indent=2) + '\n')


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def symvers(path):
    rows = {}
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) < 4:
            raise RuntimeError('Malformed Module.symvers')
        if fields[1] in rows and rows[fields[1]] != int(fields[0], 16):
            raise RuntimeError('Conflicting symbol CRC')
        rows[fields[1]] = int(fields[0], 16)
    return rows


def verify(headers, modules, dist):
    if {p.stem for p in modules.glob('*.ko')} != set(NAMES):
        raise RuntimeError('Incomplete or extra module build')
    if any(dist.iterdir()):
        raise RuntimeError('Output directory must be new and empty')
    official = symvers(headers / 'Module.symvers')
    local = symvers(modules / 'Module.symvers')
    overlap = set(official) & set(local)
    if overlap:
        raise RuntimeError('Export conflicts with official kernel: ' + ','.join(sorted(overlap)))
    providers = {**official, **local}
    crc_rows, rows = [], []
    for name in NAMES:
        source = modules / (name + '.ko')
        if command('modinfo', '-F', 'name', str(source)) != name:
            raise RuntimeError('Module identity mismatch')
        if command('modinfo', '-F', 'vermagic', str(source)) != VERMAGIC:
            raise RuntimeError('Module ABI version mismatch')
        versions = {}
        for line in command('modprobe', '--dump-modversions', str(source)).splitlines():
            crc, symbol = line.split()
            versions[symbol] = int(crc, 16)
            if symbol not in providers or providers[symbol] != int(crc, 16):
                raise RuntimeError('Unresolved or mismatched CRC: ' + name + ':' + symbol)
            crc_rows.append({'module': name, 'symbol': symbol,
                             'provider': 'official' if symbol in official else 'group'})
        undefined = command('aarch64-linux-gnu-nm', '-u', str(source)).splitlines()
        for line in undefined:
            fields = line.split()
            if fields[-2] == 'U' and fields[-1] not in providers:
                raise RuntimeError('Strong undefined symbol: ' + fields[-1])
        if not versions:
            raise RuntimeError('No versioned imports')
        target = dist / source.name
        target.write_bytes(source.read_bytes())
        subprocess.run(['aarch64-linux-gnu-strip', '--strip-debug', str(target)], check=True)
        note = modules / (name + '.build-note')
        subprocess.run(['aarch64-linux-gnu-objcopy', '--dump-section',
                        '.note.gnu.build-id=' + str(note), str(source)], check=True)
        note_data = note.read_bytes()
        if len(note_data) != 36 or note_data[:16] != bytes.fromhex('040000001400000003000000474e5500'):
            raise RuntimeError('Unexpected GNU build note')
        rows.append({'name': name, 'file': target.name, 'sha256': digest(target.read_bytes()),
                     'build_note': note_data.hex(), 'vermagic': VERMAGIC})
    manifest = {'schema': 1, 'kernel': KERNEL, 'board': 'xiaomi,cupid',
                'release': 'cupid-fnos-v1', 'modules': rows}
    (dist / 'module-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    report = {'modules': len(rows), 'versioned_imports': len(crc_rows),
              'official_imports': sum(r['provider'] == 'official' for r in crc_rows),
              'group_imports': sum(r['provider'] == 'group' for r in crc_rows),
              'crc_mismatch': 0, 'export_conflicts': 0, 'symbol_failures': 0,
              'layout_verified': False, 'phone_loaded': False}
    (modules.parent / 'symbol-report.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('headers', type=Path)
    p.add_argument('upstream', type=Path)
    p.add_argument('pas', type=Path)
    p.add_argument('modules', type=Path)
    p = sub.add_parser('verify')
    p.add_argument('headers', type=Path)
    p.add_argument('modules', type=Path)
    p.add_argument('dist', type=Path)
    args = vars(parser.parse_args())
    mode = args.pop('mode')
    (prepare if mode == 'prepare' else verify)(**args)


if __name__ == '__main__':
    main()
