#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual one-way quiesce/IRQ/worker functions with deterministic pthread seams.

synchronize_irq/cancel work are host models, not hardware observations. Tests
verify call ordering, lock availability and retry ownership; no device is used.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('actual_extract', HERE / 'test-decode-cleanup.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None

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
#include <stdatomic.h>
typedef uint32_t u32;
typedef int irqreturn_t;
#define IRQ_NONE 0
#define IRQ_WAKE_THREAD 1
#define IRQ_HANDLED 2
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define READ_ONCE(x) (x)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define dev_err(dev,...) ((void)(dev))
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
struct completion {int done;};
static void complete(struct completion *c){c->done++;}
struct iris_hfi_pending {bool active;};
static void iris_hfi_pending_cancel(struct iris_hfi_pending *p){p->active=false;}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
#define list_for_each_entry(p,h,m) for(struct list_head *_it=(h)->next;_it!=(h)&&((p)=container_of(_it,__typeof__(*(p)),m),1);_it=_it->next)
static bool list_empty(struct list_head *h){return h->next==h;}
static void list_add_tail(struct list_head *n,struct list_head *h){n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;}
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
typedef struct {int value;} refcount_t;
static int refcount_read(refcount_t *r){return r->value;}
struct iris_inst {struct list_head list;struct iris_core *core;
 bool response_closing,file_detached,orphaned;refcount_t response_refs;};
struct iris_platform_data {u32 max_session_count;};
struct iris_core;
struct iris_hfi_response_ops {void (*hfi_response_handler)(struct iris_core *);};
struct iris_core {struct mutex lock,lifecycle_lock,quiesce_lock;struct list_head instances,orphan_instances;
 int irq;u32 state,intr_status;void *dev;const struct iris_hfi_response_ops *hfi_response_ops;
 struct iris_platform_data *iris_platform_data;struct delayed_work sys_error_handler;
 struct completion core_init_done;struct iris_hfi_pending init_pending;
 bool terminal_shutdown,irq_quiescing,recovery_blocked,quiesce_irq_mask_held,shutdown_pm_held;
 bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;};
static struct iris_core core;
static struct iris_platform_data platform={16};
static int pm_error,shutdown_error,pm_usage,pm_gets,pm_puts,mask_calls,enable_calls,shutdown_calls,power_calls,queue_calls,sync_calls,cancel_calls,clear_calls,response_calls;
static bool self_work,interrupt_context,add_on_resume,cancel_return;
static struct iris_inst active;
static int events[64],event_count;
enum {PM=1,MASK,SYNC,CANCEL,SHUTDOWN,POWER,QUEUES,PUT};
static void event(int e){assert(event_count<64);events[event_count++]=e;}
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return interrupt_context;}
static int pm_runtime_resume_and_get(void *d){assert(d==&core);pm_gets++;event(PM);if(pm_error)return pm_error;pm_usage++;
 if(add_on_resume){list_add_tail(&active.list,&core.instances);}return 1;}
static void pm_runtime_put_noidle(void *d){assert(d==&core&&pm_usage==1);pm_usage--;pm_puts++;event(PUT);}
static void disable_irq_nosync(int irq){assert(irq==9);mask_calls++;event(MASK);}
static void enable_irq(int irq){assert(irq==9);enable_calls++;}
static void check_callback_locks_free(void){
 assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));
 assert(!pthread_mutex_trylock(&core.lifecycle_lock.lock));assert(!pthread_mutex_unlock(&core.lifecycle_lock.lock));
}
static pthread_t irq_thread,worker_thread;
static bool irq_join,worker_join;
static pthread_mutex_t block_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t block_cond=PTHREAD_COND_INITIALIZER;
static bool response_entered,response_release;
static void synchronize_irq(int irq){assert(irq==9);check_callback_locks_free();sync_calls++;event(SYNC);
 if(irq_join){assert(!pthread_mutex_lock(&block_mutex));response_release=true;assert(!pthread_cond_broadcast(&block_cond));assert(!pthread_mutex_unlock(&block_mutex));assert(!pthread_join(irq_thread,NULL));irq_join=false;}}
