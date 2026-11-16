#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual firmware load/unload and core cleanup progress, with secure calls stubbed."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('quiesce_fixture', HERE / 'test-quiesce-producer.py')
QUIESCE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUIESCE)
EXTRACT = QUIESCE.EXTRACT
WORK = None
CASES = 20

FIRMWARE = r'''
#include <sys/types.h>
#define MEMREMAP_WC 1
#define dev_err(dev,...) ((void)(dev))
typedef uintptr_t phys_addr_t;
struct firmware {int unused;};
struct device_node {int unused;};
struct reserved_mem {phys_addr_t base;size_t size;};
struct tz_cp_config {u32 cp_start,cp_size,cp_nonpixel_start,cp_nonpixel_size;};
static int request_error,mdt_error,auth_error,protect_error,boot_error,core_command_error,load_calls,auth_calls,protect_calls,release_calls,map_calls,unmap_calls;
static bool missing_node,missing_memory,bad_size,map_error;
static struct device_node node;
static struct firmware fw;
static struct reserved_mem reserved={4096,8192};
static struct tz_cp_config cp;
static struct device_node *of_parse_phandle(void *n,const char *p,int index){(void)n;assert(!strcmp(p,"memory-region")&&index==0);return missing_node?NULL:&node;}
static struct reserved_mem *of_reserved_mem_lookup(struct device_node *n){assert(n==&node);return missing_memory?NULL:&reserved;}
static void of_node_put(struct device_node *n){assert(n==&node);}
static int request_firmware(const struct firmware **f,const char *name,struct device *d){assert(d==core.dev&&!strcmp(name,"video.mbn"));if(request_error)return request_error;*f=&fw;return 0;}
static ssize_t qcom_mdt_get_size(const struct firmware *f){assert(f==&fw);return bad_size?-EINVAL:4096;}
static void *memremap(phys_addr_t base,size_t size,int mode){assert(base==4096&&size==8192&&mode==MEMREMAP_WC);map_calls++;return map_error?NULL:&reserved;}
static void memunmap(void *p){assert(p==&reserved);unmap_calls++;}
static void release_firmware(const struct firmware *f){assert(f==&fw);release_calls++;}
static int qcom_mdt_load(struct device *d,const struct firmware *f,const char *name,u32 id,void *mem,phys_addr_t base,size_t size,void *reloc){assert(d==core.dev&&f==&fw&&!strcmp(name,"video.mbn")&&id==9&&mem==&reserved&&base==4096&&size==8192&&!reloc);assert(core.firmware_touched&&core.firmware_may_run&&!core.firmware_shutdown_verified);load_calls++;return mdt_error;}
static int of_property_read_string_index(void *n,const char *p,int i,const char **value){(void)n;(void)value;assert(!strcmp(p,"firmware-name")&&i==0);return -ENOENT;}
static int qcom_scm_pas_auth_and_reset(u32 id){assert(id==9);auth_calls++;return auth_error;}
static int qcom_scm_mem_protect_video_var(u32 a,u32 b,u32 c,u32 d){assert(a==cp.cp_start&&b==cp.cp_size&&c==cp.cp_nonpixel_start&&d==cp.cp_nonpixel_size);protect_calls++;return protect_error;}
static int qcom_scm_pas_shutdown(u32 id){assert(id==9);shutdown_calls++;event(SHUTDOWN);return shutdown_error;}
int iris_fw_unload(struct iris_core *core);
static int iris_hfi_queues_init(struct iris_core *c){assert(c==&core);return 0;}
static int iris_vpu_power_on(struct iris_core *c){assert(c==&core);return 0;}
static int iris_vpu_boot_firmware(struct iris_core *c){assert(c==&core);return boot_error;}
static int iris_hfi_core_init(struct iris_core *c){assert(c==&core);return core_command_error;}
static void reinit_completion(struct completion *c){c->done=0;}
static int iris_wait_for_system_response(struct iris_core *c){assert(c==&core);return 0;}
'''

