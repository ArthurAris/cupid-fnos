#!/bin/bash
# SPDX-License-Identifier: GPL-2.0-only
# Run only against a newly-created disposable image, never a supplied device.
set -euo pipefail
[[ $# == 0 ]] || { printf 'This test accepts no device arguments.\n' >&2; exit 2; }
[[ $EUID == 0 ]] || { printf 'Run in Linux as root.\n' >&2; exit 2; }
filesystem=${CUPID_TEST_FILESYSTEM:-ext4}
adapter=${CUPID_TEST_ADAPTER:-}
if [[ -n $adapter ]]; then
    [[ -f $adapter ]] || { printf 'Adapter path does not exist.\n'; exit 2; }
fi
case $filesystem in ext4|btrfs) ;; *) printf 'Unsupported test filesystem.\n' >&2; exit 2 ;; esac
for tool in losetup sfdisk blockdev python3 modprobe sha256sum "mkfs.$filesystem"; do
    command -v "$tool" >/dev/null || { printf 'Missing: %s\n' "$tool" >&2; exit 2; }
done
work=$(mktemp -d "${CUPID_TEST_BASE:?provide an external trusted ext4 scratch directory}/cupid-lio-smoke.XXXXXXXX")
exec > >(tee "$work/result.log") 2>&1
printf 'ARTIFACT_DIR=%s\n' "$work"
id=$$
serial=$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')
core=/sys/kernel/config/target/core/iblock_$id
target=/sys/kernel/config/target/loopback/naa.6${serial:0:15}
tpg=$target/tpgt_1
loop=
exported=
mounted=0
export_live=0
cleanup() {
    rc=$?
    trap - EXIT
    set +e
    if [[ $mounted == 1 ]]; then umount "$work/mnt"; fi
    if [[ $export_live == 1 && -d $tpg ]]; then
        rm "$tpg/lun/lun_1/data"
        printf 'NULL\n' > "$tpg/nexus"
        if [[ -n $adapter ]]; then storage=$core/cupid-data; else storage=$core/data; fi
        rmdir "$tpg/lun/lun_1" "$tpg" "$target" "$storage" "$core"
    fi
    if [[ -n $loop ]]; then losetup -d "$loop"; fi
    printf 'TEST_EXIT=%s\n' "$rc"
    exit "$rc"
}
trap cleanup EXIT
modprobe target_core_mod
modprobe target_core_iblock
modprobe tcm_loop
if ! mountpoint -q /sys/kernel/config; then
    mount -t configfs configfs /sys/kernel/config
