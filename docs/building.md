# SPDX-License-Identifier: CC-BY-4.0

# 构建与离线验证

## 先了解限制

仓库只提供fnOS适配研究源码，不附带完整官方内核、固件、fnOS用户态、root.img、原厂DTB容器、准入/信任配置或可刷镜像。目标为官方c1147；历史fnOS PID1与7.x存储工具仅供研究，不是当前完整官方启动流水线。

对设备运行前，必须重新完成匹配内核ABI、依赖、合法固件、所有权寿命、恢复入口与当前备份检查。公开驱动入口默认关闭，本文不指导直接启用或刷写。

## 环境与输入

- Linux/WSL与Python3；建议用`python3 -I -B`执行读取外部材料的脚本。
- 主机C编译器、ASan/UBSan支持用于源码路径测试。
- ARM64交叉编译器；历史Iris目标使用GCC12/c1147头，静态client/PID1使用GCC15。不同工具链构建需自己的hash与ABI核验。
- c1147的匹配Kbuild、`.config`、generated headers、`Module.symvers`及全部提供者。没有这些材料时测试或构建应明确失败/报告缺失，不能降低验证门。
- 固件由使用者自行合法获取，不下载未知附件、不给安全加载器放宽认证。

## 当前可独立运行的host测试

在仓库目录执行明确脚本路径，不从不可信源码/下载材料目录启动解释器：

```bash
python3 -I -B tools/fnos/accelerators/native-decode/test_acceptance.py
python3 -I -B tools/fnos/accelerators/native-decode/test_deploy.py --host-test-base /var/tmp
python3 -I -B tools/fnos/accelerators/native-decode/test_native_job.py
```

部署测试只能在支持Linux权限的隔离ext4临时目录执行，不使用真实系统根、设备分区或生产服务。它验证文件事务，不验证驱动安全或设备boot备份。

Iris实际C函数提取测试使用方法见 `tests/iris/README.md`。它们的facade模拟部分内核/provider合同，不是完整VB2、真实IRQ并发或固件DMA证明。

其他 `tools/` 子目录保留历史实验脚本，参考各README和输入schema。有缺失源/外部spec/上游树的测试不应作为“全通过”统计。并未为发布重新完整构建整个操作系统。

## 发布安全检查

```bash
python3 -I -B scripts/check-public-release.py --root .
```

初始化独立Git历史后还要扫描实际暂存文件和Git对象；`.gitignore`不能删除已有历史敏感数据。全部文件白名单、第三方许可、当前状态、文档链接和依赖都须核对。

不执行手机命令的测试通过后，只能说明对应离线范围成立。真实硬件原生验收条件见 [状态](status.md) 与 [安全规则](safety.md)。
