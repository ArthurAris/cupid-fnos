# fnOS 公开源码工具：适用范围与外部输入

这是 **fnOS 专用**的 source-only 研究发布，不是完整刷机包、固件包或可直接日用的安装器。只发布审核后的文本源码、测试与配置；没有镜像、DTB、模块二进制、缓存、真实日志、凭据或设备唯一数据。脚本被保留不表示已经运行或已证明硬件可用。

主线 Linux / Ubuntu / Phosh 工具不在本仓库，见已有的主线移植仓库。

## 工具范围

| 子树/工具 | 适用范围与边界 |
| --- | --- |
| `mkbootimg/` | AOSP 打包/解包 Python；保留原 Apache-2.0 copyright/license notices。GKI 签名需外部 avbtool/密钥，不提供密钥。`mkbootimg.py` 的可信同目录导入已适配 `-I -B`。 |
| `initramfs/init_static_fnos_v1.c` | 从 `userdata` 中挂载 `/host/fnos/root.img`、核对 fnOS 标记后交接 systemd 的历史 fnOS PID1 研究。**不是当前官方 c1147 完整启动流水线**，也没有随库构建脚本或可刷 initramfs。 |
| `fnos/storage/`、`fnos/gpu-monitor/`、`fnos/adsp/` | 固定官方 fnOS 6.18.18.c1147-trim 相关候选/检查及独立安装脚本。安装、启动、块/挂载操作需要独立审查与设备授权。旧存储候选构建属历史研究，不是当前官方内核构建入口。 |
| `fnos/accelerators/native/` | 固定源码/头文件 hash 与 Module.symvers CRC、HFI/布局/ABI 门；第三方 hash 不是秘密。必须提供匹配的外部官方头文件与 upstream 源；编译成功不等于可加载或安全 DMA。 |
| `fnos/accelerators/native-decode/` | 验收解析器、真实本地文件事务及严格独立 admission；缺生产 trust/producers/receipts/live 输入拒绝。`native-job.py` 缺已验证原始 header 来源仍 fail-closed，不提供登录/token。 |
| `fnos/quiesce-*` | 历史手动 source-edit/test pipeline，显式 external baseline/evidence 参数，固定 manifest/hash 门。非通用 builder，非自包含 pipeline，不作为完成硬件验收的证明。 |

## 必須由调用者独立提供的输入

- 许可明确的外部官方 fnOS headers、匹配源码、config、`Module.symvers` 和编译/检查工具，见 `../patches/kernel/README.md` 与各子目录说明。ADSP cross-build 的 QEMU helper 还要求显式 `SYSROOT`；LIO runtime smoke 要求 `CUPID_TEST_BASE`，它操作 host modules/configfs/loop devices，未在本次执行。
- 官方 fnOS 根文件系统（如 `root.img`）、启动封装、合法固件和板级材料。本库不提供完整可刷系统，也不含原厂固件/ROM/校准。
- 设备操作工具要求绝对 `-TargetFile`（私有 JSON，实际 `serial`/`adb`）和仓库外绝对 `-PrivateOutputRoot`。没有自动生成目标文件；明显 placeholder 被拒绝。所有生成日志、备份、实际校准/UUID/SSID/凭据只保存在外部私有目录。
- native-decode production admission、独立 producer/信任文件、当前 slot/电量/恢复 receipt 与实际固件 manifest 由外部协调者提供；本发布不生成可冒充真实证明的样例。

## 安全与操作边界

Iris 驱动执行门默认关闭，本仓库不提供隐式部署或 bootloader 修改。部分显式安装工具会 `systemctl enable` 服务供后续开机使用（不会立即 start）；必须逐项审核，源码存在不代表操作授权。

历史隔离实验缺少可从主机调用的远程恢复入口，失败后会 HOLD。运行任何隔离/硬件实验前必须另行实现并验证独立、指定设备绑定的恢复方案，不能依赖盲发串口命令或重置电脑端 USB。

## 许可与遗漏

原创脚本/C 工具新增 GPL-2.0-only 标识；原内核 GPL/双许可标识不改；AOSP Apache notices 保留，完整许可由仓库 license 文档覆盖。主线板级补丁、DTS、Ubuntu/Phosh 桌面与旧 7.x 串口/部署工具已从当前文件树移除。省略未明许可的提取数据、历史 `native-repeat-*` 私有 gate-token/自修改 pipeline、原 rootfs 基线 hash 操作记录及全部缓存。保留测试里的合成 MAC/全零 UUID/拒绝样例及协议 `token` 字段，不做全词删除；USB 本地管理测试地址不是原厂 MAC，但不适合多机部署，需独立分配。

## 可重复的离线测试

从可信 release 路径执行 Python `-I -B`；部署测试 fixture 必须放 WSL ext4（不是未启用 metadata 的 DrvFS），会在新子目录使用 synthetic ELF/固件/receipt/fake root，并实际练习 openat/原子 replace/fsync/flock/恢复。不能将 host fixture 结果解释为手机 native decode、真实固件认证、全长实机备份或物理 DMA 验收。

```sh
python3 -I -B "$RELEASE/tools/fnos/accelerators/native-decode/test_acceptance.py"
python3 -I -B "$RELEASE/tools/fnos/accelerators/native-decode/test_deploy.py" --host-test-base "$EXT4_FIXTURE"
python3 -I -B "$RELEASE/tools/fnos/accelerators/native-decode/test_native_job.py"
```

`RELEASE` 是可信源码发布绝对路径，`EXT4_FIXTURE` 是独立新建的私有测试目录；这三个命令不连接设备、不访问网络、不执行真实解码/服务。其他带 runtime/device 功能的工具不是此次测试范围。

本发布实际离线复查：native acceptance 80、deploy 36、native-job 35；storage adapter 25、GPU startup 15；ADSP startup/deployment/build 76/61/6；accelerator material/firmware/runtime/FDT gates 13/40/19/10，共 **416 unittest 通过**。另对 GPU CPU oracle、load-sample C 做实际编译运行，AOSP 两个 CLI `-I -B --help`、Shell 与 PowerShell 仅语法检查通过。没有完整 kernel/module build、loop-device runtime smoke、手机或 GPU 硬件验证；历史文档数字不是本发布重新完成的实机结果。
