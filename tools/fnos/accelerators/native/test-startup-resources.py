#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual boot-poll and DMA queue allocation/free; not device firmware acceptance."""
import argparse
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE=Path(__file__).resolve().parent
SPEC=importlib.util.spec_from_file_location('extract',HERE/'test-decode-cleanup.py')
EXTRACT=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(EXTRACT)
WORK=None
CASES=11
LAST_POLL=8
PREFIX=r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
typedef uint32_t u32;typedef uintptr_t dma_addr_t;
#define BIT(n) (1u<<(n))
#define GFP_KERNEL 0
#define DMA_ATTR_WRITE_COMBINE 1
#define SZ_4K 4096
#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))
#define IFACEQ_QUEUE_SIZE 1024
#define IFACEQ_NUMQ 3
#define SFR_SIZE 512
#define IFACEQ_CMDQ_ID 0
#define IFACEQ_MSGQ_ID 1
#define IFACEQ_DBGQ_ID 2
#define IFACEQ_DFLT_QHDR 7
#define CTRL_INIT 16
#define CTRL_STATUS 20
#define CTRL_ERROR_STATUS__M 0xfe
#define CPU_CS_SCIACMDARG3 24
#define CPU_CS_H2XSOFTINTEN 28
#define CPU_CS_X2RPMH 32
#define HOST2XTENSA_INTR_ENABLE 1
#define dev_err(d,...) ((void)(d))
struct iris_hfi_queue_header {u32 status,start_addr,header_type,queue_type,q_size,pkt_size,rx_wm,tx_wm,rx_req,tx_req,rx_irq_status,tx_irq_status,read_idx,write_idx;};
struct iris_hfi_queue_table_header {u32 version;void *device_addr;char name[32];u32 size,qhdr0_offset,qhdr_size,num_q,num_active_q;struct iris_hfi_queue_header q_hdr[3];};
struct iris_iface_q_info {dma_addr_t device_addr;void *kernel_vaddr;struct iris_hfi_queue_header *qhdr;};
struct iris_core {void *dev,*reg_base,*iface_q_table_vaddr,*sfr_vaddr;dma_addr_t iface_q_table_daddr,sfr_daddr;struct iris_iface_q_info command_queue,message_queue,debug_queue;};
static struct iris_core core;
static u32 statuses[1000];
static int status_count;
static int allocs,frees,live,fail_at,polls,host_enables,setup_calls;
static size_t sizes[2];static void *ptrs[2];
static void *dma_alloc_attrs(void *dev,size_t size,dma_addr_t *addr,int flags,int attrs){assert(dev==&core&&flags==0&&attrs==1);int id=allocs++;if(allocs==fail_at)return NULL;assert(id<2);void *p=calloc(1,size);assert(p);ptrs[id]=p;sizes[id]=size;*addr=(dma_addr_t)p;live++;return p;}
static void dma_free_attrs(void *dev,size_t size,void *p,dma_addr_t addr,int attrs){assert(dev==&core&&attrs==1&&addr==(dma_addr_t)p);int id=ptrs[0]==p?0:1;assert(ptrs[id]==p&&sizes[id]==size);ptrs[id]=NULL;frees++;live--;free(p);}
static void strscpy(char *d,const char *s,size_t n){assert(strlen(s)<n);memcpy(d,s,strlen(s)+1);}
static void iris_vpu_setup_ucregion_memory_map(struct iris_core *c){assert(c==&core);setup_calls++;}
static void writel(u32 value,void *addr){uintptr_t off=(uintptr_t)addr-(uintptr_t)core.reg_base;if(off==CPU_CS_H2XSOFTINTEN){assert(value==1);host_enables++;}}
static u32 readl(void *addr){assert((uintptr_t)addr-(uintptr_t)core.reg_base==CTRL_STATUS);assert(polls<status_count);return statuses[polls++];}
static void usleep_range(int a,int b){assert(a==50&&b==100);}
'''
MAIN=r'''
int main(int argc,char **argv){assert(argc==2);int mode=atoi(argv[1]);assert(mode>=0&&mode<11);core.dev=&core;core.reg_base=(void *)4096;
 if(mode<=2||mode>=8){
  /* The first status_count-1 reads stay zero; only the scripted last read
   * can complete boot. Poll 1001 or a read after completion is a hard error. */
  int expected=0;status_count=1;
  if(mode==0)statuses[0]=1;
  if(mode==1){statuses[0]=4;expected=-EINVAL;}
  if(mode==2){status_count=1000;expected=-ETIME;}
  if(mode==8){status_count=1000;statuses[999]=1;}
  if(mode==9){status_count=999;statuses[998]=1;}
  if(mode==10){status_count=1000;statuses[999]=4;expected=-EINVAL;}
  int r=iris_vpu_boot_firmware(&core);assert(r==expected);
  assert(polls==status_count&&setup_calls==1&&host_enables==(expected==0));return 0;
 }
 if(mode==3)fail_at=1;
 if(mode==4)fail_at=2;
 int r=iris_hfi_queues_init(&core);assert(r==((mode==3||mode==4)?-ENOMEM:0));
 if(mode==3){assert(!live&&!frees&&!core.iface_q_table_vaddr);return 0;}
 if(mode==4){assert(!live&&frees==1&&!core.iface_q_table_vaddr&&!core.iface_q_table_daddr&&!core.sfr_daddr);iris_hfi_queues_deinit(&core);assert(frees==1);return 0;}
 assert(live==2&&core.command_queue.qhdr&&core.message_queue.qhdr&&core.debug_queue.qhdr);assert(core.message_queue.qhdr->rx_req==1&&core.debug_queue.qhdr->rx_req==0);iris_hfi_queues_deinit(&core);assert(!live&&frees==2&&!core.iface_q_table_vaddr&&!core.iface_q_table_daddr&&!core.sfr_vaddr&&!core.sfr_daddr&&!core.message_queue.qhdr);iris_hfi_queues_deinit(&core);assert(frees==2);
 if(mode==6){allocs=0;assert(!iris_hfi_queues_init(&core));assert(live==2);iris_hfi_queues_deinit(&core);assert(!live&&frees==4);}
 if(mode==7){assert(!core.command_queue.kernel_vaddr&&!core.debug_queue.device_addr);}
 return 0;}
