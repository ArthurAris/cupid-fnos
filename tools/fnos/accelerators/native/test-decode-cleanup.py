#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline actual-C NV12/DRC, internal-buffer and core/PM cleanup checks.

Run with /usr/bin/python3 -I. Default inputs are prepared by the pinned
build.py.prepare_iris (currently 0001..0006) in a NEW /var/tmp directory.
--work accepts an already prepared baseline. Never builds/loads a module.
Nonzero safety assertions remain FAIL, not expectedFailure or a repaired model.
HFI ops, hardware resource callbacks and Linux framework effects are seams;
all selected driver functions are extracted verbatim, including their callers.
This is bounded host control-flow coverage, NOT firmware/hardware acceptance.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C'}


def masked(text):
    # Preserve offsets while excluding braces in comments, strings and chars.
    return re.sub(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'',
                  lambda m: ''.join('\n' if c == '\n' else ' ' for c in m.group()),
                  text, flags=re.S)


def block(text, pattern, label, semicolon=False):
    clean = masked(text)
    hits = list(re.finditer(pattern, clean, re.M))
    if len(hits) != 1:
        raise ValueError('Missing/ambiguous actual definition: ' + label)
    start = hits[0].start()
    opening = clean.index('{', start)
    depth, end = 1, opening + 1
    while depth and end < len(clean):
        depth += (clean[end] == '{') - (clean[end] == '}')
        end += 1
    if depth:
        raise ValueError('Unterminated actual definition: ' + label)
    if semicolon:
        if not re.match(r'\s*;', clean[end:]):
            raise ValueError('Missing aggregate terminator: ' + label)
        end += re.match(r'\s*;', clean[end:]).end()
    return text[start:end] + '\n'


def definition(text, name):
    return block(text, r'^(?:static\s+)?(?:inline\s+)?(?:int|void|bool|u32)\s+' +
                 re.escape(name) + r'\s*\([^;{}]*\)\s*\{', name)


def aggregate(text, name):
    return block(text, r'^(?:struct|enum)\s+' + re.escape(name) + r'\s*\{', name, True)


PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <linux/videodev2.h>
typedef uint32_t u32; typedef uint64_t u64; typedef uintptr_t dma_addr_t;
#define BIT(n) (1U << (n))
typedef int spinlock_t;
#define spin_lock_init(lock) (*(lock)=0)
#define spin_lock_irqsave(lock, flags) ((void)(lock),(flags)=0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock),(void)(flags))
#define ALIGN(n,a) (((n)+(a)-1)/(a)*(a))
#define DIV_ROUND_UP(n,d) (((n)+(d)-1)/(d))
#define NUM_MBS_PER_FRAME(h,w) (DIV_ROUND_UP(h,16)*DIV_ROUND_UP(w,16))
#define max(a,b) ((a)>(b)?(a):(b))
#define min_t(t,a,b) ((t)(a)<(t)(b)?(t)(a):(t)(b))
#define GFP_KERNEL 0
#define DMA_ATTR_WRITE_COMBINE 1UL
#define DMA_ATTR_NO_KERNEL_MAPPING 2UL
#define dev_dbg(d,...) ((void)(d))
#define dev_err(d,...) ((void)(d))
#define MIN_BUFFERS 4
#ifndef V4L2_PIX_FMT_QC08C
#define V4L2_PIX_FMT_QC08C v4l2_fourcc('Q','0','8','C')
#endif
#ifndef V4L2_PIX_FMT_QC10C
#define V4L2_PIX_FMT_QC10C v4l2_fourcc('Q','1','0','C')
#endif
#define IRIS_INST_MAX_SUB_STATE_VALUE 255
#define DEFAULT_FPS 30
#define BIT_DEPTH_8 ((8U << 16) | 8U)
struct list_head { struct list_head *next, *prev; };
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define list_last_entry(h,t,m) container_of((h)->prev,t,m)
static void list_add_tail(struct list_head *n, struct list_head *h) {
 n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;
}
static void list_del(struct list_head *n) {n->prev->next=n->next;n->next->prev=n->prev;}
static bool list_empty(const struct list_head *h) {return h->next==h;}
/* No sentinel container pointer: the same list ordering/removal semantics,
 * with portable host iteration, also defined under ASan/UBSan. */
#define list_for_each_entry_safe(p,n,h,m) \
 for(struct list_head *_it=(h)->next,*_n=NULL; \
 _it!=(h) && ((_n=_it->next),((p)=container_of(_it,__typeof__(*(p)),m)), \
 ((n)=_n==(h)?NULL:container_of(_n,__typeof__(*(n)),m)),(void)(n),1); _it=_n)
#define list_for_each_entry(p,h,m) \
 for(struct list_head *_it=(h)->next;_it!=(h) && \
 ((p)=container_of(_it,__typeof__(*(p)),m),1);_it=_it->next)
