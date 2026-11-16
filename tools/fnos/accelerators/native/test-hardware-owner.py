#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual resource acquire/release/PM helpers with provider/device/DMA host seams.

PM guard put and IRQ unregister are contract seams here, not extracted lifetime
proof. Actual core/IRQ lifetime helpers are tested by test-pm-irq-release.py.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('extract', HERE / 'test-decode-cleanup.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None
CASES = 40
GUARD_CASES = 4

PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <limits.h>
typedef uint32_t u32;
typedef uintptr_t resource_size_t;
#define GFP_KERNEL 0
#define IORESOURCE_MEM 1
#define IRQF_TRIGGER_HIGH 1
#define IRQF_NO_AUTOEN 2
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define PD_FLAG_NO_DEV_LINK 1
#define PD_FLAG_DEV_LINK_ON 2
#define PD_FLAG_REQUIRED_OPP 4
#define IRIS_HW_CLK 1
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define resource_size(r) ((r)->end-(r)->start+1)
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
static bool list_empty(struct list_head *h){return h->next==h;}
struct device {struct {int usage_count;} power;};
#define atomic_read(p) (*(p))
struct resource {resource_size_t start,end;};
struct platform_device {struct device dev;struct resource res;};
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
struct icc_info {const char *name;u32 bw_min_kbps;};
struct icc_bulk_data {const char *name;u32 avg_bw,peak_bw;};
struct clk_bulk_data {int unused;};
struct reset_control_bulk_data {const char *id;};
struct dev_pm_domain_list {int id;};
struct dev_pm_domain_attach_data {const char * const *pd_names;u32 num_pd_names,pd_flags;};
struct platform_clk_data {int clk_type;const char *clk_name;};
struct iris_platform_data {struct icc_info *icc_tbl;u32 icc_tbl_size;const char * const *pmdomain_tbl;const char * const *opp_pd_tbl;u32 pmdomain_tbl_size,opp_pd_tbl_size;struct platform_clk_data *clk_tbl;u32 clk_tbl_size;const char * const *clk_rst_tbl;const char * const *controller_rst_tbl;u32 clk_rst_tbl_size,controller_rst_tbl_size;};
struct iris_core {struct device *dev;struct mutex lock,lifecycle_lock,remove_lock,quiesce_lock;struct list_head instances,orphan_instances;struct delayed_work sys_error_handler;struct iris_platform_data *iris_platform_data;
 struct {bool opp_dirty;} power_progress;int power_opp_error;
 bool hardware_owner_held,resources_releasing,irq_registered,runtime_pm_enabled,mmio_claimed,mmio_mapped,icc_acquired,clocks_acquired,domains_attached,opp_domains_attached,opp_table_added;
 resource_size_t mmio_start,mmio_size;void *reg_base;int irq;struct icc_bulk_data *icc_tbl;u32 icc_count;struct clk_bulk_data *clock_tbl;u32 clk_count;struct reset_control_bulk_data *resets,*controller_resets;struct dev_pm_domain_list *pmdomain_tbl,*opp_pmdomain_tbl;int *opp_tokens;u32 opp_token_count,reset_count,controller_reset_count;
 bool removing,remove_prepared,terminal_shutdown,firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified,firmware_may_run,queues_allocated,power_prepared,shutdown_pm_held,power_state_unknown;u32 state,open_inflight,open_owners,orphan_owners;int cpu_refs;};
static bool iris_power_progress_pending(struct iris_core *c){(void)c;return false;}
static struct iris_core core;
static struct platform_device pdev;
static struct dev_pm_domain_list domains[2]={{1},{2}};
static struct icc_info icc[]={ {"video-mem",1} };
static struct platform_clk_data clocks[]={ {IRIS_HW_CLK,"video"} };
static const char * const pd_names[]={"ctrl","hw"};
static const char * const reset_names[]={"video"};
static struct iris_platform_data platform={icc,1,pd_names,pd_names,2,2,clocks,1,reset_names,reset_names,1,1};
static int fail_at,step,allocations,claimed,mapped,icc_live,pd_live,opp_pd_live,token_live,table_live,clock_live,reset_live,ctrl_reset_live,irq_live;
static int fail_errno=-EIO,pm_error,opp_error,pm_value,pm_gets,pm_puts,rate_calls,barriers,pm_disables,irq_syncs,cancels,irq_frees;
static bool self_work,interrupt_context,missing_resource,pm_user_on_barrier;
static int events[128],event_count;
enum {E_PM=1,E_IRQ,E_WORK,E_RESET_CTRL,E_RESET,E_CLOCK,E_TABLE,E_TOKEN,E_OPP_PD,E_PD,E_ICC,E_MAP,E_REGION};
static void event(int e){assert(event_count<128);events[event_count++]=e;}
static bool fail(void){step++;return step==fail_at;}
static void *kzalloc(size_t n,int flags){(void)flags;if(fail())return NULL;void *p=calloc(1,n?n:1);assert(p);allocations++;return p;}
static void *kcalloc(size_t n,size_t size,int flags){return kzalloc(n*size,flags);}
static void kfree(void *p){if(p){assert(allocations>0);allocations--;free(p);}}
static struct resource *platform_get_resource(struct platform_device *p,int type,int index){assert(p==&pdev&&type==IORESOURCE_MEM&&!index);return missing_resource?NULL:&p->res;}
static const char *dev_name(struct device *dev){assert(dev==core.dev);return "iris";}
static void *request_mem_region(resource_size_t start,resource_size_t size,const char *name){assert(start==4096&&size==4096&&!strcmp(name,"iris"));if(fail())return NULL;claimed++;return &pdev.res;}
static void release_mem_region(resource_size_t start,resource_size_t size){assert(start==4096&&size==4096&&claimed==1&&!mapped);claimed--;event(E_REGION);}
static void *ioremap(resource_size_t start,resource_size_t size){assert(start==4096&&size==4096&&claimed==1);if(fail())return NULL;mapped++;return &pdev.res;}
static void iounmap(void *p){assert(p==&pdev.res&&mapped==1&&!irq_live&&!icc_live&&!pd_live&&!opp_pd_live&&!token_live&&!table_live&&!clock_live&&!reset_live&&!ctrl_reset_live);mapped--;event(E_MAP);}
static int of_icc_bulk_get(struct device *d,int n,struct icc_bulk_data *p){assert(d==core.dev&&n==1&&p);if(fail())return fail_errno;icc_live++;return 0;}
static void icc_bulk_put(int n,struct icc_bulk_data *p){assert(n==1&&p&&icc_live==1&&!pd_live&&!opp_pd_live&&!token_live&&!table_live&&!clock_live&&!reset_live&&!ctrl_reset_live);icc_live--;event(E_ICC);}
static int dev_pm_domain_attach_list(struct device *d,const struct dev_pm_domain_attach_data *data,struct dev_pm_domain_list **out){assert(d==core.dev&&icc_live);if(fail())return fail_errno;bool opp=data->pd_flags!=(u32)PD_FLAG_NO_DEV_LINK;*out=&domains[opp];if(opp){assert(pd_live);opp_pd_live++;}else pd_live++;return 2;}
static void dev_pm_domain_detach_list(struct dev_pm_domain_list *p){assert(!token_live&&!table_live&&!clock_live&&!reset_live&&!ctrl_reset_live);if(p==&domains[1]){assert(opp_pd_live==1);opp_pd_live--;event(E_OPP_PD);}else{assert(p==&domains[0]&&pd_live==1&&!opp_pd_live);pd_live--;event(E_PD);}}
static int dev_pm_opp_set_clkname(struct device *d,const char *name){assert(d==core.dev&&!strcmp(name,"video")&&opp_pd_live);if(fail())return fail_errno;token_live++;return 37;}
static void dev_pm_opp_clear_config(int token){assert(token==37&&token_live==1&&!table_live&&!clock_live);token_live--;event(E_TOKEN);}
static int dev_pm_opp_of_add_table(struct device *d){assert(d==core.dev&&token_live);if(fail())return fail_errno;table_live++;return 0;}
static void dev_pm_opp_of_remove_table(struct device *d){assert(d==core.dev&&table_live==1&&!clock_live);table_live--;event(E_TABLE);}
static int clk_bulk_get_all(struct device *d,struct clk_bulk_data **out){assert(d==core.dev&&table_live);if(fail())return fail_errno;*out=calloc(1,sizeof(**out));assert(*out);clock_live++;return 1;}
static void clk_bulk_put_all(int n,struct clk_bulk_data *p){assert(n==1&&p&&clock_live==1&&!reset_live&&!ctrl_reset_live);clock_live--;free(p);event(E_CLOCK);}
static int reset_control_bulk_get_exclusive(struct device *d,int n,struct reset_control_bulk_data *p){assert(d==core.dev&&n==1&&p&&clock_live);if(fail())return fail_errno;if(p==core.resets)reset_live++;else{assert(p==core.controller_resets&&reset_live);ctrl_reset_live++;}return 0;}
static void reset_control_bulk_put(int n,struct reset_control_bulk_data *p){assert(n==1&&p);if(p==core.controller_resets){assert(ctrl_reset_live==1);ctrl_reset_live--;event(E_RESET_CTRL);}else{assert(p==core.resets&&reset_live==1&&!ctrl_reset_live);reset_live--;event(E_RESET);}}
static int iris_hfi_isr(int irq,void *data){(void)irq;(void)data;return 0;}
static int iris_hfi_isr_handler(int irq,void *data){(void)irq;(void)data;return 0;}
static int request_threaded_irq(int irq,int (*top)(int,void *),int (*thread)(int,void *),u32 flags,const char *name,void *data){assert(irq==9&&top==iris_hfi_isr&&thread==iris_hfi_isr_handler&&data==&core&&!strcmp(name,"iris"));assert(flags&(u32)IRQF_NO_AUTOEN);if(fail())return fail_errno;irq_live++;return 0;}
static void check_locks(void){assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));assert(!pthread_mutex_trylock(&core.lifecycle_lock.lock));assert(!pthread_mutex_unlock(&core.lifecycle_lock.lock));}
static void free_irq(int irq,void *data){assert(irq==9&&data==&core&&irq_live==1);check_locks();irq_live--;irq_frees++;event(E_IRQ);}
static void synchronize_irq(int irq){assert(irq==9&&core.resources_releasing);check_locks();irq_syncs++;}
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return interrupt_context;}
static bool cancel_delayed_work_sync(struct delayed_work *w){assert(w==&core.sys_error_handler);check_locks();cancels++;event(E_WORK);return false;}
static void pm_runtime_barrier(struct device *d){assert(d==core.dev&&core.resources_releasing);check_locks();barriers++;event(E_PM);if(pm_user_on_barrier)d->power.usage_count++;}
static void pm_runtime_disable(struct device *d){assert(d==core.dev&&barriers);pm_disables++;}
static void pm_runtime_dont_use_autosuspend(struct device *d){assert(d==core.dev&&pm_disables);}
static void iris_core_get(struct iris_core *c){assert(c==&core&&c->cpu_refs>0);c->cpu_refs++;}
static void iris_core_put(struct iris_core *c){assert(c==&core&&c->cpu_refs>1);c->cpu_refs--;}
static int dev_pm_opp_set_rate(struct device *d,unsigned long rate){assert(d==core.dev&&rate==ULONG_MAX);rate_calls++;return opp_error;}
static int pm_runtime_get_sync(struct device *d){assert(d==core.dev);d->power.usage_count++;pm_gets++;return pm_error?pm_error:pm_value;}
static void pm_runtime_put_noidle(struct device *d){assert(d==core.dev&&d->power.usage_count>0);d->power.usage_count--;pm_puts++;}
'''

IRQ_LOCK = r'''
#define HAS_IRQ_LOCK 1
typedef struct {pthread_mutex_t lock;} raw_spinlock_t;
#define raw_spin_lock_init(p) assert(!pthread_mutex_init(&(p)->lock,NULL))
#define raw_spin_lock_irqsave(p,f) ((f)=0,assert(!pthread_mutex_lock(&(p)->lock)))
#define raw_spin_unlock_irqrestore(p,f) ((void)(f),assert(!pthread_mutex_unlock(&(p)->lock)))
'''

PM_GUARD_SEAM = r'''
#define HAS_PM_GUARD 1
static int guard_put_calls;
/* Contract seam: drop the real usage reference once, under the core lock. */
static void iris_core_pm_guard_put(struct iris_core *c){assert(c==&core);int r=pthread_mutex_trylock(&c->lock.lock);assert(r==EBUSY);if(!r)pthread_mutex_unlock(&c->lock.lock);guard_put_calls++;if(c->pm_guard_held){pm_runtime_put_noidle(c->dev);c->pm_guard_held=false;}}
'''

IRQ_UNREGISTER_SEAM = r'''
#define HAS_IRQ_UNREGISTER 1
/* Contract seam, not actual IRQ lifetime proof (see test-pm-irq-release.py). */
static int iris_irq_unregister(struct iris_core *c){assert(c==&core&&c->resources_releasing&&c->remove_prepared&&c->terminal_shutdown&&c->irq_drain_verified&&c->recovery_drain_verified);check_locks();int r=pthread_mutex_trylock(&c->quiesce_lock.lock);assert(r==EBUSY);if(!r)pthread_mutex_unlock(&c->quiesce_lock.lock);if(!c->irq_registered)return 0;assert(!c->irq_releasing&&(!c->irq_started||c->terminal_shutdown));mutex_lock(&c->lock);unsigned long flags;raw_spin_lock_irqsave(&c->irq_lock,flags);c->irq_accepting=false;c->irq_releasing=true;raw_spin_unlock_irqrestore(&c->irq_lock,flags);mutex_unlock(&c->lock);synchronize_irq(c->irq);free_irq(c->irq,c);mutex_lock(&c->lock);raw_spin_lock_irqsave(&c->irq_lock,flags);c->irq_registered=false;c->irq_started=false;c->irq_initial_mask_held=false;c->irq_top_mask_held=false;c->quiesce_irq_mask_held=false;c->power_irq_enabled=false;
#ifdef HAS_IRQ_PROBE_UNPUBLISHED
 c->irq_probe_unpublished=false;
#endif
 raw_spin_unlock_irqrestore(&c->irq_lock,flags);mutex_unlock(&c->lock);return 0;}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));memset(&pdev,0,sizeof(pdev));pdev.res.start=4096;pdev.res.end=8191;core.dev=&pdev.dev;core.irq=9;core.iris_platform_data=&platform;core.cpu_refs=2;core.hardware_owner_held=true;assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.remove_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));
