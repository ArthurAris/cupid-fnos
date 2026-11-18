# 生产 fnOS 硬解打通：自带 FFmpeg 解真实 H264 — 2026-11-18

## 结果

生产 fnOS（`6.18.18.c1147-trim`，B 槽单系统）在不改内核、不刷非 boot 分区的前提下，持久加载
iris 驱动全链路，用系统自带 `ffmpeg 8.1.1-mediasrv` 解完一段 10 秒 1280x720@30 H264：

```
[h264_v4l2m2m] Using device /dev/video0
frame= 300 fps=0.0 q=-0.0 Lsize=N/A time=00:00:10.00 speed= 34x elapsed=0:00:00.29
Input stream #0:0 (video): 300 frames decoded; 0 decode errors;
Exiting with exit code 0
```

- 300/300 帧、0 解码错误、0.29 秒完成（**34 倍实时**）、退出码 0。
- 硬件计数：iris 中断（GIC SPI 206）从 **2 → 533（+531）**，硬件确实在跑。
- 设备节点：`/dev/video0` = `qcom-iris-decoder`；`init_only_status` 八阶段全绿
  （`run_done=1, run_errno=0, ack=1, fw_booted=1, stage=7(RESPONSE)`）。
- 一次性语义符合设计：本次 FFmpeg 是该开机的**唯一真会话**，结束后 `retain_close`
  退休 core；第二次运行得到预期的 `ESHUTDOWN`。重复解码需重启（见“已知边界”）。

## 持久化与开机自动装载（2026-11-18 晚补）

三个模块固定落在 `/usr/local/lib/cupid-hwdecode/`（sha：driver `7bb3be3d…`、
runner `c532663d…`、videocc `17a7849d…`），固件在 `/lib/firmware/qcom/vpu/`，
脚本 `/usr/local/sbin/cupid-iris-load.sh` + 单元
`/etc/systemd/system/cupid-iris-load.service`（`enabled`，`Type=oneshot` +
`RemainAfterExit`，幂等：已加载直接退出）。开机 journal（三次启动逐字节一致，
`boot2-unit.log` 与 `boot3-unit.log` sha 同为 `c0ec7c84…`）：

```
cupid-iris-load: dependencies ok
cupid-iris-load: modules loaded
cupid-iris-load: ok: qcom-iris-decoder on /dev/video0
```

即：**重启后零手动干预自动出现 `/dev/video0`，直接可解码**（三次开机验证：
boot1 720p 会话 IRQ+534、boot2 1080p、boot3 600s，均在自动装载后完成）。

### fastboot 全自动救援（retry 耗尽路径）

B 槽 retry 每次启动 -1；实验中耗尽到 0 落入 fastboot（无需物理按键）。观察脚本
的轮询在 fastboot 出现时自动执行 `fastboot set_active b`（0→7 复位）+
`fastboot reboot` 回 fnOS，随后任务继续——本次 600s 验收就是被它救回后完成的，
**全程零物理干预**。

## 分辨率与负载验收

| 项 | 结果 | 证据 |
|---|---|---|
| 720p 10s H264 | 300/300 帧、0 错、**34×**、0.29s | `ff5.log`（speed=34x） |
| 720p 开机回归会话 | IRQ **2→536（+534）**，自动装载后 | `ff7.log`+`irqb/irfa` |
| **1080p** 10s H264 | 1920x1080，300 帧、0 错、1.06× 实时 | `ff8.log`（Input 行 1920x1080） |
| **600 秒持续负载** | **frame=18000、time=00:10:00.00、fps=30、speed=1x、0 错**；IRQ **2→31,471（+31,469）** | `ff9.log` / `ff9-final.txt` |
| **CABAC+B 帧复杂流** | 600/600 帧、20s、**32.2×**、0.62s、0 错；IRQ **1→1,072（+1,071）** | `ff10.log` |
| 留存内容 | 全部素材（720p/1080p/CABAC/concat 清单）在设备 `/data/tmp/bringup/` | sha 见对应 `.build/genx*.txt` |
| 二次运行（一次性语义） | 预期 `ESHUTDOWN` | `ff6.log` |

600s 输入用 concat demuxer 把 10s 1080p 片拼 60 份（`loop600.txt`）+
`-re` 实时喂入，单会话完成（`-stream_loop` 在该组合下不循环，已弃用）。
CABAC 片：`testsrc2` 20s 720p、`cabac=1:bframes=3:ref=4:8x8dct=1:me=hex:g=60`
（sha `05086f30…`），产物 `real-cabac720.mp4`。

### 运行门槛：电量怎么读

