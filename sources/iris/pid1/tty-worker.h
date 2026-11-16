/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef TTY_WORKER_H
#define TTY_WORKER_H

#include "serial-protocol.h"

/* Include this header in PID1, but call the worker ONLY in its disposable child.
 * PID1 must supervise the child externally and kill/reap it by an absolute
 * CLOCK_MONOTONIC deadline <= 8 seconds: open/realpath/read/termios/close can
 * block inside the kernel despite O_NONBLOCK. The cooperative worker deadline
 * is NOT a substitute for supervision. No alarm/signal handler is installed.
 * The fd-only protocol retains its independent, unchanged 6-second ceiling.
 *
 * Production roots are fixed /sys and /dev (NULL selects these defaults;
 * explicit identical strings are also accepted). No environment/path override.
 * Caller guarantees a private mount namespace, a genuine sysfs mount at /sys,
 * and a fresh, dedicated RAM-backed tmpfs at /dev, mounted/nodev NOT set. The
 * worker rejects non-sysfs/non-tmpfs roots but cannot prove their provenance,
 * isolation, mount ancestry or lack of hostile concurrent mount/path mutation.
 * Parent must establish and protect those invariants before forking. /dev must
 * contain no preexisting ttyGS0: there is no adoption, replacement or unlink.
 * The successful node remains on RAM tmpfs; the child always closes its fds.
 * Sysfs class links are permitted ONLY when realpath resolves the whole tty
 * directory below /sys/devices/. Directory traversal is pinned with nofollow
 * directory fds from /; the sole writable basename is ttyGS0, character 0600.
 * No block operations, shell, exec, network, scanning or device selection.
 * Success means protocol RESULT fully written, not delivery or authentication.
 */
#define TW_WORKER_NS INT64_C(8000000000)

typedef struct sp_io sp_io;
enum tw_status {
    TW_OK = 0, TW_INVALID, TW_NOT_CHILD, TW_CLOCK, TW_TIMEOUT,
    TW_PARENT_PATH, TW_PROVENANCE, TW_CLASS_PATH, TW_METADATA,
    TW_NODE_CREATE, TW_NODE_OPEN, TW_NODE_VERIFY, TW_TERMIOS, TW_PROTOCOL,
    TW_CLOSE
};
typedef struct tw_result {
    enum tw_status status;
    enum sp_status protocol_status; /* SP_INVALID until sp_worker is entered */
    struct sp_stats stats;
    int error_no;                  /* saved syscall errno, 0 for logical errors */
} tw_result;

enum tw_status tw_exchange(const char *sysfsroot, const char *ramdevdir,
                           const sp_io *test_io, tw_result *result);
/* On success only, transfer one validated still-open tty; caller must send/close
 * it within the coordinator original 8s budget. No path reopen or tty I/O. */
enum tw_status tw_exchange_keep(tw_result *, int *fd, int64_t *result_ns);
const char *tw_status_name(enum tw_status status);
/* Legacy reopen diagnostic deliberately absent in this overlay. */

#ifdef TW_TESTING
/* Explicit offline seam, absent from production builds. Callback must create
 * and open ONLY a mock node/local PTY and return a newly owned fd; the worker
 * still validates fstat/rdev, flags, raw termios and runs the real sp_worker.
 * Mock metadata supplies the actual PTY major:minor, never a real ttyGS0.
 * Test roots must be absolute pinned paths; filesystem provenance is skipped
 * ONLY by this entrypoint, and its class link must resolve below root/devices/.
 */
struct tw_test_ops {
    void *context;
    int (*node_open_fn)(void *, int, const char *, int, mode_t, dev_t);
};
enum tw_status tw_exchange_keep_test(const char *, const char *, const sp_io *,
                                    const struct tw_test_ops *, tw_result *, int *, int64_t *);
enum tw_status tw_exchange_test(const char *sysfsroot, const char *ramdevdir,
                                const sp_io *test_io,
                                const struct tw_test_ops *ops,
                                tw_result *result);
#endif
#endif
