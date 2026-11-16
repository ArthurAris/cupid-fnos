# Xiaomi 12 (cupid) — fnOS 适配研究

这是小米12（`cupid`、SM8450 / Adreno 730）的 **fnOS 专用源码研究仓库**，整理官方内核适配、存储、WiFi、GPU、ADSP/电池，以及 Iris H264 解码研究。

> 实验项目，无担保。不是即刷即用ROM，不提供fnOS用户态、固件、系统镜像或分区备份。设备操作前重新核身份、版本、完整备份和恢复材料；优先RAM启动，不照抄其他设备的分区或槽位。

## fnOS 成果与限制（2026-11-16）

| 部分 | 已记录成果 | 未完成/限制 |
|---|---|---|
| 系统与存储 | 官方 `6.18.18.c1147-trim` 上运行 systemd、Btrfs、WebUI、Docker/SMB，并进行了正常重启验证 | 更新兼容性、完整安全停机和长期稳定性仍有限制 |
| 网络 / GPU / 电池 | WiFi、Adreno 730计算与利用率/温度监控、ADSP电池及UPower/UPS显示有设备验证 | 显存统计、UPS保护阈值/低电量回归及部分电源管理未完成 |
| Iris H264/NV12 | 隔离单会话固定三帧128×128 NV12，oracle/PTS一致；双端口STOP ACK匹配；INPUT 3/3、CAPTURE 19/19次提交报告归还 | 内部backing保留，不证明DMA停止或安全回收。CAPTURE为16个allocation |
| 生命周期研究 | 逐allocation所有权、严格回包/取消入口守门、cold-init电源自锁修复，host测试和目标构建完成 | 最新修复未上机通过；PERSIST、close/reopen、重复和长视频未验收 |

**fnOS自带FFmpeg与影视的原生H264解码 level5 尚未完成。** HEVC、硬件编码、NPU及全硬件转码也未验收。hash、计数、ACK和host测试不能替代物理DMA/真实原生任务证据。逐项适配细节与实测数据见 [`docs/features.md`](docs/features.md)。

## 目录

- [`docs/features.md`](docs/features.md)：**按功能逐项列出适配内容、实测证据与限制**（系统/存储/WebUI/WiFi/GPU/监控/ADSP/电池/UPS/Iris）。
- [`build-inputs/`](build-inputs/)：官方 c1147 头文件/`Module.symvers` 与 ADSP 的 5 个 PAS 源。
- [`tools/fnos/`](tools/fnos/)：fnOS存储、GPU、ADSP、电池、ABI、硬解验收与事务部署工具。
- [`sources/iris/`](sources/iris/)：原成功隔离实验基线与最新研究后继；公开执行门默认关闭。
- [`tests/iris/`](tests/iris/)：实际C路径的host测试，明确facade边界。
- [`patches/kernel/`](patches/kernel/)：fnOS存储补丁研究；目标源码/适用性需核验。
- [`tools/initramfs/`](tools/initramfs/)：历史fnOS PID1研究，不是当前完整官方启动流水线。
- [`tools/mkbootimg/`](tools/mkbootimg/)：AOSP打包工具，不附镜像或签名密钥。
- [`docs/status.md`](docs/status.md)、[`docs/building.md`](docs/building.md)、[`docs/safety.md`](docs/safety.md)：准确状态、外部依赖和安全规则。
- [`docs/source-provenance.md`](docs/source-provenance.md)、[`docs/not-included.md`](docs/not-included.md)、[`docs/validation.md`](docs/validation.md)：来源、未包含材料和离线验证范围。
- [`scripts/check-public-release.py`](scripts/check-public-release.py)：源码和Git对象公开检查。

## 使用前

**不能 clone 后直接编译出可刷 fnOS 或 boot 镜像。** 仓库已包含官方 c1147 构建头归档（`build-inputs/`）和 ADSP 的 5 个 PAS 源；解压到 Linux 大小写敏感目录并安装匹配工具链后可构建模块。Iris 公开源码已有完整构建验证，ADSP 使用完整归档已构建通过；ADSP 仍需公开 Linux v6.18.18 的15个上游源文件。完整系统/PID1/影视任务仍需要外部材料，详见 [`docs/building.md`](docs/building.md)。

没有一行刷机quickstart。官方c1147模块构建材料已提供，但完整系统仍需要合法固件、匹配源码/配置、根文件系统和启动封装；本库不提供完整可刷流水线。不要把历史7.x fnOS研究工具当当前官方内核构建方案。

Iris硬件执行门默认关闭，不隐式部署或改bootloader状态。部分显式安装工具会启用服务供后续开机使用；须逐项审核，源码存在不表示操作授权。运行隔离实验前必须先补齐并验证恢复入口，避免无远程重启通道的HOLD。

## 许可

原创代码与Linux派生源码采用 **GPL-2.0-only**，AOSP文件保留 **Apache-2.0**；原创文档采用 **CC-BY-4.0**。原文件更具体的第三方许可证优先。见 [`NOTICE.md`](NOTICE.md) 和 [`LICENSES/`](LICENSES/)。不授予fnOS/原厂固件等未包含材料的再分发权。
