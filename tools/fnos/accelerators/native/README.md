# Native accelerator compilation prototypes

> Historical prototype notes below are not the latest successful isolated task source and do not override the public status. Latest source snapshots are in [sources/iris](../../../../sources/iris/README.md); current verification scope is in [docs/status.md](../../../../docs/status.md). Numbers below describe dated private experiments, not newly reproduced public hardware acceptance.

**Not ready for driver loading.** SM8450 Iris returns `-EOPNOTSUPP` before
register mapping or hardware attachment, with no bypass parameter. FastRPC and
videocc are also non-loadable compilation prototypes, not accepted adapters.
See [the review](../../../../docs/status.md)
and [the recheck](../../../../docs/status.md).

## Parallel lifecycle batch and actual full-ABI rerun

The latest private batch fixes OPP/ICC dirty progress, positive PM-put results,
decoder partial allocation/control unwind and m2m errno preservation. The
explicit bound-device coordinator now runs terminal shutdown, actual orphan
reclaim and strict resource release; command admission is checked before PM
get and before publication. A single host fixture executes the actual cleanup
coordinators through IRQ unregister and final core destruction, rather than
counting separate helper tests as end-to-end proof.

Fourteen scoped suites pass 413 tests plus 322 sanitized scenarios; normal
424 tests and build/static/exact replay pass. Full ABI was actually rerun on
27 objects: 998 shared types (602 verified, 396 unknown, no detected mismatch),
158 imports with only 41 complete signatures; CLI exits 2/unknown. Its wrapper
also exits 1 because the 192 MB report exceeds its read limit; both are retained.
Neither unknown nor zero mismatches permits loading. Actual void remove/unbind,
provider/module/DMA, normal recovery and full build provenance remain blocked.
See [the finite batch acceptance record](../../../../docs/status.md).

## Private PM guard / IRQ unregister handoff candidate

The newest inactive candidate holds an explicit checked-mode PM keepalive owner,
retires successful managed transient gets with noidle, and never puts after a
failed get. IRQ unregister revokes eligibility under core/raw locks, waits/frees
outside callback locks, then clears ownership in the same lock domain. Only
unpublished probe unwind or prepared terminal resource release can invoke it;
probe cleanup failure retains action/resources. Normal platform remove is NOT
integrated and cannot safely unload the retained IRQ/module/provider owners.

Eleven scoped suites pass 320 tests (263 scenarios and 57 mutations/rejection
schedules), with 263 sanitizer scenarios and normal 424 tests. New passive IRQ
tests independently release hard-top/queued-bottom/response gates; detach refs
are atomic with forced overlap, and last-poll boot success is checked.
Build/static CRC/exact replay pass, but actual remove/system sleep/physical
IRQ/provider/module/DMA and complete ABI remain unverified.
See [the PM/IRQ-release record](../../../../docs/status.md).

## Private IRQ ownership / Gen2 startup candidate

The newest private candidate tracks INITIAL/TOP/TERMINAL disable ownership,
executes IRQ APIs outside the driver raw lock, and retires TOP only after a
permanent terminal mask and actual synchronization. Checked power preparation
no longer enables the host IRQ: activation follows successful polled firmware
boot and precedes SYS_INIT; validated ACK precedes session-ready publication.
Known invalid uc_region and SFR-allocation free-size defects are repaired.
Managed legacy deinit refuses the undrained SCM path; Gen1 startup and checked
runtime PM remain unsupported. The hardware probe refusal is unchanged.

Nine scoped suites pass 266 tests (222 scenarios and 44 mutations), with 222
ASan/UBSan scenarios, plus the normal 424-case stack and build/static/replay.
IRQ/startup tests now run actual top/thread code against a host depth model,
not merely counters. Old power/strict/counter-only quiesce suites remain
historical and are not added as current acceptance. Actual IRQ chip, boot-ready,
remove/provider/module/DMA and complete ABI contracts remain unverified.
See [the IRQ/startup record](../../../../docs/status.md).

## Private strict IRQ/power-drain correction

New negative tests reproduce gaps missed by the earlier 292-case candidate:
poweroff/freequeue without threaded-IRQ drain, MMIO during partial-clock retry,
and a void-power fallback publishing terminal success. The newest inactive
candidate requires producer IRQ/work synchronization before checked poweroff,
retains initialized queues on early failure, records a controller handshake
checkpoint, and separates startup/rollback errors. Missing checked hooks refuse.
Checked runtime suspend/resume is explicitly unsupported in this terminal-only
experiment; normal recovery parity is not claimed.

