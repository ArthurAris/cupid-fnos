#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual file-detached quarantine and reclaim gate with synthetic proof inputs.

True proof bits are supplied only by fixtures to test the gate. The actual
IRQ/recovery/firmware proof producers are not exercised by this host fixture;
no runtime reclamation acceptance or device access is claimed.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


LIFETIME = load('lifetime_host_fixture', HERE / 'test-response-lifetime.py')
EXTRACT = LIFETIME.EXTRACT
WORK = None

MAIN = r'''
static struct iris_core c;
static struct iris_inst_hfi_gen2 g;
static struct v4l2_m2m_ctx m2m;
static int close_result;
static void setup(void){
 memset(&c,0,sizeof(c));memset(&g,0,sizeof(g));memset(&m2m,0,sizeof(m2m));
 assert(!pthread_mutex_init(&c.lock.value,NULL));assert(!pthread_mutex_init(&c.lifecycle_lock.value,NULL));
 INIT_LIST_HEAD(&c.instances);INIT_LIST_HEAD(&c.orphan_instances);c.state=IRIS_CORE_INIT;
 static struct iris_hfi_command_ops ops={.session_close=close_submit};
 static struct iris_platform_data platform={.hw_response_timeout=20};
 c.hfi_ops=&ops;c.iris_platform_data=&platform;c.dev=&host_device;
 struct iris_inst *i=&g.inst;i->core=&c;i->session_id=9;i->domain=DECODER;i->state=IRIS_INST_INIT;
 m2m.src=(struct vb2_queue){.streaming=true,.type=9};m2m.dst=(struct vb2_queue){.streaming=true,.type=10};
 i->m2m_ctx=&m2m;stop_instance=i;
 assert(!pthread_mutex_init(&i->lock.value,NULL));assert(!pthread_mutex_init(&i->ctx_q_lock.value,NULL));
 atomic_init(&i->response_refs.count,1);init_completion(&i->response_idle);init_completion(&i->completion);init_completion(&i->flush_completion);
 init_completion(&c.core_init_done);init_completion(&close_unlinked);init_completion(&wait_entered);
 init_completion(&g.open_done);init_completion(&g.start_done[0]);init_completion(&g.start_done[1]);
 iris_hfi_pending_init(&g.open_pending);iris_hfi_pending_init(&g.start_pending[0]);iris_hfi_pending_init(&g.start_pending[1]);iris_hfi_pending_init(&c.init_pending);iris_hfi_pending_init(&i->control_pending);
 list_add_tail(&i->list,&c.instances);
}
static void *quarantine_thread(void *arg){close_result=iris_close(arg);return NULL;}
int main(int argc,char **argv){
 assert(argc==2);int mode=atoi(argv[1]);setup();struct iris_inst *i=&g.inst;struct file f={.inst=i};
 if(mode<=3){
  fail_stop_type=9;struct iris_inst *held=iris_get_response_instance(&c,9);assert(held==i);
  pthread_t thread;assert(!pthread_create(&thread,NULL,quarantine_thread,&f));wait_for_completion(&close_unlinked);
  assert(!iris_get_response_instance(&c,9));assert(!i->file_detached);assert(!f.detached);
  assert(!atomic_load(&resources_destroyed));assert(!atomic_load(&freed));
  iris_put_response_instance(held);assert(!pthread_join(thread,NULL));assert(close_result==-EIO);
  assert(i->response_closing&&i->file_detached&&i->orphaned&&f.detached);
  assert(atomic_load(&i->response_refs.count)==0);assert(list_empty(&c.instances));assert(!list_empty(&c.orphan_instances));
  assert(atomic_load(&resources_destroyed)==1);assert(device_refs==1);
  /* File is no longer used. Gate can never infer IRQ/worker drain from CPU refs. */
  f.inst=NULL;assert(iris_reclaim_orphan_instances(&c)==-EAGAIN);assert(!atomic_load(&freed));
  if(mode==1){c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;assert(iris_reclaim_orphan_instances(&c)==-EAGAIN);}
  if(mode==2){c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;c.irq_drain_verified=true;assert(iris_reclaim_orphan_instances(&c)==-EAGAIN);}
  if(mode==3){c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;c.irq_drain_verified=true;c.recovery_drain_verified=true;
   assert(!iris_reclaim_orphan_instances(&c));assert(atomic_load(&freed));assert(list_empty(&c.orphan_instances));assert(device_refs==0);
   assert(atomic_load(&resources_destroyed)==7);assert(!iris_reclaim_orphan_instances(&c));assert(atomic_load(&resources_destroyed)==7);}
 } else if(mode>=4&&mode<=11){
  iris_remove_session(i);iris_put_response_instance(i);get_device(c.dev);i->file_detached=true;i->orphaned=true;list_add_tail(&i->list,&c.orphan_instances);
  c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;c.irq_drain_verified=true;c.recovery_drain_verified=true;
  if(mode==4)c.state=IRIS_CORE_INIT;
  if(mode==5)c.firmware_shutdown_verified=false;
  if(mode==6)c.irq_drain_verified=false;
  if(mode==7)c.recovery_drain_verified=false;
  if(mode==8)i->file_detached=false;
  if(mode==9)atomic_store(&i->response_refs.count,1);
  if(mode==10)i->response_closing=false;
  if(mode==11)i->orphaned=false;
  assert(iris_reclaim_orphan_instances(&c)==(mode<=7?-EAGAIN:-EBUSY));
  assert(!atomic_load(&resources_destroyed)&&!atomic_load(&freed));assert(!list_empty(&c.orphan_instances));
 } else if(mode==12){
  c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=c.irq_drain_verified=c.recovery_drain_verified=true;
  assert(iris_reclaim_orphan_instances(&c)==-EBUSY);assert(!atomic_load(&freed));
 } else if(mode==13){
  assert(!iris_close(&f));assert(atomic_load(&freed));assert(device_refs==0);assert(list_empty(&c.orphan_instances));assert(atomic_load(&resources_destroyed)==7);
 } else if(mode>=14&&mode<=17){
  if(mode==14)pm_get_error=-EIO;
  if(mode==15)fw_shutdown_error=-EACCES;
  if(mode==17)c.state=IRIS_CORE_DEINIT;
  c.irq_drain_verified=true;c.recovery_drain_verified=true;
  int ret=iris_core_deinit_checked(&c);
  assert(ret==(mode==14?-EIO:mode==15?-EACCES:mode==16?-EAGAIN:0));
  assert(pm_usage==0);assert(!core_poweroffs&&!core_queue_frees);
  assert(fw_shutdown_calls==(mode==14||mode==17?0:1));
  if(mode==16)assert(c.firmware_shutdown_verified&&!c.irq_drain_verified&&!c.recovery_drain_verified);
 } else if(mode>=18&&mode<=20){
  if(mode==18)close_submit_error=-EIO;
  if(mode==19)close_reply_error=1;
  if(mode==20)close_no_reply=1;
  int ret=iris_close(&f);assert(ret==(mode==20?-ETIMEDOUT:-EIO));
  assert(f.detached&&i->file_detached&&i->orphaned&&i->response_closing);
  assert(atomic_load(&i->response_refs.count)==0&&device_refs==1);
  assert(list_empty(&c.instances)&&!list_empty(&c.orphan_instances));
  assert(!atomic_load(&freed)&&atomic_load(&resources_destroyed)==1);
  f.inst=NULL;assert(iris_reclaim_orphan_instances(&c)==-EAGAIN);
 } else if(mode>=21&&mode<=25){
  /* Actual init failure must not free a queue after firmware may be running. */
  list_del_init(&i->list);c.state=IRIS_CORE_DEINIT;
  if(mode==21)boot_error=-EIO;
  if(mode==22)fw_shutdown_error=-EACCES;
  if(mode==23)load_error=-EIO;
  if(mode==24){i->orphaned=true;list_add_tail(&i->list,&c.orphan_instances);}
  if(mode==25)c.state=IRIS_CORE_ERROR;
  int ret=iris_core_init(&c);
  assert(ret==(mode==21||mode==23?-EIO:mode==24?-EBUSY:mode==25?-EIO:-ETIMEDOUT));
  if(mode==21||mode==22){assert(!core_poweroffs&&!core_queue_frees);assert(c.state==IRIS_CORE_ERROR);}
  if(mode==23){assert(core_poweroffs==1&&core_queue_frees==1);assert(c.state==IRIS_CORE_DEINIT);}
  if(mode==24||mode==25){assert(core_queue_inits==0);}
 } else return 2;
 return 0;
}
'''


