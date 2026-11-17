# PERSIST 生命周期修复验收 — 2026-11-16

**范围：仅 PERSIST 关闭退休这一单变量修复。** 整体 fnOS 原生 H264/NV12 level5 仍未完成；CLOSE ACK 字段仍缺失。本文只确认"PERSIST 不再走 HFI RELEASE、改由 CLOSE ACK retire"这一项。

**2026-11-17 更新**：该修复累计取得五轮成功 RAM 捕获（`b29e408d…`、`b89f4f05…`、`aa596c4f…`、`5eb096a0…`、`d31a18ad…`），均 `rc=0`/`stage=5`/`cpu_drained=1`/`orphaned=0`；其中后两轮使用合并后的最终驱动（`196b3e55…`）。本文此前"恢复入口未验证"的边界也已失效——第 5 版入口已在真机生效，详见[恢复入口迭代记录](fnos-recovery-entry-status-20261116.md)。

## 验收结论

PERSIST 生命周期修复**通过**，条件为：

- 两次独立 RAM 启动，同一候选结构；
- 每次 `control_rc=0`、`lifecycle_stage=5`、`cpu_drained=1`、`orphaned=0`；
- 双 STOP ACK 均匹配（BITSTREAM 98/105，RAW 99/106）；
- 三帧解码，PTS 1,000,000 / 1,040,000 / 1,080,000 µs，各 24576B，oracle 逐字节一致；
- 独立 FDE 验证器（含 bit 5 后继）两次均通过。

**这不是**安全回收、DMA 停止、close/reopen、重复会话或原生影视验收。

## 修复前的问题

上一轮冷启动后继首解三帧，但 quiesce 在 stage 2 以 `-110` 超时。45 条 180B 回执中除 PERSIST 外全部归还：

- APP INPUT 3/3、APP CAPTURE 16/16、BIN/COMV/NON_COMV/LINE 各 2、DPB 16；
- PERSIST：`flags=19`（valid+fw_owned+release_requested）、`returned=0`、`rx_generation=0`。

即 PERSIST 发出了 RELEASE 请求，固件从不回 RELEASE_DONE。

## 根因

上游 `kernel/drivers/media/platform/qcom/iris` 的 `sm8550_dec_ip_int_buf_tbl` 只含 `BUF_BIN`、`BUF_COMV`、`BUF_NON_COMV`、`BUF_LINE`；`iris_release_internal_buffers()` 从不释放 PERSIST。PERSIST 是会话级分配，只在 `iris_destroy_internal_buffers(force=true)` 于 close 时直接销毁。

本方案把 PERSIST 加入 `iris_repeat_release_internal()` 的 types 数组并等待 RELEASE_DONE，固件不会回该回包，1 秒硬件响应超时后判定失败。**这是设计错误，不是硬件故障。**

## 修复内容

后继源码快照（私有留存），只改 4 文件：

| 文件 | 改动 |
|---|---|
| `iris_buffer.c` | `iris_repeat_release_internal()` 的 types 数组移除 `BUF_PERSIST`；`iris_repeat_internal_owned()` 跳过 PERSIST；新增 `iris_repeat_persist_unclaimable()` 与 `iris_repeat_persist_close_retire()` |
| `iris_buffer.h` | 两个新函数的声明 |
| `iris_core.h` | `struct iris_repeat_owner` 增加 `close_retired` |
| `iris_vidc.c` | stage 3 后检查可声明性并 retire；回执 `flags` bit 5 记录 `close_retired` |

约束保持：物理存储全部保留，`IRIS_NATIVE_REPEAT_RECLAIM_ENABLED` 仍为 0，不声称 DMA 交接。

`iris_repeat_persist_unclaimable()` 只在 `unknown` 或 `release_requested` 时拒绝 CLOSE 声明——正常 `fw_owned` 的 PERSIST 是预期状态，因为从未为它发过单独 RELEASE。

## 两次真机证据

### 第一次（r2）

