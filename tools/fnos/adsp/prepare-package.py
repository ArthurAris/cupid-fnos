#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Prepare a clean offline package; output SHA is a review artifact, not approval.

The caller must independently review and accept/pin package-manifest.json before
passing its hash to the trusted install wrapper. No signing/authentication occurs.
Usage: python3 -I /trusted/path/prepare-package.py /clean/stage
Stage contains exactly the 16 payload files (or a previously prepared 18-file
package); an empty enabled sentinel and package-manifest.json are generated.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import stat
import sys

# Explicit trusted sibling import works under -I; never import from staging/cwd.
spec = importlib.util.spec_from_file_location('cupid_deploy', Path(__file__).with_name('deploy.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def prepare(stage):
    stage = Path(os.path.abspath(stage))
    for path in [stage, *stage.parents]:
        if path.is_symlink():
            raise deploy.Refusal('Symlink staging path')
    if not stage.is_dir():
        raise deploy.Refusal('Staging directory missing')
    names = {path.name for path in stage.iterdir()}
    required = set(deploy.FILES) - {'enabled'}
    if not required <= names or names - required - {'enabled', 'package-manifest.json'}:
        raise deploy.Refusal('Use a clean fixed-payload staging directory; no extra files')
    payloads = {name: deploy.staging_file(stage / name) for name in names}
    deploy.validate_modules(payloads['module-manifest.json'], payloads)
    deploy.validate_unit(payloads['cupid-adsp.service'])
    if payloads.get('enabled', b'') != b'':
        raise deploy.Refusal('Enabled sentinel must be empty')
    payloads['enabled'] = b''
    value = {
        'schema': 1, 'kernel': deploy.KERNEL, 'board': deploy.BOARD, 'release': deploy.RELEASE,
        'files': [
            {'file': name, 'target': target, 'mode': mode, 'sha256': deploy.sha(payloads[name])}
            for name, (target, mode) in sorted(deploy.FILES.items())
        ],
    }
    # Atomic replacement in staging, without executing any staging content.
    for name, data in [('enabled', b''), ('package-manifest.json', deploy.json_bytes(value))]:
        path = stage / name
        tmp = stage / ('.' + name + '.prepare-' + deploy.uuid.uuid4().hex)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                os.fchmod(stream.fileno(), 0o644)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
            deploy.Filesystem.sync(stage)
        finally:
            if tmp.exists():
                tmp.unlink()
    accepted_candidate = deploy.sha((stage / 'package-manifest.json').read_bytes())
    deploy.validate_package(stage, accepted_candidate)
    print('PACKAGE_REVIEW_SHA256=' + accepted_candidate)
    print('Not authenticated/accepted by this tool; review and externally pin before install.')
    return accepted_candidate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage')
    args = parser.parse_args()
    try:
        prepare(args.stage)
    except (OSError, RuntimeError) as exc:
        print('FAIL: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
