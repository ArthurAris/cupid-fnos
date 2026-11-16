#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Deterministic passive IRQ-drain host model using the candidate's actual C.

Run through WSL with python3 -I -B. Only extract source text from --work;
compile in a separate temporary directory, never in the candidate directory.
The controller alone releases hard-top, queued-bottom and response gates.
Raw-spin, IRQ context, chip hardware and firmware remain host-model seams.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import importlib.util
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('irq_startup_fixture', HERE / 'test-irq-startup.py')
STARTUP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(STARTUP)
EXTRACT = STARTUP.EXTRACT
DEFAULT_WORK = Path('/var/tmp/iris-pm-irq-release-smt5bxw5')
WORK = DEFAULT_WORK
ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C',
       'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'}
CASES = ('normal_top_thread', 'terminal_paused_hard_top', 'terminal_queued_bottom',
         'terminal_running_response', 'watchdog', 'retry')


def replace_once(text, before, after):
    if text.count(before) != 1:
        raise ValueError('Missing/ambiguous fixture anchor: ' + before)
    return text.replace(before, after, 1)


LOCKS = r'''
struct mutex {pthread_mutex_t lock;};
static _Thread_local struct mutex *held_mutexes[8];
static _Thread_local unsigned held_count, driver_raw_depth;
static _Thread_local bool hard_context, terminal_context;
static bool mutex_is_held(struct mutex *m)
{
    for (unsigned i = 0; i < held_count; ++i)
        if (held_mutexes[i] == m)
            return true;
    return false;
}
static void mutex_lock(struct mutex *m)
{
    assert(!mutex_is_held(m));
    assert(!pthread_mutex_lock(&m->lock));
    assert(held_count < 8);
    held_mutexes[held_count++] = m;
}
static void mutex_unlock(struct mutex *m)
{
    assert(mutex_is_held(m));
    unsigned i = 0;
    while (held_mutexes[i] != m)
        ++i;
    held_mutexes[i] = held_mutexes[--held_count];
    assert(!pthread_mutex_unlock(&m->lock));
}
#define lockdep_assert_held(m) assert(mutex_is_held(m))
typedef struct {pthread_mutex_t lock;} raw_spinlock_t;
static void model_raw_lock(raw_spinlock_t *p)
{
    assert(!driver_raw_depth);
    assert(!pthread_mutex_lock(&p->lock));
    ++driver_raw_depth;
}
static void model_raw_unlock(raw_spinlock_t *p)
{
    assert(driver_raw_depth == 1);
    --driver_raw_depth;
    assert(!pthread_mutex_unlock(&p->lock));
}
#define raw_spin_lock_irqsave(p,f) ((f)=0,model_raw_lock(p))
#define raw_spin_unlock_irqrestore(p,f) ((void)(f),model_raw_unlock(p))
'''

