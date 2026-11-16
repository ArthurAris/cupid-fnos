#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Verify static firmware/DT/driver inputs; never authenticate or execute firmware.

ELF program headers, not manifest completeness claims, determine required data
segments. Driver text and firmware strings remain evidence, not proof of HFI ABI
compatibility or secure-loader acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import struct

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('firmware_check', HERE / 'verify-firmware.py')
CHECK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECK)
COMPONENTS = {
    'video': {'prefix': 'vpu20_4v', 'machine': 94, 'pas_id': 9,
              'node': '/soc@0/video-codec@aa00000',
              'firmware_name': 'qcom/vpu/vpu20_4v.mbn', 'version_prefix': 'video-firmware.'},
    'cdsp': {'prefix': 'cdsp', 'machine': 164, 'pas_id': 18,
             'node': '/soc@0/remoteproc@32300000',
             'firmware_name': 'qcom/sm8450/xiaomi/cupid/cdsp.mdt', 'version_prefix': 'CDSP.'},
}
STRING = re.compile(rb'[ -~]{6,}')
REQUIRED_COMMANDS = {'INIT', 'OPEN', 'CLOSE', 'START', 'STOP', 'DRAIN', 'RESUME', 'BUFFER', 'POWER_COLLAPSE'}
REQUIRED_PROPERTIES = {'IMAGE_VERSION', 'COLOR_FORMAT', 'LINEAR_STRIDE_SCANLINE'}


def load_manifest(manifest_path, manifest_sha, expected_name):
    manifest = json.loads(CHECK.pinned_read(manifest_path, manifest_sha, 65536),
                          object_pairs_hook=CHECK.unique_json)
    if (not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or
            manifest['schema'] != 1 or manifest.get('board') != 'xiaomi,cupid' or
            manifest.get('soc') != 'sm8450' or manifest.get('firmware_name') != expected_name):
        raise ValueError('Firmware manifest schema/board/name mismatch')
    rows = manifest.get('files')
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        raise ValueError('Invalid bounded firmware file list')
    directory = Path(manifest_path).absolute().parent
    files = {}
    for row in rows:
        if not isinstance(row, dict) or set(row) != {'name', 'size', 'sha256'}:
            raise ValueError('Malformed firmware file record')
        name = row['name']
        if (not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or
                name in ('.', '..', 'manifest.json') or name in files or
                type(row['size']) is not int or not 0 <= row['size'] <= CHECK.MAX_FILE):
            raise ValueError('Invalid or duplicate firmware file name/size')
        data = CHECK.pinned_read(directory / name, row['sha256'])
        if len(data) != row['size']:
            raise ValueError('Firmware file size mismatch')
        files[name] = data
    if {path.name for path in directory.iterdir()} != set(files) | {Path(manifest_path).name}:
        raise ValueError('Extra or missing firmware directory entry')
    return manifest, files


def strings_of(blobs):
    return sorted({match.group().decode('ascii') for data in blobs
                   for match in STRING.finditer(data)})


def identity(strings):
    values = {}
    for key, prefix in (('firmware_version', 'QC_IMAGE_VERSION_STRING='),
                        ('oem_version', 'OEM_IMAGE_VERSION_STRING='),
                        ('build_date', 'BUILD_DATE='), ('build_time', 'BUILD_TIME=')):
        found = {text[len(prefix):] for text in strings if text.startswith(prefix)}
        if len(found) > 1:
            raise ValueError('Conflicting firmware identity strings: ' + key)
        if found:
            values[key] = found.pop()
    return values


def pinned_inputs(path, digest, component):
    inputs = json.loads(CHECK.pinned_read(path, digest, 65536), object_pairs_hook=CHECK.unique_json)
    if not isinstance(inputs, dict) or set(inputs) != {'schema', 'component', 'files'} or inputs['schema'] != 1 or type(inputs['schema']) is not int or inputs['component'] != component:
        raise ValueError('Invalid runtime input specification')
    rows = inputs['files']
    if not isinstance(rows, dict) or set(rows) - {'driver', 'platform_header', 'hfi_header', 'loader'}:
        raise ValueError('Invalid runtime input roles')
    if component == 'video' and not {'driver', 'platform_header', 'hfi_header'} <= set(rows):
        raise ValueError('Missing mandatory video protocol input')
    result = {}
    for role, row in rows.items():
        if not isinstance(row, dict) or set(row) != {'path', 'sha256'}:
            raise ValueError('Malformed runtime input record')
        raw = CHECK.pinned_read(Path(row['path']), row['sha256'], 2 * 1024 * 1024)
        if not raw.strip():
            raise ValueError('Empty runtime input: ' + role)
        result[role] = raw.decode('utf-8')
    return result, rows


def definitions(text, prefix):
    rows = {}
    for match in re.finditer(r'^#define\s+' + re.escape(prefix) + r'([A-Z0-9_]+)\s+(0x[0-9a-fA-F]+|[0-9]+)\s*$', text, re.M):
        name, value = match.groups()
        value = int(value, 0)
        if name in rows and rows[name] != value:
            raise ValueError('Conflicting driver protocol definition: ' + name)
        rows[name] = value
    return rows


