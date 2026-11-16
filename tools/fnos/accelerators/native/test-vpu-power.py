#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual checked VPU2 power/off, core terminal and release-gate functions.

Registers, clocks, domains, OPP, ICC and PM runtime are host seams. This is not
hardware power sequencing evidence, DMA acceptance or platform-remove proof.
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
CASES = 41

PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdio.h>
#include <limits.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>
typedef uint32_t u32;
#define GFP_KERNEL 0
#define BIT(n) (1u << (n))
#define abs(v) iris_abs((long)(v))
#define BW_THRESHOLD 50000
#define dev_err(dev, fmt, ...) ((void)(dev))
#define atomic_read(p) (*(p))
static long iris_abs(long v){return v<0?-v:v;}
#define READ_ONCE(x) (x)
#define clamp_t(type, x, lo, hi) ((type)((x) < (lo) ? (lo) : ((x) > (hi) ? (hi) : (x))))
#define container_of(p,t,m) ((t *)((char *)(p) - offsetof(t,m)))
enum platform_clk_type { IRIS_AXI_CLK, IRIS_CTRL_CLK, IRIS_HW_CLK, IRIS_AXI1_CLK };
enum iris_power_domain_id { IRIS_CTRL_POWER_DOMAIN, IRIS_HW_POWER_DOMAIN };
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define CPU_CS_X2RPMH 0x100
#define AON_WRAPPER_MVP_NOC_LPI_CONTROL 0x200
#define AON_WRAPPER_MVP_NOC_LPI_STATUS 0x204
#define WRAPPER_IRIS_CPU_NOC_LPI_CONTROL 0x300
#define WRAPPER_IRIS_CPU_NOC_LPI_STATUS 0x304
#define WRAPPER_DEBUG_BRIDGE_LPI_CONTROL 0x400
#define WRAPPER_DEBUG_BRIDGE_LPI_STATUS 0x404
#define WRAPPER_TZ_CTL_AXI_CLOCK_CONFIG 0x500
#define WRAPPER_TZ_QNS4PDXFIFO_RESET 0x504
#define MSK_SIGNAL_FROM_TENSILICA BIT(0)
#define MSK_CORE_POWER_ON BIT(1)
#define REQ_POWER_DOWN_PREP BIT(0)
#define CTL_AXI_CLK_HALT BIT(0)
#define CTL_CLK_HALT BIT(1)
#define RESET_HIGH BIT(0)
#define __iomem
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
static bool list_empty(struct list_head *h){return h->next==h;}
struct completion {int done;};
static void complete(struct completion *c){c->done++;}
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
struct iris_hfi_pending {bool active;};
static void iris_hfi_pending_cancel(struct iris_hfi_pending *p){p->active=false;}
struct device {struct {int usage_count;} power;};
struct dev_pm_domain_list {struct device *pd_devs[2];};
struct icc_info {const char *name;u32 bw_min_kbps,bw_max_kbps;};
struct icc_bulk_data {const char *name;u32 avg_bw,peak_bw;};
struct iris_power_progress {bool icc_voted,ctrl_domain_ref,hw_domain_ref,hwmode_set,axi_clk_on,ctrl_clk_on,hw_clk_on;};
struct iris_core_power {u32 clk_freq;unsigned long icc_bw;};
struct iris_core;
struct iris_platform_data;
struct iris_hfi_command_ops {int (*sys_interframe_powercollapse)(struct iris_core *);};
struct iris_power_progress;
struct iris_core;
struct iris_platform_data;
struct vpu_ops {int (*power_on_checked)(struct iris_core *);int (*power_off_checked)(struct iris_core *);};
struct iris_platform_data {const struct vpu_ops *vpu_ops;struct icc_info *icc_tbl;u32 clk_rst_tbl_size;bool no_tz_fifo_reset;
 struct dev_pm_domain_list *pmdomain_tbl;void (*set_preset_registers)(struct iris_core *);};