The new strict suite passes 33 tests. Nine scoped suites total 276 passing tests
(234 scenarios and 42 mutations), plus 234 ASan/UBSan scenarios and the unchanged
normal 424-case stack. Historical test-vpu-power's 49 cases are not rerun as
candidate acceptance because they assume now-refused undrained/runtime paths.
Compilation/static CRC/exact replay pass, but actual remove, complete IRQ depth,
provider/module/DMA and ABI contracts remain unverified. See [the strict record](../../../../docs/status.md).

## Private frontend unregister / retryable detach coordinator

The newest inactive candidate separates video object lifetime from registration.
Its explicit coordinator revokes opens, unregisters nodes once, and returns
EBUSY while file/open-inflight/video/orphan owners remain. Only after those
owners retire can it prepare terminal shutdown and release resources; release
failure retries do not resume PM again. This is NOT connected to void platform
remove, does not drop binding ownership, and does not enable orphan reclaim.

Nine private suites pass 292 tests (247 scenarios and 45 mutations), with 247
ASan/UBSan scenarios. Normal 424 tests, compilation/static CRC and exact replay
pass. V4L2 unregister is not a reference drain; framework/provider/module/DMA
lifetime remains unverified. See [the frontend detach record](../../../../docs/status.md).

## Private power IRQ-reference / init handoff candidate

The newest inactive candidate returns the IRQ enable reference taken by checked
power-on exactly once (skipping the extra disable when quiesce already masked,
never twice on retry), routes the `iris_core_init` pre-loader failure path
through checked power-off, retains queues when power is unknown **or progress is
unretired**, and propagates the real PM resume errno on checked platforms.

Eight private suites pass 263 tests (223 scenarios and 40 mutations); all 223
sanitizer scenarios pass. Normal 424 tests, target compilation/static CRC and
exact patch replay pass. IRQ enable/disable depth, non-VPU2 platforms, platform
remove and supplier/module/DMA lifetime remain unverified; the candidate stays
private and disabled. See [the power IRQ record](../../../../docs/status.md).

## Private checked VPU2 power candidate

The newest inactive candidate adds error-returning power hooks to the VPU2 ops
only: every ICC/domain/OPP/reset/clock/hwmode step is checked, a handshake
timeout returns the original errno and stops further power removal, and
`pm_runtime_put_sync` always consumes its reference (a negative return marks the
state unknown instead of being retried). The terminal path clears
`power_prepared` and frees queues only after checked power-off succeeds.

Eight private suites pass 248 tests (212 scenarios and 36 mutations); all 212
sanitizer scenarios pass. Normal 424 tests, target compilation/static CRC and
exact patch replay pass. Non-VPU2 platforms still use the unverified void
callbacks, and platform remove, supplier/module lifetime and hardware handshake
timing remain unverified; the candidate stays private and disabled. See [the
power candidate record](../../../../docs/status.md).

## Private explicit hardware-resource owner candidate

The newest inactive candidate explicitly pairs IRQ/MMIO/ICC/clock/reset/
domain/OPP/runtime-PM acquisition and release, saves successful OPP tokens,
registers IRQ with NO_AUTOEN, and holds a separate hardware CPU owner. A guarded
release interface requires terminal drain and no live file/queue/power/PM
owners; a PM user appearing during the barrier retains handles for retry.
Failed pm_runtime_get_sync references are balanced. It is NOT wired into void
platform remove and does not prove supplier/module lifetime or checked
poweroff/DMA contracts.

Seven private suites pass 214 tests (182 scenarios and 32 mutations); all 182
scenarios pass ASan/UBSan. Normal 424 tests, target compilation/static CRC and
exact patch replay pass. The candidate remains private and disabled. See [the
hardware-owner record](../../../../docs/status.md).

## Private CPU-owner / secure-load progress candidate

The newest inactive candidate separates core/packet CPU storage from devm,
accounts for binding/video/open/orphan references, rejects opens until probe
finishes, and holds a temporary reclaimer owner. Its explicit prepare-remove
interface revokes opens and rejects live/in-flight files or orphans; it is NOT
wired into void platform remove. Secure-loader attempts and shutdown errors are
recorded so uncertain half-load failures retain queues/power until terminal
drain. CPU refs do not retain device-managed hardware or module code.

