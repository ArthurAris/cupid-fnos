/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef VIDEO_PHASE_H
#define VIDEO_PHASE_H
#include "serial-protocol.h"
#include "video-report.h"
#include "video-report-v2.h"
#include <sys/types.h>

/* Userspace report ingestion only; never changes kernel admission gates. */
#ifndef IRIS_ALLOW_EXECUTION_REPORTS
#define IRIS_ALLOW_EXECUTION_REPORTS 1
#endif
#if IRIS_ALLOW_EXECUTION_REPORTS != 0 && IRIS_ALLOW_EXECUTION_REPORTS != 1
#error IRIS_ALLOW_EXECUTION_REPORTS_must_be_0_or_1
#endif

struct vp_session { int attempted; int64_t result_ns, terminal_deadline_ns; };
#define VP_TERMINAL_NS INT64_C(11000000000)
int vp_session_bind(struct vp_session *, int64_t result_ns);
/* Observer config before phase fork; snapshot child closes only this known IPC. */
void vp_observer_child_close_fd(int fd);
struct vp_result {
    int admitted, snapshot_error, tx_error, child_reaped, child_unresolved;
    pid_t child_pid;
    int video, rc, error_no;
    unsigned elapsed_ms;
};
/* Borrow the same verified nonblocking tty fd after SP_OK and RESULT written.
 * No open/reopen, fd ownership change, retry, loader, cleanup or DMA-stop claim.
 * This strict schema2 integration reads one snapshot in a supervised child <=1s,
 * then emits one <=128-byte DIAG2 in <=1s. Execution reports default closed;
 * enabling ingestion does not start a module or change driver admission.
 */
int vp_after_result(struct vp_session *session, int fd, enum sp_status status, const struct sp_stats *stats,
                    struct vp_result *result);
/* Internal same-child supervised read adapter for the postRESULT loader owner. */
typedef int (*vp_snapshot_fn)(struct vr2_snapshot *, void *);
int vp_after_result_read(struct vp_session *,int,enum sp_status,const struct sp_stats *,
                         vp_snapshot_fn,void *,struct vp_result *);
#ifdef VP_TESTING
/* Explicit offline-only seam for local socketpair/raw-PTY snapshot fixtures. */
int vp_after_result_test(struct vp_session *session, int fd, enum sp_status status, const struct sp_stats *stats,
                         vp_snapshot_fn snapshot, void *context,
                         struct vp_result *result);
#endif
#endif
