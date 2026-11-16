# fnOS storage adapter — historical experimental implementation

> 下文是2026-11-05至11-07早期7.x内核阶段的适配器/存储记录，不能覆盖后续官方c1147系统的Btrfs、WebUI、Docker/SMB与正常重启成果。当前准确范围见[公开状态](../../../docs/status.md)。本工具源码保留历史输入守门，不作为当前设备的完整部署或安全停机方案。

This adapter presents a **verified data partition** through Linux LIO/IBLOCK and
`tcm_loop` as a local SCSI disk. It does not reconfigure UFS LUNs, expose network
iSCSI, patch the WebUI, format disks, or modify fnOS's database.

On 2026-11-05 the phone's unmodified storage backend created storage space 2
(Basic → md2 → LVM → Btrfs) on the exported disk. The block stack recovered after
one subsequent reboot, but native mounting fails with
`Unknown parameter 'trimacl'` (mount exit status 32). **This is not complete fnOS storage support.**
The phone was rechecked at 21:50–21:53 on 2026-11-05 after booting the existing B
slot without flashing or changing slots. LIO/md2/LVM recovered, Btrfs passed a
read-only check, and `trimacl` mounting still failed. A new backup-GPT header CRC
inconsistency was identified; its partition entries match the primary and saved
backup. No GPT repair was performed. See [port status](../../../docs/status.md).

## Verified on WSL (2026-11-04; Btrfs regression repeated 2026-11-05)

A disposable 1 GiB parent image contained a mock system partition and a 768 MiB
data partition. Only the data partition was exported. Tests verified:

- normal SCSI disk and real GPT partition/sysfs nodes, 4 KiB logical sectors;
- read-only export rejects writes; last legal block works; out-of-range writes fail;
- ext4 read/write and Btrfs subvolumes, read-only snapshots and scrub;
- export recreation preserves synthetic VPD identity and file hashes;
- all parent regions outside the exported partition retain their complete hash;
- adapter start is idempotent and stop refuses mounted ext4/Btrfs storage;
- systemd `DevicePolicy=closed` with parent `r` and exported disk `rw` rejects
  opening the raw parent for write while allowing the exported disk.

The WSL kernel lacks `MD_LLBITMAP`. **The exact fnOS mdadm/llbitmap sequence has
not been tested here.** WSL tests do not prove fnOS accepts the presented disk or
that app permissions, updates, or shutdown work correctly on the phone.

## Current phone state must be inspected first

**Release correction:** this repository no longer ships a serial/diagnostic wrapper. The historical 7.x `linux-serial.ps1` accepted only the old kernel identity and was removed from the fnOS-only tree rather than widened for official fnOS 6.18. An independently reviewed external fnOS operator channel, actual serial, host mutex and fresh board/kernel state are required before any phone diagnostic. All device logs remain outside the release in private storage; no phone commands were executed for this release.

The earlier **outer RAID1 → GPT partition → inner RAID1 → LVM → Btrfs** layout
was inspected read-only before migration. After explicit authorization it was
replaced by the LIO-backed native space 2. The old space 1 database row remains,
but it no longer describes an intact current backing chain. Do not run the old
wipefs/mkfs trial scripts or treat the new, registered Btrfs volume as empty.

The fixed kernel's boot image prefix was read back and hashed on the phone;
matching modules were installed with a backup. Disposable on-phone llbitmap
stop/reassemble tests kept bitmap version 6, including two md/LVM/Btrfs recovery
rounds. These are **not two clean phone reboots**. There are now two post-creation
boot/recovery observations, the latest after manual entry into fastboot; clean
shutdown ordering is not evidenced, and native mounting still fails.

`trim_main.service` now uses `DevicePolicy=closed`; its pre-start check rejected
`O_RDWR` on 143 raw UFS nodes while allowing the export. This protects the tested
process context, not every service or already-open descriptor. Do not describe
it as complete host isolation. Consumer shutdown and missing-volume behavior
still require validation.

## Isolated candidate config and build

In WSL, using a new directory:

```sh
bash tools/fnos/storage/prepare-kernel-config.sh /absolute/external/linux /absolute/external/cupid-lio-candidate-NEW
# Review config.diff before continuing.
bash tools/fnos/storage/build-candidate.sh /absolute/external/linux /absolute/external/cupid-lio-candidate-NEW
```

