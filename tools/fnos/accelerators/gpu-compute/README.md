# GPU compute acceptance test

One bounded compute-shader dispatch on the Adreno 730. This is **not** an NPU, AI-model,
video or display test; a successful run proves only that a Vulkan compute pipeline executes
and returns correct results on the GPU.

## Files

| File | Purpose |
|---|---|
| `add.comp` | GLSL 4.50 compute shader: `result[i] = a[i] + b[i]`, `local_size_x=64`, index bound 255 |
| `generate-shader.py` | Compiles the checked-in GLSL with a trusted `glslangValidator`, validates with `spirv-val`, records SHA256 |
| `oracle.h` | Deterministic inputs and the CPU-side expected result, including 65 guard words |
| `test-oracle.c` | Offline assertions on the oracle (index bounds, guard words, uint overflow case) |
| `test-gpu-compute.c` | The device test: strict Adreno 730 selection, compute queue, single dispatch, fencing, readback |

## Contract

- Device must be vendor `0x5143` (Qualcomm), name containing `730`, and an integrated/discrete GPU.
  Any second eligible device is an error. `--software-rehearsal` accepts only a CPU device and
  prints `SOFTWARE_REHEARSAL_NOT_HARDWARE`; it is a self-test, never phone acceptance.
- No CPU/software fallback exists for the hardware path: no eligible device means failure.
- One dispatch of 4 workgroups x 64 invocations covering 255 elements; the shader bounds itself,
  so writes past element 254 would corrupt sentinel words and fail the guard check.
- 320 storage words per buffer: 255 outputs plus 65 sentinel words that must stay untouched.
- Host→shader and shader→host barriers; explicit flush/invalidate for non-coherent memory.
- Fence wait is capped at 3 seconds and the process has a 20-second alarm; failures `_exit` rather
  than waiting indefinitely on a possibly hung GPU.
- Requires a verified SPIR-V file; the program refuses data that is not SPIR-V magic.

## Build and offline gates

```sh
# Historical external pipeline: not shipped; see tools/README.md external inputs.
```

That compiles the oracle test, compiles the Vulkan program against pinned headers, and attempts
shader generation. Shader generation is blocked unless a trusted `glslangValidator` and `spirv-val`
are available; no substitute SPIR-V is ever synthesized.

Software rehearsal (host, llvmpipe) and the ARM64 cross build are recorded under
`external historical evidence (not distributed)` and `/var/tmp/compute-arm64-*`; neither is hardware acceptance.

## Running on the device (separate authorisation)

1. Confirm the running kernel, `libvulkan` and the freedreno ICD; the launcher must not assume the
   installed `c1193` packages match the running `c1147` kernel.
2. Copy the verified `add.spv` and the ARM64 binary, verify their SHA256, and run as the normal user
   in the render group — not as root.
3. Success requires `GPU_COMPUTE_HARDWARE_ADRENO_SHADER_OK elements=255 guard_words=65`.
4. Any `GPU_COMPUTE_FAIL`, timeout or sentinel mismatch is a failure; do not retry in a loop or fall
   back to software.

NPU and video have separate gates; see the parent README.

Release: no private build/rehearsal pipeline is distributed. Provide trusted Vulkan headers/loader, compiler, glslangValidator and spirv-val externally. Compile `test-oracle.c` independently for a CPU-only gate; shader generation is explicit via `generate-shader.py`. This release did not run any GPU or device command.
