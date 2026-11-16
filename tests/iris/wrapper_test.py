#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual C host logic; test facades enable branches, release gates remain zero."""
from pathlib import Path
exec(compile((Path(__file__).resolve().parent/'support.py').read_text(),str(Path(__file__).resolve().parent/'support.py'),'exec'))
def fn(name):return extract('iris_vidc.c',name)
h=r'''#include <stdint.h>
#include <stdbool.h>
#include <stdlib.h>
#include <stddef.h>
#include <string.h>
#include <assert.h>
#include <errno.h>
#include <stdio.h>
typedef uint32_t u32;typedef int32_t s32;
#define VIDEO_MAX_FRAME 32
#define BUF_BIN 3
#define BUF_TYPE_MAX 13
#define U32_MAX UINT32_MAX
#define READ_ONCE(x) (x)
#define __user
#define IRIS_NATIVE_REPEAT_RECLAIM_ENABLED 0
#define IRIS_REPEAT_RECEIPT_IOCTL 201
#define IRIS_REPEAT_CONTROL_IOCTL 202
#define IRIS_STOP_IOCTL 203
#define VIDIOC_STREAMOFF 204
#define VIDIOC_REQBUFS 205
#define VIDIOC_STREAMON 206
#define VIDIOC_CREATE_BUFS 207
#define VIDIOC_REMOVE_BUFS 208
#define VIDIOC_EXPBUF 209
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 10
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 9
#define V4L2_MEMORY_MMAP 1
#define IRIS_INST_ERROR 9
#define IRIS_INST_DEINIT 0
struct mutex{bool held;};
static void mutex_lock(struct mutex*m){assert(!m->held);m->held=true;}
static void mutex_unlock(struct mutex*m){assert(m->held);m->held=false;}
static void mutex_destroy(struct mutex*m){assert(!m->held);}
struct list_head{struct list_head*next,*prev;};
#define container_of(p,t,m) ((t *)((char*)(p)-offsetof(t,m)))
#define list_entry(p,t,m) container_of(p,t,m)
#define list_for_each_entry_safe(p,n,h,m) for(p=list_entry((h)->next,__typeof__(*p),m),n=list_entry(p->m.next,__typeof__(*p),m);&p->m!=(h);p=n,n=list_entry(n->m.next,__typeof__(*n),m))
struct iris_buffer{struct list_head list;};struct iris_buffers{struct list_head list;};
struct vb2_queue{u32 type;};enum v4l2_buf_type{HOST_TYPE_9=9,HOST_TYPE_10=10};
struct v4l2_requestbuffers{u32 count,type,memory;};struct iris_repeat_control{u32 version,session,generation,action;s32 result;u32 stage;};
struct iris_core{struct mutex lifecycle_lock,lock;bool native_repeat,decode_once,terminal_shutdown,decode_once_retention_ready;u32 open_owners,orphan_owners;};
struct iris_fh{void*ctrl_handler;};
struct iris_inst{struct iris_core*core;struct mutex lock,decode_ioctl_lock,ctx_q_lock;u32 session_id,repeat_lifecycle_generation,repeat_lifecycle_stage,state;bool stop_submit_blocked,repeat_cpu_drained,orphaned,file_detached;struct iris_fh fh;void*m2m_ctx,*m2m_dev;int ctrl_handler;struct iris_buffers buffers[BUF_TYPE_MAX];struct{void*dbuf[VIDEO_MAX_FRAME];bool attempted;}decode_pins[2];};
struct iris_inst_hfi_gen2{void*packet;};static struct iris_inst_hfi_gen2 gen;
#define to_iris_inst_hfi_gen2(i) ((void)(i),&gen)
struct file{struct iris_inst*inst;bool detached;};static struct iris_inst*iris_get_inst(struct file*f){return f->inst;}
static int copy_fault,framework,quiesce_calls,receipt_calls,pin_calls,free_calls,detach_calls;
static int copy_from_user(void*d,const void*s,size_t n){if(copy_fault)return 1;memcpy(d,s,n);return 0;}
static int copy_to_user(void*d,const void*s,size_t n){memcpy(d,s,n);return 0;}
#define get_user(v,p) (copy_fault?1:((v)=*(p),0))
static long iris_repeat_receipt_read(struct iris_inst*i,unsigned long a){(void)i;(void)a;receipt_calls++;return 0;}
static int iris_repeat_quiesce(struct iris_inst*i){quiesce_calls++;i->stop_submit_blocked=true;i->repeat_cpu_drained=true;i->repeat_lifecycle_stage=5;return 0;}
static int iris_stop_observer_command(struct iris_inst*i,unsigned long a){(void)i;(void)a;return -EOPNOTSUPP;}
static long video_ioctl2(struct file*f,unsigned int c,unsigned long a){(void)f;(void)c;(void)a;framework++;return 0;}
static struct vb2_queue queue;
static struct vb2_queue*v4l2_m2m_get_vq(void*c,u32 t){(void)c;queue.type=t;return &queue;}
static bool iris_decode_once_backing_pinned(struct iris_inst*i,struct vb2_queue*q){(void)i;(void)q;return true;}
static int iris_repeat_apps_owned(struct iris_inst*i,int p){(void)i;(void)p;return 0;}
#define V4L2_TYPE_IS_OUTPUT(t) ((t)==10)
static int vb2_is_streaming(struct vb2_queue*q){(void)q;return 0;}
static int vb2_get_num_buffers(struct vb2_queue*q){(void)q;return 0;}
static int v4l2_m2m_ioctl_reqbufs(struct file*f,void*h,struct v4l2_requestbuffers*r){(void)f;(void)h;(void)r;framework++;return 0;}
static int iris_decode_once_pin_queue(struct iris_inst*i,struct vb2_queue*q){(void)i;(void)q;pin_calls++;return 0;}
static int iris_destroy_internal_buffer(struct iris_inst*i,struct iris_buffer*b){(void)i;(void)b;free_calls++;return 0;}
static void dma_buf_put(void*p){(void)p;free_calls++;}
static void v4l2_m2m_ctx_release(void*p){(void)p;free_calls++;}
static void v4l2_m2m_release(void*p){(void)p;free_calls++;}
static void v4l2_ctrl_handler_free(void*p){(void)p;free_calls++;}
static void iris_vdec_inst_deinit(struct iris_inst*i){(void)i;free_calls++;}
static void iris_venc_inst_deinit(struct iris_inst*i){(void)i;free_calls++;}
static void iris_v4l2_fh_deinit(struct iris_inst*i,struct file*f){(void)i;(void)f;free_calls++;}
static void kfree(void*p){(void)p;free_calls++;}
static void iris_inst_change_state(struct iris_inst*i,u32 s){i->state=s;}
static void v4l2_fh_del(struct iris_fh*h,struct file*f){(void)h;f->detached=true;detach_calls++;}
static void iris_core_orphan_transfer(struct iris_core*c){assert(c->open_owners==1);c->open_owners--;c->orphan_owners++;}
static void iris_core_open_put(struct iris_core*c){(void)c;free_calls++;}
static bool iris_decode_once_retain_close(struct iris_inst*i){(void)i;return false;}
static struct vb2_queue*v4l2_m2m_get_src_vq(void*c){(void)c;return &queue;}
static struct vb2_queue*v4l2_m2m_get_dst_vq(void*c){(void)c;return &queue;}
static int vb2_streamoff(struct vb2_queue*q,u32 t){(void)q;(void)t;framework++;return 0;}
static int iris_session_close_checked(struct iris_inst*i){(void)i;quiesce_calls++;return 0;}
static int iris_destroy_all_internal_buffers(struct iris_inst*i,u32 t){(void)i;(void)t;free_calls++;return 0;}
static void iris_remove_session(struct iris_inst*i){(void)i;free_calls++;}
static void iris_put_response_instance(struct iris_inst*i){(void)i;free_calls++;}
struct {int response_idle;} unused;
#define wait_for_completion(p) ((void)(p))
static void iris_check_num_queued_internal_buffers(struct iris_inst*i,u32 t){(void)i;(void)t;}
static void get_device(void*p){(void)p;}
static void list_add_tail(struct list_head*h,struct list_head*p){(void)h;(void)p;}
'''
# Include enough real normal close facade to compile its unreachable nonnative path.
h=h.replace('bool native_repeat,decode_once,terminal_shutdown,decode_once_retention_ready;', 'bool native_repeat,decode_once,terminal_shutdown,decode_once_retention_ready;void*dev;struct list_head orphan_instances;')
h=h.replace('struct iris_core*core;struct mutex lock,decode_ioctl_lock,ctx_q_lock;', 'struct iris_core*core;struct list_head list;int response_idle;u32 domain;struct mutex lock,decode_ioctl_lock,ctx_q_lock;')
h+='#define DECODER 1\n#define ENCODER 2\n'
funcs=['iris_decode_once_reqbufs','iris_experiment_ioctl','iris_repeat_close','iris_close'];logic='\n'.join(fn(x)for x in funcs)
tests=r'''
static struct iris_core core;static struct iris_inst inst;static struct file file;
static void setup(bool drained){memset(&core,0,sizeof(core));memset(&inst,0,sizeof(inst));memset(&file,0,sizeof(file));core.native_repeat=core.decode_once=true;core.decode_once_retention_ready=true;core.open_owners=1;inst.core=&core;inst.session_id=1;inst.repeat_lifecycle_generation=1;inst.repeat_cpu_drained=drained;inst.stop_submit_blocked=drained;inst.state=4;file.inst=&inst;framework=quiesce_calls=receipt_calls=pin_calls=free_calls=detach_calls=copy_fault=0;}
int main(void){for(int stage=0;stage<2;stage++){setup(stage);struct v4l2_requestbuffers req={.type=10,.memory=1};assert(iris_experiment_ioctl(&file,VIDIOC_REQBUFS,(unsigned long)&req)==-EOPNOTSUPP);req.type=9;assert(iris_experiment_ioctl(&file,VIDIOC_REQBUFS,(unsigned long)&req)==-EOPNOTSUPP);assert(iris_experiment_ioctl(&file,VIDIOC_STREAMOFF,0)==-EOPNOTSUPP);assert(!framework&&!free_calls);assert(iris_decode_once_reqbufs(&file,NULL,&req)==-EOPNOTSUPP);assert(!framework&&!pin_calls);assert(!iris_experiment_ioctl(&file,IRIS_REPEAT_RECEIPT_IOCTL,0)&&receipt_calls==1);assert(iris_close(&file)==-EOPNOTSUPP);assert(file.detached&&inst.orphaned&&inst.file_detached&&core.orphan_owners==1&&!free_calls&&!quiesce_calls&&!framework);}
 setup(false);struct iris_repeat_control c={1,1,1,1,0,0};assert(!iris_experiment_ioctl(&file,IRIS_REPEAT_CONTROL_IOCTL,(unsigned long)&c));assert(c.stage==5&&quiesce_calls==1&&!framework&&!free_calls);assert(iris_close(&file)==-EOPNOTSUPP&&file.detached&&!free_calls);setup(false);c.session=2;assert(iris_experiment_ioctl(&file,IRIS_REPEAT_CONTROL_IOCTL,(unsigned long)&c)==-ESTALE&&!quiesce_calls);setup(false);struct v4l2_requestbuffers req={0,10,1};copy_fault=1;assert(iris_experiment_ioctl(&file,VIDIOC_REQBUFS,(unsigned long)&req)==-EFAULT&&!framework);
 puts("actual public ioctl/REQ0/close chain: reclaim0 denies both queue cancel commands before framework; control/receipt allowed; release/kill retains no free");return 0;}
'''
run_host('wrapper',h+logic+tests,['-Wno-unused-function'])
