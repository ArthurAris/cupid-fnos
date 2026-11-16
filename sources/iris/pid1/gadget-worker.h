/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef USB_GADGET_WORKER_H
#define USB_GADGET_WORKER_H
#include <stdint.h>
struct gw_result { int32_t status, error; uint32_t bound, local_ready, operation_index; };
int gw_setup(struct gw_result *, int64_t absolute_deadline_ns);
#ifdef GW_TESTING
struct gw_io {
    int (*operation)(const char *kind, const char *path, const char *value, void *context);
    int64_t (*now)(void *context);
    void *context;
};
int gw_setup_test(struct gw_result *, int64_t, const struct gw_io *);
#endif
#endif
