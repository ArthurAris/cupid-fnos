/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef VIDEO_REPORT_V2_H
#define VIDEO_REPORT_V2_H
#include <stddef.h>
#define VR2_SNAPSHOT_MAX 640u
#define VR2_STATUS_COUNT 9u
struct vr2_snapshot {
    int execution_enabled, stage, stage_errno, error;
    int prepared, post_bind, run_claimed, run_done, run_errno, provider_attempted;
    int ack, terminal_cpu_drain, terminal_errno, dma_stop_known;
    int queues, power, fw_touched, fw_may_run, fw_booted;
    int status[VR2_STATUS_COUNT];
};
/* Strict exact schema2 snapshot. No sysfs reads, loader, commands or device I/O.
 * Out is unchanged on failure. Provider-admission gate is absent from this wire;
 * neither execution_enabled nor provider_attempted proves admitted providers.
 */
int vr2_parse(const char *data, size_t length, struct vr2_snapshot *out);
/* Uses the unchanged schema1 module's generic vr_format, not vr_snapshot_line.
 * PREPARE/inflight => video4/-125; closed completed RUN => video4/-95;
 * attempted failure => video2/original rc; fully consistent numeric init =>video1.
 * DMA stop remains unknown (0); no isolated-owner/provider/physical proof.
 */
int vr2_snapshot_line(const struct vr2_snapshot *snapshot, unsigned elapsed_ms,
                      char *line, size_t capacity);
#endif
