#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual IRQ request/activation/top/thread/terminal/startup with disable-depth seams."""
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
CASES = 30

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
typedef uint32_t u32;
typedef int irqreturn_t;
#define IRQ_NONE 0
#define IRQ_WAKE_THREAD 1
#define IRQ_HANDLED 2
#define IRQF_TRIGGER_HIGH 1
#define IRQF_NO_AUTOEN 2
#define IRIS_CORE_DEINIT 0
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define READ_ONCE(x) (x)
#define dev_err(dev,...) ((void)(dev))
#define HFI_CMD_INIT 0x1000001
#define HFI_FW_FLAGS_SUCCESS 1
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
#define lockdep_assert_held(p) do {int _r=pthread_mutex_trylock(&(p)->lock);assert(_r==EBUSY);if(!_r)pthread_mutex_unlock(&(p)->lock);} while(0)
typedef struct {pthread_mutex_t lock;} raw_spinlock_t;
#define raw_spin_lock_irqsave(p,f) ((f)=0,assert(!pthread_mutex_lock(&(p)->lock)))
#define raw_spin_unlock_irqrestore(p,f) ((void)(f),assert(!pthread_mutex_unlock(&(p)->lock)))
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
static bool list_empty(struct list_head *h){return h->next==h;}
struct completion {int done;};
static void complete(struct completion *c){c->done++;}
static void reinit_completion(struct completion *c){c->done=0;}
struct iris_hfi_pending {bool active;u32 id;};
static void iris_hfi_pending_cancel(struct iris_hfi_pending *p){p->active=false;}
static bool iris_hfi_pending_match(struct iris_hfi_pending *p,u32 id,u32 type,u32 port){return p->active&&p->id==id&&type==HFI_CMD_INIT&&port==0;}
struct iris_hfi_packet {u32 type,flags,packet_id,port;};
struct work_struct {int unused;};struct delayed_work {struct work_struct work;};
struct iris_core;
struct iris_hfi_response_ops {void (*hfi_response_handler)(struct iris_core *);};
struct iris_core {struct mutex lock,lifecycle_lock,quiesce_lock;raw_spinlock_t irq_lock;
 struct list_head instances,orphan_instances;struct delayed_work sys_error_handler;
 int irq;void *dev;u32 state,intr_status;const struct iris_hfi_response_ops *hfi_response_ops;
 struct completion core_init_done;struct iris_hfi_pending init_pending;
 bool irq_registered,irq_managed,irq_releasing,irq_initial_mask_held,irq_top_mask_held,irq_accepting,irq_started;
 bool firmware_booted,init_acknowledged,gen2_ack_supported,core_ready,hardware_owner_held,resources_releasing,irq_probe_unpublished;
 bool queues_allocated,power_prepared,power_irq_enabled,power_state_unknown,firmware_touched,firmware_may_run;
 bool terminal_shutdown,irq_quiescing,recovery_blocked,quiesce_irq_mask_held,shutdown_pm_held;
 bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified,removing;
 u32 open_inflight,open_owners;int power_cleanup_error;};