'''


def source(work):
    boot=(work/'iris_vpu_common.c').read_text();queue=(work/'iris_hfi_queue.c').read_text()
    funcs=EXTRACT.definition(boot,'iris_vpu_boot_firmware')
    funcs+=EXTRACT.definition(queue,'iris_hfi_queue_set_header')
    funcs+=EXTRACT.block(queue,r'^static void\s+iris_hfi_queue_init\([^;{}]*\)\s*\{','queue init')
    funcs+=EXTRACT.definition(queue,'iris_hfi_queue_deinit')+EXTRACT.definition(queue,'iris_hfi_queues_init')+EXTRACT.definition(queue,'iris_hfi_queues_deinit')
    return PREFIX+funcs+MAIN


def compile_fixture(text,out,sanitize=False,historical=False):
    src=out/'resources.c';src.write_text(text);exe=out/'resources';cmd=['/usr/bin/gcc','-std=gnu11','-Wall','-Wextra','-Werror','-O1',str(src),'-o',str(exe)]
    if historical:cmd+=['-Wno-unused-parameter']  # Preserve old queue helper verbatim for baseline only.
    if sanitize:cmd+=['-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie']
    r=subprocess.run(cmd,capture_output=True,text=True,timeout=40)
    if r.returncode:raise RuntimeError(r.stdout+r.stderr)
    return exe


class ResourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='iris-start-resources-');cls.addClassCleanup(cls.temp.cleanup);cls.source=source(WORK);cls.exe=compile_fixture(cls.source,Path(cls.temp.name))
    def run_case(self,mode):
        r=subprocess.run([str(self.exe),str(mode)],capture_output=True,text=True,timeout=5);self.assertEqual(r.returncode,0,r.stdout+r.stderr)
    def mutation(self,before,after,mode):
        self.assertEqual(self.source.count(before),1)
        with tempfile.TemporaryDirectory(prefix='iris-start-resources-mutation-') as d:
            exe=compile_fixture(self.source.replace(before,after,1),Path(d));r=subprocess.run([str(exe),str(mode)],capture_output=True,text=True,timeout=5);self.assertNotEqual(r.returncode,0);self.assertIn('Assertion',r.stderr)
    def test_oracle_invalid_boot(self):self.mutation('return -EINVAL;','break;',1)
    def test_oracle_final_poll_success(self):
        self.mutation('if (!ctrl_status) {','if (count >= max_tries) {',LAST_POLL)
    def test_oracle_queue_free_size(self):
        self.mutation('dma_free_attrs(core->dev, queue_size, core->iface_q_table_vaddr,\n\t\t\t       core->iface_q_table_daddr, DMA_ATTR_WRITE_COMBINE);',
                      'dma_free_attrs(core->dev, sizeof(*q_tbl_hdr), core->iface_q_table_vaddr,\n\t\t\t       core->iface_q_table_daddr, DMA_ATTR_WRITE_COMBINE);',4)


for mode in range(CASES):
    def test(self,value=mode):self.run_case(value)
    setattr(ResourceTests,'test_case_%02d'%mode,test)


def main():
    global WORK
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--work',type=Path,required=True)
    p.add_argument('--sanitize',action='store_true')
    baseline=p.add_mutually_exclusive_group()
    baseline.add_argument('--baseline',action='store_true',
                          help='Require invalid-boot, wrong-free-size and final-poll-success assertions on historical source')
    baseline.add_argument('--baseline-last-poll',action='store_true',
                          help='Require only the final-poll-success assertion on source with the timeout boundary bug')
    args=p.parse_args();WORK=args.work
    historical=args.baseline or args.baseline_last_poll
    if historical or args.sanitize:
        with tempfile.TemporaryDirectory(prefix='iris-start-resources-run-') as d:
            exe=compile_fixture(source(WORK),Path(d),args.sanitize,historical);ok=True
            modes=(1,4,LAST_POLL) if args.baseline else ((LAST_POLL,) if historical else range(CASES))
            for mode in modes:
                r=subprocess.run([str(exe),str(mode)],capture_output=True,text=True,timeout=5,env={'PATH':'/usr/bin:/bin','ASAN_OPTIONS':'detect_leaks=1:halt_on_error=1','UBSAN_OPTIONS':'halt_on_error=1'})
                if historical:
                    print('baseline',mode,r.returncode,r.stderr.strip())
                    ok=ok and r.returncode!=0 and 'Assertion' in r.stderr
                elif r.returncode:print(r.stdout+r.stderr);return 1
            if historical:
                if ok:print('%d expected boot/queue baseline assertion failures reproduced'%len(modes))
                else:print('baseline did not reproduce every required assertion failure')
            else:print('%d boot/queue allocation ASan/UBSan scenarios passed; no firmware/DMA hardware proof'%CASES)
            return 0 if ok else 1
    r=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ResourceTests))
    if r.wasSuccessful():print('%d boot/queue allocation scenarios and 3 compiling mutation oracles passed; no firmware/DMA hardware proof'%CASES)
    return 0 if r.wasSuccessful() else 1


if __name__=='__main__':raise SystemExit(main())