struct vb2_buffer {u32 type;};
struct vb2_v4l2_buffer {struct vb2_buffer vb2_buf;};
struct vb2_queue {bool streaming,last;u32 min_reqbufs_allocation;};
struct v4l2_m2m_buffer {struct vb2_v4l2_buffer vb;struct list_head list;};
struct v4l2_m2m_ctx {struct vb2_queue src,dst;struct list_head input,output;};
#define v4l2_m2m_for_each_src_buf_safe(c,p,n) list_for_each_entry_safe(p,n,&(c)->input,list)
#define v4l2_m2m_for_each_dst_buf_safe(c,p,n) list_for_each_entry_safe(p,n,&(c)->output,list)
#define to_iris_buffer(p) container_of(p,struct iris_buffer,vb2)
struct v4l2_ctrl {int value;};
struct iris_inst;
'''

TYPES = r'''
struct iris_hfi_packet {u32 port;};
struct vpu_ops {u64 (*calc_freq)(struct iris_inst *,size_t);};
struct iris_platform_data {
 bool no_opb,prototype_restricted;u32 hw_response_timeout;
 const u32 *dec_ip_int_buf_tbl,*dec_op_int_buf_tbl,*enc_ip_int_buf_tbl,*enc_op_int_buf_tbl;
 u32 dec_ip_int_buf_tbl_size,dec_op_int_buf_tbl_size,enc_ip_int_buf_tbl_size,enc_op_int_buf_tbl_size;
 u32 (*get_vpu_buffer_size)(struct iris_inst *,enum iris_buffer_type);
 const struct vpu_ops *vpu_ops;const struct bw_info *bw_tbl_dec;u32 bw_tbl_dec_size;
};
struct iris_core;
struct iris_hfi_command_ops {
 int (*sys_init)(struct iris_core *),(*sys_image_version)(struct iris_core *),(*sys_interframe_powercollapse)(struct iris_core *);
 int (*session_queue_buf)(struct iris_inst *,struct iris_buffer *);
 int (*session_release_buf)(struct iris_inst *,struct iris_buffer *);
 int (*session_set_config_params)(struct iris_inst *,u32);
 int (*session_start)(struct iris_inst *,u32),(*session_stop)(struct iris_inst *,u32);
 int (*session_resume_drc)(struct iris_inst *,u32),(*session_resume_drain)(struct iris_inst *,u32);
 int (*session_close)(struct iris_inst *);
 int (*session_set_property)(struct iris_inst *,u32,u32,u32,u32,void *,u32);
};
struct iris_core {
 void *dev;int lock,core_init_done;enum iris_core_state state;
 const struct iris_platform_data *iris_platform_data;const struct iris_hfi_command_ops *hfi_ops;
 struct list_head instances;struct iris_core_power power;struct iris_hfi_pending init_pending;
};
struct fw_cap {u32 value,hfi_id,flags;};
struct iris_inst {
 struct iris_core *core;int domain;enum iris_inst_state state;enum iris_inst_sub_state sub_state;
 struct iris_buffers buffers[BUF_TYPE_MAX];struct list_head list;
 struct v4l2_format *fmt_src,*fmt_dst;struct v4l2_m2m_ctx *m2m_ctx;
 bool last_buffer_dequeued;u32 session_id,codec,fw_min_count;
 struct iris_hfi_pending control_pending;
 struct v4l2_rect crop;struct fw_cap fw_caps[64];struct v4l2_ctrl ctrl_handler;int fh;
 struct iris_inst_power power;struct icc_vote_data icc_data;size_t max_input_data_size;
};
struct iris_inst_hfi_gen2 {struct iris_inst inst;struct hfi_subscription_params src_subcr_params;};
int iris_destroy_internal_buffer(struct iris_inst *inst, struct iris_buffer *buffer);
#define to_iris_inst_hfi_gen2(p) container_of(p,struct iris_inst_hfi_gen2,inst)
enum {DECODER=1,ENCODER=2};
/* Framework and hardware-effect seams. Every event log has a hard bound.
 * A failed acquisition allocates no resource; successful puts undo one get.
 * HFI operation seams return injected errno, never update driver states/flags.
 */
enum event {QI=1,PON,FWLOAD,BOOT,SYS,VERSION,PC,WAIT,FWUNLOAD,POFF,QD,
 PMGET,PMPUT,PMAUTO,CLOCK,ICC,CONFIG,QUEUE,RELEASE,STAGE_PROP,PIPE_PROP,
 RESUME_IP,RESUME_OP,START,STOP,CLOSE,NOTIFY};
static int logbuf[128],nlog,fail_event,fail_n=1,event_count[64],fail_errno=-EIO;
static int live_queue,live_power,live_fw,pm_refs,heap_live,dma_live;
static int alloc_n,dma_n,alloc_fail,dma_fail,failures,suspend_flag=1;
static struct iris_core *active_core;
static int effect(int e) {assert(nlog<128);logbuf[nlog++]=e;event_count[e]++;
 return e==fail_event && event_count[e]==fail_n?fail_errno:0;}
static void check(bool ok,int line,const char *expr) {
 if(!ok){if(failures<8)fprintf(stderr,"line %d: %s\n",line,expr);failures++;}
}
#define CHECK(c) check((c),__LINE__,#c)
static void mutex_lock(int *p){assert(*p==0);*p=1;}
static void mutex_unlock(int *p){assert(*p==1);*p=0;}
static void reinit_completion(int *p){*p=0;}
static unsigned long msecs_to_jiffies(u32 n){return n;}
static int wait_for_completion_timeout(int *p,unsigned long t){(void)p;assert(t>0);
 int r=effect(WAIT);if(r==-ETIMEDOUT)return 0;if(r)active_core->state=IRIS_CORE_ERROR;return 1;}
static int iris_hfi_queues_init(struct iris_core *c){(void)c;int r=effect(QI);if(!r)live_queue++;return r;}
static void iris_hfi_queues_deinit(struct iris_core *c){(void)c;effect(QD);assert(live_queue>0);live_queue--;}
static int iris_vpu_power_on(struct iris_core *c){(void)c;int r=effect(PON);if(!r)live_power++;return r;}
static void iris_vpu_power_off(struct iris_core *c){(void)c;effect(POFF);assert(live_power>0);live_power--;}
static int iris_fw_load(struct iris_core *c){(void)c;int r=effect(FWLOAD);if(!r)live_fw++;return r;}
static void iris_fw_unload(struct iris_core *c){(void)c;effect(FWUNLOAD);assert(live_fw>0);live_fw--;}
static int iris_vpu_boot_firmware(struct iris_core *c){(void)c;return effect(BOOT);}
static int pm_runtime_resume_and_get(void *d){(void)d;int r=effect(PMGET);if(!r)pm_refs++;return r;}
static void pm_runtime_put_sync(void *d){(void)d;effect(PMPUT);pm_refs--;}
static void pm_runtime_put_autosuspend(void *d){(void)d;effect(PMAUTO);pm_refs--;}
static bool pm_runtime_suspended(void *d){(void)d;return suspend_flag;}
static int dev_pm_opp_set_rate(void *d,u64 f){(void)d;(void)f;return effect(CLOCK);}
static int iris_set_icc_bw(struct iris_core *c,u64 b){(void)c;(void)b;return effect(ICC);}
static void *kzalloc(size_t s,int g){(void)g;if(++alloc_n==alloc_fail)return NULL;
 void *p=calloc(1,s);assert(p);heap_live++;return p;}
static void kfree(void *p){assert(p);heap_live--;free(p);}
static void *dma_alloc_attrs(void *d,size_t s,dma_addr_t *a,int g,unsigned long attrs){
 (void)d;(void)g;assert(s>0);assert(attrs==(DMA_ATTR_WRITE_COMBINE|DMA_ATTR_NO_KERNEL_MAPPING));
 if(++dma_n==dma_fail){return NULL;}void *p=malloc(s);assert(p);*a=(uintptr_t)p;dma_live++;return p;}
static void dma_free_attrs(void *d,size_t s,void *p,dma_addr_t a,unsigned long attrs){
 (void)d;assert(s>0);assert(a==(uintptr_t)p);assert(attrs==3);assert(p);dma_live--;free(p);}