static bool cancel_delayed_work_sync(struct delayed_work *work){assert(work==&core.sys_error_handler);check_callback_locks_free();cancel_calls++;event(CANCEL);
 if(worker_join){assert(!pthread_join(worker_thread,NULL));worker_join=false;}return cancel_return;}
static int iris_fw_unload(struct iris_core *c){assert(c==&core);assert(c->irq_drain_verified&&c->recovery_drain_verified);shutdown_calls++;event(SHUTDOWN);return shutdown_error;}
static void iris_vpu_power_off(struct iris_core *c){assert(c->firmware_shutdown_verified&&sync_calls&&cancel_calls);power_calls++;event(POWER);}
static void iris_hfi_queues_deinit(struct iris_core *c){assert(c->firmware_shutdown_verified&&power_calls);queue_calls++;event(QUEUES);}
static void pm_runtime_mark_last_busy(void *d){assert(d==&core);}
static void iris_vpu_clear_interrupt(struct iris_core *c){assert(c==&core);clear_calls++;}
static bool iris_vpu_watchdog(struct iris_core *c,u32 status){assert(c==&core);return status!=0;}
static void respond(struct iris_core *c){assert(c==&core);response_calls++;
 assert(!pthread_mutex_lock(&block_mutex));response_entered=true;assert(!pthread_cond_broadcast(&block_cond));while(!response_release)assert(!pthread_cond_wait(&block_cond,&block_mutex));assert(!pthread_mutex_unlock(&block_mutex));}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);core.irq=9;core.dev=&core;core.state=IRIS_CORE_INIT;core.init_pending.active=true;core.iris_platform_data=&platform;
 static const struct iris_hfi_response_ops ops={respond};core.hfi_response_ops=&ops;}