#ifdef HAS_IRQ_LOCK
 raw_spin_lock_init(&core.irq_lock);
#endif
 INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);}
static void fully_prepared(void){core.removing=core.remove_prepared=core.terminal_shutdown=core.firmware_shutdown_verified=core.irq_drain_verified=core.recovery_drain_verified=true;core.state=IRIS_CORE_DEINIT;}
static int acquire(void){int ret=iris_acquire_mmio(&core,&pdev);if(ret)return ret;ret=iris_init_resources(&core);if(ret)return ret;return iris_request_irq(&core);}
static void assert_empty(void){assert(!allocations&&!claimed&&!mapped&&!icc_live&&!pd_live&&!opp_pd_live&&!token_live&&!table_live&&!clock_live&&!reset_live&&!ctrl_reset_live&&!irq_live);assert(!core.mmio_claimed&&!core.mmio_mapped&&!core.icc_acquired&&!core.clocks_acquired&&!core.domains_attached&&!core.opp_domains_attached&&!core.opp_table_added&&!core.opp_token_count&&!core.reset_count&&!core.controller_reset_count);
#ifdef HAS_IRQ_UNREGISTER
 if(core.resources_releasing)assert(!core.irq_registered&&!core.irq_started&&!core.irq_initial_mask_held&&!core.irq_top_mask_held&&!core.quiesce_irq_mask_held&&!core.irq_accepting&&!core.power_irq_enabled&&core.irq_releasing);
#ifdef HAS_IRQ_PROBE_UNPUBLISHED
 if(core.resources_releasing)assert(!core.irq_probe_unpublished);
#endif
#endif
}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode>=32&&mode<=35){pm_error=mode==32?-EIO:0;opp_error=mode==33?-ERANGE:0;pm_value=mode==34?1:0;int r=iris_enable_power_domains(&core,core.dev);assert(r==(mode==32?-EIO:mode==33?-ERANGE:0));if(mode==32){assert(pm_gets==1&&pm_puts==1&&!core.dev->power.usage_count);}else if(mode==33){assert(!pm_gets&&!pm_puts);}else{assert(pm_gets==1&&!pm_puts&&core.dev->power.usage_count==1);}return 0;}
 if(mode==36||mode==37){missing_resource=mode==36;if(mode==37)pdev.res.end=4095;assert(iris_acquire_mmio(&core,&pdev)==-EINVAL);assert_empty();return 0;}
 if(mode==38){core.hardware_owner_held=false;core.cpu_refs=1;assert(!iris_release_resources(&core));assert(core.cpu_refs==1&&!event_count&&!core.resources_releasing);return 0;}
 if(mode>=1&&mode<=15)fail_at=mode;
 int ret=acquire();
 if(mode>=1&&mode<=15){assert(ret==(mode==1?-EBUSY:(mode==2||mode==3||mode==7||mode==11||mode==13)?-ENOMEM:-EIO));assert(step==mode);iris_release_resource_handles(&core);iris_release_mmio(&core);assert_empty();iris_release_resource_handles(&core);iris_release_mmio(&core);assert_empty();return 0;}
 assert(!ret&&step==15&&core.opp_token_count==1&&core.opp_tokens[0]==37);fully_prepared();core.runtime_pm_enabled=true;
