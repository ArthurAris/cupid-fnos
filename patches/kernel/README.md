# 内核公开源码改动：基线、外部输入与遗漏

本目录是源码改动与历史配置，不是完整内核树或可直接编译/刷写的固件。未包含 DTB、Image、boot.img、固件、模块、设备校准和私有证据。

## 不可混用的两条基线

### 主线 Cupid 板级 patch

`0001-cupid-kernel-changes.patch` 以以下精确 commit 为基线；`base.txt` 保留历史 commit 元数据，不代表此发布当天的新上游状态。

- base: `ed120558cd2c5f970edc44e1273a8d7b6caed825`
- base subject: `ASoC: qcom: q6apm-voice: drop and free PCM on backend close`
- historical local head: `5a1ff1c2d6f69dd7c92497f1655645dd383657d3`
- local head subject: `arm64: dts: qcom: add initial Xiaomi 12 (cupid) board support`

local head 并非上游 commit；patch 含该本地板级改动及历史工作树 diff。`new-files/` 必须按相同相对路径放入**外部可信基线树**。保留早期调试 C、`cupid_dtb.S`、板级 DTS/DTSI、panel/touch driver 源与原 GPL/BSD/双许可标识；寄存器地址不是个人秘密。

### 官方 fnOS 候选

`0098-md-llbitmap-preserve-header.patch` 与 `config-fnos` 属于官方 **6.18.18.c1147-trim** 相关存储候选，不是上述主线 patch 的相同基线。它们不会使 llbitmap、trimacl、设备原生视频或模块 ABI 自动获得验收。官方 headers、Module.symvers、实际 upstream 输入/固定 hash 门与额外 Iris patches 见 `../../tools/fnos/storage/`、`../../tools/fnos/accelerators/native/sources.json` 和对应 README。

## 明确省略的必要输入

- `new-files/drivers/gpu/drm/panel/panel-xiaomi-cupid-blobs.h`
- `new-files/arch/arm64/kernel/cupid_font.h`

这两者未能在本次定向审查中确认可再分发的上游许可，直接 omit；没有空 header、假 blob、代用字体或“可编译”承诺。公开的 `panel-xiaomi-cupid.c` 和 `cupid_early.c` 仍引用它们，故套件**有意不自包含且不直接可编译**。调用者必须在外部树中提供许可明确、技术兼容的独立输入，先审查来源和内容，再考虑构建。`build-final.sh` 缺输入时明确拒绝。

stock decoy DTB、厂商固件/传感器校准、完整 kernel/headers、外部工具链、静态 BusyBox、根文件系统、生产 admission/backups 全不随本发布提供。历史私有改写/构建记录不是公开自动化 pipeline，不要把本目录与任意厂商/主线内核混合应用。

## 配置使用条件

- `config-v78` 是主线历史 v78 配置；`config-fnos` 是独立官方候选配置。
- 两者 `CONFIG_INITRAMFS_SOURCE=""` 已清空原本机绝对目录。必须在外部可信构建树中用 `scripts/config --set-str INITRAMFS_SOURCE "$INITRAMFS_DIR"` 显式传入已准备的 initramfs，不能把空值视为可启动配置。
- 主线历史显示 DTS 是 `sm8450-xiaomi-cupid-v-touch`，内建 `CUPID_DTB` 必须相符；`CONFIG_CPU_IDLE` 的历史黑屏限制不放宽。
- `tools/build-final.sh` 要求 `KERNEL_TREE`、`INITRAMFS_DIR`、`DECOY_DTB`；本发布未执行完整 kernel/module build，也没有设备 boot/flash。

仅可对已取得合法完整基线与所有外部输入的树做 `git apply --check` 和源码审查。缺任何外部输入时停止，不给出半套 quickstart、不生成 dummy 固件，不以 host C/解析/事务测试冒充面板、WiFi、ADSP、native decode 或 DMA 实机结果。