static void *run_irq(void *unused){(void)unused;assert(iris_hfi_isr_handler(9,&core)==IRQ_HANDLED);return NULL;}
static void *run_worker(void *unused){(void)unused;iris_sys_error_handler(&core.sys_error_handler.work);return NULL;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==1)pm_error=-EIO;
 if(mode==2||mode==3)shutdown_error=-EACCES;
 if(mode==4)self_work=true;
 if(mode==5)interrupt_context=true;
 if(mode==6)list_add_tail(&active.list,&core.instances);
 if(mode==7)add_on_resume=true;
 if(mode==8){core.state=IRIS_CORE_DEINIT;core.firmware_shutdown_verified=core.irq_drain_verified=core.recovery_drain_verified=true;core.terminal_shutdown=true;}
 if(mode==9||mode==10){irq_join=true;assert(!pthread_create(&irq_thread,NULL,run_irq,NULL));assert(!pthread_mutex_lock(&block_mutex));while(!response_entered)assert(!pthread_cond_wait(&block_cond,&block_mutex));assert(!pthread_mutex_unlock(&block_mutex));}
 if(mode==10||mode==11){core.recovery_blocked=true;worker_join=true;assert(!pthread_create(&worker_thread,NULL,run_worker,NULL));}
 if(mode==12)cancel_return=true;
 if(mode==13||mode==14){core.irq_quiescing=true;core.terminal_shutdown=mode==14;assert(iris_hfi_isr_handler(9,&core)==IRQ_HANDLED);assert(!clear_calls&&!response_calls&&!enable_calls);return 0;}
 if(mode==15||mode==16){core.terminal_shutdown=mode==15;core.state=mode==16?IRIS_CORE_ERROR:IRIS_CORE_INIT;struct iris_inst fresh={.core=&core};assert(iris_add_session(&fresh)==-ESHUTDOWN);assert(list_empty(&core.instances));return 0;}
 if(mode==17){response_release=true;assert(iris_hfi_isr_handler(9,&core)==IRQ_HANDLED);assert(clear_calls==1&&response_calls==1&&enable_calls==1);return 0;}
 if(mode==18){core.intr_status=1;response_release=true;assert(iris_hfi_isr_handler(9,&core)==IRQ_HANDLED);assert(!enable_calls);return 0;}
 if(mode==19){iris_sys_error_handler(&core.sys_error_handler.work);assert(core.state==IRIS_CORE_ERROR&&core.core_init_done.done==1&&!core.init_pending.active);return 0;}
 if(mode==20||mode==21){
  active.response_closing=true;active.file_detached=true;active.orphaned=true;active.response_refs.value=mode==21?1:0;
  list_add_tail(&active.list,&core.orphan_instances);
  assert(iris_orphan_reclaim_gate(&core)==-EAGAIN);
 }
 int ret=iris_core_quiesce(&core);
 int expected=mode==1?-EIO:(mode==2||mode==3)?-EACCES:mode==4?-EDEADLK:mode==5?-EWOULDBLOCK:(mode==6||mode==7)?-EBUSY:0;assert(ret==expected);
 if(mode==1||mode==4||mode==5||mode==6){assert(!core.terminal_shutdown&&!mask_calls&&!sync_calls&&!shutdown_calls&&pm_usage==0);return 0;}
 if(mode==7){assert(!core.terminal_shutdown&&!mask_calls&&pm_usage==0&&pm_puts==1);return 0;}
 if(mode==8){assert(pm_gets==0&&mask_calls==0&&sync_calls==0&&shutdown_calls==0);return 0;}
 assert(core.terminal_shutdown&&core.irq_quiescing&&core.recovery_blocked);assert(sync_calls==1&&cancel_calls==1&&mask_calls==1);assert(!core.init_pending.active);assert(core.core_init_done.done==1);
 if(mode==2||mode==3){assert(!core.firmware_shutdown_verified&&core.state==IRIS_CORE_ERROR&&pm_usage==1&&core.shutdown_pm_held&&!power_calls&&!queue_calls);if(mode==3){shutdown_error=0;assert(!iris_core_quiesce(&core));assert(pm_gets==1&&mask_calls==1&&sync_calls==2&&cancel_calls==2&&shutdown_calls==2&&pm_usage==0&&power_calls==1&&queue_calls==1);}return 0;}
 assert(core.firmware_shutdown_verified&&core.irq_drain_verified&&core.recovery_drain_verified&&core.state==IRIS_CORE_DEINIT);assert(!core.shutdown_pm_held&&pm_usage==0&&pm_puts==1&&power_calls==1&&queue_calls==1);
 if(mode==9||mode==10)assert(response_calls==1&&enable_calls==0);
 if(mode==20||mode==21){
  assert(iris_orphan_reclaim_gate(&core)==(mode==21?-EBUSY:0));
  core.irq_drain_verified=false;assert(iris_orphan_reclaim_gate(&core)==-EAGAIN);
 }
 if(mode==0){assert(event_count==8);int want[]={PM,MASK,SYNC,CANCEL,SHUTDOWN,POWER,QUEUES,PUT};assert(!memcmp(events,want,sizeof(want)));assert(!iris_core_quiesce(&core));assert(shutdown_calls==1&&mask_calls==1&&pm_gets==1);}
 return 0;}
'''


def fixture(work, output, sanitize=False):
    core=(work/'iris_core.c').read_text();common=(work/'iris_hfi_common.c').read_text();probe=(work/'iris_probe.c').read_text();vidc=(work/'iris_vidc.c').read_text()
    irq_body=EXTRACT.block(common,r'^irqreturn_t\s+iris_hfi_isr_handler\([^;{}]*\)\s*\{','IRQ thread')
    funcs=EXTRACT.definition(vidc,'iris_orphan_reclaim_gate')+EXTRACT.definition(core,'iris_core_quiesce')+irq_body+EXTRACT.definition(probe,'iris_sys_error_handler')+EXTRACT.definition(vidc,'iris_add_session')
    # The exact syscall-like callbacks above are seams; extracted driver code is not rewritten.
    prefix = PREFIX
    if 'queues_allocated' in core:
        prefix = prefix.replace('bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;};',
            'bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;\n bool queues_allocated,power_prepared,firmware_may_run,removing;u32 open_inflight,open_owners;};')
    if 'core->hardware_owner_held' in core:
        prefix = prefix.replace('bool queues_allocated,power_prepared,firmware_may_run,removing;',
            'bool hardware_owner_held,resources_releasing,queues_allocated,power_prepared,firmware_may_run,removing;')
    main = MAIN
    if 'core->hardware_owner_held' in core:
        main = main.replace('core.irq=9;', 'core.hardware_owner_held=true;core.irq=9;')
    if 'queues_allocated' in core:
        main = main.replace('core.irq=9;', 'core.queues_allocated=core.power_prepared=core.firmware_may_run=true;core.irq=9;')
    if 'core->removing' in vidc:
        if 'queues_allocated' not in prefix:
            prefix = prefix.replace('bool firmware_shutdown_verified,', 'bool removing;\n bool firmware_shutdown_verified,')
    # Checked-power hooks exist only in the later candidate; the legacy void
    # poweroff path keeps the original quiesce expectations of this suite.
    if 'iris_vpu_has_checked_power' in core:
        prefix += '''
