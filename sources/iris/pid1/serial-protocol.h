/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef SERIAL_PROTOCOL_H
#define SERIAL_PROTOCOL_H

#include <poll.h>
#include <stddef.h>
#include <stdint.h>
#include <sys/types.h>

/* v2, offline fd-only, one owner, already-open O_NONBLOCK descriptors:
 * device READY 2 1\n
 * host   CHALLENGE 2 1 <32 lowercase hex>\n
 * device RESPONSE 2 1 <same>\n
 * host   ACK 2 1 <same>\n
 * device RESULT 2 1 <same> OK\n
 * One attempt, 6000ms absolute CLOCK_MONOTONIC including startup/all I/O.
 * No device entropy, seed, fallback, shell, opens or commands. Nonce is host
 * correlation, NOT authentication. Exact frames/stages only; early input,
 * duplicate frames and queued tails fail. Success is complete RESULT write
 * plus an immediate empty-input check, not delivery or future silence.
 * RX/TX totals <=1024, received frames <=128; no retries/deadline reset.
 * Caller owns fd lifetime; same duplex fd, pipes and raw PTY are supported.
 */
#define SP_MAX_ATTEMPTS 1u
#define SP_MAX_FRAMES 128u
#define SP_MAX_RX 1024u
#define SP_MAX_TX 1024u
#define SP_EXCHANGE_NS INT64_C(6000000000)

enum sp_status {
    SP_OK = 0, SP_INVALID, SP_NONBLOCK, SP_CLOCK, SP_NONCE_FAILURE,
    SP_ABSENT, SP_TIMEOUT, SP_STALE, SP_VERSION, SP_NONCE_MISMATCH,
    SP_MALFORMED, SP_OVERFLOW, SP_EARLY, SP_HUP, SP_EOF,
    SP_WRITE_ZERO, SP_IO
};

struct sp_stats {
    size_t rx_bytes, tx_bytes;
    unsigned int attempts, frames, nonce_calls; /* v2 nonce_calls always zero */
    /* Compatibility field: v2 means the entire RESPONSE (not CHALLENGE) was
     * written. Keep layout/names for the existing coordinator. */
    int challenge_written, ack_validated, result_written;
};

/* NULL selects production clock/poll/read/SIGPIPE-safe write. A non-NULL
 * test_io must provide the first four hooks with syscall/errno semantics;
 * random_fn is retained for ABI compatibility, unused and may be NULL.
 * Hooks must not block beyond poll timeout; clock must be nondecreasing.
 */
struct sp_io {
    void *context;
    ssize_t (*read_fn)(void *, int, void *, size_t);
    ssize_t (*write_fn)(void *, int, const void *, size_t);
    int (*poll_fn)(void *, struct pollfd *, nfds_t, int);
    int (*clock_fn)(void *, int64_t *);
    ssize_t (*random_fn)(void *, void *, size_t, unsigned int);
};

enum sp_status sp_worker_stamped(int rx_fd, int tx_fd, const struct sp_io *test_io,
                                 struct sp_stats *stats, int64_t *result_ns);
enum sp_status sp_worker(int rx_fd, int tx_fd, const struct sp_io *test_io,
                         struct sp_stats *stats);
const char *sp_status_name(enum sp_status status);
#endif
