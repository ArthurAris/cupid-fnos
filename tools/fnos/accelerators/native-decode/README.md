# 独立 native video 部署事务工具

固定目标：`xiaomi,cupid` / `6.18.18.c1147-trim`。这是**离线输入校验与部署事务实现**，不是视频解码验收，也不代表当前 native 驱动可以装载。

## 生产边界（reader/observer 修订）

- `prepare-only` 仍只输出计划，不改目标。`apply/rollback/recover --admission /private/independent-admission.json` 现在接入真实 `AdmissionReader`、`ReadOnlyLiveObserver`、`ProductionAdmission` 和现有事务引擎，**不再硬编码永远拒绝**。
- 没有独立安装的 fixed trust、独立证据/receipt 或真实 live 前提时默认拒绝。此次只开发与离线测试，没有提供生产准入、没有运行生产 apply。
- CLI 不接受任意 producer/source pin 或 fake-root；固定 `/etc/cupid-native-decode-admission/trust.json` 由主会话独立安装，0700 父目录、0600 文件、root owner。它绑定本工具实际源码 SHA256、三个固定 producer hash、三个独立证据 hash、admission hash、supervised receipt hash 和当前操作 nonce。每次修改资产前重读 trust、producer 与证据。
- 三个 producer 固定于 `/usr/local/lib/cupid/native-decode-admission/{lifecycle,package,abi}-producer`；仅实际读文件 hash，**不执行或导入**。报告来源不能是 package 自选路径/自证 boolean；它们的 hashes 来自固定独立 trust。
- 阶段为 `prepare/stage → 验证 deployability contracts → apply staged assets → 主会话受控 native 验证 → 独立批准 persist enable`。staging 不要求尚未运行的最终 native decode 任务成功，消除装前验收循环；本工具无 enable/start/modprobe 功能。
- 无设备命令、网络、块设备读写、boot/DT 写入；只读 local device boot backup，host boot/recovery 留在电脑，由独立 supervised receipt 验证，不在设备要求第二份 201MB host copy。

### 独立 schema（所有未列字段拒绝）

JSON 都采用 schema=整数1、固定 kernel/board。实际 firmware hashes 和本机信息只放 private，本文无生产值。

1. **trust**：`schema/kernel/board/observer_sha256/producer_pins/evidence_pins/admission_sha256/receipt_sha256/nonce`。两个 pins map 恰好 lifecycle/package/abi；源码 hash 是整个 deploy.py 实际内容。主会话独立配置，不从 package 取得。
2. **admission**：`schema/kernel/board/phase/package_sha256/firmware_manifest_sha256/evidence/backups/root_snapshots`；phase 必须 `validated-staging`。evidence 三项各 `path/sha256` 指向独立 private 0600 报告，hash 必须等于 trust 的相应 pin。
3. **三份报告**：`schema/kernel/board/kind/producer_sha256/package_sha256/firmware_manifest_sha256/result/details`；result=`verified-for-staging`。lifecycle details 恰好 remove/unbind/irq-work-drain/dma-lifetime/provider-module-lifetime/normal-recovery，全部 `verified`；ABI 为 coverage=`complete-applicable`、unknown=0、mismatches=0（整数而非 boolean）；package details 为固定 assets signatures 和 runner_audit/service_audit=`verified`。实际 apply payload hashes 必须等于独立 package assets。
4. **backups**：`device_boot/host_receipt/recovery`。device_boot=`path/sha256/size`，本地 private 普通文件实际流式 hash，size 必须 **201326592**；host_receipt=`path/sha256`，独立 receipt 的 pin 必须等于 trust；recovery=`sha256/size` 是 pinned 恢复输入，不在设备访问 host 镜像。
5. **supervised host receipt**：`schema/kernel/board/producer_sha256/package_sha256/boot_id/nonce/issued_at/expires_at/slot/boot_size/boot_sha256/device_before_sha256/device_after_sha256/host_boot_sha256/host_boot_size/recovery_sha256/recovery_size`。main 负责实际 current slot 稳定读前后及电脑备份/恢复镜像，四个 boot hashes 必须一致、current slot 全长、与 live/local device backup 一致。旧 boot_id/slot/nonce、unknown 或 selfbool 拒绝。
6. **固定 live**：`/run/cupid-native-decode-admission/live.json`，0700 父目录/0600，字段 `schema/kernel/board/boot_id/nonce/issued_at/expires_at/producer_sha256/package_sha256/slot/boot_sha256/boot_size/battery_percent/runtime/authentication/iommu/recovery/assets`。实际读取 uname、DT compatible、proc boot_id、四目标哈希；与 main 生命周期 producer 的当前 supervised observation 相符。slot 仅 boot_a/b；电量≥40；runtime=`inactive-unbound-no-enablement-or-overrides`，authentication=`secure-enforced`，iommu=`translation-enforced`，recovery=`independently-verified-usable`。live 和 receipt 均须15秒内且绑定固定 nonce；不以旧 admission 当 live 证明。
7. **root_snapshots**：独立 main 提供的小型 host snapshot 副本库存，各 `before/state/files/state_file`，before 为四目标 hash/mode/null，state 为相应 signature/null，files 为四目标 private 副本路径/null。apply 持锁后 fresh 读取当前目标，要求独立 snapshot 与当前实际 hash/mode 一致；host snapshot 不由 device 从当前 bytes 伪造。恢复/rollback也要求对应当前状态的库存，不足则保留 pending 拒绝。

