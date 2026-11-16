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

## 2026-11-17 后续：dtbo 修改已回退

为实现隔离镜像内的自动重启，尝试把同一个 `reboot-mode` 节点写进生产 dtbo 后，解析脚本用错了 dtbo 表步长（44 而非 32），覆盖了相邻条目 177 字节。分区已用原始备份 `fastboot flash` 回退，**但设备随后落回隔离 HOLD、串口静默，回读哈希未确认**。

因此本文此前的"已写入 dtbo_a"结论**不再成立**：新 DT 已被回退，`reboot bootloader` 在当前设备上不可用，直到重新部署一份正确重排的 dtbo。

正确的整表重排脚本已完成离线实现与结构自检（44 条目、无重叠、偏移与 total_size 全部重算），但未上机。事件详情见[dtbo 修改事故与回退](fnos-dtbo-incident-20261117.md)。

同一节点若要进隔离镜像，需要先恢复设备访问并回读确认 dtbo 状态。

## 遗留与边界

- **模块尚未持久化**：当前是运行时 `insmod`，重启后失效。要长期生效需要把三个模块和加载逻辑纳入启动流程（生产 systemd 服务，或隔离镜像内嵌）。隔离镜像的内嵌版本已构建（init 995,704 B 含前两个模块），但本轮的 SDAM 模块还没加进去。
- **`reboot bootloader` 依赖 dtbo_a 的改动**：该分区已写入新 DT，回退方式是写回 原始 dtbo 分区备份（`c78e3734…`）。
- **SPMI 写封锁仍存在**：dmesg 有 `disallowed SPMI write to sid=0, addr=0x6146`，与 `known-issues.md` 第 14 条的 RTC 写封锁同类。这不影响 `restart@48`（0x48）的写入路径——`reboot bootloader` 已实测成功，但 0x6146 那条封锁的语义尚未查明。
- 未做：模块持久化、`mode-recovery` 验证、从隔离镜像内直接 `reboot bootloader` 的端到端测试。

## 私有材料

- 私有材料目录：新 DT 镜像与 build.json
- 私有写入记录：写入与回读记录
- 私有模块传输记录：模块传输与加载记录
- 私有权验记录：reboot-mode 类与 DT 绑定核验
- 私有实测记录：`reboot bootloader` 实测记录
- 原始 dtbo 分区备份：原始分区备份（回退用）