The six private suites pass 167 tests (142 scenarios and 25 mutations), with
142 ASan/UBSan scenarios. The normal 424-case stack, module static/CRC checks,
and exact patch replay pass. This is not full remove/devres/DMA or ABI
acceptance; the candidate remains private and disabled. See [the owner/progress
record](../../../../docs/status.md).

## Private one-way terminal drain producer

The current private experiment implements an explicit quiesce producer:
block thread re-enable/recovery scheduling, wait for IRQ and worker outside
callback-required locks, then check secure shutdown before core queue cleanup.
Twenty-five tests (22 scenarios and three mutations) and 22 sanitizer scenarios
pass, including failure/retry ownership and feeding the actual orphan gate.
This is host-model validation, not kernel hardware drain acceptance. It uses a
one-way terminal state and disables automatic recovery; driver remove/devm
ownership, gen1/PM parity and full IRQ-depth/DMA contracts remain blockers.
The producer is not enabled or automatically called; the normal 424-case stack
remains unchanged. See [the producer record](../../../../docs/status.md).

## Private file-detach/reclaim-gate candidate

The latest private candidate detaches a failed release from its live file/fh,
drains CPU response owners, and transfers retained resources to a core orphan
list. Thirty actual host cases (26 scenarios plus four mutation controls) and
26 sanitizer scenarios pass. Reclaim requires secure shutdown, IRQ drain,
recovery-work drain, no active sessions and explicit detached/zero-ref owners.
**There is no verified IRQ/work proof producer**, so actual reclaim remains
blocked; device references do not protect devm core lifetime during unbind.
This candidate is not in `sources.json` and does not complete firmware/DMA or
remove/reclaim acceptance. The normal 424-case baseline remains passing.
See [the gate candidate](../../../../docs/status.md).

## Private bounded-ACK/shutdown candidate

The newer private candidate adds dedicated OPEN/START completions and a bounded
wait: missing ACKs mark ERROR and retire pending ownership; wrong/late replies
cannot restore success. Checked core shutdown preserves the SCM shutdown errno
and does not power off/free queues on failure. Thirty actual command/wait/
shutdown cases and 26 sanitizer scenarios pass; the earlier 29 close/lifetime
cases also pass. No agent was used for this iteration. It is **not** enabled in
`sources.json`: final uncertain file-release reclaim, firmware echo, full reset,
IRQ and PM ownership remain unverified. See [the current candidate](../../../../docs/status.md).

## Private response-lifetime candidate

A new 0009 candidate is **not** enabled in `sources.json`. It adds referenced
gen2 response lookup, drained resource teardown after normal vb2 streamoff,
serialized INIT completion/timeout, matched OPEN/START failure handling, and
allocation-free 16-instance error batches. Checked close now propagates
streamoff/CLOSE submission, failed ACK and timeout outcomes; uncertain firmware
ownership retains resources in a diagnostic quarantine, **not a complete final
V4L2 file-release/reclaim path**. ACK deadlines/retirement remain unresolved.
Twenty-nine pthread/real-wait/handler tests and twenty-four ASan/UBSan cases
pass; fixture synchronization-object leak checks are excluded. Final independent
review was blocked by provider rate limiting.
The installed 0001–0008 stack retains 424 passing regression cases. Neither
candidate compilation nor these tests prove kernel IRQ/DMA or full lifetime
correctness. See [the candidate record](../../../../docs/status.md).

## Latest lifecycle work

Pinned 0007 adds INIT/STOP/CLOSE correlation by packet-id/type/port, with
spinlocked metadata and transaction ownership retained until the waiter ends.
0008 propagates DRC parse errors before notifications/START and rolls back
initialization resources and only newly allocated unsubmitted buffer suffixes.
The new cleanup suite runs 56 actual-C cases plus three deliberate regressions;
control correlation runs 22 cases plus three deliberate regressions. Both have
sanitizer modes. Final regression: 424 passing cases in fourteen suites, no
skips, with four sanitizer suites and three fresh builds complete. Strict ABI
reports remain unknown; no hardware or complete-lifecycle acceptance is claimed. These do not prove timeout-tail/session lookup concurrency,
OPEN/START response coverage, actual firmware echo, DMA or hardware readiness.
See [the lifecycle record](../../../../docs/status.md).
ABI unknowns now have root-cause categories and input/provider identities; data
symbols and NOTYPE ambiguity are not treated as verified function signatures.
The target MDT-loader source remains unavailable: the local 6.18.18-labelled
archive is truncated, and readable SCM source differs from official headers.