static struct vb2_queue *v4l2_m2m_get_src_vq(struct v4l2_m2m_ctx *c){return &c->src;}
static struct vb2_queue *v4l2_m2m_get_dst_vq(struct v4l2_m2m_ctx *c){return &c->dst;}
static bool vb2_is_streaming(struct vb2_queue *q){return q->streaming;}
static void vb2_clear_last_buffer_dequeued(struct vb2_queue *q){q->last=false;}
static struct v4l2_ctrl *v4l2_ctrl_find(struct v4l2_ctrl *h,u32 id){(void)id;return h;}
static void v4l2_ctrl_s_ctrl(struct v4l2_ctrl *c,int n){c->value=n;}
static void v4l2_event_queue_fh(int *fh,struct v4l2_event *e){(void)fh;
 assert(e->type==V4L2_EVENT_SOURCE_CHANGE);assert(e->u.src_change.changes==V4L2_EVENT_SRC_CH_RESOLUTION);effect(NOTIFY);}
/* No client buffers are inserted into this bounded fixture. Reaching the
 * unextracted client-buffer completion path is a fixture error, not success. */
static int iris_vb2_buffer_done(struct iris_inst *i,struct iris_buffer *b){(void)i;(void)b;abort();}
static int sys_init(struct iris_core *c){(void)c;return effect(SYS);}
static int sys_version(struct iris_core *c){(void)c;return effect(VERSION);}
static int sys_pc(struct iris_core *c){(void)c;return effect(PC);}
static int queue_op(struct iris_inst *i,struct iris_buffer *b){(void)i;assert(b->kvaddr);return effect(QUEUE);}
static int release_op(struct iris_inst *i,struct iris_buffer *b){(void)i;assert(b->kvaddr);return effect(RELEASE);}
static int config_op(struct iris_inst *i,u32 p){(void)i;assert(V4L2_TYPE_IS_CAPTURE(p));return effect(CONFIG);}
static int start_op(struct iris_inst *i,u32 p){(void)i;assert(V4L2_TYPE_IS_CAPTURE(p));return effect(START);}
static int stop_op(struct iris_inst *i,u32 p){(void)i;(void)p;return effect(STOP);}
static int close_op(struct iris_inst *i){(void)i;return effect(CLOSE);}
static int resume_op(struct iris_inst *i,u32 p){(void)i;return effect(V4L2_TYPE_IS_OUTPUT(p)?RESUME_IP:RESUME_OP);}
static int property_op(struct iris_inst *i,u32 id,u32 f,u32 p,u32 type,void *v,u32 s){
 (void)i;(void)f;(void)p;(void)type;assert(v && s==sizeof(u32));return effect(id==100?STAGE_PROP:PIPE_PROP);}
/* Platform callbacks: supply nonzero allocation sizes and frequency, no control logic. */
static u32 size_op(struct iris_inst *i,enum iris_buffer_type t){(void)i;(void)t;return 64;}
static u64 freq_op(struct iris_inst *i,size_t n){(void)i;return n*100;}
'''

MAIN = r'''
static const u32 ip_tbl[]={BUF_BIN,BUF_COMV},op_tbl[]={BUF_DPB};
static const struct bw_info bandwidth[]={{100000,1},{0,1}};
static const struct vpu_ops vops={.calc_freq=freq_op};
static const struct iris_hfi_command_ops hops={.sys_init=sys_init,.sys_image_version=sys_version,
 .sys_interframe_powercollapse=sys_pc,.session_queue_buf=queue_op,.session_release_buf=release_op,
 .session_set_config_params=config_op,.session_start=start_op,.session_stop=stop_op,
 .session_close=close_op,.session_resume_drc=resume_op,.session_resume_drain=resume_op,
 .session_set_property=property_op};
struct fixture {struct iris_core c;struct iris_inst_hfi_gen2 g;struct iris_platform_data p;
 struct v4l2_format src,dst;struct v4l2_m2m_ctx ctx;struct iris_buffer input;struct v4l2_m2m_buffer minput;};
static void init(struct fixture *f){memset(f,0,sizeof(*f));
 f->p=(struct iris_platform_data){.prototype_restricted=true,.hw_response_timeout=10,
 .dec_ip_int_buf_tbl=ip_tbl,.dec_ip_int_buf_tbl_size=2,.dec_op_int_buf_tbl=op_tbl,.dec_op_int_buf_tbl_size=1,
 .enc_ip_int_buf_tbl=ip_tbl,.enc_ip_int_buf_tbl_size=2,.enc_op_int_buf_tbl=op_tbl,.enc_op_int_buf_tbl_size=1,
 .get_vpu_buffer_size=size_op,.vpu_ops=&vops,.bw_tbl_dec=bandwidth,.bw_tbl_dec_size=2};
 f->c.iris_platform_data=&f->p;f->c.hfi_ops=&hops;active_core=&f->c;
 INIT_LIST_HEAD(&f->c.instances);INIT_LIST_HEAD(&f->ctx.input);INIT_LIST_HEAD(&f->ctx.output);
 struct iris_inst *i=&f->g.inst;i->core=&f->c;i->domain=DECODER;i->state=IRIS_INST_INPUT_STREAMING;
 i->fmt_src=&f->src;i->fmt_dst=&f->dst;i->m2m_ctx=&f->ctx;i->codec=V4L2_PIX_FMT_H264;i->session_id=1;
 f->src.fmt.pix_mp.width=1920;f->src.fmt.pix_mp.height=1080;f->dst.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_NV12;
 f->ctx.src.streaming=true;i->fw_min_count=2;i->last_buffer_dequeued=true;f->ctx.dst.last=true;
 i->fw_caps[STAGE].hfi_id=100;i->fw_caps[PIPE].hfi_id=101;i->fw_caps[STAGE].flags=CAP_FLAG_INPUT_PORT;
 i->fw_caps[PIPE].flags=CAP_FLAG_INPUT_PORT;i->fw_caps[PIPE].value=1;
 for(int b=0;b<BUF_TYPE_MAX;b++)INIT_LIST_HEAD(&i->buffers[b].list);
 list_add_tail(&i->list,&f->c.instances);
 f->g.src_subcr_params=(struct hfi_subscription_params){.bitstream_resolution=(1920U<<16)|1080,
 .bit_depth=BIT_DEPTH_8,.coded_frames=HFI_BITMASK_FRAME_MBS_ONLY_FLAG,.fw_min_count=2};
}
static int count(struct iris_inst *i,int type){int n=0;struct iris_buffer *b;
 list_for_each_entry(b,&i->buffers[type].list,list){(void)b;n++;}return n;}
static struct iris_buffer *nth(struct iris_inst *i,int type,int index){struct iris_buffer *b;
 list_for_each_entry(b,&i->buffers[type].list,list){if(!index--)return b;}return NULL;}
static void cleanup(struct fixture *f){struct iris_inst *i=&f->g.inst;
 for(int t=0;t<BUF_TYPE_MAX;t++){struct iris_buffer *b,*n;
 list_for_each_entry_safe(b,n,&i->buffers[t].list,list)iris_destroy_internal_buffer(i,b);}
 CHECK(heap_live==0);CHECK(dma_live==0);CHECK(f->c.lock==0);
}
static void trace(const int *expected,size_t n){CHECK(nlog==(int)n);
 for(size_t j=0;j<n && j<(size_t)nlog;j++)CHECK(logbuf[j]==expected[j]);}
