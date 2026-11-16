/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "coordinator.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

struct uc_wire {
    uint32_t magic;
    int32_t callback_result;
    struct uc_serial_result result;
};
struct uc_step_wire {
    uint32_t magic;
    int32_t callback_result;
    struct uc_step_result result;
};
_Static_assert(sizeof(struct uc_serial_result) == 20, "fixed serial result");
_Static_assert(sizeof(struct uc_wire) == 28, "fixed serial pipe frame");
_Static_assert(sizeof(struct uc_step_result) == 16, "fixed step result");
_Static_assert(sizeof(struct uc_step_wire) == 24, "fixed step pipe frame");
_Static_assert(sizeof(struct uc_wire) <= PIPE_BUF, "atomic pipe write");
_Static_assert(sizeof(struct uc_step_wire) <= PIPE_BUF, "atomic step pipe write");

static unsigned terminal(const struct uc_trace *t)
{
    return atomic_load_explicit(&t->shared.terminal, memory_order_acquire);
}
static unsigned latch(struct uc_trace *t, enum ts_terminal value)
{
    unsigned expected = TS_ACTIVE;
    atomic_compare_exchange_strong_explicit(&t->shared.terminal, &expected, value,
                                           memory_order_acq_rel, memory_order_acquire);
    return terminal(t);
}
unsigned uc_latch(struct uc_trace *t, enum ts_terminal value)
{
    if (!t) return TS_FAILURE;
    if (value != TS_FAILURE && value != TS_STALE) return terminal(t);
    return latch(t, value);
}
int uc_init(struct uc_trace *t)
{
    if (!t) return -EINVAL;
    memset(t, 0, sizeof(*t));
    atomic_init(&t->shared.terminal, TS_ACTIVE);
    atomic_init(&t->shared.count, 0);
    if (!atomic_is_lock_free(&t->shared.terminal) ||
        !atomic_is_lock_free(&t->shared.count)) {
        atomic_store_explicit(&t->shared.terminal, TS_FAILURE, memory_order_release);
        return -EINVAL;
    }
    return 0;
}
static int monotonic(int64_t *ns)
{
    struct timespec now;
    *ns = -1;
    if (clock_gettime(CLOCK_MONOTONIC, &now) < 0) return -1;
    if (now.tv_sec < 0 || now.tv_nsec < 0 || now.tv_nsec >= 1000000000 ||
        now.tv_sec > (INT64_MAX - now.tv_nsec) / 1000000000) {
        errno = EOVERFLOW;
        return -1;
    }
    *ns = (int64_t)now.tv_sec * 1000000000 + now.tv_nsec;
    return 0;
}
/* Local ts_state owns mutable timing. The observer owns no timing writes. */
static int check(struct uc_trace *t, struct ts_state *s, int64_t *now)
{
    unsigned value = terminal(t);
    int clock_result = monotonic(now);
    if (value != TS_ACTIVE) ts_latch(s, (enum ts_terminal)value);
    int ret = ts_check(s, clock_result, *now);
    value = atomic_load_explicit(&s->terminal, memory_order_acquire);
    if (value != TS_ACTIVE) (void)latch(t, (enum ts_terminal)value);
    /* Catch an observer CAS during our clock/check as well. */
    if (!ret && terminal(t) != TS_ACTIVE) ret = -ECANCELED;
    return ret;
}
static int publish(struct uc_trace *t, const struct uc_record *r)
{
    unsigned n = atomic_load_explicit(&t->shared.count, memory_order_relaxed);
    if (n >= UC_MAX_RECORDS) {
        (void)latch(t, TS_FAILURE);
        return -1;
    }
    t->shared.record[n] = *r;
    atomic_store_explicit(&t->shared.count, n + 1, memory_order_release);
    return 0;
}
static enum uc_result check_result(int ret)
{
    return ret == -ETIMEDOUT ? UC_TIMEOUT :
           ret == -ECANCELED ? UC_CANCELLED : UC_CLOCK;
}
static enum uc_result module_result(enum ms_kind kind)
{
    switch (kind) {
    case MS_OK: return UC_OK;
    case MS_ERROR: return UC_ERROR;
    case MS_TIMEOUT: return UC_TIMEOUT;
    case MS_PROTOCOL: return UC_PROTOCOL;
    case MS_CHILD: return UC_CHILD;
    case MS_CLOCK: return UC_CLOCK;
    }
    return UC_PROTOCOL;
}
static int stage_begin(struct uc_trace *t, struct ts_state *s,
                       struct uc_record *r, uint32_t milliseconds)
{
    int64_t now;
    int ret = check(t, s, &now);
    if (!ret) ret = ts_stage_begin(s, 0, now, (int64_t)milliseconds * 1000000);
    if (ret) {
        (void)latch(t, (enum ts_terminal)atomic_load_explicit(&s->terminal,
                                                            memory_order_acquire));
        return ret;
    }
    r->event = UC_BEGIN;
    r->result = UC_PENDING;
    r->begin_ns = now;
    r->finish_ns = -1;
    r->deadline_ns = s->stage_end_ns;
    return publish(t, r);
}
/* One supervisor for serial and generic steps. No namespace changes or forked
 * descendants; callback sees neither supervisor pipe fd nor mutable timing. */
