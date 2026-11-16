#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual PM owner, command get/put and IRQ unregister with kernel-operation seams."""
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
CASES = 28

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
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
#define lockdep_assert_held(p) do {int _r=pthread_mutex_trylock(&(p)->lock);assert(_r==EBUSY);if(!_r)pthread_mutex_unlock(&(p)->lock);} while(0)
#define lockdep_assert_not_held(p) do {assert(!pthread_mutex_trylock(&(p)->lock));assert(!pthread_mutex_unlock(&(p)->lock));} while(0)
typedef struct {pthread_mutex_t lock;} raw_spinlock_t;
#define raw_spin_lock_irqsave(p,f) ((f)=0,assert(!pthread_mutex_lock(&(p)->lock)))
#define raw_spin_unlock_irqrestore(p,f) ((void)(f),assert(!pthread_mutex_unlock(&(p)->lock)))
struct device {int usage,runtime_error;};
struct iris_core {struct mutex lock,lifecycle_lock,quiesce_lock;raw_spinlock_t irq_lock;struct device *dev;int irq;
 bool irq_managed,pm_guard_held,irq_registered,irq_releasing,hardware_owner_held,resources_releasing;
 bool irq_probe_unpublished,removing,remove_prepared,recovery_drain_verified;
 bool irq_started,irq_initial_mask_held,irq_top_mask_held,quiesce_irq_mask_held,irq_accepting,power_irq_enabled;
 bool terminal_shutdown,irq_quiescing,firmware_booted,queues_allocated,power_prepared;u32 state;
 bool irq_drain_verified;};