Candidate preparation uses the existing Kconfig parser with an explicit arm64
compiler environment. It does not clean or overwrite the active build config.
The build saves the previous Image and config, accepts only documented config
changes, checks board options and llbitmap, saves the candidate Image, and
restores `.config` on exit. Generated objects in the build tree are updated;
`Image.before` remains available for rollback. Use the saved `config.built`
when building matching modules. Never run `mrproper`/broad `pkill` to fix a wait.

Required options: `TARGET_CORE`, `TCM_IBLOCK`, `LOOPBACK_TARGET` and their
Kconfig dependencies. Preserve `MD_LLBITMAP`, `ARCH_QCOM`, `CUPID_*`, disabled
`CPU_IDLE`, and the existing embedded fnOS initramfs. The old `config-fnos`
file was a historical 7.2.8 configuration and is not shipped in this fnOS-only
tree; obtain and verify a matching official c1147 config externally.
See [current scope](../../../docs/status.md).

## Adapter configuration

`storage.example.json` is intentionally unusable. Build the real private file
from freshly verified geometry; do not copy trial IDs from chat history.

- `backing_partuuid`: GPT UUID of the dedicated **partition**, not a whole disk.
- `parent_ptuuid`: GPT UUID of its verified physical parent.
- `start_sector_512`, `size_sector_512`: sysfs `start`/`size` in **512-byte units**,
  even when the physical disk has 4096-byte logical sectors.
- `logical_block_size`: actual logical sector size.
- `virtual_serial`: newly generated 16 lowercase hexadecimal digits. This is a
  synthetic stable identity, **not the real UFS serial**.
- `readonly`: `true` for initial presentation. Switching to writable requires
  a stopped, unused export and approval of migration/initialization.

The adapter requires a unique UUID match, exact geometry, block-node/sysfs
agreement, no active mounts/swaps and no holders. IBLOCK defaults to exclusive
ownership. It verifies an existing export's actual backend major/minor,
exclusive claim, VPD identity, capacity, mode and LUN link before reusing it.
It never uses a fixed `/dev/sdX` or `/dev/dm-N` index.

Experimentally deployed paths (fresh installations must pass the gates below):

```text
/usr/local/lib/cupid/cupid-storage.py
/usr/local/lib/cupid/cupid-storage-policy.py
/usr/local/lib/cupid/cupid-device-access-check.py
/etc/cupid/storage.json                  mode 0600
/etc/systemd/system/cupid-storage.service
```

The phone also has ordering drop-ins for the adapter, `trim_init`, and its
consumers. These are not all represented by the base unit alone. The policy
generator writes a runtime `trim_main` drop-in and uses aliases under
`/dev/cupid-storage-policy`; `/run` device aliases failed on the installed
systemd. The accepted policy permits md/dm node creation as well as I/O.

Run Python with `-I`. `verify` reads identity only; `start` creates the export;
`status` checks it; `stop` refuses active mounts/holders and flushes before teardown.
The unit orders before `trim_main`/`extdev_service`; actual `Requires`/consumer
shutdown dependencies need validation on the installed fnOS version. It is
**not a complete deployment recipe yet**. An absent backing/config must never
cause apps to write into an unmounted directory on the system image.

## Mandatory on-device gates

1. Board/serial identity, one operator and stable power. Apply the project's
   power precautions and the user's explicit authorization for the current run;
   do not infer power condition from old logs.
2. Current boot, GPT and valid data backed up; hashes agree on phone and PC.
3. Restore native sockets/nginx after inspecting their state; no MITM remains.
4. RAM-boot candidate and inspect actual kernel options. Never force-load a
   module built for another kernel or assume Image size proves initramfs validity.
5. Read-only SCSI export discovered by the **unmodified** fnOS disk list.
6. Constrain the actual disk-writing process chain with a tested device policy.
   Do not use broad `block-sd rw`: it exposes original UFS firmware/system LUNs.
   Partition names/numbers assigned dynamically must be included accurately.
   A host policy test is not proof that the on-phone policy works.
7. Only after confirming data migration and access confinement: writable
   single-disk Basic+Btrfs creation on the exported device.
8. Native file management/Chinese filenames, PC SMB upload/download hash,
   app/Docker volume selection, backup restore and photo indexing.
9. Two clean reboots with stable VPD identity, automatic pool recovery and
   correct unmount/md-stop/flush/export-teardown shutdown ordering.

