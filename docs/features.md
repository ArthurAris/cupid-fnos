# SPDX-License-Identifier: CC-BY-4.0

# fnOS 功能适配明细

本页按功能列出 **小米12（cupid / SM8450）运行官方 fnOS 内核 `6.18.18.c1147-trim`** 时实际完成、实测验证和仍未完成的部分。它是脱敏摘要，不是原始日志或认证物理证明。

状态用语：

- **实机验证**：在指定手机上实际运行并观察到结果；另有明确限制。
- **离线验证**：源码/构建/host 测试通过，未在手机运行。
- **未适配/未验证**：没有可宣称的成果。

## 总览

| 功能 | 适配内容 | 实测结果 | 状态与限制 |
|---|---|---|---|
| 系统启动 | 官方 c1147 内核 + fnOS ARM 用户态 + 官方 systemd | 正常启动、两次正常整机重启恢复 | 实机验证；更新兼容性未验收 |
| 根与数据卷 | 根为 `userdata` 内的 `root.img` loop ext4；数据卷 Btrfs | systemd、`/vol2`、Docker、SMB 正常 | 实机验证；完整停机顺序仍有问题 |
| WebUI | fnOS 原生 5666 Web 入口 | HTTP 200、原生登录页、浏览器可见 | 实机验证 |
| WiFi | WCN6855 / ath11k，PCIe0 与 PMU 节点启用 | 自动连接、DHCP、HTTPS/WebUI、重启恢复 | 实机验证；小时级稳定性有未解释故障 |
| GPU 计算 | 官方内核 headless GPU 集成 + Turnip/freedreno | renderD128、普通用户提交/fence/4 MiB 回读 | 实机验证；功耗/长期稳定性有限 |
| GPU 监控 | kretprobe 兼容模块，向原生 hwinfo/resmon 提供利用率/温度 | 网页动态利用率与真实温度、负载回归 | 实机验证；显存统计未实现 |
| 温度 | 启用 TSENS 热区 | 38 个 thermal zone | 实机验证 |
| ADSP | 10 个树外模块，PAS/GLINK/PDR/PDM 链 | ADSP running、IPCRTR 通信 | 实机验证；异常恢复有限 |
| 电池/UPS | PDM mapper 名称修复 + battmgr 出数 | UPower 与 UPS 页显示真实容量/充电状态 | 实机验证；保护阈值/低电量关机未验收 |
| 存储适配器 | LIO/IBLOCK 导出一块数据分区 | WSL 一次性镜像真实验证；手机曾导出 | 实验性；`trimacl` ABI 未解决 |
| Iris 视频解码 | 隔离单会话 H264→NV12，STOP/所有权研究 | 固定三帧 + 双 STOP + 提交归还 + PERSIST 关闭退休 | level 4 条件验收；PERSIST 修复两次真机复现；level 5 未完成 |
| 显示（fnOS） | 未适配官方内核显示控制器/面板 | 无 | 未适配 |
| 音频/蓝牙/GPS/摄像头 | 未适配 | 无 | 未适配 |
| NPU/FastRPC | CDSP 固件与模块材料离线准备 | 未在 fnOS 实际调用 | 未适配 |
| 视频编码/HEVC | 未验收 | 无 | 未验收 |

## 1. 系统与根文件系统

- 使用官方 `6.18.18.c1147-trim` 内核与 fnOS ARM 版用户态；systemd 正常接管。
- 根文件系统位于 `userdata` 分区的 `fnOS root.img`，通过 loop 设备挂载为 ext4；`/vol2` 为 Btrfs 数据卷。
- 数据卷上有 Docker（overlay2 存储）与 SMB 服务；WebUI、应用中心相关服务正常运行。
- 实测完成两次正常整机重启：重启后根、`/vol2`、render 节点、热区与服务自动恢复。
- **限制**：fnOS 自动更新机制没有针对手机启动分区的同步实现，不能直接跟随官方内核/系统升级；系统升级兼容性未验收。完整存储停止/卸载存在额外引用导致的 EBUSY，正常安全停机链仍未完全闭合。