struct iris_core {struct device *dev;void __iomem *reg_base;int irq;
 struct mutex lock,lifecycle_lock,quiesce_lock,remove_lock;
 struct list_head instances,orphan_instances;struct delayed_work sys_error_handler;
 const struct iris_platform_data *iris_platform_data;struct iris_core_power power;
 struct icc_bulk_data *icc_tbl;u32 icc_count;struct dev_pm_domain_list *pmdomain_tbl;
 void *resets;u32 intr_status;
 struct iris_power_progress power_progress;bool power_state_unknown;int power_error;bool power_irq_enabled;
 const struct iris_hfi_command_ops *hfi_ops;bool firmware_touched;
 bool removing,remove_prepared,terminal_shutdown,irq_quiescing,recovery_blocked;
 bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;
 bool firmware_may_run,queues_allocated,power_prepared,shutdown_pm_held,quiesce_irq_mask_held;
 bool hardware_owner_held,resources_releasing,irq_registered,runtime_pm_enabled;
 u32 open_inflight,open_owners,orphan_owners,state;
 struct iris_hfi_pending init_pending;struct completion core_init_done;};
static struct iris_core core;
static struct device dev;
static struct device pd_devs[2];
static struct dev_pm_domain_list pd_list={{&pd_devs[0],&pd_devs[1]}};
static struct icc_info icc_info_tbl[]={{"video-mem",1000,9000000}};
static struct icc_bulk_data icc_tbl[1]={{"video-mem",0,0}};
static struct vpu_ops platform_ops;
static struct iris_platform_data platform;
static int events[64],event_count;
enum {E_ICC_SET,E_DOM_CTRL,E_RST,E_AXI,E_CTRL_CLK,E_DOM_HW,E_HW_CLK,E_HWMODE_ON,E_OPP_FREQ,E_PRESET,E_INTR,E_IRQ_ON,E_HWMODE_OFF,E_OPP_ZERO,E_AON,E_NOC,E_DBG,E_TZ,E_CTRL_OFF,E_AXI_OFF,E_DOM_CTRL_OFF,E_HW_CLK_OFF,E_DOM_HW_OFF,E_ICC_UNSET};
static void event(int e){assert(event_count<64);events[event_count++]=e;}
static int icc_set_error,icc_unset_error,opp_freq_error,opp_vote_error,opp_zero_error;
static int fail_step,step,reset_error,clk_enable_error[4],clk_disable_error[4],hwmode_error;
static int put_error[2],get_error[2];
static u32 aon_val=1,noc_val=1,dbg_val=0;
static int hwmode_on_calls,hwmode_off_calls,put_calls[2],get_calls[2],put_noidle_calls[2],resume_calls;
static bool tz_seen;
static int clk_en_calls[4],clk_dis_calls[4],irq_enable_calls,disable_calls,legacy_off_calls,legacy_on_calls,legacy_on_error,queue_free_calls,fw_unload_calls,queue_init_calls;
static bool self_work,interrupt_context;
static bool fail(void){step++;return step==fail_step;}
static u32 readl(const void *addr){
 uintptr_t a=(uintptr_t)addr-(uintptr_t)core.reg_base;
 if(a==AON_WRAPPER_MVP_NOC_LPI_STATUS)return aon_val;
 if(a==WRAPPER_IRIS_CPU_NOC_LPI_STATUS)return noc_val;
 if(a==WRAPPER_DEBUG_BRIDGE_LPI_STATUS)return dbg_val;
 assert(0);return 0;}
static void writel(u32 v,const void *addr){(void)v;
 uintptr_t a=(uintptr_t)addr-(uintptr_t)core.reg_base;
 if(a==AON_WRAPPER_MVP_NOC_LPI_CONTROL)event(E_AON);
 else if(a==WRAPPER_IRIS_CPU_NOC_LPI_CONTROL)event(E_NOC);
 else if(a==WRAPPER_DEBUG_BRIDGE_LPI_CONTROL)event(E_DBG);
 else if(a==WRAPPER_TZ_QNS4PDXFIFO_RESET){if(!tz_seen){tz_seen=true;event(E_TZ);}}}
#define readl_poll_timeout(addr, val, cond, delay_us, timeout_us) \
 ({ int __r; (val) = readl(addr); __r = (cond) ? 0 : -ETIMEDOUT; __r; })
static int icc_bulk_set_bw(int n,const struct icc_bulk_data *p){assert(n==1&&p==icc_tbl);
 if(p->avg_bw){
  if(fail())return -EIO;
  event(E_ICC_SET);
  return icc_set_error;
 }
 if(fail())return -EIO;
 event(E_ICC_UNSET);
 return icc_unset_error;}
