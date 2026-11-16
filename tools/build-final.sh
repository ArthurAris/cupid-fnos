#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# 构建小米12 (cupid) 的 kernel Image 和 boot.img。
#
# 用法（在 WSL/Linux 内运行）：
#   KERNEL_TREE=/absolute/external/linux ./tools/build-final.sh
#
# 需要：
#   - 已应用 patches/kernel/ 的内核树（基线见 patches/kernel/base.txt）
#   - 原厂多 DTB 容器（诱饵 DTB）：默认 out/v47-inspect/dtb
#     可用 magiskboot unpack 原厂 boot.img 后取 dtb 字段
#   - aarch64-linux-gnu- 工具链
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$HERE")
KERNEL_TREE=${KERNEL_TREE:?set KERNEL_TREE to a trusted external patched Linux tree}
OUT=${OUT:-$REPO/out}
DTS=${DTS:-sm8450-xiaomi-cupid-v-touch}
DECOY_DTB=${DECOY_DTB:?set DECOY_DTB to a legally obtained external stock DTB}
IMAGE=${IMAGE:-$OUT/Image-final}
BOOTIMG=${BOOTIMG:-$OUT/boot-final.img}

[ -d "$KERNEL_TREE" ] || { echo "kernel tree not found: $KERNEL_TREE" >&2; exit 1; }
[ -f "$DECOY_DTB" ] || {
    echo "missing decoy DTB: $DECOY_DTB" >&2
    echo "extract it from a stock boot.img (magiskboot unpack -> dtb)" >&2
    exit 1
}
# This source-only release intentionally omits unverified panel/font data.
for input in drivers/gpu/drm/panel/panel-xiaomi-cupid-blobs.h arch/arm64/kernel/cupid_font.h; do
    [ -f "$KERNEL_TREE/$input" ] || { printf 'Missing legally sourced external input: %s\n' "$input" >&2; exit 1; }
done
INITRAMFS_DIR=${INITRAMFS_DIR:?set INITRAMFS_DIR to the prepared external initramfs directory}
[ -d "$INITRAMFS_DIR" ] && [ -x "$INITRAMFS_DIR/init" ] || { printf 'Missing external initramfs/init\n' >&2; exit 1; }
[ ! -e "$IMAGE" ] && [ ! -e "$BOOTIMG" ] || { printf 'Refusing to overwrite build outputs\n' >&2; exit 1; }
mkdir -p "$OUT"
cd "$KERNEL_TREE"
scripts/config --set-str INITRAMFS_SOURCE "$INITRAMFS_DIR"

echo "== config checks =="
grep -q '^CONFIG_CUPID_FORCE_BUILTIN_DTB=y$' .config || {
    echo "CONFIG_CUPID_FORCE_BUILTIN_DTB=y is required" >&2; exit 1; }
grep -q '^# CONFIG_CPU_IDLE is not set$' .config || {
    echo "CONFIG_CPU_IDLE must stay disabled (enabling it blacks out the panel)" >&2; exit 1; }
grep -q '^CONFIG_INITRAMFS_SOURCE=' .config || {
    echo "CONFIG_INITRAMFS_SOURCE is required" >&2; exit 1; }
grep -q "qcom/$DTS.dtb" arch/arm64/boot/dts/qcom/Makefile || {
    echo "dtb-$DTS is not registered in arch/arm64/boot/dts/qcom/Makefile" >&2; exit 1; }

echo "== build DTB ($DTS) =="
export PATH=/usr/lib/ccache:$PATH
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- DTC_FLAGS='-@' "qcom/$DTS.dtb"

echo "== build Image =="
make ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
     CC='ccache aarch64-linux-gnu-gcc' CUPID_DTB="$DTS" -j"$(nproc)" Image
cp arch/arm64/boot/Image "$IMAGE"

echo "== pack boot.img (header v2, pagesize 2048) =="
python3 -I -B "$REPO/tools/mkbootimg/mkbootimg.py" \
    --kernel "$IMAGE" \
    --dtb "$DECOY_DTB" \
    --header_version 2 --pagesize 2048 \
    --base 0x00000000 --kernel_offset 0x10008000 \
    --ramdisk_offset 0x00000000 --tags_offset 0x10000100 \
    --dtb_offset 0x11f00000 \
    --output "$BOOTIMG"

echo "== done =="
sha256sum "$IMAGE" "$BOOTIMG"
echo "boot.img: $BOOTIMG"