## 2. 网络

### WiFi（WCN6855 / ath11k）

- 原诊断基线错误地禁用了 WiFi 所在的 PCIe0 控制器、其子桥与 `wcn6855-pmu`；生产候选仅把这三个节点改为 `okay`，保持内核与其余设备树不变。
- 依赖官方已有的 `pwrseq-core`、`pwrseq-qcom-wcn`、`pci-pwrctrl-pwrseq`、`ath11k_pci` 模块，vermagic 与官方内核匹配。
- 实测：自动连接、DHCP 获取地址、默认路由以 WiFi 为主、USB 网络为后备；DNS、HTTPS、WebUI 均经无线地址访问成功；两次正常重启后自动恢复。
- **限制**：动态地址，无静态租约；曾有运行数小时后 WiFi 与 USB 串口同时不可达、重启恢复的报告。缺少故障时的持久日志，不能区分整机/驱动卡死与网络故障，**小时级稳定性未验收**。

### USB 复合网络

- 开发期使用 RNDIS + ACM 复合 gadget（VID:PID `1d6b:0104`），`usb0` 固定地址、**不设默认路由**；串口为调试通道。
- 该链路用于隔离实验的主机通信与诊断；生产系统不依赖它提供上网。
- **限制**：串口 root 无密码仅限开发；发布/正式使用必须关闭或设密码。串口不是通用 shell，隔离失败镜像缺少远程重启入口。

## 3. GPU（Adreno 730）

### 计算

- 官方内核默认 adreno 在显示节点关闭时只注册 component，不创建 DRM render 节点；启用 `separate_gpu_kms=1` 后实际创建 `renderD128`。
- 生产集成启用 GPU/GMU/GPUCC/SMMU 节点并加载 14 个 GPU 模块与 6 个私有固件；GMU 固件 v4.0.7 加载。
- 实测：root 与零能力普通用户均通过 Turnip Adreno 730（vendor `0x5143`）提交；4 KiB `vkCmdFillBuffer`、3 秒有界 fence、1024 字回读一致；不在 render 组的用户被 EACCES 拒绝，权限边界有效。
- 负载回归：6 秒 2213 次队列提交、fence 全部成功、4 MiB（1,048,576 个 32 位字）逐字回读一致。
- **限制**：GPU 空闲自动挂起曾导致 USB 在 28–52 秒内消失；把 GPU `power/control` 置 `on` 后完成 180 秒与约 15 分钟观测。该对比支持临时缓解，**不是最终根因或永久配置**；系统挂起、功耗与小时级稳定性未验收。

### 监控与温度

- 兼容模块用 kretprobe 观察官方 `msm_devfreq_get_dev_status` 的**已完成成功采样**，只复制 busy/total 与时间戳；不读写 GPU 寄存器、不额外调频或唤醒。
- 提供 `gpu_busy_percentage`（0–100，过期/错误/零分母/异常返回 `EAGAIN`，不伪装 0%）与 `sample_status`（原始计数、age、generation、missed）。
- 实测：fnOS 原生 hwinfo/resmon 枚举 GPU，网页显示动态利用率与真实温度；6 秒负载下利用率 0%→94–95%→0%，温度 50–51°C→58–61°C→51°C。
- 两个兼容模块曾以启动服务方式持久安装；用户确认重启后自动加载（**该持久化重启未由本轮独立复测**）。
- **限制**：此版本 Adreno 后端的显存统计字段恒为 0，表示“未实现统计”，不是显存容量/用量为零。启动器瞬态温度读取的有界重试仍是待改进项，未在本次范围修改。

### 热区

- 启用 TSENS 对应节点后出现 38 个 thermal zone，覆盖 CPU/GPU/视频/DDR/CDSP 等；GPU 温度来自真实热区，不是固定值。

## 4. ADSP 与电池 / UPS

### ADSP 启动链

