#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual referenced getter/close and asynchronous handler in host pthread fixtures.

Real mutexes/completions model the response-versus-close ordering. Hardware,
V4L2 resources and firmware callbacks remain stubs; this is not a kernel race
proof or acceptance of firmware header/packet-id conventions.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('extract', HERE / 'test-decode-cleanup.py')
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
#include <time.h>
typedef uint32_t u32;
typedef pthread_mutex_t spinlock_t;
#define spin_lock_init(p) assert(!pthread_mutex_init((p),NULL))
#define spin_lock_irqsave(p,f) ((f)=0,assert(!pthread_mutex_lock(p)))
#define spin_unlock_irqrestore(p,f) ((void)(f),assert(!pthread_mutex_unlock(p)))
#define HFI_CMD_INIT 0x01000001
#define HFI_CMD_OPEN 0x01000003
#define HFI_CMD_START 0x01000005
#define HFI_FW_FLAGS_SUCCESS 1
#define HFI_PORT_BITSTREAM 1
#define HFI_PORT_RAW 2
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define IRIS_INST_DEINIT 0
#define IRIS_INST_INIT 1
#define IRIS_INST_ERROR 5
#define DECODER 1
#define ENCODER 2
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define msecs_to_jiffies(x) (x)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define dev_err(dev,...) ((void)(dev))
struct mutex {pthread_mutex_t value;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->value));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->value));}
static void mutex_destroy(struct mutex *m){assert(!pthread_mutex_destroy(&m->value));}
struct completion {pthread_mutex_t mutex;pthread_cond_t cond;int done;};
static struct completion wait_entered;
static void init_completion(struct completion *c){assert(!pthread_mutex_init(&c->mutex,NULL));assert(!pthread_cond_init(&c->cond,NULL));c->done=0;}
static void complete(struct completion *c){assert(!pthread_mutex_lock(&c->mutex));c->done=1;assert(!pthread_cond_broadcast(&c->cond));assert(!pthread_mutex_unlock(&c->mutex));}
static void wait_for_completion(struct completion *c){assert(!pthread_mutex_lock(&c->mutex));while(!c->done)assert(!pthread_cond_wait(&c->cond,&c->mutex));assert(!pthread_mutex_unlock(&c->mutex));}
static void reinit_completion(struct completion *c){assert(!pthread_mutex_lock(&c->mutex));c->done=0;assert(!pthread_mutex_unlock(&c->mutex));}
static unsigned long wait_for_completion_timeout(struct completion *c,unsigned long ms){
 complete(&wait_entered);
 struct timespec until;assert(!clock_gettime(CLOCK_REALTIME,&until));
 until.tv_sec+=ms/1000;until.tv_nsec+=(ms%1000)*1000000;
 if(until.tv_nsec>=1000000000){until.tv_sec++;until.tv_nsec-=1000000000;}
 assert(!pthread_mutex_lock(&c->mutex));int err=0;
 while(!c->done&&!err)err=pthread_cond_timedwait(&c->cond,&c->mutex,&until);
 assert(err==0||err==ETIMEDOUT);unsigned long done=c->done?1:0;
 assert(!pthread_mutex_unlock(&c->mutex));return done;
}
typedef struct {atomic_int count;} refcount_t;
static void refcount_inc(refcount_t *r){int old=atomic_fetch_add(&r->count,1);assert(old>0);}
static bool refcount_dec_and_test(refcount_t *r){int old=atomic_fetch_sub(&r->count,1);assert(old>0);return old==1;}
struct list_head {struct list_head *next,*prev;};
#define INIT_LIST_HEAD(h) ((h)->next=(h),(h)->prev=(h))
#define list_for_each_entry(p,h,m) for(struct list_head *_x=(h)->next;_x!=(h)&&((p)=container_of(_x,__typeof__(*(p)),m),1);_x=_x->next)
/* TRACKER */
struct iris_inst;
struct iris_hfi_command_ops {int (*session_close)(struct iris_inst *);};
struct iris_platform_data {u32 hw_response_timeout;};
struct iris_core {struct mutex lock;u32 state;struct list_head instances;struct iris_hfi_pending init_pending;struct completion core_init_done;int sys_error_handler;void *dev;const struct iris_hfi_command_ops *hfi_ops;struct iris_platform_data *iris_platform_data;};
struct vb2_queue {bool streaming;u32 type;};
struct v4l2_m2m_ctx {struct vb2_queue src,dst;};
struct iris_inst {struct iris_core *core;u32 session_id,state,domain;bool response_closing,response_error_notified;
 struct list_head list;struct mutex lock,ctx_q_lock;refcount_t response_refs;
 struct completion response_idle,completion,flush_completion;struct iris_hfi_pending control_pending;
 int ctrl_handler;void *m2m_ctx,*m2m_dev;};
