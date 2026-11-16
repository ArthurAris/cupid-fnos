#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Strict terminal power cleanup with actual core/power/IRQ code and clock-aware seams."""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('historical_power', HERE / 'test-vpu-power.py')
POWER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(POWER)
EXTRACT = POWER.EXTRACT
WORK = None
BASELINE = False
CASES = 28

EXTRA = r'''
static int mmio_reads,mmio_writes,mask_calls,sync_calls,cancel_calls;
static bool irq_inflight,irq_join,irq_entered,irq_release;
static pthread_t irq_thread;
static pthread_mutex_t irq_gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t irq_cond=PTHREAD_COND_INITIALIZER;
static int responses,reenables;
static void check_clocked(void){assert(core.power_progress.axi_clk_on&&core.power_progress.ctrl_clk_on&&core.power_progress.ctrl_domain_ref);}
static void respond(struct iris_core *c){assert(c==&core&&c->queues_allocated);responses++;
 assert(!pthread_mutex_lock(&irq_gate));irq_inflight=true;irq_entered=true;assert(!pthread_cond_broadcast(&irq_cond));while(!irq_release)assert(!pthread_cond_wait(&irq_cond,&irq_gate));irq_inflight=false;assert(c->queues_allocated);assert(!pthread_mutex_unlock(&irq_gate));}
static void assert_wait_locks_free(void){assert(!pthread_mutex_trylock(&core.lock.lock));assert(!pthread_mutex_unlock(&core.lock.lock));assert(!pthread_mutex_trylock(&core.lifecycle_lock.lock));assert(!pthread_mutex_unlock(&core.lifecycle_lock.lock));}
'''