static void supervised_run(struct uc_trace *t, struct ts_state *s,
                           struct uc_record *r, uc_serial_fn serial,
                           uc_step_fn step, void *ctx)
{
    int fds[2] = {-1, -1}, status = 0, reaped = 0;
    pid_t *pid = step ? &r->step_pid : &r->serial_pid;
    int *signal_no = step ? &r->step_signal : &r->serial_signal;
    int *incomplete = step ? &r->step_after_child_incomplete : &r->serial_after_child_incomplete;
    size_t frame_size = step ? sizeof(struct uc_step_wire) : sizeof(struct uc_wire);
    r->result = UC_ERROR;
    if (pipe2(fds, O_CLOEXEC | O_NONBLOCK) < 0) { r->error = errno; return; }
    *pid = fork();
    if (*pid < 0) { r->error = errno; *pid = 0; goto done; }
    if (!*pid) {
        struct uc_wire wire = { .magic = UINT32_C(0x55435631),
                               .result = { .status = -1 } };
        struct uc_step_wire step_wire = { .magic = UINT32_C(0x55435331),
                                         .result = { .status = -1 } };
        close(fds[0]);
        if (step) step_wire.callback_result = step(&step_wire.result, r->deadline_ns, ctx);
        else wire.callback_result = serial(&wire.result, ctx);
        const void *frame = step ? (const void *)&step_wire : (const void *)&wire;
        ssize_t n;
        do { n = write(fds[1], frame, frame_size); } while (n < 0 && errno == EINTR);
        close(fds[1]);
        _exit(n == (ssize_t)frame_size ? 0 : 112);
    }
    close(fds[1]);
    fds[1] = -1;
    for (;;) {
        int64_t now;
        int ret = check(t, s, &now);
        if (ret) { r->result = check_result(ret); r->error = -ret; break; }
        pid_t p = waitpid(*pid, &status, WNOHANG);
        if (p < 0 && errno != EINTR) { r->result = UC_CHILD; r->error = errno; break; }
        if (p == *pid) {
            reaped = 1;
            if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
                r->result = UC_CHILD;
                r->error = ECHILD;
                *signal_no = WIFSIGNALED(status) ? WTERMSIG(status) : 0;
                break;
            }
            unsigned char buf[sizeof(struct uc_wire) + 1], extra;
            struct uc_wire wire;
            ssize_t n;
            do { n = read(fds[0], buf, sizeof(buf)); } while (n < 0 && errno == EINTR);
            ssize_t eof;
            do { eof = read(fds[0], &extra, 1); } while (eof < 0 && errno == EINTR);
            if (n != (ssize_t)frame_size || eof != 0) {
                r->result = UC_PROTOCOL; r->error = EPROTO; break;
            }
            if (step) {
                struct uc_step_wire sw;
                memcpy(&sw, buf, sizeof(sw));
                if (sw.magic != UINT32_C(0x55435331) || sw.result.error < 0 ||
                    sw.result.status < 0 || sw.result.complete > 1 || sw.result.verified > 1) {
                    r->result = UC_PROTOCOL; r->error = EPROTO; break;
                }
                r->step = sw.result;
                if (sw.callback_result || sw.result.status || sw.result.error) {
                    r->result = UC_ERROR;
                    r->error = sw.result.error ? sw.result.error : EIO;
                } else if (sw.result.complete != 1 || sw.result.verified != 1) {
                    r->result = UC_PROTOCOL; r->error = EPROTO;
                } else { r->result = UC_OK; r->error = 0; }
                break;
            }
            memcpy(&wire, buf, sizeof(wire));
            if (wire.magic != UINT32_C(0x55435631) || wire.result.error < 0 ||
                wire.result.status < 0 || wire.result.challenge_written > 1 ||
                wire.result.ack_validated > 1 || wire.result.result_written > 1) {
                r->result = UC_PROTOCOL; r->error = EPROTO; break;
            }
            r->serial = wire.result;
            if (wire.callback_result || wire.result.status != UC_SERIAL_OK ||
                wire.result.error) {
                r->result = UC_ERROR;
                r->error = wire.result.error ? wire.result.error : EIO;
            } else if (wire.result.challenge_written != 1 ||
                       wire.result.ack_validated != 1 || wire.result.result_written != 1) {
                r->result = UC_PROTOCOL; r->error = EPROTO;
            } else { r->result = UC_OK; r->error = 0; }
            break;
        }
        int64_t remaining = r->deadline_ns - now;
        int ms = remaining >= 10000000 ? 10 : (int)((remaining + 999999) / 1000000);
        /* No readiness/heartbeat loop and no progress publication. */
        if (poll(NULL, 0, ms) < 0 && errno != EINTR) { r->error = errno; break; }
    }
    if (!reaped) {
        (void)kill(*pid, SIGKILL);
        if (waitpid(*pid, &status, WNOHANG) == *pid) reaped = 1;
        *incomplete = !reaped;
    }