struct iris_inst_hfi_gen2 {struct iris_inst inst;struct iris_hfi_pending open_pending,start_pending[2];
 struct completion open_done,start_done[2];int open_status,start_status[2];};
#define to_iris_inst_hfi_gen2(p) container_of(p,struct iris_inst_hfi_gen2,inst)
struct iris_hfi_packet {u32 type,port,packet_id,flags;};
struct file {struct iris_inst *inst;};
static atomic_int resources_destroyed,freed,queue_errors,stop_calls,close_calls,recovery_calls;
static u32 fail_stop_type;
static int fail_stop_callback,close_submit_error,close_reply_error,close_no_reply,close_wait_result;
static struct completion close_unlinked;
static struct vb2_queue *v4l2_m2m_get_src_vq(void *p){return &((struct v4l2_m2m_ctx *)p)->src;}
static struct vb2_queue *v4l2_m2m_get_dst_vq(void *p){return &((struct v4l2_m2m_ctx *)p)->dst;}
static bool vb2_is_streaming(struct vb2_queue *q){return q->streaming;}
static struct iris_inst *stop_instance;
static int vb2_streamoff(struct vb2_queue *q,u32 type){
 assert(!atomic_load(&close_calls));assert(q->type==type);atomic_fetch_add(&stop_calls,1);
 if(type==fail_stop_type)return -EIO;
 q->streaming=false;
 if(fail_stop_callback)stop_instance->state=IRIS_INST_ERROR;
 return 0;
}
static bool schedule_delayed_work(int *work,u32 delay){(void)work;assert(delay==10);atomic_fetch_add(&recovery_calls,1);return true;}
static struct iris_inst *iris_get_inst(struct file *f){return f->inst;}
static int close_submit(struct iris_inst *i){
 assert(!v4l2_m2m_get_src_vq(i->m2m_ctx)->streaming);assert(!v4l2_m2m_get_dst_vq(i->m2m_ctx)->streaming);
 atomic_fetch_add(&close_calls,1);
 if(close_submit_error)return close_submit_error;
 if(!close_no_reply){if(close_reply_error)i->state=IRIS_INST_ERROR;complete(&i->completion);}
 return 0;
}
int iris_wait_for_session_response(struct iris_inst *i,bool flush);
static void iris_inst_change_state(struct iris_inst *i,u32 s){i->state=s;}
static void iris_remove_session(struct iris_inst *i){mutex_lock(&i->core->lock);i->response_closing=true;i->list.prev->next=i->list.next;i->list.next->prev=i->list.prev;INIT_LIST_HEAD(&i->list);mutex_unlock(&i->core->lock);complete(&close_unlinked);}
static void v4l2_ctrl_handler_free(int *p){(void)p;atomic_fetch_add(&resources_destroyed,1);}
static void v4l2_m2m_ctx_release(void *p){assert(!v4l2_m2m_get_src_vq(p)->streaming);assert(!v4l2_m2m_get_dst_vq(p)->streaming);atomic_fetch_add(&resources_destroyed,1);}
static void v4l2_m2m_release(void *p){(void)p;atomic_fetch_add(&resources_destroyed,1);}
static void iris_vdec_inst_deinit(struct iris_inst *i){(void)i;atomic_fetch_add(&resources_destroyed,1);}
static void iris_venc_inst_deinit(struct iris_inst *i){(void)i;atomic_fetch_add(&resources_destroyed,1);}
static void iris_v4l2_fh_deinit(struct iris_inst *i,struct file *f){(void)i;(void)f;atomic_fetch_add(&resources_destroyed,1);}
static int iris_destroy_all_internal_buffers(struct iris_inst *i,u32 p){(void)i;(void)p;atomic_fetch_add(&resources_destroyed,1);return 0;}
static void iris_check_num_queued_internal_buffers(struct iris_inst *i,u32 p){(void)i;(void)p;}
static void iris_vb2_queue_error(struct iris_inst *i){(void)i;atomic_fetch_add(&queue_errors,1);}
static void kfree(void *p){struct iris_inst *i=p;assert(atomic_load(&i->response_refs.count)==0);atomic_store(&freed,1);}
void iris_put_response_instance(struct iris_inst *inst);
'''

MAIN = r'''
static void setup(struct iris_core *c,struct iris_inst_hfi_gen2 *g){
 memset(c,0,sizeof(*c));memset(g,0,sizeof(*g));
 assert(!pthread_mutex_init(&c->lock.value,NULL));INIT_LIST_HEAD(&c->instances);c->state=IRIS_CORE_INIT;
 static struct iris_hfi_command_ops ops={.session_close=close_submit};
 static struct iris_platform_data platform={.hw_response_timeout=20};
 c->hfi_ops=&ops;c->iris_platform_data=&platform;
 struct iris_inst *i=&g->inst;static struct v4l2_m2m_ctx m2m;
 m2m.src=(struct vb2_queue){.streaming=true,.type=9};m2m.dst=(struct vb2_queue){.streaming=true,.type=10};
 i->m2m_ctx=&m2m;i->core=c;i->session_id=9;i->domain=DECODER;i->state=IRIS_INST_INIT;
 stop_instance=i;
 assert(!pthread_mutex_init(&i->lock.value,NULL));assert(!pthread_mutex_init(&i->ctx_q_lock.value,NULL));
 atomic_init(&i->response_refs.count,1);init_completion(&i->response_idle);init_completion(&i->completion);init_completion(&i->flush_completion);
 init_completion(&c->core_init_done);init_completion(&close_unlinked);init_completion(&wait_entered);
 init_completion(&g->open_done);init_completion(&g->start_done[0]);init_completion(&g->start_done[1]);
 g->open_status=g->start_status[0]=g->start_status[1]=-EINPROGRESS;
 iris_hfi_pending_init(&g->open_pending);iris_hfi_pending_init(&g->start_pending[0]);iris_hfi_pending_init(&g->start_pending[1]);iris_hfi_pending_init(&c->init_pending);iris_hfi_pending_init(&i->control_pending);
 i->list.next=&c->instances;i->list.prev=&c->instances;c->instances.next=&i->list;c->instances.prev=&i->list;
}
static void *close_thread(void *arg){assert(!iris_close(arg));return NULL;}
static void *checked_close_thread(void *arg){
 struct iris_inst *i=arg;mutex_lock(&i->lock);close_wait_result=iris_session_close_checked(i);mutex_unlock(&i->lock);return NULL;
}
static void *system_wait_thread(void *arg){close_wait_result=iris_wait_for_system_response(arg);return NULL;}
int main(int argc,char **argv){
 assert(argc==2);int mode=atoi(argv[1]);static struct iris_core c;static struct iris_inst_hfi_gen2 g;setup(&c,&g);
 struct iris_inst *i=&g.inst;struct file f={i};
 if(mode<=3){
  struct iris_inst *held=iris_get_response_instance(&c,mode==1?10:9);
  if(mode==1){assert(!held);assert(atomic_load(&i->response_refs.count)==1);return 0;}
  assert(held==i&&atomic_load(&i->response_refs.count)==2);
  pthread_t t;assert(!pthread_create(&t,NULL,close_thread,&f));wait_for_completion(&close_unlinked);
  assert(!iris_get_response_instance(&c,9));
  assert(!atomic_load(&resources_destroyed));assert(!atomic_load(&freed));
  /* A response acquired before unlink can still finish, and cannot see freed resources. */
  mutex_lock(&held->lock);assert(held->response_closing);assert(!atomic_load(&resources_destroyed));mutex_unlock(&held->lock);
  iris_put_response_instance(held);assert(!pthread_join(t,NULL));
  assert(atomic_load(&freed)&&atomic_load(&resources_destroyed)==7);assert(atomic_load(&stop_calls)==2);
 } else if(mode>=4&&mode<=10){
  struct iris_hfi_packet p={.type=mode>=8?HFI_CMD_START:HFI_CMD_OPEN,.packet_id=7,.port=mode>=8?HFI_PORT_RAW:0,.flags=1};
  struct iris_hfi_pending *pending=mode>=8?&g.start_pending[1]:&g.open_pending;
  assert(!iris_hfi_pending_begin(pending,7,p.type,p.port));
  if(mode==5||mode==9)p.flags=0;
  if(mode==6)p.packet_id=8;
  if(mode==7)iris_hfi_pending_cancel(pending);
  int r=iris_hfi_gen2_handle_session_async(i,&p);
  assert(r==(mode==5||mode==9?-EIO:0));
  assert(i->state==(mode==5||mode==9?IRIS_INST_ERROR:IRIS_INST_INIT));
  assert(atomic_load(&queue_errors)==(mode==5||mode==9));
  if(mode==10){assert(!iris_hfi_gen2_handle_session_async(i,&p));assert(!atomic_load(&queue_errors));}
 } else if(mode==11){
  struct iris_hfi_packet p={.type=HFI_CMD_INIT,.packet_id=7,.flags=1};
  assert(!iris_hfi_pending_begin(&c.init_pending,7,HFI_CMD_INIT,0));
  assert(!iris_hfi_gen2_handle_system_init(&c,&p));assert(c.core_init_done.done==1);
  mutex_lock(&c.lock);iris_hfi_pending_cancel(&c.init_pending);c.state=IRIS_CORE_ERROR;mutex_unlock(&c.lock);
  assert(iris_hfi_gen2_handle_system_init(&c,&p)==-EINVAL);
 } else if(mode==12||mode==13){
  static struct iris_inst_hfi_gen2 many[300];
  for(int j=0;j<300;j++) {
   struct iris_inst *x=&many[j].inst;memset(x,0,sizeof(*x));x->core=&c;x->session_id=j+20;
   assert(!pthread_mutex_init(&x->lock.value,NULL));atomic_init(&x->response_refs.count,1);
   init_completion(&x->completion);init_completion(&x->flush_completion);init_completion(&x->response_idle);
   x->list.prev=c.instances.prev;x->list.next=&c.instances;c.instances.prev->next=&x->list;c.instances.prev=&x->list;
  }
  assert(!iris_hfi_gen2_handle_system_error(&c,NULL));assert(c.state==IRIS_CORE_ERROR);
  assert(c.core_init_done.done&&atomic_load(&recovery_calls)==1);
  for(int j=0;j<300;j++){struct iris_inst *x=&many[j].inst;assert(x->state==IRIS_INST_ERROR&&x->completion.done&&x->flush_completion.done);assert(atomic_load(&x->response_refs.count)==1);}
  if(mode==13){assert(!iris_hfi_gen2_handle_system_error(&c,NULL));assert(atomic_load(&recovery_calls)==2);}
 } else if(mode>=14&&mode<=19){
  if(mode==14)fail_stop_type=9;
  if(mode==15)fail_stop_type=10;
  if(mode==16)close_submit_error=-EIO;
  if(mode==17)close_no_reply=1;
  if(mode==18)close_reply_error=1;
  if(mode==19)fail_stop_callback=1;
  int r=iris_close(&f);assert(r==(mode==17?-ETIMEDOUT:-EIO));
  assert(!atomic_load(&freed)&&!atomic_load(&resources_destroyed));
  assert(!i->response_closing&&atomic_load(&i->response_refs.count)==1);
  assert(i->state==IRIS_INST_ERROR);
  assert(atomic_load(&close_calls)==(mode>=16&&mode<=18?1:0));
 } else if(mode==20||mode==21){
  v4l2_m2m_get_src_vq(i->m2m_ctx)->streaming=false;v4l2_m2m_get_dst_vq(i->m2m_ctx)->streaming=false;
  close_no_reply=1;pthread_t t;assert(!pthread_create(&t,NULL,checked_close_thread,i));wait_for_completion(&wait_entered);
  if(mode==20){mutex_lock(&i->lock);complete(&i->completion);mutex_unlock(&i->lock);}
  assert(!pthread_join(t,NULL));assert(close_wait_result==(mode==20?0:-ETIMEDOUT));
  if(mode==21){complete(&i->completion);assert(i->state==IRIS_INST_ERROR);}
 } else if(mode==22||mode==23){
  assert(!iris_hfi_pending_begin(&c.init_pending,7,HFI_CMD_INIT,0));
  pthread_t t;assert(!pthread_create(&t,NULL,system_wait_thread,&c));wait_for_completion(&wait_entered);
  struct iris_hfi_packet p={.type=HFI_CMD_INIT,.packet_id=7,.flags=1};
  if(mode==22)assert(!iris_hfi_gen2_handle_system_init(&c,&p));
  assert(!pthread_join(t,NULL));assert(close_wait_result==(mode==22?0:-ETIMEDOUT));
  if(mode==23){assert(iris_hfi_gen2_handle_system_init(&c,&p)==-EINVAL);assert(!c.core_init_done.done);}
 } else return 2;
 return 0;
}
'''


def fixture(work, output, sanitize=False):
    common = (work / 'iris_hfi_common.h').read_text()
    start = common.index('struct iris_hfi_pending {')
    tracker = common[start:common.index('enum hfi_packet_port_type {', start)]
    utils = (work / 'iris_utils.c').read_text()
    # Getter returns a pointer; reuse brace parser directly with a scoped pattern.
    getter = EXTRACT.block(utils, r'^struct iris_inst \*iris_get_response_instance\([^;{}]*\)\s*\{', 'response getter')
    functions = getter + EXTRACT.definition(utils, 'iris_put_response_instance')
    functions += EXTRACT.definition(utils, 'iris_wait_for_session_response')
    functions += EXTRACT.definition((work / 'iris_core.c').read_text(), 'iris_wait_for_system_response')
    functions += EXTRACT.definition((work / 'iris_vidc.c').read_text(), 'iris_session_close_checked')
    functions += EXTRACT.definition((work / 'iris_vidc.c').read_text(), 'iris_close')
    response = (work / 'iris_hfi_gen2_response.c').read_text()
    if 'static void iris_hfi_gen2_fail_async(' in response:
        functions += EXTRACT.definition(response, 'iris_hfi_gen2_fail_async')
    functions += EXTRACT.definition(response, 'iris_hfi_gen2_handle_session_async')
    functions += EXTRACT.definition(response, 'iris_hfi_gen2_handle_system_init')
    functions += EXTRACT.definition(response, 'iris_hfi_gen2_handle_system_error')
    prefix, main = PREFIX, MAIN
    vidc = (work / 'iris_vidc.c').read_text()
    if 'inst->file_detached = true;' in vidc:
        prefix = prefix.replace('struct mutex lock;u32 state;', 'struct mutex lock,lifecycle_lock;u32 state;bool terminal_shutdown,recovery_blocked;struct list_head orphan_instances;u32 open_owners,orphan_owners;')
        prefix = prefix.replace('bool response_closing,response_error_notified;', 'bool response_closing,response_error_notified,file_detached,orphaned;')
        prefix = prefix.replace('/* TRACKER */', '''