MAIN = r'''
static void preset_stub(struct iris_core *c){(void)c;event(E_PRESET);}
static void setup(void){memset(&core,0,sizeof(core));memset(&dev,0,sizeof(dev));memset(pd_devs,0,sizeof(pd_devs));
 assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));assert(!pthread_mutex_init(&core.remove_lock.lock,NULL));INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);
 core.dev=&dev;core.reg_base=(void *)0x1000;core.irq=9;core.icc_tbl=icc_tbl;core.icc_count=1;core.pmdomain_tbl=&pd_list;core.hardware_owner_held=true;
 platform.vpu_ops=&platform_ops;platform.icc_tbl=icc_info_tbl;platform.clk_rst_tbl_size=1;platform.set_preset_registers=preset_stub;platform.no_tz_fifo_reset=true;
 platform_ops.power_on_checked=iris_vpu2_power_on_checked;platform_ops.power_off_checked=iris_vpu2_power_off_checked;core.iris_platform_data=&platform;core.hfi_ops=&hfi_ops_stub;core.hfi_response_ops=&response_ops;
}
static void powered(void){assert(!iris_vpu_power_on_checked(&core));core.power_prepared=true;core.queues_allocated=true;core.state=IRIS_CORE_INIT;}
static void fixture_drain(void){core.terminal_shutdown=core.irq_quiescing=core.quiesce_irq_mask_held=core.irq_drain_verified=core.recovery_drain_verified=true;}
static void *run_irq(void *unused){(void)unused;assert(iris_hfi_isr_handler(9,&core)==2);return NULL;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==0){powered();int before=mmio_reads+mmio_writes;assert(iris_vpu_power_off_checked(&core)==-EAGAIN);assert(mmio_reads+mmio_writes==before&&!put_calls[0]&&!put_calls[1]&&core.power_irq_enabled);return 0;}
 if(mode==1){fw_load_error=-ENOENT;assert(iris_core_init(&core)==-ENOENT);assert(core.state==IRIS_CORE_ERROR&&core.queues_allocated&&core.power_prepared&&core.power_irq_enabled&&!queue_free_calls);return 0;}
 if(mode==2){powered();irq_join=true;assert(!pthread_create(&irq_thread,NULL,run_irq,NULL));assert(!pthread_mutex_lock(&irq_gate));while(!irq_entered)assert(!pthread_cond_wait(&irq_cond,&irq_gate));assert(!pthread_mutex_unlock(&irq_gate));assert(!iris_core_quiesce(&core));assert(!irq_inflight&&!irq_join&&responses==1&&reenables==0&&sync_calls==1&&cancel_calls==1&&queue_free_calls==1&&core.state==IRIS_CORE_DEINIT&&!core.power_irq_enabled);return 0;}
 if(mode==3||mode==4){powered();fixture_drain();clk_disable_error[IRIS_AXI_CLK]=-EIO;assert(iris_vpu_power_off_checked(&core)==-EIO);if(mode==3)assert(core.power_progress.controller_drained);assert(!core.power_progress.ctrl_clk_on&&core.power_progress.axi_clk_on);int reads=mmio_reads,writes=mmio_writes,ctrl=clk_dis_calls[IRIS_CTRL_CLK];clk_disable_error[IRIS_AXI_CLK]=0;assert(!iris_vpu_power_off_checked(&core));assert(mmio_reads==reads&&mmio_writes==writes&&clk_dis_calls[IRIS_CTRL_CLK]==ctrl&&!iris_power_progress_pending(&core));return 0;}
 if(mode==5||mode==6){core.power_progress.ctrl_domain_ref=true;pd_devs[0].power.usage_count=1;core.power_progress.axi_clk_on=mode==5;core.power_progress.ctrl_clk_on=mode==6;assert(iris_vpu_power_off_checked(&core)==-EAGAIN);assert(!mmio_reads&&!mmio_writes&&!put_calls[0]);return 0;}
 if(mode==7){powered();platform_ops.power_on_checked=NULL;platform_ops.power_off_checked=NULL;assert(iris_core_quiesce(&core)==-EOPNOTSUPP);assert(core.queues_allocated&&core.power_prepared&&!queue_free_calls&&!legacy_off_calls&&!resume_calls);return 0;}
 if(mode==8){powered();assert(iris_hfi_pm_suspend(&core)==-EOPNOTSUPP);assert(core.power_prepared&&core.queues_allocated&&core.power_irq_enabled&&!put_calls[0]&&!put_calls[1]);return 0;}
 if(mode==9){assert(iris_hfi_pm_resume(&core)==-EOPNOTSUPP);assert(!irq_enable_calls&&!legacy_on_calls&&!iris_power_progress_pending(&core));return 0;}
 if(mode==10){clk_enable_error[IRIS_CTRL_CLK]=-EBUSY;assert(iris_core_init(&core)==-EBUSY);assert(core.power_error==-EBUSY&&core.power_cleanup_error==-EAGAIN&&!core.power_state_unknown&&core.queues_allocated&&core.state==IRIS_CORE_ERROR);assert(core.power_progress.axi_clk_on&&!core.power_progress.ctrl_clk_on&&!mmio_reads&&!mmio_writes);return 0;}
 if(mode==11||mode==12){powered();if(mode==12)core.power_prepared=false;assert(!iris_core_quiesce(&core));int resumes=resume_calls,queues=queue_free_calls;assert(!iris_core_quiesce(&core));assert(resumes==resume_calls&&queues==queue_free_calls&&!iris_power_progress_pending(&core));return 0;}
 if(mode==13){powered();assert(!iris_core_quiesce(&core));core.power_progress.icc_voted=true;assert(!iris_core_quiesce(&core));assert(!core.power_progress.icc_voted);return 0;}
 if(mode==14){powered();fixture_drain();core.irq_drain_verified=false;assert(iris_vpu_power_off_checked(&core)==-EAGAIN);assert(!put_calls[0]&&!put_calls[1]);return 0;}
 if(mode==15){powered();fixture_drain();core.recovery_drain_verified=false;assert(iris_vpu_power_off_checked(&core)==-EAGAIN);return 0;}
 if(mode==16){powered();fixture_drain();put_error[1]=-EIO;assert(iris_vpu_power_off_checked(&core)==-EIO);assert(core.power_state_unknown&&!core.power_progress.hw_domain_ref);int puts=put_calls[1];assert(iris_vpu_power_off_checked(&core)==-EUCLEAN);assert(put_calls[1]==puts);return 0;}
 if(mode==17){fw_load_error=-ENOENT;assert(iris_core_init(&core)==-ENOENT);assert(!iris_core_quiesce(&core));assert(!core.queues_allocated&&!core.power_prepared&&!iris_power_progress_pending(&core)&&queue_free_calls==1);return 0;}
 if(mode==18){powered();fixture_drain();clk_disable_error[IRIS_CTRL_CLK]=-EIO;assert(iris_vpu_power_off_checked(&core)==-EIO);int writes=mmio_writes;clk_disable_error[IRIS_CTRL_CLK]=0;assert(!iris_vpu_power_off_checked(&core));assert(mmio_writes==writes);return 0;}
 if(mode==19){core.power_state_unknown=true;core.queues_allocated=true;core.state=IRIS_CORE_ERROR;assert(iris_core_quiesce(&core)==-EUCLEAN);assert(core.queues_allocated&&core.shutdown_pm_held&&!queue_free_calls);return 0;}
 if(mode==20){powered();fixture_drain();core.power_progress.ctrl_domain_ref=false;assert(iris_vpu_power_off_checked(&core)==-EAGAIN);assert(!mmio_reads&&!mmio_writes);return 0;}
 if(mode==21){queue_init_error=-ENOMEM;assert(iris_core_init(&core)==-ENOMEM);assert(core.state==IRIS_CORE_DEINIT&&!core.queues_allocated&&!irq_enable_calls);return 0;}
 if(mode==22){icc_set_error=-EIO;assert(iris_core_init(&core)==-EIO);assert(core.state==IRIS_CORE_DEINIT&&!core.queues_allocated&&!iris_power_progress_pending(&core)&&queue_free_calls==1);return 0;}
 if(mode==23){powered();fixture_drain();core.power_progress.icc_voted=core.power_progress.ctrl_domain_ref=core.power_progress.hw_domain_ref=core.power_progress.hwmode_set=core.power_progress.axi_clk_on=core.power_progress.ctrl_clk_on=core.power_progress.hw_clk_on=false;assert(!iris_vpu_power_off_checked(&core));assert(!core.power_irq_enabled);return 0;}
 if(mode==24||mode==25||mode==26){powered();if(mode==24)aon_val=0;if(mode==25)noc_val=0;if(mode==26)dbg_val=1;assert(iris_core_quiesce(&core)==-ETIMEDOUT);assert(core.queues_allocated&&core.power_prepared&&core.power_irq_enabled&&core.shutdown_pm_held&&!queue_free_calls&&!core.power_progress.controller_drained);return 0;}
 if(mode==27){platform_ops.power_on_checked=NULL;platform_ops.power_off_checked=NULL;legacy_on_error=-ENODEV;assert(iris_hfi_pm_resume(&core)==-EBUSY);return 0;}
 return 2;}
'''


