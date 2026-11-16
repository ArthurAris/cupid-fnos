# FNOS ABI 工具（无盘启动基线 + 一次性 ACL 观测）

本目录包含两类独立工具：`oracle-init.c`/`build-oracle.py` 的**隔离 QEMU 无盘
只读启动基线**，以及文末的**一次性新建虚拟盘 Btrfs trimacl 行为观测**。两者都
不接触手机、不操作宿主块设备、不联网；ACL 工具只创建并格式化自己 workdir 内的
新 `data.img`。

## 边界

- 只运行 `qemu-system-aarch64 -M virt`（TCG），并使用 `-nic none`、
  `-nodefaults`、`-monitor none`、`-serial stdio`、`-display none`、
  `-no-reboot`，外层固定超时。
- 不共享宿主目录，不挂载磁盘/镜像，不做 USB 直通，不启用网络。
- 不启动 shell，不启动任何飞牛服务，不改外部盘 set 模式。
- `oracle-init.c` 唯一的写入动作是三处 initramfs 内挂载；它**不挂载 Btrfs**、
  不验证 `trimacl`；`FNOS_ORACLE_BOOT_OK` 只表示无盘启动基线，不是 ACL 验收。
- 文末 ACL 观测工具是独立边界：只在新建 workdir 的新虚拟盘上挂载
  `btrfs,trimacl`、写测试 xattr；不读写宿主块设备，也不接触手机。

## 文件

- `oracle-init.c`：静态 AArch64 最小 PID1 源码。
- `build-oracle.py`：只读输入、校验哈希、交叉编译、生成 newc cpio/gzip。
- `build-oracle.py` 只接受 `--kernel <gzip>` 与 `--work-parent <已存在目录>`，
  在 work-parent 下新建 `mkdtemp`，不会删除失败的输出目录。
- 构建脚本**不执行**编译出的 `init`，也**不启动 QEMU**，不自动安装 QEMU。
- 所需交叉编译器：`aarch64-linux-gnu-gcc`（含静态 libc）。缺失时脚本报错退出，
  不尝试安装。

## 构建

```sh
python -I tools/fnos/abi/build-oracle.py \
  --kernel <GZIP_PATH> \
  --work-parent <EXISTING_DIR>
```

- 输入 gzip 的 SHA256 固定核验为
  `985ba91057b0025e62386fa380fb4a5fb9a70874ba55fc6907bf770c9a104b40`。
- 解压出的 ARM64 Image 上限 64 MiB，SHA256 固定核验为
  `3e77a740a4f79712a7bd7ecb8c2748f90ef895a78212a41cb9e33836680f17fb`，
  并检查偏移 `0x38` 处为 `ARMd`。
- `-static -O2 -Wall -Wextra -Werror` 编译，编译 cwd 为新输出目录。
- 成功时 stdout 只有一行 JSON（至少含 `workdir`、`kernel_image`、`initramfs`、
  `manifest`）；gcc 输出走 stderr。失败返回非零并保留输出目录。
- `manifest.json` 记录输入、源码、init、Image、initramfs 的哈希及建议命令，
  不含序列号、MAC、蓝牙地址、校准数据等硬件信息。

## init 行为

1. `getpid() != 1` 立即拒绝，不重启。
2. 挂载 `proc`、`sysfs`、`devtmpfs`；`EBUSY` 视为已挂载。
3. 校验 `uname().release` 精确等于 `6.18.18.c1147-trim`。
4. 从 `/proc/cmdline` 中按空白分隔核验**独立 token**
   `cupid.oracle=boot-v1`（不是子串匹配）。
5. 挂载、版本、cmdline token 任一不满足，立即打印失败并关机，不继续特殊查询。
   身份通过后，打印 `/proc/filesystems` 中 `proc`、`sysfs`、`devtmpfs`、
   `tmpfs`、`btrfs` 相关行。
6. 只读调用 `syscall(SYS_faccessat2, AT_FDCWD, "/", F_OK, 0)` 作为 baseline；
   另做特殊 `dfd=-21582` 的 `is_trimacl` 查询，打印两者原始 `rc` 和 `errno`。
   特殊查询的返回值/错误**不预设**，也不参与成功判定。