struct device {int refs;};static struct device device;
static struct device *get_device(void *p){assert(p==&device);device.refs++;return p;}
static void list_add_tail(struct list_head *n,struct list_head *h){n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;}
/* TRACKER */''')
        main = main.replace('c->state=IRIS_CORE_INIT;', 'c->state=IRIS_CORE_INIT;c->dev=&device;INIT_LIST_HEAD(&c->orphan_instances);assert(!pthread_mutex_init(&c->lifecycle_lock.value,NULL));c->open_owners=1;', 1)
        main = main.replace('assert(!atomic_load(&freed)&&!atomic_load(&resources_destroyed));\n  assert(!i->response_closing&&atomic_load(&i->response_refs.count)==1);',
            'assert(!atomic_load(&freed)&&atomic_load(&resources_destroyed)==1);\n  assert(i->response_closing&&i->file_detached&&i->orphaned&&atomic_load(&i->response_refs.count)==0);')
    if 'iris_core_open_put(core);' in vidc:
        prefix += '''
static void iris_core_open_put(struct iris_core *c){assert(c->open_owners==1);c->open_owners--;}
static void iris_core_orphan_transfer(struct iris_core *c){assert(c->open_owners==1);c->open_owners--;c->orphan_owners++;}
'''
    if 'core->init_acknowledged' in response:
        prefix = prefix.replace('struct mutex lock;u32 state;', 'struct mutex lock;u32 state;bool init_acknowledged,core_ready;')
        prefix = prefix.replace('struct mutex lock,lifecycle_lock;u32 state;', 'struct mutex lock,lifecycle_lock;u32 state;bool init_acknowledged,core_ready;')
    source = output / 'lifetime.c'
    source.write_text(prefix.replace('/* TRACKER */', tracker) + functions + main)
    binary = output / 'lifetime'
    cmd = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(source), '-o', str(binary)]
    if sanitize:
        cmd.extend(['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie'])
    r = subprocess.run(cmd,capture_output=True,text=True,timeout=40)
    if r.returncode:
        raise RuntimeError(r.stdout+r.stderr)
    return binary


class LifetimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-lifetime-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = fixture(WORK,Path(cls.temp.name))

    def run_case(self,mode):
        r = subprocess.run([str(self.binary),str(mode)],capture_output=True,text=True,timeout=5)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def mutation_rejected(self, before, after, mode):
        source = (Path(self.temp.name) / 'lifetime.c').read_text()
        self.assertEqual(source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-lifetime-mutation-') as directory:
            p = Path(directory)
            target, binary = p / 'mutant.c', p / 'mutant'
            target.write_text(source.replace(before, after, 1))
            r = subprocess.run(['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror',
                                '-O1', '-pthread', str(target), '-o', str(binary)],
                               capture_output=True, text=True, timeout=40)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            r = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Assertion', r.stderr)

    def test_oracle_rejects_missing_response_reference(self):
        self.mutation_rejected('refcount_inc(&inst->response_refs);\n\t\t\tresult = inst;',
                               '(void)&inst->response_refs;\n\t\t\tresult = inst;', 0)

    def test_oracle_rejects_close_before_stop(self):
        self.mutation_rejected('err = vb2_streamoff(src, src->type);', 'err = 0;', 0)

    def test_oracle_rejects_missing_async_failure(self):
        source = (Path(self.temp.name) / 'lifetime.c').read_text()
        if '*status = pkt->flags & HFI_FW_FLAGS_SUCCESS ? 0 : -EIO;' in source:
            self.mutation_rejected('*status = pkt->flags & HFI_FW_FLAGS_SUCCESS ? 0 : -EIO;',
                                   '*status = 0;', 5)
        else:
            self.mutation_rejected('if (pkt->flags & HFI_FW_FLAGS_SUCCESS)\n\t\treturn 0;',
                                   'if (pkt->flags & HFI_FW_FLAGS_SUCCESS || true)\n\t\treturn 0;', 5)

    def test_oracle_rejects_unchecked_streamoff_failure(self):
        self.mutation_rejected('if (ret || inst->state == IRIS_INST_ERROR)',
                               'if (false)', 14)

    def test_oracle_rejects_unchecked_close_failure(self):
        self.mutation_rejected('ret = iris_session_close_checked(inst);\n\tif (ret)\n\t\tgoto uncertain;',
                               'ret = iris_session_close_checked(inst);\n\tif (ret)\n\t\tret = 0;', 16)


for mode in range(24):
    def test(self,value=mode):
        self.run_case(value)
    setattr(LifetimeTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work',required=True,type=Path)
    parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args();WORK=args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-lifetime-sanitizer-') as d:
            binary=fixture(WORK,Path(d),True)
            for mode in range(24):
                r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5,
                    env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode:
                    print(r.stdout+r.stderr);return 1
            print('24 pthread lifetime/async-error ASan/UBSan cases passed; fixture synchronization-object leaks excluded')
            return 0
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(LifetimeTests))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':
    raise SystemExit(main())
