# SPDX-License-Identifier: CC-BY-4.0

# 状态与验收范围

更新时间：2026-11-16。这里是脱敏成果摘要，不是原始设备日志或认证物理证明。逐功能适配内容、实测数据和限制见 [功能明细](features.md)。

## fnOS 系统阶段

官方 `6.18.18.c1147-trim` 内核与 fnOS arm64 用户态在设备运行。已进行 systemd、Btrfs存储、WebUI、Docker、SMB、WiFi与正常重启验证；Adreno 730 GPU计算和网页利用率/温度监控已有实机证据，电池经ADSP/PDR/battmgr链路出数并在UPower/UPS页面显示。

不应据此推断所有fnOS功能都稳定。仍有自动更新不适配手机启动分区、GPU显存统计缺失、UPS阈值/低电量回归不足、完整存储卸载EBUSY以及部分电源管理限制。后期实验只进行RAM启动，没有将Iris生产负载持久安装。

## 视频的五级状态

| 等级 | 含义 | 当前情况 |
|---|---|---|
| 1 | 源码适配 | 已有公开源码 |
| 2 | 离线构建/协议/布局/行为测试 | 指定版本与指定范围通过；完整ABI仍未知 |
| 3 | 驱动绑定、固件协商等 | 私有隔离实验中已运行，不等于生产准入 |
| 4 | 真实解码任务 | 固定H264三帧→128×128 NV12隔离单会话已接受 |
| 5 | fnOS自带FFmpeg与影视真实任务、生命周期/重复/长负载/重启回归 | **未完成** |

### 已接受的原隔离实验

三帧NV12每帧24576字节，PTS为1000000、1040000、1080000微秒，逐字节符合固定oracle。最后将客户端上限由8扩为32，容纳固件返回的16个CAPTURE allocation。

此后BITSTREAM与RAW STOP分别取得身份匹配的成功ACK。INPUT累计3次提交全部报告归还；CAPTURE累计19次提交全部报告归还，停流后QUEUED/READONLY/unknown位图为0。19包含重新提交，不是19个不同allocation。

该实验保留所有内部backing，未验收CLOSE/free/PM teardown、DMA停止、安全回收或可重复会话。hash/nonce提供关联与完整性，不是认证物理来源。不能把原始报告的false值改成成功。

### 最新研究后继

逐allocation代际账本、当前提交/RELEASE身份校验、畸形回包严格分派与reclaim0取消入口守门均已实现和host测试。一次加固RAM实验在runner加载阶段超时，客户端未启动，未产生新帧或PERSIST回包。

发现冷初始化持core mutex时，新session-held电源快路径再次取同一把锁；该helper又要求已有会话，与cold INIT无会话条件矛盾。单变量后继移除checked入口的这个shortcut，恢复完整checked provider初始化。普通O2、ASan/UBSan和GCC12目标构建通过，但**该修复镜像尚未上机**。loader诊断没有内核PC/锁现场，不能称直接观察到设备自锁。

公开副本关闭执行门，是新的发布版本，不把原镜像hash当作这份副本已上机的证明。

### PERSIST 生命周期修复（两次真机复现）

冷初始化自锁修复后一次RAM实验首次越过此前的loader超时：客户端启动、解出三帧、双STOP ACK匹配（98/105、99/106），但quiesce在stage 2以`-110`超时。45条180B回执中APP INPUT 3/3、APP CAPTURE 16/16、BIN/COMV/NON_COMV/LINE各2、DPB 16全部归还，唯独PERSIST发出RELEASE请求后`returned=0`、`rx_generation=0`。

根因：上游`dec_ip_int_buf_tbl`不含PERSIST，它只在close时force销毁；本方案却单独发HFI RELEASE，固件不会回该回包，1秒硬件响应超时后判定失败。这是设计错误，不是硬件故障。

单变量修复把PERSIST移出release列表，改由校验通过的CLOSE ACK retire，回执`flags`bit 5记录该状态；物理存储全部保留，reclaim门仍为0。两次独立RAM观测均取得`control_rc=0`、`lifecycle_stage=5`、`cpu_drained=1`、`orphaned=0`，三帧PTS与oracle一致，独立FDE验证两次通过。

**这是单变量生命周期修复的条件验收，不是安全回收、DMA停止、close/reopen或原生影视验收。** 详见[PERSIST生命周期修复验收](fnos-persist-fix-acceptance-20261116.md)。

### 自动重启到 fastboot：DT 方案已回退（2026-11-17）

曾确认可行：给生产 dtbo 加 `nvmem-reboot-mode` 节点 + 加载三个上游模块（SDAM / reboot-mode / nvmem-reboot-mode）后，`systemctl reboot --reboot-argument=bootloader` 实测把设备直接送入 fastboot，无需人工按键。这三个驱动都是 `tristate`，编成模块即可，不需要重编官方内核。

但把同一节点写进 dtbo 的实现有缺陷：解析脚本用错 dtbo 表步长（44 而非真实 32），只看到 4 个假条目，把 entry 0 扩大后覆盖了相邻 entry 1 的前 177 字节。分区已用原始备份回退（`fastboot flash` 写入回执 OKAY），**但设备随后落入隔离 HOLD、串口静默，回读哈希未确认**。

正确的整表重排脚本已完成离线实现与结构自检（44 条目、无重叠、偏移与 total_size 全部重算），未上机。详见[dtbo 修改事故与回退](fnos-dtbo-incident-20261117.md)与[全自动重启到 fastboot](fnos-auto-reboot-to-fastboot-20261117.md)。

期间查明 `fastboot boot` 失败的另一个原因是已知的 `slot-retry-count:b=0`，`fastboot set_active b` 可恢复——与本文档既有的 B 槽 `slot-successful` 未持久置位问题一致。

### 串口恢复入口：未验证

为免除现场按键而加入的串口触发重启入口经过五版迭代，**至今没有任何一版在真机上成功触发重启**。发现的错误依次为`pause()`阻塞（串口输入不产生信号）、描述符取自只在成功路径赋值的变量、在observer子进程使用flow子进程设置的全局、把fd号写进共享内存（fd是进程局部的，跨进程无效）。当前版本改用已有的`SCM_RIGHTS`通道，编译通过但未上机。

该入口即使工作也只能重启到正常启动目标，不能进fastboot：官方Image未编入`CONFIG_NVMEM_REBOOT_MODE`，且历史记录已证实`systemctl reboot --reboot-argument=bootloader`会回原系统。详见[恢复入口状态](fnos-recovery-entry-status-20261116.md)。

## 待完成

原始CLOSE ACK字段导出与关联、CPU响应排空的物理证明、实际安全回收、真实close/reopen、30轮会话、720p/1080p与至少600秒原生负载、seek/flush/DRC/错误取消、影视选择Iris且无CPU静默回退，以及持久部署/正常重启/回滚和系统回归。

HEVC、硬件编码和NPU是独立能力，不在当前通过范围。