#define TRACE(...) do{const int e[]={__VA_ARGS__};trace(e,sizeof(e)/sizeof(e[0]));}while(0)
static void make_buffers(struct iris_inst *i,int t,int n){i->buffers[t].size=64;i->buffers[t].min_count=n;
 for(int j=0;j<n;j++){int r=iris_create_internal_buffer(i,t,j);assert(r==0);}}
static void drc(struct iris_inst *i){i->sub_state=IRIS_INST_SUB_DRC|IRIS_INST_SUB_DRC_LAST|
 IRIS_INST_SUB_INPUT_PAUSE|IRIS_INST_SUB_OUTPUT_PAUSE;}
static int core_case(struct fixture *f,int mode){int r;
 if(mode<=6){const int events[]={QI,PON,FWLOAD,BOOT,SYS,VERSION,PC};fail_event=events[mode];
 r=iris_core_init(&f->c);CHECK(r==-EIO);CHECK(f->c.state==IRIS_CORE_DEINIT);
 CHECK(live_fw==0 && live_power==0 && live_queue==0);
 if(mode==0){TRACE(QI);}if(mode==1){TRACE(QI,PON,QD);}if(mode==2){TRACE(QI,PON,FWLOAD,POFF,QD);}
 if(mode==3)TRACE(QI,PON,FWLOAD,BOOT,FWUNLOAD,POFF,QD);
 if(mode==4)TRACE(QI,PON,FWLOAD,BOOT,SYS,FWUNLOAD,POFF,QD);
 if(mode==5)TRACE(QI,PON,FWLOAD,BOOT,SYS,VERSION,FWUNLOAD,POFF,QD);
 if(mode==6)TRACE(QI,PON,FWLOAD,BOOT,SYS,VERSION,PC,FWUNLOAD,POFF,QD);
 }else if(mode==7 || mode==8){fail_event=WAIT;fail_errno=mode==7?-ETIMEDOUT:-EIO;
 r=iris_core_init(&f->c);CHECK(r==fail_errno);CHECK(f->c.state==IRIS_CORE_DEINIT);
 CHECK(live_fw==0 && live_power==0 && live_queue==0);
 TRACE(QI,PON,FWLOAD,BOOT,SYS,VERSION,PC,WAIT,FWUNLOAD,POFF,QD);
 }else if(mode==9){r=iris_core_init(&f->c);CHECK(r==0);CHECK(f->c.state==IRIS_CORE_INIT);
 TRACE(QI,PON,FWLOAD,BOOT,SYS,VERSION,PC,WAIT);nlog=0;
 CHECK(iris_core_init(&f->c)==0);CHECK(nlog==0);iris_core_deinit(&f->c);
 TRACE(PMGET,FWUNLOAD,POFF,QD,PMPUT);CHECK(pm_refs==0);CHECK(f->c.state==IRIS_CORE_DEINIT);
 CHECK(live_fw==0 && live_power==0 && live_queue==0);
 }else if(mode==10){CHECK(iris_core_init(&f->c)==0);nlog=0;fail_event=PMGET;
 iris_core_deinit(&f->c);CHECK(pm_refs==0);CHECK(event_count[PMPUT]==0);
 CHECK(event_count[FWUNLOAD]==0 && event_count[POFF]==0 && event_count[QD]==0);
 CHECK(f->c.state==IRIS_CORE_INIT);
 }else if(mode==11){f->c.state=IRIS_CORE_ERROR;CHECK(iris_core_init(&f->c)==-EINVAL);
 CHECK(nlog==0);CHECK(f->c.state==IRIS_CORE_DEINIT);
 }else if(mode==12){f->c.state=IRIS_CORE_ERROR;CHECK(iris_wait_for_system_response(&f->c)==-EIO);CHECK(nlog==0);}
 return 0;}
