# Iris source-only public snapshot

`verified/` refers only to the original fixed three-frame / STOP retained-evidence lineage. It does not mean the published gated rebuild has been device-verified. `research/` is the cold checked-power successor: actual extracted functions were host-compiled/tested, not phone-validated. Neither is production-admitted; compile governance is unchanged.

All existing execution/provider-admission/decode/release-observer/STOP/native-repeat/reclaim enable macros default to 0. Retention-installed hooks are code-presence markers, not execution admission. Public fixed experiment selectors are non-unique constants, not credentials. Original evidence sources were never edited. Every copied source has its published SHA-256, `originalactualsourcehash`, license notice status and transformation in its role manifest; paths are relative to `sources/iris`. Original GPL notices are preserved; Makefile/Kconfig and approved original project derivatives receive GPL-2.0-only SPDX without invented copyright. Main supplies license texts. Original project files lacking a notice receive the user-approved GPL-2.0-only SPDX in the release derivative, recorded with a new hash; no third-party license is overridden.

Upstream reference is the existing inventory's Linux 6.18.18 and drivers/media/platform/qcom/iris path. No upstream commit is asserted and no Git/network operation was performed. Exact official fnOS source/patch/config pairing is not established. Reference repository/tag is not authentication of these derived bytes.

## Roles and missing inputs

- clients/decoder-evidence-hold.c: original successful MAX32 fixed-three-frame HOLD client.
- clients/decoder-stop-evidence-hold.c: bounded STOP evidence client.
- clients/decoder-quiesce-hold.c: latest quiesce 180-byte receipt collector; absent raw correlated CLOSE ACK prevents complete-quiesce/reclaim proof.
- clients/decoder-repeat.c: future ABI unresolved, guard/adapter ENOTSUP. future-abi.h intentionally has empty external fixture pins and resolved=0; no native acceptance.
- clients/verify_fde.py: original offline verifier logic with an SPDX-only derivative; parser uses its adjacent relative path and checks the derivative hash, with original hash retained in the manifest. Caller must provide capture/trace/FIR1/FNV1/oracle and expected attempt/bindings/node independently. No raw evidence or fixture binaries are distributed.
- pid1/: allowlisted C/H from the STOP-DPB source snapshot only. module-records.h, decode-assets.h, decode-bindings.h and runner-record.h generated binding/pin headers are omitted; generic ram-environment.c/.h is retained after review. supervisor-sha256.inc is exported as supervisor-sha256.h with corresponding relative includes. Includes remain explicit: externally supplied generated binding/pin headers are required. No module binaries/firmware/kernel trees/images included. This is research source, not a fully reproducible standalone PID1 or device deployment kit.

## Minimal host tests

From the release root on Linux with Python and cc/GCC ASan+UBSan support, run:

```sh
python3 -I -B tests/iris/response_test.py sources/iris/research
python3 -I -B tests/iris/wrapper_test.py sources/iris/research
python3 -I -B tests/iris/cold_power_test.py sources/iris/research
```

Each compiles exact hash-checked source functions in native -O2 and ASan+UBSan -O1 modes. Host definitions deliberately enable branches to test ownership/dispatch/wrappers/cold checked power; they do not edit or enable the released gates. Cold power includes the explicit historical single-function red mutex mutant. Framework/provider/routing/PM/firmware are mocked seams, not hardware. No kernel/module/phone execution occurs. Original source-editing finish/prepare scripts are not distributed or executed.

`--output EXISTING_PARENT` creates a new unique child; default is a new /tmp child on Linux. Root or symlink output parents/ancestors are refused. Tests write only their new work directory, never source manifests or existing evidence. Results prove host logic only, not DMA, hardware decoding, deployment, teardown safety or production readiness.
