#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual CPU owners, video release and remove preflight; no devres/DMA proof."""
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
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>
typedef uint32_t u32;
#define GFP_KERNEL 0
#define VFL_DIR_M2M 1
#define VFL_TYPE_VIDEO 2
#define V4L2_CAP_VIDEO_M2M_MPLANE 4
#define V4L2_CAP_STREAMING 8
enum domain_type { DECODER = 1, ENCODER = 2 };
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
struct mutex {pthread_mutex_t lock;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->lock));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->lock));}
static int locks_destroyed;
static void mutex_destroy(struct mutex *m){assert(!pthread_mutex_destroy(&m->lock));locks_destroyed++;}
typedef struct {atomic_int value;} refcount_t;
static void refcount_inc(refcount_t *r){assert(atomic_fetch_add(&r->value,1)>0);}
static bool refcount_dec_and_test(refcount_t *r){int old=atomic_fetch_sub(&r->value,1);assert(old>0);return old==1;}
static int refs(refcount_t *r){return atomic_load(&r->value);}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
#define list_for_each_entry(p,h,m) for(struct list_head *_it=(h)->next;_it!=(h)&&((p)=container_of(_it,__typeof__(*(p)),m),1);_it=_it->next)
static bool list_empty(struct list_head *h){return h->next==h;}
static void list_add_tail(struct list_head *n,struct list_head *h){n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;}
struct device {int refs;};
struct work_struct {int unused;};
struct delayed_work {struct work_struct work;};
struct iris_core;
struct video_device {void (*release)(struct video_device *);void *fops,*ioctl_ops,*v4l2_dev;u32 vfl_dir,device_caps;char name[32];struct iris_core *data;};
struct iris_platform_data {u32 max_session_count;};
struct iris_core {struct mutex lock,lifecycle_lock,quiesce_lock,remove_lock;refcount_t owner_refs;
 struct device *dev;void *response_packet;struct list_head instances,orphan_instances;
 struct delayed_work sys_error_handler;u32 open_inflight,open_owners,orphan_owners,state;
 bool removing,remove_prepared,terminal_shutdown,irq_quiescing;
 struct video_device *vdev_dec,*vdev_enc;void *iris_v4l2_file_ops,*iris_v4l2_ioctl_ops_dec,*iris_v4l2_ioctl_ops_enc;int v4l2_dev;
 struct iris_platform_data *iris_platform_data;};