本机**没有** `/sys/class/power_supply/battery`；电量在
**`/sys/class/power_supply/qcom-battmgr-bat/capacity`**（`status`、
`voltage_now` 同目录；USB 输入在 `qcom-battmgr-usb/`）。AGENTS 的“≥40%”门槛
一律用该路径。2026-11-18 晚一次“四路失联”即电量耗尽（capacity 一直是读不到的
空值导致没预警）；充电后 capacity=48、status=Charging 即恢复。

### 构建可复现

同源重建（`make-prod-driver.py` → `build-reclaim.py`）产物与现役部署模块
**逐字节一致**：`7bb3be3decc0c369cb4d355bca40396d8a437122472f34303f204a36db599c0c`。

### 影视 App 现状与接入路径（本次查明，未做 UI e2e）

- 影视 = Web 前端（`trim.media`，端口 8005）+ 后端 `trim-media` + 媒体核心
  `mediasrv`（`/usr/trim/bin/mediasrv`，同一 `8.1.1-mediasrv` FFmpeg 构建）。
- **`mediasrv` 二进制自带全部 V4L2 M2M 解码器名**（`h264_v4l2m2m`、`hevc_v4l2m2m`、
  `vp8/vp9/mpeg*_v4l2m2m` 等），并有 `HlsTranscoder`/`VideoTranscoder` 与
  `avcodec_find_decoder_by_name`；前端"转码解码方式"枚举**包含 `V4L2_M2M`**
  （soft/qsv/vaapi/nvdec/rkmpp/vulkan/v4l2_m2m），即影视具备把转码解码指到
  我们驱动的原生选项。
- 但当前**媒体库为空**（`trimmedia.db` 的 `item`/`item_media` 均 0 条），播放走
  直连（浏览器端解码）；要触发设备侧硬解，需要：建媒体库 → 上传浏览器不能直放的
  片源（如 MPEG-2/HEVC 非直连组合）→ 播放设置里把“转码解码方式”设为 V4L2_M2M
  → 播放触发 HLS 转码。相关 API 面：`/api/v1/play/index`、`/app/internal/service/play`
  （内部服务走 Unix socket，带 `sys_secret` token 机制）。
- 这是独立的“影视转码接硬解”工作项（应用层配置 + 库扫描 + 强制转码触发），
  与本文件的驱动/解码栈验收解耦；本次硬解成果不依赖它。

## 生产 DT 手术（boot_b 两次就地 8 字节补丁）

生产 DT 里 `video-codec@aa00000` 与 `clock-controller@aaf0000`（videocc，提供
venus/vcodec0 GDSC 与 MVS0 时钟）都是 `status = "disabled"`，无平台设备、无处绑定。

工具：`out/modserve/patch_dt_status.py` —— 纯 Python 定位 FDT（结构块遍历，FDT_PROP
按 token/len/nameoff 12 字节步进），把 `disabled` **原地**覆写为 `okay` 并用 NUL 补齐，
属性长度、结构块、镜像内偏移全部不变（每次 `bytes changed: 8`）。

| 次序 | 节点 | offset | 变化 | 结果 sha256 |
|---|---|---|---|---|
| 1 | `/soc@0/video-codec@aa00000` | FDT 内（boot_b FDT @10904） | disabled→okay | `93ce116d…` → `8bec9112…` |
| 2 | `/soc@0/clock-controller@aaf0000` | status@74316（len 9） | disabled→okay | `8bec9112…` → `7e717ac6…` |

每次：`dd` 快照 → 改副本 → `dd` 写回（`conv=fsync`）→ 回读 `sha256sum` 与副本比对一致。

### 双端备份（AGENTS 纪律）

| 镜像 | sha256 | 设备 | host |
|---|---|---|---|
| 现役（双补丁，已验证可启动） | `7e717ac6a3e5895b5d0203f6c4325a5f7e354bd767a23a1a5818eb857a02381c` | `/data/tmp/dtbp/boot_b.rb.img` | `private/backup/bootb-prod-20261118/boot_b-vcodec-videocc-current.img` |
| 仅 video-codec 补丁 | `8bec9112122700f49b606579fbbcee934b62dd670d91650c8cb550d78970a3d5` | `/data/tmp/dtbp/boot_b.pre-vc.img` | `private/backup/bootb-prod-20261118/boot_b-vcodec-only.img` |

两端哈希逐字节一致（host 侧由设备 `python3 -m http.server 8098 --bind 172.16.42.2`
经 RNDIS 拉取后本机重算）。

## 中途事故：B 槽 retry 耗尽

