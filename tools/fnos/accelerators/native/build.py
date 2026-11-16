#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build compilation prototypes for the official fnOS kernel; never load them.

Static inspection is fail-closed. Successful compilation and symbol inspection
are separate from layout, protocol, firmware and hardware acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
TOOLS = {
    'make': '/usr/bin/make',
    'modinfo': '/usr/sbin/modinfo',
    'modprobe': '/usr/sbin/modprobe',
    'readelf': '/usr/bin/aarch64-linux-gnu-readelf',
    'patch': '/usr/bin/patch',
    'compiler': '/usr/bin/aarch64-linux-gnu-gcc',
}
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}
IRIS_OBJECTS = (
    'iris_buffer.o iris_common.o iris_core.o iris_ctrls.o iris_firmware.o '
    'iris_hfi_common.o iris_hfi_gen1_command.o iris_hfi_gen1_response.o '
    'iris_hfi_gen2_command.o iris_hfi_gen2_packet.o iris_hfi_gen2_response.o '
    'iris_hfi_queue.o iris_platform_gen2.o iris_power.o iris_probe.o iris_resources.o '
    'iris_state.o iris_utils.o iris_vidc.o iris_vb2.o iris_vdec.o iris_venc.o '
    'iris_vpu2.o iris_vpu3x.o iris_vpu_buffer.o iris_vpu_common.o iris_platform_sm8250.o')


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key: ' + key)
        result[key] = value
    return result


PINS = json.loads((HERE / 'sources.json').read_text(), object_pairs_hook=unique_json)


def component_objects(component):
    if component == 'iris':
        return tuple(IRIS_OBJECTS.split())
    if component == 'fastrpc':
        return ('fastrpc.o',)
    if component == 'videocc':
        return ('videocc-sm8450.o',)
    raise ValueError('Unknown module component')


def regular_file(path):
    path = Path(path).absolute()
    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents):
        raise RuntimeError('Symlink input refused: ' + str(path))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise RuntimeError('Nonordinary input refused: ' + str(path))
    return path


def sha256(path):
    digest = hashlib.sha256()
    with regular_file(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def require_pin(path, expected, label):
    if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise RuntimeError('Malformed SHA pin: ' + label)
    actual = sha256(path)
    if actual != expected:
        raise RuntimeError('Pinned input changed: ' + label)


def checked_copy(source, target, expected, label):
    require_pin(source, expected, label)
    shutil.copyfile(source, target)
    require_pin(target, expected, label + ' copy')


def official_symbols(headers):
    rows = {}
    for line in regular_file(headers / 'Module.symvers').read_text().splitlines():
        fields = line.split()
        if (len(fields) not in (4, 5) or not re.fullmatch(r'(?:0x)?[0-9a-fA-F]{8}', fields[0]) or
                not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', fields[1]) or
                fields[3] not in ('EXPORT_SYMBOL', 'EXPORT_SYMBOL_GPL')):
            raise RuntimeError('Malformed Module.symvers')
        crc = int(fields[0], 16)
        if fields[1] in rows and rows[fields[1]] != crc:
            raise RuntimeError('Conflicting official symbol CRC: ' + fields[1])
        rows[fields[1]] = crc
    if not rows:
        raise RuntimeError('Empty official symbol table')
    return rows


def command(args, cwd=None, timeout=240):
    try:
        result = subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                                timeout=timeout, env=ENV)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError('Command unavailable or timed out: ' + args[0]) from error
    if result.returncode:
        raise RuntimeError('Command failed: ' + args[0] + '\n' + result.stdout + result.stderr)
    return result


def inspect(args):
    result = command(args, timeout=15)
    # Also enforce this at the parser boundary (fixtures must not bypass it).
    if result.returncode or result.stderr.strip() or not result.stdout.strip():
        raise RuntimeError('Static inspector failed or returned no output: ' + args[0])
    return result.stdout


def single_value(args):
    lines = inspect(args).splitlines()
    if len(lines) != 1 or not lines[0].strip():
        raise RuntimeError('Expected a single inspector value')
    return lines[0].strip()