struct iris_inst {struct list_head list;struct iris_core *core;};
struct file {struct iris_core *core;};
static struct iris_core core;
static struct device dev={1};
static struct iris_platform_data platform={16};
static struct video_device video;
static bool self_work,interrupt_context,allocation_error;
static int quiesce_error,quiesce_calls,open_error,open_calls,video_register_error,video_register_calls,video_frees,core_frees,packet_frees;
static bool revoke_during_open;
static struct work_struct *current_work(void){return self_work?&core.sys_error_handler.work:NULL;}
static bool in_interrupt(void){return interrupt_context;}
static void put_device(struct device *d){assert(d==&dev&&d->refs==1);d->refs--;}
static void kfree(void *p){if(p==&core){core_frees++;}else{assert(p==core.response_packet);packet_frees++;}}
static struct iris_core *video_drvdata(struct file *f){return f->core;}
static struct video_device *video_device_alloc(void){return allocation_error?NULL:&video;}
static void video_set_drvdata(struct video_device *v,struct iris_core *c){v->data=c;}
static struct iris_core *video_get_drvdata(struct video_device *v){return v->data;}
static void video_device_release(struct video_device *v){assert(v==&video);video_frees++;}
static int video_register_device(struct video_device *v,int type,int number){assert(type==VFL_TYPE_VIDEO&&number==-1);assert(v->data==&core&&refs(&core.owner_refs)>=2);video_register_calls++;return video_register_error;}
static void strscpy(char *d,const char *s,size_t n){assert(strlen(s)<n);memcpy(d,s,strlen(s)+1);}
static int iris_core_quiesce(struct iris_core *c){assert(c==&core);assert(c->removing);assert(!c->open_inflight&&!c->open_owners&&!c->orphan_owners);assert(!pthread_mutex_trylock(&c->lock.lock));assert(!pthread_mutex_unlock(&c->lock.lock));quiesce_calls++;return quiesce_error;}
int iris_core_prepare_remove(struct iris_core *core);
static int iris_open_owned(struct file *f){assert(f->core==&core&&core.open_inflight==1&&refs(&core.owner_refs)>=2);open_calls++;if(revoke_during_open){assert(iris_core_prepare_remove(&core)==-EBUSY);return -ESHUTDOWN;}return open_error;}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));assert(!pthread_mutex_init(&core.remove_lock.lock,NULL));atomic_init(&core.owner_refs.value,1);core.dev=&dev;core.response_packet=&video;core.state=IRIS_CORE_INIT;core.iris_platform_data=&platform;INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);}
static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t ready=PTHREAD_COND_INITIALIZER;
static bool open_acquired,open_release;
static void *slow_open(void *unused){(void)unused;assert(!iris_core_open_get(&core));assert(!pthread_mutex_lock(&gate));open_acquired=true;assert(!pthread_cond_broadcast(&ready));while(!open_release)assert(!pthread_cond_wait(&ready,&gate));assert(!pthread_mutex_unlock(&gate));struct iris_inst fresh={.core=&core};assert(iris_add_session(&fresh)==-ESHUTDOWN);iris_core_open_finish(&core,false);return NULL;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();struct file f={&core};
 if(mode==0||mode==1){open_error=mode==1?-EIO:0;assert(iris_open(&f)==open_error);assert(open_calls==1&&!core.open_inflight);assert(core.open_owners==(mode==0));assert(refs(&core.owner_refs)==(mode==0?2:1));if(!mode)iris_core_open_put(&core);}
 if(mode>=2&&mode<=4){core.removing=mode==2;core.terminal_shutdown=mode==3;core.irq_quiescing=mode==4;assert(iris_open(&f)==-ESHUTDOWN);assert(!open_calls&&!core.open_inflight&&!core.open_owners&&refs(&core.owner_refs)==1);}
 if(mode==5){revoke_during_open=true;assert(iris_open(&f)==-ESHUTDOWN);assert(core.removing&&!core.open_inflight&&!core.open_owners&&!quiesce_calls&&refs(&core.owner_refs)==1);assert(!iris_core_prepare_remove(&core));assert(core.remove_prepared);}
 if(mode==6){assert(!iris_core_open_get(&core));iris_core_open_finish(&core,true);assert(iris_core_prepare_remove(&core)==-EBUSY);assert(core.removing&&!quiesce_calls);iris_core_open_put(&core);assert(!iris_core_prepare_remove(&core));}
 if(mode==7){assert(!iris_core_open_get(&core));iris_core_open_finish(&core,true);iris_core_orphan_transfer(&core);assert(!core.open_owners&&core.orphan_owners==1&&refs(&core.owner_refs)==2);assert(iris_core_prepare_remove(&core)==-EBUSY);iris_core_orphan_put(&core);assert(!core.orphan_owners&&refs(&core.owner_refs)==1);assert(!iris_core_prepare_remove(&core));}
 if(mode==8||mode==9){static struct iris_inst active;list_add_tail(&active.list,mode==8?&core.instances:&core.orphan_instances);assert(iris_core_prepare_remove(&core)==-EBUSY);assert(!quiesce_calls&&!core.remove_prepared);}
 if(mode==10){quiesce_error=-EACCES;assert(iris_core_prepare_remove(&core)==-EACCES);assert(core.removing&&!core.remove_prepared&&refs(&core.owner_refs)==1&&!core_frees);quiesce_error=0;assert(!iris_core_prepare_remove(&core)&&core.remove_prepared&&quiesce_calls==2);}
 if(mode==11){self_work=true;assert(iris_core_prepare_remove(&core)==-EDEADLK);assert(!core.removing&&!quiesce_calls);}
 if(mode==12){interrupt_context=true;assert(iris_core_prepare_remove(&core)==-EWOULDBLOCK);assert(!core.removing&&!quiesce_calls);}
 if(mode==13){pthread_t t;assert(!pthread_create(&t,NULL,slow_open,NULL));assert(!pthread_mutex_lock(&gate));while(!open_acquired)assert(!pthread_cond_wait(&ready,&gate));assert(!pthread_mutex_unlock(&gate));assert(iris_core_prepare_remove(&core)==-EBUSY);assert(!pthread_mutex_lock(&gate));open_release=true;assert(!pthread_cond_broadcast(&ready));assert(!pthread_mutex_unlock(&gate));assert(!pthread_join(t,NULL));assert(list_empty(&core.instances));assert(!iris_core_prepare_remove(&core));}
 if(mode>=14&&mode<=18){allocation_error=mode==14;video_register_error=mode==15?-EIO:0;int type=mode==16?7:mode==18?ENCODER:DECODER;int expected=mode==14?-ENOMEM:mode==15?-EIO:mode==16?-EINVAL:0;assert(iris_register_video_device(&core,type)==expected);assert(refs(&core.owner_refs)==(expected?1:2));if(!expected){assert(video.release==iris_video_device_release);iris_binding_release(&core);assert(!core_frees&&refs(&core.owner_refs)==1);video.release(&video);assert(core_frees==1&&packet_frees==1&&dev.refs==0&&locks_destroyed==4);return 0;}assert(video_frees==(mode==15||mode==16));}
 if(mode==19){iris_core_get(&core);iris_binding_release(&core);assert(!core_frees&&refs(&core.owner_refs)==1);iris_core_put(&core);assert(core_frees==1&&packet_frees==1&&dev.refs==0&&locks_destroyed==4);return 0;}
 if(mode==20){struct iris_inst fresh={.core=&core};core.removing=true;assert(iris_add_session(&fresh)==-ESHUTDOWN);assert(list_empty(&core.instances));}
 if(mode==21){struct iris_inst fresh={.core=&core};assert(!iris_add_session(&fresh));assert(!list_empty(&core.instances));assert(iris_core_prepare_remove(&core)==-EBUSY);}
 if(mode==22){assert(!iris_core_open_get(&core));assert(iris_core_prepare_remove(&core)==-EBUSY);iris_core_open_finish(&core,false);assert(!iris_core_prepare_remove(&core));assert(core.remove_prepared&&!core.open_inflight);}
 if(mode==23){iris_core_get(&core);assert(!iris_core_prepare_remove(&core));assert(!core_frees&&refs(&core.owner_refs)==2);iris_core_put(&core);assert(!core_frees&&refs(&core.owner_refs)==1);}
 assert(!core_frees&&!packet_frees&&dev.refs==1);return 0;}
