# ADSP 7.2.8 PAS 源（5 文件）

从与官方 c1147 PAS 头匹配的本地 7.2.8 树中提取的 GPL 源，供 `tools/fnos/adsp/build-adsp-modules.sh` 的第三个参数使用。路径保持上游相对结构：

- `drivers/remoteproc/qcom_common.c` / `.h`
- `drivers/remoteproc/qcom_q6v5.c` / `.h`
- `drivers/remoteproc/qcom_q6v5_pas.c`

每个文件保留原 SPDX/版权（GPL-2.0-only）。哈希与 `tools/fnos/adsp/source-inputs.json` 的 `pas` 段一致；构建脚本在复制前会逐一校验（CRLF 规范化为 LF）。

仍需使用者提供：

- 官方c1147构建头归档：`build-inputs/linux-headers-6.18.18.c1147-trim-complete.tar.xz`，先按`build-inputs/README.md`解压到Linux大小写敏感目录，再把该目录作为构建第一参数。
- 上游 Linux v6.18.18 源树（公开 tarball），其 15 个固定文件哈希同样在 `source-inputs.json` 的 `upstream` 段
- 用于 qemu 运行 aarch64 构建辅助工具的 sysroot（`SYSROOT` 环境变量）；在原生 arm64 主机上可直接构建

这不是可刷镜像；构建产物默认仍受公开 gate 与设备安全条件约束。