def host_prefix():
    prefix = LIFETIME.PREFIX.replace('#define IRIS_CORE_INIT 1', '#define IRIS_CORE_DEINIT 0\n#define IRIS_CORE_INIT 1')
    prefix = prefix.replace('resources_destroyed,freed,queue_errors,stop_calls,close_calls,recovery_calls',
                            'resources_destroyed,freed,stop_calls,close_calls')
    prefix = prefix.replace('close_no_reply,close_wait_result', 'close_no_reply')
    prefix = prefix.replace('static bool refcount_dec_and_test',
        'static int refcount_read(refcount_t *r){return atomic_load(&r->count);}\nstatic bool refcount_dec_and_test')
    prefix = prefix.replace('/* TRACKER */', r'''
static bool list_empty(struct list_head *h){return h->next==h;}
static void list_add_tail(struct list_head *n,struct list_head *h){n->prev=h->prev;n->next=h;h->prev->next=n;h->prev=n;}
static void list_del_init(struct list_head *n){n->prev->next=n->next;n->next->prev=n->prev;INIT_LIST_HEAD(n);}
static void list_splice_init(struct list_head *from,struct list_head *to){
 if(list_empty(from)){return;}
 to->next=from->next;to->prev=from->prev;to->next->prev=to;to->prev->next=to;INIT_LIST_HEAD(from);
}
#define LIST_HEAD(name) struct list_head name={&(name),&(name)}
#define list_for_each_entry_safe(p,n,h,m) \
 for(struct list_head *_it=(h)->next,*_next=NULL;_it!=(h)&& \
 ((_next=_it->next),(p)=container_of(_it,__typeof__(*(p)),m), \
 (n)=_next==(h)?NULL:container_of(_next,__typeof__(*(n)),m),(void)(n),1);_it=_next)
struct device {int unused;};static struct device host_device;static int device_refs;
struct iris_core;
static struct device *get_device(void *p){assert(p==&host_device);device_refs++;return p;}
static void put_device(struct device *p){assert(p==&host_device);assert(device_refs>0);device_refs--;}
static int pm_get_error,fw_shutdown_error,pm_usage,fw_shutdown_calls,core_poweroffs,core_queue_frees;
static int boot_error,load_error,core_queue_inits;
static int iris_hfi_queues_init(struct iris_core *core);
static int iris_vpu_power_on(struct iris_core *core);
static int iris_fw_load(struct iris_core *core);
static int iris_vpu_boot_firmware(struct iris_core *core);
static int iris_hfi_core_init(struct iris_core *core);
static int pm_runtime_resume_and_get(void *dev){(void)dev;if(pm_get_error)return pm_get_error;pm_usage++;return 0;}
static void pm_runtime_put_sync(void *dev){(void)dev;assert(pm_usage>0);pm_usage--;}
static int iris_fw_unload(struct iris_core *core);
static void iris_vpu_power_off(struct iris_core *core);
static void iris_hfi_queues_deinit(struct iris_core *core);
/* TRACKER */
''')
    prefix = prefix.replace('int sys_error_handler;void *dev;',
        'int sys_error_handler;void *dev;struct list_head orphan_instances;struct mutex lifecycle_lock;'
        'bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;')
    prefix = prefix.replace('bool response_closing,response_error_notified;',
        'bool response_closing,response_error_notified,file_detached,orphaned;')
    prefix = prefix.replace('struct file {struct iris_inst *inst;};',
        'struct file {struct iris_inst *inst;bool detached;};')
    prefix = prefix.replace('static void iris_v4l2_fh_deinit(struct iris_inst *i,struct file *f){(void)i;(void)f;atomic_fetch_add(&resources_destroyed,1);}',
        'static void iris_v4l2_fh_deinit(struct iris_inst *i,struct file *f){assert(f->inst==i);assert(!f->detached);f->detached=true;atomic_fetch_add(&resources_destroyed,1);}')
    prefix = prefix.replace('assert(!v4l2_m2m_get_src_vq(p)->streaming);assert(!v4l2_m2m_get_dst_vq(p)->streaming);',
        'v4l2_m2m_get_src_vq(p)->streaming=false;v4l2_m2m_get_dst_vq(p)->streaming=false;')
    # Remove unrelated static callback stubs to keep -Werror meaningful.
    prefix = prefix.replace('static bool schedule_delayed_work(int *work,u32 delay){(void)work;assert(delay==10);atomic_fetch_add(&recovery_calls,1);return true;}', '')
    prefix = prefix.replace('static void iris_vb2_queue_error(struct iris_inst *i){(void)i;atomic_fetch_add(&queue_errors,1);}', '')
    prefix = prefix.replace('/* DEFINITIONS_AFTER_TYPES */', '')
    prefix += '''
static int iris_fw_unload(struct iris_core *core){(void)core;fw_shutdown_calls++;return fw_shutdown_error;}
static void iris_vpu_power_off(struct iris_core *core){(void)core;core_poweroffs++;}
static void iris_hfi_queues_deinit(struct iris_core *core){(void)core;core_queue_frees++;}
static int iris_hfi_queues_init(struct iris_core *core){(void)core;core_queue_inits++;return 0;}
static int iris_vpu_power_on(struct iris_core *core){(void)core;return 0;}
static int iris_fw_load(struct iris_core *core){(void)core;return load_error;}
static int iris_vpu_boot_firmware(struct iris_core *core){(void)core;return boot_error;}
static int iris_hfi_core_init(struct iris_core *core){(void)core;return 0;}
'''
    return prefix


