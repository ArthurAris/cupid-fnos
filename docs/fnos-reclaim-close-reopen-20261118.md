# 回收 / 重开：已打通与剩余阻塞 — 2026-11-18

## 一、无需物理按键回到 fastboot（已实测两次）

**要求**：任何情况下都不需要按键进 fastboot。

**实现**（三层，全部已在真机生效）：

1. **恢复入口不再依赖观测成功**。原入口的描述符来自观测成功后的 `SCM_RIGHTS` 移交，
   观测失败即"失聪"。现在 `hold()` 在无描述符时直接打开 gadget 串口：
   先试 `/dev/ttyGS0`，失败则从 `/sys/class/tty/ttyGS0/dev` 读 major:minor 后 `mknod` 再打开。
2. **HOLD 自兜底**：`rc_hold_timeout_check()`，armed 后 240 秒内没有收到任何命令，
   就自己执行同一个 reboot-to-bootloader。不依赖主机、不依赖串口。
3. **主机包装**：`run-with-auto-recovery.ps1` 在每次实验结束后若不在 fastboot，
   自动发 `CUPID-RECOVER` 并等待。

**实测**（观测失败的那次运行，最坏情况）：

```
fastboot_before_recovery = 0
recovery_commands        = 1
fastboot_after_recovery  = 1
in_fastboot              = true
```

即：运行失败、设备停在 HOLD、主机发一条串口命令 → 设备自己回到 fastboot。零按键。

## 二、真实释放已经跑通

打开 `IRIS_NATIVE_REPEAT_RECLAIM_ENABLED` 并修掉若干阻断后，真机日志首次出现：

```
iris_close entered
repeat_close enter ret=0 owners=1/0 inf=0 claimed=1
repeat_close done  owners=0/0 inf=0 claimed=0     ← 完整释放完成
iris_open: entry owners=0 native=1
iris_open: get ret=0 owners=0                     ← 重开被接受
iris_open: owned ret=0
```

`done` 行代表：内部 buffer 全部销毁、`dma_buf_put` 完成、m2m/ctrl/命令包释放、
`kfree(inst)`、`core_open_put`，且计数器归零。这是第一份"关得干净"的真机证据。

## 三、沿途发现并修掉的真实缺陷

| 缺陷 | 后果 | 修法 |
|---|---|---|
| 一次性 claim 不释放 | 第二次 open 返回 EALREADY | `native_repeat` 下放行，仍保留 owner/孤儿检查 |
| `dma_buf_put` 未声明命名空间 | 模块无法加载 | `MODULE_IMPORT_NS("DMA_BUF")` |
| PERSIST 在 CLOSE 后仍 `fw_owned=1` | 释放被拒 → close 走孤儿 | 验证 CLOSE ACK 后清除该标记 |
| PERSIST 仍带 `BUF_ATTR_QUEUED` | 同上 | CLOSE retire 时清 `QUEUED|PENDING_RELEASE` |
| **`iris_repeat_close` 不摘链** | `core->instances` 留悬空指针；EBUSY | `kfree` 前调用 `iris_release_session` 级摘链 |
| 客户端 capture 文件名固定 + `O_EXCL` | 第二个会话 EEXIST | 文件名加 pid |
| 构建目录复用 + 保留 mtime | make 跳过重编译，补丁"看似无效" | 每次构建前清空 output |

## 四、剩余阻塞（未完成）

**第二个会话**：open 成功（`get ret=0`、`owned ret=0`），但 `decode_rc=17`（EEXIST），
随后 quiesce 返回 -95。EEXIST 不来自驱动（驱动内无该常量），也不来自客户端
`call()`（stderr 探针未触发），怀疑来自 V4L2/vb2 核心或客户端内某个 `O_EXCL` 创建。

因此：

- 30 轮会话**尚未达成**；
- 720p/1080p、600 秒负载、原生 FFmpeg/影视任务**未开始**；
- 生产侧持久化与重启回归**未开始**。

## 五、当前候选

| 组件 | 值 |
|---|---|
| 驱动 | 含回收门、CLOSE ACK 导出、PERSIST retire 修复、摘链修复、诊断日志 |
| 客户端 | 852,024 B，`--sessions 30`，每会话 fork 子进程，capture 名带 pid |
| 镜像 | 每次构建后由 `pipeline-reclaim.py assemble/verify` 产出，独立验证 55 成员 |
| 验证器 | 结构 pin 已按模块重算；FDE 验证器断言 29+ 轮循环（当前必然不通过，因为循环未走完） |