固定信任配置的安装/更新、producer如何生成真正 supervised observation、serial/host mutex、receipt/恢复输入真实性均由 main 管理，本工具不构造生产证明。15秒的完整检查若过期就安全拒绝，需要 main 重新提供独立证据，不放宽时间窗口。

## 固定白名单与输入格式

package 目录必须恰好包含以下四个资产和 `package-manifest.json`，拒绝额外项、重复记录、未知字段、symlink、hardlink、组/世界可写文件与不安全父目录。

| 文件 | 唯一目标 | 模式 |
| --- | --- | --- |
| `cupid_qcom_iris.ko` | `/usr/local/lib/cupid/native-decode/cupid_qcom_iris.ko` | 0644 |
| `cupid_videocc_sm8450.ko` | `/usr/local/lib/cupid/native-decode/cupid_videocc_sm8450.ko` | 0644 |
| `native-decode-runner` | `/usr/local/lib/cupid/native-decode/native-decode-runner` | 0755 |
| `cupid-native-decode.service` | `/etc/systemd/system/cupid-native-decode.service` | 0644 |

manifest 顶层字段恰好为 `schema`（整数 1）、`kernel`、`board`、`files`。每个 files 记录恰好为 `file`、`target`、`mode`（十进制整数）、`sha256`、`elf`。

- 两个模块的 `elf` 为 `{ "name": "对应不含.ko的文件名", "vermagic": "6.18.18.c1147-trim SMP mod_unload modversions aarch64", "build_note": "完整原始GNU note十六进制" }`。实际读取 ELF64 LE AArch64 relocatable 的 modinfo 和 GNU build-id note，不仅相信 manifest。
- runner 的 `elf` 为 `{ "machine": "aarch64", "kind": "executable" }`，要求真实 AArch64 executable/PIE ELF、有效 program headers、入口所在可执行且非可写 PT_LOAD。脚本不得作为可执行资产冒充。ELF header 校验不是 runner 行为/依赖/安全审查；未来 admission 仍须绑定经过独立审查的整个 package pin。
- service 的 `elf` 是 `null`，字节必须与 `deploy.py` 的 `SERVICE` 常量完全相同：手动 oneshot、固定 `--manual-only` runner、`NoNewPrivileges=yes`，没有 `[Install]`。只允许此版本，不接受追加指令、Alias/Also、Install、强制 modprobe 等。
- package pin 必须由外部审查流程提供 SHA256，工具不自算 hash 来充当认证。

私有 firmware manifest 顶层为 `schema/kernel/board/firmware`，firmware 恰好 `{ "path": "私有绝对源路径", "sha256": "源文件SHA256" }`。manifest 必须处于 `private/`、父目录 0700、文件 0600，并提供外部 manifest pin；实际读取源文件重新 hash。路径/hash/二进制不进入 package、公开 README 示例或输出计划；固件从不成为部署目标。

示例命令只供将来在可信主机/设备环境使用，不是在本次工作中执行的设备操作：

