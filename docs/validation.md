# SPDX-License-Identifier: CC-BY-4.0

# 公开快照离线验证记录

本文件记录此次源码公开副本的离线复查，不是设备任务或物理DMA验收。

## 工具侧

406项unittest通过：

- native-decode acceptance：80
- native-decode deploy：36
- native-job：35
- storage adapter：25
- GPU startup：15
- ADSP startup / deployment / build：76 / 61 / 6
- accelerator material / firmware / runtime / FDT gates：13 / 40 / 19 / 10

另完成GPU CPU oracle及load-sample C编译运行、AOSP两个CLI的`-I -B --help`、Shell语法与PowerShell parse-only检查。

部署测试使用新建ext4隔离fixture/fake-root，实际检查openat、原子replace、fsync、flock和失败恢复；synthetic固件/receipt/ELF不是实际设备材料。没有执行生产服务、loop-device runtime smoke、手机命令或完整系统构建。

## Iris源码路径

响应、wrapper和cold-power三个实际函数提取测试分别普通O2与ASan/UBSan执行，六次通过。各函数hash及生成C关联见 `tests/iris/test-results.json`。

测试中的IRQ、锁调度、部分framework/provider/raw状态和固件回复是明确host facade。公开执行门仍为0。客户端/回执解析器的坏envelope拒绝通过；未附带原始图像/oracle/FIR1/FNV1，因此此项不能声称完整帧重验。

## 发布数据检查

逐文件检查UTF-8文本、禁止产物/目录、私钥/凭证模式及本机路径；另使用仅留私有的已知设备serial/文件系统身份清单进行精确交叉匹配，不在本仓库公开清单内容。

USB gadget中的两组 `02:00:00:00:42:xx` 地址是源码固定的本地管理测试常量，不是原厂MAC；使用者多机运行时须独立分配。该窄例外不允许上传任意真实MAC。`/home/linux`是构建目标中的通用用户目录，不是宿主私人路径。

公开扫描和独立复核完成后，提交前还需对实际暂存文件及所有Git blob重跑扫描；公开副本不继承本机历史。此检查降低披露风险，不保证覆盖所有可能的秘密或版权问题。

真实硬件进展及限制见 [状态](status.md)。