static int dev_pm_opp_set_rate(struct device *d,unsigned long rate){assert(d==&dev);
 if(rate==ULONG_MAX){
  if(fail())return -EINVAL;
  event(E_OPP_FREQ);
  return opp_vote_error;
 }
 if(rate){
  if(fail())return -EINVAL;
  event(E_OPP_FREQ);
  return opp_freq_error;
 }
 if(fail())return -EINVAL;
 event(E_OPP_ZERO);
 return opp_zero_error;}
static int pm_runtime_get_sync(struct device *d){int i=d==&pd_devs[1];
 get_calls[i]++;d->power.usage_count++;event(i?E_DOM_HW:E_DOM_CTRL);return get_error[i];}
static void pm_runtime_put_noidle(struct device *d){int i=d==&pd_devs[1];put_noidle_calls[i]++;assert(d->power.usage_count>0);d->power.usage_count--;}
static int pm_runtime_put_sync(struct device *d){int i=d==&pd_devs[1];
 put_calls[i]++;assert(d->power.usage_count>0);d->power.usage_count--;
 event(i?E_DOM_HW_OFF:E_DOM_CTRL_OFF);return put_error[i];}
static int dev_pm_genpd_set_hwmode(struct device *d,bool on){assert(d==&pd_devs[1]);
 if(on){hwmode_on_calls++;if(fail())return -EIO;event(E_HWMODE_ON);return hwmode_error;}
 hwmode_off_calls++;event(E_HWMODE_OFF);return hwmode_error;}
static int reset_control_bulk_reset(int n,void *r){(void)r;assert(n==1);if(fail())return -EIO;event(E_RST);return reset_error;}
static int iris_prepare_enable_clock(struct iris_core *c,int type){assert(c==&core);
 clk_en_calls[type]++;if(fail())return -EIO;
 if(type==IRIS_AXI_CLK)event(E_AXI);else if(type==IRIS_CTRL_CLK)event(E_CTRL_CLK);else event(E_HW_CLK);
 return clk_enable_error[type];}
static int iris_disable_unprepare_clock(struct iris_core *c,int type){assert(c==&core);
 clk_dis_calls[type]++;if(fail())return -EIO;
 if(type==IRIS_CTRL_CLK)event(E_CTRL_OFF);else if(type==IRIS_AXI_CLK)event(E_AXI_OFF);else event(E_HW_CLK_OFF);
 return clk_disable_error[type];}
