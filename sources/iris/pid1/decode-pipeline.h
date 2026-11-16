/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef DECODE_PIPELINE_H
#define DECODE_PIPELINE_H
#include "decode-work.h"
#include "fd-handoff.h"
#include "fixed-decode-diagnostic.h"
enum dc_pipeline_stage { DC_STAGE_GUARD=1,DC_STAGE_LOAD,DC_STAGE_READY,DC_STAGE_START,DC_STAGE_EXPORT };
struct dc_pipeline {int attempted,diagnostic_attempted;struct dc_state client;};
struct dc_pipeline_result {int rc,stage,readiness_rc;pid_t readiness_pid;int readiness_reaped,readiness_unresolved;struct ms_outcome loader;struct dc_ready ready;struct dc_export exported;struct fdg_outcome diagnostic;};
/* Concrete post-RESULT path, only in main's verified persistent decode leader.
 * Caller established READY/GO/session ownership and dedicated decode guard.
 * Loader gets fixed name, immutable pinned record, no options. Caller retains
 * leader/group/HOLD process after return on ALL outcomes; no old observer
 * group cleanup, no terminal request, no unload, no tty close by this API.
 */
int dc_run_after_result(int tty,const struct hf_receipt *,const struct ms_record *runner,
 const struct fde_record *,int observer_ipc,struct dc_pipeline *,struct dc_pipeline_result *);
#endif
