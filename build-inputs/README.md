# SPDX-License-Identifier: CC-BY-4.0

# fnOS 模块构建输入

本目录提供官方c1147构建头和ADSP五个PAS源。**没有固件、内核镜像、驱动`.ko`、fnOS用户态、root.img、分区备份或设备数据。**

## 官方构建头归档

`linux-headers-6.18.18.c1147-trim-complete.tar.xz`：9,355,492字节（约9.4MB），解压内容54,728,962字节，7220个文件。SHA256：

```text
3a19038f84738af3505f51571cfeaa1090c1c947e2abd90df48d5e09023e4b08
```

必须解压到Linux/WSL的**大小写敏感**文件系统，不能直接解压到默认Windows目录。内核UAPI包含8对大小写不同的同名头，Windows会覆盖。配套`.manifest.json`记录每个文件hash/长度与来源；解压脚本会验证全部字节并拒绝路径穿越、链接/特殊文件和已有输出目录。

在仓库根目录执行：

```bash
python3 -I -B tools/fnos/unpack-build-headers.py \
  --archive build-inputs/linux-headers-6.18.18.c1147-trim-complete.tar.xz \
  --manifest build-inputs/linux-headers-6.18.18.c1147-trim-complete.manifest.json \
  --sha256 3a19038f84738af3505f51571cfeaa1090c1c947e2abd90df48d5e09023e4b08 \
  --output /var/tmp/cupid-c1147-headers
```

- 原官方头包的全部7194普通文件和7个内部链接的实际内容均保留，大小写文件名不变；内部链接转为普通文件。
- `Module.symvers`有25,562条符号，SHA256为`121ac1b2269aa229ec044ff7bc3211ee3a7eea74829f89979603240299ab31a6`；生成配置在`include/config/auto.conf`和`include/generated/autoconf.h`。
- 仅省略失效的构建机Jenkins绝对链接`kernel-package-postprocess.sh`，模块构建不需要。
- 7个AArch64 ELF仅是官方构建辅助工具（fixdep/modpost等），不是驱动模块；对应主源码包含在头包中，另补6个Linux v6.18.18公开依赖头以完整提供sorttable/mdp源码。
- 原版权/SPDX保持，附GPL/系统调用例外、MIT、BSD、0BSD、LGPL及Bison相关全文；OR许可选择GPL分支，AND声明保留双方条件。原厂商构建身份不是本机身份，未将本机凭证写入。

## ADSP PAS源

`adsp-7.2.8-pas/`保留上游相对路径下的`qcom_common.c/.h`、`qcom_q6v5.c/.h`和`qcom_q6v5_pas.c`，共5个GPL源。哈希与`tools/fnos/adsp/source-inputs.json`的`pas`段一致。

ADSP仍需要公开Linux stable v6.18.18的15个上游文件（同JSON的`upstream`段）；它们不是设备固件，不需要私有数据。原生ARM64或x86交叉编译环境/工具链仍由使用者安装。

## 构建验证

- **Iris**：公开gate0源码与匹配c1147头已完整GCC12构建成功，vermagic正确、RUN CRC为`0x2fe0a796`。产物只在本地用于验证，不上传。完整归档版Iris重验开始编译，但因系统盘耗尽/WSL只读回退未完成；不能将其计为成功。
- **ADSP**：从此完整归档解压后，配合上述15个公开上游文件和仓库PAS源，实际十模块构建/校验通过，362条导入、CRC不符0、导出冲突0；layout仍未在此次构建重验，未加载手机。
- GCC12包修订与原内核存在差异，缺vmlinux而跳过BTF；CRC匹配不是完整ABI或生产运行证明。

构建指令、工具链依赖与限制见[构建说明](../docs/building.md)。这不是完整可刷系统，不能因为模块编译通过就启用实验门或直接部署。