def source(work):
    prefix = POWER.PREFIX
    prefix = prefix.replace('hw_clk_on;};', 'hw_clk_on,controller_drained;};')
    prefix = prefix.replace('int power_error;bool power_irq_enabled;', 'int power_error,power_cleanup_error;bool power_irq_enabled;')
    prefix = prefix.replace('static long iris_abs(long v){return v<0?-v:v;}', '')
    prefix = prefix.replace('#define abs(v) iris_abs((long)(v))', '')
    prefix = prefix.replace('static u32 readl(', 'static void check_clocked(void);\nstatic u32 readl(')
    prefix = prefix.replace('uintptr_t a=(uintptr_t)addr-(uintptr_t)core.reg_base;',
                            'check_clocked();\n uintptr_t a=(uintptr_t)addr-(uintptr_t)core.reg_base;')
    prefix = prefix.replace('static void iris_hfi_queues_deinit(struct iris_core *c){(void)c;queue_free_calls++;}',
                            'static void iris_hfi_queues_deinit(struct iris_core *c){assert(!irq_inflight);queue_free_calls++;c->queues_allocated=false;}')
    # Move the shared callback state before seams that reference it.
    index = prefix.index('static u32 readl(')
    prefix = prefix[:index] + EXTRA + prefix[index:]
    prefix = prefix.replace('check_clocked();\n uintptr_t a=', 'check_clocked();mmio_reads++;\n uintptr_t a=', 1)
    start = prefix.index('static void writel(')
    prefix = prefix[:start] + prefix[start:].replace('check_clocked();\n uintptr_t a=', 'check_clocked();mmio_writes++;\n uintptr_t a=', 1)
    prefix = prefix.replace('static void disable_irq_nosync(int irq){(void)irq;disable_calls++;}',
                            'static void disable_irq_nosync(int irq){assert(irq==9);disable_calls++;mask_calls++;}')
    prefix = prefix.replace('static void synchronize_irq(int irq){(void)irq;}', r'''static void synchronize_irq(int irq){assert(irq==9);sync_calls++;assert_wait_locks_free();if(irq_join){assert(!pthread_mutex_lock(&irq_gate));irq_release=true;assert(!pthread_cond_broadcast(&irq_cond));assert(!pthread_mutex_unlock(&irq_gate));assert(!pthread_join(irq_thread,NULL));irq_join=false;}}''')
    prefix = prefix.replace('static bool cancel_delayed_work_sync(struct delayed_work *w){(void)w;return false;}',
                            'static bool cancel_delayed_work_sync(struct delayed_work *w){(void)w;cancel_calls++;assert_wait_locks_free();return false;}')
    prefix = prefix.replace('static void enable_irq(int irq){assert(irq==9);irq_enable_calls++;event(E_IRQ_ON);}',
                            'static void enable_irq(int irq){assert(irq==9);irq_enable_calls++;if(irq_inflight)reenables++;event(E_IRQ_ON);}')
    prefix = prefix.replace('struct iris_platform_data;', 'struct iris_platform_data;\nstruct iris_hfi_response_ops {void (*hfi_response_handler)(struct iris_core *);};', 1)
    prefix = prefix.replace('const struct iris_hfi_command_ops *hfi_ops;bool firmware_touched;',
                            'const struct iris_hfi_command_ops *hfi_ops;const struct iris_hfi_response_ops *hfi_response_ops;bool firmware_touched;')
    prefix += '''\nstatic const struct iris_hfi_response_ops response_ops={respond};
#define IRQ_NONE 0
#define IRQ_HANDLED 2
typedef int irqreturn_t;
static void iris_vpu_clear_interrupt(struct iris_core *c){assert(c==&core);check_clocked();}
static int iris_vpu_watchdog(struct iris_core *c,u32 status){(void)c;(void)status;return 0;}
'''
    vc = (work / 'iris_vpu_common.c').read_text()
    rc = (work / 'iris_resources.c').read_text()
    cc = (work / 'iris_core.c').read_text()
    hc = (work / 'iris_hfi_common.c').read_text()
    functions = ''.join(EXTRACT.definition(vc, name) for name in
        ('iris_power_progress_pending', 'iris_vpu_has_checked_power', 'iris_vpu_power_on_checked',
         'iris_vpu_power_off_checked', 'iris_vpu2_power_off_hw_checked', 'iris_vpu2_power_off_controller_checked'))
    if 'iris_power_off_drain_gate' in vc:
        functions += EXTRACT.definition(vc, 'iris_power_off_drain_gate')
    functions += EXTRACT.definition(vc, 'iris_vpu2_power_off_checked') + EXTRACT.definition(vc, 'iris_vpu2_power_on_checked')
    functions += ''.join(EXTRACT.definition(rc, n) for n in ('iris_set_icc_bw', 'iris_unset_icc_bw', 'iris_enable_power_domains', 'iris_disable_power_domains'))
    functions += EXTRACT.definition(cc, 'iris_core_quiesce') + EXTRACT.definition(cc, 'iris_core_init')
    functions += EXTRACT.block(hc, r'^irqreturn_t\s+iris_hfi_isr_handler\([^;{}]*\)\s*\{', 'IRQ thread')
    functions += EXTRACT.definition(hc, 'iris_hfi_pm_suspend') + EXTRACT.definition(hc, 'iris_hfi_pm_resume')
    if 'iris_power_off_drain_gate' not in vc:
        # Reproduce old control-flow defects, using the kernel-style abs seam.
        prefix += '\n#define abs(x) ((long)(x) < 0 ? -(long)(x) : (long)(x))\n'
    return prefix + functions + MAIN