def elf_module(path):
    path = regular_file(path)
    with path.open('rb') as handle:
        data = handle.read(64)
    if (len(data) != 64 or data[:7] != b'\x7fELF\x02\x01\x01' or
            struct.unpack_from('<HHI', data, 16) != (1, 183, 1) or
            struct.unpack_from('<H', data, 52)[0] != 64):
        raise RuntimeError('Module must be little-endian AArch64 ELF64 ET_REL')


def symbol_records(output):
    records = []
    table_count = None
    row_count = 0
    table_names = set()
    for line in output.splitlines():
        if not line.strip():
            continue
        table = re.fullmatch(r"Symbol table '([^']+)' contains (\d+) entries:", line)
        if table:
            if table_count is not None and row_count != table_count:
                raise RuntimeError('Incomplete ELF symbol table')
            name, count = table.groups()
            if name in table_names:
                raise RuntimeError('Duplicate ELF symbol table')
            table_names.add(name)
            table_count, row_count = int(count), 0
            continue
        if re.fullmatch(r'\s*Num:\s+Value\s+Size\s+Type\s+Bind\s+Vis\s+Ndx\s+Name\s*', line):
            continue
        match = re.fullmatch(r'\s*(\d+):\s+([0-9a-fA-F]+)\s+(\d+)\s+(\w+)\s+(\w+)\s+(\w+)\s+(\w+)(?:\s+(\S+))?\s*', line)
        if not match:
            raise RuntimeError('Malformed ELF symbol row')
        number, _value, size, kind, binding, _visibility, index, name = match.groups()
        if table_count is None or int(number) != row_count or row_count >= table_count:
            raise RuntimeError('Incomplete or unordered ELF symbol table')
        if binding not in ('LOCAL', 'GLOBAL', 'WEAK'):
            raise RuntimeError('Unsupported ELF symbol binding')
        row_count += 1
        records.append({'kind': kind, 'binding': binding, 'index': index,
                        'name': name or '', 'size': int(size)})
    if not records:
        raise RuntimeError('Empty ELF symbol table')
    if row_count != table_count:
        raise RuntimeError('Incomplete ELF symbol table')
    return records


def version_records(output):
    versions = {}
    for line in output.splitlines():
        match = re.fullmatch(r'\s*(0x[0-9a-fA-F]{8})\s+([A-Za-z_][A-Za-z0-9_]*)\s*', line)
        if not match:
            raise RuntimeError('Malformed module version row')
        crc, name = match.groups()
        if name in versions:
            raise RuntimeError('Duplicate module version import')
        versions[name] = int(crc, 16)
    if not versions:
        raise RuntimeError('Empty module version imports')
    return versions


def build_note(output):
    ids = re.findall(r'^\s*Build ID: ([0-9a-f]{40})\s*$', output, re.M)
    if len(ids) != 1 or '.note.gnu.build-id' not in output:
        raise RuntimeError('Missing or ambiguous GNU SHA1 build note')
    return ids[0]


def gate_module(ko, expected_name, expected_alias, official, headers=None):
    elf_module(ko)
    name = single_value([TOOLS['modinfo'], '-F', 'name', str(ko)])
    vermagic = single_value([TOOLS['modinfo'], '-F', 'vermagic', str(ko)])
    license_name = single_value([TOOLS['modinfo'], '-F', 'license', str(ko)])
    aliases = inspect([TOOLS['modinfo'], '-F', 'alias', str(ko)]).splitlines()
    wanted_aliases = {'of:N*T*C' + expected_alias, 'of:N*T*C' + expected_alias + 'C*'}
    if name != expected_name or vermagic != PINS['vermagic']:
        raise RuntimeError('Module name or vermagic mismatch')
    if license_name not in ('GPL', 'GPL v2', 'Dual BSD/GPL', 'Dual MIT/GPL'):
        raise RuntimeError('Module GPL license required')
    if not any(alias in wanted_aliases for alias in aliases):
        raise RuntimeError('Exact OF compatible missing')
    symbols = symbol_records(inspect([TOOLS['readelf'], '-Ws', str(ko)]))
    strong = {row['name'] for row in symbols if row['binding'] == 'GLOBAL' and row['index'] == 'UND' and row['name']}
    weak = sorted({row['name'] for row in symbols if row['binding'] == 'WEAK' and row['index'] == 'UND' and row['name']})
    exports = sorted({row['name'][len('__ksymtab_'):] for row in symbols
                      if row['index'] != 'UND' and row['name'].startswith('__ksymtab_')})
    missing = sorted(strong - official.keys())
    if missing:
        raise RuntimeError('Strong undefined symbol lacks provider: ' + ','.join(missing))
    conflicts = sorted(set(exports) & official.keys())
    if conflicts:
        raise RuntimeError('Export conflicts with official providers: ' + ','.join(conflicts))
    versions = version_records(inspect([TOOLS['modprobe'], '--dump-modversions', str(ko)]))
    for symbol, crc in versions.items():
        if symbol not in official or official[symbol] != crc:
            raise RuntimeError('Unresolved or mismatched CRC: ' + symbol)
    if strong - versions.keys():
        raise RuntimeError('Strong import lacks version CRC: ' + ','.join(sorted(strong - versions.keys())))
    note = build_note(inspect([TOOLS['readelf'], '-n', str(ko)]))
    return {'module': expected_name, 'name': name, 'vermagic': vermagic, 'license': license_name,
            'alias_ok': True, 'strong_imports': len(strong), 'weak_undefined': weak,
            'unversioned_undefined': [], 'exports': exports, 'export_conflicts': [],
            'versioned_imports': len(versions), 'crc_mismatches': [],
            'build_id': note, 'ko_sha256': sha256(ko), 'static_inspection_passed': True,
            'layout_verified': False, 'protocol_verified': False,
            'hardware_ready': False, 'passed': False}


