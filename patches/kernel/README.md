# fnOS 存储补丁研究：适用范围

本目录只保留 `0098-md-llbitmap-preserve-header.patch`，作为官方 fnOS `6.18.18.c1147-trim` 相关存储候选研究。它不是完整内核树，也不保证可直接 `git apply`。

## 明确边界

- **目标源码与适用性仍需核验。** 原 md/llbitmap 实现与补丁所依赖的基线不在库内；先取得合法外部源码、确认上下文和许可，再做 `git apply --check`。
- 该补丁不会使 llbitmap、trimacl、原生视频或模块 ABI 自动获得验收。历史存储结果、正常重启和完整安全停机是不同证据层。
- 旧 `config-v78` / `config-fnos` 配置已移除：它们实际是 7.2.8 配置，不是官方 c1147 配置。目标内核的源码、headers 与配置需由使用者按官方版本独立取得并核验。
- 不提供固件、Image、boot.img、模块、设备校准或私有证据。

## 外部输入

官方 headers、`Module.symvers`、`.config`、匹配源码、存储适配器与验收说明见 `../../tools/fnos/storage/`、`../../tools/fnos/accelerators/native/sources.json` 及对应 README。

缺任何必要输入时停止；不给出半套 quickstart、不生成 dummy 固件，不以 host C/解析/事务测试冒充存储、原生解码或 DMA 实机结果。