- 官方内核缺少完整 q6v5/smem/smp2p/glink_smem 链；树外构建 10 个模块（hwspinlock core/qcom、smem、smp2p、glink_smem、pil_info、rproc_common、q6v5_pas 等），基础设施取上游 6.18.18、PAS 相关取与官方头一致的 7.2.8 源，另加最小补丁。
- 实测 ADSP `remoteproc` 进入 running，pmic_glink 与 ADSP 双向通信，IPCRTR 绑定。
- 启动脚本按 boot_id 去重、有界等待、失败保留现场；锁不可得时不做模块改动。

### 电池读数根因与修复

- fnOS UPS 页面链路为 `trim_sac → UPower → /sys/class/power_supply/qcom-battmgr-bat`。
- 早期所有属性 `EAGAIN` 的根因是 `qcom_battmgr.service_up == false`：PDR/servreg 通知未到达。
- 进一步确认树外构建把 PDM 辅助设备名变成 `cupid_qcom_rproc_common.pd-mapper`，而官方 mapper 只接受标准 `qcom_common.pd-mapper`；修复只在该注册调用传标准名称，不整体改模块名、不改官方 mapper。
- 实测：ADSP/PDR/battmgr 全链出数，UPower 与 fnOS UPS 页面显示真实容量与 Charging 状态（验证时为 21% Charging）。
- **限制**：UPS 保护阈值仍为 0，低电量保护/关机策略未恢复验收；曾有一次有序关机读取失败后按 critical 假定处理，另一次复位无 panic 栈，不能定因。电池温度单位、DMI 识别等细节仍有历史问题记录。

## 5. 存储

### 当前生产形态

- 根在 `userdata` 的 `root.img`（loop ext4）；数据卷 `/vol2` 为 Btrfs（md/LVM 之上），Docker 与 SMB 正常。
- 该形态通过正常重启恢复验证。

### LIO/IBLOCK 适配器（实验性）

- 目的：把一块已核验的数据分区通过 Linux LIO/IBLOCK + `tcm_loop` 呈现为本地 SCSI 盘，交给 fnOS 自身存储栈管理；不重配 UFS LUN、不做网络 iSCSI、不格式化、不改 fnOS 数据库。
- WSL 一次性镜像实测：1 GiB 父镜像、仅导出 768 MiB 数据分区、4096 字节逻辑扇区；只读导出拒绝写入、最后合法 LBA 可写、越界拒绝；ext4/Btrfs 子卷/只读快照/scrub 通过；重建导出后合成 VPD 身份与文件哈希一致；父镜像未导出区域哈希不变；`DevicePolicy=closed` 下可读父盘、不能以读写打开父盘、可读写导出盘。
- `llbitmap`：修复状态辅助函数把首页头当作位图数据传播的问题（保留头，仅标记数据位脏）；49 项 ASan/UBSan 测试通过。补丁以研究形式保留在本仓库 `patches/kernel/`，**目标源码与官方 c1147 适用性仍需核验**。
- 挂载信息扫描竞态：对退出进程的 `/proc/<pid>/mountinfo` 返回 EINVAL 的情况，只有确认进程已消失或 Z/X 且无 mount namespace 才跳过，否则最多三次重枚举；持久 EINVAL/权限/IO 错误仍拒绝启动。25 项单元测试及真机扫描回归通过。
- **限制**：官方 `trimacl`/`trimafs` 文件系统 ABI 未取得匹配源码，原生挂载仍不能视为完整支持；LIO 适配器是实验性路径，不是当前一键部署方案。完整存储卸载/停止仍有 EBUSY 问题。

## 6. Iris H264/NV12 解码

### 五级状态

| 等级 | 内容 | 结果 |
|---|---|---|
| 1 | 源码适配 | 已完成并公开 |
| 2 | 离线构建/协议/布局/行为 | 指定版本和范围通过；完整 ABI 仍未知 |
| 3 | 驱动绑定、固件协商 | 隔离实验中实际运行 |
| 4 | 真实解码任务 | 固定三帧隔离单会话条件验收 |
| 5 | fnOS 自带 FFmpeg / 影视真实任务、生命周期、重复、长负载、重启回归 | **未完成** |

