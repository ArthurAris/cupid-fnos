#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Execute extracted Iris power/resource C helpers against side-effectful providers.

This is a userspace helper/provider test, NOT a module, firmware or device test.
Requires Python 3 and a local C compiler. Run Python with -I; pass source paths.
"""
import argparse
import os
from pathlib import Path
import re
import subprocess
import tempfile


FUNCTIONS = (
    "iris_vpu_interrupt_init", "iris_power_progress_pending",
    "iris_vpu2_power_off_hw_checked", "iris_vpu2_power_off_controller_checked",
    "iris_power_off_drain_gate", "iris_vpu2_power_off_checked",
    "iris_vpu2_power_on_checked",
)


def function(source, name):
    match = re.search(r"^(?:static )?(?:int|bool|void) " + name + r"\([^;]*?\)\n\{", source, re.M)
    if not match:
        raise ValueError(f"function not found: {name}")
    # These functions do not contain braces inside literals. Count from body only.
    start = source.index("{", match.start())
    depth = 0
    for i in range(start, len(source)):
        if source[i] == "{":
            depth += 1
        elif source[i] == "}":
            depth -= 1
            if not depth:
                return source[match.start():i + 1]
    raise ValueError(f"unterminated function: {name}")


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <limits.h>
typedef uint32_t u32;
#define BIT(n) (1U << (n))
#define CPU_BASE_OFFS 0
#define CPU_CS_BASE_OFFS 0x1000
#define WRAPPER_BASE_OFFS 0x2000
#define READ_ONCE(x) (x)
#define ARRAY_SIZE(x) (sizeof(x) / sizeof((x)[0]))
#define clamp_t(t, x, lo, hi) ((t)((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x))))
#define IRIS_CTRL_POWER_DOMAIN 0
#define IRIS_HW_POWER_DOMAIN 1
enum platform_clk_type { IRIS_AXI_CLK, IRIS_CTRL_CLK, IRIS_HW_CLK };
struct device { int id, usage; bool active; };
struct clk { int id, enabled; };
struct clk_bulk_data { const char *id; struct clk *clk; };
struct icc_bulk_data { const char *name; u32 avg_bw, peak_bw; };
struct icc_info { const char *name; u32 bw_min_kbps, bw_max_kbps; };
struct platform_clk_data { enum platform_clk_type clk_type; const char *clk_name; };
struct reset_control_bulk_data { int unused; };
struct dev_pm_domain_list { struct device *pd_devs[2]; };
struct iris_core;
struct iris_platform_data {
    const struct icc_info *icc_tbl;
    const struct platform_clk_data *clk_tbl;
    u32 clk_tbl_size, clk_rst_tbl_size;
    bool no_tz_fifo_reset;
    void (*set_preset_registers)(struct iris_core *);
};
'''
CORE = r'''
struct iris_core {
    struct device *dev;
    unsigned char *reg_base;
    struct icc_bulk_data *icc_tbl;
    u32 icc_count;
    const struct iris_platform_data *iris_platform_data;
    struct dev_pm_domain_list *pmdomain_tbl;
    struct clk_bulk_data *clock_tbl;
    u32 clk_count;
    struct reset_control_bulk_data *resets;
    struct { unsigned long icc_bw; u32 clk_freq; } power;
    struct iris_power_progress power_progress;
    int power_error, power_cleanup_error, power_opp_error, power_domain_put_error;
    bool power_state_unknown, power_irq_enabled, terminal_shutdown, irq_quiescing;
    bool quiesce_irq_mask_held, irq_drain_verified, recovery_drain_verified;
    u32 intr_status;
};
struct providers {
    unsigned long opp_rate;
    int opp_calls, opp_fail_call, opp_errno;
    int icc_calls, icc_fail_call, icc_fail_call2, icc_fail_index, icc_errno;
    u32 avg[2], peak[2];
    int get_ret[2], get_calls[2], put_ret[2], put_calls[2], noidle_calls[2];
    int clock_fail, reset_ret, polls, poll_fail, writes;
} w;
static struct device main_dev, domains[2];
static struct clk clocks[3];
static struct clk_bulk_data clock_tbl[3];
static struct icc_bulk_data icc_tbl[2];
static struct dev_pm_domain_list domain_tbl;
static const struct platform_clk_data clk_spec[] = {
    { IRIS_AXI_CLK, "axi" }, { IRIS_CTRL_CLK, "ctrl" }, { IRIS_HW_CLK, "hw" }
};
static const struct icc_info icc_spec[] = {
    { "video-mem", 100000, 800000 }, { "other", 0, 100000 }
};
static struct iris_platform_data platform;
static unsigned char registers[0x100000];
static struct iris_core c;
static void setup(void)
{
    memset(&w, 0, sizeof(w)); memset(&c, 0, sizeof(c));
    memset(domains, 0, sizeof(domains)); memset(clocks, 0, sizeof(clocks));
    w.clock_fail = -1; w.opp_errno = -ERANGE; w.icc_errno = -EIO;
    for (int i = 0; i < 2; i++) { domains[i].id = i; domain_tbl.pd_devs[i] = &domains[i]; }
    for (int i = 0; i < 3; i++) {
        clocks[i].id = i; clock_tbl[i].id = clk_spec[i].clk_name; clock_tbl[i].clk = &clocks[i];
    }
    icc_tbl[0] = (struct icc_bulk_data){ "video-mem", 0, 77 };
    icc_tbl[1] = (struct icc_bulk_data){ "other", 120, 250 };
    platform = (struct iris_platform_data){ .icc_tbl = icc_spec, .clk_tbl = clk_spec,
        .clk_tbl_size = 3, .clk_rst_tbl_size = 1, .no_tz_fifo_reset = true };
    c.dev = &main_dev; c.reg_base = registers; c.icc_tbl = icc_tbl; c.icc_count = 2;
    c.iris_platform_data = &platform; c.pmdomain_tbl = &domain_tbl;
    c.clock_tbl = clock_tbl; c.clk_count = 3;
}
/* Providers intentionally modify requested/active state before returning errors. */
static int dev_pm_opp_set_rate(struct device *dev, unsigned long rate)
{
    assert(dev == c.dev); w.opp_calls++; w.opp_rate = rate;
    return w.opp_calls == w.opp_fail_call ? w.opp_errno : 0;
}
static int icc_bulk_set_bw(u32 count, struct icc_bulk_data *tbl)
{
    w.icc_calls++;
    for (u32 i = 0; i < count; i++) {
        w.avg[i] = tbl[i].avg_bw; w.peak[i] = tbl[i].peak_bw;
        if ((w.icc_calls == w.icc_fail_call || w.icc_calls == w.icc_fail_call2) &&
            (int)i == w.icc_fail_index) return w.icc_errno;
    }
    return 0;
}
static int pm_runtime_get_sync(struct device *dev)
{
    dev->usage++; w.get_calls[dev->id]++;
    if (w.get_ret[dev->id] >= 0) dev->active = true;
    return w.get_ret[dev->id];
}
static void pm_runtime_put_noidle(struct device *dev)
{
    assert(dev->usage > 0); dev->usage--; w.noidle_calls[dev->id]++;
}
static int pm_runtime_put_sync(struct device *dev)
{
    assert(dev->usage > 0); dev->usage--; w.put_calls[dev->id]++;
    if (w.put_ret[dev->id] >= 0) dev->active = false;
    return w.put_ret[dev->id];
}
static int clk_prepare_enable(struct clk *clock)
{
    if (clock->id == w.clock_fail) return -EIO;
    clock->enabled++; return 0;
}
static void clk_disable_unprepare(struct clk *clock)
{
    assert(clock->enabled > 0); clock->enabled--;
}
static int dev_pm_genpd_set_hwmode(struct device *dev, bool mode)
{
    (void)mode; assert(dev == &domains[1]); return 0;
}
static int reset_control_bulk_reset(u32 count, struct reset_control_bulk_data *resets)
{
    (void)count; (void)resets; return w.reset_ret;
}
static void require_mmio(void)
{
    assert(domains[0].active && domains[0].usage > 0);
    assert(clocks[IRIS_AXI_CLK].enabled && clocks[IRIS_CTRL_CLK].enabled);
}
static u32 readl(const void *address) { (void)address; require_mmio(); return 0; }
static void writel(u32 value, void *address)
{
    (void)value; (void)address; require_mmio(); w.writes++;
}
static int provider_poll(void) { require_mmio(); return ++w.polls == w.poll_fail ? -ETIMEDOUT : 0; }
#define readl_poll_timeout(addr, value, cond, delay, timeout) provider_poll()
'''

TESTS = r'''
#define CHECK(x) do { if (!(x)) { fprintf(stderr, "%s:%d: %s\n", __func__, __LINE__, #x); return 1; } } while (0)
static bool clean(void)
{
    return !iris_power_progress_pending(&c) && !c.power_state_unknown && !c.power_irq_enabled;
}
static int positive_put(void)
{
    setup(); CHECK(!iris_enable_power_domains(&c, &domains[0])); w.put_ret[0] = 1;
    CHECK(!iris_disable_power_domains(&c, &domains[0]));
    CHECK(!c.power_domain_put_error && !c.power_opp_error && !c.power_progress.opp_dirty);
    CHECK(domains[0].usage == 0 && w.put_calls[0] == 1); return 0;
}
static int dual_errno(void)
{
    setup(); CHECK(!iris_enable_power_domains(&c, &domains[0]));
    w.opp_fail_call = w.opp_calls + 1; w.opp_errno = -ERANGE; w.put_ret[0] = -ETIMEDOUT;
    CHECK(iris_disable_power_domains(&c, &domains[0]) == -ERANGE);
    CHECK(c.power_opp_error == -ERANGE && c.power_domain_put_error == -ETIMEDOUT);
    CHECK(c.power_progress.opp_dirty && domains[0].usage == 0 && w.put_calls[0] == 1); return 0;
}
static int domain_get_unwind(void)
{
    setup(); w.get_ret[0] = -ENODEV;
    CHECK(iris_vpu2_power_on_checked(&c) == -ENODEV);
    CHECK(w.get_calls[0] == 1 && w.noidle_calls[0] == 1 && domains[0].usage == 0);
    CHECK(w.opp_calls == 2 && !w.opp_rate && clean()); return 0;
}
static int domain_get_opp_retained(void)
{
    setup(); w.get_ret[0] = -ENODEV; w.opp_fail_call = 2;
    CHECK(iris_vpu2_power_on_checked(&c) == -ENODEV);
    CHECK(c.power_error == -ENODEV && c.power_cleanup_error == -ERANGE);
    CHECK(c.power_progress.opp_dirty && iris_power_progress_pending(&c));
    CHECK(c.power_progress.icc_voted && !w.put_calls[0] && domains[0].usage == 0);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
static int opp_enable_unwind(void)
{
    setup(); w.opp_fail_call = 1;
    CHECK(iris_vpu2_power_on_checked(&c) == -ERANGE);
    CHECK(!w.get_calls[0] && w.opp_calls == 2 && !w.opp_rate && clean()); return 0;
}
static int opp_attempt_dirty(void)
{
    setup(); w.opp_fail_call = 1;
    CHECK(iris_enable_power_domains(&c, &domains[0]) == -ERANGE);
    CHECK(c.power_progress.opp_dirty && w.opp_rate == ULONG_MAX && !w.get_calls[0]);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
static int icc_initial_unwind(void)
{
    setup(); w.icc_fail_call = 1; w.icc_fail_index = 0;
    CHECK(iris_vpu2_power_on_checked(&c) == -EIO);
    CHECK(w.icc_calls == 2 && !w.avg[0] && !w.avg[1] && !w.peak[0] && !w.peak[1]);
    CHECK(!w.get_calls[0] && clean() && c.power_error == -EIO && !c.power_cleanup_error); return 0;
}
static int initial_icc_cleanup_retained(void)
{
    setup(); w.icc_fail_call = 1; w.icc_fail_call2 = 2; w.icc_fail_index = 0;
    w.avg[1] = 444; w.peak[1] = 555;
    CHECK(iris_vpu2_power_on_checked(&c) == -EIO);
    CHECK(c.power_error == -EIO && c.power_cleanup_error == -EIO);
    CHECK(c.power_progress.icc_dirty && !c.power_progress.icc_voted && iris_power_progress_pending(&c));
    CHECK(w.icc_calls == 2 && w.avg[1] == 444 && w.peak[1] == 555 && !w.get_calls[0]);
    CHECK(iris_vpu2_power_on_checked(&c) == -EBUSY && w.icc_calls == 2);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean() && w.icc_calls == 3 && !w.avg[1]); return 0;
}
static int icc_failed_unvote_retry(void)
{
    setup(); CHECK(!iris_set_icc_bw(&c, INT_MAX));
    w.icc_fail_call = 2; w.icc_fail_index = 0;
    CHECK(iris_vpu2_power_off_checked(&c) == -EIO);
    CHECK(c.power_progress.icc_dirty && c.power_progress.icc_voted && w.avg[1] == 120);
    CHECK(iris_power_progress_pending(&c));
    CHECK(!iris_vpu2_power_off_checked(&c) && clean() && w.icc_calls == 3 && !w.avg[1]); return 0;
}
static int icc_no_video_mem(void)
{
    setup(); icc_tbl[0].name = "not-video-mem"; w.icc_fail_call = 1;
    CHECK(iris_vpu2_power_on_checked(&c) == -EIO);
    CHECK(w.icc_calls == 2 && clean() && !w.peak[0] && !w.avg[1]); return 0;
}
static int icc_attempt_dirty(void)
{
    setup(); w.icc_fail_call = 1;
    CHECK(iris_set_icc_bw(&c, INT_MAX) == -EIO);
    CHECK(c.power_progress.icc_dirty && !c.power_progress.icc_voted);
    CHECK(w.avg[0] == 800000 && icc_tbl[0].avg_bw == 800000 && !c.power.icc_bw);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
static int dirty_threshold(void)
{
    setup(); CHECK(!iris_set_icc_bw(&c, 400000)); w.icc_fail_call = 2;
    CHECK(iris_set_icc_bw(&c, 600000) == -EIO);
    CHECK(c.power_progress.icc_dirty && w.avg[0] == 600000);
    CHECK(!iris_set_icc_bw(&c, 400000));
    CHECK(w.icc_calls == 3 && w.avg[0] == 400000 && !c.power_progress.icc_dirty); return 0;
}
static int opp_progress(void)
{
    setup(); c.power_progress.opp_dirty = true; CHECK(iris_power_progress_pending(&c));
    CHECK(!iris_vpu2_power_off_checked(&c) && clean() && w.opp_calls == 1); return 0;
}
static int icc_progress(void)
{
    setup(); c.power_progress.icc_dirty = true; w.avg[1] = 333;
    CHECK(iris_power_progress_pending(&c)); CHECK(!iris_vpu2_power_off_checked(&c));
    CHECK(clean() && !w.avg[1] && w.icc_calls == 1); return 0;
}
static int unsafe_partial_gate(void)
{
    setup(); CHECK(!iris_enable_power_domains(&c, &domains[0])); c.power_progress.ctrl_domain_ref = true;
    CHECK(!iris_prepare_enable_clock(&c, IRIS_AXI_CLK)); c.power_progress.axi_clk_on = true;
    int before = w.opp_calls;
    CHECK(iris_vpu2_power_off_checked(&c) == -EAGAIN);
    CHECK(w.opp_calls == before && !w.put_calls[0] && !w.polls && !w.writes);
    CHECK(c.power_progress.opp_dirty && c.power_progress.axi_clk_on && domains[0].usage == 1); return 0;
}
static int clock_failure_retained(void)
{
    setup(); w.clock_fail = IRIS_CTRL_CLK;
    CHECK(iris_vpu2_power_on_checked(&c) == -EIO);
    CHECK(c.power_cleanup_error == -EAGAIN && c.power_error == -EIO);
    CHECK(c.power_progress.ctrl_domain_ref && c.power_progress.axi_clk_on && c.power_progress.opp_dirty);
    CHECK(!w.polls && !w.put_calls[0] && domains[0].usage == 1); return 0;
}
static int terminal_drain_gate(void)
{
    setup(); c.power_irq_enabled = true; c.power_progress.opp_dirty = true;
    CHECK(iris_vpu2_power_off_checked(&c) == -EAGAIN && !w.opp_calls);
    c.terminal_shutdown = c.irq_quiescing = c.quiesce_irq_mask_held = c.irq_drain_verified = true;
    CHECK(iris_vpu2_power_off_checked(&c) == -EAGAIN && !w.opp_calls);
    c.recovery_drain_verified = true;
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
static int normal_cycle(void)
{
    setup(); w.get_ret[0] = w.get_ret[1] = 1; w.put_ret[0] = w.put_ret[1] = 1;
    CHECK(!iris_vpu2_power_on_checked(&c));
    CHECK(c.power_progress.opp_dirty && c.power_progress.icc_voted && !c.power_progress.icc_dirty);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean());
    CHECK(w.put_calls[0] == 1 && w.put_calls[1] == 1 && !c.power_domain_put_error);
    CHECK(!domains[0].usage && !domains[1].usage && !w.opp_rate && w.polls == 3);
    for (int i = 0; i < 3; i++) CHECK(!clocks[i].enabled); return 0;
}
static int dirty_blocks_reinit(void)
{
    setup(); c.power_progress.opp_dirty = true; c.power_progress.controller_drained = true;
    CHECK(iris_vpu2_power_on_checked(&c) == -EBUSY);
    CHECK(!w.opp_calls && !w.icc_calls && c.power_progress.controller_drained);
    c.power_progress.opp_dirty = false; c.power_progress.icc_dirty = true;
    CHECK(iris_vpu2_power_on_checked(&c) == -EBUSY && !w.icc_calls); return 0;
}
static int drained_no_rehandshake(void)
{
    setup(); CHECK(!iris_vpu2_power_on_checked(&c));
    CHECK(!iris_vpu2_power_off_checked(&c) && c.power_progress.controller_drained);
    int polls = w.polls, puts = w.put_calls[0] + w.put_calls[1];
    CHECK(!iris_vpu2_power_off_checked(&c) && w.polls == polls);
    CHECK(w.put_calls[0] + w.put_calls[1] == puts); return 0;
}
static int put_failure_consumed_once(void)
{
    setup(); CHECK(!iris_vpu2_power_on_checked(&c)); w.put_ret[1] = -EIO;
    CHECK(iris_vpu2_power_off_checked(&c) == -EIO && c.power_state_unknown);
    CHECK(!c.power_progress.hw_domain_ref && !domains[1].usage && w.put_calls[1] == 1);
    CHECK(c.power_progress.hw_clk_on && c.power_progress.ctrl_domain_ref);
    CHECK(iris_vpu2_power_off_checked(&c) == -EUCLEAN && w.put_calls[1] == 1 && !w.put_calls[0]); return 0;
}
static int cleanup_error_independent(void)
{
    setup(); w.reset_ret = -EINVAL; w.opp_fail_call = 2; w.opp_errno = -ERANGE;
    CHECK(iris_vpu2_power_on_checked(&c) == -EINVAL);
    CHECK(c.power_error == -EINVAL && c.power_cleanup_error == -ERANGE && c.power_opp_error == -ERANGE);
    CHECK(c.power_progress.ctrl_domain_ref && domains[0].usage == 1);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
static int final_opp_failure(void)
{
    setup(); w.opp_fail_call = 3;
    CHECK(iris_vpu2_power_on_checked(&c) == -ERANGE);
    CHECK(clean() && !w.opp_rate && c.power_error == -ERANGE && !c.power_cleanup_error);
    CHECK(w.put_calls[0] == 1 && w.put_calls[1] == 1); return 0;
}
static int controller_poll_failure(void)
{
    setup(); CHECK(!iris_vpu2_power_on_checked(&c)); w.poll_fail = 1;
    CHECK(iris_vpu2_power_off_checked(&c) == -ETIMEDOUT);
    CHECK(!c.power_progress.controller_drained && c.power_progress.ctrl_domain_ref);
    CHECK(c.power_progress.axi_clk_on && c.power_progress.ctrl_clk_on && !w.put_calls[0]);
    CHECK(!iris_vpu2_power_off_checked(&c) && clean()); return 0;
}
struct test { const char *name; int (*run)(void); };
static const struct test tests[] = {
    { "positive_put", positive_put }, { "dual_errno", dual_errno },
    { "domain_get_unwind", domain_get_unwind }, { "domain_get_opp_retained", domain_get_opp_retained },
    { "opp_enable_unwind", opp_enable_unwind }, { "opp_attempt_dirty", opp_attempt_dirty },
    { "icc_initial_unwind", icc_initial_unwind }, { "initial_icc_cleanup_retained", initial_icc_cleanup_retained },
    { "icc_failed_unvote_retry", icc_failed_unvote_retry },
    { "icc_no_video_mem", icc_no_video_mem }, { "icc_attempt_dirty", icc_attempt_dirty },
    { "dirty_threshold", dirty_threshold }, { "opp_progress", opp_progress }, { "icc_progress", icc_progress },
    { "unsafe_partial_gate", unsafe_partial_gate }, { "clock_failure_retained", clock_failure_retained },
    { "terminal_drain_gate", terminal_drain_gate }, { "normal_cycle", normal_cycle },
    { "dirty_blocks_reinit", dirty_blocks_reinit }, { "drained_no_rehandshake", drained_no_rehandshake },
    { "put_failure_consumed_once", put_failure_consumed_once },
    { "cleanup_error_independent", cleanup_error_independent }, { "final_opp_failure", final_opp_failure },
    { "controller_poll_failure", controller_poll_failure },
};
int main(int argc, char **argv)
{
    size_t count = 0;
    for (size_t i = 0; i < ARRAY_SIZE(tests); i++) {
        if (argc > 1 && strcmp(argv[1], tests[i].name)) continue;
        if (tests[i].run()) return 1;
        printf("PASS %s\n", tests[i].name); count++;
    }
    if (!count) return 2;
    printf("SCENARIOS %zu\n", count); return 0;
}
'''


def translation_unit(header, resources, vpu):
    progress = re.search(r"struct iris_power_progress \{.*?\n\};", header, re.S)
    if not progress:
        raise ValueError("missing actual iris_power_progress declaration")
    defines = "\n".join(line for line in vpu.splitlines() if line.startswith("#define "))
    actual_resources = re.sub(r'^#include[^\n]*\n', '', resources, flags=re.M)
    return (PRELUDE + progress.group() + CORE + defines + "\n" + actual_resources + "\n" +
            "\n\n".join(function(vpu, name) for name in FUNCTIONS) + TESTS)


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"mutation anchor not unique: {before!r}")
    return source.replace(before, after, 1)


def compile_and_run(directory, code, cc, sanitize, scenario=None):
    directory.mkdir()
    source = directory / "helper-test.c"
    binary = directory / "helper-test"
    source.write_text(code, encoding="utf-8")
    flags = ["-std=gnu11", "-g", "-O1", "-Wall", "-Wextra", "-Werror",
             "-Wno-unused-variable", "-Wno-misleading-indentation"]
    if sanitize:
        flags += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer", "-fno-pie", "-no-pie"]
    build = subprocess.run([cc, *flags, str(source), "-o", str(binary)],
                           cwd=directory.parent, capture_output=True, text=True, timeout=60)
    if build.returncode:
        raise RuntimeError("helper compile failed:\n" + build.stdout + build.stderr)
    env = os.environ.copy()
    env["ASAN_OPTIONS"] = "detect_leaks=1:abort_on_error=1"
    env["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    return subprocess.run([str(binary), *([scenario] if scenario else [])],
                          cwd=directory.parent, env=env, capture_output=True, text=True, timeout=30)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", "--source-dir", dest="source_dir", required=True,
                        type=Path, metavar="DIR", help="Iris source directory (aliases)")
    parser.add_argument("--cc", default="cc", help="local C compiler (default: cc)")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--sanitize", action="store_true",
                       help="run only the 24 scenarios with ASan/UBSan; skip oracle controls")
    modes.add_argument("--no-sanitize", action="store_true",
                       help="run normal scenarios and oracle controls without sanitizers")
    parser.add_argument("--mutations", action="store_true",
                        help="compatibility flag; normal mode already runs all 13 oracle controls")
    args = parser.parse_args()
    sanitized = not args.no_sanitize
    run_mutations = not args.sanitize
    root = args.source_dir.resolve()
    header = (root / "iris_core.h").read_text(encoding="utf-8")
    resources = (root / "iris_resources.c").read_text(encoding="utf-8")
    vpu = (root / "iris_vpu_common.c").read_text(encoding="utf-8")
    base = translation_unit(header, resources, vpu)
    mutations = [
        ("raw-positive-pm-put", "positive_put", "resources",
         "return core->power_domain_put_error;", "return put_ret;"),
        ("erase-opp-attempt", "opp_attempt_dirty", "resources",
         "core->power_progress.opp_dirty = true;\n\tret = dev_pm_opp_set_rate(core->dev, ULONG_MAX);",
         "core->power_progress.opp_dirty = false;\n\tret = dev_pm_opp_set_rate(core->dev, ULONG_MAX);"),
        ("clock-only-opp-cleanup", "domain_get_unwind", "vpu",
         "if (core->power_progress.opp_dirty) {",
         "if (core->power_progress.axi_clk_on || core->power_progress.ctrl_clk_on) {"),
        ("omit-opp-progress", "opp_progress", "vpu", " || p->opp_dirty", ""),
        ("omit-icc-progress", "icc_progress", "vpu", " || p->icc_dirty", ""),
        ("skip-initial-icc-unvote", "icc_initial_unwind", "vpu",
         "ret = iris_set_icc_bw(core, INT_MAX);\n\tif (ret)\n\t\tgoto err_unwind;",
         "ret = iris_set_icc_bw(core, INT_MAX);\n\tif (ret)\n\t\treturn ret;"),
        ("erase-icc-on-unvote-error", "icc_failed_unvote_retry", "resources",
         "/* Only an actual successful unvote retires provider ownership. */\n\tif (!ret) {",
         "/* Mutant: erase ownership even on failure. */\n\tif (true) {"),
        ("dirty-threshold-skip", "dirty_threshold", "resources",
         "if (!core->power_progress.icc_dirty &&", "if ("),
        ("overwrite-put-errno", "dual_errno", "resources",
         "core->power_domain_put_error = put_ret < 0 ? put_ret : 0;",
         "core->power_domain_put_error = ret; (void)put_ret;"),
        ("weaken-terminal-drain", "terminal_drain_gate", "vpu",
         "!core->recovery_drain_verified))", "false))"),
        ("erase-failed-zero-opp", "dual_errno", "resources",
         "if (!ret)\n\t\tcore->power_progress.opp_dirty = false;",
         "if (true)\n\t\tcore->power_progress.opp_dirty = false;"),
        ("omit-icc-attempt-dirty", "icc_attempt_dirty", "resources",
         "core->power_progress.icc_dirty = true;\n\t\t\tret = icc_bulk_set_bw",
         "core->power_progress.icc_dirty = false;\n\t\t\tret = icc_bulk_set_bw"),
        ("skip-partial-clock-gate", "unsafe_partial_gate", "vpu",
         "/* A powered controller without its clock prerequisite cannot be forced off. */\n\tif (!core->power_progress.controller_drained &&",
         "/* Mutant: skip pre-cleanup clock prerequisite. */\n\tif (false && !core->power_progress.controller_drained &&"),
    ]
    with tempfile.TemporaryDirectory(prefix="iris-provider-progress-") as scratch:
        directory = Path(scratch)
        result = compile_and_run(directory / "baseline", base, args.cc, sanitized)
        print(result.stdout, end="")
        if result.returncode:
            raise RuntimeError("baseline failed:\n" + result.stderr)
        count = int(re.search(r"SCENARIOS (\d+)", result.stdout).group(1))
        killed = 0
        if run_mutations:
            for name, scenario, lane, before, after in mutations:
                inputs = {"resources": resources, "vpu": vpu}
                inputs[lane] = replace_once(inputs[lane], before, after)
                code = translation_unit(header, inputs["resources"], inputs["vpu"])
                result = compile_and_run(directory / name, code, args.cc,
                                         sanitized, scenario)
                if not result.returncode:
                    raise RuntimeError(f"mutation survived: {name}")
                if not ("CHECK" in result.stderr or scenario in result.stderr or "Assertion" in result.stderr):
                    raise RuntimeError(f"mutation infrastructure failure: {name}\n{result.stderr}")
                print(f"KILLED {name} ({scenario})")
                killed += 1
        print(f"Actual-C: {count} passed, 0 failed, {count} total")
        if args.sanitize:
            print(f"{count} ASan/UBSan scenarios passed")
        else:
            print(f"oracle controls: {killed} passed, 0 failed")
        print(f"ASan/UBSan={'on' if sanitized else 'off (raw)'}; "
              "actual C helpers, modeled providers only")


if __name__ == "__main__":
    main()
