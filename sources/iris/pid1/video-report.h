/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef VIDEO_REPORT_H
#define VIDEO_REPORT_H

#include <stddef.h>

#define VR_SNAPSHOT_MAX 512
#define VR_LINE_MAX 128
#define VR_STATUS_COUNT 9
#define VR_SYSFS_PATH "/sys/devices/platform/soc@0/aa00000.video-codec/init_only_status"

struct vr_snapshot {
    int execution_enabled, stage, stage_errno, error;
    int ack, terminal_cpu_drain, terminal_errno, dma_stop_known;
    int queues, power, fw_touched, fw_may_run, fw_booted;
    int status[VR_STATUS_COUNT];
};

/* Local kernel snapshot only; the USB host cannot supply paths or commands. */
int vr_parse(const char *data, size_t length, struct vr_snapshot *out);
int vr_read(struct vr_snapshot *out);
/* video: 1 ACK+CPU-terminal success, 2 init failed, 3 deadline, 4 not started.
 * dma_stop is always unknown; no observer or successful syscall proves DMA stop.
 */
int vr_format(char *line, size_t capacity, int video, int rc,
              int error_no, unsigned elapsed_ms);
int vr_snapshot_line(const struct vr_snapshot *snapshot, unsigned elapsed_ms,
                     char *line, size_t capacity);

#endif