MAIN = r'''
static void setup(void){memset(&core,0,sizeof(core));assert(!pthread_mutex_init(&core.lock.lock,NULL));assert(!pthread_mutex_init(&core.lifecycle_lock.lock,NULL));assert(!pthread_mutex_init(&core.quiesce_lock.lock,NULL));INIT_LIST_HEAD(&core.instances);INIT_LIST_HEAD(&core.orphan_instances);core.irq=9;core.dev=&device;core.iris_platform_data=&platform;platform.pas_id=9;platform.fwname="video.mbn";platform.tz_cp_config_data=&cp;core.state=IRIS_CORE_DEINIT;}
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);setup();
 if(mode==1)request_error=-ENOENT;
 if(mode==2)missing_node=true;
 if(mode==3)missing_memory=true;
 if(mode==4)bad_size=true;
 if(mode==5)map_error=true;
 if(mode==6)mdt_error=-EIO;
 if(mode==7)auth_error=-EACCES;
 if(mode==8||mode==9){protect_error=-EPERM;shutdown_error=mode==9?-EIO:0;}
 if(mode==10)boot_error=-EPROTO;
 if(mode==11)core_command_error=-EINVAL;
 if(mode==12||mode==13){core.state=IRIS_CORE_INIT;core.firmware_touched=core.firmware_may_run=true;core.queues_allocated=core.power_prepared=true;shutdown_error=-EACCES;assert(iris_core_quiesce(&core)==-EACCES);assert(core.firmware_may_run&&core.firmware_shutdown_error==-EACCES&&!core.firmware_shutdown_verified);assert(core.queues_allocated&&core.power_prepared&&!power_calls&&!queue_calls&&pm_usage==1);if(mode==13){shutdown_error=0;assert(!iris_core_quiesce(&core));assert(!core.firmware_may_run&&!core.queues_allocated&&!core.power_prepared&&pm_gets==1&&mask_calls==1&&shutdown_calls==2&&power_calls==1&&queue_calls==1);}return 0;}
 if(mode==14){assert(!iris_core_quiesce(&core));assert(!shutdown_calls&&!power_calls&&!queue_calls&&sync_calls==1&&cancel_calls==1&&core.terminal_shutdown);assert(!iris_core_quiesce(&core));assert(sync_calls==1);return 0;}
 if(mode==15){core.state=IRIS_CORE_INIT;core.firmware_touched=core.firmware_may_run=true;core.power_prepared=true;assert(!iris_core_quiesce(&core));assert(shutdown_calls==1&&power_calls==1&&!queue_calls);return 0;}
 if(mode==16){core.firmware_may_run=true;core.firmware_shutdown_verified=true;shutdown_error=-EIO;assert(iris_fw_unload(&core)==-EIO);assert(core.firmware_may_run&&!core.firmware_shutdown_verified&&core.firmware_shutdown_error==-EIO);return 0;}
 if(mode==17){char name[130];memset(name,'x',129);name[129]=0;assert(iris_load_fw_to_memory(&core,name)==-EINVAL);assert(!core.firmware_touched&&!load_calls);return 0;}
 if(mode==18){assert(!iris_fw_load(&core));assert(core.firmware_may_run&&core.firmware_touched&&!core.firmware_load_error&&auth_calls==1&&protect_calls==1);assert(!iris_fw_unload(&core));assert(!core.firmware_may_run&&core.firmware_shutdown_verified);return 0;}
 if(mode==19){mdt_error=-ERANGE;assert(iris_fw_load(&core)==-ERANGE);assert(core.firmware_load_error==-ERANGE&&core.firmware_may_run&&load_calls==1&&!auth_calls);return 0;}
 int ret=iris_core_init(&core);int expected=mode==1?-ENOENT:(mode==2||mode==3||mode==4)?-EINVAL:mode==5?-ENOMEM:mode==6?-EIO:mode==7?-EACCES:(mode==8||mode==9)?-EPERM:mode==10?-EPROTO:mode==11?-EINVAL:0;assert(ret==expected);
 if(mode>=1&&mode<=5){assert(!core.firmware_touched&&!core.firmware_may_run&&!core.power_prepared&&!core.queues_allocated);assert(power_calls==1&&queue_calls==1&&!shutdown_calls);assert(core.state==IRIS_CORE_DEINIT);return 0;}
 assert(core.firmware_touched&&core.queues_allocated&&core.power_prepared);assert(!power_calls&&!queue_calls);
 if(mode>=6){assert(core.state==IRIS_CORE_ERROR);if(mode<=9)assert(core.firmware_load_error==expected);if(mode==9)assert(core.firmware_shutdown_error==-EIO&&core.firmware_may_run);if(mode==8||mode==10||mode==11)assert(core.firmware_shutdown_verified&&!core.firmware_may_run);}
 assert(!iris_core_quiesce(&core)||(mode==9));
 if(mode==9){assert(core.firmware_may_run&&core.queues_allocated&&!power_calls&&!queue_calls);shutdown_error=0;assert(!iris_core_quiesce(&core));}
 assert(core.state==IRIS_CORE_DEINIT&&!core.firmware_may_run&&!core.queues_allocated&&!core.power_prepared&&power_calls==1&&queue_calls==1);int count=shutdown_calls;assert(!iris_core_quiesce(&core));assert(shutdown_calls==count&&power_calls==1&&queue_calls==1);return 0;}
'''