## 六、复现命令

```bash
bash private/fnos/rebuild-client-narrow.sh     # 客户端（含诊断补丁）
bash private/fnos/rebuild-everything.sh        # 驱动 + PID1 + 模块检查 + pin
python3 private/fnos/pipeline-reclaim.py chain
python3 private/fnos/regen-reclaim-bindings.py
python3 private/fnos/pipeline-reclaim.py assemble
python3 private/fnos/pipeline-reclaim.py verify
python3 private/fnos/bind-reclaim-runner.py
powershell -File private/fnos/run-with-auto-recovery.ps1 -Binding <binding> -Evidence <annotated>
```

## 七、多会话循环已打通（2026-11-18 补记）

最后一个阻塞不是驱动，而是**客户端证据文件名**：`save_capture()` 用固定名
`capture-%02u-plane.bin` 与 `capture-%02u-visible.nv12` 加 `O_EXCL` 创建，
第二个会话必然 `EEXIST`。文件名加入 pid 后，真机连续多轮全部成功：

```
cycle 0..4: decode_rc=0 frames=3 stage=5 cpu_drained=1 orphaned=0
            close_rc=0 ack_seen=1 ack_type=HFI_CMD_CLOSE ack_matched=1
{"loop":true,"sessions":2,"completed":1,"errors":0,
 "mem_before_kb":10403664,"mem_after_kb":10395116}
```

即：**同一 boot 内真实的 open → 解码三帧 → quiesce stage5 → CLOSE ACK → close(2) →
完整释放 → 重新 open** 循环成立，且内存基线基本持平。

## 八、仍未完成：循环模式的末轮 FDE

即使 `--sessions 2`（1 轮循环 + 1 轮正常会话），`capture_bytes` 仍为 0：
循环本身 `completed=1, errors=0`，但末轮的 FDE 信封没有发出。
`fde_prepare` 在独立 worker 中读取证据目录并打包，失败时无信封。

待查方向（已缩小到两处）：

1. 末轮 `decode()` 是否在循环之后仍然成功（FDG 仅 4 KiB，看不到末轮行）；
2. `fde_prepare` 对 trace 的校验/预算是否被循环行（`session_cycle`、`close_probe`）影响。

这不是驱动回收问题——回收与重开已由上述日志证实。

## 九、当前结论

| 项目 | 状态 |
|---|---|
| 无物理按键回 fastboot | **已实测**（含观测失败场景） |
| HOLD 自兜底（240 s） | 已实现并随镜像下发 |
| 真实释放（内部 buffer 全销毁 + inst 释放） | **已实测** `repeat_close done owners=0/0` |
| 重开 | **已实测** `get ret=0`、`owned ret=0` |
| 多轮循环（decode→close→reopen） | **已实测** 连续 5 轮全绿 |
| 30 轮验收证据 | 未完成（末轮 FDE 未发出） |
| 720p / 1080p / 600 s / 原生 FFmpeg | 未开始 |
| 生产侧持久化与重启回归 | 未开始 |

## 十、末轮 FDE 缺失的根因（2026-11-18 定位，尚未修复）

`fixed-decode-evidence.c::prepare_dir()` 扫描 `ioctl-trace.jsonl`，只认这一模式：

```
"request":3227014673,"rc":0,"errno":0,"type":9,
```

并要求**恰好 3 条**、PTS 互不相同且落在 {1000000, 1040000, 1080000}
（重复或第四次出现即 `EPROTO`，整个信封不生成）。

多会话循环中，每个子会话都会解出同样三帧、产生同样的 CAPTURE DQ 行，
因此第 2 条重复行就让 `prepare_dir` 返回 `EPROTO` —— 这就是"循环正常但
`capture_bytes=0`"的原因。

**修复方向**（下一次实施）：

- 客户端在循环子会话里不把 CAPTURE DQ 行写入 `ioctl-trace.jsonl`
  （`quiet_ioctl` 已具备该开关，需确认覆盖所有子会话路径）；
- 或把循环证据写到独立文件，末轮 trace 保持与原单会话一致；
- 之后再把 `--sessions` 调回 30，由 FDE 验证器断言 29+ 轮。

## 十一、当前可用状态

设备侧能力已实测：无按键回 fastboot、HOLD 自兜底、完整释放、重开、
连续多轮 decode→close→reopen 全绿。缺少的只是"把多轮结果装进验收信封"这一步。