static void iris_vpu_interrupt_init(struct iris_core *c){assert(c==&core);event(E_INTR);}
static void iris_vpu_power_off(struct iris_core *c){(void)c;legacy_off_calls++;}
static int queue_init_error,fw_load_error,fw_touched_after_load,boot_error,core_init_error,wait_error,pc_error,hw_state_error,powercollapse_error;
static int iris_vpu_power_on(struct iris_core *c){(void)c;legacy_on_calls++;return legacy_on_error;}
static int iris_hfi_queues_init(struct iris_core *c){(void)c;queue_init_calls++;return queue_init_error;}
static int iris_fw_load(struct iris_core *c){c->firmware_touched=fw_touched_after_load;return fw_load_error;}
static int iris_vpu_boot_firmware(struct iris_core *c){(void)c;return boot_error;}
static int iris_hfi_core_init(struct iris_core *c){(void)c;return core_init_error;}
static int iris_wait_for_system_response(struct iris_core *c){(void)c;return wait_error;}
static void reinit_completion(struct completion *c){c->done=0;}
static int iris_vpu_prepare_pc(struct iris_core *c){(void)c;return pc_error;}
static int iris_set_hw_state(struct iris_core *c,bool resume){(void)c;(void)resume;return hw_state_error;}
static void pm_runtime_mark_last_busy(struct device *d){(void)d;}
static int powercollapse_stub(struct iris_core *c){(void)c;return powercollapse_error;}
static void enable_irq(int irq){assert(irq==9);irq_enable_calls++;event(E_IRQ_ON);}
static void disable_irq_nosync(int irq){(void)irq;disable_calls++;}
static void synchronize_irq(int irq){(void)irq;}
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return interrupt_context;}
static bool cancel_delayed_work_sync(struct delayed_work *w){(void)w;return false;}
static int pm_runtime_resume_and_get(struct device *d){d->power.usage_count++;resume_calls++;return 0;}
static int iris_fw_unload(struct iris_core *c){(void)c;fw_unload_calls++;return 0;}
static void iris_hfi_queues_deinit(struct iris_core *c){(void)c;queue_free_calls++;}
bool iris_power_progress_pending(struct iris_core *core);
bool iris_vpu_has_checked_power(struct iris_core *core);
int iris_vpu_power_on_checked(struct iris_core *core);
int iris_vpu_power_off_checked(struct iris_core *core);
int iris_vpu2_power_on_checked(struct iris_core *core);
int iris_vpu2_power_off_checked(struct iris_core *core);
int iris_set_icc_bw(struct iris_core *core, unsigned long icc_bw);
int iris_unset_icc_bw(struct iris_core *core);
int iris_enable_power_domains(struct iris_core *core, struct device *pd_dev);
int iris_disable_power_domains(struct iris_core *core, struct device *pd_dev);
static const struct iris_hfi_command_ops hfi_ops_stub={.sys_interframe_powercollapse=powercollapse_stub};
int iris_core_init(struct iris_core *core);
int iris_hfi_pm_suspend(struct iris_core *core);
int iris_hfi_pm_resume(struct iris_core *core);
'''

MAIN = r'''
static void preset_stub(struct iris_core *c){(void)c;event(E_PRESET);}
static void setup(void){memset(&core,0,sizeof(core));memset(&dev,0,sizeof(dev));
 memset(pd_devs,0,sizeof(pd_devs));
 assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));
 assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));assert(!pthread_mutex_init(&core.remove_lock.lock,NULL));
 INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);
 core.dev=&dev;core.reg_base=(void *)0x1000;core.irq=9;core.state=IRIS_CORE_DEINIT;
 core.icc_tbl=icc_tbl;core.icc_count=1;core.pmdomain_tbl=&pd_list;icc_tbl[0].avg_bw=0;icc_tbl[0].peak_bw=0;
 platform.vpu_ops=&platform_ops;platform.icc_tbl=icc_info_tbl;platform.clk_rst_tbl_size=1;platform.pmdomain_tbl=&pd_list;
 platform.set_preset_registers=preset_stub;platform.no_tz_fifo_reset=false;
 platform_ops.power_on_checked=iris_vpu2_power_on_checked;
 platform_ops.power_off_checked=iris_vpu2_power_off_checked;
 core.iris_platform_data=&platform;core.hardware_owner_held=true;core.hfi_ops=&hfi_ops_stub;}
