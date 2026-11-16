/* SPDX-License-Identifier: GPL-2.0-only */
/* PRIVATE finite metadata owner; no hardware assertion and no production pins. */
#ifndef DECODE_OWNER_H
#define DECODE_OWNER_H
#include "decode-pipeline.h"
#include <stdint.h>
#include <sys/types.h>
#ifndef DCO_PRODUCTION_PINNED
#define DCO_PRODUCTION_PINNED 1
#endif
#define DCO_PHASE_NS INT64_C(60000000000)
#define DCO_ISSUE_LOADER_TIMEOUT 1u
#define DCO_ISSUE_LOADER_UNRESOLVED 2u
#define DCO_ISSUE_READINESS_UNRESOLVED 4u
#define DCO_ISSUE_EXPORT_UNRESOLVED 8u
#define DCO_ISSUE_CLIENT_UNCONFIRMED 16u
#define DCO_ISSUES_ALL 31u
/* Zero-initialize once. No reset/retry/close/cleanup API: after any fork the
 * owner identity and control FD remain held until external physical reset.
 * Caller owns SIGCHLD; NEVER reap/signal this PID/group in generic init cleanup.
 * Do not copy this state into an independently active owner. */
struct dco_state {
 int attempted, control_fd;
 pid_t owner_pid;
};
struct dco_result {
 int rc, attempted, group_verified, go_sent, done_received;
 int timed_out, owner_live, owner_exited, identity_retained, unresolved;
 int client_retained;
 uint32_t inner_issues;
 pid_t owner_pid, client_pid;
 uint64_t evidence_bytes;
 int64_t deadline_ns;
 /* No hardware proof / DMA stopped / client stopped claim. */
};
/* PID1 metadata-only entry AFTER normal USB COMPLETE, confirmed flow reap and
 * hf_receive_metadata_once. Original tty FD remains owned/open by caller.
 * Child verifies setsid -> typed READY -> parent checks live session/group ->
 * GO -> dc_run_after_result -> one numeric DONE -> indefinite HOLD.
 * Absolute RESULT+60s includes handshake/work; no EOF required after DONE.
 * Every outcome after fork retains leader/control FD, even timeout/protocol
 * failure. Timeout bounds parent observation, NOT blocked kernel IO/CPU/DMA.
 * Inner FDE writer cooperatively checks the same absolute deadline. Only child
 * touches tty. No fork when records absent or gate defaults to 0. Production
 * records must originate in reviewed compiled candidate, never wire/host data.
 */
int dco_run(struct dco_state *, int tty, const struct hf_receipt *,
 const struct ms_record *runner, const struct fde_record *, struct dco_result *);
#ifdef DC_OWNER_TESTING
/* Offline fork/process/packet seam only. No production loader is linked or
 * reachable. Work receives exactly the production child/session/GO context.
 * Fake counts below are numeric metadata, never hardware/frame verification. */
typedef int (*dco_test_work_fn)(int, const struct hf_receipt *,
 struct dc_pipeline *, struct dc_pipeline_result *, void *);
enum dco_test_fault { DCO_TEST_NORMAL, DCO_TEST_BAD_READY, DCO_TEST_BAD_DONE,
 DCO_TEST_DUP_DONE, DCO_TEST_EXIT_BEFORE_DONE, DCO_TEST_NO_READY };
struct dco_test {
 dco_test_work_fn work;
 void *context;
 int64_t budget_ns; /* >0, <=60s; only shortens RESULT-relative deadline */
 enum dco_test_fault fault;
};
int dco_run_test(struct dco_state *, int tty, const struct hf_receipt *,
 const struct dco_test *, struct dco_result *);
#endif
#endif