static struct iris_core core;
static int depth,disables,enables,requests,request_error,stage,boot_error,load_error,publish_error,queue_error,power_error;
static int syncs,cancels,unloads,poweroffs,queue_frees,resumes,pm_usage,responses,clears;
static int shutdown_error;static bool unsupported,wrong_ack,failed_ack,missing_ack,watchdog,self_work,in_irq;
static bool wrong_completion;
static bool response_block,response_entered,response_release,queued,running;
static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond=PTHREAD_COND_INITIALIZER;
static pthread_t thread;
static bool inject_on_enable,join_pending;
static irqreturn_t (*registered_top)(int,void *),(*registered_thread)(int,void *);
static void *run_thread(void *unused){(void)unused;assert(!pthread_mutex_lock(&gate));queued=false;running=true;assert(!pthread_mutex_unlock(&gate));assert(registered_thread(9,&core)==IRQ_HANDLED);assert(!pthread_mutex_lock(&gate));running=false;assert(!pthread_cond_broadcast(&cond));assert(!pthread_mutex_unlock(&gate));return NULL;}
static void deliver(void){assert(core.irq_registered&&depth==0);irqreturn_t ret=registered_top(9,&core);assert(ret==IRQ_WAKE_THREAD);queued=true;join_pending=true;assert(!pthread_create(&thread,NULL,run_thread,NULL));}
static int request_threaded_irq(int irq,irqreturn_t (*top)(int,void *),irqreturn_t (*bottom)(int,void *),u32 flags,const char *name,void *data){assert(irq==9&&!strcmp(name,"iris")&&data==&core);assert(flags&IRQF_NO_AUTOEN);requests++;if(request_error)return request_error;depth=1;registered_top=top;registered_thread=bottom;return 0;}
static void enable_irq(int irq){assert(irq==9&&depth>0);assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));enables++;depth--;if(depth==0){assert(stage>=3&&core.queues_allocated&&core.power_prepared&&core.firmware_booted&&!core.terminal_shutdown&&!core.quiesce_irq_mask_held);if(inject_on_enable){inject_on_enable=false;deliver();}}}
static void disable_irq_nosync(int irq){assert(irq==9&&core.irq_registered);assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));disables++;depth++;}
static void wait_locks_free(void){assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));assert(!pthread_mutex_trylock(&core.lifecycle_lock.lock));assert(!pthread_mutex_unlock(&core.lifecycle_lock.lock));}
static void synchronize_irq(int irq){assert(irq==9);wait_locks_free();syncs++;assert(!pthread_mutex_lock(&gate));response_release=true;assert(!pthread_cond_broadcast(&cond));assert(!pthread_mutex_unlock(&gate));if(join_pending){assert(!pthread_join(thread,NULL));join_pending=false;}assert(!queued&&!running);}
static bool cancel_delayed_work_sync(struct delayed_work *w){assert(w==&core.sys_error_handler);wait_locks_free();cancels++;return false;}
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return in_irq;}
static bool iris_vpu_has_checked_power(struct iris_core *c){assert(c==&core);return !unsupported;}
static bool iris_power_progress_pending(struct iris_core *c){assert(c==&core);return c->power_prepared;}
static int pm_runtime_resume_and_get(void *d){assert(d==&core);resumes++;pm_usage++;return 0;}
static void pm_runtime_put_noidle(void *d){assert(d==&core&&pm_usage>0);pm_usage--;}
static int iris_fw_unload(struct iris_core *c){assert(c==&core);unloads++;if(!shutdown_error){c->firmware_may_run=false;c->firmware_shutdown_verified=true;}return shutdown_error;}
static int iris_vpu_power_off_checked(struct iris_core *c){assert(c==&core&&syncs&&cancels&&depth>0&&!c->irq_top_mask_held&&!queued&&!running);poweroffs++;c->power_irq_enabled=false;return 0;}
static int iris_hfi_queues_init(struct iris_core *c){assert(c==&core);return queue_error;}
static void iris_hfi_queues_deinit(struct iris_core *c){assert(c==&core&&!queued&&!running);queue_frees++;}
static int iris_vpu_power_on_checked(struct iris_core *c){assert(c==&core&&depth==1&&enables==0);stage=1;return power_error;}
static int iris_vpu_power_on(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
static int iris_fw_load(struct iris_core *c){assert(c==&core&&stage==1&&depth==1&&enables==0);if(load_error)return load_error;c->firmware_touched=c->firmware_may_run=true;stage=2;return 0;}
static int iris_vpu_boot_firmware(struct iris_core *c){assert(c==&core&&stage==2&&depth==1&&enables==0);if(boot_error)return boot_error;stage=3;return 0;}
static int iris_hfi_core_init(struct iris_core *c){assert(c==&core&&stage==3&&c->irq_started&&c->firmware_booted&&!c->core_ready);c->init_pending.active=true;c->init_pending.id=17;stage=4;if(publish_error)return publish_error;if(wrong_completion)complete(&c->core_init_done);else if(!missing_ack&&depth==0)deliver();return 0;}
static int iris_wait_for_system_response(struct iris_core *c){assert(c==&core&&stage==4&&!c->core_ready);if(join_pending){assert(!pthread_join(thread,NULL));join_pending=false;}iris_hfi_pending_cancel(&c->init_pending);return c->state==IRIS_CORE_ERROR?-EIO:c->core_init_done.done?0:-ETIMEDOUT;}
static void pm_runtime_mark_last_busy(void *d){assert(d==&core);}
static void iris_vpu_clear_interrupt(struct iris_core *c){assert(c==&core&&stage>=3&&c->queues_allocated&&c->power_prepared);clears++;}
static int iris_vpu_watchdog(struct iris_core *c,u32 value){(void)c;(void)value;return watchdog?-ETIME:0;}
static int iris_hfi_gen2_handle_system_init(struct iris_core *c,struct iris_hfi_packet *p);
static void response(struct iris_core *c){responses++;assert(c->queues_allocated&&c->firmware_booted);if(response_block){assert(!pthread_mutex_lock(&gate));response_entered=true;assert(!pthread_cond_broadcast(&cond));while(!response_release)assert(!pthread_cond_wait(&cond,&gate));assert(c->queues_allocated);assert(!pthread_mutex_unlock(&gate));}if(c->init_pending.active){struct iris_hfi_packet p={HFI_CMD_INIT,failed_ack?0:HFI_FW_FLAGS_SUCCESS,wrong_ack?18:17,0};assert(!iris_hfi_gen2_handle_system_init(c,&p));}}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));assert(!pthread_mutex_init(&core.irq_lock.lock,NULL));INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);core.irq=9;core.dev=&core;core.hardware_owner_held=true;core.gen2_ack_supported=true;static const struct iris_hfi_response_ops ops={response};core.hfi_response_ops=&ops;}
static void activate_ready(void){assert(!iris_request_irq(&core));core.queues_allocated=core.power_prepared=core.firmware_booted=core.firmware_may_run=true;core.state=IRIS_CORE_INIT;stage=3;mutex_lock(&core.lock);assert(!iris_irq_activate(&core));mutex_unlock(&core.lock);assert(depth==0&&enables==1);}
static void finish_thread(void){if(join_pending){assert(!pthread_join(thread,NULL));join_pending=false;}}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==0){assert(!iris_request_irq(&core));assert(depth==1&&enables==0&&disables==0&&core.irq_initial_mask_held);return 0;}
 if(mode==1){request_error=-EBUSY;assert(iris_request_irq(&core)==-EBUSY);assert(!core.irq_registered&&!core.irq_initial_mask_held&&!enables);return 0;}
 if(mode>=2&&mode<=8){assert(!iris_request_irq(&core));if(mode==3)load_error=-ENOENT;if(mode==4)boot_error=-EINVAL;if(mode==5)publish_error=-ENOSPC;if(mode==6)wrong_ack=true;if(mode==7)failed_ack=true;if(mode==8)missing_ack=true;int r=iris_core_init(&core);int expected=mode==3?-ENOENT:mode==4?-EINVAL:mode==5?-ENOSPC:mode==6||mode==8?-ETIMEDOUT:mode==7?-EIO:0;assert(r==expected);if(mode==2){assert(core.core_ready&&core.init_acknowledged&&depth==0&&responses==1);assert(!iris_core_init(&core));}else{assert(!core.core_ready);if(mode==3||mode==4)assert(!enables&&depth==1);else assert(enables>=1);assert(core.queues_allocated&&!queue_frees);}return 0;}
 if(mode==9){activate_ready();deliver();finish_thread();assert(depth==0&&disables==1&&enables==2&&!core.irq_top_mask_held);return 0;}
 if(mode==10){activate_ready();assert(!iris_core_quiesce(&core));assert(depth==1&&disables==1&&enables==1&&!core.irq_top_mask_held&&core.quiesce_irq_mask_held);return 0;}
 if(mode==11){assert(!iris_request_irq(&core));assert(!iris_core_quiesce(&core));assert(depth==1&&!disables&&!enables&&!core.irq_initial_mask_held&&core.quiesce_irq_mask_held);return 0;}
 if(mode==12){activate_ready();assert(registered_top(9,&core)==IRQ_WAKE_THREAD);assert(depth==1&&core.irq_top_mask_held);mutex_lock(&core.lock);core.terminal_shutdown=core.irq_quiescing=true;iris_irq_terminal_mask(&core);mutex_unlock(&core.lock);assert(depth==2);assert(registered_thread(9,&core)==IRQ_HANDLED);core.irq_drain_verified=true;mutex_lock(&core.lock);iris_irq_retire_top(&core);mutex_unlock(&core.lock);assert(depth==1&&!core.irq_top_mask_held);return 0;}
 if(mode==13){activate_ready();response_block=true;deliver();assert(!pthread_mutex_lock(&gate));while(!response_entered)assert(!pthread_cond_wait(&cond,&gate));assert(!pthread_mutex_unlock(&gate));assert(!iris_core_quiesce(&core));assert(depth==1&&syncs==1&&queue_frees==1&&!core.irq_top_mask_held&&!running);return 0;}
 if(mode==14){activate_ready();watchdog=true;deliver();finish_thread();assert(depth==1&&core.irq_top_mask_held);assert(!iris_core_quiesce(&core));assert(depth==1&&disables==2&&enables==2&&!core.irq_top_mask_held);return 0;}
 if(mode==15){activate_ready();shutdown_error=-EACCES;assert(iris_core_quiesce(&core)==-EACCES);assert(depth==1&&disables==1&&!queue_frees&&core.shutdown_pm_held);shutdown_error=0;assert(!iris_core_quiesce(&core));assert(depth==1&&disables==1&&resumes==1&&queue_frees==1);return 0;}
 if(mode==16){activate_ready();assert(!iris_core_quiesce(&core));int e=enables,d=disables;mutex_lock(&core.lock);assert(iris_irq_activate(&core)==-ESHUTDOWN);mutex_unlock(&core.lock);assert(enables==e&&disables==d&&depth==1);return 0;}
 if(mode==17){assert(!iris_request_irq(&core));core.queues_allocated=core.power_prepared=true;core.state=IRIS_CORE_INIT;stage=2;mutex_lock(&core.lock);assert(iris_irq_activate(&core)==-ESHUTDOWN);mutex_unlock(&core.lock);assert(depth==1&&!enables);return 0;}
 if(mode==18){activate_ready();assert(registered_top(9,&core)==IRQ_WAKE_THREAD);int d=disables;assert(registered_top(9,&core)==IRQ_HANDLED);assert(disables==d&&depth==1);core.irq_drain_verified=true;mutex_lock(&core.lock);core.terminal_shutdown=core.irq_quiescing=true;iris_irq_terminal_mask(&core);iris_irq_retire_top(&core);mutex_unlock(&core.lock);assert(depth==1);return 0;}
 if(mode==19){assert(!iris_request_irq(&core));core.state=IRIS_CORE_INIT;assert(iris_core_init(&core)==-EAGAIN);assert(!core.core_ready&&!enables);return 0;}
 if(mode==20){activate_ready();self_work=true;assert(iris_core_quiesce(&core)==-EDEADLK);assert(depth==0&&!disables);return 0;}
 if(mode==21){activate_ready();in_irq=true;assert(iris_core_quiesce(&core)==-EWOULDBLOCK);assert(depth==0&&!disables);return 0;}
 if(mode==22){assert(!iris_request_irq(&core));queue_error=-ENOMEM;assert(iris_core_init(&core)==-ENOMEM);assert(depth==1&&!enables&&!core.queues_allocated);return 0;}
 if(mode==23){activate_ready();core.state=IRIS_CORE_ERROR;assert(registered_top(9,&core)==IRQ_WAKE_THREAD);assert(registered_thread(9,&core)==IRQ_HANDLED);assert(depth==1&&core.irq_top_mask_held);assert(!iris_core_quiesce(&core));assert(depth==1&&!core.irq_top_mask_held);return 0;}
 if(mode==24){activate_ready();assert(!iris_core_quiesce(&core));int d=disables;assert(registered_top(9,&core)==IRQ_HANDLED);assert(disables==d&&depth==1);return 0;}
 if(mode==25){assert(!iris_request_irq(&core));wrong_completion=true;assert(iris_core_init(&core)==-EPROTO);assert(!core.core_ready&&!core.init_acknowledged&&core.state==IRIS_CORE_ERROR&&core.queues_allocated);return 0;}
 if(mode==26){assert(!iris_request_irq(&core));inject_on_enable=true;assert(!iris_core_init(&core));assert(core.core_ready&&depth==0&&responses==1&&enables==2&&disables==1);return 0;}
 if(mode==27){activate_ready();mutex_lock(&core.lock);assert(iris_irq_activate(&core)==-ESHUTDOWN);mutex_unlock(&core.lock);assert(enables==1&&depth==0);return 0;}
 if(mode==28){activate_ready();assert(!iris_core_quiesce(&core));int d=disables,e=enables;assert(!iris_core_quiesce(&core));assert(depth==1&&d==disables&&e==enables);return 0;}
 if(mode==29){assert(!iris_request_irq(&core));core.gen2_ack_supported=false;assert(iris_core_init(&core)==-EOPNOTSUPP);assert(depth==1&&!enables&&!core.queues_allocated);return 0;}
 return 2;}
