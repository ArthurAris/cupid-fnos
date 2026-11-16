#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""One actual-C terminal cleanup chain, with explicit host framework/provider seams.

Run in WSL with python3 -I -B --work <prepared candidate>. Only reads candidate
source; compiles in a separate empty temporary directory. Never edits candidate
or existing tests. Instance construction/quarantine and running firmware/power
are declared input seams, NOT probe/open/close/streaming acceptance. Shutdown,
drain, reclaim, preparation, detach, IRQ/resource release and CPU ownership are
actual extracted driver functions; no shutdown/drain proof bits are seeded.
IRQ chip/raw-spin semantics, SCM/PAS, checked power, DMA, PM and V4L2 are host
models. No device/SPI hardware, kernel scheduling or DMA shutdown proof claimed.
"""
import sys
sys.dont_write_bytecode = True

import argparse
import hashlib
import importlib.util
from pathlib import Path
import resource
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


HARDWARE = load('batch_hardware_snippets', 'test-hardware-owner.py')
PASSIVE = load('batch_passive_snippets', 'test-irq-passive-drain.py')
EXTRACT = HARDWARE.EXTRACT
WORK = None
ENV = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'LANG': 'C',
       'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1',
       'UBSAN_OPTIONS': 'halt_on_error=1'}
CASES = ('orphan_detach_once', 'shutdown_failure_retry', 'active_file_reject',
         'open_inflight_reject', 'pm_borrower_barrier_retry', 'passive_irq_response_drain',
         'power_failure_retry', 'retained_video_reference', 'empty_frontend_detach',
         'explicit_quiesce_reclaim_prepare', 'buffer_destroy_failure_retry')


PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
typedef uint32_t u32;
typedef uintptr_t resource_size_t;
typedef uintptr_t dma_addr_t;
typedef int irqreturn_t;
#define GFP_KERNEL 0
#define IRQ_NONE 0
#define IRQ_WAKE_THREAD 1
#define IRQ_HANDLED 2
#define IRQF_TRIGGER_HIGH 1
#define IRQF_NO_AUTOEN 2
#define IORESOURCE_MEM 1
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
enum domain_type {DECODER=1,ENCODER=2};
#define VFL_DIR_M2M 1
#define VFL_TYPE_VIDEO 2
#define V4L2_CAP_VIDEO_M2M_MPLANE 4
#define V4L2_CAP_STREAMING 8
#define PD_FLAG_NO_DEV_LINK 1
#define PD_FLAG_DEV_LINK_ON 2
#define PD_FLAG_REQUIRED_OPP 4
#define IRIS_HW_CLK 1
#define DMA_ATTR_WRITE_COMBINE 1
#define SZ_4K 4096
#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))
#define IFACEQ_QUEUE_SIZE 1024
#define IFACEQ_CORE_PKT_SIZE 4096
#define IFACEQ_NUMQ 3
#define SFR_SIZE 512
#define IFACEQ_CMDQ_ID 0
#define IFACEQ_MSGQ_ID 1
#define IFACEQ_DBGQ_ID 2
#define IFACEQ_DFLT_QHDR 7
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define resource_size(r) ((r)->end-(r)->start+1)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define dev_err(d,...) ((void)(d))
#define REQUIRE(v,label) do {if(!(v)){fprintf(stderr,"Assertion failed: %s (%s)\n",label,#v);abort();}} while(0)
/* LOCKS */
#define lockdep_assert_not_held(p) REQUIRE(!mutex_is_held((struct mutex *)(p)), "not_current_thread_lock_owner")
#define raw_spin_lock_init(p) assert(!pthread_mutex_init(&(p)->lock,NULL))
static void mutex_init(struct mutex *m){assert(!pthread_mutex_init(&m->lock,NULL));}
static void mutex_destroy(struct mutex *m){REQUIRE(!mutex_is_held(m),"destroy_unheld_lock");assert(!pthread_mutex_destroy(&m->lock));}
typedef raw_spinlock_t spinlock_t;
#define spin_lock_init(p) raw_spin_lock_init(p)
#define spin_lock_irqsave(p,f) raw_spin_lock_irqsave(p,f)
#define spin_unlock_irqrestore(p,f) raw_spin_unlock_irqrestore(p,f)
typedef struct {atomic_int value;} refcount_t;
static void refcount_set(refcount_t *r,int n){atomic_init(&r->value,n);}
static int refcount_read(refcount_t *r){return atomic_load(&r->value);}
static void refcount_inc(refcount_t *r){REQUIRE(atomic_fetch_add(&r->value,1)>0,"reference_on_live_object");}
static bool refcount_dec_and_test(refcount_t *r){int old=atomic_fetch_sub(&r->value,1);REQUIRE(old>0,"reference_no_underflow");return old==1;}
#define atomic_read(p) atomic_load(p)
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
#define LIST_HEAD(n) struct list_head n={&(n),&(n)}
static bool list_empty(struct list_head *h){return h->next==h;}
static void list_add_tail(struct list_head *n,struct list_head *h){n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;}
static void list_del_init(struct list_head *n){n->prev->next=n->next;n->next->prev=n->prev;INIT_LIST_HEAD(n);}
static void list_splice_init(struct list_head *from,struct list_head *to){if(list_empty(from))return;to->next=from->next;to->prev=from->prev;to->next->prev=to;to->prev->next=to;INIT_LIST_HEAD(from);}
#define list_for_each_entry(p,h,m) for(struct list_head *_it=(h)->next;_it!=(h)&&((p)=container_of(_it,__typeof__(*(p)),m),1);_it=_it->next)
#define list_for_each_entry_safe(p,n,h,m) for(struct list_head *_it=(h)->next,*_next=NULL;_it!=(h)&&((_next=_it->next),(p)=container_of(_it,__typeof__(*(p)),m),(n)=_next==(h)?NULL:container_of(_next,__typeof__(*(n)),m),(void)(n),1);_it=_next)
struct completion {int done;};
static void complete(struct completion *p){p->done++;}
/* TRACKER */
struct device {atomic_int refs;struct {atomic_int usage_count;} power;};
struct resource {resource_size_t start,end;};
struct platform_device {struct device dev;struct resource res;};
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
/* PROVIDER_TYPES */
struct iris_core;
struct iris_hfi_response_ops {void (*hfi_response_handler)(struct iris_core *);};
struct v4l2_device {int registered;};
struct video_device {struct iris_core *data;void (*release)(struct video_device *);void *fops,*ioctl_ops;struct v4l2_device *v4l2_dev;u32 vfl_dir,device_caps;char name[32];int refs;bool registered;};
struct iris_hfi_queue_header {u32 status,start_addr,header_type,queue_type,q_size,pkt_size,rx_wm,tx_wm,rx_req,tx_req,rx_irq_status,tx_irq_status,read_idx,write_idx;};
struct iris_hfi_queue_table_header {u32 version;void *device_addr;char name[32];u32 size,qhdr0_offset,qhdr_size,num_q,num_active_q;struct iris_hfi_queue_header q_hdr[3];};
struct iris_iface_q_info {dma_addr_t device_addr;void *kernel_vaddr;struct iris_hfi_queue_header *qhdr;};
struct iris_core {
 struct device *dev;struct mutex lock,lifecycle_lock,quiesce_lock,remove_lock,detach_lock;raw_spinlock_t irq_lock;refcount_t owner_refs;
 struct list_head instances,orphan_instances;struct delayed_work sys_error_handler;
 struct iris_platform_data *iris_platform_data;struct iris_hfi_pending init_pending;struct completion core_init_done;
 void *response_packet,*reg_base,*iface_q_table_vaddr,*sfr_vaddr;dma_addr_t iface_q_table_daddr,sfr_daddr;
 struct iris_iface_q_info command_queue,message_queue,debug_queue;const struct iris_hfi_response_ops *hfi_response_ops;
 struct video_device *vdev_dec,*vdev_enc;struct v4l2_device v4l2_dev;void *iris_v4l2_file_ops,*iris_v4l2_ioctl_ops_dec,*iris_v4l2_ioctl_ops_enc;
 u32 video_owners;bool decoder_registered,encoder_registered,v4l2_registered,frontend_detached,detach_complete;
 bool irq_registered,irq_managed,irq_releasing,irq_probe_unpublished,irq_started,irq_initial_mask_held,irq_top_mask_held,quiesce_irq_mask_held,irq_accepting;
 bool hardware_owner_held,resources_releasing,runtime_pm_enabled,pm_guard_held,removing,remove_prepared;
 bool terminal_shutdown,irq_quiescing,recovery_blocked,core_ready,firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;
 bool firmware_may_run,firmware_booted,queues_allocated,power_prepared,shutdown_pm_held,power_irq_enabled,power_state_unknown;
 bool mmio_claimed,mmio_mapped,icc_acquired,clocks_acquired,domains_attached,opp_domains_attached,opp_table_added;
 resource_size_t mmio_start,mmio_size;int irq;u32 state,intr_status,open_inflight,open_owners,orphan_owners;int power_cleanup_error,firmware_shutdown_error;
 struct icc_bulk_data *icc_tbl;u32 icc_count;struct clk_bulk_data *clock_tbl;u32 clk_count;struct reset_control_bulk_data *resets,*controller_resets;
 struct dev_pm_domain_list *pmdomain_tbl,*opp_pmdomain_tbl;int *opp_tokens;u32 opp_token_count,reset_count,controller_reset_count;
};
struct iris_inst {struct iris_core *core;struct list_head list;struct mutex lock,ctx_q_lock;refcount_t response_refs;struct completion response_idle;
 bool response_closing,file_detached,orphaned;u32 domain;int ctrl_handler;void *m2m_ctx,*m2m_dev,*internal[2];};
static struct iris_core *c;
static struct iris_inst *instance;
static struct platform_device pdev;
static struct dev_pm_domain_list domains[2]={{1},{2}};
static struct icc_info icc[]={{"video-mem",1}};
static struct platform_clk_data clocks[]={{IRIS_HW_CLK,"video"}};
static const char * const pd_names[]={"ctrl","hw"};
static const char * const reset_names[]={"video"};
static struct iris_platform_data platform={icc,1,pd_names,pd_names,2,2,clocks,1,reset_names,reset_names,1,1};
static int fail_at,step,allocations,claimed,mapped,icc_live,pd_live,opp_pd_live,token_live,table_live,clock_live,reset_live,ctrl_reset_live,irq_live;
static bool missing_resource;
static int buffer_error,buffer_frees,framework_frees;
static int fail_errno=-EIO,shutdown_error,power_error,shutdowns,poweroffs,queue_frees,instance_frees,core_frees,packet_frees,video_frees,v4l2_unregs;
static int depth,syncs,cancels,irq_frees,barriers,pm_disables,pm_gets,pm_puts,guard_gets;
static int events[128],event_count;
enum {E_PM=1,E_IRQ,E_WORK,E_RESET_CTRL,E_RESET,E_CLOCK,E_TABLE,E_TOKEN,E_OPP_PD,E_PD,E_ICC,E_MAP,E_REGION,
 E_SYNC,E_SHUTDOWN,E_POWER,E_QUEUE,E_INSTANCE,E_PACKET,E_CORE,E_DEVICE};
static void event(int e){assert(event_count<128);events[event_count++]=e;}
static int find_event(int e){for(int i=0;i<event_count;i++)if(events[i]==e)return i;return -1;}
static bool fail(void){step++;return step==fail_at;}
static void *kzalloc(size_t n,int flags){(void)flags;if(fail())return NULL;void *p=calloc(1,n?n:1);assert(p);allocations++;return p;}
static void *kcalloc(size_t n,size_t size,int flags){return kzalloc(n*size,flags);}
static void kfree(void *p){
 if(!p)return;
 if(p==instance){REQUIRE(!refcount_read(&instance->response_refs),"instance_cpu_response_drain");REQUIRE(c->firmware_shutdown_verified&&c->irq_drain_verified&&c->recovery_drain_verified,"instance_after_actual_proofs");instance_frees++;event(E_INSTANCE);instance=NULL;}
 else if(p==c->response_packet){REQUIRE(!c->hardware_owner_held&&!irq_live&&!mapped&&!c->video_owners,"packet_after_all_hardware_and_video_owners");packet_frees++;event(E_PACKET);}
 else if(p==c){REQUIRE(!irq_live&&!mapped&&!c->hardware_owner_held,"core_after_hardware_and_irq_free");core_frees++;event(E_CORE);}
 assert(allocations>0);allocations--;free(p);
}
/* PROVIDER_CALLBACKS */
static pthread_mutex_t model=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t changed;
static struct timespec deadline;
static pthread_t controller,irq_worker,detach_worker;
static bool irq_queued,irq_running,response_gate,response_entered,response_release,sync_waiting;
static bool barrier_gate,barrier_entered,barrier_release,detach_done;
static int detach_result;
static irqreturn_t (*registered_top)(int,void *),(*registered_bottom)(int,void *);
static void model_lock(void){assert(!pthread_mutex_lock(&model));}
static void model_unlock(void){assert(!pthread_mutex_unlock(&model));}
static void notify(void){assert(!pthread_cond_broadcast(&changed));}
static void wait_change(void){int r=pthread_cond_timedwait(&changed,&model,&deadline);if(r==ETIMEDOUT){fputs("MODEL_TIMEOUT\n",stderr);abort();}assert(!r);}
static bool irq_idle(void){return !irq_queued&&!irq_running;}
static void callback_locks_free(void){REQUIRE(!mutex_is_held(&c->lock)&&!mutex_is_held(&c->lifecycle_lock)&&!driver_raw_depth,"callback_wait_outside_driver_locks");}
static int request_threaded_irq(int irq,irqreturn_t (*top)(int,void *),irqreturn_t (*bottom)(int,void *),u32 flags,const char *name,void *data){assert(irq==9&&data==c&&!strcmp(name,"iris"));REQUIRE(flags&IRQF_NO_AUTOEN,"irq_registration_starts_masked");assert(!irq_live);irq_live=1;depth=1;registered_top=top;registered_bottom=bottom;return 0;}
static void enable_irq(int irq){assert(irq==9);REQUIRE(!driver_raw_depth&&irq_live&&depth>0,"enable_only_live_irq_outside_raw_lock");model_lock();depth--;model_unlock();}
static void disable_irq_nosync(int irq){assert(irq==9);REQUIRE(!driver_raw_depth&&irq_live,"disable_only_live_irq_outside_raw_lock");model_lock();depth++;model_unlock();}
static void synchronize_irq(int irq){assert(irq==9);callback_locks_free();model_lock();syncs++;while(!irq_idle()){sync_waiting=true;notify();wait_change();}sync_waiting=false;event(E_SYNC);notify();model_unlock();}
static void free_irq(int irq,void *data){assert(irq==9&&data==c);callback_locks_free();REQUIRE(!atomic_read(&c->dev->power.usage_count),"free_irq_no_pm_users");model_lock();REQUIRE(irq_idle()&&irq_live&&c->irq_releasing,"free_irq_after_passive_drain");irq_live=0;irq_frees++;event(E_IRQ);model_unlock();}
static bool cancel_delayed_work_sync(struct delayed_work *w){assert(w==&c->sys_error_handler);callback_locks_free();model_lock();REQUIRE(irq_idle()&&syncs>0,"cancel_requires_irq_idle_after_sync");cancels++;event(E_WORK);model_unlock();return false;}
static struct work_struct *current_work(void){return NULL;}
static bool in_interrupt(void){return hard_context;}
static struct device *get_device(struct device *d){assert(d==&pdev.dev);REQUIRE(atomic_fetch_add(&d->refs,1)>0,"device_reference_live");return d;}
static void put_device(struct device *d){assert(d==&pdev.dev);int old=atomic_fetch_sub(&d->refs,1);assert(old>1);if(core_frees){REQUIRE(find_event(E_CORE)>find_event(E_PACKET),"device_put_after_cpu_destructor");event(E_DEVICE);}}
static bool iris_vpu_has_checked_power(struct iris_core *core){assert(core==c);return true;}
static bool iris_power_progress_pending(struct iris_core *core){assert(core==c);return core->power_prepared;}
static void pm_runtime_get_noresume(struct device *d){assert(d==c->dev);atomic_fetch_add(&d->power.usage_count,1);guard_gets++;}
static int pm_runtime_resume_and_get(struct device *d){assert(d==c->dev);REQUIRE(!pm_disables,"no_fresh_resume_after_pm_disable");atomic_fetch_add(&d->power.usage_count,1);pm_gets++;return 0;}
static void pm_runtime_put_noidle(struct device *d){assert(d==c->dev);REQUIRE(atomic_fetch_sub(&d->power.usage_count,1)>0,"pm_put_has_reference");pm_puts++;}
static void pm_runtime_put_sync(struct device *d){(void)d;REQUIRE(false,"managed_pm_must_not_autosuspend");}
static void pm_runtime_mark_last_busy(struct device *d){assert(d==c->dev);}
static void pm_runtime_barrier(struct device *d){assert(d==c->dev);callback_locks_free();model_lock();barriers++;event(E_PM);if(barrier_gate){barrier_entered=true;notify();while(!barrier_release)wait_change();}model_unlock();}
static void pm_runtime_disable(struct device *d){assert(d==c->dev);callback_locks_free();pm_disables++;}
static void pm_runtime_dont_use_autosuspend(struct device *d){assert(d==c->dev&&pm_disables);}
/* SCM provider: actual iris_fw_unload owns proof publication. */
static int qcom_scm_pas_shutdown(u32 pas){assert(pas==17);REQUIRE(c->irq_drain_verified&&c->recovery_drain_verified,"scm_after_actual_irq_and_work_proofs");shutdowns++;event(E_SHUTDOWN);return shutdown_error;}
static int iris_vpu_power_off_checked(struct iris_core *core){assert(core==c);REQUIRE(c->firmware_shutdown_verified&&c->irq_drain_verified&&c->recovery_drain_verified,"checked_power_after_verified_shutdown");poweroffs++;event(E_POWER);if(power_error)return power_error;c->power_irq_enabled=false;return 0;}
static void iris_vpu_clear_interrupt(struct iris_core *core){assert(core==c&&c->queues_allocated&&mapped);}
static int iris_vpu_watchdog(struct iris_core *core,u32 status){(void)status;assert(core==c);return 0;}
/* DMA provider allocations are real host allocations, not DMA proof. */
static void *dma_alloc_attrs(struct device *d,size_t size,dma_addr_t *addr,int flags,int attrs){assert(d==c->dev&&attrs==DMA_ATTR_WRITE_COMBINE);void *p=kzalloc(size,flags);*addr=(dma_addr_t)p;return p;}
static void dma_free_attrs(struct device *d,size_t size,void *p,dma_addr_t addr,int attrs){(void)size;assert(d==c->dev&&addr==(dma_addr_t)p&&attrs==DMA_ATTR_WRITE_COMBINE);model_lock();REQUIRE(irq_idle()&&c->firmware_shutdown_verified&&c->irq_drain_verified&&c->recovery_drain_verified&&!c->power_irq_enabled,"dma_free_after_actual_terminal_proofs");queue_frees++;event(E_QUEUE);model_unlock();kfree(p);}
static void strscpy(char *d,const char *s,size_t n){assert(strlen(s)<n);memcpy(d,s,strlen(s)+1);}
/* V4L2 holds registration and independent retained-video references. */
static struct video_device *video_device_alloc(void){return kzalloc(sizeof(struct video_device),GFP_KERNEL);}
static void video_set_drvdata(struct video_device *v,struct iris_core *core){v->data=core;}
static struct iris_core *video_get_drvdata(struct video_device *v){return v->data;}
static int video_register_device(struct video_device *v,int type,int number){assert(type==VFL_TYPE_VIDEO&&number==-1&&v->data==c&&v->release);v->registered=true;v->refs=1;return 0;}
static void video_device_release(struct video_device *v){REQUIRE(!v->registered&&!v->refs,"video_free_only_final_framework_reference");video_frees++;kfree(v);}
static void video_put(struct video_device *v){assert(v&&v->refs>0);if(!--v->refs)v->release(v);}
static void video_unregister_device(struct video_device *v){if(!v)return;REQUIRE(v->registered&&c->removing&&c->frontend_detached,"frontend_revocation_before_unregister");REQUIRE(!mutex_is_held(&c->lock),"video_callback_outside_core_lock");v->registered=false;video_put(v);}
static void v4l2_device_unregister(struct v4l2_device *v){assert(v==&c->v4l2_dev);REQUIRE(!irq_live&&!mapped&&!c->hardware_owner_held,"v4l2_after_hardware_release");v4l2_unregs++;v->registered=0;}
/* Constructed instance owns response-visible allocations; reclamation is actual. */
static void instance_destroy_context(void){REQUIRE(c->firmware_shutdown_verified&&c->irq_drain_verified&&c->recovery_drain_verified,"instance_resources_after_actual_proofs");REQUIRE(instance&&!refcount_read(&instance->response_refs),"instance_resources_after_cpu_response_drain");}
static void framework_destroy_context(void){instance_destroy_context();REQUIRE(!instance->internal[0]&&!instance->internal[1],"framework_after_successful_buffer_retirement");framework_frees++;}
static void v4l2_ctrl_handler_free(int *p){assert(p==&instance->ctrl_handler);framework_destroy_context();}
static void v4l2_m2m_ctx_release(void *p){assert(p==instance->m2m_ctx);framework_destroy_context();kfree(p);instance->m2m_ctx=NULL;}
static void v4l2_m2m_release(void *p){assert(p==instance->m2m_dev);framework_destroy_context();kfree(p);instance->m2m_dev=NULL;}
static void iris_vdec_inst_deinit(struct iris_inst *i){assert(i==instance);instance_destroy_context();}
static void iris_venc_inst_deinit(struct iris_inst *i){assert(i==instance);instance_destroy_context();}
static int iris_destroy_all_internal_buffers(struct iris_inst *i,u32 plane){assert(i==instance&&(plane==9||plane==10));instance_destroy_context();unsigned index=plane==10;if(index&&buffer_error)return buffer_error;if(i->internal[index]){kfree(i->internal[index]);i->internal[index]=NULL;buffer_frees++;}return 0;}
/* Response callback is a provider seam. synchronize_irq never releases it. */
static void response(struct iris_core *core){assert(core==c);model_lock();if(response_gate){response_entered=true;notify();while(!response_release)wait_change();REQUIRE(!queue_frees&&!irq_frees&&!core_frees,"response_retains_storage_until_controller_release");}model_unlock();}
'''

MAIN = r'''
static void setup(void){
 controller=pthread_self();assert(!clock_gettime(CLOCK_MONOTONIC,&deadline));deadline.tv_sec+=5;
 pthread_condattr_t a;assert(!pthread_condattr_init(&a));assert(!pthread_condattr_setclock(&a,CLOCK_MONOTONIC));assert(!pthread_cond_init(&changed,&a));assert(!pthread_condattr_destroy(&a));
 atomic_init(&pdev.dev.refs,1);atomic_init(&pdev.dev.power.usage_count,0);pdev.res=(struct resource){4096,8191};
 c=kzalloc(sizeof(*c),GFP_KERNEL);assert(c);c->dev=get_device(&pdev.dev);refcount_set(&c->owner_refs,1);
 mutex_init(&c->lock);mutex_init(&c->lifecycle_lock);mutex_init(&c->quiesce_lock);mutex_init(&c->remove_lock);mutex_init(&c->detach_lock);raw_spin_lock_init(&c->irq_lock);
 INIT_LIST_HEAD(&c->instances);INIT_LIST_HEAD(&c->orphan_instances);iris_hfi_pending_init(&c->init_pending);
 c->response_packet=kzalloc(IFACEQ_CORE_PKT_SIZE,GFP_KERNEL);c->iris_platform_data=&platform;c->irq=9;c->removing=true;
 iris_core_get(c);c->hardware_owner_held=true;
 assert(!iris_acquire_mmio(c,&pdev));assert(!iris_init_resources(c));assert(!iris_request_irq(c));
 c->runtime_pm_enabled=true;mutex_lock(&c->lock);iris_core_pm_guard_get(c);mutex_unlock(&c->lock);
 c->v4l2_registered=true;c->v4l2_dev.registered=1;assert(!iris_register_video_device(c,DECODER));assert(!iris_register_video_device(c,ENCODER));
 /* Running firmware/power construction seam, not actual probe/open/init. */
 assert(!iris_hfi_queues_init(c));c->queues_allocated=c->power_prepared=c->firmware_booted=c->firmware_may_run=true;c->state=IRIS_CORE_INIT;
 static const struct iris_hfi_response_ops ops={response};c->hfi_response_ops=&ops;
 mutex_lock(&c->lock);assert(!iris_irq_activate(c));c->irq_probe_unpublished=false;c->removing=false;mutex_unlock(&c->lock);
 REQUIRE(!c->firmware_shutdown_verified&&!c->irq_drain_verified&&!c->recovery_drain_verified&&!c->remove_prepared,"no_seeded_cleanup_proofs");
 assert(refcount_read(&c->owner_refs)==4&&atomic_read(&c->dev->power.usage_count)==1&&depth==0);
}
static void construct_active_instance(void){
 assert(!iris_core_open_get(c));iris_core_open_finish(c,true);
 instance=kzalloc(sizeof(*instance),GFP_KERNEL);assert(instance);instance->core=c;instance->domain=DECODER;
 mutex_init(&instance->lock);mutex_init(&instance->ctx_q_lock);refcount_set(&instance->response_refs,1);
 instance->m2m_ctx=kzalloc(8,GFP_KERNEL);instance->m2m_dev=kzalloc(8,GFP_KERNEL);instance->internal[0]=kzalloc(8,GFP_KERNEL);instance->internal[1]=kzalloc(8,GFP_KERNEL);list_add_tail(&instance->list,&c->instances);
}
static void construction_quarantine(void){
 /* Explicit instance/file construction seam in place of iris_close; owner
  * transitions and response decrement are actual, never driver proof bits. */
 assert(instance&&!instance->orphaned);list_del_init(&instance->list);instance->response_closing=true;
 iris_put_response_instance(instance);assert(instance->response_idle.done);
 get_device(c->dev);instance->file_detached=instance->orphaned=true;list_add_tail(&instance->list,&c->orphan_instances);iris_core_orphan_transfer(c);
 assert(c->orphan_owners==1&&!c->open_owners&&refcount_read(&c->owner_refs)==3+(int)c->video_owners);
 REQUIRE(iris_reclaim_orphan_instances(c)==-EAGAIN,"reclaim_requires_real_proof_producer");REQUIRE(!instance_frees,"no_preterminal_reclaim");
}
static void *run_irq(void *unused){(void)unused;model_lock();irq_queued=false;irq_running=true;notify();model_unlock();assert(registered_bottom(9,c)==IRQ_HANDLED);model_lock();irq_running=false;notify();model_unlock();return NULL;}
static void launch_response(void){
 assert(pthread_equal(controller,pthread_self()));hard_context=true;assert(registered_top(9,c)==IRQ_WAKE_THREAD);hard_context=false;
 model_lock();irq_queued=true;assert(!pthread_create(&irq_worker,NULL,run_irq,NULL));while(!response_entered)wait_change();model_unlock();
}
static void *run_detach(void *unused){(void)unused;terminal_context=true;int r=iris_device_detach(c);model_lock();detach_result=r;detach_done=true;notify();model_unlock();terminal_context=false;return NULL;}
static void launch_detach(void){model_lock();detach_done=false;assert(!pthread_create(&detach_worker,NULL,run_detach,NULL));model_unlock();}
static int finish_detach(void){assert(!pthread_join(detach_worker,NULL));return detach_result;}
static void assert_retained(void){REQUIRE(c->hardware_owner_held&&irq_live&&mapped&&!irq_frees&&!instance_frees&&!queue_frees&&!core_frees,"failure_retains_all_destructive_owners");if(instance)REQUIRE(instance->m2m_ctx&&instance->m2m_dev&&!framework_frees&&!buffer_frees&&refcount_read(&c->owner_refs)==3+(int)c->video_owners,"failure_retains_instance_framework_and_cpu_owner");}
static void verify_and_destroy(void){
 REQUIRE(c->detach_complete&&c->remove_prepared&&!c->hardware_owner_held&&!c->video_owners&&!c->open_owners&&!c->open_inflight&&!c->orphan_owners,"actual_chain_completes_all_owners");
 REQUIRE(!irq_live&&!mapped&&!claimed&&!icc_live&&!pd_live&&!opp_pd_live&&!token_live&&!table_live&&!clock_live&&!reset_live&&!ctrl_reset_live,"all_provider_handles_retired");
 REQUIRE(!c->irq_registered&&c->irq_releasing&&!c->irq_started&&!c->irq_initial_mask_held&&!c->irq_top_mask_held&&!c->quiesce_irq_mask_held&&!c->power_irq_enabled,"irq_driver_and_framework_agree");
 assert(c->terminal_shutdown&&c->firmware_shutdown_verified&&c->irq_drain_verified&&c->recovery_drain_verified&&!c->queues_allocated&&!c->shutdown_pm_held&&!c->pm_guard_held);
 assert(!atomic_read(&c->dev->power.usage_count)&&refcount_read(&c->owner_refs)==1&&irq_frees==1&&video_frees==2&&v4l2_unregs==1&&queue_frees==2&&!core_frees);
 int before=event_count;assert(!iris_device_detach(c));assert(!iris_release_resources(c));assert(event_count==before&&irq_frees==1);
 REQUIRE(find_event(E_QUEUE)>find_event(E_SHUTDOWN)&&find_event(E_QUEUE)>find_event(E_POWER),"queues_after_shutdown_and_power");
 REQUIRE(find_event(E_IRQ)>find_event(E_PM)&&find_event(E_MAP)>find_event(E_IRQ),"pm_then_irq_then_mmio_release");
 if(instance_frees)REQUIRE(find_event(E_INSTANCE)>find_event(E_QUEUE)&&find_event(E_INSTANCE)<find_event(E_IRQ),"orphan_after_quiesce_before_hardware_release");
 iris_binding_release(c);
 REQUIRE(core_frees==1&&packet_frees==1&&!allocations&&atomic_read(&pdev.dev.refs)==1,"final_binding_drives_cpu_destruction");
 REQUIRE(find_event(E_CORE)>find_event(E_REGION)&&find_event(E_DEVICE)>find_event(E_CORE),"cpu_and_device_destroy_after_hardware");
 /* Driver has freed c; never dereference it again. */
 printf("PASS lifecycle-batch cleanup\n");
}
int main(int argc,char **argv){
 assert(argc==2);int mode=atoi(argv[1]);assert(mode>=0&&mode<11);setup();
 if(mode!=3&&mode!=7&&mode!=8){construct_active_instance();if(mode!=2)construction_quarantine();}
 if(mode==2){assert(iris_device_detach(c)==-EBUSY);assert_retained();assert(c->open_owners==1&&!c->remove_prepared);construction_quarantine();}
 if(mode==3){assert(!iris_core_open_get(c));assert(iris_device_detach(c)==-EBUSY);assert_retained();assert(c->open_inflight==1&&!c->remove_prepared);iris_core_open_finish(c,false);}
 if(mode==7){struct video_device *retained=c->vdev_dec;retained->refs++;assert(iris_device_detach(c)==-EBUSY);assert_retained();assert(c->video_owners==1&&!c->remove_prepared);video_put(retained);}
 if(mode==1){shutdown_error=-EACCES;assert(iris_device_detach(c)==-EACCES);assert_retained();assert(c->shutdown_pm_held&&atomic_read(&c->dev->power.usage_count)==2&&c->orphan_owners==1&&!c->remove_prepared);int gets=pm_gets;shutdown_error=0;assert(!iris_device_detach(c));REQUIRE(pm_gets==gets,"shutdown_retry_does_not_double_get");assert(shutdowns==2);}
 else if(mode==6){power_error=-EIO;assert(iris_device_detach(c)==-EIO);assert_retained();assert(c->firmware_shutdown_verified&&!c->firmware_may_run&&c->shutdown_pm_held);int gets=pm_gets;power_error=0;assert(!iris_device_detach(c));REQUIRE(pm_gets==gets&&shutdowns==1,"power_retry_keeps_pm_and_skips_verified_shutdown");}
 else if(mode==4){
  barrier_gate=true;launch_detach();model_lock();while(!barrier_entered&&!detach_done)wait_change();REQUIRE(barrier_entered&&!detach_done&&!irq_frees&&!core_frees,"controller_observed_pm_barrier_before_free");model_unlock();
  /* External controller admits borrower; barrier itself creates/releases none. */
  pm_runtime_get_noresume(c->dev);model_lock();barrier_release=true;notify();model_unlock();assert(finish_detach()==-EBUSY);
  REQUIRE(c->hardware_owner_held&&irq_live&&mapped&&!irq_frees&&!core_frees&&c->remove_prepared&&!c->runtime_pm_enabled&&!c->pm_guard_held,"pm_borrower_retains_hardware_after_barrier");
  int gets=pm_gets;pm_runtime_put_noidle(c->dev);assert(!iris_device_detach(c));REQUIRE(pm_gets==gets&&barriers==1&&pm_disables==1,"prepared_retry_without_fresh_resume");
 }
 else if(mode==5){
  response_gate=true;launch_response();launch_detach();model_lock();while(!sync_waiting&&!detach_done)wait_change();
  REQUIRE(sync_waiting&&!detach_done&&!response_release&&!queue_frees&&!irq_frees&&!instance_frees,"passive_sync_does_not_release_callback");
  assert(pthread_equal(controller,pthread_self()));response_release=true;notify();model_unlock();assert(!finish_detach());assert(!pthread_join(irq_worker,NULL));
 }
 else if(mode==10){
  buffer_error=-ENOMEM;assert(iris_device_detach(c)==-ENOMEM);
  REQUIRE(instance&&!list_empty(&c->orphan_instances)&&c->orphan_owners==1&&instance->orphaned&&instance->m2m_ctx&&instance->m2m_dev&&!instance_frees&&!framework_frees,"buffer_failure_preserves_instance_list_and_framework");
  REQUIRE(buffer_frees==1&&!instance->internal[0]&&instance->internal[1]&&c->hardware_owner_held&&irq_live&&mapped&&!irq_frees&&!core_frees&&refcount_read(&c->owner_refs)==3&&atomic_read(&c->dev->refs)==3,"partial_buffer_retirement_keeps_core_device_and_hardware_owners");
  REQUIRE(c->firmware_shutdown_verified&&queue_frees==2&&!c->remove_prepared,"buffer_failure_occurs_after_real_quiesce");
  int gets=pm_gets,secure=shutdowns;buffer_error=0;assert(!iris_device_detach(c));
  REQUIRE(instance_frees==1&&framework_frees==3&&buffer_frees==2&&pm_gets==gets&&shutdowns==secure,"buffer_retry_exactly_once_without_fresh_shutdown");
 }
 else if(mode==9){assert(iris_core_prepare_remove(c)==-EBUSY);assert(!iris_core_quiesce(c));assert(!iris_reclaim_orphan_instances(c));assert(instance_frees==1);assert(!iris_core_prepare_remove(c));assert(!iris_device_detach(c));}
 else assert(!iris_device_detach(c));
 assert(instance_frees==(mode!=3&&mode!=7&&mode!=8));assert(framework_frees==3*instance_frees&&buffer_frees==2*instance_frees);verify_and_destroy();return 0;
}
'''


def source(work):
    """Borrow model fragments; selected coordinator definitions remain verbatim."""
    probe = (work / 'iris_probe.c').read_text()
    core = (work / 'iris_core.c').read_text()
    common = (work / 'iris_hfi_common.c').read_text()
    vidc = (work / 'iris_vidc.c').read_text()
    queue = (work / 'iris_hfi_queue.c').read_text()
    firmware = (work / 'iris_firmware.c').read_text()
    header = (work / 'iris_hfi_common.h').read_text()
    tracker = header[header.index('struct iris_hfi_pending {'):header.index('enum hfi_packet_port_type {')]
    provider_types = HARDWARE.PREFIX[HARDWARE.PREFIX.index('struct icc_info {'):HARDWARE.PREFIX.index('struct iris_core {')]
    # Small platform data declaration is a host-provider ABI, not driver proof.
    callbacks = HARDWARE.PREFIX[HARDWARE.PREFIX.index('static struct resource *platform_get_resource('):HARDWARE.PREFIX.index('static int iris_hfi_isr(')]
    callbacks = callbacks.replace('core.dev', 'c->dev').replace('core.resets', 'c->resets').replace('core.controller_resets', 'c->controller_resets')
    callbacks = callbacks.replace('assert(n==1&&p);if(p==',
                                  'REQUIRE(!irq_live,"provider_release_after_irq_free");assert(n==1&&p);if(p==')
    prefix = PREFIX.replace('/* LOCKS */', PASSIVE.LOCKS).replace('/* TRACKER */', tracker)
    prefix = prefix.replace('/* PROVIDER_TYPES */', provider_types).replace('/* PROVIDER_CALLBACKS */', callbacks)
    # Actual unload needs just the provider's synthetic PAS identifier.
    prefix = prefix.replace('u32 clk_rst_tbl_size,controller_rst_tbl_size;};',
                            'u32 clk_rst_tbl_size,controller_rst_tbl_size,pas_id;};')
    prefix = prefix.replace('clocks,1,reset_names,reset_names,1,1};', 'clocks,1,reset_names,reset_names,1,1,17};')
    names = {
        'core': ('iris_core_release', 'iris_core_get', 'iris_core_put',
                 'iris_core_pm_guard_get', 'iris_core_pm_guard_put', 'iris_core_pm_put',
                 'iris_core_open_get', 'iris_core_open_finish', 'iris_core_open_put',
                 'iris_core_orphan_transfer', 'iris_core_orphan_put',
                 'iris_core_quiesce', 'iris_core_prepare_remove'),
        'probe': ('iris_init_icc', 'iris_init_power_domains', 'iris_init_clocks',
                  'iris_init_reset_table', 'iris_init_resets', 'iris_init_resources',
                  'iris_release_resource_handles', 'iris_release_mmio', 'iris_acquire_mmio',
                  'iris_request_irq', 'iris_resource_release_gate', 'iris_release_resources',
                  'iris_video_device_release', 'iris_binding_release',
                  'iris_register_video_device', 'iris_device_detach'),
        'common': ('iris_irq_activate', 'iris_irq_terminal_mask', 'iris_irq_retire_top',
                   'iris_irq_unregister'),
        'vidc': ('iris_orphan_reclaim_gate', 'iris_reclaim_orphan_instances'),
        'queue': ('iris_hfi_queue_set_header', 'iris_hfi_queue_init', 'iris_hfi_queue_deinit',
                  'iris_hfi_queues_init', 'iris_hfi_queues_deinit'),
        'firmware': ('iris_fw_unload',),
    }
    utils = (work / 'iris_utils.c').read_text()
    texts = {'core': core, 'probe': probe, 'common': common, 'vidc': vidc,
             'queue': queue, 'firmware': firmware, 'utils': utils}
    names['utils'] = ('iris_put_response_instance',)
    definitions = [EXTRACT.definition(texts[group], name)
                   for group, group_names in names.items() for name in group_names]
    definitions.extend(EXTRACT.block(common, r'^irqreturn_t\s+' + name + r'\([^;{}]*\)\s*\{', name)
                       for name in ('iris_hfi_isr', 'iris_hfi_isr_handler'))
    # Prototypes prevent extraction order from altering or replacing coordinators.
    prototypes = []
    for definition in definitions:
        prototypes.append(definition[:definition.index('{')].strip() + ';\n')
    return prefix + '\n'.join(prototypes) + '\n'.join(definitions) + MAIN


MUTATIONS = {
    'missing_quiesce_sync': ('\tsynchronize_irq(core->irq);', '\t(void)core->irq;',
                             'iris_core_quiesce', 5, 'cancel_requires_irq_idle_after_sync'),
    'missing_pm_recheck': ('if (atomic_read(&core->dev->power.usage_count)) {',
                          'if (false) {', 'iris_release_resources', 4, 'free_irq_no_pm_users'),
    'skipped_free_irq': ('\tfree_irq(core->irq, core);', '\t(void)free_irq; (void)core->irq;',
                         'iris_irq_unregister', 0, 'provider_release_after_irq_free'),
}


def mutate(text, name):
    before, after, function, _, _ = MUTATIONS[name]
    definition = EXTRACT.definition(text, function)
    if definition.count(before) != 1 or text.count(definition) != 1:
        raise ValueError('Missing/ambiguous mutation anchor: ' + name)
    return text.replace(definition, definition.replace(before, after, 1), 1)


def compile_fixture(text, directory, sanitize=False):
    src = directory / 'lifecycle_batch.c'
    src.write_text(text)
    binary = directory / 'lifecycle_batch'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1',
            '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, cwd=directory, env=ENV, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError('Fixture compile failed:\n' + result.stdout + result.stderr)
    return binary


def run_fixture(binary, mode):
    return subprocess.run([str(binary), str(mode)], cwd=binary.parent, env=ENV,
                          capture_output=True, text=True, timeout=10)


class LifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-lifecycle-host-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.text = source(WORK)
        baseline = root / 'baseline'
        baseline.mkdir()
        cls.binary = compile_fixture(cls.text, baseline)
        cls.mutants = {}
        for name in MUTATIONS:
            target = root / name
            target.mkdir()
            cls.mutants[name] = compile_fixture(mutate(cls.text, name), target)

    def case(self, mode):
        result = run_fixture(self.binary, mode)
        self.assertEqual(result.returncode, 0, CASES[mode] + '\n' + result.stdout + result.stderr)

    def mutation(self, name):
        _, _, _, mode, label = MUTATIONS[name]
        result = run_fixture(self.mutants[name], mode)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('Assertion failed: ' + label, result.stderr)
        self.assertNotIn('MODEL_TIMEOUT', result.stderr)
        self.assertNotIn('AddressSanitizer', result.stderr)
        self.assertNotIn('runtime error:', result.stderr)


for mode, name in enumerate(CASES):
    def case_test(self, value=mode):
        self.case(value)
    setattr(LifecycleTests, 'test_case_' + name, case_test)
for name in MUTATIONS:
    def mutation_test(self, value=name):
        self.mutation(value)
    setattr(LifecycleTests, 'test_mutation_' + name, mutation_test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--compile-only', action='store_true',
                        help='Compile baseline and mutations without asserting final-source behavior')
    args = parser.parse_args()
    WORK = args.work.resolve()
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if not Path('/usr/bin/gcc').is_file() or not WORK.is_dir():
        print('ENVIRONMENT ERROR: WSL GCC or candidate unavailable', file=sys.stderr)
        return 2
    text = source(WORK)
    digest = hashlib.sha256(text.encode()).hexdigest()
    print('Extracted host fixture SHA256 ' + digest, flush=True)
    if args.compile_only or args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-lifecycle-check-') as temporary:
            root = Path(temporary)
            binary = compile_fixture(text, root, args.sanitize)
            if args.compile_only:
                for name in MUTATIONS:
                    target = root / name
                    target.mkdir()
                    compile_fixture(mutate(text, name), target, args.sanitize)
                print('Baseline + 3 mutations compiled; no lifecycle pass claim')
                return 0
            for mode, name in enumerate(CASES):
                result = run_fixture(binary, mode)
                if result.returncode:
                    print(name + '\n' + result.stdout + result.stderr, file=sys.stderr)
                    return 1
            for name, (_, _, _, mode, label) in MUTATIONS.items():
                target = root / name
                target.mkdir()
                mutant = compile_fixture(mutate(text, name), target, True)
                result = run_fixture(mutant, mode)
                if (not result.returncode or 'Assertion failed: ' + label not in result.stderr
                        or any(marker in result.stderr for marker in
                               ('MODEL_TIMEOUT', 'AddressSanitizer', 'runtime error:'))):
                    print(name + '\n' + result.stdout + result.stderr, file=sys.stderr)
                    return 1
        print('11 lifecycle-batch ASan/UBSan scenarios passed; 3 sanitized compiling mutations rejected; '
              'explicit host seams, no DMA/device proof')
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromTestCase(LifecycleTests))
    if result.wasSuccessful():
        print('11 actual cleanup-chain cases + 3 compiling invariant mutations passed; '
              '5s event / 10s process / 40s compile bounds; host seams, no probe/open/DMA/device proof')
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