done:
    if (fds[0] >= 0) close(fds[0]);
    if (fds[1] >= 0) close(fds[1]);
}
static int stage_finish(struct uc_trace *t, struct ts_state *s, struct uc_record *r)
{
    int64_t now;
    int ret = check(t, s, &now);
    r->event = UC_FINISH;
    r->finish_ns = now;
    if (ret && (ret != -ECANCELED || r->result == UC_OK)) {
        r->result = check_result(ret);
        r->error = -ret;
    }
    if (r->result == UC_OK) {
        ret = ts_stage_complete(s, 0, now);
        if (ret) { r->result = check_result(ret); r->error = -ret; }
    }
    if (r->result != UC_OK)
        (void)latch(t, r->result == UC_TIMEOUT ? TS_STALE : TS_FAILURE);
    if (publish(t, r)) return -1;
    return r->result == UC_OK && terminal(t) == TS_ACTIVE ? 0 : -1;
}
static int run_step(struct uc_trace *t, struct ts_state *s, enum uc_stage stage,
                    uint32_t milliseconds, uc_step_fn callback, void *ctx)
{
    struct uc_record r = { .stage = stage, .module_index = UINT32_MAX };
    if (stage_begin(t, s, &r, milliseconds)) return -1;
    supervised_run(t, s, &r, NULL, callback, ctx);
    return stage_finish(t, s, &r);
}
static unsigned run(struct uc_trace *t, const struct ms_record *records, size_t count,
                    ms_load_fn load, uc_serial_fn serial, void *ctx,
                    const struct uc_limits *limits, const struct uc_hooks *hooks,
                    int v2)
{
    const struct uc_limits defaults = {UC_MODULE_MS, UC_SERIAL_MS, UC_TOTAL_MS, UC_STEP_MS};
    struct ts_state state;
    int64_t now;
    if (!t) return TS_FAILURE;
    if (terminal(t) != TS_ACTIVE) return terminal(t);
    if (!limits) limits = &defaults;
    if ((count && (!records || !load)) || !serial || count > UC_MAX_MODULES ||
        (v2 && (!hooks || !hooks->mount || !hooks->gadget)) ||
        atomic_load_explicit(&t->shared.count, memory_order_acquire) != 0 ||
        !limits->module_ms || limits->module_ms > UC_MODULE_MS ||
        !limits->serial_ms || limits->serial_ms > UC_SERIAL_MS ||
        !limits->total_ms || limits->total_ms > UC_TOTAL_MS ||
        limits->step_ms > UC_STEP_MS || monotonic(&now))
        return latch(t, TS_FAILURE);
    if (ts_init(&state, now, (int64_t)limits->total_ms * 1000000))
        return latch(t, TS_FAILURE);
    uint32_t step_ms = limits->step_ms ? limits->step_ms : UC_STEP_MS;
    if (v2 && run_step(t, &state, UC_API_MOUNT, step_ms, hooks->mount, ctx))
        return terminal(t);
    struct ms_state modules = {0};
    for (size_t i = 0; i < count; ++i) {
        struct uc_record record = { .stage = UC_MODULE, .module_index = (uint32_t)i };
        if (stage_begin(t, &state, &record, limits->module_ms)) return terminal(t);
        /* Passing the recorded stage deadline also bounds hash/fork overhead. */
        record.module = ms_run(&modules, &records[i], limits->module_ms,
                               record.deadline_ns, load, NULL, ctx);
        record.result = module_result(record.module.kind);
        record.error = record.module.error;
        if (stage_finish(t, &state, &record)) return terminal(t);
    }
    if (v2 && run_step(t, &state, UC_GADGET, step_ms, hooks->gadget, ctx))
        return terminal(t);
    struct uc_record record = { .stage = UC_SERIAL, .module_index = UINT32_MAX };
    if (stage_begin(t, &state, &record, limits->serial_ms)) return terminal(t);
    supervised_run(t, &state, &record, serial, NULL, ctx);
    if (stage_finish(t, &state, &record)) return terminal(t);
    /* Final absolute-deadline check; observer CAS and COMPLETE compete only on
     * the shared terminal, so a late success can never replace STALE/FAILURE. */
    int ret = check(t, &state, &now);
    if (ret) return terminal(t);
    return latch(t, TS_COMPLETE);
}
unsigned uc_run(struct uc_trace *t, const struct ms_record *records, size_t count,
                ms_load_fn load, uc_serial_fn serial, void *ctx,
                const struct uc_limits *limits)
{
    return run(t, records, count, load, serial, ctx, limits, NULL, 0);
}
unsigned uc_run_v2(struct uc_trace *t, const struct ms_record *records, size_t count,
                   ms_load_fn load, uc_serial_fn serial, void *ctx,
                   const struct uc_limits *limits, const struct uc_hooks *hooks)
{
    return run(t, records, count, load, serial, ctx, limits, hooks, 1);
}
