/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef FIXED_DECODE_DIAGNOSTIC_H
#define FIXED_DECODE_DIAGNOSTIC_H
#include <stddef.h>
#include <stdint.h>
#define FDG_HEADER 512u
#define FDG_RESULT_MAX 1024u
#define FDG_TRACE_MAX 4096u
#define FDG_STDERR_MAX 1024u
#define FDG_MAX (FDG_HEADER+FDG_RESULT_MAX+FDG_TRACE_MAX+FDG_STDERR_MAX+32u)
struct dc_pipeline;
struct dc_pipeline_result;
struct fde_record;
struct fdg_outcome {int rc,attempted,worker_reaped,worker_unresolved,tx_started;size_t bytes;};
/* Failure-only diagnostic, never hardware/frame proof. Call only in verified
 * persistent owner after receipt verification, once, before ANY FDE write call.
 * Child has no tty/control descriptors and a <=1s absolute preparation budget.
 * Owner writes only its complete, reaped child's blob on SAME nonblocking fd,
 * checking original RESULT+60 deadline before each write. No resource release. */
int fdg_emit_same_fd(int tty,int observer_ipc,int64_t result_ns,
 const struct fde_record *,struct dc_pipeline *,const struct dc_pipeline_result *,struct fdg_outcome *);
#endif