MODEL = r'''
#include <stdio.h>
#include <time.h>
#define REQUIRE(value, label) do { \
    if (!(value)) { \
        fprintf(stderr, "Assertion failed: %s (%s)\n", label, #value); \
        abort(); \
    } \
} while (0)

/* Every access to depth, IRQ occupancy, gates and event counters owns model. */
static pthread_mutex_t model = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed;
static struct timespec deadline;
static pthread_t controller, hard_worker, bottom_worker, terminal_worker;
static irqreturn_t (*registered_top)(int, void *), (*registered_bottom)(int, void *);
static int depth, disables, enables, hard_active, bottom_queued, bottom_running;
static int syncs, sync_exits, queue_frees, responses, resumes, pm_usage, poweroffs;
static int shutdown_error, terminal_result;
static bool watchdog, hard_created, bottom_created, terminal_done, sync_waiting;
static bool gate_top, gate_bottom, gate_response;
static bool top_paused, bottom_paused, response_paused;
static bool release_top, release_bottom, release_response;
static unsigned events, top_finished_at, bottom_finished_at, response_finished_at;
static unsigned sync_entered_at, sync_finished_at, queue_freed_at;

static void model_lock(void) { assert(!pthread_mutex_lock(&model)); }
static void model_unlock(void) { assert(!pthread_mutex_unlock(&model)); }
static void notify(void) { assert(!pthread_cond_broadcast(&changed)); }
static void wait_change(void)
{
    int ret = pthread_cond_timedwait(&changed, &model, &deadline);
    if (ret == ETIMEDOUT) {
        fputs("MODEL_TIMEOUT: event deadline exceeded\n", stderr);
        abort();
    }
    assert(!ret);
}
static bool irq_idle(void)
{
    return !hard_active && !bottom_queued && !bottom_running;
}
static void irq_api_context(void)
{
    REQUIRE(!driver_raw_depth, "irq_api_outside_driver_raw_lock");
}
static void drain_context(void)
{
    irq_api_context();
    REQUIRE(terminal_context && !hard_context, "drain_in_terminal_process_context");
    REQUIRE(!mutex_is_held(&core.lock) && !mutex_is_held(&core.lifecycle_lock),
            "drain_outside_core_and_lifecycle_locks");
}

static int request_threaded_irq(int irq, irqreturn_t (*top)(int, void *),
                                irqreturn_t (*bottom)(int, void *), u32 flags,
                                const char *name, void *data)
{
    irq_api_context();
    assert(irq == 9 && !strcmp(name, "iris") && data == &core);
    assert(flags & IRQF_NO_AUTOEN);
    model_lock();
    depth = 1;
    registered_top = top;
    registered_bottom = bottom;
    model_unlock();
    return 0;
}
static void enable_irq(int irq)
{
    irq_api_context();
    assert(irq == 9);
    model_lock();
    REQUIRE(depth > 0, "enable_has_disable_reference");
    --depth;
    ++enables;
    model_unlock();
}
static void disable_irq_nosync(int irq)
{
    irq_api_context();
    assert(irq == 9);
    model_lock();
    /* Actual top has published TOP ownership, but has not disabled the chip. */
    if (hard_context && gate_top) {
        top_paused = true;
        notify();
        while (!release_top)
            wait_change();
    }
    ++depth;
    ++disables;
    model_unlock();
}
static void __attribute__((unused)) synchronize_irq(int irq)
{
    assert(irq == 9);
    drain_context();
    model_lock();
    ++syncs;
    sync_entered_at = ++events;
    notify();
    /* Passive wait only: no gate assignment, no delivery, no worker join. */
    while (!irq_idle()) {
        sync_waiting = true;
        notify();
        wait_change();
    }
    sync_waiting = false;
    ++sync_exits;
    sync_finished_at = ++events;
    notify();
    model_unlock();
}
static bool cancel_delayed_work_sync(struct delayed_work *w)
{
    assert(w == &core.sys_error_handler);
    drain_context();
    model_lock();
    REQUIRE(irq_idle(), "cancel_requires_finished_hard_top_and_bottom");
    REQUIRE(syncs == sync_exits && syncs > 0, "cancel_after_completed_sync");
    model_unlock();
    return false;
}
static struct work_struct *current_work(void) { return NULL; }
static bool in_interrupt(void) { return hard_context; }
static bool iris_vpu_has_checked_power(struct iris_core *c) { assert(c == &core); return true; }
static bool iris_power_progress_pending(struct iris_core *c) { return c->power_prepared; }
static int pm_runtime_resume_and_get(void *d)
{
    assert(d == &core);
    model_lock();
    ++resumes;
    ++pm_usage;
    model_unlock();
    return 0;
}
static void pm_runtime_put_noidle(void *d)
{
    assert(d == &core);
    model_lock();
    assert(pm_usage > 0);
    --pm_usage;
    model_unlock();
}
static int iris_fw_unload(struct iris_core *c)
{
    assert(c == &core);
    if (!shutdown_error) {
        c->firmware_may_run = false;
        c->firmware_shutdown_verified = true;
    }
    return shutdown_error;
}
static int iris_vpu_power_off_checked(struct iris_core *c)
{
    assert(c == &core);
    model_lock();
    REQUIRE(irq_idle(), "power_off_after_hard_top_and_bottom");
    assert(syncs == sync_exits && syncs && depth > 0);
    assert(!c->irq_top_mask_held);
    ++poweroffs;
    model_unlock();
    c->power_irq_enabled = false;
    return 0;
}
static void iris_hfi_queues_deinit(struct iris_core *c)
{
    assert(c == &core);
    model_lock();
    REQUIRE(irq_idle(), "queue_free_after_hard_top_and_bottom");
    REQUIRE(!top_paused || release_top, "queue_free_after_controller_top_release");
    REQUIRE(!bottom_paused || release_bottom, "queue_free_after_controller_bottom_release");
    REQUIRE(!response_paused || release_response, "queue_free_after_controller_response_release");
    ++queue_frees;
    queue_freed_at = ++events;
    REQUIRE(queue_freed_at > top_finished_at && queue_freed_at > bottom_finished_at,
            "queue_free_ordered_after_top_and_bottom_finish");
    REQUIRE(queue_freed_at > sync_finished_at && sync_exits == syncs && syncs,
            "queue_free_ordered_after_passive_sync");
    model_unlock();
}
static void pm_runtime_mark_last_busy(void *d) { assert(d == &core); }
static void iris_vpu_clear_interrupt(struct iris_core *c)
{
    assert(c == &core && c->queues_allocated && c->power_prepared);
}
static int iris_vpu_watchdog(struct iris_core *c, u32 value)
{
    (void)value;
    assert(c == &core);
    return watchdog ? -ETIME : 0;
}
static void response(struct iris_core *c)
{
    mutex_lock(&c->lock);
    assert(c->queues_allocated && c->firmware_booted);
    mutex_unlock(&c->lock);
    model_lock();
    ++responses;
    if (gate_response) {
        response_paused = true;
        notify();
        while (!release_response)
            wait_change();
        REQUIRE(!queue_frees, "response_keeps_queues_until_controller_release");
    }
    response_finished_at = ++events;
    model_unlock();
    mutex_lock(&c->lock);
    assert(c->queues_allocated && c->firmware_booted);
    mutex_unlock(&c->lock);
}
'''

