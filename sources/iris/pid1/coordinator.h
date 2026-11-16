/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef USB_RAM_COORDINATOR_H
#define USB_RAM_COORDINATOR_H

#include <stddef.h>
#include <stdint.h>
#include <stdatomic.h>
#include "module-supervisor.h"
#include "transport-state.h"

#define UC_MAX_RECORDS 64u
/* Mount, gadget and serial also publish immutable BEGIN/FINISH pairs. */
#define UC_MAX_MODULES (UC_MAX_RECORDS / 2u - 3u)
#define UC_MODULE_MS 20000u
#define UC_STEP_MS 20000u
#define UC_SERIAL_MS 8000u
#define UC_TOTAL_MS 120000u
#define UC_SERIAL_OK 0

/* Existing serial adapter API: status/error == 0 and EACH protocol flag == 1,
 * callback return 0, exact fixed frame, EOF and normally reaped exit 0.
 * This is not a claim of RESULT delivery or protocol authentication. */
struct uc_serial_result {
    int32_t status, error;
    uint32_t challenge_written, ack_validated, result_written;
};
typedef int (*uc_serial_fn)(struct uc_serial_result *, void *);
/* Step success fields are NOT serial protocol flags. Both must equal 1;
 * status/error and callback return must be 0. error is nonnegative errno-style. */
struct uc_step_result {
    int32_t status, error;
    uint32_t complete, verified;
};
typedef int (*uc_step_fn)(struct uc_step_result *, int64_t deadline_ns, void *);
struct uc_hooks { uc_step_fn mount, gadget; };
/* Trailing step_ms == 0 uses UC_STEP_MS; nonzero may only shorten it. */
struct uc_limits { uint32_t module_ms, serial_ms, total_ms, step_ms; };

enum uc_stage { UC_MODULE, UC_SERIAL, UC_API_MOUNT, UC_GADGET };
enum uc_event { UC_BEGIN, UC_FINISH };
enum uc_result {
    UC_PENDING, UC_OK, UC_ERROR, UC_TIMEOUT, UC_PROTOCOL,
    UC_CHILD, UC_CLOCK, UC_CANCELLED
};
struct uc_record {
    enum uc_stage stage;
    enum uc_event event;
    enum uc_result result;
    uint32_t module_index; /* UINT32_MAX for any non-module stage */
    int error;
    int64_t begin_ns, finish_ns, deadline_ns;
    struct ms_outcome module;
    struct uc_serial_result serial;
    pid_t serial_pid;
    int serial_signal, serial_after_child_incomplete;
    struct uc_step_result step;
    pid_t step_pid;
    int step_signal, step_after_child_incomplete;
};
struct uc_shared {
    atomic_uint terminal; /* enum ts_terminal; ACTIVE -> terminal only */
    atomic_uint count;
    struct uc_record record[UC_MAX_RECORDS];
};
struct uc_trace { struct uc_shared shared; };

/* Call once BEFORE observation; cross-process trace must be MAP_SHARED. */
int uc_init(struct uc_trace *);
/* Observers acquire count, copy ONLY record[0..count), never write records/count
 * or timing. Their only permitted write is ACTIVE -> FAILURE/STALE via latch.
 * Main is the sole record writer; COMPLETE is reserved for the coordinator. */
unsigned uc_latch(struct uc_trace *, enum ts_terminal);
/* Legacy modules -> serial flow and serial types retained for existing callers.
 * limits == NULL uses 20s/module, 8s/serial, 120s total. */
unsigned uc_run(struct uc_trace *, const struct ms_record *, size_t,
                ms_load_fn, uc_serial_fn, void *, const struct uc_limits *);
/* One-shot v2: count starts at 0, mount -> modules -> gadget -> serial, at most
 * 29 modules (16 modules produce 38 records). Both hooks are required exactly
 * once; a failed/stale stage stops all later work. No discovery, logging, exec,
 * reboot, retry, HOLD or device policy is provided here. Main supplies verified
 * immutable allowlist and production loader; fake hooks/loaders are host-only.
 * All callbacks run ONLY in supervised children sharing the parent's mount
 * namespace: NO unshare, NO descendants, NO trace writes. Mount success uses
 * only step success fields, never serial protocol flags. Hooks receive the
 * fixed absolute deadline, serial retains its old adapter API and gets no pipe
 * fd. Own SIGCHLD (no external reaper/SA_NOCLDWAIT), fork single-threaded without
 * unsafe atfork handlers; inherited callback fds/context must stay fork-stable.
 * Positive module/serial/total limits may ONLY shorten the existing ceilings;
 * step_ms defaults to 20s. No global extension of the 120s total ceiling.
 * Absolute CLOCK_MONOTONIC deadlines include fork/hash/load/reap/protocol work;
 * no heartbeat publications or deadline resets. Success requires exact fixed
 * frame, EOF and normally reaped exit 0, followed by the final deadline check.
 * STALE/FAILURE never become COMPLETE. after_child_incomplete means kill/reap
 * was NOT confirmed: caller retains failure HOLD and owns later cleanup, never
 * resumes flow. Returns sticky enum ts_terminal, never ACTIVE for a valid run. */
unsigned uc_run_v2(struct uc_trace *, const struct ms_record *, size_t,
                   ms_load_fn, uc_serial_fn, void *, const struct uc_limits *,
                   const struct uc_hooks *);

#endif