static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}
static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
'''
    if 'iris_power_progress_pending' in vidc:
        prefix = prefix.replace('u32 open_inflight,open_owners;};', 'u32 open_inflight,open_owners;bool power_state_unknown;};')
        prefix += '''
static bool iris_power_progress_pending(struct iris_core *c){assert(c==&core);return false;}
'''
    if 'terminal_shutdown && core->state == IRIS_CORE_DEINIT' in core:
        prefix = prefix.replace('bool power_state_unknown;', 'bool power_state_unknown,power_irq_enabled;int power_cleanup_error;')
        prefix = prefix.replace('static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}',
                                'static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return true;}')
        prefix = prefix.replace('static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}',
                                'static int iris_vpu_power_off_checked(struct iris_core *c){iris_vpu_power_off(c);return 0;}')
        prefix = prefix.replace('return shutdown_error;}', 'if (!shutdown_error)c->firmware_may_run=false;return shutdown_error;}')
        main = main.replace('core.terminal_shutdown=true;}', 'core.terminal_shutdown=true;core.queues_allocated=core.power_prepared=core.firmware_may_run=false;}')
    source=output/'quiesce.c';source.write_text(prefix+funcs+main);binary=output/'quiesce'
    args=['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-pthread',str(source),'-o',str(binary)]
    if sanitize:args+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
    r=subprocess.run(args,capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    return binary


class QuiesceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='iris-quiesce-tests-');cls.addClassCleanup(cls.temp.cleanup);cls.binary=fixture(WORK,Path(cls.temp.name))
    def run_case(self,mode):
        r=subprocess.run([str(self.binary),str(mode)],capture_output=True,text=True,timeout=5);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
    def mutation_rejected(self,before,after,mode):
        source=(Path(self.temp.name)/'quiesce.c').read_text();self.assertEqual(source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-quiesce-mutation-') as directory:
            p=Path(directory);src=p/'mutant.c';exe=p/'mutant';src.write_text(source.replace(before,after,1))
            r=subprocess.run(['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-pthread',str(src),'-o',str(exe)],capture_output=True,text=True,timeout=40);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            r=subprocess.run([str(exe),str(mode)],capture_output=True,text=True,timeout=5);self.assertNotEqual(r.returncode,0);self.assertIn('Assertion',r.stderr)
    def test_oracle_rejects_shutdown_error_ignored(self):
        text = (WORK / 'iris_core.c').read_text()
        before = 'ret = core->firmware_may_run ? iris_fw_unload(core) : 0;' if 'core->firmware_may_run ?' in text else 'ret = iris_fw_unload(core);'
        self.mutation_rejected(before, before + ' ret = 0;', 2)
    def test_oracle_rejects_thread_enable_after_quiesce(self):
        self.mutation_rejected('if (!core->irq_quiescing && !core->terminal_shutdown &&','if (true &&',9)
    def test_oracle_rejects_double_disable_on_retry(self):
        self.mutation_rejected('if (!core->quiesce_irq_mask_held) {','if (true) {',3)


for mode in range(22):
    def test(self,value=mode):self.run_case(value)
    setattr(QuiesceTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',required=True,type=Path);p.add_argument('--sanitize',action='store_true');args=p.parse_args();WORK=args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-quiesce-sanitizer-') as d:
            binary=fixture(WORK,Path(d),True)
            for mode in range(22):
                r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode:print(r.stdout+r.stderr);return 1
            print('22 quiesce/IRQ/worker ASan/UBSan cases passed; host callback models, no hardware evidence');return 0
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(QuiesceTests));return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
