# 全自动重启到 fastboot：DT 与模块方案 — 2026-11-17

**结论：打通了。** `systemctl reboot --reboot-argument=bootloader` 首次在真机上把设备直接送进 fastboot。此前这条路从未成功过（历史记录见 `docs/fnos-wifi-recovery-20261109.md:83`）。

## 问题

每轮 RAM 实验结束都依赖现场长按电源键。目标是让设备自己回到 fastboot，实现无人值守循环。

此前的判断是"官方内核没有 reboot-mode 支持"。本轮查清了真实原因，并证明可以不改内核解决。

## 根因（三层，逐层查清）

### 1. 生产 DT 缺 `reboot-mode` 节点

生产系统 DT 里：

```
pmic@0/pon@1300                 qcom,pmk8350-pon     ← 存在，但无 reboot-mode 子节点
pmic@0/nvram@7100/reboot-reason@48                  ← 存在（offset 0x48, 1 byte）
```

`restart@48` 这个 nvmem cell **已经存在**，而且已经被 `reboot_reason` 引用：

```dts
sdam@7100 {
    compatible = "qcom,spmi-sdam";
    reg = <0x7100>;
    restart@48 { reg = <0x48 0x01>; bits = <0x01 0x07>; phandle = <0x14>; };
};

reboot_reason {
    compatible = "qcom,reboot-reason";
    nvmem-cells = <0x14>;
    nvmem-cell-names = "restart_reason";
};
```

缺的只是一个 `nvmem-reboot-mode` 节点去写这个 cell。

### 2. 三个驱动都没编进内核

| 配置 | 值 | 影响 |
|---|---|---|
| `CONFIG_NVMEM_SPMI_SDAM` | 未设置 | 没有 SDAM nvmem provider，cell 不存在 |
| `CONFIG_REBOOT_MODE` | 未设置 | 没有 reboot-mode 核心，`devm_reboot_mode_register` 不在 vmlinux |
| `CONFIG_NVMEM_REBOOT_MODE` | 未设置 | 没有写入模式的驱动 |

三者都是 `tristate`，可以编成模块——**不需要重编内核**。

### 3. 之前判断失误的纠正

上一轮我误判"生产 DT 无 `reboot-reason` 节点"，是因为探测路径少写了一层（漏了 `nvram@7100/`）。实际节点存在。这个错误已在文档中纠正。

## 实施

### 编译三个模块

从内核树取上游源码，用官方 c1147 头 + GCC12 构建：

| 模块 | 源文件 | 剥离后大小 |
|---|---|---|
| `nvmem_qcom-spmi-sdam.ko` | `drivers/nvmem/qcom-spmi-sdam.c` | 11,832 B |
| `reboot-mode.ko` | `drivers/power/reset/reboot-mode.c` | 18,288 B |
| `nvmem-reboot-mode.ko` | `drivers/power/reset/nvmem-reboot-mode.c` | 9,384 B |

vermagic 均为 `6.18.18.c1147-trim SMP mod_unload modversions aarch64`。

加载顺序：SDAM → reboot-mode 核心 → nvmem-reboot-mode（后者依赖 `devm_reboot_mode_register`）。

### 修改 dtbo_a

在 entry 0 的 `sdam@7100` 内、`restart@48` 之后插入：

```dts
reboot-mode {
    compatible = "nvmem-reboot-mode";
    nvmem-cells = <0x14>;
    nvmem-cell-names = "reboot-mode";
    mode-bootloader = <0x77665501>;
    mode-fastboot = <0x77665501>;
    mode-recovery = <0x77665502>;
};
```

新 DT 502,146 B，原 slot 501,960 B。entry 0 后有空闲 4,300,770 B，扩展 slot 即可。

### 安全门

| 项 | 结果 |
|---|---|
| 写前设备 dtbo_a 哈希 | `c78e3734728b8ed6f8f28897d963ddb0d0757cdc7b33d1ba6ebed52a4cfad9e6` |
| 与 2026-10-27 备份比对 | **完全一致**（双端哈希满足） |
| 新镜像哈希 | `14d2e67b3ef4353ce4d96807a7fc0176f9142a232f45af7c21b0d06645bb7a3c` |
| 离线验证 | 只有 entry 0 与 table 的 dtb_size 字段变化，其余字节完全一致；4 个 entry 全部可解码 |
| 传输 | RNDIS 25,165,824 B，设备端 `sha256sum` 与主机一致 |
| 写后回读 | `14d2e67b…`，与写入镜像一致 |

