/* SPDX-License-Identifier: GPL-2.0-only */
/* PRIVATE offline FDE1 component. No device admission or hardware assertion. */
#ifndef FIXED_DECODE_EVIDENCE_H
#define FIXED_DECODE_EVIDENCE_H
#include <stddef.h>
#include <stdint.h>
#define FDE_HEADER 288u
#define FDE_PAYLOAD 73728u
#define FDE_FRAME 24576u
#define FDE_TRACE_MAX 49152u
#define FDE_MAX (FDE_HEADER + FDE_TRACE_MAX + 3u*16u + FDE_PAYLOAD + 32u)
#define FDE_PARENT_MAX 131072u
/* Ordered SHA256: client source, executed client ELF, kernel Image, FIR1,
 * FNV1 oracle, final immutable attempt record (record does not hash itself).
 * No paths/options/commands from the wire. Record supplied by future PID1. */
struct fde_record { uint64_t attempt; unsigned char binding[6][32]; };
struct fde_blob { unsigned char bytes[FDE_MAX]; size_t size; };
/* Call only after client's complete HOLD marker is fsynced, while it retains
 * its video buffers. Reads fixed /run/iris-decode-evidence only, no stdin.
 * Caller owns RAM blob; return errno value, size remains zero on failure. */
int fde_prepare(const struct fde_record *, struct fde_blob *);
/* SAME inherited open nonblocking fd; no close/open/read/termios/commands.
 * Absolute CLOCK_MONOTONIC deadline <=60s from call, never refreshed.
 * Partial/error export is terminal; receiver must reject and never resume. */
int fde_write_same_fd(int, const struct fde_blob *, int64_t deadline_ns);
#ifdef FDE_TESTING
/* Synthetic fixture seam only; absent from production object. */
int fde_test_prepare(int dirfd, const struct fde_record *, struct fde_blob *);
#endif
#endif
