# 全自动硬解循环打通 — 2026-11-17

**结论：从 fastboot 出发的完整实验循环已实现全自动，无需任何人工按键。**

## 闭环验证

一次完整循环的实测序列：

```
1. 设备在 fastboot
2. fastboot boot r6 镜像           → 隔离系统启动
3. 主机 USB v2 协议 + 60 秒观测    → 硬解三帧
4. quiesce 控制                    → rc=0 / stage=5 / cpu_drained=1
5. 隔离 PID1 进入 HOLD
6. 主机串口发送 CUPID-RECOVER      → 隔离 PID1 加载三个模块
7. reboot(RESTART2, "bootloader")  → 设备直接进入 fastboot
8. 主机只读确认                    → designated_fastboot_confirmed=true
```

全程 0 次人工按键、0 次分区写入（除已批准的 dtbo 部署）、0 次切槽。

## 硬解结果（第三次复现）

r6 观测（私有 attempt 记录，原始材料不随仓库公开）：

| 指标 | 值 |
|---|---|
| `usb_protocol_success` | true |
| `observation_completed` | true |
| `capture_bytes` | 101,803 |
| `capture_sha256` | `d31a18adc7c93699262abd9631ba5d4e71d613ab81395c990f9e91d9cb11a69b` |
| `control_rc` | **0** |
| `lifecycle_stage` | **5** |
| `cpu_drained` | **1** |
| `orphaned` | 0 |
| STOP ACK | BITSTREAM 98/105、RAW 99/106，均匹配 |

PERSIST 修复至此已在三次独立 RAM 观测中复现（r2、r5、r6），均 `rc=0`/`stage=5`。

## 三个关键组件

### 1. dtbo 正确重排（已部署，双槽一致）

原 dtbo 是 44 条目、`dt_entry_size=32` 的 Android dt_table。先前误用 44 字节步长只看到 4 个假条目，导致 entry 0 扩大时覆盖 entry 1。

正确做法（私有重排脚本）：

- 用真实字段顺序解析：`magic, total_size, header_size, entry_size, entry_count, entries_offset, page_size, version`
- 按真实 stride=32 遍历全部 **44 个**条目
- entry 0 的 DTB 从 501,960 增至 502,116 字节（新增 `reboot-mode` 节点）
- 其余 43 个条目内容不变，整体后移 156 字节
- 保持原镜像的连续排布（原镜像并非页对齐）
- 重算每个 `dtb_offset`、`dtb_size` 与头部 `total_size`

严格验证（不复用重排脚本的代码路径）：

- 表头除 `total_size` 外所有字段不变
- 44 个条目中**只有 entry 0 的 DTS 不同**
- **entry 1 字节完全一致**（上次被破坏的就是它）
- 无重叠、全部为合法 FDT

部署：`dtbo_a` 与 `dtbo_b` 均为 `359b5bc004da0c732362a2f4bd32361ce72ad7ad5d1712129fb6b6b24b125c25`，写入与回读双确认（含 `iflag=direct` 绕过页缓存复核）。

原始 dtbo 分区备份（私有留存）：`c78e3734728b8ed6f8f28897d963ddb0d0757cdc7b33d1ba6ebed52a4cfad9e6`。

### 2. 三个上游模块（编成模块，不重编内核）

| 模块 | 作用 | 大小 |
|---|---|---|
| `nvmem_qcom-spmi-sdam.ko` | 提供 SDAM nvmem provider | 11,832 B |
| `reboot-mode.ko` | 提供 `devm_reboot_mode_register` | 18,288 B |
| `nvmem-reboot-mode.ko` | 绑定 DT 节点并写入重启模式 | 9,384 B |

三者均为 `tristate`，从内核树取上游源码用官方 c1147 头 + GCC12 构建，vermagic 为 `6.18.18.c1147-trim SMP mod_unload modversions aarch64`。

加载顺序即依赖顺序：SDAM → reboot-mode 核心 → nvmem-reboot-mode。

加载后：

```
lsmod: nvmem_reboot_mode / reboot_mode / nvmem_qcom_spmi_sdam
/sys/class/reboot-mode/nvmem-reboot-mode/reboot_modes  →  "recovery bootloader"
```

### 3. 隔离镜像内嵌与恢复入口

三个模块以字节数组形式嵌入 PID1，用 `init_module(2)` 从内存加载，**不新增归档成员**，保持冻结的 55 成员契约与 PREPARE26 模块列表不变。

恢复入口 `recovery.c`：

- 串口固定字面量 `CUPID-RECOVER`（`RC_LINE_MAX` 32，超长丢弃）
- 无 shell、无 argv、无 exec、无文件/网络访问、无重试循环、单次有效
- 有界轮询 `usleep(50000)`，每 tick 重试 arm
- 描述符来自 `SCM_RIGHTS` handoff 的独立 `F_DUPFD_CLOEXEC` 副本（`dco_run` 的子进程会带走它自己的副本）
- 加载三模块成功后调 `syscall(SYS_reboot, MAGIC1, MAGIC2, RESTART2, "bootloader")`，失败回退普通重启

## 已知限制

- **恢复入口只在观测成功的 HOLD 生效**。`hf_receive_metadata_once` 要求 `confirmed==1`；观测失败的 HOLD 拿不到串口描述符，入口无法救场。这是设计限制，不是字面量或 DTR/RTS 问题。
- **模块是运行时加载**，未做持久化。隔离镜像已内嵌全部三个模块；生产系统侧重启后失效。
- `mode-recovery` 未验证。
- dmesg 仍有 `disallowed SPMI write to sid=0, addr=0x6146`（与 `known-issues.md` 第 14 条 RTC 封锁同类）；不影响 `0x48` 路径（已实测成功），但该封锁语义尚未查明。
- 全自动循环依赖 dtbo 已部署这一前提；`dtbo_a`/`dtbo_b` 回退方式是写回原始分区备份。

## 仍未完成的 level 5 项

原始 CLOSE ACK 字段导出、安全回收与真实 free、真实 close/reopen、30 轮会话、720p/1080p、至少 600 秒连续负载、seek/flush/DRC/错误取消、fnOS 自带 FFmpeg 与影视真实任务、无 CPU 静默回退证明、持久部署与重启回归。

HEVC、硬件编码、NPU 独立且未验收。