7. baseline、版本、cmdline token 均正常时打印
   `FNOS_ORACLE_BOOT_OK`（并明确非 ACL 验收）；否则打印
   `FNOS_ORACLE_BOOT_FAIL`。
8. `sync()` 后 `reboot(RB_POWER_OFF)`；若该调用失败则作为 PID1 永久阻塞，
   由父进程超时收割，绝不退出 PID1。

## 运行限制

使用构建 manifest 中给出的隔离命令；外层必须带固定超时，例如
`timeout -k 5 60 qemu-system-aarch64 ...`。除 `-M virt` 外不要添加磁盘、
9p/virtiofs、`-device usb-*` 或网络设备。本工具不提供、也不验证
Btrfs 挂载、原生应用权限或 `trimacl` 语义。

## 一次性 Btrfs ACL 行为观测（独立新工具）

- `acl-init.c`：仅作 PID1 的观测程序。校验固定 release / `aarch64` / 独立
  cmdline token 后才 `finit_module` 固定 `virtio_blk.ko`；只接受
  `/dev/vda` 为块设备、大小 256 MiB、`/sys/block/vda/serial` 精确为
  `FNOS_ORACLE_TEST`，随后以 `btrfs,trimacl` 挂载到 `/vol1`。测试文件由
  root 以 mode `0600` 创建；每个文件设置 ACL 前后各观察两个测试 UID 的
  `O_RDONLY`/`O_WRONLY`（不 `O_TRUNC`、不实际写、不删除），baseline 的
  `EACCES` 与 set/get 长度或 hex 规范化差异都只作观测，不硬性判成功；只有
  setxattr/getxattr 真实系统调用错误才 `FAIL`。`VOLUME_ROOT` 和 `TEST_DIR`
  先各设 `EVERYONE ALLOW EXEC|READ_ATTR` + `SELF`，并由两个 UID 对目录本身
  调用 `faccessat2(AT_FDCWD, dir, X_OK, 0)` 观察最终目录搜索权限；
  `IS_TRIMACL` 按 `dfd=-21582`
  的普通路径字符串调用并原样记录 rc/errno，不预设语义、不作为通过条件。
- `build-acl-oracle.py`：**仅支持 Linux/WSL**；原生 Windows 在读取输入或创建文件前
  明确拒绝，不能通过去掉 `O_NOFOLLOW`/`O_CLOEXEC` 来绕过平台要求。
  只接受**普通文件**输入，固定校验内核 gzip、
  Image、stock initrd、initrd 内 `virtio_blk.ko` 的 SHA-256；流式解析 zstd
  newc，不整体落盘归档；在新 workdir 内交叉编译 `acl-init.c`，构造只含
  `init`、`proc/sys/dev/tmp/vol1`、`dev/console`、`modules/virtio_blk.ko`
  的 gzip newc initramfs。另在工作目录生成 `run-acl-oracle.sh`：仅当
  `qemu_rc==0`、日志含完整行
  `FNOS_ACL_ORACLE_OBSERVATIONS_COMPLETE NOT_COMPATIBILITY_ACCEPTANCE`
  （兼容 LF/CRLF），且不含 `FNOS_ACL_ORACLE_FAIL`、`Kernel panic`、`SETUP_FAIL`
  或 `SKIPPED` 时退出 0。未完整执行的扩展不能因最后有标记而算成功。
- `test_acl_builder.py`：测试纯 newc 读取/构造、截断/重复/非普通/硬链接
  成员拒绝、gzip/ELF 检查，以及 `create_data_image` 的 4096 字节临时文件
  大小、`sha256_fd`、重复路径和预置 symlink 拒绝；不构建、不格式化、
  不启动 VM。
