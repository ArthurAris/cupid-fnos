#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Actual C host logic; test facades enable branches, release gates remain zero."""
from pathlib import Path
exec(compile((Path(__file__).resolve().parent/'support.py').read_text(),str(Path(__file__).resolve().parent/'support.py'),'exec'))
def fn(n,name):return extract(n,name)
def st(n,name):
 t=source_text(n);s=t.index('struct '+name+' {');return t[s:t.index('\n};',s)+3]
headers=r'''#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>
#include <assert.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <errno.h>
typedef uint32_t u32;typedef uint64_t u64;typedef int32_t s32;typedef unsigned char u8;
#define U32_MAX UINT32_MAX
#define BIT(x) (1U<<(x))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof(a[0]))
#define READ_ONCE(x) (x)
#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))
#define VIDEO_MAX_FRAME 32
#define IFACEQ_CORE_PKT_SIZE 4096
#define IRIS_FW_VERSION_LENGTH 256
#define DECODER 1
#define ENCODER 2
#define IRIS_CORE_INIT 1
#define IRIS_CORE_ERROR 9
#define IRIS_INST_ERROR 9
#define IRIS_INST_STREAMING 4
#define IRIS_INST_INPUT_STREAMING 2
#define IRIS_INST_SUB_DRC 4
#define IRIS_INST_SUB_DRC_LAST 8
#define HFI_PORT_NONE 0
#define HFI_PORT_BITSTREAM 1
#define HFI_PORT_RAW 2
#define HFI_PAYLOAD_NONE 0
#define HFI_PAYLOAD_U32 1
#define HFI_PAYLOAD_S32 2
#define HFI_PAYLOAD_U64 3
#define HFI_PAYLOAD_S64 4
#define HFI_PAYLOAD_STRUCTURE 5
#define HFI_PAYLOAD_U32_ENUM 6
#define HFI_PAYLOAD_Q16 7
#define HFI_PAYLOAD_32_PACKED 8
#define HFI_PAYLOAD_64_PACKED 9
#define BUF_ATTR_DEFERRED BIT(0)
#define BUF_ATTR_PENDING_RELEASE BIT(1)
#define BUF_ATTR_QUEUED BIT(2)
#define BUF_ATTR_DEQUEUED BIT(3)
#define BUF_ATTR_BUFFER_DONE BIT(4)
#define BUF_ATTR_READONLY BIT(5)
#define BUF_ATTR_RETURNED BIT(6)
#define BUF_INPUT 1
#define BUF_OUTPUT 2
#define BUF_BIN 3
#define BUF_ARP 4
#define BUF_COMV 5
#define BUF_NON_COMV 6
#define BUF_LINE 7
#define BUF_DPB 8
#define BUF_PERSIST 9
#define BUF_SCRATCH_2 11
#define BUF_VPSS 12
#define BUF_TYPE_MAX 13
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 10
#define IRIS_STOP_FAILURE_INPUT_MISSING 1
#define IRIS_STOP_FAILURE_INPUT_VALIDATE 2
#define IRIS_STOP_FAILURE_INPUT_FLAGS 3
#define IRIS_STOP_FAILURE_OUTPUT_MISSING 4
#define IRIS_STOP_FAILURE_OUTPUT_VALIDATE 5
#define IRIS_STOP_FAILURE_OUTPUT_FLAGS 6
#define IRIS_STOP_FAILURE_OUTPUT_DRAIN 7
#define IRIS_STOP_FAILURE_OUTPUT_DRC 8
#define IRIS_STOP_FAILURE_BUFFER_PAYLOAD 9
#define IRIS_STOP_FAILURE_PACKET_FLAGS 10
#define IRIS_STOP_FAILURE_BUFFER_TYPE 11
#define IRIS_STOP_FAILURE_BUFFER_PORT 12
#define IRIS_STOP_FAILURE_CLOSING 13
#define IRIS_STOP_FAILURE_PREERROR 14
#define IRIS_STOP_FAILURE_SOURCE_CHANGE 15
#define IRIS_STOP_FAILURE_SESSION_FLAGS 16
#define IRIS_STOP_FAILURE_SESSION_TYPE 17
#define IRIS_STOP_FAILURE_CALLBACK_RET 18
#define IRIS_STOP_FAILURE_CALLBACK_STATE 19
#define IRIS_STOP_FAILURE_DEQUEUE_RET 20
#define IRIS_STOP_FAILURE_DEQUEUE_STATE 21
#define IRIS_STOP_FAILURE_STOP_SUCCESS 22
#define IRIS_STOP_FAILURE_STOP_PAUSE 23
#define V4L2_BUF_FLAG_ERROR 1
#define IRIS_RELEASE_OBSERVER_ENABLED 1
#define lockdep_assert_held(x) assert((x)->held)
struct mutex {bool held;};
static void mutex_lock(struct mutex *m){assert(!m->held);m->held=true;}
static void mutex_unlock(struct mutex *m){assert(m->held);m->held=false;}
#define spin_lock_irqsave(l,f) do{(void)(l);(f)=0;}while(0)
#define spin_unlock_irqrestore(l,f) do{(void)(l);(void)(f);}while(0)
struct atomic {int value;};typedef struct atomic atomic_t;
#define atomic_inc(p) ((p)->value++)
#define atomic_add(n,p) ((p)->value+=(n))
#define atomic_set(p,n) ((p)->value=(n))
struct completion {int done;};static void complete(struct completion *c){c->done++;}
struct list_head {struct list_head *next,*prev;};
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define list_entry(p,t,m) container_of(p,t,m)
#define list_for_each_entry(p,h,m) for(p=list_entry((h)->next,__typeof__(*p),m);&p->m!=(h);p=list_entry(p->m.next,__typeof__(*p),m))
#define list_for_each_entry_safe(p,n,h,m) for(p=list_entry((h)->next,__typeof__(*p),m),n=list_entry(p->m.next,__typeof__(*p),m);&p->m!=(h);p=n,n=list_entry(n->m.next,__typeof__(*n),m))
static void list_init(struct list_head *h){h->next=h->prev=h;}
static void list_add(struct list_head *e,struct list_head *h){e->next=h;e->prev=h->prev;h->prev->next=e;h->prev=e;}
struct iris_inst;struct iris_core;struct iris_buffer;
'''
# Real firmware definitions, enum values, and exact wire/ledger structures.
headers+='\n'.join(l for l in source_text('iris_hfi_gen2_defines.h').splitlines()if not l.startswith('#include'))+'\n'
headers+='\n'.join(st(n,s)for n,s in [('iris_core.h','iris_repeat_owner'),('iris_core.h','iris_stop_transaction'),('iris_hfi_gen2_packet.h','iris_hfi_header'),('iris_hfi_gen2_packet.h','iris_hfi_packet'),('iris_hfi_gen2_packet.h','iris_hfi_buffer')])
headers+=r'''
struct iris_hfi_pending {int lock;bool active,responded;u32 type,port,packet_id;};
struct v4l2_m2m_buffer {struct list_head list;};
struct iris_buffer {struct v4l2_m2m_buffer vb;struct list_head list;struct iris_repeat_owner *repeat_owner;u32 type,index,buffer_size,attr,flags,data_offset,data_size;u64 device_addr,timestamp;bool experiment_retired;};
#define to_iris_buffer(x) container_of(x,struct iris_buffer,vb)
struct v4l2_m2m_ctx {struct list_head src,dst;};
#define v4l2_m2m_for_each_src_buf_safe(c,p,n) list_for_each_entry_safe(p,n,&(c)->src,list)
#define v4l2_m2m_for_each_dst_buf_safe(c,p,n) list_for_each_entry_safe(p,n,&(c)->dst,list)
struct iris_buffers {struct list_head list;};
struct iris_hfi_frame_info {int value;};
struct iris_platform {bool no_opb;};
struct iris_core {struct mutex lock;bool native_repeat,decode_once,stop_observer_enabled,release_observer_enabled;int state;struct iris_platform *iris_platform_data;
 atomic_t decode_progress_session_rx,decode_progress_session_state,decode_progress_in_enter,decode_progress_in_err,decode_progress_in_done,decode_progress_psc_seen;
};
struct iris_inst {struct iris_core *core;struct mutex lock;struct iris_hfi_pending control_pending;struct completion completion,flush_completion,stop_app_returned,repeat_internal_returned;struct iris_buffers buffers[BUF_TYPE_MAX];struct iris_repeat_owner repeat_app[2][VIDEO_MAX_FRAME];struct iris_stop_transaction repeat_stop[2];struct v4l2_m2m_ctx *m2m_ctx;u32 session_id,domain,stop_rx_header,stop_rx_packet,stop_rx_flags,state,sub_state,refs;bool response_closing,stop_attempted,stop_submit_blocked,decode_source_change_seen;struct {bool attempted;}decode_pins[2];struct iris_hfi_frame_info frame;};
struct iris_inst_hfi_gen2 {struct iris_hfi_frame_info hfi_frame_info;};
static struct iris_inst_hfi_gen2 gen;
#define to_iris_inst_hfi_gen2(i) (&gen)
struct iris_hfi_gen2_inst_hfi_range {u32 begin,end;int(*handle)(struct iris_inst *,struct iris_hfi_packet *);};
static struct iris_inst *routed;static int failures,legacy,observer_calls,apps_done,layout_calls,returns,system_failures;
static void iris_inst_change_state(struct iris_inst*i,int s){i->state=s;}
static void iris_vb2_queue_error(struct iris_inst*i){(void)i;failures++;}
static void iris_hfi_gen2_fail_async(struct iris_inst*i){(void)i;}
static struct iris_inst *iris_get_response_instance(struct iris_core*c,u32 s){if(!routed||routed->core!=c||routed->session_id!=s||routed->response_closing)return NULL;routed->refs++;return routed;}
static void iris_put_response_instance(struct iris_inst*i){assert(i->refs>1);i->refs--;}
static void iris_stop_failure_error(struct iris_inst*i,u32 r,int e){(void)i;(void)r;(void)e;}
static void iris_stop_failure_buffer_ret(struct iris_inst*i,int e){(void)i;(void)e;}
static void iris_stop_failure_message(struct iris_inst*i,struct iris_hfi_header*h){(void)i;(void)h;}
static void iris_input_comv_reply(struct iris_inst*i,struct iris_hfi_header*h){(void)i;(void)h;}
static void iris_decode_progress_classify(struct iris_inst*i,struct iris_hfi_header*h){(void)i;(void)h;}
static void iris_stop_failure_cursor(struct iris_inst*i,u32 o,u32 p){(void)i;(void)o;(void)p;}
static void iris_stop_failure_malformed(struct iris_core*c,void*p,u32 n,int e){(void)c;(void)p;(void)n;(void)e;}
static int iris_hfi_gen2_handle_system_error(struct iris_core*c,struct iris_hfi_packet*p){(void)p;c->state=IRIS_CORE_ERROR;system_failures++;return -EINVAL;}
static int iris_hfi_gen2_handle_system_response(struct iris_core*c,struct iris_hfi_header*h){(void)c;(void)h;return -EINVAL;}
static int iris_hfi_gen2_handle_session_error(struct iris_inst*i,struct iris_hfi_packet*p){(void)p;i->state=IRIS_INST_ERROR;failures++;return 0;}
static int iris_hfi_gen2_handle_session_info(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;return 0;}
static int iris_hfi_gen2_handle_session_property(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;return 0;}
static void iris_hfi_gen2_init_src_change_param(struct iris_inst*i){(void)i;layout_calls++;}
static int iris_stop_internal_return_consume(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;observer_calls++;return 0;}
static int iris_release_observer_consume(struct iris_inst*i,struct iris_hfi_header*h,struct iris_hfi_packet*p){(void)i;(void)h;(void)p;observer_calls++;return 0;}
static void iris_stop_observer_return(struct iris_inst*i,struct iris_buffer*b,struct iris_hfi_buffer*w){(void)i;(void)b;(void)w;}
static int iris_inst_sub_state_change_drain_last(struct iris_inst*i){(void)i;return 0;}
static int iris_inst_sub_state_change_drc_last(struct iris_inst*i){(void)i;return 0;}
static int iris_vb2_buffer_done(struct iris_inst*i,struct iris_buffer*b){(void)i;(void)b;apps_done++;return 0;}
static int iris_destroy_internal_buffer(struct iris_inst*i,struct iris_buffer*b){(void)i;(void)b;assert(0&&"native must not free through legacy");return -EINVAL;}
static int iris_hfi_gen2_handle_session_async(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;return 0;}
static int iris_hfi_gen2_handle_src_change(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;return 0;}
static int iris_hfi_gen2_handle_session_drain(struct iris_inst*i,struct iris_hfi_packet*p){(void)i;(void)p;return 0;}
static bool iris_hfi_pending_match(struct iris_hfi_pending*p,u32 id,u32 type,u32 port){if(!p->active||p->responded||p->packet_id!=id||p->type!=type||p->port!=port)return false;p->responded=true;return true;}
static int iris_inst_sub_state_change_pause(struct iris_inst*i,u32 p){(void)i;(void)p;return 0;}
struct {struct iris_stop_transaction transaction[2];atomic_t result_valid;} unused_stop;
#define smp_load_acquire(p) (*(p))
'''
# Actual stop handler references retained-only evidence: facade supplies it.
headers=headers.replace('struct iris_platform *iris_platform_data;','struct iris_platform *iris_platform_data;struct {struct iris_stop_transaction transaction[2];struct {int counter;} result_valid;} stop_evidence;')
# Compiler prototypes for actual mutually dependent response functions.
headers+='\nstatic int iris_repeat_app_return(struct iris_inst*,struct iris_buffer*,struct iris_hfi_buffer*);\nstatic int iris_hfi_gen2_handle_session_buffer(struct iris_inst*,struct iris_hfi_packet*);\n'
functions=[('iris_buffer.c',x)for x in ['iris_repeat_owner_submit','iris_repeat_owner_return','iris_repeat_apps_owned','iris_repeat_internal_owned','iris_mark_internal_buffer_returned']]+[('iris_hfi_gen2_command.c','iris_hfi_gen2_get_port_from_buf_type')]+[('iris_hfi_gen2_response.c',x)for x in ['iris_hfi_gen2_buf_type_to_driver','iris_hfi_gen2_is_valid_hfi_buffer_type','iris_hfi_gen2_is_valid_hfi_port','iris_hfi_gen2_get_driver_buffer_flags','iris_hfi_gen2_validate_packet_payload','iris_hfi_gen2_validate_packet','iris_hfi_gen2_validate_hdr_packet','iris_hfi_gen2_validate_buffer','iris_hfi_gen2_handle_session_close','iris_hfi_gen2_handle_input_buffer','iris_hfi_gen2_handle_output_buffer','iris_hfi_gen2_handle_dequeue_buffers','iris_repeat_internal_return','iris_repeat_app_return','iris_hfi_gen2_handle_release_internal_buffer','iris_hfi_gen2_handle_session_stop','iris_hfi_gen2_handle_session_buffer','iris_hfi_gen2_handle_session_command','iris_hfi_gen2_session_failure','iris_decode_once_source_change_admit','iris_hfi_gen2_handle_session_response','iris_hfi_gen2_handle_response']]
# Explicit facade corrections are host definitions, never altered driver functions.
headers=headers.replace('struct iris_inst;struct iris_core;struct iris_buffer;', 'enum iris_buffer_type { HOST_0,HOST_1,HOST_2,HOST_3,HOST_4,HOST_5,HOST_6,HOST_7,HOST_8,HOST_9,HOST_10,HOST_11,HOST_12,HOST_13 };\nstruct iris_inst;struct iris_core;struct iris_buffer;')
headers=headers.replace('struct iris_hfi_frame_info {int value;};','struct iris_hfi_frame_info {u32 picture_type;bool data_corrupt,overflow;};')
headers=headers.replace('struct v4l2_m2m_buffer {struct list_head list;};','struct v4l2_m2m_buffer {struct list_head list;int vb;};')
headers=headers.replace('container_of(x,struct iris_buffer,vb)','container_of(container_of(x,struct v4l2_m2m_buffer,vb),struct iris_buffer,vb)')
headers=headers.replace('bool native_repeat,decode_once,stop_observer_enabled,release_observer_enabled;', 'bool native_repeat,decode_once,stop_observer_enabled,release_observer_enabled,decode_once_admission,removing,core_ready,terminal_shutdown,irq_quiescing,recovery_blocked,firmware_shutdown_verified,irq_drain_verified,recovery_drain_verified;')
headers=headers.replace('session_id,domain,stop_rx_header','repeat_close_header,repeat_close_packet,session_id,domain,stop_rx_header')
headers=headers.replace('#define V4L2_BUF_FLAG_ERROR 1','#define V4L2_BUF_FLAG_ERROR 1\n#define V4L2_BUF_FLAG_KEYFRAME 8\n#define V4L2_BUF_FLAG_PFRAME 16\n#define V4L2_BUF_FLAG_BFRAME 32\n#define V4L2_BUF_FLAG_LAST 0x10000000')
headers=headers.replace('#define to_iris_inst_hfi_gen2(i) (&gen)','#define to_iris_inst_hfi_gen2(i) ((void)(i),&gen)')
logic='\n'.join(fn(n,s)for n,s in functions)
# Retained observer flags have a helper only on old branches; actual legacy handler
# stays in the translation unit and native dispatch must never call it.
tests=r'''
static struct iris_core core;static struct iris_inst inst;static struct iris_platform platform;static struct v4l2_m2m_ctx ctx;static struct iris_buffer obj[3];static struct iris_repeat_owner owner[3];
static void setup(u32 type){memset(&core,0,sizeof(core));memset(&inst,0,sizeof(inst));memset(obj,0,sizeof(obj));memset(owner,0,sizeof(owner));memset(&ctx,0,sizeof(ctx));core.native_repeat=true;core.decode_once=true;core.state=IRIS_CORE_INIT;core.iris_platform_data=&platform;inst.core=&core;inst.session_id=1;inst.domain=DECODER;inst.refs=1;inst.state=IRIS_INST_STREAMING;inst.stop_attempted=true;inst.stop_submit_blocked=true;inst.m2m_ctx=&ctx;list_init(&ctx.src);list_init(&ctx.dst);for(int t=0;t<BUF_TYPE_MAX;t++)list_init(&inst.buffers[t].list);routed=&inst;failures=legacy=observer_calls=apps_done=layout_calls=returns=system_failures=0;
 obj[0].type=type;obj[0].index=2;obj[0].device_addr=0x2000;obj[0].buffer_size=4096;obj[0].repeat_owner=&owner[0];obj[0].attr=BUF_ATTR_QUEUED;list_add(&obj[0].list,&inst.buffers[type].list);u32 wt=type==BUF_PERSIST?HFI_BUFFER_PERSIST:type==BUF_DPB?HFI_BUFFER_DPB:type==BUF_BIN?HFI_BUFFER_BIN:type==BUF_COMV?HFI_BUFFER_COMV:type==BUF_NON_COMV?HFI_BUFFER_NON_COMV:HFI_BUFFER_LINE;u32 port=iris_hfi_gen2_get_port_from_buf_type(&inst,type);assert(!iris_repeat_owner_submit(&owner[0],70,77,wt,port,0x2000,4096,0));
 inst.repeat_stop[port==HFI_PORT_RAW]=(struct iris_stop_transaction){.request_header=98,.request_packet=105,.port=port};inst.control_pending=(struct iris_hfi_pending){.active=true,.type=HFI_CMD_STOP,.packet_id=105,.port=port};}
static unsigned char *message(size_t body,u32 payload,u32 h,u32 p,u32 wt,u32 port,u32 bf,u32 flags,size_t *bytes){*bytes=sizeof(struct iris_hfi_header)+sizeof(struct iris_hfi_packet)+body;unsigned char *raw=calloc(1,*bytes);assert(raw);struct iris_hfi_header*hdr=(void*)raw;hdr->size=*bytes;hdr->num_packets=1;hdr->session_id=1;hdr->header_id=h;struct iris_hfi_packet*pkt=(void*)(raw+sizeof(*hdr));pkt->size=sizeof(*pkt)+body;pkt->type=HFI_CMD_BUFFER;pkt->payload_info=payload;pkt->packet_id=p;pkt->port=port;pkt->flags=flags;if(body>=sizeof(struct iris_hfi_buffer)){struct iris_hfi_buffer*w=(void*)pkt->payload;*w=(struct iris_hfi_buffer){.type=wt,.index=2,.base_address=0x2000,.buffer_size=4096,.flags=bf};}return raw;}
static int dispatch(size_t body,u32 payload,u32 h,u32 p,u32 wt,u32 port,u32 bf,u32 flags){size_t n;unsigned char *raw=message(body,payload,h,p,wt,port,bf,flags,&n);int ret=iris_hfi_gen2_handle_response(&core,raw,n);free(raw);assert(inst.refs==1&&!inst.lock.held);return ret;}
static void rejected(u32 before){assert(owner[0].fw_owned && owner[0].returned==0);assert(obj[0].attr==before);assert(observer_calls==0 && apps_done==0);}
int main(void){size_t wb=sizeof(struct iris_hfi_buffer);
 for(int type=BUF_BIN;type<=BUF_LINE;type++){if(type==BUF_ARP)continue;setup(type);assert(!dispatch(wb,HFI_PAYLOAD_STRUCTURE,98,105,owner[0].type,1,0,1));assert(!owner[0].fw_owned&&owner[0].returned==1&&obj[0].attr==BUF_ATTR_RETURNED);assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,98,105,owner[0].type,1,0,1)<0);assert(owner[0].returned==1);}
 setup(BUF_DPB);assert(!dispatch(wb,HFI_PAYLOAD_STRUCTURE,70,77,HFI_BUFFER_DPB,2,0,1));assert(!owner[0].fw_owned);
 setup(BUF_DPB);owner[0].header=71;owner[0].packet=78;assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,70,77,HFI_BUFFER_DPB,2,0,1)<0);rejected(BUF_ATTR_QUEUED);
 setup(BUF_BIN);inst.control_pending.responded=true;assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,98,105,HFI_BUFFER_BIN,1,0,1)<0);rejected(BUF_ATTR_QUEUED);
 for(int mutation=0;mutation<12;mutation++){setup(BUF_PERSIST);owner[0].release_requested=true;owner[0].request_generation=owner[0].generation;owner[0].request_header=39;owner[0].request_packet=46;obj[0].attr|=BUF_ATTR_PENDING_RELEASE;u32 h=39,p=46,port=0,wt=HFI_BUFFER_PERSIST,flags=1,bf=1;
 if(mutation==0)obj[0].attr&=~BUF_ATTR_QUEUED;if(mutation==1)obj[0].attr|=BUF_ATTR_DEQUEUED;if(mutation==2)obj[0].attr|=BUF_ATTR_BUFFER_DONE;if(mutation==3)obj[0].attr|=BUF_ATTR_DEFERRED;if(mutation==4)obj[0].attr|=BUF_ATTR_RETURNED;if(mutation==5)obj[0].attr&=~BUF_ATTR_PENDING_RELEASE;if(mutation==6)obj[0].attr|=BUF_ATTR_READONLY;if(mutation==7)obj[0].experiment_retired=true;if(mutation==8)owner[0].request_generation--;if(mutation==9)h--;if(mutation==10)port=2;if(mutation==11)wt=0xdead;
 u32 attrs=obj[0].attr;assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,h,p,wt,port,bf,flags)<0);rejected(attrs);}
 setup(BUF_PERSIST);owner[0].release_requested=true;owner[0].request_generation=owner[0].generation;owner[0].request_header=39;owner[0].request_packet=46;obj[0].attr|=BUF_ATTR_PENDING_RELEASE;assert(!dispatch(wb,HFI_PAYLOAD_STRUCTURE,39,46,HFI_BUFFER_PERSIST,0,17,1));assert(owner[0].readonly&&owner[0].fw_owned&&owner[0].returned==0);assert(obj[0].attr&BUF_ATTR_READONLY);assert(!dispatch(wb,HFI_PAYLOAD_STRUCTURE,39,46,HFI_BUFFER_PERSIST,0,1,1));assert(!owner[0].fw_owned&&owner[0].returned==1&&obj[0].attr==BUF_ATTR_RETURNED);
 setup(BUF_BIN);obj[0].attr|=BUF_ATTR_PENDING_RELEASE;owner[0].release_requested=true;owner[0].request_header=39;owner[0].request_packet=46;owner[0].request_generation=owner[0].generation;inst.stop_submit_blocked=false;assert(!dispatch(wb,HFI_PAYLOAD_STRUCTURE,39,46,HFI_BUFFER_BIN,1,1,1));assert(obj[0].experiment_retired);obj[1]=obj[0];obj[1].index=3;obj[1].device_addr=0x4000;obj[1].repeat_owner=&owner[1];obj[1].attr=BUF_ATTR_QUEUED;obj[1].experiment_retired=false;assert(!iris_repeat_owner_submit(&owner[1],80,87,HFI_BUFFER_BIN,1,0x4000,4096,0));list_add(&obj[1].list,&inst.buffers[BUF_BIN].list);mutex_lock(&inst.lock);assert(iris_repeat_internal_owned(&inst));mutex_unlock(&inst.lock);u32 oldattr=obj[0].attr;assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,39,46,HFI_BUFFER_BIN,1,1,1)<0);assert(obj[0].attr==oldattr&&owner[1].fw_owned);
 for(int variant=0;variant<5;variant++)for(int observed=0;observed<2;observed++){setup(BUF_PERSIST);core.release_observer_enabled=observed;owner[0].release_requested=true;owner[0].request_generation=1;owner[0].request_header=39;owner[0].request_packet=46;obj[0].attr|=BUF_ATTR_PENDING_RELEASE;size_t size=variant==0?0:variant==1?4:variant==2?wb-4:variant==3?wb+4:wb;u32 info=variant==4?HFI_PAYLOAD_NONE:HFI_PAYLOAD_STRUCTURE;assert(dispatch(size,info,39,46,HFI_BUFFER_PERSIST,0,1,1)<0);rejected(BUF_ATTR_QUEUED|BUF_ATTR_PENDING_RELEASE);}
 setup(BUF_BIN);assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,98,105,HFI_BUFFER_BIN,1,HFI_BUF_FW_FLAG_READONLY,1)<0);rejected(BUF_ATTR_QUEUED);
 for(int flags=0;flags<16;flags++){if(flags==1||flags==3)continue;setup(BUF_BIN);assert(dispatch(wb,HFI_PAYLOAD_STRUCTURE,98,105,HFI_BUFFER_BIN,1,0,flags)<0);rejected(BUF_ATTR_QUEUED);}
 setup(BUF_BIN);size_t n;unsigned char*raw=message(wb,HFI_PAYLOAD_STRUCTURE,98,105,HFI_BUFFER_BIN,1,0,1,&n);((struct iris_hfi_header*)raw)->session_id=2;assert(iris_hfi_gen2_handle_response(&core,raw,n)<0);free(raw);rejected(BUF_ATTR_QUEUED);
 setup(BUF_BIN);inst.stop_attempted=false;obj[0].type=BUF_OUTPUT;obj[0].buffer_size=24576;obj[0].device_addr=0x6000;obj[0].index=1;list_add(&obj[0].vb.list,&ctx.dst);memset(&owner[0],0,sizeof(owner[0]));assert(!iris_repeat_owner_submit(&inst.repeat_app[1][1],79,86,HFI_BUFFER_RAW,2,0x6000,24576,0));
 size_t appn;unsigned char*appraw=message(wb,HFI_PAYLOAD_STRUCTURE,79,86,HFI_BUFFER_RAW,2,0,1,&appn);struct iris_hfi_buffer*appwire=(void*)((struct iris_hfi_packet*)(appraw+sizeof(struct iris_hfi_header)))->payload;appwire->index=1;appwire->base_address=0x6000;appwire->buffer_size=24576;appwire->data_size=24576;assert(!iris_hfi_gen2_handle_response(&core,appraw,appn));assert(apps_done==1&&inst.repeat_app[1][1].returned==1);obj[0].attr=BUF_ATTR_QUEUED;assert(!iris_repeat_owner_submit(&inst.repeat_app[1][1],95,102,HFI_BUFFER_RAW,2,0x6000,24576,0));((struct iris_hfi_header*)appraw)->header_id=95;((struct iris_hfi_packet*)(appraw+sizeof(struct iris_hfi_header)))->packet_id=102;appwire->flags=HFI_BUF_FW_FLAG_LAST;assert(!iris_hfi_gen2_handle_response(&core,appraw,appn));assert(apps_done==2&&inst.repeat_app[1][1].returned==2&&inst.repeat_app[1][1].rx_header==95&&inst.repeat_app[1][1].rx_packet==102&&!(inst.repeat_app[1][1].readonly));free(appraw);
 puts("actual complete response/strict internal/owner chain: STOP4/DPB/PERSIST/PSC/APP latest LAST and malformed/attrs/readonly/stale/oldgeneration passed; no old observer/free fallback");return 0;}
'''
run_host('response',headers+logic+tests,['-Wno-sign-compare','-Wno-unused-variable','-Wno-unused-function','-Wno-misleading-indentation'])
