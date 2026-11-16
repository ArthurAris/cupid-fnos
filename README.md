# Xiaomi 12 (cupid) — Mainline Linux & fnOS Research

小米 12（`cupid`、SM8450 / Adreno 730）主线 Linux 移植与 fnOS 适配的**源码研究仓库**。

本仓库整理主线 Linux/Ubuntu/Phosh、fnOS 系统适配、GPU/ADSP，以及近期 Iris H264 解码的源码与离线验证工具。**不是即刷即用 ROM，不提供固件、系统镜像或分区备份。**

> 实验项目，无任何担保。错误刷写可能导致数据丢失或设备无法启动。先核实设备、完整备份并验证恢复材料；优先 RAM 启动。不要复制其他设备的分区号、槽位、地址或凭证。

## 当前成果与边界（2026-11-16）

| 部分 | 已记录成果 | 未完成/限制 |
|---|---|---|
| 主线 Linux + Ubuntu/Phosh | 120Hz DSC 显示、触摸、GPU 合成、CPU 调频、ADSP 电池、USB 网络、无线联网与远程桌面 | 音频、蓝牙、GPS、自动旋转、挂起等不完整；不是当前 fnOS 显示验收 |
| fnOS 系统 | 官方 `6.18.18.c1147-trim` 上运行 systemd、存储、WebUI、Docker/SMB；WiFi、GPU 计算/监控、ADSP 电池已进行设备验证 | 更新兼容性、安全完整停机、显存统计及长期稳定性仍有限制 |
| Iris H264/NV12 | 隔离单会话：固定三帧 128×128 NV12，与 oracle/PTS 一致；BITSTREAM/RAW 两个 STOP ACK 匹配；INPUT 3/3、CAPTURE 19/19 次提交报告归还 | 内部 backing 保留；不证明 DMA 停止或安全回收。CAPTURE 是 16 个 allocation，不是 19 个不同缓冲 |
| 最新生命周期研究 | 逐 allocation 所有权、严格回包分派、取消入口守门、冷启动电源自锁修复；主机测试与目标模块构建已完成 | 最新修复未上机通过；PERSIST、close/reopen、重复任务和长视频未验收 |

**fnOS 原生影视 H264 解码 level 5 尚未达成。** HEVC、硬件编码、NPU、全硬件转码也未验收。源码中的计数、hash、ACK 或 host 测试不能代替物理 DMA/真实原生任务证据。

## 目录

- [`sources/iris/`](sources/iris/)：近期 Iris 源码；已验证原实验的基线与离线研究后继分开存放，公开副本的执行门默认关闭。
- [`tests/iris/`](tests/iris/)：可公开的实际 C 路径测试；host facade 和真实设备验证严格分开。
- [`patches/`](patches/)：历史主线内核改动、配置与 fnOS 存储修复；有外部输入/未包含材料，不保证直接组成完整可编译系统。
- [`tools/`](tools/)：构建、验证、事务部署及适配工具。历史部署工具不能套用于当前系统。
- [`docs/status.md`](docs/status.md)：按范围区分已验证与未验证状态。
- [`docs/building.md`](docs/building.md)：依赖、构建与离线验证入口。
- [`docs/safety.md`](docs/safety.md)：设备及数据安全规则。
- [`docs/source-provenance.md`](docs/source-provenance.md)：源码来源、发布副本与原实验版本的关系。
- [`docs/not-included.md`](docs/not-included.md)：不公开的材料和再分发边界。
- [`scripts/check-public-release.py`](scripts/check-public-release.py)：公开快照与 Git 对象扫描。

## 使用前

先阅读 [构建说明](docs/building.md)、[安全规则](docs/safety.md) 和各目录 README。**不提供一行刷机命令**。匹配设备、内核版本、ABI、依赖及完整备份均须使用者重新验证，不能复用原设备的准入材料。

原实验使用自行合法取得的设备固件和官方内核材料；它们不在本仓库。公开 Iris 驱动入口默认关闭，本仓库不隐式部署或修改 bootloader 状态。**部分历史安装器在显式调用后会启用服务供后续启动使用，rootfs 配置脚本也会在目标文件系统中启用桌面服务**；务必按各工具说明审核，不将源码存在视作操作授权。

## 许可证

- 本项目原创代码与 Linux 派生源码：**GPL-2.0-only**，第三方文件原有更具体的许可证优先。
- `tools/mkbootimg/` 中 AOSP 文件：**Apache-2.0**，保留原始声明。
- 本项目原创说明文档：**CC-BY-4.0**。

详细适用范围和上游归属见 [`NOTICE.md`](NOTICE.md) 与 [`LICENSES/`](LICENSES/)。许可不授予 fnOS、原厂固件或其他未包含材料的再分发权。
