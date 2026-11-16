/* SPDX-License-Identifier: GPL-2.0-only
 * PRIVATE next-stage H.264 evidence/HOLD derivative; not admitted for hardware execution.
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

#define MAX_BUFS 32
#define MAX_BYTES (8U*1024U*1024U)
#define MAX_FRAMES 16
struct ops {
 int (*open)(const char *,int); int (*close)(int);
 int (*ioctl)(int,unsigned long,void *);
 void *(*map)(void *,size_t,int,int,int,off_t);
 int (*unmap)(void *,size_t); double (*now)(void);
};
struct mapping { void *p; size_t len; bool queued; };
struct queue { enum v4l2_buf_type type; struct mapping b[MAX_BUFS]; unsigned n; bool requested,stream; };
struct data { unsigned char *p; size_t n; };
struct result { unsigned frames,drc,output_dq,capture_dq; bool eos,timeout,cleanup_ok; int error; };
static double real_now(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return t.tv_sec+t.tv_nsec/1e9; }
static void *real_map(void *p,size_t n,int prot,int flags,int fd,off_t off) {return mmap(p,n,prot,flags,fd,off);}
static int real_ioctl(int fd,unsigned long r,void *p) {return ioctl(fd,r,p);}
static int real_open(const char *path,int flags) {return open(path,flags);}
/* Experimental evidence collector. Kernel retain-safe release is a prerequisite.
 * No software decoder, subprocess, STREAMOFF, REQBUFS0, munmap or video close.
 * HOLD is CPU/userspace retention only; SIGKILL/crash still calls kernel release.
 * A separate future kernel coordinator must implement STOP_RETAIN and deadline.
 */