## Latest safety repairs

The earlier 842/842 type count and 287 tests did not establish complete ABI/HFI
acceptance. The revised validators reject the reproduced false positives:

- `abi-complete.py` preserves qualifiers, distinguishes void/typed pointers,
  treats incomplete functions/aggregates as unknown, checks every selected
  local/provider variant and reports missing imported signatures separately.
- All component objects come from the builder's immutable object list and are
  cross-checked with the generated Kbuild `.mod` and link command records.
  Iris now inspects 27 objects, not the former six-object scope.
- Final regression: 335 tests in twelve suites, no skips; 43 response and 24
  layout cases also pass ASan/UBSan. Fresh GCC15.2 builds all finish, with
  `abi_status=unknown`: FastRPC 334 verified/259 unknown of 593 shared names;
  videocc 312/268 of 580; Iris 601/396 of 997, with 27 objects inspected.
  No detected mismatch is not a proof of compatibility; complete graph and
  ABI flags remain false. See [the repair](../../../../docs/status.md).
- An audit of the older Iris prototype finds 995 shared type names: 599
  verified, 396 unknown, zero detected mismatches; only 29/134 strong imported
  signatures are fully evidenced. This is **not** complete ABI acceptance.
- The standalone complete-ABI CLI returns nonzero for unknown/mismatch/error.
  Build publication rejects actual mismatches. It may retain a non-loadable
  prototype with explicitly unknown ABI evidence and `passed=false`,
  `complete_abi_verified=false`, `hardware_ready=false`.
- Static ELF symbol parsing checks announced counts and contiguous row indexes,
  so syntactically valid partial symbol tables are refused.

## Offline reproduction

```sh
python3 -I -B build.py iris --upstream /path/linux-6.18.18 \
    --headers /path/official-headers --out /var/tmp/new-iris \
    --layout-spec "$BTF_SPEC" --layout-spec-sha256 "$BTF_SPEC_SHA256"
# The same options apply to fastrpc and videocc.
python3 -I -B test-build.py
python3 -I -B test-layout.py
python3 -I -B test-type-signatures.py
python3 -I -B test-abi-complete.py
python3 -I -B test-iris-behavior.py
python3 -I -B test-hfi-lifecycle.py
python3 -I -B test-hfi-response.py
python3 -I -B test-output-formats.py
python3 -I -B test-hfi-response.py --sanitize
python3 -I -B test-output-formats.py --sanitize
python3 -I -B ../test-firmware.py
python3 -I -B ../test-firmware-runtime.py
```

The layout spec contains separately pinned private Image/System.map/split BTF
references. Callers cannot weaken the scoped layout requirements. Source and
patch inputs are explicitly pinned, ordinary files only; extra source entries
and zero-fuzz patches requiring an offset are refused. Builds run under a fixed
PATH/locale. Full shared-header, Kbuild-tool and compiler provenance remains
unverified: the three header pins and a GCC-version comparison do not establish
all those identities. The official module uses Debian GCC 12.2.0-14; the local
rootfs compiler is 12.2.0-14+deb12u1, not the identical package revision.

## Static and ABI gates

The original module gate checks AArch64 ELF64 ET_REL, module name/vermagic,
GPL license, exact OF alias, SHA1 build-id, strong/weak symbol binding, provider
existence, every imported modversion CRC and export conflicts. The complete
ABI tool can rerun that separate gate when `--headers` is supplied. It never
labels an absent local signature as CRC-verified without that check.

The scoped `layout.py` and `type-signatures.py` gates still check their named
cross-core structure/function requirements. The recursive ABI audit records
verified/unknown/mismatch separately; incomplete named pointers are unknown,
not silently equivalent to arbitrary targets. Array storage-extent comparison
does not prove source indexing semantics. No BTF or absent/optimized DWARF
information is not evidence of compatibility. Private-type exclusions and all
checked object hashes are reported.

## Pinned Iris patch stack

Historical 0001–0004 are retained. The repair patches are applied afterwards:

- **0001:** SM8450 platform prototype and early probe refusal.
- **0002:** partial no-opb/context-bank/DRC-RESUME, stage/pipe and TZ FIFO
  exception backport. These are local-reference behaviors, not firmware proof.
- **0003:** historical encoder DPB addition; its ten-bit arithmetic was wrong.
- **0004:** historical capture-format port; its `ALIGN(width,192)` and
  response/stride integration were incomplete.