def compile_modules(headers, work):
    args = [TOOLS['make'], '-C', str(headers), 'M=' + str(work), 'ARCH=arm64',
            'CROSS_COMPILE=/usr/bin/aarch64-linux-gnu-', 'modules']
    try:
        result = command(args, cwd='/var/tmp', timeout=480)
    except RuntimeError as error:
        (work / 'build.log').write_text(str(error))
        raise
    (work / 'build.log').write_text(result.stdout + result.stderr)


def build_fastrpc(upstream, headers, work):
    pin = PINS['fastrpc']
    checked_copy(upstream / pin['file'], work / 'fastrpc.c', pin['sha256'], pin['file'])
    (work / 'Makefile').write_text('obj-m := ' + pin['module'] + '.o\n' + pin['module'] + '-y := fastrpc.o\n'
                                 'ccflags-y += -DCONFIG_QCOM_FASTRPC=1\n')
    compile_modules(headers, work)
    return work / (pin['module'] + '.ko')


def build_videocc(upstream, headers, work):
    pin = PINS['videocc']
    inputs = {pin['file']: pin['sha256'], **{'drivers/clk/qcom/' + name: digest for name, digest in pin['headers'].items()}}
    for name, expected in inputs.items():
        require_pin(upstream / name, expected, name)
    for name, expected in inputs.items():
        checked_copy(upstream / name, work / Path(name).name, expected, name)
    header = (work / 'clk-rcg.h').read_bytes()
    before = b'\tu32\t\t\tparked_cfg;\n\tbool\t\t\thw_clk_ctrl;'
    after = b'\tu32\t\t\tparked_cfg;\n\tunsigned long\t\tconfigured_freq;\n\tbool\t\t\thw_clk_ctrl;'
    if header.count(before) != 1:
        raise RuntimeError('clk_rcg2 layout anchor changed')
    (work / 'clk-rcg.h').write_bytes(header.replace(before, after, 1))
    (work / 'Makefile').write_text('obj-m := ' + pin['module'] + '.o\n' + pin['module'] + '-y := videocc-sm8450.o\n'
                                 'ccflags-y += -I$(src) -DCONFIG_SM_VIDEOCC_8450=1\n')
    compile_modules(headers, work)
    return work / (pin['module'] + '.ko')