```text
python3 -I -B /trusted/native-decode/deploy.py prepare-only /private/stage PACKAGE_SHA256 --firmware-manifest /private/firmware.json --firmware-manifest-sha256 PRIVATE_MANIFEST_SHA256
python3 -I -B /trusted/native-decode/deploy.py apply /private/stage PACKAGE_SHA256 --firmware-manifest /private/firmware.json --firmware-manifest-sha256 PRIVATE_MANIFEST_SHA256
python3 -I -B /trusted/native-decode/deploy.py rollback PACKAGE_SHA256
python3 -I -B /trusted/native-decode/deploy.py recover PACKAGE_SHA256
```

生产三项持久动作还必须提供 `--admission`；缺失固定独立信任/实际证据时拒绝，不能将本文示例当生产准入。

## 事务、备份和恢复

事务核心使用 Linux dirfd/openat no-follow 路径遍历、设备本地 `/run/cupid-native-decode.lock` 非阻塞 flock、临时文件 O_EXCL、文件 fsync、同目录原子 replace、目录 fsync 和完整数据/模式 readback。只允许四个资产以及 `/var/lib/cupid-native-decode/deploy` 的固定 state/pending 和 UUID transaction snapshot 文件；拒绝无关 WiFi、USB、NAS、模块树、boot/block 路径。

每次事务先读取**当前实际**四个目标及 state，在两个端点保存 snapshot，并复读双端内容/hash/mode。快照缺失项明确保存为不存在；rollback 只恢复这些实际 snapshot，而不猜测“原厂内容”或递归删除目录。备份目录 0700、文件 0600，保留备份和事务结果。原始模式在 metadata 中保存。

- 修改每个资产或 state 前，重新调用 live observer、重新验证双端 snapshot、确认当前全目标集合和 state 未漂移，单文件写入再次 CAS 检查。
- pending journal 在第一次资产写入前持久化。普通文件/replace/fsync 失败自动走验证恢复；KeyboardInterrupt/SIGKILL 等保留 pending，重新实例化后可用核心 recover。
- recover 只接受当前文件处于记录的 before 或 after；陌生内容、metadata/hash/state 冲突先拒绝，不覆盖。恢复前还给**实际部分安装状态**生成新的双端 snapshot。
- 不允许覆盖已有 installed 事务；更新须先显式 rollback。rollback 绑定原 package pin；不存在实际 snapshot 则拒绝。不会恢复原来不存在的 wants 链接，也不改系统启用状态。
- 崩溃可能留下私有临时文件/空目录；不会递归清理这些目录，避免删除其他内容。完整设备生命周期停止/卸载与主会话操作者管理不属于此工具。

## 当前未提供的生产证据

reader/observer和事务连接已实现，但本次没有安装固定 trust、三个真实独立producer报告、supervised当前槽/电脑备份/恢复输入receipt或实机live observation。缺失任何一项仍拒绝。main需在独立批准后提供这些实际资料；不以本文或host fixtures作为验收。native最终受控硬件验证和persist enable是后续独立阶段，不是staging循环前提。

boot/DT仍只生成external coordinator计划，不生成/刷写候选image，也不读取block设备。所有serial/host mutex和实际设备观察归main。

## 隔离 host 测试

```text
# 从可信目录执行；WSL 测试根必须放在 ext4，不能使用不支持0600/0700的DrvFS。
python3 -I -B /trusted/native-decode/test_deploy.py --host-test-base /var/tmp/cupid-offline-private/fnos -v
```

测试在新的 `iris-native-deploy-test-20261115-UUID/` 下生成 fake-root、stage、private firmware 和独立 host snapshot 文件，结束清理每例内容。receipt/adapter用短小synthetic数据；另有单项在WSL ext4创建全长201326592B sparse文件，实际流式hash与长度拒绝测试，无C盘201MB备份、无第二份host镜像。fixtures均非生产准入。

测试禁止真实 subprocess/shell/network/socket，并约束工具绝对 os.open 路径只在每例沙箱；fake-root 内 WiFi/USB/NAS、模块树与假 boot/block canary 必须保持原样。覆盖实际原子写、replace前后失败、目录 fsync 失败、多个持久边界中断恢复、rollback中断、漂移冲突、双端 snapshot 损坏、native/live门丢失、flock争用、路径/权限/symlink/hardlink/FIFO/重复JSON/恶意manifest/未经审查service拒绝。

host 通过仅说明文件事务测试通过，不说明全长实机 boot backup、native decode、最终 ABI 或设备安全验收通过。