static void buffer_case(struct fixture *f,int mode){struct iris_inst *i=&f->g.inst;struct iris_buffer *a,*b;int r;
 if(mode==0){CHECK(iris_create_internal_buffer(i,BUF_BIN,0)==0);CHECK(alloc_n==0 && dma_n==0);}
 else if(mode==1 || mode==2){i->buffers[BUF_BIN].size=64;alloc_fail=mode==1?1:0;dma_fail=mode==2?1:0;
 CHECK(iris_create_internal_buffer(i,BUF_BIN,0)==-ENOMEM);CHECK(count(i,BUF_BIN)==0);CHECK(heap_live==0 && dma_live==0);}
 else if(mode==3){make_buffers(i,BUF_BIN,2);a=nth(i,BUF_BIN,0);b=nth(i,BUF_BIN,1);
 CHECK(a && b);CHECK(a->index==0 && b->index==1);CHECK(a->type==BUF_BIN && a->buffer_size==64);
 CHECK(a->attr==0);CHECK(iris_destroy_internal_buffer(i,a)==0);CHECK(count(i,BUF_BIN)==1);CHECK(nth(i,BUF_BIN,0)==b);}
 else if(mode==4 || mode==5){make_buffers(i,BUF_BIN,1);a=nth(i,BUF_BIN,0);
 a->attr=BUF_ATTR_DEFERRED|BUF_ATTR_DEQUEUED;if(mode==5)fail_event=QUEUE;r=iris_queue_buffer(i,a);
 CHECK(r==(mode==5?-EIO:0));CHECK(a->attr==(mode==5?(BUF_ATTR_DEFERRED|BUF_ATTR_DEQUEUED):(BUF_ATTR_QUEUED|BUF_ATTR_DEQUEUED)));CHECK(count(i,BUF_BIN)==1);}
 else if(mode==6){make_buffers(i,BUF_DPB,2);CHECK(iris_queue_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)==0);
 CHECK(event_count[QUEUE]==0);CHECK(nth(i,BUF_DPB,0)->attr==BUF_ATTR_DEFERRED);
 i->state=IRIS_INST_STREAMING;CHECK(iris_queue_internal_deferred_buffers(i,BUF_DPB)==0);CHECK(event_count[QUEUE]==2);
 CHECK(nth(i,BUF_DPB,0)->attr==BUF_ATTR_QUEUED);}
 else if(mode==7 || mode==8){make_buffers(i,BUF_DPB,4);i->state=IRIS_INST_STREAMING;
 nth(i,BUF_DPB,0)->attr=BUF_ATTR_PENDING_RELEASE|BUF_ATTR_DEFERRED;nth(i,BUF_DPB,1)->attr=BUF_ATTR_QUEUED;
 nth(i,BUF_DPB,2)->attr=BUF_ATTR_DEFERRED;if(mode==8)fail_event=QUEUE;
 r=iris_queue_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);CHECK(r==(mode==8?-EIO:0));
 CHECK(event_count[QUEUE]==(mode==8?1:2));CHECK(nth(i,BUF_DPB,0)->attr==(BUF_ATTR_PENDING_RELEASE|BUF_ATTR_DEFERRED));
 CHECK(nth(i,BUF_DPB,2)->attr==(mode==8?BUF_ATTR_DEFERRED:BUF_ATTR_QUEUED));CHECK(nth(i,BUF_DPB,3)->attr==(mode==8?0:BUF_ATTR_QUEUED));}
 else if(mode==9 || mode==10){make_buffers(i,BUF_BIN,4);nth(i,BUF_BIN,0)->attr=BUF_ATTR_PENDING_RELEASE|BUF_ATTR_QUEUED;
 nth(i,BUF_BIN,1)->attr=BUF_ATTR_DEFERRED;nth(i,BUF_BIN,2)->attr=BUF_ATTR_QUEUED;nth(i,BUF_BIN,3)->attr=BUF_ATTR_QUEUED;
 if(mode==10){fail_event=RELEASE;}r=iris_release_internal_buffers(i,BUF_BIN);CHECK(r==(mode==10?-EIO:0));
 CHECK(event_count[RELEASE]==(mode==10?1:2));CHECK(nth(i,BUF_BIN,2)->attr==(mode==10?BUF_ATTR_QUEUED:(BUF_ATTR_QUEUED|BUF_ATTR_PENDING_RELEASE)));
 CHECK(iris_release_internal_buffers(i,BUF_BIN)==0);CHECK(event_count[RELEASE]==(mode==10?3:2));}
 else if(mode==11){make_buffers(i,BUF_DPB,3);make_buffers(i,BUF_PERSIST,1);a=nth(i,BUF_DPB,0);a->attr=BUF_ATTR_QUEUED;
 nth(i,BUF_DPB,1)->attr=BUF_ATTR_DEQUEUED;nth(i,BUF_DPB,2)->attr=BUF_ATTR_PENDING_RELEASE;
 CHECK(iris_destroy_dequeued_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)==0);CHECK(count(i,BUF_DPB)==1);
 CHECK(nth(i,BUF_DPB,0)==a);CHECK(count(i,BUF_PERSIST)==1);
 CHECK(iris_destroy_all_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)==0);CHECK(count(i,BUF_DPB)==0 && count(i,BUF_PERSIST)==0);
 CHECK(iris_destroy_all_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)==0);}
 else if(mode==12 || mode==13){i->buffers[BUF_BIN].size=64;i->buffers[BUF_BIN].min_count=1;
 i->buffers[BUF_COMV].size=64;i->buffers[BUF_COMV].min_count=1;alloc_fail=mode==12?2:0;dma_fail=mode==13?2:0;
 CHECK(iris_create_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)==-ENOMEM);
 /* The repaired allocator rolls back only this call's unsubmitted suffix. */
 CHECK(count(i,BUF_BIN)==0 && count(i,BUF_COMV)==0);
 CHECK(iris_destroy_all_internal_buffers(i,V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)==0);CHECK(heap_live==0 && dma_live==0);}
 else if(mode==14){fail_event=QUEUE;CHECK(iris_alloc_and_queue_persist_bufs(i,BUF_PERSIST)==-EIO);
 CHECK(count(i,BUF_PERSIST)==1);CHECK(nth(i,BUF_PERSIST,0)->attr==0);fail_event=0;
 CHECK(iris_alloc_and_queue_persist_bufs(i,BUF_PERSIST)==0);
 CHECK(event_count[QUEUE]==2);CHECK(nth(i,BUF_PERSIST,0)->attr & BUF_ATTR_QUEUED);}
 else if(mode==15 || mode==16){make_buffers(i,BUF_BIN,3);for(int j=0;j<3;j++)nth(i,BUF_BIN,j)->attr=BUF_ATTR_DEFERRED;
 if(mode==16){fail_event=QUEUE;fail_n=2;}r=iris_queue_internal_deferred_buffers(i,BUF_BIN);
 CHECK(r==(mode==16?-EIO:0));CHECK(event_count[QUEUE]==(mode==16?2:3));
 CHECK(nth(i,BUF_BIN,0)->attr==BUF_ATTR_QUEUED);CHECK(nth(i,BUF_BIN,1)->attr==(mode==16?BUF_ATTR_DEFERRED:BUF_ATTR_QUEUED));
 CHECK(nth(i,BUF_BIN,2)->attr==(mode==16?BUF_ATTR_DEFERRED:BUF_ATTR_QUEUED));}
}
static void stream_case(struct fixture *f,int mode){struct iris_inst *i=&f->g.inst;int r;f->c.state=IRIS_CORE_INIT;drc(i);u32 before=i->sub_state;
 if(mode<=6){const int events[]={CONFIG,RELEASE,STAGE_PROP,PIPE_PROP,RESUME_IP,START,QUEUE};
 fail_event=events[mode];if(mode==1){make_buffers(i,BUF_BIN,1);nth(i,BUF_BIN,0)->attr=BUF_ATTR_QUEUED;}
 if(mode==6)fail_n=3; /* two input queues succeed, first DPB queue fails */
 r=iris_vdec_streamon_output(i);CHECK(r==-EIO);
 if(mode<=3)CHECK(event_count[RESUME_IP]==0 && event_count[START]==0);
 if(mode==4)CHECK(event_count[RESUME_IP]==1 && event_count[START]==0);
 if(mode==5)CHECK(event_count[START]==1 && event_count[QUEUE]==2);
 if(mode<=5)CHECK(i->sub_state==before);
 CHECK(event_count[STOP]==(mode==0?0:1));
 }else if(mode==7){CHECK(iris_vdec_streamon_output(i)==0);CHECK(i->state==IRIS_INST_STREAMING);CHECK(i->sub_state==0);
 CHECK(!i->last_buffer_dequeued);CHECK(event_count[RESUME_IP]==1 && event_count[START]==1);
 CHECK(event_count[QUEUE]==4);CHECK(count(i,BUF_DPB)==2);
 CHECK(nth(i,BUF_DPB,0)->attr==BUF_ATTR_QUEUED && nth(i,BUF_DPB,1)->attr==BUF_ATTR_QUEUED);
 CHECK(event_count[STAGE_PROP]==1 && event_count[PIPE_PROP]==1);
 }else if(mode==8 || mode==9){fail_event=mode==8?RESUME_IP:RESUME_OP;
 CHECK(iris_vdec_start_cmd(i)==-EIO);CHECK(i->sub_state==before);CHECK(event_count[START]==0);
 CHECK(event_count[RESUME_OP]==(mode==8?0:1));
 }else if(mode==10){CHECK(iris_vdec_start_cmd(i)==0);CHECK(i->sub_state==0);TRACE(RESUME_IP,RESUME_OP);}
 else if(mode==11){fail_event=PMGET;CHECK(iris_vdec_streamon_output(i)==-EIO);
 CHECK(event_count[RESUME_IP]==0 && event_count[START]==0 && event_count[QUEUE]==0);CHECK(pm_refs==0);CHECK(i->sub_state==before);}
 else if(mode==12 || mode==13){alloc_fail=mode==12?2:0;dma_fail=mode==13?2:0;
 CHECK(iris_vdec_streamon_output(i)==-ENOMEM);CHECK(event_count[START]==0 && event_count[RESUME_IP]==0);
 /* streamon allocation failure must not orphan its newly allocated prefix */
 CHECK(count(i,BUF_DPB)==0);CHECK(heap_live==0 && dma_live==0);}
}
static void response_case(struct fixture *f,int mode){struct iris_inst *i=&f->g.inst;struct iris_hfi_packet pkt={.port=HFI_PORT_BITSTREAM};
 if(mode==0){CHECK(iris_hfi_gen2_handle_src_change(i,&pkt)==0);CHECK(i->state==IRIS_INST_INPUT_STREAMING);
 CHECK(event_count[NOTIFY]==1);CHECK(f->src.fmt.pix_mp.width==1920);CHECK(f->dst.fmt.pix_mp.pixelformat==V4L2_PIX_FMT_NV12);
 CHECK(f->dst.fmt.pix_mp.width==1920 && f->dst.fmt.pix_mp.height==1088);
 CHECK(i->buffers[BUF_OUTPUT].min_count==2 && f->ctx.dst.min_reqbufs_allocation==2);}
 else if(mode==1){pkt.port=HFI_PORT_RAW;CHECK(iris_hfi_gen2_handle_src_change(i,&pkt)==0);CHECK(i->sub_state==0);CHECK(nlog==0);}
 else if(mode==2){i->sub_state=IRIS_INST_SUB_DRC;CHECK(iris_hfi_gen2_handle_src_change(i,&pkt)==-EINVAL);CHECK(event_count[NOTIFY]==0);}
 else {if(mode==3 || mode==7)f->g.src_subcr_params.bitstream_resolution=1080;
 if(mode==4){f->g.src_subcr_params.bit_depth=(10U<<16)|10;}if(mode==5){f->g.src_subcr_params.crop_offsets[1]=1080;}
 if(mode==6)f->dst.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_P010;
 int r=iris_hfi_gen2_handle_src_change(i,&pkt);CHECK(i->state==IRIS_INST_ERROR);
 if(mode==7){nlog=0;memset(event_count,0,sizeof(event_count));r=iris_vdec_streamon_output(i);
 CHECK(r<0);CHECK(event_count[START]==0 && event_count[RESUME_IP]==0 && event_count[QUEUE]==0);}
 else{CHECK(r<0);CHECK(event_count[NOTIFY]==0);}}
}
static void power_case(struct fixture *f,int mode){struct iris_inst *i=&f->g.inst;
 if(mode==0){fail_event=PMGET;CHECK(iris_scale_power(i)==-EIO);TRACE(PMGET);CHECK(pm_refs==0);}
 else if(mode==1){CHECK(iris_scale_power(i)==0);TRACE(PMGET,PMAUTO,ICC);CHECK(pm_refs==0);}
 else if(mode==2){suspend_flag=0;CHECK(iris_scale_power(i)==0);TRACE(ICC);CHECK(pm_refs==0);}
 else if(mode==3){fail_event=ICC;CHECK(iris_scale_power(i)==-EIO);TRACE(PMGET,PMAUTO,ICC);CHECK(pm_refs==0);}
}
int main(int argc,char **argv){assert(argc==3);struct fixture f;init(&f);int mode=atoi(argv[2]);
 if(!strcmp(argv[1],"core"))core_case(&f,mode);
 else if(!strcmp(argv[1],"buffer"))buffer_case(&f,mode);
 else if(!strcmp(argv[1],"stream"))stream_case(&f,mode);
 else if(!strcmp(argv[1],"response"))response_case(&f,mode);
 else if(!strcmp(argv[1],"power"))power_case(&f,mode);else return 2;
 cleanup(&f);if(failures){fprintf(stderr,"%d safety assertion(s) failed; events:",failures);
 for(int j=0;j<nlog;j++){fprintf(stderr," %d",logbuf[j]);}fprintf(stderr,"\n");}return failures?1:0;}