def prepare_iris(upstream, work):
    pin = PINS['iris']
    source_dir = upstream / pin['upstream_dir']
    if {path.name for path in source_dir.iterdir()} != set(pin['files']):
        raise RuntimeError('Extra or missing Iris source entry')
    for name, expected in pin['files'].items():
        require_pin(source_dir / name, expected, pin['upstream_dir'] + '/' + name)
    patches = [(HERE / pin['patch'], pin['patch_sha256'])]
    patches.extend((HERE / row['path'], row['sha256']) for row in pin.get('followup_patches', []))
    for patch, digest in patches:
        require_pin(patch, digest, 'Iris patch')
    for name, expected in pin['files'].items():
        checked_copy(source_dir / name, work / name, expected, name)
    logs = []
    for patch, _digest in patches:
        result = command([TOOLS['patch'], '--batch', '--forward', '--fuzz=0', '-p1', '-d', str(work), '-i', str(patch)])
        logs.append(result.stdout + result.stderr)
        if re.search(r'offset|fuzz', result.stdout + result.stderr, re.I):
            raise RuntimeError('Iris patch requires fuzzy or shifted application')
    (work / 'patch.log').write_text(''.join(logs))
    (work / 'Makefile').write_text('obj-m := ' + pin['module'] + '.o\n' + pin['module'] + '-y := ' + IRIS_OBJECTS + '\n'
                                 'ccflags-y += -I$(src)\n')


def build_iris(upstream, headers, work):
    prepare_iris(upstream, work)
    behavior = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-iris-behavior.py'),
                        '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'behavior-tests.log').write_text(behavior.stdout + behavior.stderr)
    lifecycle = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-hfi-lifecycle.py'),
                         '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'hfi-lifecycle-tests.log').write_text(lifecycle.stdout + lifecycle.stderr)
    response = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-hfi-response.py'),
                        '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'hfi-response-tests.log').write_text(response.stdout + response.stderr)
    formats = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-output-formats.py'),
                       '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'output-format-tests.log').write_text(formats.stdout + formats.stderr)
    correlation = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-control-correlation.py'),
                           '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'control-correlation-tests.log').write_text(correlation.stdout + correlation.stderr)
    cleanup = command(['/usr/bin/python3', '-I', '-B', str(HERE / 'test-decode-cleanup.py'),
                       '--work', str(work)], cwd='/var/tmp', timeout=120)
    (work / 'decode-cleanup-tests.log').write_text(cleanup.stdout + cleanup.stderr)
    compile_modules(headers, work)
    return work / (PINS['iris']['module'] + '.ko')