#ifdef HAS_PM_GUARD
 if(mode>=40&&mode<=43){core.pm_guard_held=true;core.dev->power.usage_count=1;}
#endif
 int expected=0;
 if(mode==16){core.remove_prepared=false;expected=-EAGAIN;}
 if(mode==17){core.removing=false;expected=-EAGAIN;}
 if(mode==18){core.terminal_shutdown=false;expected=-EAGAIN;}
 if(mode==19){core.irq_drain_verified=false;expected=-EAGAIN;}
 if(mode==20){core.recovery_drain_verified=false;expected=-EAGAIN;}
 if(mode==21){core.firmware_shutdown_verified=false;expected=-EAGAIN;}
 if(mode==22){core.open_inflight=1;expected=-EBUSY;}
 if(mode==23){core.open_owners=1;expected=-EBUSY;}
 if(mode==24){core.orphan_owners=1;expected=-EBUSY;}
 if(mode==25){core.firmware_may_run=true;expected=-EBUSY;}
 if(mode==26){core.queues_allocated=true;expected=-EBUSY;}
 if(mode==27){core.power_prepared=true;expected=-EBUSY;}
 if(mode==28){core.shutdown_pm_held=true;expected=-EBUSY;}
 if(mode==29){core.dev->power.usage_count=1;expected=-EBUSY;}
 if(mode==30){self_work=true;expected=-EDEADLK;}
 if(mode==31){interrupt_context=true;expected=-EWOULDBLOCK;}