'''


def source(work):
    core = (work / 'iris_core.c').read_text()
    probe = (work / 'iris_probe.c').read_text()
    vidc = (work / 'iris_vidc.c').read_text()
    names = ('iris_core_release', 'iris_core_get', 'iris_core_put', 'iris_core_open_get',
             'iris_core_open_finish', 'iris_core_open_put', 'iris_core_orphan_transfer',
             'iris_core_orphan_put', 'iris_core_prepare_remove')
    functions = ''.join(EXTRACT.definition(core, name) for name in names)
    functions += ''.join(EXTRACT.definition(probe, name) for name in
                         ('iris_video_device_release', 'iris_binding_release', 'iris_register_video_device'))
    functions += EXTRACT.definition(vidc, 'iris_open') + EXTRACT.definition(vidc, 'iris_add_session')
    prefix, main = PREFIX, MAIN
    if 'core->detach_lock' in core:
        prefix = prefix.replace('lock,lifecycle_lock,quiesce_lock,remove_lock;',
                                'lock,lifecycle_lock,quiesce_lock,remove_lock,detach_lock;')
        prefix = prefix.replace('bool removing,remove_prepared,terminal_shutdown,irq_quiescing;',
                                'bool removing,remove_prepared,terminal_shutdown,irq_quiescing;u32 video_owners;bool decoder_registered,encoder_registered;')
        main = main.replace('atomic_init(&core.owner_refs.value,1);',
                            'assert(!pthread_mutex_init(&core.detach_lock.lock,NULL));atomic_init(&core.owner_refs.value,1);')
        main = main.replace('locks_destroyed==4', 'locks_destroyed==5')
    if 'core->core_ready' in vidc:
        prefix = prefix.replace('bool removing,remove_prepared,terminal_shutdown,irq_quiescing;',
                                'bool removing,remove_prepared,terminal_shutdown,irq_quiescing,core_ready;')
        main = main.replace('core.state=IRIS_CORE_INIT;', 'core.state=IRIS_CORE_INIT;core.core_ready=true;')
    return prefix + functions + main


def compile_fixture(text, output, sanitize=False):
    src = output / 'owner.c'
    src.write_text(text)
    binary = output / 'owner'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class OwnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-owner-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-owner-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_open_revocation(self):
        self.mutation('if (core->removing || core->terminal_shutdown || core->irq_quiescing) {',
                      'if (core->terminal_shutdown || core->irq_quiescing) {', 2)

    def test_oracle_video_late_drvdata(self):
        self.mutation('video_set_drvdata(vdev, core);', 'video_set_drvdata(vdev, NULL);', 17)

    def test_oracle_prepare_ignores_shutdown(self):
        self.mutation('ret = iris_core_quiesce(core);', 'ret = iris_core_quiesce(core); ret = 0;', 10)

    def test_oracle_orphan_drops_cpu_owner(self):
        self.mutation('core->orphan_owners++;', 'core->orphan_owners++; iris_core_put(core);', 7)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(OwnerTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-owner-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(CASES):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
        print('%d CPU owner/video/pre-remove ASan/UBSan scenarios passed; no devres/DMA evidence' % CASES)
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OwnerTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
