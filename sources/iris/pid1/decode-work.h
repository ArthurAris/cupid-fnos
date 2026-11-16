/* SPDX-License-Identifier: GPL-2.0-only */
/* PRIVATE successor interface. Producer adapter intentionally pending main. */
#ifndef DECODE_WORK_H
#define DECODE_WORK_H
#include "fixed-decode-evidence.h"
#include "module-supervisor.h"
#include <sys/types.h>
#define DC_PHASE_NS INT64_C(60000000000)
#define DC_COMPLETION_NS INT64_C(45000000000)
#define DC_READY_FRESH_NS INT64_C(1000000000)
/* This is an in-process producer contract, NOT the existing schema2 sysfs.
 * Main's new decode_once_status schema1 adapter must supply exact observed
 * values. No parser is guessed here and no default-success producer exists.
 * No host/tty deserialization or conversion from vr2_snapshot is valid. */
struct dc_ready {
 uint32_t schema; uint64_t attempt; int64_t observed_ns;
 int decode_enabled,retention_ready,prepared,post_bind;
 int run_claimed,run_done,run_errno,stage,stage_errno,error;
 int ack,core_ready,admission,claimed,terminal,dma_stop_known;
 int decoder_published; unsigned node_major,node_minor;
 int decoder_registered,encoder_registered;
};
#define DC_READY_MAX 1024u
/* Exact new decode_once_status four-line schema1 only; out unchanged on fail.
 * read() is fixed-path, read-only and may wait on kernel core lock: invoke in
 * supervised decode owner/worker, never synchronous unbounded PID1 context. */
int dc_ready_parse(const char *,size_t,uint64_t attempt,int64_t observed_ns,struct dc_ready *);
int dc_ready_read(uint64_t attempt,struct dc_ready *);
struct dc_state {
 int attempted,launched,exec_confirmed,export_attempted,exported,error;
 int fde_tx_attempted;
 pid_t client_pid; int client_pidfd;
 uint64_t attempt;int64_t deadline_ns,completion_deadline_ns;struct fde_record bindings;
};
struct dc_export {
 int rc,worker_reaped,worker_unresolved,client_retained;
 int tx_started,completion_rc;
 size_t bytes;pid_t worker_pid;
 /* No hardware-proof boolean: success is bounded content export only. */
};
/* Return zero only for exact live ACK ready state. Never schema2 terminal. */
int dc_ready_validate(const struct dc_ready *,uint64_t attempt,int64_t now_ns);
/* One-shot gate: verifies fixed ms_record assets and fexecve SAME verified fd.
 * Same result_ns+60s budget; fixed argv[15], empty env; tty never inherited by
 * client. Caller single-threaded, owns SIGCHLD and keeps state alive.
 * On error/timeout client may be alive: state PID/pidfd retained, NEVER killed.
 * Must only run in dedicated persistent decode owner; not old rp_observe.
 */
int dc_start(struct dc_state *,const struct dc_ready *,const struct fde_record *,int64_t result_ns);
/* Poll fixed stdout and exact terminal HOLD line while client still alive.
 * Short-lived proof worker reads/prepares; parent receives bounded blob and
 * reaps only that worker, then writes FDE over the existing tty FD.
 * Never waits for client exit or calls kernel terminal/STOP/unload. Caller
 * may supply an independently verified kernel terminal action outside API.
 * No tty reopen/read/config; fd identity/exclusivity remains main's contract.
 */
int dc_export_same_fd(struct dc_state *,int tty,const struct fde_record *,struct dc_export *);
#ifdef DC_TESTING
int dc_test_completion(int dirfd);
int dc_test_spawn(int verified_elf_fd,int result_fd,int stderr_fd,pid_t *,int *);
#endif
#endif