'''


def source(work):
    common = (work / 'iris_hfi_common.c').read_text()
    probe = (work / 'iris_probe.c').read_text()
    core = (work / 'iris_core.c').read_text()
    response = (work / 'iris_hfi_gen2_response.c').read_text()
    functions = ''.join(EXTRACT.definition(common, n) for n in ('iris_irq_activate', 'iris_irq_terminal_mask', 'iris_irq_retire_top'))
    functions += EXTRACT.block(common, r'^irqreturn_t\s+iris_hfi_isr\([^;{}]*\)\s*\{', 'top')
    functions += EXTRACT.block(common, r'^irqreturn_t\s+iris_hfi_isr_handler\([^;{}]*\)\s*\{', 'thread')
    functions += EXTRACT.definition(probe, 'iris_request_irq')
    functions += EXTRACT.definition(response, 'iris_hfi_gen2_handle_system_init')
    functions += EXTRACT.definition(core, 'iris_core_quiesce') + EXTRACT.definition(core, 'iris_core_init')
    prefix, main = PREFIX, MAIN
    if 'Only never-started, never-touched checked hardware permits inline rollback.' in core:
        prefix = prefix.replace('return c->power_prepared;', 'return c->power_prepared && poweroffs == 0;')
        prefix = prefix.replace('assert(c==&core&&syncs&&cancels&&depth>0&&!c->irq_top_mask_held&&!queued&&!running);',
                                'assert(c==&core&&depth>0&&!c->irq_top_mask_held&&!queued&&!running);assert((syncs&&cancels)||(!c->firmware_touched&&!c->firmware_may_run&&!c->power_irq_enabled));')
        main = main.replace('else assert(enables>=1);assert(core.queues_allocated&&!queue_frees);',
                            'else assert(enables>=1);if(mode==3){assert(!core.queues_allocated&&queue_frees==1&&poweroffs==1);}else assert(core.queues_allocated&&!queue_frees);')
    return prefix + functions + main


def compile_fixture(text, output, sanitize=False):
    src = output / 'irq_owner.c';src.write_text(text);binary = output / 'irq_owner'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    r = subprocess.run(args,capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    return binary


class IrqTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='iris-irq-tests-');cls.addClassCleanup(cls.temp.cleanup)
        cls.source=source(WORK);cls.binary=compile_fixture(cls.source,Path(cls.temp.name))
    def run_case(self,mode):
        r=subprocess.run([str(self.binary),str(mode)],capture_output=True,text=True,timeout=5);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
    def mutation(self,before,after,mode):
        self.assertEqual(self.source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-irq-mutation-') as directory:
            binary=compile_fixture(self.source.replace(before,after,1),Path(directory))
            r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5);self.assertNotEqual(r.returncode,0);self.assertIn('Assertion',r.stderr)
    def test_oracle_noautoen(self):self.mutation('IRQF_TRIGGER_HIGH | IRQF_NO_AUTOEN','IRQF_TRIGGER_HIGH',0)
    def test_oracle_top_mask(self):self.mutation('/* The target chip must support a hard-IRQ disable; no driver raw lock is held. */\n\tdisable_irq_nosync(irq);', '/* The target chip must support a hard-IRQ disable; no driver raw lock is held. */\n\t(void)irq;',9)
    def test_oracle_terminal_mask(self):self.mutation('disable_irq_nosync(core->irq);','/* missing permanent mask */ (void)core->irq;',10)
    def test_oracle_top_retirement(self):self.mutation('if (retire)\n\t\tenable_irq(core->irq);','if (retire)\n\t\t(void)core->irq;',14)
    def test_oracle_extra_enable(self):self.mutation('if (!ret)\n\t\tenable_irq(core->irq);', 'if (!ret) { enable_irq(core->irq); enable_irq(core->irq); }',2)
    def test_oracle_duplicate_terminal_disable(self):self.mutation('if (disable)\n\t\tdisable_irq_nosync(core->irq);', 'if (disable) { disable_irq_nosync(core->irq); }\n\tdisable_irq_nosync(core->irq);',15)
    def test_oracle_activate_before_boot(self):self.mutation('!core->queues_allocated || !core->power_prepared || !core->firmware_booted ||','!core->queues_allocated || !core->power_prepared ||',17)
    def test_oracle_ready_before_ack(self):self.mutation('if (!core->init_acknowledged || core->state != IRIS_CORE_INIT ||','if (core->state != IRIS_CORE_INIT ||',25)


for mode in range(CASES):
    def test(self,value=mode):self.run_case(value)
    setattr(IrqTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',type=Path,required=True);p.add_argument('--sanitize',action='store_true');args=p.parse_args();WORK=args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-irq-sanitizer-') as directory:
            binary=compile_fixture(source(WORK),Path(directory),True)
            for mode in range(CASES):
                r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode:print(r.stdout+r.stderr);return 1
        print('%d IRQ-depth/startup ASan/UBSan scenarios passed; host IRQ core model, not hardware proof'%CASES);return 0
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(IrqTests));return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
