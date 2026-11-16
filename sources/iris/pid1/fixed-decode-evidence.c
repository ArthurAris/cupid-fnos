/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "fixed-decode-evidence.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>
#include "supervisor-sha256.h"
_Static_assert(FDE_MAX <= FDE_PARENT_MAX,"parent bound");
static void p32(unsigned char *p,uint32_t n){for(unsigned i=0;i<4;i++)p[i]=(unsigned char)(n>>(i*8));}
static void p64(unsigned char *p,uint64_t n){for(unsigned i=0;i<8;i++)p[i]=(unsigned char)(n>>(i*8));}
static int read_fixed(int dir,const char *name,unsigned char *p,size_t max,size_t *size){
 int fd=openat(dir,name,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK);if(fd<0)return errno;
 struct stat a,b;int e=0;size_t n=0;
 if(fstat(fd,&a))e=errno;
 else if(!S_ISREG(a.st_mode)||a.st_nlink!=1||a.st_size<=0||(uint64_t)a.st_size>max)e=EINVAL;
 while(!e&&n<(size_t)a.st_size){ssize_t r=read(fd,p+n,(size_t)a.st_size-n);if(r<0&&errno==EINTR)continue;if(r<=0){e=EIO;break;}n+=(size_t)r;}
 unsigned char tail;if(!e&&read(fd,&tail,1)!=0)e=EFBIG;
 if(!e&&fstat(fd,&b))e=errno;
 if(!e&&(a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec||a.st_ctim.tv_sec!=b.st_ctim.tv_sec||a.st_ctim.tv_nsec!=b.st_ctim.tv_nsec))e=ESTALE;
 close(fd);if(!e)*size=n;return e;
}
/* Exact supervisor SHA functions above; adapt via anonymous RAM fd only. */
static int digest(const unsigned char *p,size_t n,unsigned char out[32]){
 int fd=memfd_create("fde-sha",MFD_CLOEXEC);if(fd<0)return errno;size_t done=0;int e=0;
 while(done<n){ssize_t r=write(fd,p+done,n-done);if(r<0&&errno==EINTR)continue;if(r<=0){e=EIO;break;}done+=(size_t)r;}
 if(!e&&lseek(fd,0,SEEK_SET)<0)e=errno;
 if(!e)e=hash_fd(fd,n,out);
 close(fd);return e;
}
static int prepare_dir(int dir,const struct fde_record *r,struct fde_blob *b){
 if(!b)return EINVAL;
 b->size=0;if(!r||!r->attempt)return EINVAL;
 for(unsigned j=0;j<6;j++){unsigned v=0;for(unsigned k=0;k<32;k++)v|=r->binding[j][k];if(!v)return EINVAL;}
 memset(b->bytes,0,FDE_HEADER);unsigned char *h=b->bytes;size_t trace=0;
 int e=read_fixed(dir,"ioctl-trace.jsonl",h+FDE_HEADER,FDE_TRACE_MAX,&trace);if(e)return e;
 /* Match only the unchanged client's successful nonempty CAPTURE DQ line.
  * Full trace/session/metadata semantics are checked independently by host. */
 unsigned order[3]={0},seen=0,count=0;char line[2048];size_t pos=0;
 while(pos<trace){size_t end=pos;while(end<trace&&h[FDE_HEADER+end]!='\n')end++;
  if(end==trace||end-pos>=sizeof(line))return EPROTO;
  memcpy(line,h+FDE_HEADER+pos,end-pos);line[end-pos]=0;pos=end+1;
  if(strstr(line,"\"request\":3227014673,\"rc\":0,\"errno\":0,\"type\":9,")){
   char *p=strstr(line,",\"pts_us\":");char *q=strstr(line,",\"bytesused\":");char *s=strstr(line,",\"data_offset\":");
   unsigned long long pts=0;unsigned used=0,off=0;
   if(!p||!q||!s||sscanf(p,",\"pts_us\":%llu,",&pts)!=1||sscanf(q,",\"bytesused\":%u,",&used)!=1||sscanf(s,",\"data_offset\":%u",&off)!=1)return EPROTO;
   if(used==off)continue;
   if(used<off||used-off!=FDE_FRAME||pts<1000000||pts>1080000||(pts-1000000)%40000||count>=3)return EPROTO;
   unsigned idx=(unsigned)((pts-1000000)/40000);if(seen&(1u<<idx))return EPROTO;
   seen|=1u<<idx;order[count++]=idx;
  }
 }
 if(count!=3||seen!=7)return EPROTO;
 memcpy(h,"FDE1",4);p32(h+4,1);p32(h+8,FDE_HEADER);p64(h+16,r->attempt);p32(h+24,3);p32(h+28,128);p32(h+32,128);memcpy(h+36,"NV12",4);p32(h+40,FDE_PAYLOAD);p32(h+44,(uint32_t)trace);memcpy(h+48,r->binding,192);
 e=digest(h+FDE_HEADER,trace,h+240);if(e)return e;
 size_t base=FDE_HEADER+trace;
 /* Read actual plane and visible bytes separately; export actual plane only
  * after exact equality. No oracle/source input is read by this component. */
 unsigned char visible[FDE_FRAME];
 for(unsigned arrival=0;arrival<3;arrival++){
  unsigned idx=order[arrival];unsigned char *f=h+base+idx*(16+FDE_FRAME);char name[64];size_t n=0;
  p32(f,idx);p32(f+4,FDE_FRAME);p64(f+8,1000000u+40000u*idx);
  snprintf(name,sizeof(name),"capture-%02u-plane.bin",arrival);e=read_fixed(dir,name,f+16,FDE_FRAME,&n);if(e)return e;if(n!=FDE_FRAME)return EPROTO;
  snprintf(name,sizeof(name),"capture-%02u-visible.nv12",arrival);e=read_fixed(dir,name,visible,FDE_FRAME,&n);if(e)return e;
  if(n!=FDE_FRAME||memcmp(visible,f+16,FDE_FRAME))return EBADMSG;
 }
 size_t total=base+3*(16+FDE_FRAME)+32;p32(h+12,(uint32_t)total);
 e=digest(h,total-32,h+total-32);if(e)return e;b->size=total;return 0;
}
int fde_prepare(const struct fde_record *r,struct fde_blob *b){
 if(b)b->size=0;
 int root=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(root<0)return errno;
 int run=openat(root,"run",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);int e=errno;close(root);if(run<0)return e;
 int dir=openat(run,"iris-decode-evidence",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);e=errno;close(run);if(dir<0)return e;
 struct stat st;if(fstat(dir,&st))e=errno;else if(st.st_uid!=0||(st.st_mode&0022))e=EPERM;else e=prepare_dir(dir,r,b);
 close(dir);return e;
}
#ifdef FDE_TESTING
int fde_test_prepare(int d,const struct fde_record *r,struct fde_blob *b){return prepare_dir(d,r,b);}
#endif
static int64_t now_ns(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return -1;return (int64_t)t.tv_sec*1000000000+t.tv_nsec;}
int fde_write_same_fd(int fd,const struct fde_blob *b,int64_t end){
 if(!b||b->size<FDE_HEADER+32||b->size>FDE_MAX)return EINVAL;
 int flags=fcntl(fd,F_GETFL);if(flags<0)return errno;if(!(flags&O_NONBLOCK))return EINVAL;
 int64_t now=now_ns();if(now<0)return EIO;if(end<=now||end-now>60000000000LL)return EINVAL;
 size_t pos=0;
 while(pos<b->size){now=now_ns();if(now<0)return EIO;if(now>=end)return ETIMEDOUT;
  struct pollfd p={fd,POLLOUT,0};int ms=(int)((end-now)/1000000);if(ms<1)ms=1;
  int rc=poll(&p,1,ms);if(rc<0&&errno==EINTR)continue;if(rc<0)return errno;if(!rc)continue;
  if(p.revents&(POLLERR|POLLHUP|POLLNVAL))return EIO;
  now=now_ns();if(now<0)return EIO;if(now>=end)return ETIMEDOUT;
  size_t n=b->size-pos;if(n>4096)n=4096;ssize_t sent=write(fd,b->bytes+pos,n);
  if(sent<0&&(errno==EINTR||errno==EAGAIN))continue;
  if(sent<=0)return EIO;
  pos+=(size_t)sent;
 }
 return 0;
}