第一次 videocc 补丁后 `reboot`，设备落入 fastboot（USB 枚举为 `Android Bootloader
Interface`，串口(COM4)长时间消失）。bootloader 只读取证：

```
current-slot: b   slot-retry-count:b: 0   slot-unbootable:b: yes   slot-successful:b: no
```

`slot-successful` 从未被置位 → 每次正常启动都在消耗 retry（与 `known-issues` 第 212 行
的历史记录一致）。恢复用 bootloader 自己的命令（AGENTS 第 4 条）：

```
fastboot -s 73e27502 set_active b   # retry 恢复 7、unbootable:no
fastboot -s 73e27502 reboot
```

随后正常进入 fnOS。补丁本身未被验证为启动失败——是启动前就被 retry 门拒之门外。

## 生产准入变体（构建侧，全部离线）

链条：`iris-gateonly-tree` → `make-prod-driver.py` → `build-reclaim.py` →
`build-prod-runner.py`（均在 WSL，pinned headers `/var/tmp/iris-reqbufs-lock-copied-ljfpcjqg/headers`，
vermagic `6.18.18.c1147-trim SMP mod_unload modversions aarch64`）。

### 驱动 `make-prod-driver.py`（5 处编辑、4 个文件；probe/handshake 代码原样）

1. `iris_probe.c`：`iris_is_init_only_device()` 放宽为 `dev->of_node != NULL`（免私有 token）。
2. `iris_probe.c`：`iris_is_decode_once_device()` 同款放宽 —— init-only 只准备不发布；
   `/dev/video` 来自 decode-once 路径，必须让未改 DT 也能进该模式。
3. `iris_vidc.c`：`iris_close` 对**没碰过队列、没建过 session** 的 close 走正常 unwind，
   不触发 `retain_close` 退休（FFmpeg 探测是 open→…→close→reopen 两段式）。
4. `iris_core.c`：`iris_core_open_put` 的 claim 回收条件由 `native_repeat &&` 放宽为
   `(native_repeat || decode_once) &&` —— 探测型 close 后核心仍 ready，释放一次性
   claim 供 reopen；真会话仍走 retain_close 退休，不可逆语义不变。
5. `iris_vdec.c`：`iris_vdec_try_fmt` 补 0×0 占位回退（与 `iris_vdec_s_fmt` 既有注释
   同源）—— 早期 TRY_FMT 不知道分辨率，`iris_calc_capture_layout()` 对 0×0 直接 -EINVAL。

驱动 sha 演进（每步上机验证）：

| sha256 | 内容 |
|---|---|
| `bdecd787…` | 仅准入 ×2（→ /dev/video0 注册成功） |
| `5f4b9dd8…` | + 生命周期 3/4（→ FFmpeg 探测 close 后可 reopen） |
| **`7bb3be3decc0c369cb4d355bca40396d8a437122472f34303f204a36db599c0c`** | + TRY_FMT 占位（→ **解码成功**），现役 |

### runner `make-prod-runner.py`

`iris-decode-runner-source-q6p0t81k` 的封闭隔离准入放宽两处（bootargs 精确匹配、
DT compatible+token 精确匹配），**供应商钉住、hold/recheck、`iris_decode_once_run`
调用序列原样**。产出 `cupid-iris-decode-runner-prod.ko` `c532663d…`（361,520 B，
对驱动 `Module.symvers` 做 `KBUILD_EXTRA_SYMBOLS` 版本化）。

### 其余组件

- videocc：`cupid_videocc_sm8450.ko` `17a7849d…`（驱动名 `video_cc-sm8450`、兼容
  `qcom,sm8450-videocc`，insmod 即绑）。
- 固件：`private/firmware/cupid-video/` 14 件（`vpu20_4v.mbn/.mdt/.b00-.b19`）打包
  `cupid-vpu-firmware.tar`，解到 `/lib/firmware/qcom/vpu/`（生产原无 `vpu20_4v.mbn`，
  自带 `vpu20_p4_gen2_s6.mbn` 与本驱动无关）。
- 样片：`sample-h264.h264` 与用自带 libx264 现场生成的 `/tmp/real720p.mp4`
  （10s 720p30，sha `a77011ba…`）。

### 装载链（复现步骤，设备端）

```
modprobe videobuf2-dma-contig; modprobe v4l2-mem2mem; modprobe mdt_loader
insmod /tmp/cupid-videocc-sm8450.ko        # DRIVER=video_cc-sm8450
insmod /tmp/qcom-iris-prod.ko              # prepared=1
insmod /tmp/cupid-iris-decode-runner-prod.ko
# init_only_status: admission→response 全 0，run_done=1, ack=1, fw_booted=1
ls -l /dev/video0                          # qcom-iris-decoder
ffmpeg -loglevel debug -c:v h264_v4l2m2m -i /tmp/real720p.mp4 -f null -
```

