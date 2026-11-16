#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile actual queue/header/response/wait functions against bounded host fixtures."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('c_extract', HERE / 'test-iris-behavior.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None

PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32; typedef uint64_t u64;
typedef int spinlock_t;
#define spin_lock_init(lock) (*(lock)=0)
#define spin_lock_irqsave(lock, flags) ((void)(lock),(flags)=0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock),(void)(flags))
/* TRACKER */
#define BIT(n) (1u << (n))
#define READ_ONCE(x) (x)
#define min(a,b) ((a)<(b)?(a):(b))
#define mb() ((void)0)
#define rmb() ((void)0)
#define IFACEQ_CORE_PKT_SIZE 4096
#define IFACEQ_QUEUE_SIZE 1024
#define IRIS_FW_VERSION_LENGTH 128
#define msecs_to_jiffies(x) (x)
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
struct completion { int done; };
struct mutex { int unused; };
struct iris_platform_data { u32 hw_response_timeout; };
struct iris_core { u32 state; struct completion core_init_done; void *dev;
    struct iris_platform_data *iris_platform_data; struct iris_hfi_pending init_pending;
    struct mutex lock; };
struct iris_inst { struct iris_core *core; struct completion completion, flush_completion;
    struct mutex lock; u32 state; struct iris_hfi_pending control_pending; };