def source(work):
    prefix = QUIESCE.PREFIX
    prefix = prefix.replace('struct iris_platform_data {u32 max_session_count;};',
        'struct tz_cp_config; struct iris_platform_data {u32 max_session_count,pas_id;const char *fwname;struct tz_cp_config *tz_cp_config_data;};')
    prefix = prefix.replace('struct iris_core;', 'struct device {void *of_node;}; static struct device device;\nstruct iris_core;', 1)
    prefix = prefix.replace('void *dev;const struct iris_hfi_response_ops', 'struct device *dev;const struct iris_hfi_response_ops')
    prefix = prefix.replace('bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;};',
        'bool firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;\n bool queues_allocated,power_prepared,firmware_touched,firmware_may_run,removing;u32 open_inflight,open_owners;int firmware_load_error,firmware_shutdown_error;};')
    prefix = prefix.replace('assert(d==&core);', 'assert(d==core.dev);')
    prefix = prefix.replace('assert(d==&core&&pm_usage==1);', 'assert(d==core.dev&&pm_usage==1);')
    start = prefix.index('static int iris_fw_unload(')
    end = prefix.index('static void iris_vpu_power_off(', start)
    prefix = prefix[:start] + prefix[end:]
    prefix = prefix.replace('assert(c->firmware_shutdown_verified&&sync_calls&&cancel_calls);', 'assert(c==&core);')
    prefix = prefix.replace('assert(c->firmware_shutdown_verified&&power_calls);', 'assert(c==&core&&power_calls);')
    # Unrelated seams from the reused IRQ fixture are deliberately not compiled.
    start = prefix.index('static void pm_runtime_mark_last_busy(')
    prefix = prefix[:start]
    prefix = prefix.replace('static int refcount_read(refcount_t *r){return r->value;}', '')
    prefix = prefix.replace('static void enable_irq(int irq){assert(irq==9);enable_calls++;}', '')
    prefix = prefix.replace('response_entered,response_release', 'response_release')
    prefix = prefix.replace('mask_calls,enable_calls,shutdown_calls', 'mask_calls,shutdown_calls')
    prefix = prefix.replace('cancel_calls,clear_calls,response_calls', 'cancel_calls')
    prefix = prefix.replace('static struct iris_platform_data platform={16};', 'static struct iris_platform_data platform;')
    if 'core->hardware_owner_held' in (work / 'iris_core.c').read_text():
        prefix = prefix.replace('bool queues_allocated,power_prepared,', 'bool hardware_owner_held,resources_releasing,queues_allocated,power_prepared,')
    if 'power_state_unknown' in (work / 'iris_core.c').read_text():
        prefix = prefix.replace('bool hardware_owner_held,resources_releasing,queues_allocated,power_prepared,',
                                'bool hardware_owner_held,resources_releasing,power_state_unknown,queues_allocated,power_prepared,')
        prefix = prefix.replace('bool removing;', 'bool removing;bool power_state_unknown;') if 'bool removing;' in prefix else prefix
    firmware = (work / 'iris_firmware.c').read_text()
    core = (work / 'iris_core.c').read_text()
    functions = ''.join(EXTRACT.definition(firmware, name) for name in
                        ('iris_load_fw_to_memory', 'iris_fw_load', 'iris_fw_unload'))
    functions += EXTRACT.definition(core, 'iris_core_quiesce') + EXTRACT.definition(core, 'iris_core_init')
    if 'iris_vpu_has_checked_power' in core:
        prefix += '''
static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}
static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
static int iris_vpu_power_on_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
static bool iris_power_progress_pending(struct iris_core *c){(void)c;return false;}
'''
    main = MAIN
    if 'core->hardware_owner_held' in core:
        main = main.replace('core.irq=9;', 'core.hardware_owner_held=true;core.irq=9;')
    if 'terminal_shutdown && core->state == IRIS_CORE_DEINIT' in core:
        prefix = prefix.replace('power_state_unknown,queues_allocated', 'power_state_unknown,power_irq_enabled,queues_allocated')
        prefix = prefix.replace('int firmware_load_error,firmware_shutdown_error;', 'int firmware_load_error,firmware_shutdown_error,power_cleanup_error;')
        prefix = prefix.replace('static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return false;}',
                                'static bool iris_vpu_has_checked_power(struct iris_core *c){(void)c;return true;}')
        prefix = prefix.replace('static int iris_vpu_power_off_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}',
                                'static int iris_vpu_power_off_checked(struct iris_core *c){iris_vpu_power_off(c);return 0;}')
        prefix = prefix.replace('static int iris_vpu_power_on_checked(struct iris_core *c){(void)c;return -EOPNOTSUPP;}',
                                'static int iris_vpu_power_on(struct iris_core *c);\nstatic int iris_vpu_power_on_checked(struct iris_core *c){return iris_vpu_power_on(c);}')
        # No IRQ/power progress seam here: early pre-loader failures are the historical zero-progress cases.
        prefix = prefix.replace('assert(c==&core&&power_calls);',
                                'assert(c==&core&&!c->firmware_may_run&&!c->power_prepared);')
        main = main.replace('assert(power_calls==1&&queue_calls==1&&!shutdown_calls);', 'assert(!power_calls&&queue_calls==1&&!shutdown_calls);')
    if 'iris_irq_activate(core)' in core:
        prefix = prefix.replace('bool hardware_owner_held,', 'bool core_ready,firmware_booted,init_acknowledged,irq_managed,hardware_owner_held,')
        prefix += '''
static int iris_irq_activate(struct iris_core *c){(void)c;return -EOPNOTSUPP;}
static void iris_irq_terminal_mask(struct iris_core *c){if(!c->quiesce_irq_mask_held){disable_irq_nosync(c->irq);c->quiesce_irq_mask_held=true;}}
static void iris_irq_retire_top(struct iris_core *c){(void)c;}
'''
    if 'core->gen2_ack_supported' in core:
        prefix = prefix.replace('bool core_ready,firmware_booted,init_acknowledged,irq_managed,',
                                'bool core_ready,firmware_booted,init_acknowledged,irq_managed,gen2_ack_supported,')
        firmware_seams = FIRMWARE.replace('static int iris_wait_for_system_response(struct iris_core *c){assert(c==&core);return 0;}',
                                         'static int iris_wait_for_system_response(struct iris_core *c){assert(c==&core);c->init_acknowledged=true;return 0;}')
    else:
        firmware_seams = FIRMWARE
    if 'Only never-started, never-touched checked hardware permits inline rollback.' in core:
        main = main.replace('assert(!power_calls&&queue_calls==1&&!shutdown_calls);',
                            'assert(power_calls==1&&queue_calls==1&&!shutdown_calls);')
    return prefix + '\n#define MAX_FIRMWARE_NAME_SIZE 128\n' + firmware_seams + functions + main


