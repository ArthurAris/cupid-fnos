#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Build the published Iris module against the included official c1147 headers.
# Usage: build-iris-module.sh <headers-dir> <new-output-dir> [cross-prefix]
# Never loads modules, touches a phone or disables a gate.
set -euo pipefail

HDR=${1:?official c1147 headers directory}
OUT=${2:?new output directory}
CROSS=${3:-aarch64-linux-gnu-}
CC=${CC:-${CROSS}gcc}
HOSTCC=${HOSTCC:-gcc}
JOBS=${JOBS:-2}
[[ $JOBS =~ ^[1-8]$ ]] || { echo "JOBS must be an integer from 1 to 8" >&2; exit 1; }
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(cd -- "$HERE/../.." && pwd)
SRC="$REPO/sources/iris/verified"
PIN=121ac1b2269aa229ec044ff7bc3211ee3a7eea74829f89979603240299ab31a6
[[ -n $CC && -n $HOSTCC ]] || { echo "compiler command required" >&2; exit 1; }
# CC may include flags (for example a trusted sysroot compiler's -B path).
${CC} --version >/dev/null || { echo "target compiler unavailable" >&2; exit 1; }
${HOSTCC} --version >/dev/null || { echo "host compiler unavailable" >&2; exit 1; }

[[ -d $HDR && -f $HDR/Module.symvers && ! -L $HDR ]] || { echo "headers missing" >&2; exit 1; }
[[ $(sha256sum "$HDR/Module.symvers" | cut -d' ' -f1) == "$PIN" ]] || { echo "Module.symvers pin mismatch" >&2; exit 1; }
[[ -d $SRC && -f $SRC/Makefile ]] || { echo "published Iris source missing" >&2; exit 1; }
[[ ! -e $OUT && ! -L $OUT ]] || { echo "output must not exist" >&2; exit 1; }
# Public gates must stay off; refuse a modified tree.
for macro in IRIS_INIT_ONLY_EXECUTION_ENABLED IRIS_INIT_ONLY_PROVIDER_ADMISSION_ENABLED \
             IRIS_DECODE_ONCE_ENABLED IRIS_STOP_OBSERVER_ENABLED IRIS_RELEASE_OBSERVER_ENABLED; do
    grep -Rq "^#define $macro 0$" "$SRC" || { echo "gate not closed: $macro" >&2; exit 1; }
done

WORK=$(mktemp -d /var/tmp/cupid-iris-build-XXXXXX)
mkdir "$WORK/output"
cp -a "$SRC" "$WORK/driver"
python3 -I -B - "$HDR" "$WORK/headers" <<'PY'
from pathlib import Path
import hashlib
import shutil
import sys
source, output = map(Path, sys.argv[1:])
shutil.copytree(source, output)
for path in source.rglob('*'):
    if path.is_file():
        copied = output / path.relative_to(source)
        if path.stat().st_size != copied.stat().st_size or hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(copied.read_bytes()).digest():
            raise SystemExit('Header copy failed byte verification')
PY
[[ -s $WORK/headers/Makefile && -s $WORK/headers/Module.symvers ]] || {
    echo "header copy incomplete" >&2; exit 1; }
rm -rf "$WORK/driver/source-manifest.json" "$WORK/driver/README.md"

# Official headers ship aarch64 helper tools; execute them through qemu when the
# host is not arm64. SYSROOT must be an absolute, trusted aarch64 sysroot.
if [[ $(uname -m) != aarch64 ]]; then
    SYSROOT=${SYSROOT:?provide an external aarch64 sysroot for the header helper tools}
    [[ $SYSROOT == /* && -d $SYSROOT && ! -L $SYSROOT ]] || { echo "invalid SYSROOT" >&2; exit 1; }
    export QEMU_LD_PREFIX="$SYSROOT"
    while IFS= read -r f; do
        if file -b "$f" | grep -q 'ELF 64-bit.*ARM aarch64'; then
            mv "$f" "$f.aarch64"
            printf '#!/bin/sh\nexec /usr/bin/qemu-aarch64-static -L "%s" "%s" "$@"\n' "$SYSROOT" "$f.aarch64" > "$f"
            chmod 0755 "$f"
        fi
    done < <(find "$WORK/headers/scripts" -type f)
fi

make -f "$WORK/headers/Makefile" M="$WORK/driver" MO="$WORK/output" ARCH=arm64 \
     CROSS_COMPILE="$CROSS" CC="$CC" HOSTCC="$HOSTCC" -j"$JOBS" modules > "$WORK/build.log" 2>&1 || {
    echo "BUILD_FAILED log=$WORK/build.log" >&2; exit 1; }

KO="$WORK/output/cupid_qcom_iris.ko"
[[ -f $KO ]] || { echo "module not produced" >&2; exit 1; }
grep -q '0x2fe0a796[[:space:]]*iris_decode_once_run' "$WORK/output/Module.symvers" || {
    echo "decode runner CRC mismatch" >&2; exit 1; }
mkdir "$OUT"
cp "$KO" "$OUT/cupid_qcom_iris.ko"
sha256sum "$KO" > "$OUT/cupid_qcom_iris.ko.sha256"
{
    echo "source=$SRC"
    echo "headers_Module.symvers=$PIN"
    echo "cc=$CC"
    echo "hostcc=$HOSTCC"
    echo "vermagic_expected=6.18.18.c1147-trim SMP mod_unload modversions aarch64"
    echo "gates=0"
    echo "not_loaded=true"
} > "$OUT/build-record.txt"
printf 'RESULT_DIR=%s\nWORK_DIR=%s\n' "$OUT" "$WORK"