- **只用于新建虚拟盘**：`data.img` 必须由构建脚本在新建 workdir 内以
  `O_CREAT|O_EXCL|O_NOFOLLOW` 创建普通文件、`ftruncate` 到 256 MiB，并
  保持该 fd，经 `/proc/self/fd/<fd>` 对新文件运行
  `mkfs.btrfs -q -L FNOS_ORACLE_TEST`，格式化前后用 fstat/lstat 校验
  inode/设备/类型/大小。严禁把 `/dev/sdX`、`/dev/nvmeXnY`、手机分区、loop
  设备、宿主已有镜像或任何块设备路径作为 `--kernel`、`--stock-initrd` 或
  磁盘输出；脚本不通过 loop/mount 格式化。
- 构建脚本**不启动 QEMU**，`acl-init.c` 不做损坏 fuzz。`acl-v2` 在原
  12 项权限观测保持原样之外，增加有界的目录继承、组身份与卸载/重挂
  一致性观测（范围与未覆盖项见文末 acl-v2 一节）。原始观测行
  与 `FNOS_ACL_ORACLE_OBSERVATIONS_COMPLETE NOT_COMPATIBILITY_ACCEPTANCE`
  只表示本轮案例收集结束，不是权限兼容性验收。

## acl-v2 观测范围（第二轮，有界）

原 12 项权限观测（baseline、case1、case2 顺序两组、root/test-dir 遍历）保持
原样；token/版本从 `acl-v1` 升为 `acl-v2`（磁盘测试目录名仍是
`acl-oracle-v1`，原 case 路径不变）。新增：

- 目录继承 3 例（`inherit-self-file-dir` / `inherit-self-file` /
  `inherit-self-dir`）：每例一个测试子目录（root 0700，`EVERYONE ALLOW
  EXEC|READ_ATTR SELF`），其内 parent root 0700，两条规则：独立
  `EVERYONE ALLOW EXEC|READ_ATTR SELF`（保证遍历）与被测
  `USER42001 ALLOW READ|EXEC|READ_ATTR`，inherit 标志分别为
  SELF|FILE|DIR、SELF|FILE、SELF|DIR。parent 内 root 创建 file 0600 与
  dir 0700，dir 内再建 file 0600；不给子节点补 ACL、创建后不 chmod。
- 子节点只读观测：原始 `getxattr system.trim_acl`（ENODATA 记为观测）与
  `faccessat2 dfd=-21576` 的 32 字节结构展开查询（filename/buffer/
  buffer_size/fd/flags，flags bit30=0 走 filename 分支；与 -21582
  plain-path is_trimacl 查询严格区分），打印 rc/errno/hex，不预设语义。
- 两个 UID 对 parent 与 child-dir 的 X_OK、对 child-file/grandchild-file
  的 O_RDONLY 真实结果，不做继承假设。
- 组 case `case3-group`：文件 root 0600，ACL 为 GROUP43001 ALLOW READ +
  USER42001 DENY READ；child 内 setgroups→setgid→setuid（无 root 保留）。
  42001+supp[43001]、42002+supp[43001]、42002 空 supp 各测 O_RDONLY 与
  O_WRONLY；既有 probe 增加 supp 参数，原 case 传空。
- 全部 case 完成后 sync/umount，同一已核验 vda 重新 `btrfs,trimacl` 挂载
  （不新建、不 setxattr），复测 case1/case2ab/case2ba 及一个继承子文件的
  两 UID O_RDONLY，最后再正常卸载；同一次 VM 启动内完成，无第二次启动。

未覆盖：NO_PROPAGATE/EXCLUDE、rename/delete、POSIX flags、`e_level`、
OWNER/AUTHENTICATEDUSER/SYSTEM tag、缓存失效、损坏回退、ext4 等其他文件
系统、真实多用户会话。`CASE ... SETUP_FAIL` 表示该 case 因未知权限结果失败
并停止扩展，不改 root 权限重试；EACCES/ENODATA 只是观测。基础设施错误仍打
`FNOS_ACL_ORACLE_FAIL`。`OBSERVATIONS_COMPLETE NOT_COMPATIBILITY_ACCEPTANCE`
仍只是观测收集完成，不是兼容性验收。
