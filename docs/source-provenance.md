# SPDX-License-Identifier: CC-BY-4.0

# fnOS 适配源码来源

## 官方内核目标

目标为fnOS官方 `6.18.18.c1147-trim`。Iris、视频时钟、ADSP及其他树外模块必须使用匹配的Kbuild、headers、config、Module.symvers和提供者；公开Linux 6.18.18只是框架参考，不是厂商c1147完整源码的替代。

上游参考：

- Linux stable：<https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/>
- Iris框架：<https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/drivers/media/platform/qcom/iris?h=v6.18.18>

“纯fnOS”指项目适配目标，不意味着删除fnOS需要的Linux派生GPL驱动。主线Linux/Ubuntu/Phosh的板级/桌面移植材料不在本仓库当前文件树。

## Iris快照

`verified`是原隔离实验源码的发布派生版本；`research`是最新生命周期和冷初始化修复后继。保留上游版权，关闭默认执行门。role manifest记录原文件与公开文件各自hash，省略原设备身份、nonce、信任资料与本机路径。

原基线包含REQBUFS锁修复、空DQEVENT语义、COMV固件兼容、软件控域、STOP内部返回与DPB提交身份关联；研究版补所有权代际、严格分派/取消守门和cold-init自锁修复。文件hash不是物理执行或完整上游身份认证，未确认的upstream commit保持unknown，不编造来源。

## fnOS历史启动和存储研究

`init_static_fnos_v1.c`是从userdata中的fnOS根镜像交接systemd的历史研究。它不是官方c1147完整启动依赖，也不意味着已发布可刷initramfs。

llbitmap补丁和部分7.x存储构建脚本保留为fnOS研究；对应目标源码、官方兼容性及真实系统验收需独立核对。不能拿它们覆盖后期官方内核的存储成果或当通用主线配置。

## 测试与许可

实际C提取测试使用明确host facade。编译器/CRC、sanitizer、协议解析与真实设备任务属于不同证据层。原始私人日志和完整准入材料未上传。

原创隔离PID1、client、验证器与脚本采用GPL-2.0-only；AOSP mkbootimg保留Apache-2.0；其他原文件许可证优先。外部固件/用户态/镜像的许可不由本仓库授予，见[未包含材料](not-included.md)。
