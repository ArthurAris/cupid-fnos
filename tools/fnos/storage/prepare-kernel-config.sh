#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Produce an isolated candidate config. Do not replace the running build's .config.
set -euo pipefail
[[ $# == 2 ]] || { printf 'Usage: %s KERNEL_TREE NEW_OUTPUT_DIRECTORY\n' "$0" >&2; exit 2; }
tree=$(realpath "$1")
output=$(realpath -m "$2")
[[ -r $tree/.config && -x $tree/scripts/config ]] || exit 2
[[ ! -e $output ]] || { printf 'Output directory already exists.\n' >&2; exit 2; }
[[ -r $tree/drivers/target/loopback/Kconfig ]] || exit 2
mkdir -m 700 "$output"
cp "$tree/.config" "$output/config.baseline"
cp "$tree/.config" "$output/.config"
"$tree/scripts/config" --file "$output/.config" --enable TARGET_CORE --enable TCM_IBLOCK --enable LOOPBACK_TARGET --enable MD_LLBITMAP
[[ -x $tree/scripts/kconfig/conf ]] || { printf 'Build the host Kconfig conf tool first.\n' >&2; exit 2; }
compiler=aarch64-linux-gnu-gcc
pahole_version=0
if command -v pahole >/dev/null 2>&1; then
    pahole_version=$(pahole --version | tr -cd '0-9')
fi
# Resolve only this config; make O= refuses a source tree with existing builds.
(
    cd "$tree"
    env ARCH=arm64 SRCARCH=arm64 srctree="$tree" \
        CC="$compiler" LD=aarch64-linux-gnu-ld NM=aarch64-linux-gnu-nm \
        OBJCOPY=aarch64-linux-gnu-objcopy RUSTC=rustc BINDGEN=bindgen \
        PAHOLE_VERSION="$pahole_version" \
        KERNELVERSION="$(make -s kernelversion)" \
        CC_VERSION_TEXT="$($compiler --version | head -1)" \
        KCONFIG_CONFIG="$output/.config" \
        "$tree/scripts/kconfig/conf" --olddefconfig Kconfig
)
cmp "$tree/.config" "$output/config.baseline"
for name in ARCH_QCOM CUPID_FORCE_BUILTIN_DTB CUPID_EARLY_FB BLK_DEV_MD BLK_DEV_DM MD_LLBITMAP TARGET_CORE TCM_IBLOCK LOOPBACK_TARGET; do
    grep -qx "CONFIG_$name=y" "$output/.config" || { printf 'Missing critical option: %s\n' "$name" >&2; exit 1; }
done
grep -qx '# CONFIG_CPU_IDLE is not set' "$output/.config"
grep -q '^CONFIG_INITRAMFS_SOURCE="[^"]\+"$' "$output/.config"
"$tree/scripts/diffconfig" "$output/config.baseline" "$output/.config" | tee "$output/config.diff"
sha256sum "$output/config.baseline" "$output/.config"
printf 'Candidate only: %s\nReview config.diff before building.\n' "$output/.config"
