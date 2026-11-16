#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# 创建 initramfs 源目录（给 CONFIG_INITRAMFS_SOURCE 用）。
#
# 用法（WSL/Linux 内运行）：
#   ./tools/initramfs/build-initramfs.sh [目标目录]
# 目标目录与 BUSYBOX 均由调用者显式提供；不使用本机默认路径。
#
# 产物是一个目录（不是 cpio）：内核构建时会把该目录编进 Image。
set -eu

HERE=$(cd -- "$(dirname -- "$0")" && pwd)
REPO=$(cd -- "$HERE/../.." && pwd)
R=${1:?provide an external initramfs output directory}
BUSYBOX=${BUSYBOX:?provide a trusted external static AArch64 busybox}

if [ -e "$R" ] && [ -n "$(ls -A "$R" 2>/dev/null)" ]; then
    echo "refusing to overwrite non-empty $R" >&2
    exit 1
fi

mkdir -p "$R"/bin "$R"/sbin "$R"/etc "$R"/proc "$R"/sys "$R"/dev/pts \
         "$R"/tmp "$R"/run "$R"/newroot

[ -f "$BUSYBOX" ] || { printf 'Missing external busybox; no download is performed\n' >&2; exit 1; }
install -m 0755 "$BUSYBOX" "$R/bin/busybox"
for app in sh ls cat mount umount mkdir sleep dmesg grep ip kill ln pwd reboot \
           poweroff dd sync tr readlink touch ps free uname; do
    ln -sf busybox "$R/bin/$app"
done

echo "compiling init (static)..."
aarch64-linux-gnu-gcc -static -Wall -Wextra -Werror -O2 \
    -o "$R/init" "$HERE/init_static_v21.c"
chmod 0755 "$R/init"

echo "built: $R"
echo "next: scripts/config --set-str INITRAMFS_SOURCE \"$R\" && make olddefconfig"
