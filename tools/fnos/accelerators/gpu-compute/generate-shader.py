#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile the checked-in GLSL in a new directory; never synthesize substitute SPIR-V."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='New output directory')
    parser.add_argument('--compiler', type=Path)
    parser.add_argument('--validator', type=Path)
    args = parser.parse_args()
    compiler = args.compiler or (Path(shutil.which('glslangValidator')) if shutil.which('glslangValidator') else None)
    validator = args.validator or (Path(shutil.which('spirv-val')) if shutil.which('spirv-val') else None)
    if (not compiler or not compiler.is_absolute() or not compiler.is_file() or
            not validator or not validator.is_absolute() or not validator.is_file()):
        raise SystemExit('BLOCKED: trusted glslangValidator and spirv-val required; no shader or software fallback generated')
    if args.output.exists():
        raise SystemExit('Output must not exist')
    source = Path(__file__).with_name('add.comp')
    with tempfile.TemporaryDirectory(prefix='cupid-shader-build-') as temp:
        work = Path(temp)
        (work / 'add.comp').write_bytes(source.read_bytes())
        subprocess.run([str(compiler), '-V', '--target-env', 'vulkan1.0', '-S', 'comp',
                        '-o', str(work / 'add.spv'), str(work / 'add.comp')], cwd=work, check=True, timeout=30)
        subprocess.run([validator, '--target-env', 'vulkan1.0', str(work / 'add.spv')], cwd=work, check=True, timeout=10)
        data = (work / 'add.spv').read_bytes()
        version = subprocess.check_output([str(compiler), '--version'], cwd=work, text=True, timeout=5)
        args.output.mkdir(parents=True)
        (args.output / 'add.spv').write_bytes(data)
        report = {'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                  'spirv_sha256': hashlib.sha256(data).hexdigest(), 'compiler': str(compiler),
                  'compiler_version': version, 'spirv_validated': True,
                  'hardware_tested': False, 'phone_loaded': False}
        (args.output / 'shader-manifest.json').write_text(json.dumps(report, indent=2) + '\n')
        print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