MAIN = r'''
static void *run_bottom(void *unused)
{
    (void)unused;
    model_lock();
    assert(bottom_queued == 1 && !bottom_running);
    if (gate_bottom) {
        bottom_paused = true;
        notify();
        while (!release_bottom)
            wait_change();
    }
    --bottom_queued;
    ++bottom_running;
    notify();
    model_unlock();
    assert(registered_bottom(9, &core) == IRQ_HANDLED);
    model_lock();
    --bottom_running;
    bottom_finished_at = ++events;
    notify();
    model_unlock();
    return NULL;
}
static void *run_hard(void *unused)
{
    (void)unused;
    hard_context = true;
    assert(registered_top(9, &core) == IRQ_WAKE_THREAD);
    hard_context = false;
    model_lock();
    /* Transfer to queued bottom with no idle gap observable by sync. */
    assert(hard_active == 1 && !bottom_created);
    ++bottom_queued;
    bottom_created = true;
    assert(!pthread_create(&bottom_worker, NULL, run_bottom, NULL));
    --hard_active;
    top_finished_at = ++events;
    notify();
    model_unlock();
    return NULL;
}
static void launch_irq(void)
{
    assert(pthread_equal(pthread_self(), controller));
    model_lock();
    assert(!hard_created && depth == 0 && irq_idle());
    /* Admission precedes terminal disable; sync must include this hard top. */
    hard_created = true;
    ++hard_active;
    assert(!pthread_create(&hard_worker, NULL, run_hard, NULL));
    model_unlock();
}
static void *run_terminal(void *unused)
{
    (void)unused;
    terminal_context = true;
    int ret = iris_core_quiesce(&core);
    model_lock();
    REQUIRE(depth == 1, "terminal_return_depth_one");
    REQUIRE(irq_idle(), "terminal_return_after_hard_top_and_bottom");
    terminal_result = ret;
    terminal_done = true;
    notify();
    model_unlock();
    terminal_context = false;
    return NULL;
}
static void launch_terminal(void)
{
    assert(pthread_equal(pthread_self(), controller));
    model_lock();
    terminal_done = false;
    assert(!pthread_create(&terminal_worker, NULL, run_terminal, NULL));
    model_unlock();
}
static void prove_passive_wait(void)
{
    /* Caller owns model. A closed gate makes non-return a causal invariant. */
    REQUIRE(syncs == 1 && sync_entered_at && sync_waiting && !sync_exits,
            "controller_observed_sync_entered_and_waiting");
    REQUIRE(!terminal_done && !queue_frees, "controller_observed_no_return_or_queue_free");
    REQUIRE(!irq_idle(), "controller_observed_outstanding_irq");
    REQUIRE(!release_top && !release_bottom && !release_response,
            "sync_did_not_release_controller_gates");
}
static void wait_sync(void)
{
    while (!sync_waiting && !terminal_done)
        wait_change();
    prove_passive_wait();
}
static void release_gate(bool *gate)
{
    assert(pthread_equal(pthread_self(), controller));
    REQUIRE(sync_waiting && syncs == 1 && !sync_exits && !terminal_done && !queue_frees,
            "each_controller_release_after_sync_before_return_or_free");
    *gate = true;
    notify();
}
static void join_irq(void)
{
    model_lock();
    while (!irq_idle())
        wait_change();
    model_unlock();
    if (hard_created) {
        assert(!pthread_join(hard_worker, NULL));
        assert(bottom_created);
        assert(!pthread_join(bottom_worker, NULL));
    }
}
static int join_terminal(void)
{
    model_lock();
    while (!terminal_done)
        wait_change();
    int ret = terminal_result;
    model_unlock();
    assert(!pthread_join(terminal_worker, NULL));
    return ret;
}
static void setup(void)
{
    controller = pthread_self();
    assert(!clock_gettime(CLOCK_MONOTONIC, &deadline));
    deadline.tv_sec += 5;
    pthread_condattr_t attr;
    assert(!pthread_condattr_init(&attr));
    assert(!pthread_condattr_setclock(&attr, CLOCK_MONOTONIC));
    assert(!pthread_cond_init(&changed, &attr));
    assert(!pthread_condattr_destroy(&attr));
    assert(!pthread_mutex_init(&core.lock.lock, NULL));
    assert(!pthread_mutex_init(&core.lifecycle_lock.lock, NULL));
    assert(!pthread_mutex_init(&core.quiesce_lock.lock, NULL));
    assert(!pthread_mutex_init(&core.irq_lock.lock, NULL));
    INIT_LIST_HEAD(&core.instances);
    INIT_LIST_HEAD(&core.orphan_instances);
    core.irq = 9;
    core.dev = &core;
    core.hardware_owner_held = true;
    static const struct iris_hfi_response_ops ops = {response};
    core.hfi_response_ops = &ops;
    assert(!iris_request_irq(&core));
    core.queues_allocated = core.power_prepared = core.firmware_booted = true;
    core.firmware_may_run = true;
    core.state = IRIS_CORE_INIT;
    mutex_lock(&core.lock);
    assert(!iris_irq_activate(&core));
    mutex_unlock(&core.lock);
    model_lock();
    assert(depth == 0 && enables == 1 && !disables);
    model_unlock();
}
static void verify_finished(void)
{
    /* Terminal and IRQ pthreads have been joined: driver fields are stable. */
    assert(core.terminal_shutdown && core.quiesce_irq_mask_held);
    assert(!core.irq_top_mask_held && !core.irq_accepting);
    assert(core.irq_drain_verified && core.recovery_drain_verified);
    assert(core.firmware_shutdown_verified && !core.queues_allocated);
    assert(!core.shutdown_pm_held && core.state == IRIS_CORE_DEINIT);
    model_lock();
    assert(irq_idle() && depth == 1 && queue_frees == 1 && pm_usage == 0);
    assert(syncs == sync_exits && poweroffs == 1 && resumes == 1);
    if (hard_created) {
        assert(top_finished_at && bottom_finished_at);
        assert(sync_finished_at > top_finished_at && sync_finished_at > bottom_finished_at);
        assert(queue_freed_at > bottom_finished_at && queue_freed_at > response_finished_at);
    }
    model_unlock();
}
int main(int argc, char **argv)
{
    assert(argc == 2);
    int mode = atoi(argv[1]);
    assert(mode >= 0 && mode < 6);
    setup();
    gate_top = mode == 1;
    gate_bottom = mode == 1 || mode == 2;
    gate_response = mode == 3 || mode == 5;
    watchdog = mode == 4;
    if (mode == 5)
        shutdown_error = -EACCES;
    launch_irq();
    if (mode == 0 || mode == 4) {
        join_irq();
        model_lock();
        assert(responses == 1 && disables == 1);
        assert(depth == (mode == 4 ? 1 : 0));
        assert(enables == (mode == 4 ? 1 : 2));
        model_unlock();
        assert(core.irq_top_mask_held == (mode == 4));
        launch_terminal();
    } else {
        model_lock();
        while (!(gate_top ? top_paused : gate_bottom ? bottom_paused : response_paused))
            wait_change();
        if (gate_top)
            assert(hard_active == 1 && !bottom_queued && !bottom_running && depth == 0);
        else if (gate_bottom)
            assert(!hard_active && bottom_queued == 1 && !bottom_running && depth == 1);
        else
            assert(!hard_active && !bottom_queued && bottom_running == 1 && depth == 1);
        model_unlock();
        launch_terminal();
        model_lock();
        wait_sync();
        assert(depth == (gate_top ? 1 : 2));
        if (gate_top) {
            release_gate(&release_top);
            while (!bottom_paused)
                wait_change();
            assert(!hard_active && bottom_queued == 1 && !bottom_running && depth == 2);
        }
        if (gate_bottom)
            release_gate(&release_bottom);
        else
            release_gate(&release_response);
        model_unlock();
    }
    int ret = join_terminal();
    if (mode != 0 && mode != 4)
        join_irq();
    if (mode == 5) {
        assert(ret == -EACCES && core.queues_allocated && core.shutdown_pm_held);
        assert(!core.irq_top_mask_held && core.quiesce_irq_mask_held);
        model_lock();
        assert(depth == 1 && !queue_frees && pm_usage == 1 && resumes == 1);
        assert(syncs == 1 && sync_exits == 1 && enables == 2 && disables == 2);
        model_unlock();
        shutdown_error = 0;
        launch_terminal();
        assert(!join_terminal());
        model_lock();
        assert(syncs == 2 && enables == 2 && disables == 2 && resumes == 1);
        model_unlock();
    } else {
        assert(!ret);
    }
    verify_finished();
    model_lock();
    assert(responses == (mode == 1 || mode == 2 ? 0 : 1));
    model_unlock();
    printf("PASS passive-drain scenario %d\n", mode);
    return 0;
}
'''


