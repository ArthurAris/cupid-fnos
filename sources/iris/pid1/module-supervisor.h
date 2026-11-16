/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef MODULE_SUPERVISOR_H
#define MODULE_SUPERVISOR_H
#include <stdint.h>
#include <sys/types.h>
#define MS_MAX_BYTES (16u * 1024u * 1024u)
struct ms_record {
    char name[64], ram_path[256];
    uint64_t size;
    unsigned char sha256[32];
    char fixedoptions[256];
};
enum ms_kind { MS_OK, MS_ERROR, MS_TIMEOUT, MS_PROTOCOL, MS_CHILD, MS_CLOCK };
struct ms_outcome {
    enum ms_kind kind;
    pid_t pid;
    int error, timeout, term_signal, after_child_incomplete;
};
struct ms_state { int stopped; struct ms_outcome last; };
/* Main owns the immutable allowlist, stage/protocol and absolute MONOTONIC
 * total deadline. One call attempts ONE record; failure permanently latches
 * this state. No progress/diagnostic callbacks, retries or stage advancement.
 * load has syscall semantics (0 or -1/errno) and runs ONLY in the child.
 * clock returns 0 plus monotonic ns or -1/errno; NULL uses clock_gettime.
 * Nonzero after_child_incomplete means exit/reap was NOT confirmed: main must
 * remain stopped even if SIGKILL was sent. A fresh state is NOT a retry policy.
 * Caller must own SIGCHLD (no SA_NOCLDWAIT/reaper), keep state/records stable,
 * and fork from a single-threaded process without unsafe atfork handlers.
 */
typedef int (*ms_load_fn)(int, const char *, void *);
typedef int (*ms_clock_fn)(void *, int64_t *);
struct ms_outcome ms_run(struct ms_state *, const struct ms_record *, uint32_t,
                         int64_t, ms_load_fn, ms_clock_fn, void *);
/* Only known inherited tty and observer IPC are closed in the module child. */
struct ms_outcome ms_run_owned(struct ms_state *, const struct ms_record *, uint32_t,
 int64_t, ms_load_fn, ms_clock_fn, void *, int tty, int observer_ipc);
#ifndef MS_NO_PRODUCTION
/* Explicit production entry point; host tests exclude this symbol entirely. */
int ms_finit_module(int fd, const char *fixedoptions, void *unused);
#endif
#endif
