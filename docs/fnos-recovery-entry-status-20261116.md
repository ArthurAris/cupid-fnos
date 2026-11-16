# 串口恢复入口状态：未验证 — 2026-11-16

**结论：恢复入口至今没有任何一版在真机上成功触发重启。** 本文如实记录四版迭代、每次的错误判断和当前未验证状态，避免把"看起来对"写成"已验证"。

## 动机

隔离 RAM 镜像没有主机可调用的重启入口，每轮实验结束都依赖现场长按电源键。目标是加一个受控入口，让设备能自行离开 HOLD。

## 内核能力边界（已核实）

| 项 | 状态 | 含义 |
|---|---|---|
| `CONFIG_POWER_RESET` | y | 支持 |
| `CONFIG_ARM_PSCI_FW` | y | 支持 |
| PSCI restart handler | 已注册（`psci.c:682`） | `reboot()` 能真正重启 SoC |
| `CONFIG_NVMEM_REBOOT_MODE` | **未设置** | **无法选择 bootloader** |

DT 里有 `pon@1300`（`qcom,pmk8350-pon`）、`pwrkey`、`resin`、`mode-bootloader` 字符串，但 nvram-reboot-mode 驱动不在官方 Image 中。

历史记录也印证：`docs/fnos-wifi-recovery-20261109.md:83` 记载 `systemctl reboot --reboot-argument=bootloader` 曾回原系统。

**因此恢复入口即使工作，也只能重启到正常启动目标，不能进 fastboot。**

## 四版迭代

### 第 1 版：模块本身（29 项单元测试通过）

`recovery.c` 实现固定字面量 `CUPID-RECOVER` 匹配后调用 `reboot(RB_AUTOBOOT)`。边界：无 shell、无 argv、无 exec、无文件/网络访问、无重试循环、单次有效。

29 项单元测试用真实 PTY 覆盖：未武装惰性、字面量精确匹配、超长行丢弃、错误前缀不匹配、重启后不再重复、拒绝坏 fd。

**问题**：这些测试只覆盖模块本身，完全没有覆盖 PID1 的集成方式。

### 第 2 版：`hold()` 接入（真机失败）

第一次真机测试：串口写入 14 字节成功，设备未重启。

诊断出两个问题：

1. **`hold()` 用 `pause()`。** 串口输入不产生信号，`pause()` 永久阻塞，`rc_poll()` 只执行一次。**入口从头到尾是聋的。**
2. **fd 来源错误。** `hold()` 读 `diagnostic_tty_fd`，该变量只在 `terminal_report()` 成功路径赋值。

修复：改为 `usleep(50000)` 有界轮询；`serial_adapter()` 协议成功时保留独立 fd 到全局 `recovery_tty_fd`。

PID1 重编 `13cbdb28…`。

### 第 3 版：全局 fd（真机失败）

第二次真机测试：仍失败。

发现第三个问题：**`hold()` 在 `observer()` 子进程里运行**，而 fd 是 `flow` 子进程设置的。三个进程内存不共享，observer 里全局恒为 -1。

错误修法：把 fd 号写进 `MAP_SHARED` 的 `diagnostic` 结构。

### 第 4 版：共享内存（被测试拦下，根本错误）

写了 `test-recovery-process-model.py`，真实复现跨进程拓扑：子进程写入共享内存，父进程读取。

测试结果：`rc_arm returned -9`（`-EBADF`）。

**这是根本性错误**：文件描述符是进程局部的，把 fd 号写进共享内存，另一个进程拿到的编号无效。现有架构本来就用 `SCM_RIGHTS`（`hf_send_once` → `hf_receive_metadata_once`）在 flow 和 PID1 之间传 fd，正是因为这一点。

### 第 5 版（当前）：改用 SCM_RIGHTS（编译通过，未上机）

- `hold()` 使用 `diagnostic_tty_fd`，该变量由 PID1 主进程在 `terminal_report()` 内通过 `SCM_RIGHTS` 接收。
- 在 PID1 主进程 `terminal_report()` 之后直接进入 `hold()`，而不是在 observer 循环里空转。
- 保留 `usleep` 有界轮询与每 tick 重试 arm。

PID1 编译通过：929944B，SHA256 `486e23eafa78a33b4bfe10b18c7c6936bffc5b83f523dbadb402946fa1addab7`。

**尚未上机。前四版都"看起来对"但都错了，因此这一版同样不能假定正确。**

## 教训

三次错误都在同一个盲区：**单元测试覆盖了模块逻辑，没有覆盖 PID1 的进程拓扑与 fd 生命周期。**

新增的 `test-recovery-process-model.py` 是针对这个盲区的回归测试。它已经证明了自己的价值——正是它抓出了第 4 版的 `-EBADF`。

## 当前状态与建议

- 恢复入口对整体目标（硬解 level5 验收）不是必需项。
- 即使最终工作，也只能重启到生产系统，仍需要一次 adb/fastboot 操作才能回到 fastboot。
- 建议把恢复入口作为独立支线，不与 PERSIST 修复验收混在一起。

PERSIST 修复已独立验收，见[PERSIST 生命周期修复验收](fnos-persist-fix-acceptance-20261116.md)。

## 私有材料

- 第 5 版源码与 manifest：私有留存
- 两次真机测试记录：私有留存
- 第二次测试所在 attempt：私有留存

未刷分区、未切槽、未自动重试。