def compile_fixture(text, output, sanitize=False):
    src = output / 'progress.c'
    src.write_text(text)
    binary = output / 'progress'
    args = ['/usr/bin/gcc', '-std=gnu11', '-Wall', '-Wextra', '-Werror', '-O1', '-pthread', str(src), '-o', str(binary)]
    if sanitize:
        args += ['-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-fno-pie', '-no-pie']
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class ProgressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-progress-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.source = source(WORK)
        cls.binary = compile_fixture(cls.source, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation(self, before, after, mode):
        self.assertEqual(self.source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='iris-progress-mutation-') as directory:
            binary = compile_fixture(self.source.replace(before, after, 1), Path(directory))
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_load_failure_cleanup(self):
        if 'Only never-started, never-touched checked hardware permits inline rollback.' in (WORK / 'iris_core.c').read_text():
            self.mutation('if (core->firmware_touched) {\n\t\t\tcore->core_ready = false;\n\t\t\tcore->state = IRIS_CORE_ERROR;\n\t\t\tgoto exit;',
                          'if (core->firmware_touched) {\n\t\t\tcore->core_ready = false;\n\t\t\tcore->state = IRIS_CORE_ERROR;\n\t\t\tgoto error_queue_deinit;', 9)
        else:
            self.mutation('if (core->firmware_touched) {', 'if (false) {', 9)

    def test_oracle_loader_errno(self):
        self.mutation('core->firmware_load_error = ret;\n\t\treturn ret;\n\t}\n\n\tret = qcom_scm_pas_auth_and_reset',
            'core->firmware_load_error = ret;\n\t\treturn -ENOMEM;\n\t}\n\n\tret = qcom_scm_pas_auth_and_reset', 19)

    def test_oracle_shutdown_error_lost(self):
        self.mutation('core->firmware_shutdown_error = ret;', 'core->firmware_shutdown_error = 0;', 16)

    def test_oracle_unknown_loader_started(self):
        self.mutation('core->firmware_may_run = true;', 'core->firmware_may_run = false;', 6)


for mode in range(CASES):
    def test(self, value=mode):
        self.run_case(value)
    setattr(ProgressTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', required=True, type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    WORK = args.work
    if args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-progress-sanitizer-') as directory:
            binary = compile_fixture(source(WORK), Path(directory), True)
            for mode in range(CASES):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5,
                    env={'PATH': '/usr/bin:/bin', 'ASAN_OPTIONS': 'detect_leaks=0:halt_on_error=1', 'UBSAN_OPTIONS': 'halt_on_error=1'})
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
        print('%d actual load/shutdown/core-progress ASan/UBSan scenarios passed; secure calls stubbed' % CASES)
        return 0
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProgressTests))
    return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