BUILDERS = {'fastrpc': build_fastrpc, 'videocc': build_videocc, 'iris': build_iris}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('component', choices=sorted(BUILDERS))
    parser.add_argument('--upstream', required=True, type=Path)
    parser.add_argument('--headers', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--layout-spec', required=True, type=Path, help='Private pinned official BTF references')
    parser.add_argument('--layout-spec-sha256', required=True)
    args = parser.parse_args()
    require_pin(args.layout_spec, args.layout_spec_sha256, 'private layout specification')
    specification = json.loads(regular_file(args.layout_spec).read_text(), object_pairs_hook=unique_json)
    layout_spec = importlib.util.spec_from_file_location('native_layout_gate', HERE / 'layout.py')
    layout_gate = importlib.util.module_from_spec(layout_spec)
    layout_spec.loader.exec_module(layout_gate)
    signature_spec = importlib.util.spec_from_file_location('native_signature_gate', HERE / 'type-signatures.py')
    signature_gate = importlib.util.module_from_spec(signature_spec)
    signature_spec.loader.exec_module(signature_gate)
    if os.path.lexists(args.out):
        raise SystemExit('Output directory must not exist: ' + str(args.out))
    for name, expected in PINS['official_headers'].items():
        require_pin(args.headers / name, expected, 'headers/' + name)
    official = official_symbols(args.headers)
    args.out.mkdir(parents=True)
    work = Path(tempfile.mkdtemp(prefix='native-' + args.component + '-', dir='/var/tmp'))
    try:
        ko = BUILDERS[args.component](args.upstream, args.headers, work)
        pin = PINS[args.component]
        report = gate_module(ko, pin['module'], pin['alias'], official, args.headers)
        layout_report = layout_gate.audit(specification, work, args.component)
        (args.out / 'layout-report.json').write_text(json.dumps(layout_report, indent=2) + '\n')
        report.update(component=args.component, work_dir=str(work), phone_operated=False, driver_loaded=False,
                      compiler=single_value([TOOLS['compiler'], '-dumpfullversion']),
                      c_behavior_scope_verified=args.component == 'iris' and (work / 'behavior-tests.log').is_file(),
                      hfi_lifecycle_scope_verified=args.component == 'iris' and (work / 'hfi-lifecycle-tests.log').is_file(),
                      hfi_response_scope_verified=args.component == 'iris' and (work / 'hfi-response-tests.log').is_file(),
                      output_formats_scope_verified=args.component == 'iris' and (work / 'output-format-tests.log').is_file(),
                      control_correlation_scope_verified=args.component == 'iris' and (work / 'control-correlation-tests.log').is_file(),
                      decode_cleanup_scope_verified=args.component == 'iris' and (work / 'decode-cleanup-tests.log').is_file(),
                      layout_scope_verified=layout_report['layout_scope_verified'],
                      complete_abi_verified=False,
                      remaining_gates=['independent signatures for CRC-only/no-BTF imports',
                                       'protocol and firmware pairing', 'secure-loader and runtime acceptance',
                                       'isolated RAM acceptance'])
        if not layout_report['layout_scope_verified']:
            report['remaining_gates'].insert(0, 'missing or mismatched required type layout')
            (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            raise RuntimeError('Required shared layout scope is incomplete; no prototype published')
        signature_report = signature_gate.audit(specification, work, args.component)
        (args.out / 'type-signature-report.json').write_text(json.dumps(signature_report, indent=2) + '\n')
        report.update(member_types_scope_verified=signature_report['member_types_scope_verified'],
                      function_signatures_scope_verified=signature_report['function_signatures_scope_verified'],
                      complete_type_graph_verified=False, all_imported_function_signatures_verified=False)
        if not (signature_report['member_types_scope_verified'] and
                signature_report['function_signatures_scope_verified']):
            report['remaining_gates'].insert(0, 'missing or mismatched scoped member types/function signatures')
            (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            raise RuntimeError('Required type/signature scope incomplete; no prototype published')
        abi_spec = importlib.util.spec_from_file_location('native_abi_gate', HERE / 'abi-complete.py')
        abi_gate = importlib.util.module_from_spec(abi_spec)
        abi_spec.loader.exec_module(abi_gate)
        abi_report = abi_gate.audit(specification, work, args.component, args.headers)
        (args.out / 'abi-report.json').write_text(json.dumps(abi_report, indent=2) + '\n')
        report.update(shared_type_graph_verified=abi_report['complete_shared_type_graph_verified'],
                      abi_status=abi_report['status'], abi_objects_checked=abi_report['objects_checked'],
                      abi_types_unknown=abi_report['undecidable_type_names'],
                      abi_private_types_excluded=abi_report['private_type_names_excluded'],
                      abi_shared_types=abi_report['shared_type_names'],
                      abi_imports_crc_only=len(abi_report['imports_crc_only']),
                      abi_imports_without_btf=len(abi_report['imports_without_btf']))
        if abi_report['status'] in ('mismatch', 'error'):
            report['remaining_gates'].insert(0, 'shared ABI conflict or mismatch')
            (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            raise RuntimeError('Shared ABI conflict or mismatch; no prototype published')
        if abi_report['status'] == 'unknown':
            report['remaining_gates'].insert(0, 'unverified shared ABI types/signatures')
        # Retain as an explicitly non-loadable compilation artifact, not a release.
        artifact = args.out / 'prototype'
        artifact.mkdir()
        shutil.copyfile(ko, artifact / ko.name)
        (args.out / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
        shutil.copyfile(work / 'build.log', args.out / 'build.log')
        if args.component == 'iris':
            shutil.copyfile(work / 'behavior-tests.log', args.out / 'behavior-tests.log')
            shutil.copyfile(work / 'hfi-lifecycle-tests.log', args.out / 'hfi-lifecycle-tests.log')
            shutil.copyfile(work / 'hfi-response-tests.log', args.out / 'hfi-response-tests.log')
            shutil.copyfile(work / 'output-format-tests.log', args.out / 'output-format-tests.log')
            shutil.copyfile(work / 'control-correlation-tests.log', args.out / 'control-correlation-tests.log')
            shutil.copyfile(work / 'decode-cleanup-tests.log', args.out / 'decode-cleanup-tests.log')
    except (OSError, RuntimeError, ValueError, struct.error) as error:
        (args.out / 'failure.json').write_text(json.dumps({'error': str(error), 'work_dir': str(work),
                                                         'passed': False, 'hardware_ready': False}, indent=2) + '\n')
        raise SystemExit(str(error)) from error
    print(json.dumps(report, sort_keys=True))


if __name__ == '__main__':
    main()
