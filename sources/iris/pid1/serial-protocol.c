/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "serial-protocol.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

static ssize_t real_read(void *p, int fd, void *buf, size_t n)
{ (void)p; return read(fd, buf, n); }

/* Consume only our newly generated SIGPIPE; do not change caller disposition. */
static ssize_t real_write(void *p, int fd, const void *buf, size_t n)
{
    sigset_t set, old, pending;
    struct timespec zero = {0, 0};
    int rc, had_pending, saved;
    ssize_t ret;
    (void)p;
    sigemptyset(&set);
    sigaddset(&set, SIGPIPE);
    rc = pthread_sigmask(SIG_BLOCK, &set, &old);
    if (rc) { errno = rc; return -1; }
    if (sigpending(&pending) < 0) {
        saved = errno;
        (void)pthread_sigmask(SIG_SETMASK, &old, NULL);
        errno = saved;
        return -1;
    }
    had_pending = sigismember(&pending, SIGPIPE);
    ret = write(fd, buf, n);
    saved = errno;
    if (ret < 0 && saved == EPIPE && !had_pending) {
        while (sigtimedwait(&set, NULL, &zero) < 0 && errno == EINTR) {}
    }
    (void)pthread_sigmask(SIG_SETMASK, &old, NULL);
    errno = saved;
    return ret;
}
static int real_poll(void *p, struct pollfd *fds, nfds_t n, int ms)
{ (void)p; return poll(fds, n, ms); }
static int real_clock(void *p, int64_t *ns)
{
    struct timespec ts;
    (void)p;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0 || ts.tv_sec < 0 ||
        ts.tv_nsec < 0 || ts.tv_nsec >= 1000000000 ||
        ts.tv_sec > (INT64_MAX - ts.tv_nsec) / 1000000000) return -1;
    *ns = (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
    return 0;
}
static const struct sp_io production = {
    NULL, real_read, real_write, real_poll, real_clock, NULL
};
struct exchange {
    struct sp_stats stats;
    char nonce[33], response[64], result[64], rx[SP_MAX_RX];
    size_t used;
    int64_t result_ns;
};
static enum sp_status deadline(const struct sp_io *io, int64_t start,
                               int64_t *last, struct exchange *s, int *ms)
{
    int64_t now, remaining;
    if (io->clock_fn(io->context, &now) < 0 || now < *last) return SP_CLOCK;
    *last = now;
    if (now - start >= SP_EXCHANGE_NS)
        return s->stats.tx_bytes == 10 && !s->stats.rx_bytes ? SP_ABSENT : SP_TIMEOUT;
    remaining = SP_EXCHANGE_NS - (now - start);
    *ms = (int)((remaining + 999999) / 1000000);
    return SP_OK;
}
static enum sp_status parse(struct exchange *s, int challenge)
{
    size_t prefix = challenge ? 10 : 4, len = prefix + 37;
    const char *name = challenge ? "CHALLENGE " : "ACK ";
    if (s->used != len || memcmp(s->rx, name, prefix) ||
        s->rx[prefix + 1] != ' ' || s->rx[prefix + 3] != ' ' ||
        s->rx[len - 1] != '\n') return SP_MALFORMED;
    if (s->rx[prefix] != '2') return SP_VERSION;
    if (s->rx[prefix + 2] != '1') return SP_STALE;
    for (size_t i = prefix + 4; i < len - 1; ++i)
        if (!((s->rx[i] >= '0' && s->rx[i] <= '9') ||
              (s->rx[i] >= 'a' && s->rx[i] <= 'f'))) return SP_MALFORMED;
    if (challenge) memcpy(s->nonce, s->rx + prefix + 4, 32);
    else if (memcmp(s->nonce, s->rx + prefix + 4, 32)) return SP_NONCE_MISMATCH;
    return SP_OK;
}
/* Account each byte/frame once; per-stage buffer is independent of totals. */
static enum sp_status receive(const struct sp_io *io, int fd, struct exchange *s,
                               int *received)
{
    size_t room = SP_MAX_RX - s->stats.rx_bytes;
    ssize_t n;
    *received = 0;
    if (!room || s->used >= sizeof(s->rx)) return SP_OVERFLOW;
    n = io->read_fn(io->context, fd, s->rx + s->used, room);
    if (n < 0) {
        if (errno == EINTR) return SP_EARLY; /* internal retry sentinel */
        if (errno == EAGAIN || errno == EWOULDBLOCK) return SP_OK;
        return SP_IO;
    }
    if (!n) return SP_EOF;
    if ((size_t)n > room) return SP_OVERFLOW;
    *received = 1;
    s->stats.rx_bytes += (size_t)n;
    for (size_t i = s->used; i < s->used + (size_t)n; ++i)
        if (s->rx[i] == '\n') {
            if (s->stats.frames == SP_MAX_FRAMES) return SP_OVERFLOW;
            ++s->stats.frames;
        }
    s->used += (size_t)n;
    return SP_OK;
}
static enum sp_status run(int rxfd, int txfd, const struct sp_io *io,
                          struct exchange *s)
{
    int64_t start, last;
    size_t offset = 0, response_len = 0, result_len = 0;
    enum { READY, CHALLENGE, RESPONSE, ACK, RESULT, DONE } phase = READY;
    enum sp_status status;
    int flags, ms;
    if (!io->read_fn || !io->write_fn || !io->poll_fn || !io->clock_fn)
        return SP_INVALID;
    /* Capture the absolute deadline before fd validation/startup. */
    if (io->clock_fn(io->context, &start) < 0 || start < 0) return SP_CLOCK;
    last = start;
    s->stats.attempts = 1;
    flags = fcntl(rxfd, F_GETFL);
    if (flags < 0) return SP_INVALID;
    if (!(flags & O_NONBLOCK)) return SP_NONBLOCK;
    flags = fcntl(txfd, F_GETFL);
    if (flags < 0) return SP_INVALID;
    if (!(flags & O_NONBLOCK)) return SP_NONBLOCK;
    for (;;) {
        int reading = phase == CHALLENGE || phase == ACK, received;
        struct pollfd fds[2] = {
            {rxfd, POLLIN, 0}, {txfd, reading ? 0 : POLLOUT, 0}
        };
        status = deadline(io, start, &last, s, &ms);
        if (status != SP_OK) return status;
        int ready = io->poll_fn(io->context, fds, 2, phase == DONE ? 0 : ms);
        status = deadline(io, start, &last, s, &ms);
        if (status != SP_OK) return status;
        if (ready < 0) { if (errno == EINTR) continue; return SP_IO; }
        if ((fds[0].revents | fds[1].revents) & POLLNVAL) return SP_INVALID;
        if ((fds[0].revents | fds[1].revents) & POLLHUP) return SP_HUP;
        if ((fds[0].revents | fds[1].revents) & POLLERR) return SP_IO;
        /* POLLIN gates reads: raw TTY VMIN=VTIME=0 can return zero without
         * EOF when there is no input. RX still precedes every TX; DONE uses
         * the zero-time poll above to reject queued tails without empty reads. */
        if (fds[0].revents & POLLIN) {
            status = receive(io, rxfd, s, &received);
            if (status == SP_EARLY) continue; /* read EINTR, never write */
            if (status != SP_OK) return status;
            status = deadline(io, start, &last, s, &ms);
            if (status != SP_OK) return status;
            if (received) {
                if (!reading) return phase == READY || phase == RESPONSE ? SP_EARLY : SP_MALFORMED;
                size_t expected = phase == CHALLENGE ? 47 : 41;
                if (memchr(s->rx, '\n', s->used)) {
                    status = parse(s, phase == CHALLENGE);
                    if (status != SP_OK) return status;
                    s->used = 0; /* never accumulate CHALLENGE and ACK */
                    if (phase == CHALLENGE) {
                        response_len = (size_t)snprintf(s->response, sizeof(s->response),
                                                       "RESPONSE 2 1 %s\n", s->nonce);
                        result_len = (size_t)snprintf(s->result, sizeof(s->result),
                                                     "RESULT 2 1 %s OK\n", s->nonce);
                        if (response_len >= sizeof(s->response) || result_len >= sizeof(s->result))
                            return SP_OVERFLOW;
                        phase = RESPONSE;
                    } else { s->stats.ack_validated = 1; phase = RESULT; }
                    offset = 0;
                    continue; /* fresh RX check before next phase's first write */
                }
                if (s->used >= expected || s->stats.rx_bytes == SP_MAX_RX) return SP_OVERFLOW;
            }
        }
        if (phase == DONE) return SP_OK;
        if (!reading && (fds[1].revents & POLLOUT)) {
            const char *buf = phase == READY ? "READY 2 1\n" :
                              phase == RESPONSE ? s->response : s->result;
            size_t len = phase == READY ? 10 : phase == RESPONSE ? response_len : result_len;
            size_t remaining = len - offset;
            if (remaining > SP_MAX_TX - s->stats.tx_bytes) return SP_OVERFLOW;
            status = deadline(io, start, &last, s, &ms);
            if (status != SP_OK) return status;
            ssize_t n = io->write_fn(io->context, txfd, buf + offset, remaining);
            if (n < 0) {
                if (errno == EPIPE) return SP_HUP;
                if (errno != EINTR && errno != EAGAIN && errno != EWOULDBLOCK) return SP_IO;
            } else {
                if (!n) return SP_WRITE_ZERO;
                if ((size_t)n > remaining) return SP_OVERFLOW;
                offset += (size_t)n;
                s->stats.tx_bytes += (size_t)n;
            }
            status = deadline(io, start, &last, s, &ms);
            if (status != SP_OK) return status;
            if (offset == len) {
                offset = 0;
                if (phase == READY) phase = CHALLENGE;
                else if (phase == RESPONSE) {
                    s->stats.challenge_written = 1; /* v2: complete RESPONSE */
                    phase = ACK;
                } else { s->stats.result_written = 1; s->result_ns = last; phase = DONE; }
            }
        }
    }
}
enum sp_status sp_worker_stamped(int rx_fd, int tx_fd, const struct sp_io *test_io,
                                 struct sp_stats *stats, int64_t *result_ns)
{
    struct exchange s = {0};
    enum sp_status status = run(rx_fd, tx_fd, test_io ? test_io : &production, &s);
    if (stats) *stats = s.stats;
    if (result_ns) *result_ns = s.result_ns;
    explicit_bzero(&s, sizeof(s));
    return status;
}
enum sp_status sp_worker(int rx_fd, int tx_fd, const struct sp_io *test_io,
                         struct sp_stats *stats)
{ return sp_worker_stamped(rx_fd, tx_fd, test_io, stats, NULL); }
const char *sp_status_name(enum sp_status status)
{
    static const char *const names[] = {
        "ok", "invalid", "nonblock", "clock", "nonce-failure", "absent",
        "timeout", "stale", "version", "nonce-mismatch", "malformed",
        "overflow", "early", "hup", "eof", "write-zero", "io"
    };
    return (unsigned int)status < sizeof(names) / sizeof(names[0]) ? names[status] : "unknown";
}