def source(work):
    # Importing the existing test does not invoke its CLI or run its cases.
    # Reuse its declarations, replacing only host-model seams in this fixture.
    prefix = STARTUP.PREFIX.split('static int depth,', 1)[0]
    if prefix == STARTUP.PREFIX:
        raise ValueError('Missing startup PREFIX model boundary')
    start = prefix.index('struct mutex {')
    end = prefix.index('struct list_head {', start)
    prefix = prefix[:start] + LOCKS + prefix[end:]
    # Current startup PREFIX already declares irq_releasing/irq_probe_unpublished.
    # Its lockdep macro is inside the lock block replaced above, not duplicated.
    prefix = replace_once(prefix, 'static void reinit_completion(',
                          'static void __attribute__((unused)) reinit_completion(')
    prefix = replace_once(prefix, 'static bool iris_hfi_pending_match(',
                          'static bool __attribute__((unused)) iris_hfi_pending_match(')
    common = (work / 'iris_hfi_common.c').read_text()
    probe = (work / 'iris_probe.c').read_text()
    core = (work / 'iris_core.c').read_text()
    actual = ''.join(EXTRACT.definition(common, name) for name in
                     ('iris_irq_activate', 'iris_irq_terminal_mask', 'iris_irq_retire_top'))
    actual += EXTRACT.block(common, r'^irqreturn_t\s+iris_hfi_isr\([^;{}]*\)\s*\{', 'hard top')
    actual += EXTRACT.block(common, r'^irqreturn_t\s+iris_hfi_isr_handler\([^;{}]*\)\s*\{', 'bottom')
    actual += EXTRACT.definition(probe, 'iris_request_irq')
    actual += EXTRACT.definition(core, 'iris_core_quiesce')
    return prefix + MODEL + actual + MAIN


