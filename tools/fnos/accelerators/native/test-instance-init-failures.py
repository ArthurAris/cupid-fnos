#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile extracted Iris instance/open unwind against explicit native seams.

Run python3 -I test-instance-init-failures.py --work /absolute/source-root
Add --sanitize for ASan/UBSan; default mode is normal. Source is read ONLY from
--work, never from the repository or this script's directory by default.
Actual, unmodified function bodies: decoder init/deinit, iris_ctrls_init,
open_owned, and fh init/deinit. No kernel headers or module build are involved.
Seams replace allocation, PM/core initialization, instance factory, m2m/vb2,
control-framework entry points, and final publication. Their ownership oracle
rejects duplicate releases, invalid/error-pointer releases and leaked resources.
This proves local synchronous unwind, NOT framework internals, DMA shutdown,
concurrent publication/removal, active-file disconnection or actual close.

Compiler inputs/binaries live in a new empty temporary directory, separate from
source data. Every mutant must compile successfully before runtime rejection
counts; failed compilation is a test failure, never a killed mutant. Each mode
runs the baseline once (24 cases) and each of the 18 mutants once. Sanitizer mode
does not run normal mode first or print normal-mode runner counters.
"""

import argparse
import os
from pathlib import Path
import re
import resource
import shutil
import subprocess
import tempfile


def extract(text, name):
    # These functions contain no braces in literals/comments; reject ambiguity.
    match = re.search(r"(?m)^(?:static )?(?:int|void) " + re.escape(name) +
                      r"\([^\n]*\)\n\{", text)
    if match is None:
        raise AssertionError(f"cannot extract actual function {name}")
    start = match.start()
    depth = 1
    pos = match.end()
    while depth:
        if pos >= len(text):
            raise AssertionError(f"unclosed actual function {name}")
        depth += (text[pos] == "{") - (text[pos] == "}")
        pos += 1
    return text[start:pos] + "\n"


PREAMBLE = r'''
#include <errno.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define CHECK(x) do { if (!(x)) { fprintf(stderr, "case=%s line=%d: %s\n", \
    case_name, __LINE__, #x); exit(1); } } while (0)
#define GFP_KERNEL 0
#define MIN_BUFFERS 4
#define DEFAULT_WIDTH 1920
#define DEFAULT_HEIGHT 1080
#define ALIGN(x, a) (((x) + (a) - 1) & ~((a) - 1))
#define INST_FW_CAP_MAX 3
#define CAP_FLAG_MENU 1
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 1
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 2
#define V4L2_PIX_FMT_H264 3
#define V4L2_PIX_FMT_NV12 4
#define V4L2_FIELD_NONE 0
#define V4L2_COLORSPACE_DEFAULT 0
#define V4L2_XFER_FUNC_DEFAULT 0
#define V4L2_YCBCR_ENC_DEFAULT 0
#define V4L2_QUANTIZATION_DEFAULT 0
#define V4L2_CID_MIN_BUFFERS_FOR_CAPTURE 1001
#define V4L2_CID_MIN_BUFFERS_FOR_OUTPUT 1002
#define IRIS_INST_DEINIT 0
#define ERR_PTR(e) ((void *)(intptr_t)(e))
#define IS_ERR(p) ((uintptr_t)(p) >= (uintptr_t)-4095)
#define IS_ERR_OR_NULL(p) (!(p) || IS_ERR(p))
#define PTR_ERR(p) ((int)(intptr_t)(p))
#define INIT_LIST_HEAD(p) ((void)(p))
#define init_completion(p) ((void)(p))
#define iris_hfi_pending_init(p) ((void)(p))
#define refcount_set(p, v) (*(p) = (v))
#define hash32_ptr(p) ((uint32_t)(uintptr_t)(p))
#define dev_err(...) ((void)0)

typedef uint32_t u32;
enum { DECODER = 1, ENCODER };
enum { BUF_INPUT, BUF_OUTPUT, BUF_BIN, BUF_ARP, BUF_COMV, BUF_NON_COMV,
       BUF_LINE, BUF_DPB, BUF_PERSIST, BUF_SCRATCH_1, BUF_SCRATCH_2, BUF_VPSS,
       BUF_MAX };
struct platform_inst_fw_cap { u32 cap_id, flags, min, max, step_or_mask, value; };
struct v4l2_format {
    u32 type;
    struct { struct {
        u32 width, height, pixelformat, num_planes, field, colorspace, xfer_func;
        u32 ycbcr_enc, quantization;
        struct { u32 bytesperline, sizeimage; } plane_fmt[1];
    } pix_mp; } fmt;
};
struct v4l2_ctrl_handler { int live, error; };
struct v4l2_ctrl { int unused; };
struct v4l2_m2m_dev { int unused; };
struct v4l2_m2m_ctx { int unused; };
struct v4l2_fh {
    struct v4l2_ctrl_handler *ctrl_handler;
    struct v4l2_m2m_ctx *m2m_ctx;
    int initialized, added;
};
struct video_device { const char *name; };
struct iris_inst;
struct iris_platform_data { struct iris_inst *(*get_instance)(void); };
struct iris_core {
    void *dev;
    struct video_device *vdev_dec, *vdev_enc;
    struct iris_platform_data *iris_platform_data;
    struct platform_inst_fw_cap inst_fw_caps_dec[INST_FW_CAP_MAX];
};
struct iris_inst {
    struct iris_core *core;
    struct v4l2_format *fmt_src, *fmt_dst;
    u32 fw_min_count, codec, domain, session_id, state;
    struct { u32 min_count, size; int list; } buffers[BUF_MAX];
    struct platform_inst_fw_cap fw_caps[INST_FW_CAP_MAX];
    int lock, ctx_q_lock, completion, control_pending, response_refs;
    int response_idle, response_closing, response_error_notified, flush_completion;
    struct v4l2_fh fh;
    struct v4l2_ctrl_handler ctrl_handler;
    struct v4l2_m2m_dev *m2m_dev;
    struct v4l2_m2m_ctx *m2m_ctx;
};
struct file {
    struct iris_core *core;
    struct video_device *vdev;
    struct v4l2_fh *private_data;
};
struct vb2_queue { int unused; };

/* Seams below model contracts only; actual Iris functions are appended later. */
enum failure {
    NONE, SRC_ALLOC, DST_ALLOC, CTRL_INIT, CTRL_STD, CTRL_MENU, CTRL_MIN,
    FACTORY_ERR, FACTORY_NULL, M2M_DEV_ERR, M2M_DEV_NULL, M2M_CTX_ERR,
    M2M_CTX_NULL, PUBLICATION, PM_GET, CORE_INIT
};
enum object_kind { INSTANCE, FORMAT, M2M_DEV, M2M_CTX };
struct allocation { void *ptr; enum object_kind kind; int live; };
static struct allocation allocated[32];
static int nallocated, fmt_attempts, fmt_live, instance_live, m2m_dev_live;
static int m2m_ctx_live, ctrl_live, ctrl_inits, ctrl_frees, dev_releases;
static int ctx_releases, fh_inits, fh_exits, locks_live, pm_held;
static int publish_calls, published, scenario, menu_cap;
static const char *case_name;
static struct iris_inst *last_inst;
static struct iris_core core;
static struct iris_platform_data platform;
static struct video_device decoder = { "qcom-iris-decoder" };
static struct video_device encoder = { "qcom-iris-encoder" };
static const int iris_ctrl_ops = 0, iris_m2m_ops = 0;
static struct v4l2_ctrl dummy_ctrl;

static void *allocate(size_t size, enum object_kind kind)
{
    CHECK(nallocated < 32);
    void *ptr = calloc(1, size);
    CHECK(ptr != NULL);
    allocated[nallocated++] = (struct allocation){ ptr, kind, 1 };
    if (kind == FORMAT) fmt_live++;
    if (kind == INSTANCE) instance_live++;
    if (kind == M2M_DEV) m2m_dev_live++;
    if (kind == M2M_CTX) m2m_ctx_live++;
    return ptr;
}
static void retire(void *ptr, enum object_kind kind)
{
    CHECK(!IS_ERR_OR_NULL(ptr));
    for (int i = 0; i < nallocated; i++) {
        if (allocated[i].ptr != ptr) continue;
        CHECK(allocated[i].kind == kind && allocated[i].live);
        allocated[i].live = 0;
        if (kind == FORMAT) fmt_live--;
        if (kind == INSTANCE) instance_live--;
        if (kind == M2M_DEV) m2m_dev_live--;
        if (kind == M2M_CTX) m2m_ctx_live--;
        /* Defer libc free until reset: detects duplicate frees deterministically. */
        return;
    }
    CHECK(0);
}
static void *kzalloc(size_t size, int flags)
{
    fmt_attempts++;
    if ((scenario == SRC_ALLOC && fmt_attempts == 1) ||
        (scenario == DST_ALLOC && fmt_attempts == 2)) return NULL;
    return allocate(size, FORMAT);
}
static void kfree(void *ptr)
{
    if (!ptr) return;
    CHECK(!IS_ERR(ptr));
    for (int i = 0; i < nallocated; i++)
        if (allocated[i].ptr == ptr) { retire(ptr, allocated[i].kind); return; }
    CHECK(0);
}
static void mutex_init(int *lock) { CHECK(!*lock); *lock = 1; locks_live++; }
static void mutex_destroy(int *lock) { CHECK(*lock); *lock = 0; locks_live--; }
static int iris_get_buffer_size(struct iris_inst *inst, int type)
{
    CHECK(inst->fmt_src && inst->fmt_dst);
    return 4096;
}
static int iris_vpu_buf_count(struct iris_inst *inst, int type) { return 4; }
static u32 iris_get_v4l2_id(u32 id) { return id ? 100 : 0; }
static int v4l2_ctrl_handler_init(struct v4l2_ctrl_handler *h, u32 count)
{
    CHECK(!h->live);
    ctrl_inits++;
    /* Failed framework init exposes no owned handler; Iris returns directly. */
    if (scenario == CTRL_INIT) return -ENOMEM;
    h->live = 1; h->error = 0; ctrl_live++;
    return 0;
}
static void v4l2_ctrl_handler_free(struct v4l2_ctrl_handler *h)
{
    CHECK(h->live);
    h->live = 0; ctrl_live--; ctrl_frees++;
}
static struct v4l2_ctrl *v4l2_ctrl_new_std(struct v4l2_ctrl_handler *h,
        const void *ops, u32 id, u32 min, u32 max, u32 step, u32 value)
{
    CHECK(h->live);
    if ((scenario == CTRL_STD && id == 100) ||
        (scenario == CTRL_MIN && id != 100)) {
        h->error = -ENOSPC;
        return NULL;
    }
    return &dummy_ctrl;
}
static struct v4l2_ctrl *v4l2_ctrl_new_std_menu(struct v4l2_ctrl_handler *h,
        const void *ops, u32 id, u32 max, u32 mask, u32 value)
{
    CHECK(h->live);
    if (scenario == CTRL_MENU) { h->error = -ENOSPC; return NULL; }
    return &dummy_ctrl;
}
static struct iris_inst *get_instance(void)
{
    if (scenario == FACTORY_ERR) return ERR_PTR(-EOVERFLOW);
    if (scenario == FACTORY_NULL) return NULL;
    last_inst = allocate(sizeof(*last_inst), INSTANCE);
    return last_inst;
}
static struct iris_core *video_drvdata(struct file *f) { return f->core; }
static struct video_device *video_devdata(struct file *f) { return f->vdev; }
static int pm_runtime_resume_and_get(void *dev)
{
    if (scenario == PM_GET) return -EHOSTDOWN;
    pm_held++;
    return 0;
}
static int iris_core_init(struct iris_core *c)
{
    return scenario == CORE_INIT ? -EIO : 0;
}
static void iris_core_pm_put(struct iris_core *c) { CHECK(pm_held == 1); pm_held--; }
static void v4l2_fh_init(struct v4l2_fh *fh, struct video_device *dev)
{
    CHECK(!fh->initialized); fh->initialized = 1; fh_inits++;
}
static void v4l2_fh_add(struct v4l2_fh *fh, struct file *f)
{
    CHECK(fh->initialized && !fh->added && !f->private_data);
    fh->added = 1; f->private_data = fh;
}
static void v4l2_fh_del(struct v4l2_fh *fh, struct file *f)
{
    CHECK(fh->added && f->private_data == fh);
    fh->added = 0; f->private_data = NULL;
}
static void v4l2_fh_exit(struct v4l2_fh *fh)
{
    CHECK(fh->initialized && !fh->added && !fh->ctrl_handler);
    fh->initialized = 0; fh_exits++;
}
static struct v4l2_m2m_dev *v4l2_m2m_init(const void *ops)
{
    if (scenario == M2M_DEV_ERR) return ERR_PTR(-EAGAIN);
    if (scenario == M2M_DEV_NULL) return NULL;
    return allocate(sizeof(struct v4l2_m2m_dev), M2M_DEV);
}
/* Queue initialization/vb2 internals are deliberately outside this local test. */
static int iris_m2m_queue_init(void *p, struct vb2_queue *src, struct vb2_queue *dst)
{
    return 0;
}
static struct v4l2_m2m_ctx *v4l2_m2m_ctx_init(struct v4l2_m2m_dev *dev,
        void *priv, int (*init)(void *, struct vb2_queue *, struct vb2_queue *))
{
    CHECK(m2m_dev_live == 1 && !IS_ERR_OR_NULL(dev));
    if (scenario == M2M_CTX_ERR) return ERR_PTR(-ENOSPC);
    if (scenario == M2M_CTX_NULL) return NULL;
    return allocate(sizeof(struct v4l2_m2m_ctx), M2M_CTX);
}
static void v4l2_m2m_ctx_release(struct v4l2_m2m_ctx *ctx)
{
    CHECK(m2m_dev_live == 1);
    retire(ctx, M2M_CTX); ctx_releases++;
}
static void v4l2_m2m_release(struct v4l2_m2m_dev *dev)
{
    CHECK(m2m_ctx_live == 0);
    retire(dev, M2M_DEV); dev_releases++;
}
static int iris_add_session(struct iris_inst *inst)
{
    CHECK(inst->fmt_src && inst->fmt_dst && ctrl_live == 1);
    CHECK(m2m_ctx_live == 1 && m2m_dev_live == 1);
    publish_calls++;
    if (scenario == PUBLICATION) return -ESHUTDOWN;
    published++;
    return 0;
}
/* Encoder implementation is outside scope; only successful-init publication
 * cleanup is checked using an explicit ownership-compatible encoder seam. */
static int iris_venc_inst_init(struct iris_inst *inst)
{
    inst->fmt_src = allocate(sizeof(*inst->fmt_src), FORMAT);
    inst->fmt_dst = allocate(sizeof(*inst->fmt_dst), FORMAT);
    CHECK(v4l2_ctrl_handler_init(&inst->ctrl_handler, 1) == 0);
    return 0;
}
static void iris_venc_inst_deinit(struct iris_inst *inst)
{
    kfree(inst->fmt_src); inst->fmt_src = NULL;
    kfree(inst->fmt_dst); inst->fmt_dst = NULL;
}
void iris_vdec_inst_deinit(struct iris_inst *inst);
'''

TESTS = r'''
static void reset(enum failure failure, const char *name)
{
    for (int i = 0; i < nallocated; i++) free(allocated[i].ptr);
    memset(allocated, 0, sizeof(allocated));
    nallocated = fmt_attempts = fmt_live = instance_live = m2m_dev_live = 0;
    m2m_ctx_live = ctrl_live = ctrl_inits = ctrl_frees = dev_releases = 0;
    ctx_releases = fh_inits = fh_exits = locks_live = pm_held = 0;
    publish_calls = published = 0;
    scenario = failure; case_name = name; last_inst = NULL;
    memset(&core, 0, sizeof(core));
    platform.get_instance = get_instance;
    core.iris_platform_data = &platform;
    core.vdev_dec = &decoder; core.vdev_enc = &encoder;
    core.inst_fw_caps_dec[1].cap_id = 1;
    core.inst_fw_caps_dec[1].flags = menu_cap ? CAP_FLAG_MENU : 0;
}
static void no_leaks(void)
{
    CHECK(fmt_live == 0 && instance_live == 0 && m2m_dev_live == 0);
    CHECK(m2m_ctx_live == 0 && ctrl_live == 0 && locks_live == 0 && pm_held == 0);
    for (int i = 0; i < nallocated; i++) CHECK(!allocated[i].live);
}
static void decoder_case(enum failure failure, const char *name, int expected,
        int attempts, int controls, int frees)
{
    menu_cap = failure == CTRL_MENU;
    reset(failure, name);
    struct iris_inst inst = { .core = &core, .domain = DECODER };
    CHECK(iris_vdec_inst_init(&inst) == expected);
    CHECK(fmt_attempts == attempts && ctrl_inits == controls && ctrl_frees == frees);
    if (!expected) {
        CHECK(inst.fmt_src && inst.fmt_dst && fmt_live == 2 && ctrl_live == 1);
        CHECK(inst.codec == V4L2_PIX_FMT_H264 && inst.fw_min_count == MIN_BUFFERS);
        CHECK(inst.fmt_dst->fmt.pix_mp.pixelformat == V4L2_PIX_FMT_NV12);
        CHECK(inst.fw_caps[1].cap_id == 1);
        v4l2_ctrl_handler_free(&inst.ctrl_handler);
    } else {
        CHECK(!inst.fmt_src && !inst.fmt_dst);
    }
    iris_vdec_inst_deinit(&inst);
    iris_vdec_inst_deinit(&inst);
    CHECK(!inst.fmt_src && !inst.fmt_dst);
    no_leaks();
}
static void open_case(enum failure failure, const char *name, int expected,
        int domain, int controls, int frees, int devs, int contexts, int publications)
{
    menu_cap = failure == CTRL_MENU;
    reset(failure, name);
    struct file f = { .core = &core, .vdev = domain == DECODER ? &decoder : &encoder };
    CHECK(iris_open_owned(&f) == expected);
    CHECK(ctrl_inits == controls && ctrl_frees == frees);
    CHECK(dev_releases == devs && ctx_releases == contexts);
    CHECK(publish_calls == publications && published == !expected);
    if (!expected) {
        CHECK(f.private_data && f.private_data->m2m_ctx == last_inst->m2m_ctx);
        CHECK(fmt_live == 2 && ctrl_live == 1 && instance_live == 1);
        /* Success cleanup is harness-owned, not a substituted actual close. */
        v4l2_ctrl_handler_free(&last_inst->ctrl_handler);
        if (domain == DECODER) {
            iris_vdec_inst_deinit(last_inst);
            iris_vdec_inst_deinit(last_inst);
        } else {
            iris_venc_inst_deinit(last_inst);
        }
        v4l2_m2m_ctx_release(last_inst->m2m_ctx);
        v4l2_m2m_release(last_inst->m2m_dev);
        iris_v4l2_fh_deinit(last_inst, &f);
        mutex_destroy(&last_inst->ctx_q_lock);
        mutex_destroy(&last_inst->lock);
        kfree(last_inst);
    } else if (last_inst && domain == DECODER) {
        CHECK(!last_inst->fmt_src && !last_inst->fmt_dst);
    }
    CHECK(!f.private_data && fh_inits == fh_exits);
    no_leaks();
}
int main(void)
{
    decoder_case(SRC_ALLOC, "decoder/src-alloc", -ENOMEM, 1, 0, 0);
    decoder_case(DST_ALLOC, "decoder/dst-alloc", -ENOMEM, 2, 0, 0);
    decoder_case(CTRL_INIT, "decoder/ctrl-handler-init", -ENOMEM, 2, 1, 0);
    decoder_case(CTRL_STD, "decoder/ctrl-std", -EINVAL, 2, 1, 1);
    decoder_case(CTRL_MENU, "decoder/ctrl-menu", -EINVAL, 2, 1, 1);
    decoder_case(CTRL_MIN, "decoder/ctrl-minbuffers", -ENOSPC, 2, 1, 1);
    decoder_case(NONE, "decoder/success-and-idempotent-deinit", 0, 2, 1, 0);
    open_case(FACTORY_ERR, "open/factory-err", -EOVERFLOW, DECODER, 0, 0, 0, 0, 0);
    open_case(FACTORY_NULL, "open/factory-null", -ENOMEM, DECODER, 0, 0, 0, 0, 0);
    open_case(M2M_DEV_ERR, "open/m2m-dev-err", -EAGAIN, DECODER, 0, 0, 0, 0, 0);
    open_case(M2M_DEV_NULL, "open/m2m-dev-null", -ENOMEM, DECODER, 0, 0, 0, 0, 0);
    open_case(M2M_CTX_ERR, "open/m2m-ctx-err", -ENOSPC, DECODER, 0, 0, 1, 0, 0);
    open_case(M2M_CTX_NULL, "open/m2m-ctx-null", -ENOMEM, DECODER, 0, 0, 1, 0, 0);
    open_case(SRC_ALLOC, "open/src-alloc", -ENOMEM, DECODER, 0, 0, 1, 1, 0);
    open_case(DST_ALLOC, "open/dst-alloc", -ENOMEM, DECODER, 0, 0, 1, 1, 0);
    open_case(CTRL_INIT, "open/ctrl-handler-init", -ENOMEM, DECODER, 1, 0, 1, 1, 0);
    open_case(CTRL_STD, "open/ctrl-std", -EINVAL, DECODER, 1, 1, 1, 1, 0);
    open_case(CTRL_MENU, "open/ctrl-menu", -EINVAL, DECODER, 1, 1, 1, 1, 0);
    open_case(CTRL_MIN, "open/ctrl-minbuffers", -ENOSPC, DECODER, 1, 1, 1, 1, 0);
    open_case(PUBLICATION, "open/decoder-last-publication", -ESHUTDOWN, DECODER, 1, 1, 1, 1, 1);
    open_case(PUBLICATION, "open/encoder-seam-last-publication", -ESHUTDOWN, ENCODER, 1, 1, 1, 1, 1);
    open_case(PM_GET, "open/pm-failure", -EHOSTDOWN, DECODER, 0, 0, 0, 0, 0);
    open_case(CORE_INIT, "open/core-init-failure", -EIO, DECODER, 0, 0, 0, 0, 0);
    open_case(NONE, "open/decoder-success", 0, DECODER, 1, 0, 0, 0, 1);
    reset(NONE, "finished");
    return 0;
}
'''


def replace_once(body, old, new):
    if body.count(old) != 1:
        raise AssertionError(f"mutation anchor is not unique: {old!r}")
    return body.replace(old, new, 1)


def run(root, sanitize=False):
    sources = {name: (root / (name + ".c")).read_text(encoding="utf-8")
               for name in ("iris_vdec", "iris_vidc", "iris_ctrls")}
    bodies = {}
    for source, names in (
        ("iris_vdec", ("iris_vdec_inst_init", "iris_vdec_inst_deinit")),
        ("iris_ctrls", ("iris_ctrls_init",)),
        ("iris_vidc", ("iris_v4l2_fh_init", "iris_v4l2_fh_deinit", "iris_open_owned")),
    ):
        for name in names:
            bodies[name] = extract(sources[source], name)
    order = ("iris_ctrls_init", "iris_vdec_inst_init", "iris_vdec_inst_deinit",
             "iris_v4l2_fh_init", "iris_v4l2_fh_deinit", "iris_open_owned")
    mutations = [
        ("src-allocation-guard", "iris_vdec_inst_init",
         "\tif (!inst->fmt_src)\n\t\tgoto fail_fmt;", ""),
        ("dst-allocation-guard", "iris_vdec_inst_init",
         "\tif (!inst->fmt_dst)\n\t\tgoto fail_fmt;", ""),
        ("ctrl-failure-format-unwind", "iris_vdec_inst_init",
         "\tif (ret)\n\t\tgoto fail_fmt;", ""),
        ("src-deinit-null", "iris_vdec_inst_deinit", "\tinst->fmt_src = NULL;", ""),
        ("dst-deinit-null", "iris_vdec_inst_deinit", "\tinst->fmt_dst = NULL;", ""),
        ("failed-ctrl-doublefree", "iris_vdec_inst_init",
         "\tiris_vdec_inst_deinit(inst);", "\tv4l2_ctrl_handler_free(&inst->ctrl_handler);\n\tiris_vdec_inst_deinit(inst);"),
        ("actual-ctrl-error-free", "iris_ctrls_init",
         "\tv4l2_ctrl_handler_free(&inst->ctrl_handler);", ""),
        ("factory-error-preservation", "iris_open_owned",
         "return inst ? PTR_ERR(inst) : -ENOMEM;", "return -EINVAL;"),
        ("factory-null-errno", "iris_open_owned",
         "return inst ? PTR_ERR(inst) : -ENOMEM;", "return inst ? PTR_ERR(inst) : -EINVAL;"),
        ("m2m-dev-error-preservation", "iris_open_owned",
         "ret = inst->m2m_dev ? PTR_ERR(inst->m2m_dev) : -ENOMEM;", "ret = -EINVAL;"),
        ("m2m-dev-null-errno", "iris_open_owned",
         "ret = inst->m2m_dev ? PTR_ERR(inst->m2m_dev) : -ENOMEM;",
         "ret = inst->m2m_dev ? PTR_ERR(inst->m2m_dev) : -EINVAL;"),
        ("m2m-ctx-error-preservation", "iris_open_owned",
         "ret = inst->m2m_ctx ? PTR_ERR(inst->m2m_ctx) : -ENOMEM;", "ret = -EINVAL;"),
        ("m2m-ctx-null-errno", "iris_open_owned",
         "ret = inst->m2m_ctx ? PTR_ERR(inst->m2m_ctx) : -ENOMEM;",
         "ret = inst->m2m_ctx ? PTR_ERR(inst->m2m_ctx) : -EINVAL;"),
        ("publication-control-free", "iris_open_owned",
         "\tv4l2_ctrl_handler_free(&inst->ctrl_handler);", ""),
        ("publication-format-free", "iris_open_owned",
         "\t\tiris_vdec_inst_deinit(inst);", "\t\t(void)0;"),
        ("m2m-ctx-free", "iris_open_owned", "\tv4l2_m2m_ctx_release(inst->m2m_ctx);", ""),
        ("m2m-dev-free", "iris_open_owned", "\tv4l2_m2m_release(inst->m2m_dev);", ""),
        ("publication-double-control-free", "iris_open_owned",
         "\tv4l2_ctrl_handler_free(&inst->ctrl_handler);",
         "\tv4l2_ctrl_handler_free(&inst->ctrl_handler);\n\tv4l2_ctrl_handler_free(&inst->ctrl_handler);"),
    ]
    cases = len(re.findall(r"(?m)^    (?:decoder_case|open_case)\(", TESTS))
    if cases != 24 or len(mutations) != 18:
        raise AssertionError("original 24 cases and 18 mutation controls must remain intact")
    compiler = shutil.which("cc")
    if compiler is None:
        raise RuntimeError("existing native cc required; no installation attempted")
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    environment = os.environ.copy()
    if sanitize:
        environment["ASAN_OPTIONS"] = "detect_leaks=1:halt_on_error=1:abort_on_error=1"
        environment["UBSAN_OPTIONS"] = "halt_on_error=1:print_stacktrace=1"
    with tempfile.TemporaryDirectory(prefix="iris-instance-unwind-") as tmp:
        work = Path(tmp)

        def compile_run(label, functions):
            code = work / (label + ".c")
            binary = work / label
            code.write_text(PREAMBLE + "\n".join(functions[n] for n in order) + TESTS,
                            encoding="utf-8")
            command = [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                       "-Wno-unused-parameter", "-Wno-unused-label", "-O0", "-g"]
            if sanitize:
                command += ["-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                            "-fno-sanitize-recover=all", "-fno-pie", "-no-pie"]
            command += [str(code), "-o", str(binary)]
            built = subprocess.run(command, cwd=work, text=True, capture_output=True,
                                   timeout=30)
            if built.returncode:
                raise AssertionError(f"{label}: MUST compile successfully\n{built.stderr}")
            return subprocess.run([str(binary)], cwd=work, text=True,
                                  capture_output=True, timeout=10, env=environment)

        baseline = compile_run("baseline", bodies)
        if baseline.returncode:
            raise AssertionError("actual-source baseline failed\n" + baseline.stderr)
        if sanitize and any(marker in baseline.stderr for marker in
                            ("AddressSanitizer", "runtime error:", "LeakSanitizer")):
            raise AssertionError("baseline sanitizer diagnostic\n" + baseline.stderr)
        for label, name, old, new in mutations:
            changed = dict(bodies)
            changed[name] = replace_once(changed[name], old, new)
            result = compile_run(label, changed)
            if result.returncode == 0:
                raise AssertionError(f"mutant survived runtime oracle: {label}")
            evidence = result.stderr.strip().splitlines()
            detail = evidence[0] if evidence else f"signal {-result.returncode}"
            print(f"KILLED (compiled): {label}: {detail}")
        if sanitize:
            print(f"{cases} instance-init/open ASan/UBSan scenarios passed; "
                  f"{len(mutations)} sanitized compiling mutations rejected")
        else:
            print(f"Actual-C: {cases} passed, 0 failed, {cases} total; "
                  f"oracle controls: {len(mutations)} passed, 0 failed")
        print("Scope: extracted local unwind + declared framework seams; no full-module/device proof")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True,
                        help="candidate source directory; no default repository source")
    parser.add_argument("--sanitize", action="store_true",
                        help="run once with ASan/UBSan instead of normal mode")
    args = parser.parse_args()
    root = args.work.resolve()
    if not root.is_dir():
        parser.error("--work must name an existing candidate directory")
    run(root, sanitize=args.sanitize)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
