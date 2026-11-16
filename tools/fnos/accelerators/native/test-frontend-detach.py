#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual retryable detach and video release callbacks with host V4L2 seams.

unregister does not drain open/mmap/video references. The void platform remove
is not tested as safe and is intentionally not connected to this interface.
When detach calls quiesce/reclaim these remain explicit contract seams here;
actual terminal proofs and reclamation are covered by test-lifecycle-batch.py.
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
CASES = 24

PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
typedef uint32_t u32;
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
static bool list_empty(struct list_head *h){return h->next==h;}
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
struct iris_core;
struct video_device {struct iris_core *core;int registered,refs;};
struct v4l2_device {int unused;};
struct iris_core {struct mutex lock,detach_lock;struct list_head instances,orphan_instances;
 struct delayed_work sys_error_handler;struct video_device *vdev_dec,*vdev_enc;
 struct v4l2_device v4l2_dev;u32 video_owners,open_inflight,open_owners,orphan_owners;
 bool decoder_registered,encoder_registered,v4l2_registered,frontend_detached,detach_complete;
 bool removing,remove_prepared,power_state_unknown;atomic_int refs;};
static struct iris_core core;
static struct video_device dec,enc;
static bool unsupported,self_work,interrupt_context,release_during_unregister,fail_open_during_unregister;
static bool concurrent_case;
static pthread_barrier_t detach_entry,detach_refs_ready;
static int prepare_error,release_error,video_unregs,video_frees,v4l2_unregs,prepares,releases;
static atomic_int gets,puts;
static int events[32],event_count;
enum {DEC=1,ENC,PREPARE,RELEASE,V4L2};
static void event(int e){assert(event_count<32);events[event_count++]=e;}
static void entry_gate(pthread_barrier_t *gate){int ret=pthread_barrier_wait(gate);assert(!ret||ret==PTHREAD_BARRIER_SERIAL_THREAD);}
static void iris_core_get(struct iris_core *c){
 assert(c==&core);
 /* Rendezvous only before detach_lock: callbacks/put must never wait here. */
 if(concurrent_case)entry_gate(&detach_entry);
 assert(atomic_fetch_add(&c->refs,1)>0);atomic_fetch_add(&gets,1);
 /* Both callers hold their references before either can serialize detach. */
 if(concurrent_case)entry_gate(&detach_refs_ready);
}
static void iris_core_put(struct iris_core *c){assert(c==&core);assert(atomic_fetch_sub(&c->refs,1)>1);atomic_fetch_add(&puts,1);}
static struct iris_core *video_get_drvdata(struct video_device *v){assert(v==&dec||v==&enc);return v->core;}
static void video_device_release(struct video_device *v){assert(!v->registered&&!v->refs);video_frees++;}
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return interrupt_context;}
static bool iris_vpu_has_checked_power(struct iris_core *c){assert(c==&core);return !unsupported;}
static void locks_available(void){assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));}
static int iris_core_prepare_remove(struct iris_core *c){assert(c==&core&&c->removing&&!c->video_owners&&!c->open_inflight&&!c->open_owners&&!c->orphan_owners);locks_available();prepares++;event(PREPARE);if(!prepare_error)c->remove_prepared=true;return prepare_error;}
static int iris_release_resources(struct iris_core *c){assert(c==&core&&c->remove_prepared&&!c->video_owners&&!c->open_inflight&&!c->open_owners&&!c->orphan_owners);locks_available();releases++;event(RELEASE);return release_error;}
static void v4l2_device_unregister(struct v4l2_device *v){assert(v==&core.v4l2_dev&&!core.video_owners&&!release_error&&releases);locks_available();v4l2_unregs++;event(V4L2);}
static void iris_video_device_release(struct video_device *v);
static void video_put(struct video_device *v){assert(v->refs>0);if(!--v->refs)iris_video_device_release(v);}
static void video_unregister_device(struct video_device *v){
 if(!v)return;
 assert(v->registered&&core.removing&&core.frontend_detached);
 locks_available();video_unregs++;event(v==&dec?DEC:ENC);v->registered=false;
 if(fail_open_during_unregister&&v==&dec){assert(core.open_inflight==1);core.open_inflight--;core.refs--;video_put(v);}
 video_put(v);
 if(release_during_unregister&&v==&enc&&core.open_owners){core.open_owners--;core.refs--;video_put(&dec);}
}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.detach_lock.lock,NULL));INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);atomic_init(&core.refs,3);atomic_init(&gets,0);atomic_init(&puts,0);core.video_owners=2;core.vdev_dec=&dec;core.vdev_enc=&enc;core.decoder_registered=core.encoder_registered=core.v4l2_registered=true;dec=(struct video_device){&core,1,1};enc=(struct video_device){&core,1,1};}
static void existing_file(void){core.open_owners++;core.refs++;dec.refs++;}
static void close_file(void){assert(core.open_owners==1);core.open_owners--;core.refs--;video_put(&dec);}
static void *run_detach(void *p){int *ret=p;*ret=iris_device_detach(&core);return NULL;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==1||mode==2||mode==3||mode==4||mode==18||mode==19){existing_file();}
 if(mode==3){core.open_owners--;core.open_inflight++;}
 if(mode==4){core.open_owners--;core.orphan_owners++;}
 if(mode==5||mode==6){dec.refs++;}
 if(mode==7)unsupported=true;
 if(mode==8)core.power_state_unknown=true;
 if(mode==9||mode==10)prepare_error=-EACCES;
 if(mode==11||mode==12)release_error=-EBUSY;
 if(mode==13)self_work=true;
 if(mode==14)interrupt_context=true;
 if(mode==15){static struct list_head active;core.instances.next=&active;}
 if(mode==16){static struct list_head orphan;core.orphan_instances.next=&orphan;}
 if(mode==17){core.remove_prepared=true;}
 if(mode==18)release_during_unregister=true;
 if(mode==19){core.open_owners--;core.open_inflight++;fail_open_during_unregister=true;}
 if(mode==20){core.encoder_registered=false;enc.registered=false;video_put(&enc);}
 if(mode==21){core.decoder_registered=false;dec.registered=false;video_put(&dec);core.encoder_registered=false;enc.registered=false;video_put(&enc);}
 if(mode==22){core.v4l2_registered=false;}
 if(mode==23){
  int a=-1,b=-1;pthread_t ta,tb;
  assert(!pthread_barrier_init(&detach_entry,NULL,2));
  assert(!pthread_barrier_init(&detach_refs_ready,NULL,2));
  concurrent_case=true;
  assert(!pthread_create(&ta,NULL,run_detach,&a));assert(!pthread_create(&tb,NULL,run_detach,&b));
  assert(!pthread_join(ta,NULL));assert(!pthread_join(tb,NULL));
  concurrent_case=false;
  assert(!pthread_barrier_destroy(&detach_entry));assert(!pthread_barrier_destroy(&detach_refs_ready));
  assert(!a&&!b&&core.detach_complete&&core.refs==1&&video_unregs==2&&video_frees==2&&v4l2_unregs==1&&prepares==1&&releases==1);
  assert(atomic_load(&gets)==2&&atomic_load(&puts)==4);
  return 0;
 }
 int before=core.refs;
 int ret=iris_device_detach(&core);
 int expected=(mode>=1&&mode<=6)||mode==15||mode==16?-EBUSY:mode==7?-EOPNOTSUPP:mode==8?-EUCLEAN:(mode==9||mode==10)?-EACCES:(mode==11||mode==12)?-EBUSY:mode==13?-EDEADLK:mode==14?-EWOULDBLOCK:0;
 assert(ret==expected);
 if(mode==13||mode==14){assert(!core.removing&&!core.frontend_detached&&!video_unregs&&!gets&&core.refs==before);return 0;}
 assert(core.removing&&core.frontend_detached);assert(!core.decoder_registered&&!core.encoder_registered);
 if(expected){assert(!core.detach_complete&&!v4l2_unregs);if(expected==-EBUSY&&mode<=6)assert(!prepares&&!releases);}
 if(mode==1){assert(core.open_owners==1&&core.video_owners==1&&dec.refs==1&&!core.vdev_enc&&core.vdev_dec==&dec);return 0;}
 if(mode==2){close_file();assert(!iris_device_detach(&core));assert(core.refs==1&&core.detach_complete&&video_unregs==2&&v4l2_unregs==1);return 0;}
 if(mode==3){assert(core.open_inflight==1&&core.video_owners==1);core.open_inflight--;core.refs--;video_put(&dec);assert(!iris_device_detach(&core));assert(video_unregs==2);return 0;}
 if(mode==4){assert(core.orphan_owners==1&&dec.refs==1);video_put(&dec);assert(iris_device_detach(&core)==-EBUSY);assert(!prepares&&!releases);return 0;}
 if(mode==5){assert(core.video_owners==1&&!core.open_owners&&!prepares);return 0;}
 if(mode==6){video_put(&dec);assert(!iris_device_detach(&core)&&core.refs==1&&core.detach_complete&&video_unregs==2);return 0;}
 if(mode==7||mode==8||mode==15||mode==16){assert(!prepares&&!releases);return 0;}
 if(mode==9){assert(prepares==1&&!releases);return 0;}
 if(mode==10){prepare_error=0;assert(!iris_device_detach(&core)&&prepares==2&&releases==1&&video_unregs==2);return 0;}
 if(mode==11){assert(core.remove_prepared&&prepares==1&&releases==1);return 0;}
 if(mode==12){release_error=0;assert(!iris_device_detach(&core));assert(prepares==1&&releases==2&&core.refs==1&&video_unregs==2);return 0;}
 assert(core.detach_complete&&!core.video_owners&&core.refs==1&&!core.vdev_dec&&!core.vdev_enc);assert(v4l2_unregs==(mode==22?0:1));assert(prepares==(mode==17?0:1));int calls=video_unregs+prepares+releases+v4l2_unregs;assert(!iris_device_detach(&core));assert(calls==video_unregs+prepares+releases+v4l2_unregs&&core.refs==1);
 if(mode==0){int want[]={DEC,ENC,PREPARE,RELEASE,V4L2};assert(event_count==5&&!memcmp(want,events,sizeof(want)));}
 return 0;}
