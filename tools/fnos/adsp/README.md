# 官方 fnOS 的 ADSP / 电池驱动链

## 历史实现与当前发布边界

下文保留2026-11-11这组工具的历史范围，不覆盖后续ADSP电池出数/UPower与UPS页面的设备验证。新版源码的离线检查不是原部署版本的完整验收；当前成果和限制统一见[公开状态](../../../docs/status.md)。

历史旧版曾出现ADSP/IPCRTR运行但电池EAGAIN；这里修复了PDM名称匹配，并保留真实属性读取后才能ready的门。UPS保护阈值、异常关机及完整重启回归尚未完成。

**显式运行安装器会按新安装策略执行systemctl enable，供后续开机加载；并不立即start。** 启动脚本在disabled/degraded等条件下仍可能尝试标准pmic_glink fallback。此行为未在本次发布操作中执行，不能将本工具称为纯只读或承诺不会启用服务。

已确认旧实现错误：`qcom_common.c`用`auxiliary_device_add()`，树外构建把PDM设备名变成
`cupid_qcom_rproc_common.pd-mapper`；官方mapper只接受`qcom_common.pd-mapper`。
新版只对该注册调用传标准名称，不整体改KBUILD_MODNAME，不改官方mapper。
设备目录与驱动目录同时存在不等于绑定成功。

## 离线构建

```sh
bash build-adsp-modules.sh <official-headers> <upstream-6.18.18> <local-7.2.8> <new-output-dir>
```

- 输出目录必须不存在；构建工作目录始终新建，保留日志及未strip对象供ABI核验。
- `source-inputs.json`固定已核验输入字节（源码CRLF规范化后）；不下载或执行输入目录中的Python。
  该清单记录本地接受的版本，不替代上游签名认证；官方头文件来源仍须可信。
- 基础设施与QRTR用上游v6.18.18；PAS/common/q6v5用本地7.2.8，匹配官方回移API。
- `prepare-build.py`要求补丁锚点唯一：补hwspinlock模块license、保留标准PDM名称；PAS
  移除官方未导出的shutdown IRQ访问器是既有兼容取舍，**并非证明所有attach情形等价**。
- 生成完整10模块及`module-manifest.json`（固定顺序、SHA、vermagic、原始GNU build note）。
  检查全部官方/组内版本化导入、强未定义符号和官方导出冲突，不只打印CRC结果。
- 本轮10模块362条版本化导入通过，14项跨模块共享结构布局匹配官方Image/模块BTF。
  这些静态门不证明固件握手、远端服务状态、真实电量或故障恢复。

## 启动

`start.sh`仅用`python3 -I -B`调用`adsp-start.py`，不会从cwd导入。

1. 持共享`/run/cupid-adsp.lock`，检查部署pending、内核/板型/发行版、可信enabled与整组文件。
2. 加载前核SHA/ELF build note/name/vermagic、安全父路径；已加载模块核live/build identity。
3. 按name与父平台设备识别ADSP；不假定remoteproc0。核对ADSP所属IPCRTR实际driver/module。
4. 检查标准PDM辅助设备实际绑定官方mapper。绑定并不单独证明locator握手。
5. 原生电池capacity/present/status/type有界读取，EAGAIN/暂未创建重试；0–100容量有效。
6. 只有读到真实有效属性才报告ready；degraded/failed非零，NAS不得Requires本unit。

持久state按boot_id去重、写前fsync；同boot失败不重新加载；两个不同boot失败后安全模式。
旧attempts迁移，旧ok不是成功证据。锁不可得时不做任何模块改动；持锁状态存储失败允许标准退路。
wrong-env/disabled/safe/error的标准`pmic_glink`退路有界且检查live；失败明确上报。
ExecStopPost补处理失败/超时，正常停止不卸载模块。**不能保证从内核卡死、SIGKILL或不可中断modprobe恢复**。
PDR会在locator上线时调度待查询，旧“早probe必然永久失败”注释不再成立。

## 文件部署与回退

本轮没有运行生产安装。未来安装前仍须单独上机授权、核验身份/电量、双端备份哈希和恢复材料。

- 用新的干净stage放10模块、module-manifest、start.sh、adsp-start.py、deploy.py、unit、blacklist。
  `prepare-package.py <stage>`生成空enabled及固定17项package-manifest；额外文件/链接拒绝。
- 包SHA是待审查产物，不是自动授权。可信安装器用`install.sh <stage> <externally-accepted-SHA>`。
  不执行尚未核验stage里的deploy.py。
- 安装/卸载拒绝active或过渡中unit、不自动stop/start，不强制卸载或重绑。
  必须另行正常stop（不卸载ADSP）后，才可做文件事务。
- 记录原目标字节/模式、systemd enable状态，逐文件原子替换/fsync/读回；失败恢复并保留备份。
  多文件事务不是单步全局原子操作：掉电留下pending，新loader拒绝custom加载，需显式`deploy.py recover`。
- `uninstall.sh`只按record恢复原文件，漂移/外来链接拒绝所有目标写入；不递归删除目录，不删启动计数。
  旧安装被接管后回退恢复的是**接管前旧配置**，不是必然stock。
  默认不改模块；`--pmic-fallback`是显式标准补载，失败非零且说明文件事务已提交。

## 离线测试

```sh
python3 -I -B test-startup.py
python3 -I -B test-deployment.py
python3 -I -B test-build.py
```

在WSL Linux root的临时模拟根执行，真实subprocess/modprobe/insmod禁止。
76项启动、61项部署及6项构建准备测试覆盖身份/权限、乱序、ABI身份、实际绑定、EAGAIN、超时、同boot去重、
两次失败、安全退路、共享锁、中断事务恢复、安装/卸载故障和漂移保护。模拟成功不是硬件验收。

下一阶段需先取得新鲜电量与安全上机方案；验证PDM真实绑定、locator/charger PD、原生容量→UPower→网页，
再验证正常重启和UPS保护。不能用固定百分比、伪造DMI或“unit success”代替验收。