#ifdef HAS_PM_GUARD
 if(mode==41){core.dev->power.usage_count=2;expected=-EBUSY;}
 if(mode==42){core.dev->power.usage_count=-1;expected=-EBUSY;}
#endif
 if(mode==39||mode==43){pm_user_on_barrier=true;assert(iris_release_resources(&core)==-EBUSY);assert(core.hardware_owner_held&&core.resources_releasing&&irq_live&&mapped&&token_live&&!irq_frees&&!irq_syncs&&!cancels&&core.cpu_refs==2&&core.dev->power.usage_count==1);
#ifdef HAS_PM_GUARD
  if(mode==43)assert(!core.pm_guard_held&&pm_puts==1&&guard_put_calls==1);
#endif
  core.dev->power.usage_count=0;pm_user_on_barrier=false;assert(!iris_release_resources(&core));assert_empty();assert(!core.hardware_owner_held&&core.cpu_refs==1&&barriers==1&&pm_disables==1&&irq_frees==1&&irq_syncs==1&&cancels==1);
#ifdef HAS_PM_GUARD
  if(mode==43)assert(!core.pm_guard_held&&!core.dev->power.usage_count&&pm_puts==1&&guard_put_calls==2);
#endif
  int count=event_count;assert(!iris_release_resources(&core));assert(core.cpu_refs==1&&event_count==count&&irq_frees==1);
#ifdef HAS_PM_GUARD
  if(mode==43)assert(pm_puts==1&&guard_put_calls==2);
#endif
  return 0;}
 assert(iris_release_resources(&core)==expected);
 if(expected){assert(!event_count&&!core.resources_releasing&&core.hardware_owner_held&&core.cpu_refs==2&&irq_live&&mapped&&token_live);
#ifdef HAS_PM_GUARD
  assert(!pm_puts&&!guard_put_calls);if(mode==41||mode==42)assert(core.pm_guard_held&&core.dev->power.usage_count==(mode==41?2:-1));
#endif
  return 0;}