#define IRIS_CORE_INIT 1
static struct iris_core core;
static struct device dev;
static int get_error,queue_error,sync_calls,free_calls,puts,noidle_puts,gets,disables,enables;
static bool irq_context;
static void pm_runtime_get_noresume(struct device *d){assert(d==&dev);d->usage++;gets++;}
static void pm_runtime_put_noidle(struct device *d){assert(d==&dev&&d->usage>0);d->usage--;puts++;noidle_puts++;}
static int pm_runtime_put_sync(struct device *d){assert(d==&dev&&d->usage>0);d->usage--;puts++;if(!d->usage&&core.irq_managed){d->runtime_error=-EOPNOTSUPP;return -EOPNOTSUPP;}return 0;}
static void pm_runtime_put_autosuspend(struct device *d){pm_runtime_put_sync(d);}
static int pm_runtime_resume_and_get(struct device *d){assert(d==&dev);if(get_error)return get_error;if(d->runtime_error)return -EINVAL;d->usage++;gets++;return 0;}
static int iris_hfi_queue_cmd_write_locked(struct iris_core *c,void *packet,u32 size){assert(c==&core&&packet==&core&&size==4);lockdep_assert_held(&c->lock);return queue_error;}
static bool in_interrupt(void){return irq_context;}
static void locks_free(void){assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));}
static void synchronize_irq(int irq){assert(irq==9&&core.irq_releasing&&!core.irq_accepting);locks_free();sync_calls++;}
static void free_irq(int irq,void *data){assert(irq==9&&data==&core&&core.irq_registered&&core.irq_releasing&&sync_calls);locks_free();free_calls++;}
static void disable_irq_nosync(int irq){assert(irq==9&&core.irq_registered&&!core.irq_releasing);assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));disables++;}
static void enable_irq(int irq){assert(irq==9&&core.irq_registered&&!core.irq_releasing);assert(!pthread_mutex_trylock(&core.irq_lock.lock));assert(!pthread_mutex_unlock(&core.irq_lock.lock));enables++;}
'''
MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));memset(&dev,0,sizeof(dev));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));assert(!pthread_mutex_init(&core.irq_lock.lock,NULL));core.dev=&dev;core.irq=9;core.irq_managed=true;core.hardware_owner_held=true;core.removing=true;core.irq_probe_unpublished=true;}
static void guard_get(void){mutex_lock(&core.lock);iris_core_pm_guard_get(&core);mutex_unlock(&core.lock);}
static void guard_put(void){mutex_lock(&core.lock);iris_core_pm_guard_put(&core);mutex_unlock(&core.lock);}
static void assert_retired(void){assert(!core.irq_registered&&!core.irq_started&&!core.irq_initial_mask_held&&!core.irq_top_mask_held&&!core.quiesce_irq_mask_held&&!core.irq_accepting&&!core.power_irq_enabled&&core.irq_releasing&&!core.irq_probe_unpublished);}
static int unregister_final(void){int r;mutex_lock(&core.quiesce_lock);core.irq_probe_unpublished=false;core.resources_releasing=core.remove_prepared=core.terminal_shutdown=core.irq_drain_verified=core.recovery_drain_verified=true;r=iris_irq_unregister(&core);mutex_unlock(&core.quiesce_lock);return r;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==0){guard_get();guard_get();assert(dev.usage==1&&gets==1&&core.pm_guard_held);guard_put();guard_put();assert(!dev.usage&&puts==1&&!core.pm_guard_held&&!dev.runtime_error);return 0;}
 if(mode==1||mode==2||mode==3){guard_get();if(mode==2)get_error=-EIO;if(mode==3)queue_error=-ENOSPC;assert(iris_hfi_queue_cmd_write(&core,&core,4)==(mode==2?-EIO:mode==3?-ENOSPC:0));assert(dev.usage==1&&!dev.runtime_error);assert(puts==(mode==2?0:1));if(mode==1){assert(!iris_hfi_queue_cmd_write(&core,&core,4));assert(dev.usage==1&&puts==2);}return 0;}
 if(mode==4){core.irq_managed=false;get_error=-EIO;assert(iris_hfi_queue_cmd_write(&core,&core,4)==-EIO);assert(!puts&&!dev.usage);return 0;}
 if(mode==5){guard_get();assert(pm_runtime_resume_and_get(&dev)==0);iris_core_pm_put(&core);assert(!dev.runtime_error&&dev.usage==1);assert(pm_runtime_resume_and_get(&dev)==0);iris_core_pm_put(&core);assert(!dev.runtime_error&&dev.usage==1&&noidle_puts==2);return 0;}
 if(mode==6){guard_get();get_error=-EACCES;assert(pm_runtime_resume_and_get(&dev)==-EACCES);assert(dev.usage==1&&!puts);return 0;}
 if(mode==7){core.irq_managed=false;guard_get();assert(!dev.usage&&!core.pm_guard_held);return 0;}
 if(mode==8){guard_get();assert(pm_runtime_resume_and_get(&dev)==0);guard_put();assert(dev.usage==1&&!core.pm_guard_held);iris_core_pm_put(&core);assert(!dev.usage&&!dev.runtime_error);return 0;}
 if(mode==9){guard_get();core.irq_registered=true;core.irq_initial_mask_held=true;assert(!iris_irq_unregister(&core));assert_retired();assert(!free_calls||free_calls==1);assert(!iris_irq_unregister(&core));assert(free_calls==1&&sync_calls==1&&!disables&&!enables);return 0;}
 if(mode==10||mode==11){core.irq_registered=true;core.irq_started=true;core.power_irq_enabled=true;core.irq_accepting=true;core.quiesce_irq_mask_held=true;core.terminal_shutdown=true;core.irq_top_mask_held=mode==11;assert(!unregister_final());assert_retired();assert(core.terminal_shutdown&&!enables&&!disables&&free_calls==1);return 0;}
 if(mode==12){assert(!iris_irq_unregister(&core));assert(!free_calls&&!sync_calls);return 0;}
 if(mode==13){core.irq_registered=core.irq_started=true;assert(iris_irq_unregister(&core)==-EBUSY);assert(core.irq_registered&&!core.irq_releasing&&!free_calls);return 0;}
 if(mode==14){core.irq_registered=core.irq_releasing=true;assert(iris_irq_unregister(&core)==-EBUSY);assert(!free_calls);return 0;}
 if(mode==15){core.irq_registered=true;irq_context=true;assert(iris_irq_unregister(&core)==-EWOULDBLOCK);assert(!free_calls&&!core.irq_releasing);return 0;}
 if(mode>=16&&mode<=20){core.irq_registered=mode!=16;core.irq_releasing=mode==17;core.hardware_owner_held=mode!=18;core.resources_releasing=mode==19;core.quiesce_irq_mask_held=true;core.irq_drain_verified=true;core.irq_top_mask_held=true;mutex_lock(&core.lock);iris_irq_retire_top(&core);mutex_unlock(&core.lock);assert(enables==(mode==20));assert(core.irq_top_mask_held==(mode!=20));return 0;}
 if(mode==21){mutex_lock(&core.lock);iris_irq_terminal_mask(&core);mutex_unlock(&core.lock);assert(!disables&&!core.quiesce_irq_mask_held);return 0;}
 if(mode==22){core.irq_registered=true;core.irq_releasing=true;mutex_lock(&core.lock);iris_irq_terminal_mask(&core);mutex_unlock(&core.lock);assert(!disables&&!core.quiesce_irq_mask_held);return 0;}
 if(mode==23){core.irq_registered=true;assert(!iris_irq_unregister(&core));core.firmware_booted=core.power_prepared=core.queues_allocated=true;core.state=IRIS_CORE_INIT;mutex_lock(&core.lock);assert(iris_irq_activate(&core)==-ESHUTDOWN);iris_irq_terminal_mask(&core);iris_irq_retire_top(&core);mutex_unlock(&core.lock);assert(!enables&&!disables);return 0;}
 if(mode==24){guard_get();assert(pm_runtime_resume_and_get(&dev)==0);iris_core_pm_put(&core);guard_put();assert(!dev.runtime_error&&!dev.usage&&noidle_puts==2);return 0;}
 if(mode==25){core.irq_registered=true;core.irq_probe_unpublished=false;core.terminal_shutdown=true;assert(iris_irq_unregister(&core)==-EBUSY);assert(!free_calls&&core.irq_registered&&!core.irq_releasing);return 0;}
 if(mode==26){core.irq_registered=core.irq_started=true;core.irq_probe_unpublished=true;core.removing=true;assert(iris_irq_unregister(&core)==-EBUSY);assert(!free_calls);return 0;}
 if(mode==27){core.irq_registered=true;core.irq_probe_unpublished=true;core.removing=false;assert(iris_irq_unregister(&core)==-EBUSY);assert(!free_calls);return 0;}
 return 2;}
'''


