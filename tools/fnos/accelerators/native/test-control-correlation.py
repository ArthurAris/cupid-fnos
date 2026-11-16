#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual blocking-control tracker and response handlers, with host lock stubs."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('extract', HERE / 'test-iris-behavior.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None
PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
typedef uint32_t u32;
typedef int spinlock_t;
#define spin_lock_init(lock) (*(lock)=0)
#define spin_lock_irqsave(lock, flags) ((void)(lock),(flags)=0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock),(void)(flags))
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 2
#define IRIS_INST_ERROR 5
#define HFI_CMD_INIT 0x01000001
#define HFI_CMD_CLOSE 0x01000004
#define HFI_CMD_STOP 0x01000006
#define HFI_FW_FLAGS_SUCCESS 1
#define HFI_PORT_RAW 2
#define HFI_PORT_BITSTREAM 1
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
'''
STUBS = r'''
struct completion {int done;};
struct iris_core {u32 state;struct iris_hfi_pending init_pending;struct completion core_init_done;int lock;};
#define mutex_lock(lock) (assert(!*(lock)),*(lock)=1)
#define mutex_unlock(lock) (assert(*(lock)),*(lock)=0)
struct iris_inst {u32 state;struct iris_hfi_pending control_pending;struct completion completion;};
struct iris_hfi_packet {u32 type,port,packet_id,flags;};
static int completes, pauses;
static void complete(struct completion *c) {c->done++;completes++;}
static void iris_inst_change_state(struct iris_inst *i,u32 s) {i->state=s;}
static int iris_inst_sub_state_change_pause(struct iris_inst *i,u32 p) {(void)i;(void)p;pauses++;return 0;}
'''
MAIN = r'''
int main(int argc,char **argv) {
 assert(argc==2);int mode=atoi(argv[1]);
 struct iris_core c={.state=IRIS_CORE_INIT};struct iris_inst i={0};
 struct iris_hfi_packet p={.type=HFI_CMD_INIT,.packet_id=7,.port=0,.flags=1};
 iris_hfi_pending_init(&c.init_pending);iris_hfi_pending_init(&i.control_pending);
 if(mode<=7) {
  assert(!iris_hfi_pending_begin(&c.init_pending,7,HFI_CMD_INIT,0));
  if(mode==1) p.packet_id=8;
  if(mode==2) p.port=1;
  if(mode==3) p.type=HFI_CMD_CLOSE;
  if(mode==4) iris_hfi_pending_cancel(&c.init_pending);
  if(mode==5) {iris_hfi_pending_cancel(&c.init_pending);assert(!iris_hfi_pending_begin(&c.init_pending,8,HFI_CMD_INIT,0));}
  if(mode==6) p.flags=0;
  if(mode==7) c.state=0;
  int r=iris_hfi_gen2_handle_system_init(&c,&p);
  if(mode==0||mode==6) {assert(!r&&completes==1);assert(iris_hfi_pending_active(&c.init_pending));}
  else assert(completes==0);
  if(mode==6) assert(c.state==IRIS_CORE_ERROR);
  if(mode==0) {assert(!iris_hfi_gen2_handle_system_init(&c,&p));assert(completes==1);}
 } else if(mode>=8&&mode<=15) {
  p.type=HFI_CMD_STOP;p.port=HFI_PORT_RAW;
  assert(!iris_hfi_pending_begin(&i.control_pending,7,HFI_CMD_STOP,HFI_PORT_RAW));
  if(mode==9) p.type=HFI_CMD_CLOSE;
  if(mode==10) p.port=HFI_PORT_BITSTREAM;
  if(mode==11) p.packet_id=8;
  if(mode==12) iris_hfi_pending_cancel(&i.control_pending);
  if(mode==13) {iris_hfi_pending_cancel(&i.control_pending);assert(!iris_hfi_pending_begin(&i.control_pending,8,HFI_CMD_STOP,HFI_PORT_RAW));}
  if(mode==14) p.flags=0;
  int r=iris_hfi_gen2_handle_session_stop(&i,&p);
  if(mode==8||mode==14||mode==15) {
   assert(completes==1);assert(r==(mode==14?-EIO:0));
   assert(pauses==(mode==14?0:1));
  } else assert(completes==0&&pauses==0);
  if(mode==15) {assert(!iris_hfi_gen2_handle_session_stop(&i,&p));assert(completes==1&&pauses==1);}
 } else if(mode>=16&&mode<=20) {
  p.type=HFI_CMD_CLOSE;p.port=0;
  assert(!iris_hfi_pending_begin(&i.control_pending,7,HFI_CMD_CLOSE,0));
  if(mode==17) p.packet_id=8;
  if(mode==18) p.flags=0;
  if(mode==19) iris_hfi_pending_cancel(&i.control_pending);
  iris_hfi_gen2_handle_session_close(&i,&p);
  assert(completes==((mode==16||mode==18||mode==20)?1:0));
  if(mode==18) assert(i.state==IRIS_INST_ERROR);
  if(mode==20) {iris_hfi_gen2_handle_session_close(&i,&p);assert(completes==1);}
 } else if(mode==21) {
  assert(!iris_hfi_pending_begin(&i.control_pending,7,HFI_CMD_STOP,2));
  assert(iris_hfi_pending_begin(&i.control_pending,8,HFI_CMD_CLOSE,0)==-EBUSY);
  assert(!iris_hfi_pending_match(&i.control_pending,8,HFI_CMD_CLOSE,0));
  assert(iris_hfi_pending_match(&i.control_pending,7,HFI_CMD_STOP,2));
  assert(iris_hfi_pending_begin(&i.control_pending,8,HFI_CMD_CLOSE,0)==-EBUSY);
  iris_hfi_pending_cancel(&i.control_pending);
  assert(!iris_hfi_pending_begin(&i.control_pending,8,HFI_CMD_CLOSE,0));
 } else return 2;
 return 0;
}
'''


def tracker_definition(work):
    text = (work / 'iris_hfi_common.h').read_text()
    start = text.index('struct iris_hfi_pending {')
    end = text.index('enum hfi_packet_port_type {', start)
    return text[start:end]


def fixture(work, directory, sanitize=False):
    common = tracker_definition(work)
    response = (work / 'iris_hfi_gen2_response.c').read_text()
    funcs = ''.join(EXTRACT.definition(response, name) for name in (
        'iris_hfi_gen2_handle_system_init', 'iris_hfi_gen2_handle_session_close', 'iris_hfi_gen2_handle_session_stop'))
    source = directory / 'correlation.c'
    source.write_text(PREFIX + common + STUBS + funcs + MAIN)
    binary = directory / 'correlation'
    args = ['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', str(source), '-o', str(binary)]
    if sanitize:
        args.extend(['-fsanitize=address,undefined', '-fno-omit-frame-pointer'])
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class CorrelationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-correlation-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = fixture(WORK, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation_rejected(self, before, after, mode):
        source = (Path(self.temp.name) / 'correlation.c').read_text()
        self.assertEqual(source.count(before), 1)
        with tempfile.TemporaryDirectory(prefix='correlation-mutation-') as directory:
            path = Path(directory)
            target, binary = path / 'mutant.c', path / 'mutant'
            target.write_text(source.replace(before, after, 1))
            result = subprocess.run(['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                                     '-O1', str(target), '-o', str(binary)],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Assertion', result.stderr)

    def test_oracle_rejects_unmatched_packet_id(self):
        self.mutation_rejected('pending->packet_id == packet_id',
                               '(pending->packet_id == packet_id || true)', 1)

    def test_oracle_rejects_duplicate_completion(self):
        self.mutation_rejected('pending->responded = true;', 'pending->responded = false;', 0)

    def test_oracle_rejects_lost_transaction_reservation(self):
        self.mutation_rejected('pending->responded = true;',
                               '{ pending->responded = true; pending->active = false; }', 21)


for mode in range(22):
    def test(self, value=mode):
        self.run_case(value)
    setattr(CorrelationTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='iris-correlation-inputs-') as directory:
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
            for mode in range(22):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
            print('22 correlation ASan/UBSan cases passed')
            return 0
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CorrelationTests))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