def compile_fixture(text, output, sanitize=False, historical=False):
    src = output / 'strict.c'
    src.write_text(text)
    binary = output / 'strict'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if historical:
        args += ['-Wno-sign-compare']  # Baseline C retained verbatim; candidate has no suppression.
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class StrictTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-strict-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        r = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-strict-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            r = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn('Assertion', r.stderr)

    def test_oracle_unmasked_poweroff(self):
        self.mutation('ret = iris_power_off_drain_gate(core);', 'ret = iris_power_off_drain_gate(core); ret = 0;', 0)

    def test_oracle_repeated_mmio(self):
        self.mutation('core->power_progress.controller_drained = true;', 'core->power_progress.controller_drained = false;', 3)

    def test_oracle_init_release_before_drain(self):
        self.mutation('if (core->power_irq_enabled || core->power_state_unknown ||\n\t    iris_power_progress_pending(core)) {', 'if (false) {', 1)

    def test_oracle_pending_idempotence(self):
        self.mutation('!core->power_state_unknown && !iris_power_progress_pending(core) &&', '!core->power_state_unknown &&', 13)

    def test_oracle_irq_not_joined(self):
        self.mutation('synchronize_irq(core->irq);', '/* skipped IRQ drain */ (void)synchronize_irq;', 2)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(StrictTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    parser.add_argument('--baseline', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    if args.baseline:
        with tempfile.TemporaryDirectory(prefix='iris-strict-baseline-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), historical=True)
            results = []
            for mode in (0, 1, 4, 5, 7, 8, 10):
                r = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
                print('baseline case', mode, 'exit', r.returncode, r.stderr.strip())
                results.append(r.returncode != 0)
            return 0 if all(results) else 1
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-strict-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(CASES):
                r = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if r.returncode:
                    print(r.stdout + r.stderr)
                    return 1
        print('%d strict IRQ/power/clock-aware ASan/UBSan scenarios passed; host models, no hardware proof' % CASES)
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(StrictTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