## 真机验证

加载三个模块后：

```
lsmod:
  nvmem_reboot_mode      12288  1
  reboot_mode            16384  1 nvmem_reboot_mode
  nvmem_qcom_spmi_sdam   16384  1

/sys/class/reboot-mode/nvmem-reboot-mode/reboot_modes  →  "recovery bootloader"
/sys/bus/platform/drivers/nvmem-reboot-mode/reboot-mode →  DT 节点已绑定
of_node/name  →  reboot-mode
```

执行 `systemctl reboot --reboot-argument=bootloader` 后，USB 出现 `Android Bootloader Interface`，`fastboot getvar product` 返回 `cupid`。

**设备直接进了 fastboot，全程无人工按键。**

## 2026-11-17 后续：正确重排的 dtbo 已部署，全链路实测通过

中途出现过一次事故：首版解析脚本用错表步长（44 而非 32），只看到 4 个假条目，entry 0 扩大时覆盖了 entry 1 的 177 字节，且当时的校验脚本复用了同一错误假设而误判通过。事故与恢复完整记录见[dtbo 修改事故与回退](fnos-dtbo-incident-20261117.md)。

事故后的正确做法与最终状态：

- 重写重排脚本，按真实字段顺序 `magic, total_size, header_size, entry_size, entry_count, entries_offset, page_size, version` 解析，按真实 stride=32 遍历全部 **44** 个条目。
- 独立校验（不复用重排代码路径）：表头除 `total_size` 外全部不变，只有 entry 0 的 DTS 不同，**entry 1 字节完全一致**，无重叠，全部为合法 FDT。
- `dtbo_a` 与 `dtbo_b` 均已部署为 `359b5bc0…b125c25`，写入后回读（含 `iflag=direct` 复核）双端一致；原始备份 `c78e3734…` 在手。

部署后的实测结果：

| 路径 | 结果 |
|---|---|
| 生产系统 `systemctl reboot --reboot-argument=bootloader` | USB 出现 `Android Bootloader Interface`，`getvar product` = `cupid` |
| 隔离镜像 HOLD 串口发 `CUPID-RECOVER` | 内嵌三模块加载成功 → `reboot(RESTART2,"bootloader")` → fastboot |
| 主机只读确认 | `designated_fastboot_confirmed=true`，`bootloader_interface=1`，`gadget_interfaces=0` |

**因此本文此前"新 DT 已被回退、`reboot bootloader` 不可用"的结论已作废。** 当前双槽 dtbo 均带 `reboot-mode` 节点，两条重启路径都可用，且从 fastboot 出发的完整实验循环（fastboot → RAM 启动 → 硬解观测 → HOLD → `CUPID-RECOVER` → fastboot）无需任何人工按键。闭环详情见[全自动硬解循环打通](fnos-full-auto-decode-loop-20261117.md)。

## 遗留与边界

- **模块尚未持久化**：生产系统仍是运行时 `insmod`，重启后失效。隔离镜像已内嵌全部三个模块（init 995,800 B），但生产侧需要 systemd 服务或 initramfs 前置加载。
- **`reboot bootloader` 依赖 dtbo 改动**：回退方式是写回原始 dtbo 分区备份（`c78e3734…`），`dtbo_b` 同理。
- **SPMI 写封锁仍存在**：dmesg 有 `disallowed SPMI write to sid=0, addr=0x6146`，与 `known-issues.md` 第 14 条的 RTC 写封锁同类。这不影响 `restart@48`（0x48）的写入路径——`reboot bootloader` 已实测成功，但 0x6146 那条封锁的语义尚未查明。
- 未做：模块持久化、`mode-recovery` 验证。

## 私有材料

原始设备日志、镜像、分区备份、写入/回读记录与模块传输记录只留私有，不随仓库公开。公开副本只保留脱敏结论与哈希。