'''

FUNCTIONS = {
    'iris_utils.c': ('iris_split_mode_enabled', 'iris_res_is_less_than'),
    'iris_vpu_buffer.c': ('output_min_count', 'internal_buffer_count', 'iris_vpu_dpb_count', 'iris_vpu_buf_count'),
    'iris_buffer.c': ('iris_calc_capture_layout', 'iris_apply_capture_layout', 'iris_fill_internal_buf_info',
                      'iris_get_internal_buffers', 'iris_create_internal_buffer', 'iris_create_internal_buffers',
                      'iris_queue_buffer', 'iris_queue_internal_deferred_buffers', 'iris_queue_internal_buffers',
                      'iris_destroy_internal_buffer', 'iris_destroy_internal_buffers', 'iris_destroy_all_internal_buffers',
                      'iris_destroy_dequeued_internal_buffers', 'iris_release_internal_buffers',
                      'iris_release_input_internal_buffers', 'iris_alloc_and_queue_persist_bufs',
                      'iris_alloc_and_queue_input_int_bufs'),
    'iris_state.c': ('iris_allow_inst_state_change', 'iris_inst_change_state', 'iris_inst_state_change_streamon',
                     'iris_inst_state_change_streamoff', 'iris_inst_allow_sub_state', 'iris_inst_change_sub_state',
                     'iris_inst_sub_state_change_drc'),
    'iris_ctrls.c': ('iris_get_port_info', 'iris_set_stage', 'iris_set_pipe'),
    'iris_power.c': ('iris_calc_bw', 'iris_set_interconnects', 'iris_vote_interconnects', 'iris_set_clocks',
                     'iris_scale_clocks', 'iris_scale_power'),
    'iris_hfi_common.c': ('iris_hfi_get_v4l2_color_primaries', 'iris_hfi_get_v4l2_transfer_char',
                          'iris_hfi_get_v4l2_matrix_coefficients', 'iris_hfi_core_init'),
    'iris_core.c': ('iris_core_deinit', 'iris_wait_for_system_response', 'iris_core_init'),
    'iris_common.c': ('iris_process_streamon_output', 'iris_flush_deferred_buffers', 'iris_kill_session', 'iris_session_streamoff'),
    'iris_vdec.c': ('iris_vdec_src_change', 'iris_vdec_streamon_output', 'iris_vdec_start_cmd'),
    'iris_hfi_gen2_response.c': ('iris_hfi_gen2_read_input_subcr_params', 'iris_hfi_gen2_handle_src_change'),
}
AGGREGATES = {
    'iris_buffer.h': ('iris_buffer_type', 'iris_buffer_attributes', 'iris_buffer', 'iris_buffers', 'iris_capture_layout'),
    'iris_state.h': ('iris_core_state', 'iris_inst_state', 'iris_inst_sub_state'),
    'iris_platform_common.h': ('stage_type', 'platform_inst_fw_cap_type', 'platform_inst_fw_cap_flags',
                               'bw_info', 'iris_core_power', 'iris_inst_power', 'icc_vote_data'),
    'iris_hfi_common.h': ('hfi_packet_port_type', 'hfi_packet_payload_info', 'hfi_packet_host_flags',
                         'hfi_color_primaries', 'hfi_transfer_characteristics', 'hfi_matrix_coefficients',
                         'hfi_subscription_params'),
}
CASE_NAMES = {
    'core': ('queue-init-failure', 'power-on-failure', 'fw-load-failure', 'fw-boot-failure',
             'sys-init-failure', 'version-failure', 'powercollapse-failure', 'response-timeout-cleanup',
             'response-error-cleanup', 'success-idempotence-and-deinit', 'deinit-pm-get-failure',
             'existing-core-error', 'wait-existing-error'),
    'buffer': ('zero-size-no-allocation', 'kzalloc-failure', 'dma-failure-kfree', 'list-metadata-unlink',
               'queue-success-flags', 'queue-failure-preserves-flags', 'dpb-defer-then-queue',
               'queue-skips-pending-and-owned', 'queue-error-stops-prefix', 'release-idempotence',
               'release-error-retry', 'dequeued-and-force-destroy', 'partial-kzalloc-explicit-destroy',
               'partial-dma-explicit-destroy', 'persist-queue-error-retry', 'deferred-success', 'deferred-error-prefix'),
    'stream': ('config-error-no-start', 'release-error-no-start', 'stage-error-no-start', 'pipe-error-no-start',
               'resume-error-no-start', 'start-error-no-dpb-queue', 'dpb-queue-error-stop', 'nv12-drc-success',
               'start-cmd-input-resume-error', 'start-cmd-output-resume-error', 'start-cmd-success',
               'pm-error-no-start-resume', 'partial-kzalloc-cleanup', 'partial-dma-cleanup'),
    'response': ('valid-nv12-source-change', 'wrong-port-no-op', 'duplicate-drc-rejected',
                 'invalid-resolution-no-notify', 'unsupported-depth-no-notify', 'invalid-crop-no-notify',
                 'layout-error-no-notify', 'invalid-response-no-later-start'),
    'power': ('pm-get-error-no-put-votes', 'pm-success-balanced', 'active-no-pm-get', 'icc-error-balanced'),
}
CASES = [(group, n, name) for group, names in CASE_NAMES.items() for n, name in enumerate(names)]
# Negative controls edit ONLY generated extracted C in the separate fixture dir.
MUTATIONS = (
    ('queue-error-flags-oracle', 'buf->attr |= BUF_ATTR_QUEUED;', 'buf->attr |= BUF_ATTR_DEFERRED;', 'buffer', 4),
    ('reverse-cleanup-oracle', 'error_power_off:\n\tiris_vpu_power_off(core);\nerror_queue_deinit:\n\tiris_hfi_queues_deinit(core);',
     'error_power_off:\n\tiris_hfi_queues_deinit(core);\n\tiris_vpu_power_off(core);\n\tgoto error;\nerror_queue_deinit:\n\tiris_hfi_queues_deinit(core);', 'core', 2),
    ('resume-failure-oracle', 'if (ret)\n\t\t\treturn ret;\n\t\tclear_sub_state |= IRIS_INST_SUB_INPUT_PAUSE;',
     'if (ret)\n\t\t\tret = 0;\n\t\tclear_sub_state |= IRIS_INST_SUB_INPUT_PAUSE;', 'stream', 4),
)


def fixture(work, output, sanitize=False):
    defs = ''
    manifest = []
    for filename, names in AGGREGATES.items():
        text = (work / filename).read_text()
        defs += ''.join(aggregate(text, name) for name in names)
    text = (work / 'iris_hfi_gen2_defines.h').read_text()
    for name in ('HFI_BITMASK_BITSTREAM_WIDTH', 'HFI_BITMASK_BITSTREAM_HEIGHT', 'HFI_BITMASK_FRAME_MBS_ONLY_FLAG'):
        hits = re.findall(r'^#define\s+' + name + r'\s+[^\n]+', text, re.M)
        if len(hits) != 1:
            raise ValueError('Missing actual define ' + name)
        defs += hits[0] + '\n'
    common = (work / 'iris_hfi_common.h').read_text()
    start = common.index('struct iris_hfi_pending {')
    defs += common[start:common.index('enum hfi_packet_port_type {', start)]
    functions = ''
    for filename, names in FUNCTIONS.items():
        text = (work / filename).read_text()
        for name in names:
            body = definition(text, name)
            functions += body
            manifest.append({'file': filename, 'function': name, 'sha256': hashlib.sha256(body.encode()).hexdigest()})
    source = output / 'actual-cleanup.c'
    source.write_text(PREFIX + defs + TYPES + functions + MAIN)
    return compile_fixture(source, output / 'actual-cleanup', sanitize), source, manifest


def compile_fixture(source, binary, sanitize):
    command = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-g', str(source), '-o', str(binary)]
    if sanitize:
        command += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(command, cwd='/var/tmp', env=ENV, capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError('GCC failed:\n' + (result.stdout + result.stderr)[-12000:])
    return binary


def run_case(binary, group, mode, sanitize):
    env = dict(ENV)
    if sanitize:
        env.update(ASAN_OPTIONS='detect_leaks=1:halt_on_error=1', UBSAN_OPTIONS='halt_on_error=1:print_stacktrace=1')
    try:
        result = subprocess.run([str(binary), group, str(mode)], cwd='/var/tmp', env=env,
                                capture_output=True, text=True, timeout=5)
        return result.returncode, (result.stdout + result.stderr)[-3500:]
    except subprocess.TimeoutExpired:
        return 124, 'Case exceeded five-second bound'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--upstream', type=Path, default=Path('/var/tmp/linux-src/linux-6.18.18'))
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--report', type=Path, help='Optional JSON findings path (use private/, never docs/)')
    parser.add_argument('--no-mutations', action='store_true', help='Skip separate oracle negative controls')
    args = parser.parse_args()
    if os.name != 'posix' or not Path('/usr/bin/gcc').is_file():
        parser.error('Run offline under WSL with /usr/bin/gcc and python3 -I')
    if not __import__('sys').flags.isolated:
        parser.error('Python isolated mode is required: python3 -I')
    with tempfile.TemporaryDirectory(prefix='iris-cleanup-inputs-', dir='/var/tmp') as inputs, \
            tempfile.TemporaryDirectory(prefix='iris-cleanup-fixture-', dir='/var/tmp') as generated:
        work = args.work.resolve() if args.work else Path(inputs)
        if not args.work:
            spec = importlib.util.spec_from_file_location('cleanup_builder', HERE / 'build.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            builder.prepare_iris(args.upstream, work)
        binary, source, manifest = fixture(work, Path(generated), args.sanitize)
        rows = []
        for group, mode, name in CASES:
            rc, log = run_case(binary, group, mode, args.sanitize)
            rows.append({'case': group + '/' + name, 'status': 'PASS' if rc == 0 else 'FAIL', 'returncode': rc, 'log': log})
            print(('PASS ' if rc == 0 else 'FAIL ') + group + '/' + name)
            if rc:
                print(log.rstrip())
        mutations = []
        if not args.no_mutations:
            original = source.read_text()
            for name, before, after, group, mode in MUTATIONS:
                if original.count(before) != 1:
                    mutations.append({'case': name, 'status': 'ERROR', 'log': 'Mutation anchor is not unique'})
                    continue
                mutant = Path(generated) / (name + '.c')
                mutant.write_text(original.replace(before, after, 1))
                mutated_binary = compile_fixture(mutant, Path(generated) / name, args.sanitize)
                rc, log = run_case(mutated_binary, group, mode, args.sanitize)
                # A crash/timeout/sanitizer finding is not a successful assertion oracle.
                caught = rc == 1 and 'safety assertion(s) failed' in log and 'AddressSanitizer' not in log and 'runtime error:' not in log
                mutations.append({'case': name, 'status': 'PASS' if caught else 'FAIL', 'returncode': rc, 'log': log})
                print(('PASS ' if caught else 'FAIL ') + 'oracle/' + name)
        passed = sum(row['status'] == 'PASS' for row in rows)
        failed = len(rows) - passed
        oracle_failed = sum(row['status'] != 'PASS' for row in mutations)
        print(f'Actual-C: {passed} passed, {failed} failed, {len(rows)} total; oracle controls: '
              f'{len(mutations)-oracle_failed} passed, {oracle_failed} failed; sanitizer={args.sanitize}')
        report = {'scope': 'bounded offline host actual-C cleanup/DRC; NOT complete driver/hardware verification',
                  'work': str(work), 'sanitizer': args.sanitize, 'passed': passed, 'failed': failed,
                  'total': len(rows), 'cases': rows, 'oracle_controls': mutations, 'extracted': manifest,
                  'phone_operated': False, 'network_used': False, 'driver_modified': False,
                  'findings_0006': [
                      {'cases': ['core/response-timeout-cleanup', 'core/response-error-cleanup'],
                       'site': 'iris_core.c:83-85',
                       'observation': 'Wait failure returns without reverse FW/power/queue cleanup; state ERROR and resources retained.',
                       'integration_hint': 'Route failed wait through serialized reverse cleanup; resources are still reachable for later deinit.'},
                      {'cases': ['core/deinit-pm-get-failure'], 'site': 'iris_core.c:15-26',
                       'observation': 'Failed PM get is ignored; unload/power-off/deinit and put_sync still run, yielding mock usage -1.',
                       'integration_hint': 'Do not put a reference that was not acquired; handle failed resume before powered cleanup.'},
                      {'cases': ['buffer/persist-queue-error-retry'], 'site': 'iris_buffer.c:751-752,767-769',
                       'observation': 'After queue failure leaves an allocated unqueued persist buffer, retry returns 0 on nonempty list without queueing.',
                       'integration_hint': 'Retry unqueued existing buffers or clean them on failed setup; do not treat nonempty as fully queued.'},
                      {'cases': ['stream/pm-error-no-start-resume'], 'site': 'iris_common.c:98',
                       'observation': 'Real iris_scale_power returns -EIO; its caller ignores it and continues input queue/RESUME/START/DPB queue.',
                       'integration_hint': 'Propagate PM/scaling error before reconfiguration and commands.'},
                      {'cases': ['stream/partial-kzalloc-cleanup', 'stream/partial-dma-cleanup'],
                       'site': 'iris_vdec.c:418-420; iris_buffer.c:519-524',
                       'observation': 'Second DPB allocation fails; streamon returns -ENOMEM with first new DPB and DMA allocation still on the list.',
                       'integration_hint': 'New-prefix rollback or explicit caller teardown required; this test does NOT establish a permanent leak, because forced destroy reclaims it.'},
                      {'cases': ['response/invalid-resolution-no-notify', 'response/unsupported-depth-no-notify',
                                 'response/invalid-crop-no-notify', 'response/layout-error-no-notify'],
                       'site': 'iris_hfi_gen2_response.c:553,573-589,680-683',
                       'observation': 'Void parameter reader sets ERROR and returns early; handle_src_change nevertheless sends source-change and returns 0.',
                       'integration_hint': 'Make parsing/layout failure observable and propagate it before source-change notification.'},
                      {'cases': ['response/invalid-response-no-later-start'],
                       'site': 'iris_vdec.c:403-430; iris_common.c:91-155; iris_state.c:50-51',
                       'observation': 'After actual invalid DRC response sets ERROR, later actual streamon still queues input and sends START, returning 0.',
                       'integration_hint': 'Reject ERROR state before config/allocation/control commands; returning success from state helper must not authorize START.'},
                  ],
                  'uncovered': ['HFI wire/response packet validation and command correlation',
                                'real firmware, IRQs, concurrency and DMA/PM implementation',
                                'platform size/frequency callbacks and core resource-acquisition internals',
                                'NV12 pixel correctness, full vb2 userspace ioctl lifecycle',
                                'encoder and other capture formats',
                                'full pending-response concurrency and timeout-tail isolation']}
        for finding in report['findings_0006']:
            relevant = [row for row in rows if row['case'] in finding['cases']]
            finding['current_case_status'] = 'PASS' if relevant and all(row['status'] == 'PASS' for row in relevant) else 'FAIL'
            finding['baseline'] = 'historical 0001..0006; observation is not a claim about the supplied work'
        if args.report:
            # Report writes require an explicit path under the repository private directory.
            dest = args.report.resolve()
            private = (HERE.parents[3] / 'private').resolve()
            if not dest.is_relative_to(private):
                parser.error('--report must be under this repository private/')
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(report, indent=2) + '\n')
        return 1 if failed or oracle_failed else 0


if __name__ == '__main__':
    raise SystemExit(main())