'''


ORPHAN_COORDINATOR_SEAMS = r'''
/* Host contract seams ONLY: never write firmware/IRQ/work proof bits. */
static int orphan_reclaim_error,quiesces,reclaims;
static bool seam_quiesce_completed,seam_reclaim_completed;
static int iris_core_quiesce(struct iris_core *c){
 assert(c==&core&&c->removing&&!c->video_owners&&!c->open_inflight&&!c->open_owners);
 assert(list_empty(&c->instances));locks_available();quiesces++;event(QUIESCE);
 /* Orphan ownership is permitted by quiesce, not silently retired here. */
 seam_quiesce_completed=true;return 0;
}
static int iris_reclaim_orphan_instances(struct iris_core *c){
 assert(c==&core&&seam_quiesce_completed&&!c->video_owners&&!c->open_inflight&&!c->open_owners);
 assert(list_empty(&c->instances));locks_available();reclaims++;event(RECLAIM);
 if(orphan_reclaim_error)return orphan_reclaim_error;
 /* Constructed orphan/instance resource owner has one CPU reference each.
  * This models the contract, not actual buffer/instance destruction. */
 while(c->orphan_owners){c->orphan_owners--;iris_core_put(c);}
 INIT_LIST_HEAD(&c->orphan_instances);seam_reclaim_completed=true;return 0;
}
'''


def source(work):
    probe = (work / 'iris_probe.c').read_text()
    coordinator = EXTRACT.definition(probe, 'iris_device_detach')
    prefix, main = PREFIX, MAIN
    if 'iris_reclaim_orphan_instances' in coordinator:
        prefix = prefix.replace('enum {DEC=1,ENC,PREPARE,RELEASE,V4L2};',
                                'enum {DEC=1,ENC,PREPARE,RELEASE,V4L2,QUIESCE,RECLAIM};')
        prefix = prefix.replace('locks_available();prepares++;event(PREPARE);',
                                'assert(list_empty(&c->instances)&&list_empty(&c->orphan_instances));'
                                'assert(event_count&&events[event_count-1]==RECLAIM);'
                                'locks_available();prepares++;event(PREPARE);')
        prefix += ORPHAN_COORDINATOR_SEAMS
        main = main.replace('if(mode==4){core.open_owners--;core.orphan_owners++;}',
                            'if(mode==4){core.open_owners--;core.orphan_owners++;orphan_reclaim_error=-EAGAIN;}')
        main = main.replace('if(mode==16){static struct list_head orphan;core.orphan_instances.next=&orphan;}',
                            'if(mode==16){static struct list_head orphan;core.orphan_instances.next=&orphan;orphan_reclaim_error=-EBUSY;}')
        main = main.replace(
            'if(mode==4){assert(core.orphan_owners==1&&dec.refs==1);video_put(&dec);assert(iris_device_detach(&core)==-EBUSY);assert(!prepares&&!releases);return 0;}',
            'if(mode==4){assert(core.orphan_owners==1&&dec.refs==1&&!quiesces&&!reclaims);'
            'video_put(&dec);int held=core.refs;assert(iris_device_detach(&core)==-EAGAIN);'
            'assert(core.orphan_owners==1&&core.refs==held&&!core.detach_complete&&!v4l2_unregs&&!prepares&&!releases);'
            'assert(quiesces==1&&reclaims==1);orphan_reclaim_error=0;assert(!iris_device_detach(&core));'
            'assert(!core.orphan_owners&&core.refs==1&&core.detach_complete&&quiesces==2&&reclaims==2&&prepares==1&&releases==1);return 0;}')
        main = main.replace('if(mode==7||mode==8||mode==15||mode==16){assert(!prepares&&!releases);return 0;}',
                            'if(mode==7||mode==8||mode==15||mode==16){assert(!prepares&&!releases);'
                            'if(mode==16)assert(quiesces==1&&reclaims==1&&!list_empty(&core.orphan_instances)&&core.refs==1&&!v4l2_unregs);return 0;}')
        main = main.replace('int want[]={DEC,ENC,PREPARE,RELEASE,V4L2};assert(event_count==5',
                            'int want[]={DEC,ENC,QUIESCE,RECLAIM,PREPARE,RELEASE,V4L2};assert(event_count==7')
        # These are seam observations, not proof bits attributed to actual C.
        main = main.replace('int before=core.refs;',
                            'int before=core.refs;seam_quiesce_completed=seam_reclaim_completed=false;')
        main = main.replace('assert(ret==expected);',
                            'assert(ret==expected);if(!expected&&mode!=17)assert(seam_quiesce_completed&&seam_reclaim_completed);')
    return prefix + EXTRACT.definition(probe, 'iris_video_device_release') + coordinator + main


def compile_fixture(text, output, sanitize=False):
    src = output / 'detach.c'
    src.write_text(text)
    binary = output / 'detach'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class DetachTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-detach-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-detach-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_unregister_is_not_drain(self):
        self.mutation('core->video_owners || !list_empty(&core->instances)', '!list_empty(&core->instances)', 5)

    def test_oracle_release_error_propagates(self):
        self.mutation('ret = iris_release_resources(core);', 'ret = iris_release_resources(core); ret = 0;', 11)

    def test_oracle_revocation_precedes_unregister(self):
        self.mutation('core->removing = true;', 'core->removing = false;', 0)

    def test_oracle_retry_skips_fresh_pm_resume(self):
        self.mutation('if (!core->remove_prepared) {', 'if (true) {', 12)

    def test_oracle_no_duplicate_unregister(self):
        self.mutation('core->frontend_detached = true;', 'core->frontend_detached = false;', 2)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(DetachTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-detach-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(CASES):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
        print('%d frontend detach ASan/UBSan scenarios passed; limited V4L2/PM/quiesce/reclaim contract seams, no remove/DMA proof' % CASES)
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(DetachTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
