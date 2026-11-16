# dtbo 修改事故与回退 — 2026-11-17

**这是一次真实的破坏性操作记录。** 我修改 `dtbo_a` 时用错了 dtbo 表结构，覆盖了相邻条目 177 字节。分区已用原始备份回退，但设备随后落入隔离 HOLD 且串口静默，未能完成回读确认。

## 错误过程

目标是给生产 DT 加 `nvmem-reboot-mode` 节点，使 `reboot bootloader` 可用（此前已实测成功一次）。

我先写了 `build-dtbo-modified.py`，其中用：

```python
total, hdr, entry, count, size, page = struct.unpack_from('>6I', data, 8)
```

这里有两个错误：

1. **字段顺序错位**：从 offset 8 读 6 个 u32 得到的是 `header_size, dt_entry_size, dt_entry_count, dt_entries_offset, page_size, version`，不是我以为的 `total, header, entry, count, size, page`。
2. **entry stride 用错**：我按 44 字节步长遍历，而真实 `dt_entry_size` 是 **32**。因此只识别出 4 个"条目"（实为错位读取的垃圾），看不到真实的 44 个。

### 造成的破坏

真实布局（正确解析后）：

```
entry 0: offset 1440   size 501960  end 503400
entry 1: offset 503400 size 416179  ← 紧接 entry 0，间隔 0 字节
```

我新增的节点让 entry 0 的 DTB 从 501960 涨到 502146 字节。原脚本把它写到 `1440..503586`，**覆盖了 entry 1 的前 177 字节**。

更糟的是，验证脚本 `verify-dtbo-modified.py` 用了同样的错误步长，所以它报告"只有预期区域变化、4 个条目全部可解码"——**假阳性**。我据此执行了写入。

技术设计考虑：**我的离线自检脚本与生成脚本共享同一个错误假设，因此无法自证。** 正确做法是用独立实现（如 `mkdtimg dump`）交叉验证表结构，而不是用自己的解析器验证自己。

## 回退

设备当时在 fastboot，执行：

```
fastboot flash dtbo_a 原始 dtbo 分区备份
  Sending 'dtbo_a' (24576 KB)  OKAY
  Writing 'dtbo_a'             OKAY
```

写入源为 2026-10-27 的原始备份，SHA256 `c78e3734728b8ed6f8f28897d963ddb0d0757cdc7b33d1ba6ebed52a4cfad9e6`，写前在本机重新校验通过。

**回读确认未完成**：设备随后进入隔离 HOLD，串口静默、RNDIS 不可达，无法执行 `dd if=/dev/sde18 | sha256sum` 验证。因此"dtbo_a 已恢复"目前只有 fastboot 的写入回执，没有设备端哈希证据。

## 附带查明的无关问题

期间 `fastboot boot` 报 `Failed to load/authenticate boot image: Not Found`。起初怀疑是 dtbo 损坏，实际是文档已记录的已知问题：

- `slot-retry-count:b` 为 **0**、`slot-successful:b` 为 no
- 执行 `fastboot set_active b` 后 retry 恢复为 7，`fastboot boot` 立即 `OKAY`

这与 `known-issues.md` 第 186 行记录的恢复法一致（B 槽 `slot-successful` 未持久置位，反复重启可能回 fastboot）。

## 正确的重排方案（已离线实现，未上机）

`repack-dtbo.py` 用正确字段顺序与真实 stride=32 重排整个表：

- 正确解析出 **44 个** popuplated 条目
- entry 0 增长到 502116 字节（+156）
- 其余 43 个条目内容不变，整体后移 156 字节
- 保持原镜像的**连续排布**（原镜像并非页对齐：entry0 在 1440，entry1 在 503400）
- 重算每个 `dtb_offset`、每个 `dtb_size` 与头部 `total_size`（16628996 → 16629152）
- 独立校验：44 个条目全部是合法 FDT、**无任何重叠**、全部在分区内
- 输出 `重排后的 dtbo 镜像（私有留存）`，SHA256 `359b5bc004da0c732362a2f4bd32361ce72ad7ad5d1712129fb6b6b24b125c25`

**未上机**，也未写入设备。

## 现状与门槛

| 项 | 状态 |
|---|---|
| dtbo_a 恢复 | 已 flash 原始备份（写入回执 OKAY），**回读未确认** |
| 设备可控性 | 隔离 HOLD，串口静默、RNDIS 不可达 |
| 恢复入口能否触发 | 本次实测未响应（见下） |
| 正确重排脚本 | 离线完成、结构自检通过、未上机 |

## 恢复入口在这一状态下的局限

隔离镜像的恢复入口走 `SCM_RIGHTS` 接收串口 fd，而 `hf_receive_metadata_once` 要求 `confirmed==1`（flow 正常退出且未标记失败）。因此：

- **观测成功的 HOLD** → 可以拿到 fd → 入口可武装
- **观测失败的 HOLD** → 拿不到 fd → 入口永久失聪

本次设备处于后者，因此串口写 `CUPID-RECOVER` 无任何效果。这不是命令字面量或 DTR/RTS 的问题（两者都已验证）。

这是设计层面的真实限制：恢复入口目前只能在"顺利结束"的实验后生效，无法在失败后救场。若要覆盖失败路径，需要让 flow 在退出前自己保留一份可用的 tty 描述符并交给 PID1，而不是依赖 `confirmed` 门控的 handoff 通道。

## 下一步门槛

必须先物理重启设备到 fastboot 或生产系统，才能：

1. 回读 `dtbo_a` 确认哈希等于 `c78e3734…`
2. 决定是否部署重排后的 dtbo（需重新走一遍验证）
3. 继续硬解验收

在此之前不做任何分区写入。