def source(work):
    core = (work / 'iris_core.c').read_text()
    common = (work / 'iris_hfi_common.c').read_text()
    queue = (work / 'iris_hfi_queue.c').read_text()
    functions = ''.join(EXTRACT.definition(core, n) for n in ('iris_core_pm_guard_get', 'iris_core_pm_guard_put', 'iris_core_pm_put'))
    functions += EXTRACT.definition(queue, 'iris_hfi_queue_cmd_write')
    functions += ''.join(EXTRACT.definition(common, n) for n in ('iris_irq_activate', 'iris_irq_terminal_mask', 'iris_irq_retire_top', 'iris_irq_unregister'))
    prefix, main = PREFIX, MAIN
    if 'ret = -ESHUTDOWN;' in EXTRACT.definition(queue, 'iris_hfi_queue_cmd_write'):
        prefix = prefix.replace('bool irq_drain_verified;};', 'bool irq_drain_verified,core_ready;};')
        main = main.replace('core.removing=true;core.irq_probe_unpublished=true;',
                            'core.removing=false;core.core_ready=true;core.state=IRIS_CORE_INIT;core.irq_probe_unpublished=true;')
        main = main.replace('if(mode==9){guard_get();', 'if(mode==9){core.removing=true;guard_get();')
        main = main.replace('if(mode==23){core.irq_registered=true;', 'if(mode==23){core.removing=true;core.irq_registered=true;')
    return prefix + functions + main


def compile_fixture(text, out, sanitize=False):
    src=out/'pm_irq.c';src.write_text(text);exe=out/'pm_irq'
    cmd=['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-pthread',str(src),'-o',str(exe)]
    if sanitize:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    return exe


class OwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='iris-pm-free-tests-');cls.addClassCleanup(cls.temp.cleanup);cls.source=source(WORK);cls.exe=compile_fixture(cls.source,Path(cls.temp.name))
    def run_case(self,mode):
        r=subprocess.run([str(self.exe),str(mode)],capture_output=True,text=True,timeout=5);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
    def mutation(self,before,after,mode):
        self.assertEqual(self.source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-pm-free-mutation-') as d:
            exe=compile_fixture(self.source.replace(before,after,1),Path(d));r=subprocess.run([str(exe),str(mode)],capture_output=True,text=True,timeout=5);self.assertNotEqual(r.returncode,0);self.assertIn('Assertion',r.stderr)
    def test_oracle_failed_get_put(self):self.mutation('if (ret < 0)\n\t\treturn ret;', 'if (ret < 0) { iris_core_pm_put(core); return ret; }',2)
    def test_oracle_sync_put_poison(self):self.mutation('if (core->irq_managed)\n\t\tpm_runtime_put_noidle(core->dev);', 'if (core->irq_managed)\n\t\t(void)pm_runtime_put_sync(core->dev);',24)
    def test_oracle_guard_double_get(self):self.mutation('core->irq_managed && !core->pm_guard_held', 'core->irq_managed',0)
    def test_oracle_stale_irq_state(self):self.mutation('core->irq_initial_mask_held = false;\n\tcore->irq_top_mask_held = false;','core->irq_initial_mask_held = true;\n\tcore->irq_top_mask_held = false;',9)
    def test_oracle_unregistered_mask(self):self.mutation('if (!core->irq_registered || core->irq_releasing || !core->hardware_owner_held ||\n\t    core->resources_releasing) {\n\t\traw_spin_unlock_irqrestore(&core->irq_lock, flags);\n\t\treturn;\n\t}\n\tcore->irq_accepting = false;', 'if (core->irq_releasing || !core->hardware_owner_held ||\n\t    core->resources_releasing) {\n\t\traw_spin_unlock_irqrestore(&core->irq_lock, flags);\n\t\treturn;\n\t}\n\tcore->irq_accepting = false;',21)


for mode in range(CASES):
    def test(self,value=mode):self.run_case(value)
    setattr(OwnerTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',required=True,type=Path);p.add_argument('--sanitize',action='store_true');args=p.parse_args();WORK=args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-pm-free-sanitizer-') as d:
            exe=compile_fixture(source(WORK),Path(d),True)
            for mode in range(CASES):
                r=subprocess.run([str(exe),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode:print(r.stdout+r.stderr);return 1
        print('%d PM guard/IRQ unregister ASan/UBSan scenarios passed; kernel/provider seams, no hardware proof'%CASES);return 0
    r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OwnerTests));return 0 if r.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
