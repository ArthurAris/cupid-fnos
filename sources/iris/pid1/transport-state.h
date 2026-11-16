/* SPDX-License-Identifier: GPL-2.0-only */
#ifndef USB_TRANSPORT_STATE_H
#define USB_TRANSPORT_STATE_H
#include <errno.h>
#include <stdint.h>
#include <stdatomic.h>

/* State belongs to the supervising caller, never to loader polling heartbeats.
 * Only actual stage completion advances sequence. Failure/stale is sticky;
 * late COMPLETE cannot replace it. Not a hardware watchdog or recovery policy. */
enum ts_terminal { TS_ACTIVE, TS_COMPLETE, TS_FAILURE, TS_STALE };
struct ts_state {
    atomic_uint terminal;
    atomic_uint sequence;
    int64_t previous_ns, overall_end_ns, stage_end_ns;
};
static inline int ts_init(struct ts_state *state, int64_t now, int64_t total_ns)
{
    atomic_init(&state->terminal, TS_ACTIVE);
    atomic_init(&state->sequence, 0);
    state->previous_ns = now;
    state->overall_end_ns = now;
    state->stage_end_ns = now;
    if (!atomic_is_lock_free(&state->terminal) || !atomic_is_lock_free(&state->sequence) ||
        now < 0 || total_ns <= 0 || now > INT64_MAX - total_ns) {
        atomic_store_explicit(&state->terminal, TS_FAILURE, memory_order_release);
        return -EINVAL;
    }
    state->overall_end_ns = now + total_ns;
    state->stage_end_ns = state->overall_end_ns;
    return 0;
}
static inline unsigned ts_latch(struct ts_state *state, enum ts_terminal value)
{
    unsigned expected = TS_ACTIVE;
    if (value == TS_ACTIVE || value > TS_STALE)
        return atomic_load_explicit(&state->terminal, memory_order_acquire);
    atomic_compare_exchange_strong_explicit(&state->terminal, &expected, value,
                                           memory_order_acq_rel, memory_order_acquire);
    return atomic_load_explicit(&state->terminal, memory_order_acquire);
}
/* Clock values are supplied by the supervisor and validated; -1 is failure,
 * never a fake zero timestamp. Timing fields have one supervisor writer. */
static inline int ts_check(struct ts_state *state, int clock_result, int64_t now)
{
    if (atomic_load_explicit(&state->terminal, memory_order_acquire) != TS_ACTIVE)
        return -ECANCELED;
    if (clock_result || now < 0 || now < state->previous_ns) {
        ts_latch(state, TS_FAILURE);
        return -EIO;
    }
    state->previous_ns = now;
    if (now >= state->overall_end_ns || now >= state->stage_end_ns) {
        ts_latch(state, TS_STALE);
        return -ETIMEDOUT;
    }
    return 0;
}
static inline int ts_stage_begin(struct ts_state *state, int clock_result,
                                 int64_t now, int64_t stage_ns)
{
    int ret = ts_check(state, clock_result, now);
    if (ret)
        return ret;
    if (stage_ns <= 0 || now > INT64_MAX - stage_ns) {
        ts_latch(state, TS_FAILURE);
        return -EINVAL;
    }
    int64_t end = now + stage_ns;
    state->stage_end_ns = end < state->overall_end_ns ? end : state->overall_end_ns;
    return 0;
}
static inline int ts_stage_complete(struct ts_state *state, int clock_result, int64_t now)
{
    int ret = ts_check(state, clock_result, now);
    if (ret)
        return ret;
    atomic_fetch_add_explicit(&state->sequence, 1, memory_order_release);
    state->stage_end_ns = state->overall_end_ns;
    return 0;
}
#endif
