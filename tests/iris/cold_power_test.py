#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual C host logic; test facades enable branches, release gates remain zero."""
from pathlib import Path
exec(compile((Path(__file__).resolve().parent/'support.py').read_text(),str(Path(__file__).resolve().parent/'support.py'),'exec'))
def fn(d,n,name):return extract(n,name)
def decl(n,name):
 t=source_text(n);s=t.index(name+' {');return t[s:t.index('\n};',s)+3]
h=r'''#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <string.h>
#include <setjmp.h>
typedef uint32_t u32;
#define ARRAY_SIZE(x) (sizeof(x)/sizeof(x[0]))
#define READ_ONCE(x) (x)
#define WRITE_ONCE(x,v) ((x)=(v))
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define IRIS_CTRL_POWER_DOMAIN 0
#define IRIS_HW_POWER_DOMAIN 1
#define IRIS_AXI_CLK 0
#define IRIS_CTRL_CLK 1
#define IRIS_HW_CLK 2
#define IRIS_INIT_ONLY_EXECUTION_ENABLED 1
#define IRIS_INIT_ONLY_PROVIDER_ADMISSION_ENABLED 1
#define IRIS_DECODE_ONCE_ENABLED 1
#define ECANCELED 125
#define THIS_MODULE 0
struct mutex {bool held;};static jmp_buf jump;static int nested_lock;
static void mutex_lock(struct mutex*m){if(m->held){nested_lock++;longjmp(jump,1);}m->held=true;}
static void mutex_unlock(struct mutex*m){assert(m->held);m->held=false;}
struct list_head {struct list_head*next,*prev;};static bool list_empty(struct list_head*h){return h->next==h;}
#define list_first_entry(h,t,m) ((t *)((char*)(h)->next - offsetof(t,m)))
struct completion {int done;};static void reinit_completion(struct completion*c){c->done=0;}
struct iris_hfi_pending {bool active;};static void iris_hfi_pending_cancel(struct iris_hfi_pending*p){p->active=false;}
struct device {int value;};struct iris_core;struct iris_inst;
struct vpu_ops {int(*power_on_checked)(struct iris_core*);int(*power_off_checked)(struct iris_core*);int(*power_on_controller)(struct iris_core*);int(*power_on_hw)(struct iris_core*);void(*power_off_controller)(struct iris_core*);};
struct iris_platform_data {const struct vpu_ops*vpu_ops;u32 clk_rst_tbl_size;void(*set_preset_registers)(struct iris_core*);};
struct power_domains {struct device*pd_devs[2];};
'''
h+='struct iris_pm_get_progress {int unused;};\n'+decl('iris_core.h','struct iris_power_progress')+'\n'+decl('iris_core.h','enum iris_init_only_stage')+'\n'
h+=r'''
struct iris_core {struct mutex lock,lifecycle_lock;bool native_repeat,decode_once,init_only,init_only_attempted,init_only_module_held,init_only_remove_blocked,init_only_prepared,init_only_post_bind,init_only_run_claimed,init_only_provider_attempted,init_only_remove_seen,removing,terminal_shutdown,irq_quiescing,resources_releasing,irq_releasing,hardware_owner_held,irq_registered,irq_managed,runtime_pm_enabled,pm_guard_held,encoder_registered,decoder_registered,v4l2_registered,decode_once_enabled,decode_once_retention_ready,decode_once_admission,decode_once_claimed,queues_allocated,power_prepared,firmware_touched,firmware_may_run,power_state_unknown,power_irq_enabled,irq_started,gen2_ack_supported,core_ready,init_acknowledged,firmware_booted,firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified,swmode_request_held;u32 state,video_owners,open_inflight,open_owners,orphan_owners,irq,intr_status;void*vdev_enc,*vdev_dec,*resets;struct device*dev;struct list_head instances,orphan_instances;struct iris_platform_data*iris_platform_data;struct power_domains*pmdomain_tbl;struct iris_power_progress power_progress;struct {u32 clk_freq;} power;struct iris_hfi_pending init_pending;struct completion core_init_done;enum iris_init_only_stage init_only_stage;int init_only_status[IRIS_INIT_ONLY_STAGE_COUNT],init_only_error,power_error,power_cleanup_error,power_opp_error,power_domain_put_error;};
struct iris_inst {struct list_head list;struct iris_core*core;bool response_closing,orphaned;u32 response_refs;};
static struct iris_platform_data sm8450_data;static struct iris_inst inst;
static int pm_calls,oppdomains_calls,icc_calls,domains_calls,reset_calls,clock_calls,swmode_calls,opp_calls,preset_calls,interrupt_calls,fw_calls,boot_calls,irq_calls,sysinit_calls,provider_fail,scaled,ref_puts;
static char trace[128];static void tr(char c){size_t n=strlen(trace);trace[n]=c;trace[n+1]=0;}
static int pm_runtime_active(struct device*d){(void)d;return 1;}
static bool iris_power_progress_pending(struct iris_core*c){(void)c;return false;}
static int iris_init_only_pm_ready(struct iris_core*c){assert(c->lock.held&&c->lifecycle_lock.held);pm_calls++;tr('P');return provider_fail==1?-EIO:0;}
static int iris_init_only_activate_opp_domains(struct iris_core*c){assert(c->lock.held);oppdomains_calls++;tr('O');return provider_fail==2?-EIO:0;}
static int iris_set_icc_bw(struct iris_core*c,int bw){assert(c->lock.held&&bw==INT_MAX);icc_calls++;tr('I');return provider_fail==3?-EIO:0;}
static int iris_enable_power_domains(struct iris_core*c,struct device*d){assert(c->lock.held&&d);domains_calls++;tr('D');return 0;}
static int reset_control_bulk_reset(u32 count,void*p){(void)p;assert(count==2);reset_calls++;tr('R');return 0;}
static int iris_prepare_enable_clock(struct iris_core*c,int n){assert(c->lock.held);assert(n>=0&&n<=2);clock_calls++;tr('C');return 0;}
static int dev_pm_genpd_set_hwmode(struct device*d,bool mode){assert(d&&!mode);swmode_calls++;tr('S');return 0;}
static int dev_pm_opp_set_rate(struct device*d,u32 freq){assert(d&&freq);opp_calls++;tr('F');return provider_fail==4?-EIO:0;}
static void preset(struct iris_core*c){assert(c->lock.held&&c->swmode_request_held);preset_calls++;tr('T');}
static void iris_vpu_interrupt_init(struct iris_core*c){assert(c->lock.held);interrupt_calls++;tr('V');}
#define dev_info(...) ((void)0)
#define dev_err(...) ((void)0)
static int iris_vpu2_power_off_checked(struct iris_core*c){(void)c;assert(0&&"isolated failures must retain");return -EIO;}
static int iris_hfi_queues_init(struct iris_core*c){assert(c->lock.held);tr('Q');return 0;}
static int iris_init_only_pm_active(struct iris_core*c){assert(c->lock.held);tr('A');return 0;}
static int iris_fw_load(struct iris_core*c){assert(c->lock.held&&c->power_prepared);fw_calls++;tr('L');return 0;}
static int iris_vpu_boot_firmware(struct iris_core*c){assert(c->lock.held);boot_calls++;tr('B');return 0;}
static int iris_irq_activate(struct iris_core*c){assert(c->lock.held);irq_calls++;tr('E');return 0;}
static int iris_hfi_core_init(struct iris_core*c){assert(c->lock.held&&c->firmware_booted&&c->power_prepared);sysinit_calls++;tr('H');return 0;}
static int iris_wait_for_system_response(struct iris_core*c){assert(!c->lock.held&&c->lifecycle_lock.held);c->init_acknowledged=true;tr('W');return 0;}
static int iris_scale_power(struct iris_inst*i){assert(!i->core->lock.held);mutex_lock(&i->core->lock);scaled++;int ret=i->core->init_acknowledged&&i->core->power_prepared&&i->core->swmode_request_held?0:-ESHUTDOWN;mutex_unlock(&i->core->lock);return ret;}
static void refcount_inc(u32*r){(*r)++;}
static void iris_put_response_instance(struct iris_inst*i){assert(i->response_refs>1);i->response_refs--;ref_puts++;}
static void iris_unset_icc_bw(struct iris_core*c){(void)c;assert(0);}
static void enable_irq(u32 irq){(void)irq;assert(0&&"ordinary native power must not re-enable irq");}
static int iris_vpu_power_on_checked(struct iris_core*);
'''
logic='\n'.join(fn(d,n,s)for n,s in [('iris_vpu_common.c','iris_vpu_has_checked_power'),('iris_vpu_common.c','iris_repeat_power_held'),('iris_vpu_common.c','iris_vpu2_power_on_checked'),('iris_vpu_common.c','iris_vpu_power_on_checked'),('iris_vpu_common.c','iris_vpu_power_on'),('iris_core.c','iris_core_init_only')])
# Historical one-function red mutant; no source edits.
bad='int bad_iris_vpu_power_on_checked(struct iris_core *core)\n{\n\tconst struct vpu_ops *ops = core->iris_platform_data->vpu_ops;\n\n\tif (core->native_repeat)\n\t\treturn iris_repeat_power_held(core);\n\n\tif (!ops->power_on_checked)\n\t\treturn -EOPNOTSUPP;\n\n\treturn ops->power_on_checked(core);\n}'
# Run actual core cold caller twice, swapping just checked entry body for red path.
badcaller=fn(d,'iris_core.c','iris_core_init_only').replace('iris_core_init_only','bad_iris_core_init_only',1).replace('iris_vpu_power_on_checked(core)','bad_iris_vpu_power_on_checked(core)')
tests=r'''
static const struct vpu_ops ops={.power_on_checked=iris_vpu2_power_on_checked,.power_off_checked=iris_vpu2_power_off_checked};static struct iris_core core;static struct device device,ctrl,hw;static struct power_domains pd={.pd_devs={&ctrl,&hw}};
static void setup(void){memset(&core,0,sizeof(core));memset(&inst,0,sizeof(inst));memset(trace,0,sizeof(trace));pm_calls=oppdomains_calls=icc_calls=domains_calls=reset_calls=clock_calls=swmode_calls=opp_calls=preset_calls=interrupt_calls=fw_calls=boot_calls=irq_calls=sysinit_calls=provider_fail=scaled=ref_puts=nested_lock=0;sm8450_data=(struct iris_platform_data){.vpu_ops=&ops,.clk_rst_tbl_size=2,.set_preset_registers=preset};core.iris_platform_data=&sm8450_data;core.pmdomain_tbl=&pd;core.dev=&device;core.native_repeat=core.decode_once=core.init_only=core.init_only_module_held=core.init_only_remove_blocked=core.init_only_prepared=core.init_only_post_bind=core.init_only_run_claimed=core.init_only_provider_attempted=core.removing=core.hardware_owner_held=core.irq_registered=core.irq_managed=core.runtime_pm_enabled=core.pm_guard_held=core.decode_once_enabled=core.decode_once_retention_ready=core.decoder_registered=core.v4l2_registered=core.gen2_ack_supported=true;core.video_owners=1;core.vdev_dec=(void*)1;core.state=IRIS_CORE_DEINIT;core.instances.next=core.instances.prev=&core.instances;core.orphan_instances.next=core.orphan_instances.prev=&core.orphan_instances;}
int main(void){setup();if(setjmp(jump)==0){bad_iris_core_init_only(&core);assert(0&&"old code must attempt core mutex re-entry");}assert(nested_lock==1&&core.lock.held&&core.lifecycle_lock.held&&core.init_only_stage==IRIS_INIT_ONLY_POWER&&sysinit_calls==0&&pm_calls==0&&list_empty(&core.instances));
 setup();assert(!iris_core_init_only(&core));assert(!nested_lock&&!core.lock.held&&!core.lifecycle_lock.held&&core.init_acknowledged&&core.power_prepared&&core.swmode_request_held);assert(pm_calls==1&&oppdomains_calls==1&&icc_calls==1&&domains_calls==2&&reset_calls==1&&clock_calls==3&&swmode_calls==1&&opp_calls==1&&preset_calls==1&&interrupt_calls==1&&fw_calls==1&&boot_calls==1&&irq_calls==1&&sysinit_calls==1);assert(!strcmp(trace,"QPOIDRCCDC SFTVALBEHW")==0);int hardware=domains_calls+clock_calls+reset_calls+opp_calls;assert(iris_core_init_only(&core)==-EALREADY);assert(domains_calls+clock_calls+reset_calls+opp_calls==hardware);core.open_owners=1;inst.core=&core;inst.response_refs=1;inst.list.next=inst.list.prev=&core.instances;core.instances.next=core.instances.prev=&inst.list;assert(!iris_vpu_power_on(&core));assert(scaled==1&&ref_puts==1&&inst.response_refs==1&&hardware==domains_calls+clock_calls+reset_calls+opp_calls);inst.orphaned=true;assert(iris_vpu_power_on(&core)==-ESHUTDOWN);assert(scaled==1&&hardware==domains_calls+clock_calls+reset_calls+opp_calls);
 for(int fail=1;fail<=4;fail++){setup();provider_fail=fail;assert(iris_core_init_only(&core)==-EIO);assert(!core.lock.held&&!core.lifecycle_lock.held&&!sysinit_calls&&!fw_calls&&core.state==IRIS_CORE_ERROR);assert(core.init_only_error==-EIO);assert(iris_core_init_only(&core)==-EALREADY);}
 setup();core.state=IRIS_CORE_ERROR;assert(iris_core_init_only(&core)<0&&!pm_calls&&!sysinit_calls);setup();core.init_only_prepared=false;assert(iris_core_init_only(&core)<0&&!pm_calls&&!sysinit_calls);
 puts("exact cold core->checked full provider path: old red mutex re-entry caught; new cold empty green PM/OPP/ICC/domains/swmode/preset/SYSINIT; retained ordinary session no hardware reinit; failures retained");return 0;}
'''
tests=tests.replace('assert(!strcmp(trace,"QPOIDRCCDC SFTVALBEHW")==0);','assert(!strcmp(trace,"QPOIDRCCDCSFTVALBEHW"));')
run_host('power',h+logic+'\n'+bad+'\n'+badcaller+'\n'+tests,['-Wno-unused-function'])
