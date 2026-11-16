# SPDX-License-Identifier: CC-BY-4.0

# Iris 离线路径测试

这些测试编译发布源码中的实际函数，使用明确的小型host facade；**不运行手机、固件或真实DMA，也不开放发布驱动的默认执行门**。

在Linux/WSL仓库根目录执行：

```bash
python3 -I -B tests/iris/response_test.py sources/iris/research
python3 -I -B tests/iris/wrapper_test.py sources/iris/research
python3 -I -B tests/iris/cold_power_test.py sources/iris/research
```

每组分别普通O2与ASan/UBSan O1构建并执行，输出写入新的临时工作目录。真实覆盖包括响应分派、allocation owner代际/tuple、畸形回包旧释放回退拒绝、STREAMOFF/REQ0公开守门、退出保留以及cold-init mutex重入回归。

测试facade可使能指定分支以检查它们，但不会修改公开源码。IRQ并发、full VB2、provider raw状态和SYS_INIT回复等由facade代替，不能将测试结果计为设备通过。`test-results.json`记录已完成host测试；源码比较/发布关联见各role manifest与`export-audit.json`。

客户端/验证器的外部捕获、FIR1/FNV1与oracle未包含。缺少它们的测试仅验证坏输入拒绝，不能说成完整帧验收。研究版尚未上机，原三帧/双STOP实验的边界见[状态](../../docs/status.md)。