模块、固件、样片在设备侧持久保存于 `/data/tmp/bringup/`（重启不丢），证据在
`/data/tmp/bringup/ev/`（含 `SHA256SUMS`）。

## FFmpeg 不兼容的诊断过程

### 方法学：dmesg 不可用

生产内核日志被既有的 `spmi spmi-0: disallowed SPMI write` 洪水（实测 ~85µs/行、
~12k 行/秒）瞬间轮转，任何历史行都在亚秒级被挤出（`dmesg | wc -l`=1801，其中 spmi
占 1736；模块 pr_info、开机横幅全部查无）。因此证据链改为三件套：

1. **kretprobe/kprobe 事件**（tracefs 可用、可抓返回值与实参：`iris_core_open_get`、
   `iris_open`、`v4l2_open`、`iris_querycap`(含 caps 读值)、`iris_enum_fmt`、
   `iris_try_fmt_vid_mplane`、`iris_s_fmt_vid_mplane`、`iris_decode_once_reqbufs`(含
   count/type/memory)、`v4l2_m2m_ioctl_streamon`、`iris_decode_once_retain_close`)；
2. **`/proc/interrupts` iris 行前后差**（硬件活跃计数）；
3. **`init_only_status` sysfs**（阶段与错误码）。

排障途中两次踩坑并修正：`trace_pipe` 后台读者会吞掉事件（先杀再抓）；`$retval`/`$arg3`
在 bash 双引号里要 `\$` 转义，否则注册成功但取值为空。

### 根因（FFmpeg 8 源码 + 探针双重实锤）

FFmpeg 8（`apt-get source ffmpeg` 拿 8.0.1，`libavcodec/v4l2_m2m.c`）探测模型：

```
opendir(/dev) → 对每个 videoN: open → QUERYCAP → ENUM_FMT/TRY_FMT(output)
              → ENUM_FMT/TRY_FMT(capture) → close → 下一个/失败
全部失败 → "Could not find a valid device"
成功后 → reopen → S_FMT → REQBUFS → STREAMON → 解码
```

与驱动的冲突两处：

1. **close 即退休**：decode-once 契约在第一次 close 调 `iris_decode_once_retain_close`
   （置 `removing=true`、撤 admission、state=ERROR），且 `decode_once_claimed` 永不清零。
   探测的第一次 close 就烧掉整机本次开机的唯一会话 → reopen 必然 `ESHUTDOWN(108)`。
   证据：`open(get=0) → querycap → [35µs 后] rretain=1 → 再次 open get=-108`。
2. **TRY_FMT 0×0**：FFmpeg 探测 capture 时不知道分辨率，传 `width=height=0`；
   `iris_vdec_try_fmt` 没有 s_fmt 那样的占位回退，`iris_calc_capture_layout()` 对
   0×0 返回 `-EINVAL`。证据：`try(output,H264)=0` → `try(capture,YUV420P)=-95`（被
   `prototype_restricted` 正确拒绝）→ 枚举到 NV12 → **`try(capture,NV12)=-22`**。

另：`-device /dev/video0` 这个 AVOption **不生效**（`ff_v4l2_m2m_codec_init` 无条件
opendir 扫描），只能修驱动侧。

### 修复后复现时间线（kretprobe，pid=ffmpeg）

```
iget=0  iopen=0  rqc(caps=0x84004000|EXT_PIX, dcaps=M2M_MPLANE|STREAMING)
renum=0  rtry(output,H264)=0  rtry(capture,YUV420P)=-95(预期)
renum=0(NV12)  rtry(capture,NV12)=0          ← 占位回退生效
iget=0  iopen=0                              ← reopen 成功，claim 回收生效
S_FMT(out)=0  S_FMT(cap)=0
REQBUFS out cnt=16 mmap=0 → STREAMON=0 → REQBUFS cap cnt=20 mmap=0 → STREAMON=0
[286ms 解码窗口，IRQ +531]
REQBUFS cnt=0 → -22（一次性闸门对释放路径的拒绝，FFmpeg 忽略，无害）
rretain=1                                    ← 真会话退休（设计内）
```

## 证据档案

设备持久区 `/data/tmp/bringup/ev/`（含 `SHA256SUMS`）：

