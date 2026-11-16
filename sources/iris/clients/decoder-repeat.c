/* SPDX-License-Identifier: GPL-2.0-only
 * PRIVATE fixed-fixture repeat prototype; future ABI unresolved, NOT device-admitted.
 * Existing enumerate-v4l2.c is capability-only and is NOT this client.
 * FIR1 = magic,u32le count,(u32le AU size, AU bytes)*.
 * FNV1 = magic,u32le width,height,count, tightly packed NV12 frames.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/klog.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include <signal.h>
#include <sys/sysmacros.h>
#include <stdarg.h>
#include <poll.h>
#include "future-abi.h"
#include "supervisor-sha256.h"

#define MAX_BUFS 32
#define MAX_BYTES (8U*1024U*1024U)
#define MAX_FRAMES 16
/* LOCAL prototype structs, NOT a driver wire ABI. Adapters remain unresolved. */
struct identity {uint64_t session,generation;unsigned node_major,node_minor;};
struct receipt_slot {unsigned submitted,returned,matched,outstanding,readonly,unknown;};
struct receipt {
 unsigned version,status,internal_pending,release_wait,cpu_pending,cpu_active;
 uint64_t session,generation;
 unsigned request_header[2],request_packet[2],reply_header[2],reply_packet[2];
 unsigned slots[2];struct receipt_slot app[2][MAX_BUFS];
};
struct queue;
struct ops {
 int (*open)(const char *,int); int (*close)(int);
 int (*ioctl)(int,unsigned long,void *);
 void *(*map)(void *,size_t,int,int,int,off_t);
 int (*unmap)(void *,size_t); double (*now)(void);
 int (*node)(int,struct stat *);
 /* These callbacks inspect immutable main-configured pins and read-only kernel state.
  * Peer tokens are never execution authority. real callbacks remain fail-closed. */
 int (*guard)(int,unsigned,struct identity *,double);
 /* Single constrained checked lifecycle action BEFORE STREAMOFF; receipt is then
  * read-only evidence of actual correlated app returns and internal safety. */
 int (*checked_lifecycle)(int,unsigned,const struct queue *,const struct queue *,const struct identity *,struct receipt *,double);
 void (*idle)(void);
};
struct mapping { void *p; size_t len; bool queued; unsigned submitted; };
struct queue { enum v4l2_buf_type type; struct mapping b[MAX_BUFS]; unsigned n,mapped; bool requested,stream; };
struct data { unsigned char *p; size_t n; };
struct result {
 unsigned frames,drc,output_dq,capture_dq,out_maps,cap_maps,unmapped;
 unsigned streamoff_ok,reqbufs0_ok; int close_rc; bool opened,eos,timeout,cleanup_ok,contract_observed;
 int error; unsigned pts[3]; char hash[3][65];
};
static double real_now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static void *real_map(void *p,size_t n,int prot,int flags,int fd,off_t off) {return mmap(p,n,prot,flags,fd,off);}
static int real_ioctl(int fd,unsigned long r,void *p) {return ioctl(fd,r,p);}
static int real_open(const char *path,int flags) {return open(path,flags);}
/* Failure retains fd + mappings and returns to the outer HOLD loop. SIGKILL/crash
 * still invokes kernel release: userspace HOLD is NOT DMA safety. Success is
 * conditional on independently matched kernel per-buffer contract, NOT close().
 * No retry on any error (including EINTR); no next cycle after any failure.
 * 30s/600s checked at each operation boundary; a stuck kernel syscall cannot be
 * safely preempted by userspace. Future driver checked contract must bound waits. */