static void pending_none(void){assert(!iris_power_progress_pending(&core));}
static void quiesce_ready(void){core.state=IRIS_CORE_INIT;core.firmware_shutdown_verified=false;
 core.irq_drain_verified=core.recovery_drain_verified=false;
 core.firmware_may_run=false;core.queues_allocated=true;core.power_prepared=true;core.shutdown_pm_held=false;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode<=12){
  if(mode==3)icc_set_error=-EIO;
  if(mode==4)get_error[0]=-EACCES;
  if(mode==5)reset_error=-EINVAL;
  if(mode==6)clk_enable_error[IRIS_AXI_CLK]=-ENODEV;
  if(mode==7)clk_enable_error[IRIS_CTRL_CLK]=-EBUSY;
  if(mode==8)get_error[1]=-EACCES;
  if(mode==9)clk_enable_error[IRIS_HW_CLK]=-ENODEV;
  if(mode==10)hwmode_error=-EIO;
  if(mode==11)opp_freq_error=-ERANGE;
  if(mode==12){fail_step=8;}
  int r=iris_vpu_power_on_checked(&core);
  int expected=mode==3?-EIO:mode==4?-EACCES:mode==5?-EINVAL:mode==6?-ENODEV:mode==7?-EBUSY:mode==8?-EACCES:mode==9?-ENODEV:mode==10?-EIO:mode==11?-ERANGE:mode==12?-EIO:0;
  assert(r==expected);
  if(!r){
   int want[]={E_ICC_SET,E_OPP_FREQ,E_DOM_CTRL,E_RST,E_AXI,E_CTRL_CLK,E_OPP_FREQ,E_DOM_HW,E_HW_CLK,E_HWMODE_ON,E_OPP_FREQ,E_PRESET,E_INTR,E_IRQ_ON};
   if(event_count!=(int)(sizeof(want)/sizeof(want[0]))){
    fprintf(stderr,"event_count=%d:",event_count);
    for(int i=0;i<event_count;i++)fprintf(stderr," %d",events[i]);
    fprintf(stderr,"\n");}
   assert(event_count==(int)(sizeof(want)/sizeof(want[0])));assert(!memcmp(events,want,sizeof(want)));
   assert(core.power_progress.icc_voted&&core.power_progress.ctrl_domain_ref&&core.power_progress.hw_domain_ref);
   assert(core.power_progress.hwmode_set&&core.power_progress.axi_clk_on&&core.power_progress.ctrl_clk_on&&core.power_progress.hw_clk_on);
   assert(iris_vpu2_power_on_checked(&core)==-EBUSY);
   if(mode<=2){
    event_count=0;tz_seen=false;
    if(mode==2)platform.no_tz_fifo_reset=true;
    assert(!iris_vpu_power_off_checked(&core));
    int wantoff[]={E_OPP_ZERO,E_HWMODE_OFF,E_OPP_ZERO,E_DOM_HW_OFF,E_HW_CLK_OFF,E_AON,E_NOC,E_DBG,E_TZ,E_CTRL_OFF,E_AXI_OFF,E_OPP_ZERO,E_DOM_CTRL_OFF,E_ICC_UNSET};
    int wantnotz[]={E_OPP_ZERO,E_HWMODE_OFF,E_OPP_ZERO,E_DOM_HW_OFF,E_HW_CLK_OFF,E_AON,E_NOC,E_DBG,E_CTRL_OFF,E_AXI_OFF,E_OPP_ZERO,E_DOM_CTRL_OFF,E_ICC_UNSET};
    if(mode==2){
     if(event_count!=(int)(sizeof(wantnotz)/sizeof(wantnotz[0]))){
      fprintf(stderr,"notz_count=%d:",event_count);
      for(int i=0;i<event_count;i++)fprintf(stderr," %d",events[i]);
      fprintf(stderr,"\n");}
     assert(event_count==(int)(sizeof(wantnotz)/sizeof(wantnotz[0])));
     assert(!memcmp(events,wantnotz,sizeof(wantnotz)));
    } else {
     if(event_count!=(int)(sizeof(wantoff)/sizeof(wantoff[0]))){
      fprintf(stderr,"off_count=%d:",event_count);
      for(int i=0;i<event_count;i++)fprintf(stderr," %d",events[i]);
      fprintf(stderr,"\n");}
     assert(event_count==(int)(sizeof(wantoff)/sizeof(wantoff[0])));
     assert(!memcmp(events,wantoff,sizeof(wantoff)));
    }
    pending_none();assert(!core.power_state_unknown);assert(core.power.icc_bw==0);
    assert(pd_devs[0].power.usage_count==0&&pd_devs[1].power.usage_count==0);
    assert(!iris_vpu_power_off_checked(&core));
    return 0;
   }
  }
  /* Every failure unwinds exactly the recorded progress and enables no IRQ. */
  assert(irq_enable_calls==0&&!core.power_state_unknown);
  assert(!iris_power_progress_pending(&core));assert(core.power.icc_bw==0);
  assert(pd_devs[0].power.usage_count==0&&pd_devs[1].power.usage_count==0);
  if(mode==3)assert(get_calls[0]==0&&icc_tbl[0].avg_bw==0);
  if(mode==4)assert(get_calls[0]==1&&put_noidle_calls[0]==1&&clk_en_calls[IRIS_AXI_CLK]==0);
  if(mode==5)assert(put_calls[0]==1&&clk_en_calls[IRIS_AXI_CLK]==0);
  if(mode==6)assert(clk_en_calls[IRIS_AXI_CLK]==1&&clk_en_calls[IRIS_CTRL_CLK]==0);
  if(mode==7)assert(clk_dis_calls[IRIS_AXI_CLK]==1&&put_calls[0]==1);
  if(mode==8)assert(put_calls[1]==0&&put_noidle_calls[1]==1&&clk_en_calls[IRIS_HW_CLK]==0);
  if(mode==9)assert(clk_dis_calls[IRIS_HW_CLK]==0&&put_calls[1]==1);
  if(mode==10)assert(hwmode_on_calls==1&&hwmode_off_calls==0&&put_calls[1]==1);
  if(mode==11)assert(hwmode_off_calls==1&&put_calls[1]==1&&put_calls[0]==1);
  if(mode==12){
   fail_step=0;irq_enable_calls=0;event_count=0;
   assert(!iris_vpu_power_on_checked(&core));assert(irq_enable_calls==1);
   event_count=0;tz_seen=false;assert(!iris_vpu_power_off_checked(&core));pending_none();
  }
  return 0;
 }
 if(mode==13){assert(!iris_vpu_power_off_checked(&core));assert(!event_count);return 0;}
 if(mode==14){core.power_state_unknown=true;assert(iris_vpu_power_off_checked(&core)==-EUCLEAN);return 0;}
 if(mode>=15&&mode<=23){
  assert(!iris_vpu_power_on_checked(&core));event_count=0;
  if(mode==15)opp_zero_error=-EIO;
  if(mode==16)aon_val=0;
  if(mode==17)noc_val=0;
  if(mode==18)dbg_val=1;
  if(mode==19)clk_disable_error[IRIS_CTRL_CLK]=-EIO;
  if(mode==20)put_error[0]=-EIO;
  if(mode==21)put_error[1]=-EIO;
  if(mode==22)hwmode_error=-EIO;
  if(mode==23)icc_unset_error=-EIO;
  int r=iris_vpu_power_off_checked(&core);
  int expected=mode==15?-EIO:mode==16?-ETIMEDOUT:mode==17?-ETIMEDOUT:mode==18?-ETIMEDOUT:mode==19?-EIO:
   (mode==20||mode==21)?-EIO:mode==22?-EIO:mode==23?-EIO:0;
  assert(r==expected);
  if(mode==15){assert(core.power_progress.hwmode_set&&irq_enable_calls==1);return 0;}
  if(mode==16||mode==17||mode==18){assert(core.power_progress.ctrl_clk_on&&core.power_progress.axi_clk_on);assert(!core.power_progress.hw_domain_ref);return 0;}
  if(mode==19){assert(core.power_progress.ctrl_clk_on&&core.power_progress.ctrl_domain_ref);return 0;}
  if(mode==20){assert(core.power_state_unknown);assert(!core.power_progress.ctrl_domain_ref);assert(put_calls[0]==1);assert(pd_devs[0].power.usage_count==0);return 0;}
  if(mode==21){assert(core.power_state_unknown);assert(!core.power_progress.hw_domain_ref);assert(put_calls[1]==1);return 0;}
  if(mode==22){assert(core.power_progress.hwmode_set);return 0;}
  if(mode==23){assert(core.power_progress.icc_voted&&core.power.icc_bw!=0);icc_unset_error=0;assert(!iris_vpu_power_off_checked(&core));assert(!core.power_progress.icc_voted);return 0;}
 }
 if(mode>=24&&mode<=27){
  assert(!iris_vpu_power_on_checked(&core));quiesce_ready();
  if(mode==24)opp_zero_error=-EIO;
  if(mode==27){platform_ops.power_on_checked=NULL;platform_ops.power_off_checked=NULL;}
  int r=iris_core_quiesce(&core);
  if(mode==24){
   assert(r==-EIO);assert(core.power_prepared);assert(!queue_free_calls);
   assert(core.state!=IRIS_CORE_DEINIT);assert(core.shutdown_pm_held);
   assert(core.terminal_shutdown&&core.irq_drain_verified&&core.recovery_drain_verified);
   return 0;
  }
  if(mode==27){
   assert(!r);assert(legacy_off_calls==1);assert(!core.power_prepared);
   assert(queue_free_calls==1);assert(core.state==IRIS_CORE_DEINIT);return 0;
  }
  assert(!r);assert(!core.power_prepared&&core.state==IRIS_CORE_DEINIT);pending_none();
  assert(queue_free_calls==1);
  if(mode==26){int resumes=resume_calls;assert(!iris_core_quiesce(&core));assert(resume_calls==resumes);}
  return 0;
 }
 if(mode==28){assert(iris_vpu_has_checked_power(&core));
  platform_ops.power_on_checked=NULL;platform_ops.power_off_checked=NULL;
  assert(!iris_vpu_has_checked_power(&core));assert(iris_vpu_power_off_checked(&core)==-EOPNOTSUPP);
  assert(iris_vpu_power_on_checked(&core)==-EOPNOTSUPP);return 0;}
 if(mode==29){
  assert(!iris_vpu_power_on_checked(&core));
  core.power_prepared=true;
  core.removing=core.remove_prepared=true;
  assert(!iris_core_quiesce(&core));
  int g=iris_resource_release_gate(&core);
  fprintf(stderr,"gate=%d rem=%d prep=%d term=%d st=%u fw=%d irq=%d rec=%d own=%u/%u/%u q=%d pp=%d pmh=%d use=%d unk=%d pend=%d\n",
   g,core.removing,core.remove_prepared,core.terminal_shutdown,core.state,core.firmware_shutdown_verified,
   core.irq_drain_verified,core.recovery_drain_verified,core.open_inflight,core.open_owners,core.orphan_owners,
   core.queues_allocated,core.power_prepared,core.shutdown_pm_held,core.dev->power.usage_count,
   core.power_state_unknown,iris_power_progress_pending(&core));
  assert(!g);
  core.power_progress.icc_voted=true;
  assert(iris_resource_release_gate(&core)==-EBUSY);
  core.power_progress.icc_voted=false;
  core.power_state_unknown=true;
  assert(iris_resource_release_gate(&core)==-EBUSY);
  return 0;}
 if(mode==30){
  assert(!iris_core_init(&core));
  assert(core.state==IRIS_CORE_INIT&&core.power_prepared&&core.queues_allocated);
  assert(irq_enable_calls==1&&core.power_irq_enabled);
  assert(iris_power_progress_pending(&core));
  assert(queue_init_calls==1);
  return 0;}
 if(mode==31){
  fw_load_error=-ENOENT;fw_touched_after_load=false;
  assert(iris_core_init(&core)==-ENOENT);
  assert(core.state==IRIS_CORE_DEINIT);
  assert(!core.power_prepared&&!core.queues_allocated);
  assert(!iris_power_progress_pending(&core));
  assert(irq_enable_calls==1&&!core.power_irq_enabled);
  assert(disable_calls==1&&queue_free_calls==1);
  return 0;}
 if(mode==32){
  fw_load_error=-ENOENT;clk_disable_error[IRIS_CTRL_CLK]=-EIO;
  assert(iris_core_init(&core)==-ENOENT);
  assert(core.state==IRIS_CORE_ERROR);
  assert(core.power_prepared&&core.queues_allocated);
  assert(iris_power_progress_pending(&core));
  assert(!queue_free_calls);
  return 0;}
 if(mode==33){
  core.power_progress.icc_voted=true;
  assert(iris_core_init(&core)==-EBUSY);
  assert(core.state==IRIS_CORE_ERROR&&core.queues_allocated);
  assert(!core.power_prepared);
  assert(irq_enable_calls==0);
  return 0;}
 if(mode==34||mode==35){
  assert(!iris_vpu_power_on_checked(&core));
  if(mode==35)core.quiesce_irq_mask_held=true;
  int before=disable_calls;
  assert(!iris_vpu_power_off_checked(&core));
  assert(!core.power_irq_enabled);
  assert(disable_calls==before+(mode==35?0:1));
  assert(!iris_vpu_power_off_checked(&core));
  assert(disable_calls==before+(mode==35?0:1));
  return 0;}
 if(mode==36){
  assert(!iris_vpu_power_on_checked(&core));
  core.power_prepared=true;
  assert(!iris_hfi_pm_suspend(&core));
  assert(!iris_power_progress_pending(&core)&&!core.power_irq_enabled);
  assert(disable_calls==1);
  return 0;}
 if(mode==37){
  assert(!iris_vpu_power_on_checked(&core));
  core.power_prepared=true;
  clk_disable_error[IRIS_CTRL_CLK]=-EACCES;
  assert(iris_hfi_pm_suspend(&core)==-EACCES);
  assert(core.power_error==-EACCES);
  assert(iris_power_progress_pending(&core));
  return 0;}
 if(mode==38){
  icc_set_error=-EACCES;
  assert(iris_hfi_pm_resume(&core)==-EACCES);
  assert(core.power_error==-EACCES);
  assert(!iris_power_progress_pending(&core));
  return 0;}
 if(mode==39){
  boot_error=-EPROTO;
  assert(iris_hfi_pm_resume(&core)==-EPROTO);
  assert(!iris_power_progress_pending(&core));
  assert(!core.power_irq_enabled&&disable_calls==1);
  return 0;}
 if(mode==40){
  boot_error=-EPROTO;clk_disable_error[IRIS_CTRL_CLK]=-EIO;
  assert(iris_hfi_pm_resume(&core)==-EPROTO);
  assert(core.power_state_unknown&&iris_power_progress_pending(&core));
  return 0;}
 return 2;}
