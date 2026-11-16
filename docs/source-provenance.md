# SPDX-License-Identifier: CC-BY-4.0

# 源码来源与发布派生

## 两条内核路线

1. 主线Linux/Ubuntu/Phosh：历史板级patch基线见 `patches/kernel/base.txt` 和对应README。这些改动不是fnOS官方内核ABI的替代。
2. fnOS：目标为官方 `6.18.18.c1147-trim`。Iris外置模块使用匹配Kbuild/headers/config/Module.symvers构建；原版Linux 6.18.18只是框架参考，不等于厂商c1147完整源码。

上游参考：

- Linux stable：<https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/>
- Iris框架参考：<https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/drivers/media/platform/qcom/iris?h=v6.18.18>
- 公共小米板级参考：<https://github.com/MiCode/Xiaomi_Kernel_OpenSource>
- Novatek DSC参考版本：MiCode commit `7d10d738b2c4299f3b8549c61a01ece39f19c788`，相关模型通用参数记录见本项目状态说明；不包含原厂ROM或本机校准。

## Iris快照

`verified`表示原隔离实验对应源码的派生版本；`research`表示最新生命周期/冷初始化修复后继。都保留Linux源版权，并在公开副本中关闭默认执行门。每阶段source manifest列出原源文件hash与公开文件hash，原始私有绝对目录、设备身份、nonce或准入资料不会作为路径保留。

最近的原源码基线包含REQBUFS锁修复、空DQEVENT语义、COMV固件兼容、软件控域、STOP内部返回与DPB提交身份关联。研究后继补齐所有权代际、取消入口/畸形回包守门及冷启动自锁修复。具体变更与局部验证见该目录README。

原57文件快照包含本地回移及修复，不是对一个公开upstream commit的无修改copy。未能精确确认的完整来源commit保留为unknown；仓库不编造上游身份或完整可复现承诺。文件hash可用于比较，不能独立证明许可证来源、构建行为或物理设备认证。

## 测试与工具

源码提取C函数测试使用明确host facade；编译器/CRC、sanitizer、协议解析和实际手机任务是不同证据层。发布副本的新测试结果在公开的离线说明中记为host-only。私人原始日志与真实设备准入材料不会上传。

本项目原创隔离PID1、client、验证器与脚本采用GPL-2.0-only；AOSP mkbootimg与其他上游文件保留原许可证。发布省略的材料与许可限制见 [未包含材料](not-included.md)。
