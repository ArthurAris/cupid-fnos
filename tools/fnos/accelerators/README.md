# Accelerator adaptation tools

Read-only and offline tooling for GPU compute, NPU (Hexagon/CDSP) and video acceleration on
`cupid`. The five-level status table and the current blockers are in
[docs/fnos-accelerator-adaptation.md](../../../docs/status.md).

| Path | Purpose |
|---|---|
| `gpu-compute/` | Single-dispatch compute-shader acceptance test, GLSL source, SPIR-V generation and CPU oracle |
| `npu/enumerate-fastrpc.py` | Presence inventory for remoteproc and `/dev/fastrpc*`; no IOCTL, invoke or driver load |
| `video/enumerate-v4l2.c` | V4L2 capability/format enumeration only; never `STREAMON` or changes format |
| `audit-materials.py` | Static material gate: pinned SHA256, DSP ISA, Linux-glibc vs Android bionic, SM8450 module alias |
| `test-gates.py` | Offline rejection tests for those gates |
| `native/` | Fail-closed compilation inspection for FastRPC/videocc/Iris prototypes; revised SM8450 Iris probe refuses hardware access |
| `native/layout.py`, `native/test-layout.py` | Required scoped BTF/DWARF comparison on fresh objects; missing/mismatched type shapes fail, full ABI and hardware stay unverified |
| `native/type-signatures.py`, `native/test-type-signatures.py` | Scoped member types and selected actual imported functions compared against official provider BTF; unresolved information blocks prototype publication |
| `verify-firmware.py` | Pinned private firmware/manifest verification against actual candidate DTB memory, with strict ELF/MDT and split/full-image checks |
| `dtb-status.py` | Exact status-property edits with full DT/property/byte scope checks; no partition access |
| `test-firmware.py`, `test-dtb-status.py` | Synthetic firmware/FDT negative tests; no proprietary materials |

## Boundaries

- This directory includes offline parsers/builders plus separately gated runtime helpers. Native deployment and native-job paths are not read-only: only their host fixtures were exercised here. No published test proves physical DMA, native decode, or firmware authentication.
- ADSP running, working battery telemetry, GPU Vulkan submission and CPU execution are not NPU
  evidence. Decode success is not encode evidence. GPU utilization is not codec evidence.
- Material manifests are private and pinned by SHA256; the auditor reports
  `hardware_ready: false` and `firmware_signature_and_pairing_verified: false` until a real signed
  runtime and firmware pairing is established.

## Offline commands

```sh
python3 -I -B test-gates.py
# Historical external pipeline: not shipped; see tools/README.md external inputs.
# Historical external pipeline: not shipped; see tools/README.md external inputs.
```

Passing these means the tools behave as intended, not that any accelerator works on the phone.