'''


def source(work):
    vc = (work / 'iris_vpu_common.c').read_text()
    rc = (work / 'iris_resources.c').read_text()
    cc = (work / 'iris_core.c').read_text()
    pc = (work / 'iris_probe.c').read_text()
    hc = (work / 'iris_hfi_common.c').read_text()
    names_vc = ('iris_power_progress_pending', 'iris_vpu_has_checked_power', 'iris_vpu_power_on_checked',
                'iris_vpu_power_off_checked', 'iris_vpu2_power_off_hw_checked',
                'iris_vpu2_power_off_controller_checked', 'iris_vpu2_power_off_checked',
                'iris_vpu2_power_on_checked')
    functions = ''.join(EXTRACT.definition(vc, name) for name in names_vc)
    functions += ''.join(EXTRACT.definition(rc, name) for name in
                         ('iris_set_icc_bw', 'iris_unset_icc_bw', 'iris_enable_power_domains',
                          'iris_disable_power_domains'))
    functions += EXTRACT.definition(cc, 'iris_core_quiesce')
    if 'iris_core_init' in cc:
        functions += EXTRACT.definition(cc, 'iris_core_init')
    for name in ('iris_hfi_pm_suspend', 'iris_hfi_pm_resume'):
        if name in hc:
            functions += EXTRACT.definition(hc, name)
    functions += EXTRACT.definition(pc, 'iris_resource_release_gate')
    return PREFIX + functions + MAIN


def compile_fixture(text, output, sanitize=False):
    src = output / 'vpu_power.c'
    src.write_text(text)
    binary = output / 'vpu_power'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-Wno-sign-compare',
            '-Wno-absolute-value', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class PowerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-vpu-power-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-vpu-power-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_poll_failure_ignored(self):
        self.mutation('''		ret = readl_poll_timeout(core->reg_base + AON_WRAPPER_MVP_NOC_LPI_STATUS,
					 val, val & BIT(0), 200, 2000);
		if (ret)
			return ret;''',
                      '''		ret = readl_poll_timeout(core->reg_base + AON_WRAPPER_MVP_NOC_LPI_STATUS,
					 val, val & BIT(0), 200, 2000);
		if (ret)
			ret = 0;''', 16)

    def test_oracle_pm_reference_cleared_before_put(self):
        self.mutation('''		core->power_progress.hw_domain_ref = false;
		if (ret) {''',
                      '''		if (ret) {''', 21)

    def test_oracle_quiesce_ignores_power_error(self):
        self.mutation('''			ret = iris_vpu_power_off_checked(core);
			if (ret)
				goto release_lifecycle;''',
                      '''			ret = iris_vpu_power_off_checked(core);
			if (ret)
				ret = 0;''', 24)

    def test_oracle_irq_reference_not_released(self):
        self.mutation('''		if (!core->quiesce_irq_mask_held)
			disable_irq_nosync(core->irq);
		core->power_irq_enabled = false;''',
                      '''		core->power_irq_enabled = false;''', 31)

    def test_oracle_init_frees_queues_after_power_failure(self):
        self.mutation('''			core->state = IRIS_CORE_ERROR;
			if (!ret)
				ret = shutdown_ret;
			goto exit;''',
                      '''			core->state = IRIS_CORE_ERROR;
			if (!ret)
				ret = shutdown_ret;
			goto error_queue_deinit;''', 32)

    def test_oracle_resume_reports_busy(self):
        self.mutation('''		core->power_error = ret;
		/* Checked platforms report the real step error. */
		return ret;''',
                      '''		core->power_error = ret;
		/* Checked platforms report the real step error. */
		return -EBUSY;''', 38)

    def test_oracle_init_retains_only_unknown(self):
        self.mutation('if (core->power_state_unknown || iris_power_progress_pending(core)) {',
                      'if (core->power_state_unknown) {', 33)

    def test_oracle_icc_failure_keeps_budget(self):
        self.mutation('''			ret = icc_bulk_set_bw(core->icc_count, core->icc_tbl);
			if (ret) {''',
                      '''			ret = icc_bulk_set_bw(core->icc_count, core->icc_tbl);
			if (false) {''', 3)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(PowerTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-vpu-power-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(CASES):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
        print('%d checked VPU power ASan/UBSan scenarios passed; registers/clocks/domains/PM are host seams' % CASES)
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PowerTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
