#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual OPEN/START wait, ACK matching and checked shutdown in host pthreads.

This is a private-candidate test requiring --work. Firmware echo conventions,
secure-world shutdown meaning and final uncertain file-release reclaim remain
unverified. It does not load modules or access a device.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('actual_c_extract', HERE / 'test-decode-cleanup.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None

PREFIX = r'''
#define _POSIX_C_SOURCE 200809L
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <pthread.h>
#include <stdatomic.h>
#include <time.h>
typedef uint8_t u8; typedef uint32_t u32; typedef uint64_t u64;
#define BIT(n) (1u<<(n))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define U32_MAX UINT32_MAX
#define GFP_KERNEL 0
#define DECODER 1
#define ENCODER 2
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define msecs_to_jiffies(x) (x)
typedef pthread_mutex_t spinlock_t;
#define spin_lock_init(p) assert(!pthread_mutex_init((p),NULL))
#define spin_lock_irqsave(p,f) ((f)=0,assert(!pthread_mutex_lock(p)))
#define spin_unlock_irqrestore(p,f) ((void)(f),assert(!pthread_mutex_unlock(p)))
struct mutex {pthread_mutex_t value;};
static void mutex_lock(struct mutex *m){assert(!pthread_mutex_lock(&m->value));}
static void mutex_unlock(struct mutex *m){assert(!pthread_mutex_unlock(&m->value));}
struct completion {pthread_mutex_t lock;pthread_cond_t cond;u32 done,calls;};
static void init_completion(struct completion *c){assert(!pthread_mutex_init(&c->lock,NULL));assert(!pthread_cond_init(&c->cond,NULL));c->done=c->calls=0;}
static void complete(struct completion *c){assert(!pthread_mutex_lock(&c->lock));c->done=1;c->calls++;assert(!pthread_cond_broadcast(&c->cond));assert(!pthread_mutex_unlock(&c->lock));}
static void reinit_completion(struct completion *c){assert(!pthread_mutex_lock(&c->lock));c->done=0;assert(!pthread_mutex_unlock(&c->lock));}
static unsigned long wait_for_completion_timeout(struct completion *c,unsigned long ms){
 struct timespec end;assert(!clock_gettime(CLOCK_REALTIME,&end));end.tv_sec+=ms/1000;end.tv_nsec+=(ms%1000)*1000000;
 if(end.tv_nsec>=1000000000){end.tv_sec++;end.tv_nsec-=1000000000;}
 assert(!pthread_mutex_lock(&c->lock));int err=0;
 while(!c->done&&!err)err=pthread_cond_timedwait(&c->cond,&c->lock,&end);
 assert(!err||err==ETIMEDOUT);unsigned long done=c->done?1:0;
 assert(!pthread_mutex_unlock(&c->lock));return done;
}
static void wait_for_completion(struct completion *c){assert(!pthread_mutex_lock(&c->lock));while(!c->done)assert(!pthread_cond_wait(&c->cond,&c->lock));assert(!pthread_mutex_unlock(&c->lock));}
/* DEFINITIONS */
struct iris_platform_data {u32 hw_response_timeout;};
struct iris_core {struct iris_platform_data *iris_platform_data;struct mutex lock;u32 state,header_id,packet_id;void *dev;};
struct iris_inst_hfi_gen2 {struct iris_hfi_header *packet;bool ipsc_properties_set,opsc_properties_set;
 struct iris_hfi_pending open_pending,start_pending[2];struct completion open_done,start_done[2];int open_status,start_status[2];};
struct iris_inst {struct iris_core *core;struct iris_inst_hfi_gen2 *hfi;struct mutex lock;
 u32 state,domain,session_id;bool response_closing;struct completion completion,flush_completion;};
#define to_iris_inst_hfi_gen2(p) ((p)->hfi)
static int live_heap,queue_error,pm_error,pm_success_value,shutdown_error,pm_refs,unloads,poweroffs,queue_deinits,codec_calls,default_calls;
static atomic_int queue_errors;
static struct iris_inst *active_inst;
static struct completion request_ready;
static struct iris_hfi_packet request;
static int reply_mode;
static void *kzalloc(size_t n,int flags){(void)flags;void *p=calloc(1,n);assert(p);live_heap++;return p;}
static void kfree(void *p){assert(p);live_heap--;free(p);}
static void iris_inst_change_state(struct iris_inst *i,u32 state){i->state=state;}
static void iris_vb2_queue_error(struct iris_inst *i){(void)i;atomic_fetch_add(&queue_errors,1);}
static u32 iris_hfi_gen2_get_port(struct iris_inst *i,u32 plane){(void)i;return plane==9?HFI_PORT_BITSTREAM:plane==10?HFI_PORT_RAW:HFI_PORT_NONE;}
static int iris_hfi_gen2_subscribe_change_param(struct iris_inst *i,u32 plane){(void)i;(void)plane;return 0;}
static int iris_hfi_gen2_subscribe_property(struct iris_inst *i,u32 plane){(void)i;(void)plane;return 0;}
static int iris_hfi_gen2_session_set_codec(struct iris_inst *i){(void)i;codec_calls++;return 0;}
static int iris_hfi_gen2_session_set_default_header(struct iris_inst *i){(void)i;default_calls++;return 0;}
static void iris_hfi_gen2_packet_session_command(struct iris_inst *i,u32 type,u32 flags,u32 port,u32 session,u32 payload_type,void *payload,u32 length){
 struct iris_hfi_header *h=i->hfi->packet;memset(h,0,4096);h->size=sizeof(*h)+sizeof(struct iris_hfi_packet)+length;
 h->session_id=session;h->header_id=i->core->header_id++;h->num_packets=1;
 struct iris_hfi_packet *p=(void *)((u8 *)h+sizeof(*h));p->size=sizeof(*p)+length;p->type=type;p->flags=flags;
 p->port=port;p->packet_id=i->core->packet_id++;p->payload_info=payload_type;
 if(length)memcpy(p->payload,payload,length);
}
static int iris_hfi_queue_cmd_write(struct iris_core *c,void *packet,u32 size){
 (void)c;struct iris_hfi_header *h=packet;assert(size==h->size);struct iris_hfi_packet *p=(void *)((u8 *)h+sizeof(*h));
 if(queue_error)return queue_error;
 request=*p;complete(&request_ready);return 0;
}
static int pm_runtime_resume_and_get(void *dev){(void)dev;if(pm_error)return pm_error;pm_refs++;return pm_success_value;}
static void pm_runtime_put_sync(void *dev){(void)dev;assert(pm_refs>0);pm_refs--;}
static int iris_fw_unload(struct iris_core *c){(void)c;unloads++;return shutdown_error;}
static void iris_vpu_power_off(struct iris_core *c){(void)c;poweroffs++;}
static void iris_hfi_queues_deinit(struct iris_core *c){(void)c;queue_deinits++;}
'''

MAIN = r'''
static void setup(struct iris_core *c,struct iris_inst *i,struct iris_inst_hfi_gen2 *g){
 static struct iris_platform_data p={.hw_response_timeout=40};memset(c,0,sizeof(*c));memset(i,0,sizeof(*i));memset(g,0,sizeof(*g));
 c->iris_platform_data=&p;c->state=IRIS_CORE_INIT;assert(!pthread_mutex_init(&c->lock.value,NULL));
 i->core=c;i->hfi=g;i->session_id=9;i->domain=DECODER;assert(!pthread_mutex_init(&i->lock.value,NULL));
 init_completion(&request_ready);init_completion(&i->completion);init_completion(&i->flush_completion);
 init_completion(&g->open_done);init_completion(&g->start_done[0]);init_completion(&g->start_done[1]);
 iris_hfi_pending_init(&g->open_pending);iris_hfi_pending_init(&g->start_pending[0]);iris_hfi_pending_init(&g->start_pending[1]);active_inst=i;
}
static void *respond(void *unused){
 (void)unused;wait_for_completion(&request_ready);struct iris_inst *i=active_inst;mutex_lock(&i->lock);
 struct iris_hfi_packet p=request;p.flags=reply_mode==1?0:HFI_FW_FLAGS_SUCCESS;
 if(reply_mode==2)p.packet_id++;
 if(reply_mode==3){struct iris_inst_hfi_gen2 *g=i->hfi;complete(p.type==HFI_CMD_OPEN?&g->open_done:&g->start_done[p.port==HFI_PORT_RAW]);}
 else if(reply_mode==4) {iris_inst_change_state(i,IRIS_INST_ERROR);iris_hfi_gen2_fail_async(i);}
 else {int ret=iris_hfi_gen2_handle_session_async(i,&p);assert(ret==(reply_mode==1?-EIO:0));
  if(reply_mode==5){assert(!iris_hfi_gen2_handle_session_async(i,&p));}}
 mutex_unlock(&i->lock);return NULL;
}
int main(int argc,char **argv){
 assert(argc==2);int mode=atoi(argv[1]);static struct iris_core c;static struct iris_inst i;static struct iris_inst_hfi_gen2 g;setup(&c,&i,&g);
 if(mode>=20){
  if(mode==20)pm_error=-EIO;
  if(mode==21)shutdown_error=-EACCES;
  if(mode==23||mode==25)c.state=IRIS_CORE_DEINIT;
  if(mode==25)pm_success_value=1;
  int r=iris_core_deinit_checked(&c);
  assert(r==(mode==20?-EIO:mode==21?-EACCES:0));assert(pm_refs==0);
  assert(unloads==(mode==20||mode==23||mode==25?0:1));assert(poweroffs==(mode==20||mode==21||mode==23||mode==25?0:1));assert(queue_deinits==poweroffs);
  assert(c.state==(mode==20?IRIS_CORE_INIT:mode==21?IRIS_CORE_ERROR:IRIS_CORE_DEINIT));
  if(mode==24){assert(!iris_core_deinit_checked(&c));assert(unloads==1&&poweroffs==1&&queue_deinits==1);}
  return 0;
 }
 bool opening=mode==0||mode==3||mode==5||mode==8||mode==15;
 bool missing=mode==5||mode==6||mode==10;
 if(mode==3||mode==4)reply_mode=1;
 if(mode==7)reply_mode=2;
 if(mode==12)reply_mode=5;
 if(mode==13)reply_mode=3;
 if(mode==14)reply_mode=4;
 if(mode==8||mode==9)queue_error=-ENOSPC;
 if(mode==15)c.packet_id=U32_MAX;
 if(!opening){i.state=IRIS_INST_INIT;g.packet=kzalloc(4096,GFP_KERNEL);}
 bool thread=!missing&&!queue_error&&mode!=15&&mode!=16&&mode!=17&&mode!=18&&mode!=19;
 if(mode==18){i.state=IRIS_INST_ERROR;}
 if(mode==19){kfree(g.packet);g.packet=NULL;}
 if(mode==17){iris_hfi_pending_begin(&g.start_pending[0],1,HFI_CMD_START,HFI_PORT_BITSTREAM);}
 pthread_t t;if(thread)assert(!pthread_create(&t,NULL,respond,NULL));
 mutex_lock(&i.lock);int r=opening?iris_hfi_gen2_session_open(&i):iris_hfi_gen2_session_start(&i,mode==2?10:mode==16?12:9);
 mutex_unlock(&i.lock);if(thread)assert(!pthread_join(t,NULL));
 int expected=(mode==3||mode==4||mode==14)?-EIO:(missing||mode==7)?-ETIMEDOUT:queue_error?queue_error:mode==15?-EOVERFLOW:mode==16||mode==19?-EINVAL:mode==17?-EBUSY:mode==18?-EIO:mode==13?-EPROTO:0;
 assert(r==expected);
 if(opening){assert(!iris_hfi_pending_active(&g.open_pending));assert((g.packet!=NULL)==(r==0));}
 else if(mode!=17)assert(!iris_hfi_pending_active(&g.start_pending[mode==2]));
 if(mode==6||mode==10||mode==7){assert(i.state==IRIS_INST_ERROR);struct iris_hfi_packet late=request;late.flags=HFI_FW_FLAGS_SUCCESS;
  mutex_lock(&i.lock);assert(!iris_hfi_gen2_handle_session_async(&i,&late));assert(i.state==IRIS_INST_ERROR);assert(g.start_done[0].calls==0);
  assert(iris_hfi_gen2_session_start(&i,9)==-EIO);mutex_unlock(&i.lock);}
 if(mode==11){reinit_completion(&request_ready);assert(!pthread_create(&t,NULL,respond,NULL));mutex_lock(&i.lock);assert(!iris_hfi_gen2_session_start(&i,9));mutex_unlock(&i.lock);assert(!pthread_join(t,NULL));assert(g.start_done[0].calls==2);}
 if(mode==12)assert(g.start_done[0].calls==1);
 if(g.packet){kfree(g.packet);g.packet=NULL;}assert(live_heap==0);return 0;
}
'''


def fixture(work, directory, sanitize=False):
    common=(work/'iris_hfi_common.h').read_text();start=common.index('struct iris_hfi_pending {')
    tracker=common[start:common.index('enum hfi_packet_port_type {',start)]
    defs=tracker
    for name in ('hfi_packet_port_type','hfi_packet_payload_info','hfi_packet_host_flags'):
        defs+=EXTRACT.aggregate(common,name)
    for name in ('iris_hfi_header','iris_hfi_packet'):
        defs+=EXTRACT.aggregate((work/'iris_hfi_gen2_packet.h').read_text(),name)
    defs+=EXTRACT.aggregate((work/'iris_state.h').read_text(),'iris_inst_state')
    defs+=EXTRACT.aggregate((work/'iris_state.h').read_text(),'iris_core_state')
    defines=(work/'iris_hfi_gen2_defines.h').read_text()
    defs+=''.join(line+'\n' for line in defines.splitlines() if re.match(r'#define HFI_CMD_(OPEN|START)\s',line))
    defs+=EXTRACT.aggregate(defines,'hfi_packet_firmware_flags')
    cmd=(work/'iris_hfi_gen2_command.c').read_text();resp=(work/'iris_hfi_gen2_response.c').read_text()
    funcs=''.join(EXTRACT.definition(cmd,name) for name in ('iris_hfi_gen2_wait_async','iris_hfi_gen2_session_open','iris_hfi_gen2_session_start'))
    funcs+=EXTRACT.definition(resp,'iris_hfi_gen2_fail_async')+EXTRACT.definition(resp,'iris_hfi_gen2_handle_session_async')
    funcs+=EXTRACT.definition((work/'iris_core.c').read_text(),'iris_core_deinit_checked')
    core = (work/'iris_core.c').read_text()
    prefix, main = PREFIX, MAIN
    if 'iris_vpu_has_checked_power' in core:
        prefix += '''
static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}
static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
'''

    if 'core->lifecycle_lock' in EXTRACT.definition(core, 'iris_core_deinit_checked'):
        prefix = prefix.replace('struct mutex lock;u32 state,header_id,packet_id;',
            'struct mutex lock,lifecycle_lock;u32 state,header_id,packet_id;bool terminal_shutdown,firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified,queues_allocated,power_prepared;')
        prefix = prefix.replace('#define GFP_KERNEL 0', '#define GFP_KERNEL 0\n#define READ_ONCE(x) (x)')
        main = main.replace('i->core=c;', 'assert(!pthread_mutex_init(&c->lifecycle_lock.value,NULL));i->core=c;', 1)
        main = main.replace('mode==21?-EACCES:0', 'mode==21?-EACCES:(mode==22||mode==24)?-EAGAIN:0')
        main = main.replace('assert(poweroffs==(mode==20||mode==21||mode==23||mode==25?0:1));', 'assert(!poweroffs);')
        main = main.replace('mode==21?IRIS_CORE_ERROR:IRIS_CORE_DEINIT', '(mode==21||mode==22||mode==24)?IRIS_CORE_ERROR:IRIS_CORE_DEINIT')
        main = main.replace('assert(!iris_core_deinit_checked(&c));assert(unloads==1&&poweroffs==1&&queue_deinits==1);',
            'assert(iris_core_deinit_checked(&c)==-EAGAIN);assert(unloads==2&&!poweroffs&&!queue_deinits);')
    if 'core->irq_managed' in core:
        prefix = prefix.replace('bool terminal_shutdown,', 'bool irq_managed,core_ready,terminal_shutdown,')
    source=directory/'ack.c';source.write_text(prefix.replace('/* DEFINITIONS */',defs)+funcs+main)
    binary=directory/'ack';args=['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-pthread',str(source),'-o',str(binary)]
    if sanitize:args+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
    r=subprocess.run(args,capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    return binary


CASES=list(range(26))
class AckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='iris-ack-tests-');cls.addClassCleanup(cls.temp.cleanup);cls.binary=fixture(WORK,Path(cls.temp.name))
    def run_case(self,mode):
        r=subprocess.run([str(self.binary),str(mode)],capture_output=True,text=True,timeout=5)
        self.assertEqual(r.returncode,0,r.stdout+r.stderr)

    def mutation_rejected(self, before, after, mode):
        source=(Path(self.temp.name)/'ack.c').read_text()
        self.assertEqual(source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-ack-mutation-') as directory:
            path=Path(directory);target=path/'mutant.c';binary=path/'mutant'
            target.write_text(source.replace(before,after,1))
            r=subprocess.run(['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1',
                              '-pthread',str(target),'-o',str(binary)],capture_output=True,text=True,timeout=40)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(r.returncode,0)
            self.assertIn('Assertion',r.stderr)

    def test_oracle_rejects_missing_timeout_failure(self):
        self.mutation_rejected('if (!completed) {','if (completed == 2) {',6)

    def test_oracle_rejects_ignored_shutdown_result(self):
        self.mutation_rejected('ret = iris_fw_unload(core);','ret = iris_fw_unload(core); ret = 0;',21)

    def test_oracle_rejects_ack_type_mismatch(self):
        self.mutation_rejected('pending->packet_id == packet_id',
                               '(pending->packet_id == packet_id || true)',7)

    def test_oracle_rejects_missing_pending_retirement(self):
        self.mutation_rejected('iris_hfi_pending_cancel(pending);\n\tif (!completed)',
                               '(void)pending;\n\tif (!completed)',11)
for case in CASES:
    def test(self,mode=case):self.run_case(mode)
    setattr(AckTests,'test_case_%02d'%case,test)


def main():
    global WORK
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--work',required=True,type=Path);p.add_argument('--sanitize',action='store_true');a=p.parse_args();WORK=a.work
    if a.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-ack-sanitizer-') as d:
            binary=fixture(WORK,Path(d),True)
            for mode in CASES:
                r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode:print(r.stdout+r.stderr);return 1
            print('26 real OPEN/START/shutdown ASan/UBSan cases passed; synchronization-object leak checks excluded');return 0
    r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AckTests));return 0 if r.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
