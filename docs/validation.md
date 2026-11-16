# SPDX-License-Identifier: CC-BY-4.0

# 公开快照离线验证记录

本文件记录此次源码公开副本的离线复查，不是设备任务或物理DMA验收。

## 工具侧

416项unittest通过：

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

## 模块构建输入补充验证

新增完整c1147构建头归档已逐成员验证7220个文件、全部hash与大小写名称；补六个公开Linux v6.18.18辅助源码依赖头和实际适用许可全文。五个PAS源与固定清单一致。原Windows平铺头复制会覆盖8对大小写同名文件，已从未提交副本移除，只发布完整归档。

- 同份公开gate0Iris源码＋匹配c1147头完整GCC12构建通过：产物版本正确，RUN CRC`0x2fe0a796`；`.ko`不上传。
- 从此次完整归档解压的头＋仓库PAS源＋15个公开v6.18.18上游文件，ADSP十模块实际构建/校验通过：362条导入（331官方/31组内），CRC不符0、导出冲突0、符号失败0，layout此次未重验、未加载设备。
- Iris完整归档重验开始编译，但宿主盘耗尽/WSL只读回退使其未完成；失败日志保留，不能说成成功。原公开源的构建通过与此失败分别记录。
- 新解包工具5项host负例通过：错误hash、已有/相对输出、重复与路径穿越manifest拒绝；一次完整解包验证成功。
- ADSP构建准备6项与native acceptance80项回归通过；不将构建器修正提升成生产安全/完整ABI证明。

## 发布数据检查

逐文件检查UTF-8文本、禁止产物/目录、私钥/凭证模式及本机路径；另使用仅留私有的已知设备serial/文件系统身份清单进行精确交叉匹配，不在本仓库公开清单内容。

USB gadget中的两组 `02:00:00:00:42:xx` 地址是源码固定的本地管理测试常量，不是原厂MAC；使用者多机运行时须独立分配。该窄例外不允许上传任意真实MAC。`/home/linux`是构建目标中的通用用户目录，不是宿主私人路径。

公开扫描和独立复核完成后，提交前还需对实际暂存文件及所有Git blob重跑扫描；公开副本不继承本机历史。此检查降低披露风险，不保证覆盖所有可能的秘密或版权问题。

真实硬件进展及限制见 [状态](status.md)。