def fixture(work, output, sanitize=False):
    common = (work / 'iris_hfi_common.h').read_text(); start = common.index('struct iris_hfi_pending {')
    tracker = common[start:common.index('enum hfi_packet_port_type {', start)]
    utils = (work / 'iris_utils.c').read_text()
    getter = EXTRACT.block(utils, r'^struct iris_inst \*iris_get_response_instance\([^;{}]*\)\s*\{', 'response getter')
    functions = getter + EXTRACT.definition(utils, 'iris_put_response_instance')
    functions += EXTRACT.definition(utils, 'iris_wait_for_session_response')
    vidc = (work / 'iris_vidc.c').read_text()
    functions += ''.join(EXTRACT.definition(vidc, name) for name in
        ('iris_session_close_checked', 'iris_orphan_reclaim_gate', 'iris_reclaim_orphan_instances', 'iris_close'))
    functions += ''.join(EXTRACT.definition((work / 'iris_core.c').read_text(), name)
                         for name in ('iris_core_deinit_checked','iris_wait_for_system_response','iris_core_init'))
    prefix, main = host_prefix(), MAIN
    core_text = (work / 'iris_core.c').read_text()
    if 'terminal_shutdown' in core_text:
        prefix = prefix.replace('bool firmware_shutdown_verified,',
            'bool terminal_shutdown,removing,queues_allocated,power_prepared,firmware_touched,firmware_may_run;u32 open_owners,orphan_owners;int cpu_refs,power_cleanup_error;\n bool firmware_shutdown_verified,')
        prefix = prefix.replace('#define IRIS_CORE_DEINIT 0', '#define IRIS_CORE_DEINIT 0\n#define READ_ONCE(x) (x)')
        main = main.replace('c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified',
            'c.terminal_shutdown=true;c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified')
        # Legacy deinit/init tests are not terminal. Positive reclaim tests are.
        main = main.replace('c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;c.irq_drain_verified=true;c.recovery_drain_verified=true;',
            'c.terminal_shutdown=true;c.state=IRIS_CORE_DEINIT;c.firmware_shutdown_verified=true;c.irq_drain_verified=true;c.recovery_drain_verified=true;')
    if 'owner_refs' in core_text:
        prefix += '''
static void iris_core_get(struct iris_core *core){assert(core->cpu_refs>0);core->cpu_refs++;}
static void iris_core_put(struct iris_core *core){assert(core->cpu_refs>1);core->cpu_refs--;}
static void iris_core_open_put(struct iris_core *core){assert(core->open_owners>0);core->open_owners--;iris_core_put(core);}
static void iris_core_orphan_transfer(struct iris_core *core){assert(core->open_owners>0);core->open_owners--;core->orphan_owners++;}
static void iris_core_orphan_put(struct iris_core *core){assert(core->orphan_owners>0);core->orphan_owners--;iris_core_put(core);}
'''
        main = main.replace('c.state=IRIS_CORE_INIT;', 'c.state=IRIS_CORE_INIT;c.cpu_refs=2;c.open_owners=1;', 1)
        main = main.replace('iris_remove_session(i);iris_put_response_instance(i);get_device(c.dev);',
            'iris_remove_session(i);iris_put_response_instance(i);get_device(c.dev);iris_core_orphan_transfer(&c);')
        main = main.replace('assert(!iris_reclaim_orphan_instances(&c));assert(atomic_load(&freed));',
            'assert(!iris_reclaim_orphan_instances(&c));assert(c.cpu_refs==1&&!c.orphan_owners);assert(atomic_load(&freed));')
    if 'iris_power_progress_pending' in (work / 'iris_vidc.c').read_text():
        prefix = prefix.replace('bool terminal_shutdown,removing,queues_allocated,power_prepared,firmware_touched,firmware_may_run;',
                                'bool terminal_shutdown,removing,queues_allocated,power_prepared,firmware_touched,firmware_may_run,power_state_unknown;')
        prefix += '''
static bool iris_power_progress_pending(struct iris_core *c){(void)c;return false;}
static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}
static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
static int iris_vpu_power_on_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
'''
    if 'core->power_irq_enabled' in EXTRACT.definition(core_text, 'iris_core_init'):
        prefix = prefix.replace('power_state_unknown;', 'power_state_unknown,power_irq_enabled;')
    if 'Legacy/untracked power is not proof' in core_text:
        # This seam reports unchecked power. A pre-loader error now retains the
        # prepared power/queues; do not turn the seam into a checked-power proof.
        main = main.replace(
            'if(mode==23){assert(core_poweroffs==1&&core_queue_frees==1);assert(c.state==IRIS_CORE_DEINIT);}',
            'if(mode==23){assert(!core_poweroffs&&!core_queue_frees);assert(c.power_prepared&&c.queues_allocated&&c.state==IRIS_CORE_ERROR);}')
    elif 'No synchronize_irq while lifecycle/core are held' in core_text:
        # Fixture power-on has no actual IRQ/clock progress; test that zero-progress branch only.
        main = main.replace('assert(core_poweroffs==1&&core_queue_frees==1);',
                            'assert(!core_poweroffs&&core_queue_frees==1);')
    if 'iris_irq_activate(core)' in core_text:
        prefix = prefix.replace('bool terminal_shutdown,', 'bool irq_managed,firmware_booted,init_acknowledged,core_ready,terminal_shutdown,')
        prefix += '\nstatic int iris_irq_activate(struct iris_core *c){(void)c;return -EOPNOTSUPP;}\n'
    if 'core->gen2_ack_supported' in core_text:
        prefix = prefix.replace('bool irq_managed,firmware_booted,', 'bool gen2_ack_supported,irq_managed,firmware_booted,')
    if 'Keep every orphan/framework owner until all fallible buffer frees pass' in vidc:
        # Buffer-free seam asserts the gate inputs and new pre-splice ordering;
        # it never creates proof bits. Exactly two directional calls still occur.
        prefix = prefix.replace('static atomic_int resources_destroyed,freed,',
            'static int orphan_buffer_frees;\nstatic atomic_int resources_destroyed,freed,')
        prefix = prefix.replace(
            'static void v4l2_ctrl_handler_free(int *p){(void)p;atomic_fetch_add(&resources_destroyed,1);}',
            'static void v4l2_ctrl_handler_free(int *p){(void)p;if(stop_instance->file_detached)assert(orphan_buffer_frees==2);atomic_fetch_add(&resources_destroyed,1);}')
        prefix = prefix.replace(
            'static int iris_destroy_all_internal_buffers(struct iris_inst *i,u32 p){(void)i;(void)p;atomic_fetch_add(&resources_destroyed,1);return 0;}',
            r'''static int iris_destroy_all_internal_buffers(struct iris_inst *i,u32 p){
 if(i->orphaned){
  struct iris_core *core=i->core;
  assert(core->terminal_shutdown&&core->state==IRIS_CORE_DEINIT);
  assert(core->firmware_shutdown_verified&&core->irq_drain_verified&&core->recovery_drain_verified);
  assert(i->file_detached&&i->response_closing&&!refcount_read(&i->response_refs));
  assert(!list_empty(&core->orphan_instances)&&!atomic_load(&freed));
  assert(p==(orphan_buffer_frees?V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE:V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE));
  assert(orphan_buffer_frees<2&&atomic_load(&resources_destroyed)==1+orphan_buffer_frees);
  assert(pthread_mutex_trylock(&i->lock.value)==EBUSY);
  assert(pthread_mutex_trylock(&core->lifecycle_lock.value)==EBUSY);
  assert(!pthread_mutex_trylock(&core->lock.value));assert(!pthread_mutex_unlock(&core->lock.value));
  orphan_buffer_frees++;
 }
 atomic_fetch_add(&resources_destroyed,1);return 0;
}''')
    source = output / 'orphan.c'
    source.write_text(prefix.replace('/* TRACKER */', tracker) + functions + main)
    binary = output / 'orphan'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(source), '-o', str(binary)]
    if sanitize: args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args,capture_output=True,text=True,timeout=40)
    if result.returncode: raise RuntimeError(result.stdout+result.stderr)
    return binary


class OrphanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-orphan-tests-');cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = fixture(WORK,Path(cls.temp.name))
    def run_case(self,mode):
        result=subprocess.run([str(self.binary),str(mode)],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def mutation_rejected(self,before,after,mode):
        source=(Path(self.temp.name)/'orphan.c').read_text()
        self.assertEqual(source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-orphan-mutation-') as directory:
            p=Path(directory);target=p/'mutant.c';binary=p/'mutant'
            target.write_text(source.replace(before,after,1))
            r=subprocess.run(['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1','-pthread',str(target),'-o',str(binary)],capture_output=True,text=True,timeout=40)
            self.assertEqual(r.returncode,0,r.stdout+r.stderr)
            r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5)
            self.assertNotEqual(r.returncode,0)
            self.assertIn('Assertion',r.stderr)

    def test_oracle_rejects_missing_irq_proof(self):
        gate = EXTRACT.definition((WORK / 'iris_vidc.c').read_text(), 'iris_orphan_reclaim_gate')
        before = gate[gate.index('\tif ('):gate.index('\n\t\treturn -EAGAIN;')]
        self.mutation_rejected(before, before.replace('!core->irq_drain_verified || ', ''), 6)

    def test_oracle_rejects_missing_file_detach(self):
        self.mutation_rejected('!inst->response_closing || !inst->file_detached ||',
                               '!inst->response_closing ||',8)

    def test_oracle_rejects_missing_reference_drain(self):
        self.mutation_rejected('!inst->orphaned || refcount_read(&inst->response_refs))',
                               '!inst->orphaned || (refcount_read(&inst->response_refs) < 0))',9)

    def oracle_reclaimer_owner(self):
        self.mutation_rejected('iris_core_get(core);\n\tmutex_lock(&core->lifecycle_lock);',
                               'iris_core_get(core); iris_core_put(core);\n\tmutex_lock(&core->lifecycle_lock);', 3)

    def test_oracle_rejects_missing_detach_transition(self):
        self.mutation_rejected('inst->file_detached = true;',
                               'inst->file_detached = false;',0)


for mode in range(26):
    def test(self,value=mode): self.run_case(value)
    setattr(OrphanTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--work',required=True,type=Path);parser.add_argument('--sanitize',action='store_true')
    args=parser.parse_args();WORK=args.work
    if 'iris_core_get(core);' in EXTRACT.definition((WORK / 'iris_vidc.c').read_text(), 'iris_reclaim_orphan_instances'):
        setattr(OrphanTests, 'test_oracle_rejects_missing_reclaimer_owner', OrphanTests.oracle_reclaimer_owner)
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-orphan-sanitizer-') as d:
            binary=fixture(WORK,Path(d),True)
            for mode in range(26):
                r=subprocess.run([str(binary),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=0:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if r.returncode: print(r.stdout+r.stderr);return 1
            print('26 file-detach/orphan gate ASan/UBSan cases passed; proof bits supplied only by fixtures');return 0
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OrphanTests));return 0 if result.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
