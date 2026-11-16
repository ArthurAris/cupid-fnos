# SPDX-License-Identifier: CC-BY-4.0

# 状态与验收范围

更新时间：2026-11-16。这里是脱敏成果摘要，不是原始设备日志或认证物理证明。

## fnOS 系统阶段

本仓库仅记录fnOS适配。主线Linux/Ubuntu/Phosh的显示、触摸和桌面成果属于独立仓库，不是这里的官方fnOS内核显示验收。

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

## 待完成

PERSIST真实RELEASE归还、原始CLOSE ACK关联、CPU响应排空/实际安全回收、真实close/reopen、30轮会话、720p/1080p与至少600秒原生负载、seek/flush/DRC/错误取消、影视选择Iris且无CPU静默回退，以及持久部署/正常重启/回滚和系统回归。

HEVC、硬件编码和NPU是独立能力，不在当前通过范围。