def driver_expectations(inputs):
    blocks = re.findall(r'struct iris_platform_data sm8450_data\s*=\s*\{(.*?)\n\};', inputs['driver'], re.S)
    if len(blocks) != 1:
        raise ValueError('Missing or ambiguous SM8450 driver data')
    names = re.findall(r'\.fwname\s*=\s*"([^"]+)"', blocks[0])
    if names != [COMPONENTS['video']['firmware_name']]:
        raise ValueError('Patched SM8450 driver firmware name differs from manifest')
    if re.findall(r'\.pas_id\s*=\s*(\w+)\s*,', blocks[0]) != ['IRIS_PAS_ID']:
        raise ValueError('SM8450 driver does not select the verified PAS constant')
    pas = re.findall(r'^#define\s+IRIS_PAS_ID\s+(\d+)\s*$', inputs['platform_header'], re.M)
    if pas != ['9']:
        raise ValueError('Patched driver PAS ID mismatch')
    commands = definitions(inputs['hfi_header'], 'HFI_CMD_')
    properties = definitions(inputs['hfi_header'], 'HFI_PROP_')
    if not REQUIRED_COMMANDS <= commands.keys() or not REQUIRED_PROPERTIES <= properties.keys():
        raise ValueError('Missing mandatory HFI definitions')
    return commands, properties


def audit(manifest_path, manifest_sha, dtb_path, dtb_sha, inputs_path, inputs_sha, component='video'):
    config = COMPONENTS[component]
    prefix, machine = config['prefix'], config['machine']
    manifest, files = load_manifest(manifest_path, manifest_sha, config['firmware_name'])
    nodes = CHECK.parse_dtb(CHECK.pinned_read(dtb_path, dtb_sha, CHECK.MAX_DTB))
    region = CHECK.memory_region(nodes, config['node'])
    structure = CHECK.validate_mdt(files, prefix, machine, region)
    programs = CHECK.elf_programs(files[prefix + '.mdt'], machine)
    loads = [row for row in programs if row['type'] == 1 and row['memsz'] and
             row['flags'] & CHECK.TYPE_MASK != CHECK.TYPE_HASH]
    required = [prefix + '.b%02d' % row['index'] for row in loads if row['filesz']]
    if 'expected_segments' in manifest and manifest['expected_segments'] != required:
        raise ValueError('Manifest expected segment list differs from ELF requirements')
    if 'mdt' in manifest:
        if manifest['mdt'].get('machine') != machine or manifest['mdt'].get('loadable') != sum(row['type'] == 1 for row in programs):
            raise ValueError('Manifest ELF summary conflicts with observed headers')
    inputs, rows = pinned_inputs(inputs_path, inputs_sha, component)
    strings = strings_of([files[prefix + '.b%02d' % row['index']] for row in loads if row['filesz']])
    version = identity(strings)
    if not version.get('firmware_version', '').startswith(config['version_prefix']):
        raise ValueError('Missing or malformed firmware version string')
    report = {**structure, **version, 'component': component,
              'manifest_sha256': manifest_sha, 'dtb_sha256': dtb_sha,
              'input_spec_sha256': inputs_sha, 'input_sha256': {role: row['sha256'] for role, row in rows.items()},
              'pinned_files': len(files), 'file_hashes_verified': True,
              'firmware_name': config['firmware_name'], 'required_segments': required,
              'elf_machine': machine, 'program_headers': len(programs),
              'static_consistency_passed': True, 'static_pairing_consistent': False,
              'protocol_abi_verified': False, 'signature_verified': False,
              'runtime_pairing_verified': False, 'firmware_authentication_bypassed': False,
              'loader_provenance_status': 'pinned-input' if 'loader' in inputs else 'unknown',
              'secure_loader_acceptance': 'unverified', 'secure_loader_required': True,
              'hardware_ready': False, 'phone_operated': False}
    if component == 'video':
        commands, properties = driver_expectations(inputs)
        found = [name for name in sorted(commands) if any(re.search(r'\bHFI_CMD_' + re.escape(name) + r'\b', text) for text in strings)]
        report.update(driver_pas_id=9, driver_text_consistent=True,
                      hfi_command_definitions=commands, hfi_property_definitions=properties,
                      hfi_command_text_found=found,
                      format_text_evidence={name: any('HFI_COLOR_FORMAT_' + name in text for text in strings)
                                            for name in ('NV12', 'NV12_UBWC', 'P010', 'YUV420_TP10_UBWC')})
    else:
        report.update(driver_pairing_checked=False,
                      fastrpc_shell_text_evidence={name: any(name in text for text in strings)
                                                  for name in ('fastrpc_shell_3', 'fastrpc_shell_unsigned_3')})
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--component', choices=sorted(COMPONENTS), default='video')
    parser.add_argument('--manifest', required=True, type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--dtb', required=True, type=Path)
    parser.add_argument('--dtb-sha256', required=True)
    parser.add_argument('--inputs', required=True, type=Path)
    parser.add_argument('--inputs-sha256', required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        report = audit(args.manifest, args.manifest_sha256, args.dtb, args.dtb_sha256,
                       args.inputs, args.inputs_sha256, args.component)
    except (OSError, ValueError, RuntimeError, KeyError, struct.error) as error:
        report = {'component': args.component, 'static_consistency_passed': False,
                  'static_pairing_consistent': False, 'protocol_abi_verified': False,
                  'runtime_pairing_verified': False, 'signature_verified': False,
                  'hardware_ready': False, 'error': str(error)}
    text = json.dumps(report, indent=2) + '\n'
    if args.out:
        args.out.write_text(text)
    print(text)
    return 0 if report['static_consistency_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
