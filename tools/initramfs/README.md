# fnOS PID1 研究（历史路线）

本目录只保留 `init_static_fnos_v1.c`：从 `userdata` 分区挂载 `/host/fnos/root.img`，校验 fnOS 标记后交接 systemd 的历史研究实现。

**它不是当前官方 c1147 完整启动流水线**，也没有随库提供构建脚本、静态 BusyBox 或可刷 initramfs。

## 行为范围

- 根发现：GPT `PARTNAME=userdata` + ext4 + `/etc/cupid-fnos-release` 标记；不是任意分区猜测。
- 挂载 `/proc`、`/sys`、`/dev`、`/run` 后 `switch_root` 到 fnOS 根镜像。
- 注册 USB 复合 gadget：ACM 串口 + RNDIS（`1d6b:0104`），不设默认路由。
- 开发期串口 root 无密码；发布/正式使用前必须关闭或设密码。

## 已知限制

- 失败路径进入不退出/保留现场，**没有已实现的主机侧远程 reboot 入口**；SysRq 关闭、无用户态看门狗。恢复需要外部现场操作，不能靠盲发串口命令。
- 这是历史 7.x 阶段实现，不代表官方 6.18.18.c1147-trim 当前的启动封装或 systemd 交接验收。
- 文件源码仅用于研究与安全审查；不提供 `CONFIG_INITRAMFS_SOURCE` 打包步骤或现成镜像。

## 修改注意

- PID1 绝不能正常返回；所有错误路径保留或重启。
- 不依赖 PATH；命令用绝对路径或直接 syscall。
- 任何新的恢复入口必须独立、指定设备绑定并有界测试，不能把普通串口 shell 或复位本身当所有权归还证明。