| 文件 | sha256 | 内容 |
|---|---|---|
| `ff5.log` | `9957a84d…` | 720p `-loglevel debug` 全输出（Using device、300 帧、speed=34x、exit 0） |
| `ff6.log` | `b8eb1067…` | 第二次运行，ESHUTDOWN（一次性语义验证） |
| `ff7.log` | `21556b59…` | 开机自动装载后的 720p 会话（IRQ+534） |
| `ff8.log` | `e3c4ca99…` | 1080p 解码（1920x1080、300 帧、1.06×） |
| `ff9.log` / `ff9-final.txt` | `320070e8…` | **600s：frame=18000、time=00:10:00、speed=1x** |
| `ff10.log` | `0398c32b…` | **CABAC+B 帧：600/600 帧、32.2×、0 错** |
| `boot1/2/3/5-unit.log` | `32e3e727…` / `c0ec7c84…`(×2) | 多次开机 systemd 自动装载 journal |
| `trace-ffrun4.txt` | `6518229e…` | kretprobe 全时间线（probe→reopen→S_FMT→REQBUFS→STREAMON→退休） |
| `irqb.txt` / `irfa.txt` | `d2d3e07d…` / `8ec50f5d…` | 600s 前后 iris IRQ 2→31471 |

本机：`private/fnos/.build/evid*.txt`、`ffrun*/bootchk*/load600*` 系列脚本与输出；
备份 `private/backup/bootb-prod-20261118/`。

## 工具学（本次踩过的两个 PowerShell 坑，留档防复犯）

1. **大小写不敏感变量互踩**：`$out = Drain …` 会覆盖参数 `$Out`，导致结尾
   `WriteAllText` 拿到含控制字符的串口文本 → “路径中具有非法字符”，**脚本跑完
   却不落盘**（此前多轮“神秘 ArgumentException”的统一根因）；`$fb = & $FB devices`
   同理毁掉 fastboot 路径变量。后续脚本一律用 `$raw`/`$fbOut`。
2. **PS 5.1 `2>&1` + `$ErrorActionPreference='Stop'`**：原生程序（fastboot）的
   stderr 行会被包装成终止性 `NativeCommandError`。对 fastboot 调用去掉 `2>&1`。

## 已知边界

1. **每开机一个真会话**：close 退休是一次性契约（隔离验收件同款）。重复解码、
   `--sessions 30` 信封、多客户端，需要 native-repeat 路径（DT 加
   `qcom,sm8450-iris-decode-once`/`qcom,iris-native-repeat-token` 属性或恢复
   `native_repeat` 判定），属后续工作。
2. **dmesg 被 SPMI 洪水刷穿**（生产既有问题，非本链路引入）：任何基于 dmesg 的
   取证在该机不可靠；已改用 tracefs + 中断计数 + sysfs。
3. **B 槽 retry 会持续消耗**：`slot-successful` 不置位则每次启动 -1，耗尽即落
   fastboot；恢复法 `fastboot set_active b`（本次实测 retry 0→7）。
4. FFmpeg 收尾的 `V4L2 failed to unmap … buffers` 与 REQBUFS count=0 返回 -22 为
   已知无害噪声（退出码仍为 0）。
5. ~~装载链手动~~ → **已自动化**：systemd `cupid-iris-load` 开机自动装载，
   三次重启回归通过（见上文“持久化与开机自动装载”）。剩余验证面：影视 App
   UI 层走同一 mediasrv/ffmpeg 核心的端到端任务，以及 `--sessions 30` 多会话
   信封（需 native-repeat 路径，独立工作项）。
6. **2026-11-18 21:32 一次“共同失联”——实为电池耗尽**（非挂死）：装载自检刚
   通过、解码任务尚未开始时 USB gadget 消失，随后四路 30+ 分钟全无；充电后
   正常启动查明是电量耗尽。**电量节点：`/sys/class/power_supply/qcom-battmgr-bat/capacity`**
   （本机无 `battery` 节点，此前 40% 门槛读不到）。恢复实测 capacity=48、
   status=Charging、USB 输入 5.08 V；当次 CABAC 会话顺延到次日完成（见下）。

## 复现要点（一次做对清单）

1. 备份 boot_b → `patch_dt_status.py` 两个节点 → 写回回读哈希。
2. retry 异常先 `getvar`，耗尽用 `set_active b`（bootloader 命令，不手写位）。
3. 构建顺序固定：`make-prod-driver` → `build-reclaim` → `build-prod-runner`；
   生成器用 `ignore_errors`+`dirs_exist_ok` 规避 Windows 目录句柄锁；产物必须
   **按内容验证**（文件数、编辑标记、sha 变化），不信任 `$?`。
4. 设备侧：先固件后模块、runner 最后；FFmpeg 证据一次抓齐（debug 级 + trace + IRQ）。