A read-only flag on `sda38` does **not** protect it from writes through the whole
`sda` node. Layered md is supported in principle; an `llbitmap` error is not
proof that nesting is unsupported. Single-member RAID1 gives no redundancy.

## fnOS-specific filesystem ABI remains unresolved

The installed official `trimafs2.ko` advertises
`6.18.18.c1147-trim ... modversions aarch64`; the phone runs `7.2.8-cupid+`.
Startup reports unknown `trimafs`, and `/fs` is absent. Separately, native
storage mounting invokes Btrfs with `trimacl` and tries an ext4 fallback with
`trimacl,prjquota`; both reject the unknown option with exit status 32.

Official headers extend inode state, twelve inode-operation callbacks, and
MAY_* permission bits. They define custom allow/deny and inheritance semantics,
distinct user-facing and disk-layout ACL structures, and `system.trim_flags`.
POSIX ACLs are not a drop-in replacement. Implementation source has not been
obtained from the public locations examined; that is not proof no source exists.
The visible Feishu form is issue feedback, not a verified source-request form.

Locate matching source or establish an isolated, versioned behavioral reference
before implementing these interfaces. Do not force-load an incompatible module,
ignore `trimacl`, substitute a plain bind mount, or claim application/permission
support solely because pool creation succeeds. See
[port status](../../../docs/status.md) for evidence and remaining acceptance work.

## Mountinfo exit-race regression (2026-11-07)

The installed helper once failed during boot with `EINVAL` from an exiting
process's `/proc/<pid>/mountinfo`. Kernel `mounts_open_common()` returns this
when the task or its mount namespace is gone. A first bounded-retry-only fix
still refused during a real fnOS VM boot. The final scanner skips `EINVAL`
only after confirming the process has disappeared, or is Z/X with no mount
namespace. Otherwise it re-enumerates at most three times, 50 ms apart, retaining
all previously observed mounted devices. Persistent `EINVAL`, permission
errors and I/O errors still refuse startup. It does not start an export after
an incomplete scan.

Twenty-five unit tests pass (including malformed state rejection), alongside
phone RAM regression of the preceding 24-test revision, including live/zombie namespace checks,
transient/persistent errors and mounted backing rejection after retry. Real
procfs churn with eight unreaped zombies passed 30 scans on WSL and 20 scans
on the phone. The phone test uploaded the helper/tests only into a unique
`/run` directory; **the installed helper was not overwritten**, and no storage
service was restarted. The final adapter also passed the disposable
Btrfs/LIO/device-policy smoke test in a historical external scratch directory (not distributed).
These are regression results, not two automatic phone reboots.

## Run disposable tests

```sh
# Separate authorization: uses host kernel modules/configfs/loop devices; not run for this release.
export CUPID_TEST_BASE=/absolute/external/ext4-scratch
bash tools/fnos/storage/tests/loopback-smoke.sh
CUPID_TEST_FILESYSTEM=btrfs bash tools/fnos/storage/tests/loopback-smoke.sh
CUPID_TEST_FILESYSTEM=btrfs \
CUPID_TEST_ADAPTER="$(realpath tools/fnos/storage/cupid-storage.py)" \
bash tools/fnos/storage/tests/loopback-smoke.sh
CUPID_TEST_DEVICE_POLICY=1 \
CUPID_TEST_ADAPTER="$(realpath tools/fnos/storage/cupid-storage.py)" \
bash tools/fnos/storage/tests/loopback-smoke.sh
python3 -I tools/fnos/storage/tests/test_adapter.py
```

Run as root in WSL/Linux. The test accepts **no device arguments**, creates a
new `cupid-lio-smoke.*` directory beneath explicit external `CUPID_TEST_BASE`, and detaches only its own test devices.
It retains images/logs for evidence; it does not delete them or existing targets.

## Rollback

Back up first. Stop consumers and unmount their storage before stopping the
adapter. Never force-remove a busy LIO target or assemble unknown RAID members.
The adapter does not format its backing, but **fnOS initialization does**; an
old filesystem cannot be recovered just by removing LIO after initialization.
Restore verified metadata/data backups where required. Restore known-good boot
using the designated serial and bootloader commands only, never hand-edit slot
flags. Review update scripts before upgrading; automatic updates stay disabled.
