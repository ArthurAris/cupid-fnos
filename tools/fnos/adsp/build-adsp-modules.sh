#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Build the pinned ten-module ADSP chain in new directories; never load modules.
# Usage: build-adsp-modules.sh <official-headers> <upstream-6.18.18> <local-7.2.8> <new-output-dir>
set -euo pipefail

BASEHDR="${1:?official headers}"
UP="${2:?upstream v6.18.18}"
L7="${3:?local 7.2.8}"
DIST="${4:?new output directory}"
SOURCE=$(dirname "$(readlink -f "$0")")
CROSS_COMPILE=${CROSS_COMPILE:-aarch64-linux-gnu-}
CC=${CC:-${CROSS_COMPILE}gcc}
HOSTCC=${HOSTCC:-gcc}
JOBS=${JOBS:-2}
[[ $JOBS =~ ^[1-8]$ ]] || { printf 'JOBS must be an integer from 1 to 8\n' >&2; exit 1; }
SYSROOT=${SYSROOT:?provide a trusted absolute external fnOS userspace sysroot for qemu build helpers}
[[ $SYSROOT == /* && -d $SYSROOT && ! -L $SYSROOT && $SYSROOT != *$'\n'* ]] || { printf 'Invalid external SYSROOT\n' >&2; exit 1; }
export CUPID_ADSP_SYSROOT="$SYSROOT"
export QEMU_LD_PREFIX="$SYSROOT"
${CC} --version >/dev/null || { printf 'Target compiler unavailable\n' >&2; exit 1; }
${HOSTCC} --version >/dev/null || { printf 'Host compiler unavailable\n' >&2; exit 1; }
[ ! -e "$DIST" ] && [ ! -L "$DIST" ] || { printf 'Output must not exist\n' >&2; exit 1; }
WORK=$(mktemp -d /var/tmp/cupid-adsp-build-XXXXXX)
MOD="$WORK/modules"
mkdir "$MOD"

# Check all pinned inputs before invoking any compiler from the copied header tree.
python3 -I -B "$SOURCE/prepare-build.py" prepare "$BASEHDR" "$UP" "$L7" "$MOD"
cp -a "$BASEHDR" "$WORK/headers"
while IFS= read -r f; do
    case "$f" in *.aarch64) continue ;; esac
    if file -b "$f" | grep -q 'ELF 64-bit.*ARM aarch64'; then
        [ ! -e "$f.aarch64" ] || { printf 'Ambiguous build tool\n' >&2; exit 1; }
        mv "$f" "$f.aarch64"
    fi
    if [ -f "$f.aarch64" ]; then
        printf '#!/bin/sh\n: "${CUPID_ADSP_SYSROOT:?external sysroot required}"\nexec /usr/bin/qemu-aarch64-static -L "$CUPID_ADSP_SYSROOT" "%s" "$@"\n' "$f.aarch64" > "$f"
        chmod 0755 "$f"
    fi
done < <(find "$WORK/headers/scripts" -type f)

if ! make -f "$WORK/headers/Makefile" M="$MOD" MO="$MOD" ARCH=arm64 \
    CROSS_COMPILE="$CROSS_COMPILE" CC="$CC" HOSTCC="$HOSTCC" -j"$JOBS" modules > "$WORK/build.log" 2>&1; then
    printf 'BUILD_FAILED log=%s\n' "$WORK/build.log" >&2
    exit 1
fi
mkdir "$DIST"
python3 -I -B "$SOURCE/prepare-build.py" verify "$BASEHDR" "$MOD" "$DIST"
printf 'RESULT_DIR=%s\nWORK_DIR=%s\n' "$DIST" "$WORK"