'''

STUBS = r'''
struct iris_iface_q_info { struct iris_hfi_queue_header *qhdr; void *kernel_vaddr; };
static int complete_calls, wait_result, wait_calls, inject_core_error, inject_inst_error;
static struct iris_core *current_core;
static struct iris_inst *current_inst;
static uintptr_t waited_completion;
static char version[256];
static void complete(struct completion *c) { c->done=1; complete_calls++; }
static void mutex_lock(struct mutex *m) { (void)m; }
static void mutex_unlock(struct mutex *m) { (void)m; }
static void iris_inst_change_state(struct iris_inst *inst,u32 state) { inst->state=state; }
static int wait_for_completion_timeout(struct completion *c,u32 timeout) {
    assert(timeout==1000); wait_calls++; waited_completion=(uintptr_t)c;
    if(inject_core_error) current_core->state=IRIS_CORE_ERROR;
    if(inject_inst_error) current_inst->state=IRIS_INST_ERROR;
    return wait_result;
}
static int iris_inst_sub_state_change_pause(struct iris_inst *inst, u32 plane) {
    (void)inst; assert(plane==9||plane==10); return 0;
}
static void log_version(void *dev,const char *format,...) {
    va_list a; (void)dev; va_start(a,format); vsnprintf(version,sizeof(version),format,a); va_end(a);
}
#define dev_dbg log_version
'''

MAIN = r'''
int main(int argc,char **argv) {
    assert(argc==2); int mode=atoi(argv[1]);
    _Alignas(8) u8 data[4096]={0}, ring[1024]={0};
    struct iris_hfi_header *h=(void *)data;
    struct iris_hfi_packet *p=(void *)(data+sizeof(*h));
    static struct iris_platform_data platform={1000};
    static struct iris_core core={.state=IRIS_CORE_INIT,.iris_platform_data=&platform};
    static struct iris_inst inst={.core=&core,.state=IRIS_INST_INIT};
    current_core=&core; current_inst=&inst;
    iris_hfi_pending_init(&core.init_pending); iris_hfi_pending_init(&inst.control_pending);
    h->size=64; h->num_packets=1;
    p->size=32; p->type=HFI_CMD_INIT; p->payload_info=HFI_PAYLOAD_NONE;
    if(mode<=11) {
        u32 actual=64;
        if(mode==1) p->size=256;
        if(mode==2) h->num_packets=0;
        if(mode==3) h->num_packets=2;
        if(mode==4) actual=40;
        if(mode==5) {h->size=68; actual=68;}
        if(mode==6) p->size=31;
        if(mode==7) p->payload_info=100;
        if(mode==8) {p->type=HFI_PROP_CROP_OFFSETS;p->payload_info=HFI_PAYLOAD_64_PACKED;}
        if(mode==9) {p->type=HFI_CMD_BUFFER;p->payload_info=HFI_PAYLOAD_STRUCTURE;}
        if(mode==10) {h->size=68;actual=68;p->size=36;p->payload_info=HFI_PAYLOAD_U32;p->payload[0]=7;}
        if(mode==11) {h->size=160;actual=160;p->size=128;p->type=HFI_PROP_IMAGE_VERSION;p->payload_info=HFI_PAYLOAD_STRING;}
        assert(iris_hfi_gen2_validate_hdr_packet(&core,h,actual)==((mode==0||mode==10)?0:-EINVAL));
    } else if(mode>=12 && mode<=16) {
        p->flags=mode==13?0:HFI_FW_FLAGS_SUCCESS;
        if(mode==14) p->type=HFI_CMD_CLOSE;
        if(mode==15) core.state=IRIS_CORE_DEINIT;
        if(mode==16) core.state=IRIS_CORE_ERROR;
        assert(!iris_hfi_pending_begin(&core.init_pending,p->packet_id,HFI_CMD_INIT,p->port));
        int r=iris_hfi_gen2_handle_system_init(&core,p);
        if(mode==12||mode==13) {
            assert(r==0 && complete_calls==1);
            assert(core.state==(mode==13?IRIS_CORE_ERROR:IRIS_CORE_INIT));
        } else assert(r==-EINVAL && complete_calls==0);
    } else if(mode>=17 && mode<=22) {
        wait_result=mode==17||mode==20?0:1;
        inject_core_error=mode==19;
        inject_inst_error=mode==21;
        int r=mode<=19?iris_wait_for_system_response(&core):iris_wait_for_session_response(&inst,mode==22);
        assert(r==(mode==17||mode==20?-ETIMEDOUT:mode==19||mode==21?-EIO:0));
        if(mode==17) assert(core.state==IRIS_CORE_ERROR);
        if(mode==20) assert(inst.state==IRIS_INST_ERROR);
        if(mode==22) assert(waited_completion==(uintptr_t)&inst.flush_completion);
    } else if(mode>=23 && mode<=36) {
        struct iris_hfi_queue_header q={.queue_type=IFACEQ_MSGQ_ID,.read_idx=0,.write_idx=4};
        struct iris_iface_q_info info={&q,ring};
        u32 size=16, actual=99, original_read;
        if(mode==24) size=0;
        if(mode==25) size=15;
        if(mode==26) size=32;
        if(mode==27) q.read_idx=256;
        if(mode==28) q.write_idx=256;
        if(mode==29) q.write_idx=0;
        if(mode==30) {q.read_idx=254;q.write_idx=2;}
        if(mode==31) {q.read_idx=252;q.write_idx=0;}
        if(mode==32) size=5000;
        u32 start=q.read_idx<256?q.read_idx*4:0;
        memcpy(ring+start,&size,4);
        if(mode==30) {memset(ring+start+4,0x33,4);memset(ring,0x44,8);}
        original_read=q.read_idx;
        memset(data,0xa5,sizeof(data));
        int r=iris_hfi_queue_read(&info,data,mode==33?8:4096,mode==34?NULL:&actual);
        bool success=mode==23||mode==30||mode==31||mode==35||mode==36;
        if(success) {
            assert(r==0 && actual==16);
            u32 copied;memcpy(&copied,data,4);assert(copied==size);
            for(u32 i=16;i<sizeof(data);i++) assert(data[i]==0xa5);
            if(mode==30) assert(data[4]==0x33 && data[8]==0x44 && q.read_idx==2);
            if(mode==31) assert(q.read_idx==0);
        } else {
            assert(r==(mode==29?-ENODATA:mode==34?-EINVAL:-EBADMSG));
            if(mode!=34) assert(actual==0);
            assert(q.read_idx==original_read);
            for(u32 i=0;i<sizeof(data);i++) assert(data[i]==0xa5);
        }
    } else if(mode==37||mode==38) {
        p->type=HFI_CMD_CLOSE;
        p->flags=mode==37?HFI_FW_FLAGS_SUCCESS:0;
        assert(!iris_hfi_pending_begin(&inst.control_pending,p->packet_id,HFI_CMD_CLOSE,p->port));
        iris_hfi_gen2_handle_session_close(&inst,p);
        assert(complete_calls==1);
        assert(inst.state==(mode==37?IRIS_INST_INIT:IRIS_INST_ERROR));
    } else if(mode==39||mode==40) {
        p->type=HFI_PROP_IMAGE_VERSION;p->size=mode==39?160:64;
        memset(p->payload,0,p->size-sizeof(*p));
        ((u8 *)p->payload)[0]='Z';
        int r=iris_hfi_gen2_handle_system_property(&core,p);
        assert(r==(mode==39?0:-EINVAL));
        if(mode==39) assert(strncmp(version,"firmware version: Z",19)==0);
    } else if(mode==41||mode==42) {
        p->type=HFI_CMD_STOP;p->port=HFI_PORT_RAW;
        p->flags=mode==41?HFI_FW_FLAGS_SUCCESS:0;
        assert(!iris_hfi_pending_begin(&inst.control_pending,p->packet_id,HFI_CMD_STOP,p->port));
        int ret=iris_hfi_gen2_handle_session_stop(&inst,p);
        assert(ret==(mode==41?0:-EIO));assert(complete_calls==1);
        assert(inst.state==(mode==41?IRIS_INST_INIT:IRIS_INST_ERROR));
    } else return 2;
    return 0;
}
'''


def fixture(work, output, sanitize=False):
    response = (work / 'iris_hfi_gen2_response.c').read_text()
    queue = (work / 'iris_hfi_queue.c').read_text()
    defs = ''.join(EXTRACT.aggregate_definition((work / 'iris_hfi_common.h').read_text(), name)
                   for name in ('hfi_packet_port_type', 'hfi_packet_payload_info'))
    defs += (work / 'iris_hfi_gen2_defines.h').read_text()
    defs += ''.join(EXTRACT.aggregate_definition((work / 'iris_hfi_gen2_packet.h').read_text(), name)
                    for name in ('iris_hfi_header', 'iris_hfi_packet', 'iris_hfi_buffer'))
    defs += ''.join(EXTRACT.aggregate_definition((work / 'iris_state.h').read_text(), name)
                    for name in ('iris_core_state', 'iris_inst_state'))
    defs += ''.join(EXTRACT.aggregate_definition((work / 'iris_hfi_queue.h').read_text(), name)
                    for name in ('iris_iface_queue', 'iris_hfi_queue_header'))
    functions = EXTRACT.definition(queue, 'iris_hfi_queue_read')
    functions += ''.join(EXTRACT.definition(response, name) for name in (
        'iris_hfi_gen2_validate_packet', 'iris_hfi_gen2_validate_hdr_packet',
        'iris_hfi_gen2_handle_system_init', 'iris_hfi_gen2_handle_session_close',
        'iris_hfi_gen2_handle_session_stop',
        'iris_hfi_gen2_handle_image_version_property', 'iris_hfi_gen2_handle_system_property'))
    # The existing extractor accepts bool separately through a small local adaptation.
    payload = response[response.index('static bool iris_hfi_gen2_validate_packet_payload('):]
    payload = payload[:payload.index('\n}\n') + 3]
    functions = payload + functions
    functions += EXTRACT.definition((work / 'iris_core.c').read_text(), 'iris_wait_for_system_response')
    functions += EXTRACT.definition((work / 'iris_utils.c').read_text(), 'iris_wait_for_session_response')
    source = output / 'actual-response.c'
    common = (work / 'iris_hfi_common.h').read_text()
    start = common.index('struct iris_hfi_pending {')
    tracker = common[start:common.index('enum hfi_packet_port_type {', start)]
    source.write_text(PREFIX.replace('/* TRACKER */', tracker) + defs + STUBS + functions + MAIN)
    binary = output / 'actual-response'
    args = ['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', str(source), '-o', str(binary)]
    if sanitize:
        args.extend(['-fsanitize=address,undefined', '-fno-omit-frame-pointer'])
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class ResponseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-response-repair-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = fixture(WORK, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


for mode in range(43):
    def test(self, case=mode):
        self.run_case(case)
    setattr(ResponseTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='iris-response-inputs-') as directory:
        if args.work:
            WORK = args.work
        else:
            spec = importlib.util.spec_from_file_location('builder', HERE / 'build.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            WORK = Path(directory)
            builder.prepare_iris(Path('/var/tmp/linux-src/linux-6.18.18'), WORK)
        if args.sanitize:
            binary = fixture(WORK, Path(directory), True)
            for mode in range(43):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
            print('43 ASan/UBSan cases passed')
            return 0
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ResponseTests))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