### 已通过的隔离实验（level 4，条件验收）

- 固定 H264 三帧 → 128×128 NV12，每帧 24,576 B，PTS 1,000,000 / 1,040,000 / 1,080,000 µs，三帧逐字节匹配固定 oracle。
- 单次 RAM 启动被接受，主机匹配 RESULT，完整观察 60.0135346 秒并清理，收到 82,852 B FDE1；独立总验收 `verification_rc=0`、`outcome=fde_three_frame_accepted`。
- 关键推进：修复 REQBUFS 回调重复取锁；非阻塞 DQEVENT 空队列在该内核返回 ENOENT，仅对 DQEVENT 视为无事件；省略固件不支持的 COMV_COUNT 属性；写 CORE_CLK_RUN preset；固件前 `dev_pm_genpd_set_hwmode(false)` 并由账本持有；客户端 MAX_BUFS 从 8 扩到 32，容纳固件实际返回的 16 个 CAPTURE allocation。
- 停流：BITSTREAM STOP（header 98 / packet 105）与 RAW STOP（99 / 106）均取得身份匹配的成功 ACK；INPUT 累计提交/归还 3/3，CAPTURE 19/19（19 为累计提交，不是 19 个不同 allocation）；停流后 QUEUED/READONLY/unknown 位图为 0。
- **边界**：所有内部 backing 继续保留，未 free/CLOSE/PM teardown；不证明 DMA 停止或安全回收。验收是候选/单次 boot/主机/节点 ioctl/oracle 的条件证据，不是认证物理来源；原始报告中 hardware false 不反写。

### 生命周期研究（最新，未上机通过）

- 逐 allocation 代际账本：记录每次实际序列化提交的 header/packet/generation/tuple，APP 使用最新提交身份而非首投；内部对象区分旧代 PSC 释放、新代 STOP 返回、DPB 首投身份与 PERSIST 独立 RELEASE 请求，READONLY/unknown 不解除所有权。
- 有界关闭控制：STOP → 逐 APP 归还 → 剩余内部 RELEASE → CLOSE → 撤 lookup/base → 有界 CPU 响应排空；控制本身不 free。
- 180 字节逐对象回执，含内部 list ordinal、原 buffer index/retired、请求与实际回包身份/flags；原始 CLOSE ACK 尚未包含，因此完整 quiesce 仍不能判定。
- 三处安全门修复：reclaim=0 时公开 STREAMOFF/REQBUFS(0) 与 stop 回调均在进入框架前拒绝；内部归还先核对 QUEUED/PENDING/READONLY/retired/原 validator 再改所有者；所有 native BUFFER 先严格校验完整 STRUCTURE 再读 wire type，畸形回包不进入旧释放路径。
- 冷启动修复：发现 checked 电源入口在 cold INIT 持 core mutex 时再次获取同一锁，且 session-held helper 要求已有会话，与初始化无会话矛盾；单变量后继移除该 shortcut，恢复完整 checked provider 初始化。普通 O2/ASan/UBSan 与实际 core 调用者测试通过，GCC12 目标模块构建通过（179 imports/180 versions，完整 ABI 仍 unknown）。
- **边界**：一次加固镜像 RAM 实验在 runner loader 阶段超时 `-110`，客户端未启动、无新帧/PERSIST 回包。不能把该静态锁缺陷当已直接观测的设备根因，也不能以 host 测试冒充原生任务。

### PERSIST 生命周期修复（条件验收，两次真机复现）

冷启动修复后一次 RAM 实验首次越过 loader 超时：客户端启动、解出三帧、双 STOP ACK 匹配（98/105、99/106），但 quiesce 在 stage 2 以 `-110` 超时。45 条 180B 回执中除 PERSIST 外全部归还，PERSIST `flags=19`（valid+fw_owned+release_requested）、`returned=0`、`rx_generation=0`。