MUTATIONS = {
    'missing_sync': ('\tsynchronize_irq(core->irq);', '\t(void)core->irq;'),
    'skipped_top_retirement': ('if (retire)\n\t\tenable_irq(core->irq);',
                               'if (retire)\n\t\t(void)core->irq;'),
}


def compile_fixture(text, directory, sanitize=False):
    path = directory / 'irq_passive_drain.c'
    path.write_text(text)
    binary = directory / 'irq_passive_drain'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1',
            '-pthread', str(path), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, cwd=directory, env=ENV, capture_output=True,
                            text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('Fixture compile failed:\n' + result.stdout + result.stderr)
    return binary


def run_fixture(binary, mode):
    return subprocess.run([str(binary), str(mode)], cwd=binary.parent, env=ENV,
                          capture_output=True, text=True, timeout=10)


class PassiveDrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-irq-passive-')
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        text = source(WORK)
        baseline = directory / 'baseline'
        baseline.mkdir()
        cls.binary = compile_fixture(text, baseline)
        cls.mutants = {}
        for name, (before, after) in MUTATIONS.items():
            target = directory / name
            target.mkdir()
            cls.mutants[name] = compile_fixture(replace_once(text, before, after), target)

    def run_case(self, mode):
        result = run_fixture(self.binary, mode)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, name, mode, invariant):
        result = run_fixture(self.mutants[name], mode)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Assertion failed: ' + invariant, result.stderr)
        self.assertNotIn('MODEL_TIMEOUT', result.stderr)
        self.assertNotIn('AddressSanitizer', result.stderr)
        self.assertNotIn('runtime error:', result.stderr)

    def test_missing_sync_hard_top(self):
        self.mutation('missing_sync', 1, 'cancel_requires_finished_hard_top_and_bottom')

    def test_missing_sync_queued_bottom(self):
        self.mutation('missing_sync', 2, 'cancel_requires_finished_hard_top_and_bottom')

    def test_missing_sync_running_response(self):
        self.mutation('missing_sync', 3, 'cancel_requires_finished_hard_top_and_bottom')

    def test_skipped_top_retirement_paused_hard_top(self):
        self.mutation('skipped_top_retirement', 1, 'terminal_return_depth_one')

    def test_skipped_top_retirement_watchdog(self):
        self.mutation('skipped_top_retirement', 4, 'terminal_return_depth_one')


for mode, name in enumerate(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(PassiveDrainTests, 'test_case_' + name, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, default=DEFAULT_WORK)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work.resolve()
    # Mutation aborts must not deposit core files anywhere.
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if not Path('/usr/bin/gcc').is_file() or not WORK.is_dir():
        print('ENVIRONMENT ERROR: WSL GCC or candidate directory unavailable', file=sys.stderr)
        return 2
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-irq-passive-sanitize-') as temporary:
            binary = compile_fixture(source(WORK), Path(temporary), sanitize=True)
            for mode, name in enumerate(CASES):
                result = run_fixture(binary, mode)
                if result.returncode:
                    print(name + ':\n' + result.stdout + result.stderr, file=sys.stderr)
                    return 1
        print('6 passive-drain ASan/UBSan scenarios passed; host model, not hardware proof')
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(PassiveDrainTests))
    if result.wasSuccessful():
        print('6 scenarios + 5 invariant rejection schedules / 2 compiling mutations passed; '
              '5s event / 10s process / 40s compile bounds; host model, not hardware proof')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