static FILE *trace_file;
static int evidence_fd = -1;
static unsigned trace_seq, input_pts_index, cycle_index;
static bool evidence_bad;
static unsigned long long process_starttime;
static uint64_t previous_session,previous_generation;
static int trace_sync(void) {
 if(!trace_file || fflush(trace_file) || fsync(fileno(trace_file))) {evidence_bad=true;errno=EIO;return -1;}
 return 0;
}
static int record(const char *fmt,...) {
 if(!trace_file){errno=EIO;return -1;}
 va_list a;va_start(a,fmt);int r=vfprintf(trace_file,fmt,a);va_end(a);
 if(r<0){evidence_bad=true;errno=EIO;return -1;}return trace_sync();
}
static const unsigned pts_us[3] = {1000000,1040000,1080000};
static unsigned seen_pts;
static int write_all(int fd,const void *v,size_t n) {
 const unsigned char *p=v;
 while(n){ssize_t r=write(fd,p,n);if(r<0&&errno==EINTR)continue;if(r<=0)return -1;p+=r;n-=(size_t)r;}
 return 0;
}
static int save_capture(const struct mapping *m,const struct v4l2_plane *p,unsigned frame,unsigned w,unsigned h,unsigned stride,unsigned rows,char digest[65]) {
 size_t uv=(size_t)stride*rows,need=uv+(size_t)stride*(h/2-1)+w;
 if(p->data_offset>p->bytesused||p->bytesused>m->len||need>p->bytesused-p->data_offset){errno=EPROTO;return -1;}
 const unsigned char *src=(const unsigned char *)m->p+p->data_offset;
 /* Hash actual captured visible bytes, not the oracle. Later cycles never write raw frames. */
 unsigned char visible[24576];
 for(unsigned y=0;y<h;y++)memcpy(visible+(size_t)y*w,src+(size_t)y*stride,w);
 for(unsigned y=0;y<h/2;y++)memcpy(visible+(size_t)w*h+(size_t)y*w,src+uv+(size_t)y*stride,w);
 unsigned char hash[32];hash_memory(visible,sizeof(visible),hash);
 for(unsigned i=0;i<32;i++)snprintf(digest+2*i,3,"%02x",hash[i]);
 if(cycle_index)return 0;
 char name[64];snprintf(name,sizeof(name),"capture-%02u-visible.nv12",frame);
 int fd=openat(evidence_fd,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
 if(fd<0)return -1;int bad=write_all(fd,visible,sizeof(visible));
 if(fsync(fd))bad=-1;if(close(fd))bad=-1;return bad;
}
static int real_node(int fd,struct stat *s){return fstat(fd,s);}
static void real_idle(void){struct timespec t={0,1000000};nanosleep(&t,NULL);}
static int real_guard(int fd,unsigned cycle,struct identity *id,double end) {
 (void)fd;(void)cycle;(void)id;(void)end;
 /* Do not invent a driver ABI. This release has immutable unresolved pins.
  * Main must define + review an exact read-only kernel token adapter before use. */
 errno=ENOTSUP;return -1;
}
static int real_checked_lifecycle(int fd,unsigned cycle,const struct queue *out,const struct queue *cap,const struct identity *id,struct receipt *r,double end) {
 (void)fd;(void)cycle;(void)out;(void)cap;(void)id;(void)r;(void)end;
 /* Must validate STOP reply identifiers + all submitted per-buffer matched returns,
  * outstanding/readonly/unknown zero, before REQBUFS0/unmap/close. NOT global DMA. */
 errno=ENOTSUP;return -1;
}
static const struct ops real_ops={real_open,close,real_ioctl,real_map,munmap,real_now,real_node,real_guard,real_checked_lifecycle,real_idle};
static uint32_t le32(const unsigned char *p) {return p[0]|(uint32_t)p[1]<<8|(uint32_t)p[2]<<16|(uint32_t)p[3]<<24;}
static int load(const char *path,struct data *d) {
 struct stat s; int fd=open(path,O_RDONLY|O_CLOEXEC|O_NOFOLLOW);
 if(fd<0)return -1;
 if(fstat(fd,&s)||!S_ISREG(s.st_mode)||s.st_size<16||s.st_size>MAX_BYTES) {close(fd);return -1;}
 d->n=(size_t)s.st_size; d->p=malloc(d->n); size_t done=0;
 if(!d->p){close(fd);return -1;}
 while(done<d->n){ssize_t n=read(fd,d->p+done,d->n-done);if(n<0&&errno==EINTR)continue;if(n<=0){close(fd);free(d->p);d->p=NULL;return -1;}done+=(size_t)n;}
 close(fd);return 0;
}
static int call(const struct ops *o,int fd,unsigned long req,void *p,double end) {
 if(evidence_bad){errno=EIO;return -1;}
 if(o->now()>=end){errno=ETIMEDOUT;return -1;}
 if(trace_seq>=131072){errno=EOVERFLOW;return -1;}
 int r=o->ioctl(fd,req,p);int saved=r<0?errno:0;
 if(trace_file) {
  ++trace_seq;
  fprintf(trace_file,"{\"seq\":%u,\"monotonic\":%.9f,\"request\":%lu,\"rc\":%d,\"errno\":%d",trace_seq,o->now(),req,r,saved);
  if(req==VIDIOC_QBUF||req==VIDIOC_DQBUF){struct v4l2_buffer *b=p;struct v4l2_plane *pl=b->m.planes;
   fprintf(trace_file,",\"type\":%u,\"index\":%u,\"flags\":%u,\"sequence\":%u,\"pts_us\":%lld,\"planes\":%u,\"length\":%u,\"bytesused\":%u,\"data_offset\":%u",b->type,b->index,b->flags,b->sequence,(long long)b->timestamp.tv_sec*1000000+b->timestamp.tv_usec,b->length,pl->length,pl->bytesused,pl->data_offset);
  }
  if(req==VIDIOC_S_FMT||req==VIDIOC_G_FMT){struct v4l2_format *f=p;struct v4l2_pix_format_mplane *m=&f->fmt.pix_mp;
   fprintf(trace_file,",\"type\":%u,\"fourcc\":%u,\"width\":%u,\"height\":%u,\"planes\":%u,\"stride\":%u,\"sizeimage\":%u",f->type,m->pixelformat,m->width,m->height,m->num_planes,m->plane_fmt[0].bytesperline,m->plane_fmt[0].sizeimage);
  }
  if(req==VIDIOC_REQBUFS){struct v4l2_requestbuffers *b=p;fprintf(trace_file,",\"type\":%u,\"count\":%u,\"memory\":%u",b->type,b->count,b->memory);}
  if(req==VIDIOC_STREAMON||req==VIDIOC_STREAMOFF)fprintf(trace_file,",\"type\":%u",*(unsigned *)p);
  if(req==VIDIOC_DECODER_CMD){struct v4l2_decoder_cmd *d=p;fprintf(trace_file,",\"decoder_cmd\":%u,\"flags\":%u",d->cmd,d->flags);}
  if(req==VIDIOC_G_SELECTION){struct v4l2_selection *s=p;fprintf(trace_file,",\"type\":%u,\"target\":%u,\"left\":%d,\"top\":%d,\"width\":%u,\"height\":%u",s->type,s->target,s->r.left,s->r.top,s->r.width,s->r.height);}
  if(req==VIDIOC_DQEVENT&&r>=0){struct v4l2_event *e=p;fprintf(trace_file,",\"event\":%u,\"changes\":%u",e->type,e->u.src_change.changes);}
  fprintf(trace_file,"}\n");if(trace_sync())return -1;
 }
 errno=saved;return r;
}
static int allocate(const struct ops *o,int fd,struct queue *q,double end) {
 struct v4l2_requestbuffers r={.count=16,.type=q->type,.memory=V4L2_MEMORY_MMAP};
 if(call(o,fd,VIDIOC_REQBUFS,&r,end))return -1;
 q->requested=true;if(r.count<16||r.count>MAX_BUFS){errno=EINVAL;return -1;}q->n=r.count;
 for(unsigned i=0;i<q->n;i++){
  struct v4l2_plane p={0};struct v4l2_buffer b={.type=q->type,.memory=V4L2_MEMORY_MMAP,.index=i,.length=1,.m.planes=&p};
  if(call(o,fd,VIDIOC_QUERYBUF,&b,end))return -1;
  if(b.length!=1||!p.length||p.length>MAX_BYTES){errno=EINVAL;return -1;}
  if(o->now()>=end){errno=ETIMEDOUT;return -1;}
  q->b[i].len=p.length;q->b[i].p=o->map(NULL,p.length,PROT_READ|PROT_WRITE,MAP_SHARED,fd,p.m.mem_offset);
  if(q->b[i].p==MAP_FAILED)return -1;q->mapped++;
 }
 return 0;
}
static int submit(const struct ops *o,int fd,struct queue *q,unsigned i,const unsigned char *src,size_t n,double end) {
 if(i>=q->n||q->b[i].queued||n>q->b[i].len){errno=EINVAL;return -1;}
 if(n)memcpy(q->b[i].p,src,n);
 struct v4l2_plane p={.length=q->b[i].len,.bytesused=n,.data_offset=0};
 struct v4l2_buffer b={.type=q->type,.memory=V4L2_MEMORY_MMAP,.index=i,.length=1,.m.planes=&p};
 if(V4L2_TYPE_IS_OUTPUT(q->type)) {
  if(input_pts_index>=3){errno=EINVAL;return -1;}
  b.timestamp.tv_sec=pts_us[input_pts_index]/1000000;
  b.timestamp.tv_usec=pts_us[input_pts_index]%1000000;
 }
 if(call(o,fd,VIDIOC_QBUF,&b,end))return -1;
 if(V4L2_TYPE_IS_OUTPUT(q->type))input_pts_index++;
 q->b[i].queued=true;q->b[i].submitted++;return 0;
}
static int start(const struct ops *o,int fd,struct queue *q,double end) {int t=q->type;if(call(o,fd,VIDIOC_STREAMON,&t,end))return -1;q->stream=true;return 0;}
static int capture_setup(const struct ops *o,int fd,struct queue *q,unsigned w,unsigned h,unsigned *stride,unsigned *rows,double end) {
 struct v4l2_format f={.type=q->type};if(call(o,fd,VIDIOC_G_FMT,&f,end))return -1;
 struct v4l2_pix_format_mplane *p=&f.fmt.pix_mp;
 if(p->pixelformat!=V4L2_PIX_FMT_NV12||p->num_planes!=1||p->width!=w||p->height!=h||p->plane_fmt[0].bytesperline!=128||p->plane_fmt[0].sizeimage!=24576){errno=ENOTSUP;return -1;}
 struct v4l2_selection s={.type=q->type,.target=V4L2_SEL_TGT_COMPOSE};
 if(call(o,fd,VIDIOC_G_SELECTION,&s,end))return -1;
 if(s.r.left||s.r.top||s.r.width!=w||s.r.height!=h){errno=ENOTSUP;return -1;}
 *stride=p->plane_fmt[0].bytesperline;*rows=(p->height+31u)&~31u;
 if(allocate(o,fd,q,end))return -1;
 for(unsigned i=0;i<q->n;i++)if(submit(o,fd,q,i,NULL,0,end))return -1;
 return start(o,fd,q,end);
}
static int compare_frame(const struct mapping *m,const struct v4l2_plane *p,const unsigned char *expected,unsigned w,unsigned h,unsigned stride,unsigned rows) {
 if(p->data_offset>p->bytesused||p->bytesused>m->len)return -1;
 size_t payload=p->bytesused-p->data_offset;
 size_t uv=(size_t)stride*rows,need=uv+(size_t)stride*(h/2-1)+w;
 if(need>payload)return -1;
 const unsigned char *src=(const unsigned char *)m->p+p->data_offset;
 for(unsigned y=0;y<h;y++)if(memcmp(src+(size_t)y*stride,expected+(size_t)y*w,w))return -1;
 for(unsigned y=0;y<h/2;y++)if(memcmp(src+uv+(size_t)y*stride,expected+(size_t)w*h+(size_t)y*w,w))return -1;
 return 0;
}
/* Single run, no probing/fallback, bounded input and total monotonic deadline.
 * Only first SOURCE_CHANGE is admitted; a second is rejected without cleanup.
 * The fixed one-generation fixture does not exercise DRC.
 */
static struct result decode(const struct ops *o,const char *device,const char *driver,const struct data *input,const struct data *oracle,double seconds) {
 /* No cross-open globals: reinitialize even on cycle > 0. Each owns a new FILE. */
 trace_seq=0;input_pts_index=0;seen_pts=0;evidence_bad=false;
 struct result z={.close_rc=-1};int fd=-1;double end=o->now()+seconds;unsigned stride=0,rows=0,next=0,done=0;bool cap_ready=false,stopped=false;
 struct queue out={.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE},cap={.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
 const unsigned char *au[MAX_FRAMES];unsigned sizes[MAX_FRAMES];
 if(input->n<8||oracle->n<16||memcmp(input->p,"FIR1",4)||memcmp(oracle->p,"FNV1",4)){z.error=EINVAL;return z;}
 unsigned count=le32(input->p+4),w=le32(oracle->p+4),h=le32(oracle->p+8),frames=le32(oracle->p+12);
 if(count!=3||frames!=3||w!=128||h!=128||oracle->n!=16+(size_t)w*h*3/2*frames){z.error=EINVAL;return z;}
 size_t pos=8,frame_size=(size_t)w*h*3/2;
 for(unsigned i=0;i<count;i++){if(pos+4>input->n){z.error=EINVAL;return z;}sizes[i]=le32(input->p+pos);pos+=4;if(!sizes[i]||sizes[i]>MAX_BYTES||sizes[i]>input->n-pos){z.error=EINVAL;return z;}au[i]=input->p+pos;pos+=sizes[i];}
 if(pos!=input->n){z.error=EINVAL;return z;}
 if(o->now()>=end){z.error=ETIMEDOUT;z.timeout=true;return z;}
 fd=o->open(device,O_RDWR|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);if(fd<0){z.error=errno;return z;}z.opened=true;
 struct stat ds;
 if(o->node(fd,&ds))goto fail;
 if(!S_ISCHR(ds.st_mode)){errno=ENODEV;goto fail;}
 fprintf(trace_file,"{\"kind\":\"opened_character_device\",\"major\":%u,\"minor\":%u}\n",major(ds.st_rdev),minor(ds.st_rdev));
 struct v4l2_capability c={0};
 if(call(o,fd,VIDIOC_QUERYCAP,&c,end))goto fail;
 fprintf(trace_file,"{\"kind\":\"querycap\",\"driver_hex\":\"");
 for(unsigned i=0;i<sizeof(c.driver);i++)fprintf(trace_file,"%02x",c.driver[i]);
 fprintf(trace_file,"\",\"card_hex\":\"");for(unsigned i=0;i<sizeof(c.card);i++)fprintf(trace_file,"%02x",c.card[i]);
 fprintf(trace_file,"\",\"bus_info_hex\":\"");for(unsigned i=0;i<sizeof(c.bus_info);i++)fprintf(trace_file,"%02x",c.bus_info[i]);
 fprintf(trace_file,"\",\"capabilities\":%u,\"device_caps\":%u}\n",c.capabilities,c.device_caps);
 if(strnlen((char *)c.card,sizeof(c.card))==sizeof(c.card)||strcmp((char *)c.card,"Iris Decoder")){errno=ENODEV;goto fail;}
 unsigned caps=(c.capabilities&V4L2_CAP_DEVICE_CAPS)?c.device_caps:c.capabilities;
 if(strnlen((char *)c.driver,sizeof(c.driver))==sizeof(c.driver)||strcmp((char *)c.driver,driver)||!(caps&V4L2_CAP_VIDEO_M2M_MPLANE)||!(caps&V4L2_CAP_STREAMING)){errno=ENODEV;goto fail;}
 struct identity identity={0};
 if(trace_sync()||o->guard(fd,cycle_index,&identity,end))goto fail;
 if(!identity.session||!identity.generation||identity.session==previous_session||identity.generation<=previous_generation||identity.node_major!=major(ds.st_rdev)||identity.node_minor!=minor(ds.st_rdev)){errno=ESTALE;goto fail;}
 previous_session=identity.session;previous_generation=identity.generation;
 struct v4l2_event_subscription sub={.type=V4L2_EVENT_SOURCE_CHANGE};if(call(o,fd,VIDIOC_SUBSCRIBE_EVENT,&sub,end))goto fail;
 sub.type=V4L2_EVENT_EOS;if(call(o,fd,VIDIOC_SUBSCRIBE_EVENT,&sub,end))goto fail;
 struct v4l2_format f={.type=out.type};f.fmt.pix_mp.width=w;f.fmt.pix_mp.height=h;f.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_H264;f.fmt.pix_mp.num_planes=1;f.fmt.pix_mp.plane_fmt[0].sizeimage=MAX_BYTES;
 if(call(o,fd,VIDIOC_S_FMT,&f,end))goto fail;
 if(f.fmt.pix_mp.pixelformat!=V4L2_PIX_FMT_H264||f.fmt.pix_mp.num_planes!=1||f.fmt.pix_mp.width!=w||f.fmt.pix_mp.height!=h){errno=ENOTSUP;goto fail;}
 if(allocate(o,fd,&out,end))goto fail;
 for(unsigned i=0;i<out.n&&next<count;i++,next++)if(submit(o,fd,&out,i,au[next],sizes[next],end))goto fail;
 if(start(o,fd,&out,end))goto fail;
 while(!z.eos){
  if(o->now()>=end){errno=ETIMEDOUT;goto fail;}
  struct v4l2_event e={0};
  if(!call(o,fd,VIDIOC_DQEVENT,&e,end)){
   if(e.type==V4L2_EVENT_EOS){/* Event alone is NOT frame/LAST evidence. */}
   else if(e.type==V4L2_EVENT_SOURCE_CHANGE&&(e.u.src_change.changes&V4L2_EVENT_SRC_CH_RESOLUTION)){
    if(cap_ready){errno=ENOTSUP;goto fail;}
    if(capture_setup(o,fd,&cap,w,h,&stride,&rows,end))goto fail;
    cap_ready=true;z.drc++;
   }else{errno=EPROTO;goto fail;}
  }else if(errno!=EAGAIN&&errno!=ENOENT)goto fail;
  struct queue *qs[2]={&out,&cap};
  for(unsigned k=0;k<2;k++){
   struct queue *q=qs[k];if(!q->stream)continue;
   struct v4l2_plane p={0};struct v4l2_buffer b={.type=q->type,.memory=V4L2_MEMORY_MMAP,.length=1,.m.planes=&p};
   if(call(o,fd,VIDIOC_DQBUF,&b,end)){if(errno==EAGAIN)continue;goto fail;}
   if(b.length!=1||b.index>=q->n||!q->b[b.index].queued||p.data_offset>p.bytesused||p.bytesused>q->b[b.index].len){errno=EPROTO;goto fail;}
   q->b[b.index].queued=false;
   if(b.flags&V4L2_BUF_FLAG_ERROR){errno=EIO;goto fail;}
   if(!k){z.output_dq++;done++;if(next<count){if(submit(o,fd,q,b.index,au[next],sizes[next],end))goto fail;next++;}}
   else{
    z.capture_dq++;size_t n=p.bytesused-p.data_offset;
    if(n){
     if(b.timestamp.tv_sec<0||b.timestamp.tv_sec>2147483647L||b.timestamp.tv_usec<0||b.timestamp.tv_usec>=1000000){errno=EPROTO;goto fail;}
     long long pts=(long long)b.timestamp.tv_sec*1000000+b.timestamp.tv_usec;unsigned match=3;
     for(unsigned j=0;j<3;j++)if(pts==pts_us[j])match=j;
     if(z.frames>=3){errno=EPROTO;goto fail;}
     if(match==3||(seen_pts&(1U<<match))||compare_frame(&q->b[b.index],&p,oracle->p+16+match*frame_size,w,h,stride,rows)){errno=EBADMSG;goto fail;}
     if(save_capture(&q->b[b.index],&p,z.frames,w,h,stride,rows,z.hash[z.frames]))goto fail;
     z.pts[z.frames]=(unsigned)pts;
     if(record("{\"kind\":\"oracle\",\"cycle\":%u,\"arrival\":%u,\"pts_us\":%u,\"frame_sha256\":\"%s\",\"match\":true}\n",cycle_index,z.frames,z.pts[z.frames],z.hash[z.frames]))goto fail;
     seen_pts|=1U<<match;z.frames++;
    }
    if(b.flags&V4L2_BUF_FLAG_LAST){if(z.frames!=frames||!stopped||done!=count){errno=EPROTO;goto fail;}z.eos=true;}
    else if(submit(o,fd,q,b.index,NULL,0,end))goto fail;
   }
  }
  if(done==count&&!stopped){struct v4l2_decoder_cmd d={.cmd=V4L2_DEC_CMD_STOP};if(call(o,fd,VIDIOC_DECODER_CMD,&d,end))goto fail;stopped=true;}
  o->idle();
 }
 if(seen_pts!=7||input_pts_index!=3){errno=EPROTO;goto fail;}
 /* LAST/oracle -> checked lifecycle + structured receipt -> both STREAMOFF ->
  * both REQBUFS0 -> munmap -> close. No reuse of old once-only STOP observer ioctl. */
 struct queue *qs[2]={&out,&cap};struct receipt receipt={0};
 if(o->now()>=end){errno=ETIMEDOUT;goto fail;}
 int checked_rc=o->checked_lifecycle(fd,cycle_index,&out,&cap,&identity,&receipt,end);int checked_errno=checked_rc?errno:0;
 if(record("{\"kind\":\"checked_lifecycle_actual_return\",\"cycle\":%u,\"rc\":%d,\"errno\":%d}\n",cycle_index,checked_rc,checked_errno))goto fail;
 if(checked_rc){errno=checked_errno;goto fail;}
 if(o->now()>=end){errno=ETIMEDOUT;goto fail;}
 /* Successful control return alone is never accepted as bare safe=true. */
 if(record("{\"kind\":\"receipt_header\",\"version\":%u,\"session\":%llu,\"generation\":%llu,\"status\":%u,\"internal_pending\":%u,\"release_wait\":%u,\"cpu_pending\":%u,\"cpu_active\":%u}\n",receipt.version,(unsigned long long)receipt.session,(unsigned long long)receipt.generation,receipt.status,receipt.internal_pending,receipt.release_wait,receipt.cpu_pending,receipt.cpu_active))goto fail;
 bool receipt_bad=receipt.version!=1||receipt.status||receipt.session!=identity.session||receipt.generation!=identity.generation||receipt.internal_pending||receipt.release_wait||receipt.cpu_pending||receipt.cpu_active;
 for(unsigned k=0;k<2;k++){
  if(record("{\"kind\":\"receipt_stop\",\"queue\":%u,\"request_header\":%u,\"request_packet\":%u,\"reply_header\":%u,\"reply_packet\":%u,\"slots\":%u}\n",k,receipt.request_header[k],receipt.request_packet[k],receipt.reply_header[k],receipt.reply_packet[k],receipt.slots[k]))goto fail;
  if(receipt.slots[k]!=qs[k]->n||!receipt.request_header[k]||!receipt.request_packet[k]||receipt.request_header[k]!=receipt.reply_header[k]||receipt.request_packet[k]!=receipt.reply_packet[k])receipt_bad=true;
  for(unsigned i=0;i<qs[k]->n;i++){
   const struct receipt_slot *r=&receipt.app[k][i];
   if(record("{\"kind\":\"receipt_app_slot\",\"queue\":%u,\"index\":%u,\"local_submitted\":%u,\"submitted\":%u,\"returned\":%u,\"matched\":%u,\"outstanding\":%u,\"readonly\":%u,\"unknown\":%u}\n",k,i,qs[k]->b[i].submitted,r->submitted,r->returned,r->matched,r->outstanding,r->readonly,r->unknown))goto fail;
   if(r->submitted!=qs[k]->b[i].submitted||r->returned!=r->submitted||r->matched!=r->returned||r->outstanding||r->readonly||r->unknown)receipt_bad=true;
  }
 }
 if(receipt_bad){errno=EPROTO;goto fail;}z.contract_observed=true;
 for(unsigned k=0;k<2;k++){
  int t=qs[k]->type;if(call(o,fd,VIDIOC_STREAMOFF,&t,end))goto fail;
  qs[k]->stream=false;z.streamoff_ok++;
 }
 for(unsigned k=0;k<2;k++){
  struct v4l2_requestbuffers r={.count=0,.type=qs[k]->type,.memory=V4L2_MEMORY_MMAP};
  if(call(o,fd,VIDIOC_REQBUFS,&r,end))goto fail;
  if(r.count){errno=EPROTO;goto fail;}z.reqbufs0_ok++;qs[k]->requested=false;
 }
 for(unsigned k=0;k<2;k++)for(unsigned i=0;i<qs[k]->n;i++){
  if(o->now()>=end){errno=ETIMEDOUT;goto fail;}
  int rc=o->unmap(qs[k]->b[i].p,qs[k]->b[i].len);int e=rc?errno:0;
  if(record("{\"kind\":\"munmap\",\"cycle\":%u,\"type\":%u,\"index\":%u,\"rc\":%d,\"errno\":%d}\n",cycle_index,qs[k]->type,i,rc,e))goto fail;
  if(rc){errno=e;goto fail;}qs[k]->b[i].p=NULL;z.unmapped++;
 }
 if(o->now()>=end){errno=ETIMEDOUT;goto fail;}
 z.close_rc=o->close(fd);int ce=z.close_rc?errno:0;
 if(record("{\"kind\":\"close_actual_return\",\"cycle\":%u,\"rc\":%d,\"errno\":%d,\"device_teardown_proven\":false}\n",cycle_index,z.close_rc,ce))goto fail;
 if(z.close_rc){errno=ce;goto fail;}z.cleanup_ok=true;
 goto finish;
fail:z.error=errno?errno:EIO;z.timeout=z.error==ETIMEDOUT;
finish:
 z.out_maps=out.mapped;z.cap_maps=cap.mapped;
 return z;
}

#define RDE2_CAP 32768U
struct repeat_result {unsigned requested,completed,attempted;int error;bool hold;double mono_start,mono_end;struct result cycle[30];};
static int cycle_count(const char *s,unsigned *n){
 if(!s||!*s)return -1;unsigned v=0;
 for(;*s;s++){if(*s<'0'||*s>'9')return -1;v=v*10+(unsigned)(*s-'0');if(v>30)return -1;}
 if(!v)return -1;*n=v;return 0;
}
static int next_trace(int dir,unsigned cycle,const struct ops *o,double end) {
 if(o->now()>=end){errno=ETIMEDOUT;return -1;}
 char name[64];snprintf(name,sizeof(name),"cycle-%02u-trace.jsonl",cycle);
 int fd=openat(dir,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)return -1;
 trace_file=fdopen(fd,"w");if(!trace_file){close(fd);return -1;}
 return record("{\"kind\":\"cycle_begin\",\"cycle\":%u,\"pid\":%ld,\"starttime_ticks\":%llu,\"monotonic\":%.9f,\"native_proof\":false}\n",cycle,(long)getpid(),process_starttime,o->now());
}
static struct repeat_result repeat(const struct ops *o,const char *device,const char *driver,const struct data *in,const struct data *oracle,unsigned count,int dir){
 struct repeat_result all={.requested=count};all.mono_start=o->now();double whole=all.mono_start+600.;previous_session=previous_generation=0;
 if(count<1||count>30){all.error=EINVAL;return all;}
 evidence_fd=dir;
 for(unsigned i=0;i<count;i++){
  cycle_index=i;
  if(next_trace(dir,i,o,whole)){all.error=errno;all.hold=true;break;}
  double left=whole-o->now();if(left<=0){all.error=ETIMEDOUT;all.hold=true;break;}
  all.cycle[i]=decode(o,device,driver,in,oracle,left<30?left:30);all.attempted++;
  struct result *z=&all.cycle[i];
  if(record("{\"kind\":\"cycle_end\",\"cycle\":%u,\"frames\":%u,\"pts_mask\":%u,\"error\":%d,\"streamoff_returns\":%u,\"queuefree_returns\":%u,\"close_rc\":%d,\"conditional_scope\":true,\"hardware_decode_verified\":false,\"hardware_task_proven\":false,\"dma_stop_verified\":false,\"kernel_stop_retain_verified\":false}\n",i,z->frames,seen_pts,z->error,z->streamoff_ok,z->reqbufs0_ok,z->close_rc)){
   if(!z->error)z->error=EIO;
  }
  if(z->error||!z->cleanup_ok){all.error=z->error?z->error:EIO;all.hold=true;break;}
  if(fclose(trace_file)){trace_file=NULL;all.error=EIO;all.hold=true;break;}trace_file=NULL;
  if(o->now()>=whole){all.error=ETIMEDOUT;all.hold=true;break;}
  all.completed++;
 }
 all.mono_end=o->now();return all;
}
static int append(char *b,size_t *n,const char *fmt,...){
 if(*n>=RDE2_CAP)return -1;va_list a;va_start(a,fmt);int k=vsnprintf(b+*n,RDE2_CAP-*n,fmt,a);va_end(a);
 if(k<0||(size_t)k>=RDE2_CAP-*n){errno=EOVERFLOW;return -1;}*n+=(size_t)k;return 0;
}
static int summary(const struct repeat_result *r,char b[RDE2_CAP],size_t *n){
 *n=0;
 if(append(b,n,"{\"schema\":\"RDE2\",\"pid\":%ld,\"starttime_ticks\":%llu,\"mono_start\":%.9f,\"mono_end\":%.9f,\"cycles_requested\":%u,\"cycles_completed\":%u,\"attempted\":%u,\"error\":%d,\"hold\":%s,\"scope\":\"conditional_perbuffer_contract_not_device_teardown\",\"hardware_decode_verified\":false,\"hardware_task_proven\":false,\"dma_stop_verified\":false,\"kernel_stop_retain_verified\":false,\"native_proof\":false,\"raw_frame_cycles\":1,\"cycles\":[",(long)getpid(),process_starttime,r->mono_start,r->mono_end,r->requested,r->completed,r->attempted,r->error,r->hold?"true":"false"))return -1;
 for(unsigned i=0;i<r->attempted;i++){
  const struct result *z=&r->cycle[i];
  if(append(b,n,"%s{\"cycle\":%u,\"errno\":%d,\"output_dq\":%u,\"capture_dq\":%u,\"streamoff_rc0\":%u,\"reqbufs0_rc0\":%u,\"unmapped\":%u,\"close_rc\":%d,\"frames\":[",i?",":"",i,z->error,z->output_dq,z->capture_dq,z->streamoff_ok,z->reqbufs0_ok,z->unmapped,z->close_rc))return -1;
  for(unsigned j=0;j<z->frames;j++)if(append(b,n,"%s{\"pts_us\":%u,\"sha256\":\"%s\"}",j?",":"",z->pts[j],z->hash[j]))return -1;
  if(append(b,n,"]}"))return -1;
 }
 return append(b,n,"]}\n");
}
#ifndef CLIENT_TEST
static int pinned_fixture(const char *path,struct data *d,const char *pin){
 if(load(path,d))return -1;unsigned char hash[32];char text[65];hash_memory(d->p,d->n,hash);
 for(unsigned i=0;i<32;i++)snprintf(text+2*i,3,"%02x",hash[i]);if(strcmp(text,pin)){errno=EBADMSG;return -1;}return 0;
}
static unsigned long long starttime_ticks(void){
 char b[4096];int fd=open("/proc/self/stat",O_RDONLY|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return 0;
 ssize_t n=read(fd,b,sizeof(b)-1);close(fd);if(n<=0)return 0;b[n]=0;char *p=strrchr(b,')');if(!p||p[1]!=' ')return 0;p+=2;
 for(unsigned f=3;f<22;f++){p=strchr(p,' ');if(!p)return 0;p++;}
 char *end;errno=0;unsigned long long v=strtoull(p,&end,10);return errno||end==p||(*end!=' '&&*end!='\n')?0:v;
}
static int emit_summary(const struct repeat_result *r,int dir,int pipefd){
 char b[RDE2_CAP];size_t n;if(summary(r,b,&n))return -1;
 unsigned char h[8]={'R','D','E','2',0,0,0,0};for(unsigned i=0;i<4;i++)h[4+i]=(unsigned char)(n>>(8*i));
 int fd=openat(dir,"summary.rde2",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)return -1;
 int bad=write_all(fd,h,8)||write_all(fd,b,n)||fsync(fd);if(close(fd))bad=1;if(bad)return -1;
 /* Pipe integration not admitted yet. Caller must provide a draining nonblocking
  * pipe; bounded 1s write, no rawframes and no truncated partial summary acceptance. */
 if(pipefd>=0){
  struct stat st;int flags=fcntl(pipefd,F_GETFL);
  if(fstat(pipefd,&st)||!S_ISFIFO(st.st_mode)||flags<0||!(flags&O_NONBLOCK)){errno=EINVAL;return -1;}
  unsigned char packet[RDE2_CAP+8];memcpy(packet,h,8);memcpy(packet+8,b,n);size_t done=0;double end=real_now()+1;
  while(done<n+8){if(real_now()>=end){errno=ETIMEDOUT;return -1;}ssize_t k=write(pipefd,packet+done,n+8-done);
   if(k>0){done+=(size_t)k;continue;}if(k<0&&errno==EAGAIN){struct pollfd p={pipefd,POLLOUT,0};poll(&p,1,1);continue;}return -1;}
 }
 return 0;
}
int main(int argc,char **argv){
 const char *device=NULL,*driver="iris_driver",*input=NULL,*oracle=NULL,*evidence=NULL;bool execute=false;unsigned cycles=30;int pipefd=-1;
 for(int i=1;i<argc;i++){
  if(!strcmp(argv[i],"--execute"))execute=true;
  else if(i+1<argc&&!strcmp(argv[i],"--device"))device=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--input"))input=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--oracle"))oracle=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--evidence-dir"))evidence=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--cycles")){if(cycle_count(argv[++i],&cycles))return 2;}
  else if(i+1<argc&&!strcmp(argv[i],"--summary-fd")){char *p;long v=strtol(argv[++i],&p,10);if(*p||v<3||v>1024)return 2;pipefd=(int)v;}
  else return 2;
 }
 if(!execute||!device||!input||!oracle||!evidence||strncmp(device,"/dev/video",10)||!device[10])return 2;
 for(const char *p=device+10;*p;p++)if(*p<'0'||*p>'9')return 2;
 /* Immutable local configuration is unresolved, so reject BEFORE open or ioctl.
  * Neither CLI nor any peer-provided ready token can bypass this branch. */
 if(!IRIS_REPEAT_PINS_RESOLVED){fputs("future_driver_abi_unresolved; native_proof=false\n",stderr);return 2;}
 process_starttime=starttime_ticks();if(!process_starttime)return 2;
 struct data in={0},ref={0};if(pinned_fixture(input,&in,IRIS_INPUT_SHA256)||pinned_fixture(oracle,&ref,IRIS_ORACLE_SHA256))return 2;
 umask(077);if(mkdir(evidence,0700))return 2;int dir=open(evidence,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);if(dir<0)return 2;
 sigset_t signals;sigfillset(&signals);if(sigprocmask(SIG_BLOCK,&signals,NULL))return 2;
 struct repeat_result r=repeat(&real_ops,device,driver,&in,&ref,cycles,dir);
 if(emit_summary(&r,dir,pipefd)){r.error=errno;r.hold=true;}
 if(r.hold){if(trace_file)record("{\"kind\":\"HOLD\",\"error\":%d,\"no_retry\":true,\"no_failure_cleanup\":true,\"native_proof\":false}\n",r.error);fflush(stdout);for(;;)pause();}
 free(in.p);free(ref.p);close(dir);return 0;
}
#endif