static FILE *trace_file;
static int evidence_fd = -1;
static unsigned trace_seq, input_pts_index;
static const unsigned pts_us[3] = {1000000,1040000,1080000};
static unsigned seen_pts;
static int write_all(int fd,const void *v,size_t n) {
 const unsigned char *p=v;
 while(n){ssize_t r=write(fd,p,n);if(r<0&&errno==EINTR)continue;if(r<=0)return -1;p+=r;n-=(size_t)r;}
 return 0;
}
static int save_capture(const struct mapping *m,const struct v4l2_plane *p,unsigned frame,unsigned w,unsigned h,unsigned stride,unsigned rows) {
 char name[64];snprintf(name,sizeof(name),"capture-%02u-plane.bin",frame);
 int fd=openat(evidence_fd,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);
 if(fd<0)return -1;
 int bad=write_all(fd,(const unsigned char *)m->p+p->data_offset,p->bytesused-p->data_offset);
 if(fsync(fd))bad=-1;if(close(fd))bad=-1;if(bad)return -1;
 size_t uv=(size_t)stride*rows,need=uv+(size_t)stride*(h/2-1)+w;
 if(need>p->bytesused-p->data_offset){errno=EPROTO;return -1;}
 snprintf(name,sizeof(name),"capture-%02u-visible.nv12",frame);
 fd=openat(evidence_fd,name,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(fd<0)return -1;
 const unsigned char *src=(const unsigned char *)m->p+p->data_offset;
 for(unsigned y=0;y<h&&!bad;y++)bad=write_all(fd,src+(size_t)y*stride,w);
 for(unsigned y=0;y<h/2&&!bad;y++)bad=write_all(fd,src+uv+(size_t)y*stride,w);
 if(fsync(fd))bad=-1;if(close(fd))bad=-1;return bad;
}
static const struct ops real_ops={real_open,close,real_ioctl,real_map,munmap,real_now};
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
 for(;;){if(o->now()>=end){errno=ETIMEDOUT;return -1;}int r=o->ioctl(fd,req,p);int saved=r<0?errno:0;
 if(trace_file&&!(r<0&&(saved==EAGAIN||(req==VIDIOC_DQEVENT&&saved==ENOENT)))) {
  if(++trace_seq>4096){errno=EOVERFLOW;return -1;}
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
  fprintf(trace_file,"}\n");if(fflush(trace_file)||fsync(fileno(trace_file))){errno=EIO;return -1;}
 }
 errno=saved;if(r>=0||saved!=EINTR)return r;}
}
static int allocate(const struct ops *o,int fd,struct queue *q,double end) {
 struct v4l2_requestbuffers r={.count=4,.type=q->type,.memory=V4L2_MEMORY_MMAP};
 if(call(o,fd,VIDIOC_REQBUFS,&r,end))return -1;
 q->requested=true;if(r.count<2||r.count>MAX_BUFS){errno=EINVAL;return -1;}q->n=r.count;
 for(unsigned i=0;i<q->n;i++){
  struct v4l2_plane p={0};struct v4l2_buffer b={.type=q->type,.memory=V4L2_MEMORY_MMAP,.index=i,.length=1,.m.planes=&p};
  if(call(o,fd,VIDIOC_QUERYBUF,&b,end)||b.length!=1||!p.length||p.length>MAX_BYTES){errno=EINVAL;return -1;}
  q->b[i].len=p.length;q->b[i].p=o->map(NULL,p.length,PROT_READ|PROT_WRITE,MAP_SHARED,fd,p.m.mem_offset);
  if(q->b[i].p==MAP_FAILED)return -1;
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
 q->b[i].queued=true;return 0;
}
static int start(const struct ops *o,int fd,struct queue *q,double end) {int t=q->type;if(call(o,fd,VIDIOC_STREAMON,&t,end))return -1;q->stream=true;return 0;}
static int capture_setup(const struct ops *o,int fd,struct queue *q,unsigned w,unsigned h,unsigned *stride,unsigned *rows,double end) {
 struct v4l2_format f={.type=q->type};if(call(o,fd,VIDIOC_G_FMT,&f,end))return -1;
 struct v4l2_pix_format_mplane *p=&f.fmt.pix_mp;
 if(p->pixelformat!=V4L2_PIX_FMT_NV12||p->num_planes!=1||p->width!=w||p->height!=h||p->plane_fmt[0].bytesperline!=128||p->plane_fmt[0].sizeimage!=24576){errno=ENOTSUP;return -1;}
 struct v4l2_selection s={.type=q->type,.target=V4L2_SEL_TGT_COMPOSE};
 if(call(o,fd,VIDIOC_G_SELECTION,&s,end)||s.r.left||s.r.top||s.r.width!=w||s.r.height!=h){errno=ENOTSUP;return -1;}
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
 struct result z={.cleanup_ok=true};int fd=-1;double end=o->now()+seconds;unsigned stride=0,rows=0,next=0,done=0;bool cap_ready=false,stopped=false;
 struct queue out={.type=V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE},cap={.type=V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE};
 const unsigned char *au[MAX_FRAMES];unsigned sizes[MAX_FRAMES];
 if(input->n<8||oracle->n<16||memcmp(input->p,"FIR1",4)||memcmp(oracle->p,"FNV1",4)){z.error=EINVAL;return z;}
 unsigned count=le32(input->p+4),w=le32(oracle->p+4),h=le32(oracle->p+8),frames=le32(oracle->p+12);
 if(count!=3||frames!=3||w!=128||h!=128||oracle->n!=16+(size_t)w*h*3/2*frames){z.error=EINVAL;return z;}
 size_t pos=8,frame_size=(size_t)w*h*3/2;
 for(unsigned i=0;i<count;i++){if(pos+4>input->n){z.error=EINVAL;return z;}sizes[i]=le32(input->p+pos);pos+=4;if(!sizes[i]||sizes[i]>MAX_BYTES||sizes[i]>input->n-pos){z.error=EINVAL;return z;}au[i]=input->p+pos;pos+=sizes[i];}
 if(pos!=input->n){z.error=EINVAL;return z;}
 fd=o->open(device,O_RDWR|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);if(fd<0){z.error=errno;return z;}
 struct stat ds;
 if(fstat(fd,&ds)||!S_ISCHR(ds.st_mode)){errno=ENODEV;goto fail;}
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
     if(z.frames>=3||save_capture(&q->b[b.index],&p,z.frames,w,h,stride,rows))goto fail;
     if(match==3||(seen_pts&(1U<<match))||compare_frame(&q->b[b.index],&p,oracle->p+16+match*frame_size,w,h,stride,rows)){errno=EBADMSG;goto fail;}
     seen_pts|=1U<<match;z.frames++;
    }
    if(b.flags&V4L2_BUF_FLAG_LAST){if(z.frames!=frames||!stopped||done!=count){errno=EPROTO;goto fail;}z.eos=true;}
    else if(submit(o,fd,q,b.index,NULL,0,end))goto fail;
   }
  }
  if(done==count&&!stopped){struct v4l2_decoder_cmd d={.cmd=V4L2_DEC_CMD_STOP};if(call(o,fd,VIDIOC_DECODER_CMD,&d,end))goto fail;stopped=true;}
 }
 goto cleanup;
