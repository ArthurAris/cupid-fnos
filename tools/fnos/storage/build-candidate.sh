#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
set -euo pipefail
[[ $# == 2 ]] || { printf 'Usage: %s KERNEL_TREE CANDIDATE_DIRECTORY\n' "$0" >&2; exit 2; }
tree=$(realpath "$1")
candidate=$(realpath "$2")
[[ -r $candidate/config.baseline && -r $candidate/.config ]] || exit 2
[[ ! -e $candidate/active-config.saved && ! -e $candidate/Image.before ]] || {
    printf 'Candidate already used; keep its artifacts and prepare a new directory.\n' >&2
    exit 2
}
exec 9>"$tree/.cupid-build.lock"
flock -n 9 || { printf 'Another cupid build is running.\n' >&2; exit 2; }
cmp "$tree/.config" "$candidate/config.baseline"
python3 -I - "$candidate/config.baseline" "$candidate/.config" <<'PY'
from pathlib import Path
import sys

def parse(path):
    result = {}
    for line in Path(path).read_text().splitlines():
        if line.startswith('CONFIG_') and '=' in line:
            key, value = line.split('=', 1)
            result[key] = value
        elif line.startswith('# CONFIG_') and line.endswith(' is not set'):
            result[line[2:-11]] = 'n'
    return result

before, after = map(parse, sys.argv[1:])
allowed = {'CONFIG_TARGET_CORE', 'CONFIG_TCM_IBLOCK', 'CONFIG_LOOPBACK_TARGET',
           'CONFIG_BLK_DEV_INTEGRITY', 'CONFIG_SGL_ALLOC', 'CONFIG_CRC64',
           'CONFIG_CRC64_ARCH', 'CONFIG_CRC_T10DIF', 'CONFIG_CRC_T10DIF_ARCH',
           'CONFIG_MD_LLBITMAP'}
for key in before.keys() | after.keys():
    old, new = before.get(key, 'n'), after.get(key, 'n')
    if old != new:
        print(f'{key}: {old} -> {new}')
        if key not in allowed or new != 'y':
            raise SystemExit('Unexpected config change; refusing build')
for key in ('CONFIG_ARCH_QCOM', 'CONFIG_CUPID_FORCE_BUILTIN_DTB',
            'CONFIG_CUPID_EARLY_FB', 'CONFIG_MD_LLBITMAP',
            'CONFIG_TARGET_CORE', 'CONFIG_TCM_IBLOCK', 'CONFIG_LOOPBACK_TARGET'):
    if after.get(key) != 'y':
        raise SystemExit(f'Missing critical option: {key}')
if after.get('CONFIG_CPU_IDLE', 'n') != 'n':
    raise SystemExit('CPU_IDLE must remain disabled')
PY
cp "$tree/.config" "$candidate/active-config.saved"
cp "$tree/arch/arm64/boot/Image" "$candidate/Image.before"
sha256sum "$candidate/active-config.saved" "$candidate/Image.before" > "$candidate/baseline.sha256"
cp "$candidate/.config" "$tree/.config"
restore_config() {
    rc=$?
    trap - EXIT
    cp "$candidate/active-config.saved" "$tree/.config"
    printf 'ACTIVE_CONFIG_RESTORED build_exit=%s\n' "$rc"
    exit "$rc"
}
trap restore_config EXIT
make -C "$tree" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    CC='ccache aarch64-linux-gnu-gcc' \
    CUPID_DTB=sm8450-xiaomi-cupid-v-touch -j16 Image
for name in ARCH_QCOM CUPID_FORCE_BUILTIN_DTB CUPID_EARLY_FB MD_LLBITMAP TARGET_CORE TCM_IBLOCK LOOPBACK_TARGET; do
    grep -qx "CONFIG_$name=y" "$tree/include/config/auto.conf"
done
cp "$tree/arch/arm64/boot/Image" "$candidate/Image.lio"
cp "$tree/include/config/auto.conf" "$candidate/auto.conf.lio"
cp "$tree/.config" "$candidate/config.built"
sha256sum "$candidate/Image.lio" "$candidate/config.built" | tee "$candidate/candidate.sha256"
printf 'CANDIDATE_BUILD_OK\n'