#ifdef HAS_PM_GUARD
 if(mode==40)assert(!core.pm_guard_held&&!core.dev->power.usage_count&&pm_puts==1&&guard_put_calls==1&&!pm_gets);
#endif
 assert_empty();assert(core.resources_releasing&&!core.hardware_owner_held&&core.cpu_refs==1&&barriers==1&&pm_disables==1&&irq_frees==1&&irq_syncs==1&&cancels==1);
 int want[]={E_PM,E_IRQ,E_WORK,E_RESET_CTRL,E_RESET,E_CLOCK,E_TABLE,E_TOKEN,E_OPP_PD,E_PD,E_ICC,E_MAP,E_REGION};assert(event_count==(int)ARRAY_SIZE(want));assert(!memcmp(events,want,sizeof(want)));assert(!iris_release_resources(&core));assert(core.cpu_refs==1&&event_count==(int)ARRAY_SIZE(want));return 0;}
'''


def source(work):
    probe = (work / 'iris_probe.c').read_text()
    resources = (work / 'iris_resources.c').read_text()
    names = ('iris_init_icc', 'iris_init_power_domains', 'iris_init_clocks', 'iris_init_reset_table',
             'iris_init_resets', 'iris_init_resources', 'iris_release_resource_handles', 'iris_release_mmio',
             'iris_acquire_mmio', 'iris_request_irq', 'iris_resource_release_gate', 'iris_release_resources')
    functions = ''.join(EXTRACT.definition(probe, name) for name in names)
    functions += EXTRACT.definition(resources, 'iris_enable_power_domains')
    prefix = PREFIX
    if 'core->power_irq_enabled' in EXTRACT.definition(probe, 'iris_resource_release_gate'):
        prefix = prefix.replace('shutdown_pm_held,power_state_unknown;', 'shutdown_pm_held,power_state_unknown,power_irq_enabled;')
    if 'core->irq_managed' in probe:
        prefix = prefix.replace('bool hardware_owner_held,', 'bool irq_managed,irq_initial_mask_held,hardware_owner_held,')
        prefix += '\nstatic bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return true;}\n'
    request = EXTRACT.definition(probe, 'iris_request_irq')
    release = EXTRACT.definition(probe, 'iris_release_resources')
    if 'core->irq_lock' in request or 'iris_irq_unregister(core)' in release:
        prefix = prefix.replace('struct iris_core {', IRQ_LOCK + '\nstruct iris_core {raw_spinlock_t irq_lock;')
        prefix = prefix.replace('static void check_locks(void){', 'static void check_locks(void){assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));')
    if 'core->irq_probe_unpublished' in request:
        prefix = prefix.replace('hardware_owner_held,', 'irq_probe_unpublished,hardware_owner_held,')
        prefix += '\n#define HAS_IRQ_PROBE_UNPUBLISHED 1\n'
    if 'iris_core_pm_guard_put(core)' in release:
        prefix = prefix.replace('hardware_owner_held,', 'pm_guard_held,hardware_owner_held,')
        prefix += PM_GUARD_SEAM
    if 'iris_irq_unregister(core)' in release:
        prefix = prefix.replace('hardware_owner_held,', 'irq_releasing,irq_accepting,irq_started,irq_top_mask_held,quiesce_irq_mask_held,hardware_owner_held,')
        prefix = prefix.replace('check_locks();irq_live--;', 'assert(core.irq_releasing&&!core.irq_accepting&&irq_syncs==1);check_locks();irq_live--;')
        prefix = prefix.replace('check_locks();irq_syncs++;', 'assert(core.irq_registered&&core.irq_releasing&&!core.irq_accepting);check_locks();irq_syncs++;')
        prefix += IRQ_UNREGISTER_SEAM
    return prefix + functions + MAIN


def scenario_count(work):
    release = EXTRACT.definition((work / 'iris_probe.c').read_text(), 'iris_release_resources')
    return CASES + (GUARD_CASES if 'iris_core_pm_guard_put(core)' in release else 0)


def compile_fixture(text, output, sanitize=False):
    src = output / 'hardware.c'
    src.write_text(text)
    binary = output / 'hardware'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class HardwareTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-hardware-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-hardware-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_positive_opp_token(self):
        self.mutation('if (ret < 0)\n\t\t\t\treturn ret;\n\t\t\tcore->opp_tokens',
                      'if (ret)\n\t\t\t\treturn ret;\n\t\t\tcore->opp_tokens', 0)

    def test_oracle_irq_autoenable(self):
        self.mutation('IRQF_TRIGGER_HIGH | IRQF_NO_AUTOEN', 'IRQF_TRIGGER_HIGH', 0)

    def test_oracle_missing_drain_gate(self):
        self.mutation('!core->irq_drain_verified || !core->recovery_drain_verified',
                      '!core->recovery_drain_verified', 19)

    def test_oracle_early_mmio_release(self):
        self.mutation('iris_release_resource_handles(core);\n\tiris_release_mmio(core);',
                      'iris_release_mmio(core);\n\tiris_release_resource_handles(core);', 0)

    def test_oracle_pm_barrier_user(self):
        self.mutation('if (atomic_read(&core->dev->power.usage_count)) {', 'if (false) {', 39)

    def test_oracle_pm_failure_reference(self):
        self.mutation('pm_runtime_put_noidle(pd_dev);', '(void)pm_runtime_put_noidle;', 32)

    def test_oracle_missing_hardware_owner_drop(self):
        self.mutation('iris_core_put(core);\n\t/* The immutable one-way terminal state',
                      '(void)core;\n\t/* The immutable one-way terminal state', 0)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(HardwareTests, 'test_case_%02d' % mode, test)


class GuardHardwareTests(HardwareTests):
    def test_oracle_missing_pm_guard_put(self):
        self.mutation('iris_core_pm_guard_put(core);', '(void)iris_core_pm_guard_put;', 40)

    def test_oracle_missing_pm_guard_gate(self):
        self.mutation('atomic_read(&core->dev->power.usage_count) != (core->pm_guard_held ? 1 : 0)',
                      'false', 41)


for mode in range(CASES, CASES + GUARD_CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(GuardHardwareTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    cases = scenario_count(WORK)
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-hardware-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(cases):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
        print('%d explicit resource acquire/release/PM ASan/UBSan scenarios passed; provider/device/PM guard/IRQ unregister host seams, no DMA or actual core/IRQ lifetime proof' % cases)
        return 0
    tests = GuardHardwareTests if cases > CASES else HardwareTests
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(tests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