- **0005:** actual received-length queue API, validated ring indexes/published
  byte extent/capacity/alignment, total HFI message and property payload bounds,
  correct INIT type check, error completion/wait-state handling. Old gen1 queue
  callers retain compatible wrappers; gen1 parser equivalence is not claimed.
- **0006:** checked common capture-layout calculation, arithmetic rounding to
  192-pixel multiples, byte stride, aligned UV scanlines and sizeimage shared
  by TRY_FMT/S_FMT/input-size updates/DRC/HFI stride; DPB sizing reuses the
  existing target `hfi_buffer_dpb_enc` instead of duplicated faulty formulas;
  encoder HFI_DPB maps to the actual `BUF_DPB` list for no-opb instances.

**SM8450 capability restriction:** NV12 is the sole candidate output; encoding
and P010/QC08C/QC10C are refused by platform-specific checks. Generic layout
helpers can be tested offline without advertising unproven formats. Ten-bit
response negotiation and no-opb UBWC split policy remain unknown. Other
platforms retain their non-restricted paths. The hardware probe remains refused
regardless of these checks.

## Behavior-test scope

- `test-iris-behavior.py`: 44 selected actual C control-flow/packet/buffer cases,
  including target-helper encoder DPB sizing. Corrected 720p ten-bit helper
  result: 2031616 bytes, not the earlier 1593344; this is not SM8450 firmware
  acceptance or proof of the platform's buffer contract.
- `test-hfi-lifecycle.py`: 21 serializer/selected wrapper cases; subscriptions,
  firmware and framework waits are stubbed. It is not a full driver lifecycle.
- `test-hfi-response.py`: 43 actual queue/header/init/close/stop/version/wait cases,
  including truncation, wrap, destination guards, zero packet count, wrong INIT
  and error-during-wait. The same cases run with ASan/UBSan. It does not prove
  full session dispatch, packet-id correlation, late-response lifetime,
  suspend/resume, DMA or hardware behavior.
- `test-output-formats.py`: 24 actual enum/TRY_FMT/S_FMT/layout/stride/DPB-mapping
  cases, with sanitizer mode. DRC's shared layout call is implemented, but full
  response-driven DRC, allocation/queue/release and firmware acceptance remain
  unverified; no complete-format-lifecycle claim is made.

Each Iris build runs these four normal suites before compilation. Reports use
scope-specific flags, never `protocol_verified=true` or `hardware_ready=true`.

## Firmware static input audit

`../verify-firmware-runtime.py` now requires a pinned actual DTB and a pinned
runtime input specification, in addition to the immutable firmware manifest:

```sh
python3 -I -B ../verify-firmware-runtime.py --component video \
    --manifest "$FW_MANIFEST" --manifest-sha256 "$FW_SHA" \
    --dtb "$DTB" --dtb-sha256 "$DTB_SHA" \
    --inputs "$RUNTIME_INPUTS" --inputs-sha256 "$INPUTS_SHA"
```

The private input spec has `schema=1`, `component`, and `files` records with
`path`/`sha256`. Video requires roles `driver`, `platform_header`, `hfi_header`;
`loader` is optional and otherwise explicitly unknown. CDSP does not establish
any driver/model-runtime pairing. Blank protocol input and absent mandatory
HFI definitions are refused. Firmware-required segments come from ELF, not
`complete`/`expected_segments`; metadata/split/full-MBN data and actual DT
carveout bounds are checked using the strict validator.

Real fixed video (14 files) and CDSP (19 files) materials still pass static
consistency. Both report `static_pairing_consistent=false`,
`protocol_abi_verified=false`, `signature_verified=false`,
`runtime_pairing_verified=false`, `loader_provenance_status=unknown`.
Firmware version/format/shell strings are textual evidence only. This tool does
not parse authenticated certificate trust or prove that the present device's
SCM secure loader accepts a firmware image. No authentication bypass exists.

## Hardware stop conditions

Complete ABI/tool/header provenance, firmware trust/runtime contract,
packet-id/session lifetime, full PM/DMA lifecycle and native media selection
remain gates. Firmware, actual DTBs, private manifests, images and raw logs stay
in `private/`. A current complete 192MiB boot_b backup has not been verified;
a matching boot-image prefix is not a full partition backup. RAM booting a
production read-write image is not an isolated rescue environment. Phone tests
require separate authorization, reliable battery and a verified rollback.
