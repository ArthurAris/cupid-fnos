#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Build modules for the verified RAM-boot candidate, not the restored baseline.
set -euo pipefail
[[ $# == 2 ]] || { printf 'Usage: %s KERNEL_TREE CANDIDATE_DIRECTORY\n' "$0" >&2; exit 2; }
tree=$(realpath "$1")
candidate=$(realpath "$2")
[[ -r $candidate/config.built && -r $candidate/Image.lio ]] || exit 2
[[ ! -e $candidate/modules-candidate ]] || { printf 'Module output exists; refusing overwrite.\n' >&2; exit 2; }
exec 9>"$tree/.cupid-build.lock"
flock -n 9 || { printf 'Another cupid build is running.\n' >&2; exit 2; }
cmp "$tree/.config" "$candidate/active-config.saved"
cp "$tree/.config" "$candidate/config.before-modules"
restore() {
    rc=$?
    trap - EXIT
    cp "$candidate/config.before-modules" "$tree/.config"
    printf 'BASELINE_RESTORED MODULE_BUILD_EXIT=%s\n' "$rc"
    exit "$rc"
}
trap restore EXIT
cp "$candidate/config.built" "$tree/.config"
make -C "$tree" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    CC='ccache aarch64-linux-gnu-gcc' CUPID_DTB=sm8450-xiaomi-cupid-v-touch -j16 modules
make -C "$tree" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- \
    INSTALL_MOD_PATH="$candidate/modules-candidate" modules_install
release=$(make -s -C "$tree" ARCH=arm64 CROSS_COMPILE=aarch64-linux-gnu- kernelrelease)
module="$candidate/modules-candidate/lib/modules/$release/kernel/fs/btrfs/btrfs.ko"
[[ -f $module ]] || exit 1
modinfo "$module"
# Exclude build-tree links; archive only built modules and dependency indexes.
tar -czf "$candidate/modules-candidate.tar.gz" \
    --exclude='*/build' --exclude='*/source' \
    -C "$candidate/modules-candidate" lib
sha256sum "$candidate/modules-candidate.tar.gz" | tee "$candidate/modules-candidate.sha256"
printf 'CANDIDATE_MODULES_BUILT=%s\n' "$release"
