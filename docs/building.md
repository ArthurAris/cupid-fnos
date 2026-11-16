# SPDX-License-Identifier: CC-BY-4.0

# 构建与离线验证

## 能否编译使用

**不能clone后直接生成可刷fnOS/boot镜像。** 仓库提供模块源码、官方c1147构建头归档和ADSP五个PAS源；不含固件、fnOS用户态、root.img、原厂DTB容器、签名密钥或设备准入材料。

可以运行Python离线测试与Iris实际C路径测试；可以在安装合适工具链后构建Iris模块。ADSP还需要公开Linux v6.18.18的15个源文件。模块编译通过不等于可生产加载或原生影视验收。

## 1. 解压官方构建头

必须使用Linux/ext4大小写敏感目录，不能解压到默认Windows文件夹。完整头包存在8对大小写不同的头文件；归档已保留全部文件名，配套清单逐项hash校验。

```bash
python3 -I -B tools/fnos/unpack-build-headers.py \
  --archive build-inputs/linux-headers-6.18.18.c1147-trim-complete.tar.xz \
  --manifest build-inputs/linux-headers-6.18.18.c1147-trim-complete.manifest.json \
  --sha256 3a19038f84738af3505f51571cfeaa1090c1c947e2abd90df48d5e09023e4b08 \
  --output /var/tmp/cupid-c1147-headers
```

归档约9.4MB、解压约54.7MB、7220文件，含`Module.symvers`、生成配置及Kbuild材料。7个AArch64构建辅助工具附带对应源码/依赖头和原许可证；不是驱动`.ko`。详见[构建输入](../build-inputs/README.md)。

## 2. 工具链

- Linux/WSL、Python3、GNU make、file、binutils，以及ARM64 GCC12.2交叉编译器（例如Debian12的`gcc-12-aarch64-linux-gnu`）。
- x86主机需要`qemu-user-static`与合法AArch64 sysroot，包含glibc动态加载器及助手所需运行库。不能只设置一个空目录；`SYSROOT`须是使用者安装的真实sysroot。
- `CC`、`HOSTCC`和交叉前缀可以显式指定；默认并行度2（`JOBS=1..8`），控制宿主磁盘/内存占用。
- 本项目验证使用GCC12.2.0 Debian `12.2.0-14+deb12u1`；原内核为`12.2.0-14`。保留修订号差异/BTF缺vmlinux和完整ABI未知，不将版本号接近称等价证明。

原生ARM64可直接运行头包助手；ADSP脚本仍要求有效SYSROOT参数。交叉编译器可由宿主原生工具链提供，或使用匹配sysroot内的ARM64编译器并配置qemu与`-B`工具路径。

## 3. Iris模块

在仓库根目录运行：

```bash
SYSROOT=/path/to/aarch64/sysroot \
CC=aarch64-linux-gnu-gcc-12 HOSTCC=gcc JOBS=2 \
  bash tools/fnos/build-iris-module.sh \
  /var/tmp/cupid-c1147-headers /var/tmp/iris-build
```

输出目录必须不存在。脚本校验`Module.symvers`固定hash和公开硬件执行门为0，逐文件核对复制的headers后构建，检查`iris_decode_once_run`CRC`0x2fe0a796`，输出模块及构建记录。不会加载模块/改手机。

**验证状态**：同份公开gate0Iris源码已用c1147头完整GCC12构建，版本`6.18.18.c1147-trim SMP mod_unload modversions aarch64`正确。归档版保留了原头全部内容，但此次Iris归档重验因宿主空间耗尽、WSL只读回退未完成；不将失败算成功。运行前仍须匹配ABI、依赖、认证固件、所有权/恢复合同和当前备份。

## 4. ADSP十模块

从公开Linux stable v6.18.18取得源码（不能拿任意6.18标签替代），构建器逐项核对15个文件hash。PAS五文件已包含，不必下载完整7.2.8树。

```bash
SYSROOT=/path/to/aarch64/sysroot \
CC=aarch64-linux-gnu-gcc-12 HOSTCC=gcc JOBS=2 \
  bash tools/fnos/adsp/build-adsp-modules.sh \
  /var/tmp/cupid-c1147-headers /path/to/linux-6.18.18 \
  build-inputs/adsp-7.2.8-pas /var/tmp/adsp-build
```

脚本使用固定新输出目录，构建后检查全部10模块身份、版本化导入/导出和冲突。完整归档解压后的材料已实际构建通过：362导入（331官方、31组内）、CRC不符0、导出冲突0、符号失败0；`layout_verified=false`，没有加载设备。

## 仍需外部材料

- 存储候选/LIO内核：完整匹配内核树和配置；不能凭几个头构建完整内核。
- llbitmap补丁目标源码需核验；此次未选择上传。
- 隔离PID1缺实际模块/fixture生成的绑定头；不随库提供设备nonce/准入数据。
- 完整启动镜像缺BusyBox、固件、fnOS用户态/root.img、DTB封装和恢复材料。
- 自带FFmpeg/影视真实任务、PERSIST安全回收和重复长负载仍未验收。

## 离线测试与公开检查

```bash
python3 -I -B tools/fnos/accelerators/native-decode/test_acceptance.py
python3 -I -B tools/fnos/accelerators/native-decode/test_deploy.py --host-test-base /var/tmp
python3 -I -B tools/fnos/accelerators/native-decode/test_native_job.py
python3 -I -B scripts/check-public-release.py --root .
python3 -I -B scripts/check-source-manifests.py --root .
```

部署测试fixture只放支持Linux权限的隔离ext4目录。Iris提取测试见[测试说明](../tests/iris/README.md)，host facade不是真实IRQ/firmwareDMA证明。公开检查会解压扫描归档每个成员及Git中对应blob；不能仅因压缩文件名相符放行未知二进制。

完整fnOS/原生影视状态见[状态](status.md)，设备运行前安全条件见[安全](safety.md)。
