# SPDX-License-Identifier: CC-BY-4.0

# 来源与许可证说明

## 适用许可证

本项目原创代码（脚本、验证器、隔离监督器与适配代码）在此发布副本中采用 **GPL-2.0-only**。原创README与说明文档采用 **CC-BY-4.0**。文件已有更具体的第三方版权或许可证时，原文件声明优先；不得用本仓库许可证覆盖第三方限制。

- `sources/iris/`：Linux Qualcomm Iris 驱动派生，保留 Qualcomm Innovation Center、Linaro及相关上游版权/SPDX，按GPL-2.0-only分发。
- `patches/kernel/`：仅保留fnOS存储修复研究，遵循原Linux GPL条款；目标源码与适用性需另行核验，不作为已验收官方补丁。
- `tools/mkbootimg/`：Android Open Source Project打包代码，保留Apache-2.0原始声明。
- 某些代码从内核头或算法派生，保留原SPDX和Linux syscall note等适用例外。用户空间独立脚本不意味着可改变内核来源条款。

许可证文本见 `LICENSES/`；GNU/Creative Commons/Apache的名称不表示其对本项目背书。

## 源码版本与原实验

这是经过脱敏、关闭默认硬件执行入口的新源码快照，不继承本机Git历史。原实验文件的逐项hash与公开派生文件的hash在各source manifest中分开记录；许可证注释、路径和执行门变化都会产生新的hash。

原隔离实验通过只限固定H264三帧/双STOP提交归还观察。最新研究后继只完成指定离线构建/测试，尚未实机验收。公开源码副本不能借用原镜像的hash声称自身已被刷写或通过物理验证。

完整源树来自Linux Iris框架、针对SM8450回移的VPU2/gen2平台数据及本项目修复。原完整官方c1147源码/头、精确工具链和本机原始证据不随库再分发；来源详见 `docs/source-provenance.md`。无法核实的上游commit不会编造。

## 不包含的第三方产品

fnOS用户态、原厂固件/ROM/校准、设备镜像与备份均未包含。这里的GPL/CC许可不授予它们的许可权。名称Xiaomi、Qualcomm和fnOS仅用于描述兼容目标。

来源/再分发权不明确的数据不发布；主线板级与Ubuntu桌面资料已从本仓库当前文件树分离。范围修正采用正常新增提交，未强推重写最初公开提交。

## 文档归属

原创脱敏文档归属 Cupid Linux contributors，采用CC-BY-4.0。其他文件内保留的上游作者和版权归其原作者。引用公开源码及许可链接不替代完整许可证要求。