fail:z.error=errno?errno:EIO;z.timeout=z.error==ETIMEDOUT;
cleanup:;/* Intentionally no V4L2 streaming cleanup or video close. */
 if(fd>=0)z.cleanup_ok=false; /* retained != successful cleanup */
 return z;
}
#ifndef CLIENT_TEST
static void progress_snapshot(void) {
 static const char path[]="/sys/devices/platform/soc@0/aa00000.video-codec/decode_once_progress";
 char bytes[768];int fd=open(path,O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
 if(fd<0){fprintf(stderr,"IRIS_PROGRESS_READ errno=%d\n",errno);fflush(stderr);return;}
 ssize_t n=read(fd,bytes,sizeof(bytes));int e=errno;close(fd);
 if(n<=0||n>=(ssize_t)sizeof(bytes)){fprintf(stderr,"IRIS_PROGRESS_READ errno=%d\n",n<0?e:EPROTO);fflush(stderr);return;}
 if(bytes[n-1]!='\n'){fprintf(stderr,"IRIS_PROGRESS_READ errno=%d\n",EPROTO);fflush(stderr);return;}
 for(ssize_t i=0;i<n;i++)if(!((bytes[i]>='a'&&bytes[i]<='z')||(bytes[i]>='0'&&bytes[i]<='9')||bytes[i]=='_'||bytes[i]=='='||bytes[i]=='-'||bytes[i]==' '||(i==n-1&&bytes[i]=='\n'))){fprintf(stderr,"IRIS_PROGRESS_READ errno=%d\n",EPROTO);fflush(stderr);return;}
 fprintf(stderr,"IRIS_PROGRESS %.*s",(int)n,bytes);fflush(stderr);
}
static int log_contains(const char *p,size_t n,const char *word) {
 size_t w=strlen(word);if(w>n)return 0;
 for(size_t i=0;i+w<=n;i++){size_t j=0;for(;j<w;j++){unsigned char c=(unsigned char)p[i+j];if(c>='A'&&c<='Z')c=(unsigned char)(c-'A'+'a');if(c!=(unsigned char)word[j])break;}if(j==w)return 1;}
 return 0;
}
static size_t log_select(const char *bytes,size_t n,char out[900],unsigned *matches,unsigned *truncated) {
 static const char *keys[]={"iris","vpu","video","smmu","iommu","fault","scm","firmware","dma"};
 size_t used=0;*matches=0;*truncated=0;
 for(size_t p=0;p<n;){size_t end=p;while(end<n&&bytes[end]!='\n')end++;if(end<n)end++;size_t len=end-p;int selected=0;
  if(!log_contains(bytes+p,len,"sync_state()"))for(unsigned i=0;i<sizeof(keys)/sizeof(keys[0]);i++)if(log_contains(bytes+p,len,keys[i])){selected=1;break;}
  if(selected){(*matches)++;const char *line=bytes+p;if(len>900){line+=len-900;len=900;*truncated=1;}if(used+len>900){size_t drop=used+len-900;memmove(out,out+drop,used-drop);used-=drop;*truncated=1;}memcpy(out+used,line,len);used+=len;}
  p=end;
 }
 return used;
}
static void kernel_log_snapshot(void) {
 char bytes[65536],selected[900];int n=klogctl(3,bytes,sizeof(bytes));int saved=errno;unsigned matches=0,truncated=0;size_t used=0;
 if(n>0&&n<=(int)sizeof(bytes))used=log_select(bytes,(size_t)n,selected,&matches,&truncated);
 fprintf(trace_file,"{\"kind\":\"kernel_log_selected\",\"read_rc\":%d,\"observed_bytes\":%d,\"matches\":%u,\"truncated\":%u,\"selected_bytes\":%zu,\"selected_hex\":\"",n<0?-saved:(n>(int)sizeof(bytes)?-EPROTO:0),n>=0?n:0,matches,truncated,used);
 for(size_t i=0;i<used;i++)fprintf(trace_file,"%02x",(unsigned char)selected[i]);
 fprintf(trace_file,"\"}\n");
}

static void input_snapshot(void) {
 static const char path[]="/sys/devices/platform/soc@0/aa00000.video-codec/decode_once_input";
 unsigned char data[701];size_t used=0;int rc=0;int fd=open(path,O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
 if(fd<0)rc=-errno;else{ssize_t n=read(fd,data,sizeof(data));int saved=errno;close(fd);if(n<0)rc=-saved;else if(!n||n>700||data[n-1]!='\n')rc=-EPROTO;else{used=(size_t)n;for(size_t i=0;i<used;i++)if(!((data[i]>='a'&&data[i]<='z')||(data[i]>='0'&&data[i]<='9')||data[i]=='='||data[i]=='_'||data[i]=='-'||data[i]==' '||data[i]=='\n')){rc=-EPROTO;used=0;break;}}}
 fprintf(trace_file,"{\"kind\":\"input_provenance\",\"read_rc\":%d,\"bytes\":%zu,\"data_hex\":\"",rc,used);
 for(size_t i=0;i<used;i++)fprintf(trace_file,"%02x",data[i]);
 fprintf(trace_file,"\"}\n");
}

static void report(struct result z) {printf("{\"hardwaredecode\":false,\"status\":\"%s\",\"frames\":%u,\"drc\":%u,\"output_dq\":%u,\"capture_dq\":%u,\"eos\":%s,\"deadline\":%s,\"cleanup_ok\":%s,\"errno\":%d,\"software_fallback\":false,\"hardware_task_proven\":false}\n",z.error?"unknown":"content_match_not_hardware_proof",z.frames,z.drc,z.output_dq,z.capture_dq,z.eos?"true":"false",z.timeout?"true":"false",z.cleanup_ok?"true":"false",z.error);}
int main(int argc,char **argv) {
 const char *device=NULL,*driver=NULL,*input=NULL,*oracle=NULL,*evidence=NULL;bool retain_contract=false;bool execute=false;double deadline=5;
 for(int i=1;i<argc;i++){
  if(!strcmp(argv[i],"--execute"))execute=true;
  else if(!strcmp(argv[i],"--require-retain-safe-driver"))retain_contract=true;
  else if(i+1<argc&&!strcmp(argv[i],"--evidence-dir"))evidence=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--device"))device=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--driver"))driver=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--input"))input=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--oracle"))oracle=argv[++i];
  else if(i+1<argc&&!strcmp(argv[i],"--deadline")){char *p;deadline=strtod(argv[++i],&p);if(*p||!(deadline>=0.1&&deadline<=30))return 2;}
  else return 2;
 }
 if(!execute||!retain_contract||!evidence||!device||!driver||strcmp(driver,"iris_driver")||!input||!oracle||strncmp(device,"/dev/video",10)||!device[10])return 2;
 for(const char *p=device+10;*p;p++)if(*p<'0'||*p>'9')return 2;
 struct data a={0},b={0};if(load(input,&a)||load(oracle,&b)){free(a.p);free(b.p);return 2;}
 /* Explicit argument is not authentication of kernel retention; manifest gates apply. */
 evidence_fd=open(evidence,O_RDONLY|O_DIRECTORY|O_CLOEXEC|O_NOFOLLOW);if(evidence_fd<0)return 2;
 int logfd=openat(evidence_fd,"ioctl-trace.jsonl",O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC|O_NOFOLLOW,0600);if(logfd<0)return 2;
 trace_file=fdopen(logfd,"w");if(!trace_file)return 2;
 sigset_t signals;sigfillset(&signals);if(sigprocmask(SIG_BLOCK,&signals,NULL))return 2;
 struct result z=decode(&real_ops,device,driver,&a,&b,deadline);if(z.error){progress_snapshot();kernel_log_snapshot();input_snapshot();}report(z);
 fprintf(trace_file,"{\"kind\":\"hold_request_external_stop_retain\",\"frames\":%u,\"pts_seen_mask\":%u,\"errno\":%d,\"eos\":%s,\"hardware_decode_verified\":false,\"dma_stop_verified\":false,\"kernel_stop_retain_verified\":false}\n",z.frames,seen_pts,z.error,z.eos?"true":"false");
 fflush(trace_file);fsync(logfd);fflush(stdout);
 /* Never exit after opening video; external recovery owns eventual termination. */
 for(;;)pause();
}
#endif