- 私有 attempt 记录（原始材料不随仓库公开）
- 捕获 101898B，SHA256 `b29e408d6d70d49c2edd14e810ac986d1bf09036dc18f342bfe6793c74bcb73f`
- 观测 60.0133 秒，`usb_protocol_success`、`observation_completed`、`cleanup_success` 均 true
- 三帧 PTS 1.0/1.04/1.08s，各 24576B

### 第二次（r3）

- 私有 attempt 记录（原始材料不随仓库公开）
- 捕获 101803B，SHA256 `aa596c4f9926d5ecc3174302ac6e481573e696a73dbd6e84db3708e7a350b7f5`
- 观测 60 秒级，`usb_protocol_success`、`observation_completed`、`cleanup_success` 均 true
- 三帧 PTS 与第一次一致

两次的 quiesce 控制记录均为：

```
{"kind":"quiesce_control","rc":0,"result":0,"stage":5,
 "session":1,"generation":1,"explicit_once":true,"reclaim_enabled":false}
```

status 回执均为：

```
stop_blocked=1 close_attempted=1 cpu_drained=1 orphaned=0
lifecycle_generation=1 lifecycle_stage=5
ah=98 ap=105 aa=1  bh=99 bp=106 ba=1
```

PERSIST 回执由修复前的 `flags=19`、`request_generation=1`、`returned=0` 变为 `flags=35`（含 bit 5）、`request_generation=0`、`returned=0`。

两次结果的汇总记录私有留存，SHA256 `1f6e728aef7d63f4f8f631c1b0302b7903aa28fdc55d614a183fb8892a5509fe`。

## 验证器变更

### FDE 验证器后继

冻结版 `verify_fde.py`（`78957211…`）拒绝任何 `flags & ~31`。新协议在 PERSIST 上用 bit 5 记录 close_retired。后继 `verify_fde.py`（`1047173f…`，私有留存）只放宽一处：

- bit 5 仅对 PERSIST（kind 9）允许；
- 且必须与 `stage==5` 一致（stage 5 时必须置位）；
- 其余掩码、extent、身份、顺序、生命周期绑定、帧/oracle 校验与全部 proof 标志保持原样。

冻结版本未修改。

### 独立结构验证器后继

冻结版 36 个函数 pin 中 5 个字节变化、2 个新增，全部可追溯到本次修复；逐条原因记录在私有审计文件中。验证逻辑本身未放宽。

## 模块身份

- 驱动 `cupid_qcom_iris.ko`：12,066,296B，SHA256 `196b3e557fa8682e8e06a384c617d87ddfba2242e37886b0be6447337c891b83`
- ABI：179 imports / 180 versions，零新增、零移除、零变更，missing/mismatch 0
- `.text` 段 SHA256 `e12d869d2e3898acc7640e928b36ccf97952428aa796c6922f7f34e979d913cf`，与随机路径构建逐字节一致
- 构建路径已固定为 `/var/tmp/iris-persist-recovery-{init,mod}-fixed`，消除调试信息路径造成的哈希漂移

## 候选镜像

- r3 候选镜像（私有留存）
- `boot-iris-decode-once-NOT-AUTHORIZED.img`，63,338,496B，SHA256 `95f02064896a79bebae2649ec958c71cb926a01977dd9436ba72dad93bf32234`
- 独立结构验证：`independent_offline_structure_verified=true`、`official_Image_unchanged=true`、55 归档成员、28 禁用节点、38 函数 pin 匹配

## 明确未完成

- **恢复入口**：本文写作时尚未验证；2026-11-17 第 5 版已在真机生效，详见[恢复入口迭代记录](fnos-recovery-entry-status-20261116.md)。
- CLOSE ACK 原始字段仍不在 180B 回执内，完整 quiesce 外部证明不成立。
- 安全回收、全核心 DMA 停止、真实 close/reopen、30 轮、720p/1080p、600 秒、seek/flush/DRC/取消、自带 FFmpeg/影视后端、持久部署与重启回归均未验收。
- `hardware_decode_verified`、`frame_acceptance_verified` 在 admission 与 manifest 中仍为 false；本文是条件证据验收，不是认证物理来源。

未刷分区、未切槽、未自动重试。所有原始材料留在 `private/`。