- **根因**：上游 `dec_ip_int_buf_tbl` 不含 PERSIST，它只在 close 时 force 销毁；本方案却单独发 HFI RELEASE 并等待 RELEASE_DONE，固件不会回该回包，1 秒硬件响应超时后判定失败。这是设计错误，不是硬件故障。
- **修复**：PERSIST 移出 release 列表，改由校验通过的 CLOSE ACK retire；`iris_repeat_persist_unclaimable()` 只在 unknown 或 release_requested 时拒绝声明；回执 `flags` bit 5 记录 `close_retired`。物理存储全部保留，`IRIS_NATIVE_REPEAT_RECLAIM_ENABLED` 仍为 0。
- **两次独立真机结果**：均 `control_rc=0`、`lifecycle_stage=5`、`cpu_drained=1`、`orphaned=0`，双 STOP ACK 匹配，三帧 PTS 1.0/1.04/1.08s 各 24576B，独立 FDE 验证两次通过；捕获 `b29e408d…`（101898B）与 `aa596c4f…`（101803B）。PERSIST 回执变为 `flags=35`（含 close_retired）、`request_generation=0`。
- **边界**：这是 PERSIST 关闭退休的单变量条件验收。CLOSE ACK 原始字段仍不在回执内，完整 quiesce 外部证明不成立；安全回收、全核心 DMA 停止、close/reopen、长负载与原生影视任务均未验收。详见[PERSIST 生命周期修复验收](fnos-persist-fix-acceptance-20261116.md)。

### 串口恢复入口（未验证）

为免除现场按键而加入的串口触发重启入口经过五版迭代，**至今没有任何一版在真机上成功触发重启**。错误依次为 `pause()` 阻塞（串口输入不产生信号）、描述符取自只在成功路径赋值的变量、在 observer 子进程使用 flow 子进程设置的全局、把 fd 号写进共享内存（fd 是进程局部的，跨进程无效）。当前版本改用已有的 `SCM_RIGHTS` 通道，编译通过但未上机。该入口即使工作也只能重启到正常启动目标，不能进 fastboot（官方 Image 未编入 `CONFIG_NVMEM_REBOOT_MODE`）。详见[恢复入口状态](fnos-recovery-entry-status-20261116.md)。

### 未完成

原始 CLOSE ACK 字段导出与关联、CPU 响应排空的物理证明、实际安全回收与 free、真实 close/reopen、30 轮会话、720p/1080p、至少 600 秒连续负载、seek/flush/DRC/错误取消、fnOS 自带 FFmpeg 与影视真实任务、无 CPU 静默回退证明、持久部署与重启回归。HEVC、硬件编码、NPU 独立且未验收。

## 7. 未适配/未完成功能

- **fnOS 下显示**：未适配官方内核的显示控制器/面板；fnOS 通过 WebUI/远程使用，不是本地桌面。
- **音频、蓝牙、GPS、摄像头、传感器融合**：未适配/未验证。
- **NPU / CDSP FastRPC**：固件与模块材料有离线准备，未在 fnOS 完成实际调用验收。
- **硬件视频编码、HEVC、AV1**：未验收。
- **系统挂起/休眠**：未适配。
- **OTA 升级**：未验收，不建议直接在手机上跟随官方更新。
- **完整安全停机/卸载**：存储卸载仍有额外引用造成 EBUSY，未做强制/lazy 卸载。

## 8. 证据分层

- 实机结果来自指定设备的单次或多次受控启动，原始日志/镜像/备份只留私有，不随仓库公开。
- 离线测试（416 项 Python、Iris 三组普通/ASan/UBSan、GCC12 构建、ABI 导入 CRC）只证明对应 host/构建范围，不是物理 DMA、原生任务或完整 ABI 证明。
- 条件验收不等于认证物理来源；hash/nonce 提供关联与完整性，不提供设备来源认证。
