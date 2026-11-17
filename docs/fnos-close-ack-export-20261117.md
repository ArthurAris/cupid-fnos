# CLOSE ACK 原始字段导出验收 — 2026-11-17

**结论：固件自己的 CLOSE 确认字第一次进入证据链，并由独立验证器断言通过。**

此前 45 条 180 字节回执记录了全部 APP / 内部 buffer 的归还与代际，但没有任何一条包含 CLOSE ACK 本身。quiesce 只能证明驱动内部状态走到 stage 5，不能证明固件确实确认了关闭。这是全局 level 5 的最大缺口。本次补上了。

## 单变量改动

只改驱动，客户端、内核、准入规则、reclaim 门、PERSIST 关闭退休逻辑均不变。

1. **捕获位置**：`iris_hfi_gen2_handle_session_close` 在任何身份校验**之前**记录原始字。校验失败或不匹配的 ACK 也会留下记录，不会被静默丢弃。
2. **存储**：`struct iris_inst` 增加 12 个 u32 字段。
3. **导出**：整会话回执（`kind == 0xFFFFFFFF`）本身没有 owner，它的 owner 槽位原本恒为 0，正好用来承载 CLOSE ACK。`flags` 只使用 bit 0..4，落在冻结验证器既有的 31 掩码内，因此**没有放宽任何验证逻辑**。

回执字段映射：

| 回执槽 | 内容 |
|---|---|
| `generation` | 是否观察到 CLOSE 包 |
| `header` / `packet` | 我们发出的 CLOSE 请求 header / packet |
| `type` / `port` / `size` | ACK 原始包类型 / 端口 / 字节数 |
| `addr_lo` / `addr_hi` | ACK 原始 flags / payload_info |
| `request_header` / `request_packet` | 承载 ACK 的 RX header id / ACK 回的 packet id |
| `request_generation` / `submitted` | ACK 原始 reserved[0] / reserved[1] |
| `flags` | bit0 seen、bit1 matched、bit2 success、bit3 information、bit4 size==32 |
| `returned` | 观察到的 CLOSE 包个数 |

## 设备结果

第七次 RAM 运行（私有 attempt 记录，原始材料不随仓库公开）：

| 指标 | 值 |
|---|---|
| `usb_protocol_success` | true |
| `frame_content_consistent` | true |
| `capture_bytes` | 101,898 |
| `capture_sha256` | `f8a2e11c88e6e1126efc7bad89c8eb224f83cfac71fee84d3ddaf1ab4d17c9e0` |
| `control_rc` / `lifecycle_stage` | **0** / **5** |
| 回执条数 | 45 |

整会话回执解析：

```
version          = 1
session          = 1
kind             = 0xffffffff
generation       = 1          CLOSE 包已观察到
req_header       = 0x00000064  我们发的 CLOSE header
req_packet       = 0x0000006b  我们发的 CLOSE packet
ack_type         = 0x01000004  HFI_CMD_CLOSE
ack_port         = 0          HFI_PORT_NONE
ack_size         = 32         裸包，无 payload
ack_flags        = 1          HFI_FW_FLAGS_SUCCESS，别无其他
payload_info     = 0
ack_header       = 0x00000064  与我们发出的 header 一致
ack_packet       = 0x0000006b  回显我们的 packet id
flags            = 0x17       seen|matched|success|size
stop_blocked     = 1
close_attempted  = 1
cpu_drained      = 1
orphaned         = 0
lifecycle_stage  = 5
ack_seen         = 1          恰好一次
```

即：固件确实收到了我们的 CLOSE，用我们给的 header 与 packet 身份回了一个 SUCCESS 的裸确认包，没有附带 payload，没有 READONLY 或其它标志，并且只回了一次。

## 验证器（收紧，非放宽）

FDE 验证器后继 `9c383fef…` 对整会话回执**新增**断言：

- `generation == 1`（确实收到 CLOSE 包）
- `returned == 1`（恰好一次）
- `flags` 含 seen 与 matched 位
- `type == 0x01000004`
- `port == 0`
- `size == 32`
- `payload_info == 0`
- `ack_flags` 只允许 `SUCCESS`，且必须含 `SUCCESS`
- ACK 回显的 header 与 packet 等于我们发出的

新增断言全部是收紧。PERSIST 的 bit 5 规则、掩码、extent、身份、顺序、生命周期绑定、帧 oracle 与全部 proof 标志原样保留。

验证器自身先被测过：13 个合成用例，正确导出被接受，**每一种错误（无 ACK、不匹配、错类型、错端口、带 payload、失败 ACK、多余标志、回显不一致、两个 ACK）都被拒绝**。

## 结构性证明

驱动新增 12 个 u32 字段会让 `struct iris_inst` 变大 48 字节，从而搬动所有访问其后续字段的函数的访存偏移。为把"布局移动"与"代码改动"分开：

- 构建**对照模块**：旧源码 + 仅新头文件。
- 对全部 **414** 个函数逐指令比较（归一化地址、跳转目标、寄存器编号）。
- 结果：**412 个完全一致，只有 2 个不同** —— `iris_hfi_gen2_handle_session_command`（+34 条指令，即捕获代码，CLOSE 处理器被内联于此）与 `iris_repeat_receipt_read`（+20 条，即导出代码）。

由此，验证器 pin 表中其余全部变化都被证明只来自布局或重定位，而不是逻辑改动。`iris_hfi_gen2_handle_session_command` 此前不在 pin 集合内，本次一并加入，pin 数 38 → 39。

## 组件身份

| 组件 | 值 |
|---|---|
| 驱动模块 | `02407c6c69b86483d5d2c7e146b48e251b6b907d2b52553ba252c71b54bd07b6`（12,076,640 B） |
| PID1 | `55d9f4fb08079576f7afe22e82bb7bfb8ba69721f0784e7d033418058426a168`（995,800 B，未变） |
| 镜像 | `559590a85b99b22343c912b431ed7c1eb13eaaa800c290d2f6a3178c1a471a47`（63,348,736 B） |
| 结构验证器 | `cfb2f23c3522fd62df404d5fc35b2b4335c2d47e466309a5ea2c7e7b5590d317` |
| FDE 验证器 | `9c383fefba5d4cafdd9f53d58db8a967ac88a9d234f79fa55b9c1e27b54d2994` |

驱动 ABI 面完全未变：179 imports / 180 versions，零新增、零删除、零 CRC 变化。

## 边界

- 这是**固件侧确认**，不是 DMA 停止证明。ACK 说明固件接受了 CLOSE，不等于硬件已停止访存。
- 安全回收、真实 free、close/reopen、30 轮会话、720p/1080p、600 秒负载、seek/flush/DRC、原生 FFmpeg 与影视任务仍未验收。
- `hardware_decode_verified` / `frame_acceptance_verified` 在准入与清单中仍为 false。
- 本方案的整会话回执借用了 owner 槽位；若日后需要同时导出多个会话级事务，应改为独立记录类型，而不是继续复用槽位。