#!/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Read-only inspection; use tools/linux-serial.ps1 for target verification.
set -eu
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
printf '=== board ===\n'
tr '\000' '\n' < /proc/device-tree/compatible
uname -r
printf '=== power ===\n'
for supply in /sys/class/power_supply/*; do
    [ -f "$supply/type" ] || continue
    printf '%s\n' "${supply##*/}"
    for field in type capacity status voltage_now current_now online; do
        if [ -f "$supply/$field" ]; then
            printf '%s=' "$field"
            cat "$supply/$field"
        fi
    done
done
printf '=== fnOS version ===\n'
for file in /etc/cupid-fnos-release /usr/trim/etc/version; do
    if [ -f "$file" ]; then cat "$file"; fi
done
printf '=== block layout ===\n'
lsblk -p -o NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,PARTLABEL,PARTUUID
printf '=== parent GPT ===\n'
for path in /sys/class/block/*; do
    [ -f "$path/partition" ] && continue
    case ${path##*/} in sd*) sfdisk --dump "/dev/${path##*/}" ;; esac
done
printf '=== actual md/dm state ===\n'
if [ -r /proc/mdstat ]; then cat /proc/mdstat; fi
if command -v dmsetup >/dev/null 2>&1; then dmsetup table; fi
for device in /sys/class/block/md*; do
    [ -e "$device/md" ] || continue
    mdadm --detail "/dev/${device##*/}"
done
printf '=== mounts ===\n'
findmnt --real -o TARGET,SOURCE,FSTYPE,OPTIONS
printf '=== storage-related services ===\n'
for unit in cupid-fnosdata trim_main filestor_service extdev_service trim_nginx smbd; do
    systemctl show "$unit.service" -p Id -p ActiveState -p SubState -p FragmentPath
    systemctl cat "$unit.service" || true
done
printf '=== temporary proxy processes and sockets ===\n'
ps -eo pid,ppid,args | grep -E '[m]itm_ws|[n]odefix|[t]rim' || true
find /run -maxdepth 1 -name 'trim_cgi.socket*' -ls
printf '=== nginx upstream ===\n'
grep -n -A3 'upstream trim_cgi' /usr/trim/nginx/conf/nginx.conf
printf '=== recent storage errors ===\n'
journalctl -u trim_main.service -b --no-pager -n 120 | grep -E 'ERROR|RETRY|partition|bitmap|mdadm|storage|array' || true
printf '=== kernel storage options ===\n'
if [ -r /proc/config.gz ]; then
    gzip -dc /proc/config.gz | grep -E 'CONFIG_(TARGET_CORE|TCM_IBLOCK|LOOPBACK_TARGET|MD_LLBITMAP|CUPID|ARCH_QCOM|CPU_IDLE)'
fi
printf 'CUPID_STORAGE_DIAGNOSTICS_DONE\n'