fi
[[ -d /sys/kernel/config/target/loopback ]] || mkdir /sys/kernel/config/target/loopback
if [[ -n $adapter ]]; then
    core=/sys/kernel/config/target/core/iblock_$((16#${serial: -8}))
    target=/sys/kernel/config/target/loopback/naa.6${serial:0:15}
    tpg=$target/tpgt_1
fi
[[ ! -e $core && ! -e $target ]] || { printf 'Configfs name collision.\n'; exit 2; }
truncate -s 1G "$work/parent.img"
loop=$(losetup --find --show --partscan --sector-size 4096 "$work/parent.img")
printf 'label: gpt\nunit: sectors\n\nstart=256, size=32768, type=L, name=system-test\nstart=33024, size=196608, type=L, name=data-test\n' | sfdisk "$loop"
udevadm settle --timeout=10
backing=${loop}p2
[[ -b $backing ]] || { printf 'Missing backing partition: %s\n' "$backing"; exit 1; }
[[ $(blockdev --getsize64 "$backing") == 805306368 ]] || exit 1
# Put sentinels outside the exported partition, including the mock system.
python3 -I - "$work/parent.img" <<'PY'
import os, sys
with open(sys.argv[1], 'r+b', buffering=0) as f:
    for offset in (1048576, 32*1024*1024, 960*1024*1024):
        f.seek(offset)
        f.write(os.urandom(4096))
    os.fsync(f.fileno())
PY
outside_hash() {
    python3 -I - "$work/parent.img" <<'PY'
import hashlib, sys
h = hashlib.sha256()
with open(sys.argv[1], 'rb') as f:
    for begin, end in ((0, 33024*4096), (229632*4096, 1024**3)):
        f.seek(begin)
        remaining = end-begin
        while remaining:
            block = f.read(min(remaining, 1024**2))
            if not block:
                raise RuntimeError('Unexpected end of parent image')
            h.update(block)
            remaining -= len(block)
print(h.hexdigest())
PY
}
outside_before=$(outside_hash)
create_export() {
    mode=$1
    if [[ -n $adapter ]]; then
        python3 -I - "$backing" "$loop" "$serial" "$mode" "$work/adapter.json" <<'PY'
import json, pathlib, subprocess, sys
backing, parent, serial, mode, destination = sys.argv[1:]
sysdev = pathlib.Path('/sys/class/block') / pathlib.Path(backing).name
def value(device, key):
    return subprocess.check_output(['blkid', '-p', '-s', key, '-o', 'value', device], text=True).strip().lower()
config = dict(backing_partuuid=value(backing, 'PART_ENTRY_UUID'),
              parent_ptuuid=value(parent, 'PTUUID'),
              start_sector_512=int((sysdev/'start').read_text()),
              size_sector_512=int((sysdev/'size').read_text()),
              logical_block_size=int(subprocess.check_output(['blockdev', '--getss', backing], text=True)),
              virtual_serial=serial, readonly=bool(int(mode)))
pathlib.Path(destination).write_text(json.dumps(config))
PY
        python3 -I "$adapter" verify --config "$work/adapter.json"
        exported=$(python3 -I "$adapter" start --config "$work/adapter.json")
        export_live=1
        again=$(python3 -I "$adapter" start --config "$work/adapter.json")
        [[ $again == "$exported" ]] || { printf 'Idempotent start changed device.\n'; exit 1; }
        printf 'PASS adapter start idempotence\n'
        printf 'SCSI_DISK=%s SERIAL=%s RO=%s\n' "$exported" "$serial" "$(blockdev --getro "$exported")"
        return
    fi
    mkdir "$core" "$core/data" "$target" "$tpg"
    export_live=1
    printf 'udev_path=%s,readonly=%s\n' "$backing" "$mode" > "$core/data/control"
    printf '1\n' > "$core/data/enable"
    printf '%s\n' "$serial" > "$core/data/wwn/vpd_unit_serial"
    printf 'naa.6%s\n' "${serial:0:15}" > "$tpg/nexus"
    mkdir "$tpg/lun/lun_1"
    ln -s "$core/data" "$tpg/lun/lun_1/data"
    address=$(tr -d '\n' < "$tpg/address")
    for ((i=0; i<50; i++)); do
        candidates=(/sys/class/scsi_device/"$address":1/device/block/*)
        if [[ ${#candidates[@]} == 1 && -d ${candidates[0]} ]]; then
            exported=/dev/${candidates[0]##*/}
            capacity=$(blockdev --getsize64 "$exported" 2>/dev/null || true)
            [[ $capacity == 805306368 ]] && break
        fi
        sleep .1
    done
    [[ -b $exported ]] || { printf 'No SCSI disk appeared.\n'; exit 1; }
    [[ $(blockdev --getsize64 "$exported") == 805306368 ]] || exit 1
    printf 'SCSI_DISK=%s ADDRESS=%s SERIAL=%s RO=%s\n' "$exported" "$address" "$serial" "$(blockdev --getro "$exported")"
}
drop_export() {
    if [[ -n $adapter ]]; then
        python3 -I "$adapter" stop --config "$work/adapter.json"
        export_live=0
        exported=
        return
    fi
    rm "$tpg/lun/lun_1/data"
    printf 'NULL\n' > "$tpg/nexus"
    rmdir "$tpg/lun/lun_1" "$tpg" "$target" "$core/data" "$core"
    export_live=0
    exported=
}
create_export 1
[[ $(blockdev --getro "$exported") == 1 ]]
python3 -I - "$exported" <<'PY'
import errno, os, sys
fd = None
try:
    fd = os.open(sys.argv[1], os.O_RDWR)
    os.pwrite(fd, b'R'*4096, 0)
except OSError as e:
    assert e.errno in (errno.EROFS, errno.EACCES, errno.EPERM), e
    print('PASS readonly SCSI writes rejected')
else:
    raise RuntimeError('Read-only write unexpectedly succeeded')
finally:
    if fd is not None:
        os.close(fd)
PY
drop_export
create_export 0
[[ $(blockdev --getro "$exported") == 0 ]]
if [[ ${CUPID_TEST_DEVICE_POLICY:-0} == 1 ]]; then
    probe=$(realpath "$(dirname "$0")/policy_probe.py")
    systemd-run --quiet --wait --pipe --collect \
        --unit="cupid-lio-policy-$id" \
        -p DevicePolicy=closed -p "DeviceAllow=$loop r" \
        -p "DeviceAllow=$exported rw" \
        /usr/bin/python3 -I "$probe" "$loop" "$exported"
fi
python3 -I - "$exported" <<'PY'
import errno, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    size = os.lseek(fd, 0, os.SEEK_END)
    os.pwrite(fd, b'L'*4096, size-4096)
    os.fsync(fd)
    assert os.pread(fd, 4096, size-4096) == b'L'*4096
    assert os.pread(fd, 4096, size) == b''
    try:
        os.pwrite(fd, b'X'*4096, size)
    except OSError as e:
        assert e.errno in (errno.ENOSPC, errno.EIO, errno.EINVAL), e
    else:
        raise RuntimeError('Out-of-bounds write succeeded')
    print('PASS last LBA works; out-of-bounds write rejected')
finally:
    os.close(fd)
PY
printf 'label: gpt\n\nsize=700MiB, type=L, name=fnos-test\n' | sfdisk "$exported"
udevadm settle --timeout=10
properties=$(udevadm info --query=property --name "$exported")
serial_property=$(printf '%s\n' "$properties" | grep -E '^ID_(SCSI_SERIAL|SERIAL_SHORT)=' | grep -F "=$serial" || true)
[[ -n $serial_property ]] || { printf 'VPD serial was not preserved:\n%s\n' "$properties"; exit 1; }
part=${exported}1
[[ -b $part && -d /sys/class/block/${exported##*/}/${part##*/} ]] || {
    printf 'Missing real partition/sysfs child.\n'; exit 1;
}
if [[ $filesystem == btrfs ]]; then
    modprobe btrfs
    mkfs.btrfs -q -L fnos-test "$part"
else
    mkfs.ext4 -q -m 0 -L fnos-test "$part"
fi
mkdir "$work/mnt"
mount "$part" "$work/mnt"
mounted=1
if [[ -n $adapter ]]; then
    if python3 -I "$adapter" stop --config "$work/adapter.json"; then
        printf 'Adapter incorrectly stopped a mounted export.\n'; exit 1
    fi
    printf 'PASS adapter refuses to dismantle mounted storage\n'
fi
python3 -I - "$work/mnt" <<'PY'
import os, sys
path = os.path.join(sys.argv[1], '\u4e2d\u6587\u6d4b\u8bd5.dat')
with open(path, 'wb') as f:
    f.write(os.urandom(4*1024**2))
    f.flush()
    os.fsync(f.fileno())
PY
if [[ $filesystem == btrfs ]]; then
    btrfs subvolume create "$work/mnt/source"
    printf 'snapshot content\n' > "$work/mnt/source/probe"
    btrfs subvolume snapshot -r "$work/mnt/source" "$work/mnt/snapshot"
    cmp "$work/mnt/source/probe" "$work/mnt/snapshot/probe"
    btrfs scrub start -B "$work/mnt"
    printf 'PASS Btrfs subvolume, read-only snapshot, and scrub\n'
fi
file_before=$(find "$work/mnt" -maxdepth 1 -type f -exec sha256sum {} \; | cut -d' ' -f1)
sync
umount "$work/mnt"
mounted=0
if [[ $filesystem == btrfs ]]; then
    btrfs check --readonly "$part"
else
    e2fsck -fn "$part"
fi
printf 'PASS real GPT partition and %s read/write\n' "$filesystem"
drop_export
create_export 0
udevadm settle --timeout=10
serial_property=$(udevadm info --query=property --name "$exported" | grep -E '^ID_(SCSI_SERIAL|SERIAL_SHORT)=' | grep -F "=$serial" || true)
[[ -n $serial_property ]] || exit 1
part=${exported}1
if [[ $filesystem == btrfs ]]; then
    mount -o ro "$part" "$work/mnt"
else
    mount -o ro,noload "$part" "$work/mnt"
fi
mounted=1
file_after=$(find "$work/mnt" -maxdepth 1 -type f -exec sha256sum {} \; | cut -d' ' -f1)
[[ $file_before == "$file_after" ]]
umount "$work/mnt"
mounted=0
outside_after=$(outside_hash)
[[ $outside_before == "$outside_after" ]]
printf 'PASS export recreation retains file hashes and serial\n'
printf 'PASS entire non-exported parent regions unchanged: %s\n' "$outside_after"
printf 'LOOPBACK_SMOKE_OK\n'
