/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "decode-pipeline.h"
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "supervisor-sha256.h"
#if defined(FDG_TESTING) && defined(FDG_TEST_ROOT)
#define ROOT FDG_TEST_ROOT
#else
#define ROOT "/run/iris-decode-evidence"
#endif
_Static_assert(FDG_MAX<=16384,"bounded diagnostic");
static int64_t tick(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000||t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000)return -1;return (int64_t)t.tv_sec*1000000000+t.tv_nsec;}
static void p32(unsigned char *p,uint32_t v){for(unsigned i=0;i<4;i++)p[i]=(unsigned char)(v>>(8*i));}
static void p64(unsigned char *p,uint64_t v){for(unsigned i=0;i<8;i++)p[i]=(unsigned char)(v>>(8*i));}
static uint32_t g32(const unsigned char *p){uint32_t v=0;for(unsigned i=0;i<4;i++)v|=(uint32_t)p[i]<<(8*i);return v;}
static int live(const struct dc_state *s){if(s->client_pid<=0||s->client_pidfd<0)return 0;struct pollfd f={s->client_pidfd,POLLIN,0};return poll(&f,1,0)==0;}
static int root_open(void){
 int fd=open("/",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);if(fd<0)return -errno;
 char path[256];
#ifdef FDG_TESTING
 const char *root=getenv("FDG_TEST_ROOT");if(!root||strlen(root)>=sizeof(path)){close(fd);return -EINVAL;}memcpy(path,root,strlen(root)+1);
#else
 if(sizeof(ROOT)>sizeof(path)){close(fd);return -ENAMETOOLONG;}memcpy(path,ROOT,sizeof(ROOT));
#endif
 char *save=NULL,*part=strtok_r(path,"/",&save);
 while(part){int n=openat(fd,part,O_RDONLY|O_DIRECTORY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);int e=errno;close(fd);if(n<0)return -e;fd=n;part=strtok_r(NULL,"/",&save);}
 struct stat st;if(fstat(fd,&st)){int e=errno;close(fd);return -e;}if(st.st_uid||(st.st_mode&0022)){close(fd);return -EPERM;}return fd;
}
/* Only these three compiled filenames. Never read capture/input/kernel logs.
 * Snapshot size is recorded; only exported prefix/tail is envelope-hashed. */
static int section(int dir,const char *name,unsigned char *out,size_t cap,int tail,size_t *used,uint64_t *source,uint32_t *flags,uint32_t trunc){
 *used=0;*source=0;
 if(dir<0)return dir;
 int fd=openat(dir,name,O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);if(fd<0)return -errno;
 struct stat a,b;int rc=0;size_t n=0;off_t start=0;
 if(fstat(fd,&a))rc=-errno;
 else if(!S_ISREG(a.st_mode)||a.st_uid||a.st_nlink!=1||(a.st_mode&0022)||a.st_size<0)rc=-EPERM;
 if(!rc){n=(uint64_t)a.st_size>cap?cap:(size_t)a.st_size;if(tail)start=a.st_size-(off_t)n;}
 size_t got=0;
 while(!rc&&got<n){ssize_t z=pread(fd,out+got,n-got,start+(off_t)got);if(z<0&&errno==EINTR)continue;if(z<=0){rc=z<0?-errno:-EIO;break;}got+=(size_t)z;}
 if(!rc&&fstat(fd,&b))rc=-errno;
 if(!rc&&(a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec||a.st_ctim.tv_sec!=b.st_ctim.tv_sec||a.st_ctim.tv_nsec!=b.st_ctim.tv_nsec))rc=-ESTALE;
 close(fd);if(rc)return rc;
 *source=(uint64_t)a.st_size;
 if((uint64_t)a.st_size>cap)*flags|=trunc;
 if(tail){
  size_t first=0;if(start){while(first<n&&out[first]!='\n')first++;if(first<n)first++;}
  size_t last=n;while(last>first&&out[last-1]!='\n')last--;
  if(last<n)*flags|=16u;
  n=last-first;memmove(out,out+first,n);
 }
 *used=n;return 0;
}
static int digest(const unsigned char *p,size_t n,unsigned char sum[32]){
 int fd=memfd_create("fdg-sha",MFD_CLOEXEC);if(fd<0)return -errno;size_t done=0;int e=0;
 while(done<n){ssize_t z=write(fd,p+done,n-done);if(z<0&&errno==EINTR)continue;if(z<=0){e=z<0?errno:EIO;break;}done+=(size_t)z;}
 if(!e&&lseek(fd,0,SEEK_SET)<0)e=errno;
 if(!e)e=hash_fd(fd,n,sum);
 close(fd);return -e;
}
static int32_t json_errno(const unsigned char *p,size_t n){
 static const char key[]="\"errno\":";size_t found=0;int32_t value=0;
 for(size_t i=0;i+sizeof(key)-1<n;i++)if(!memcmp(p+i,key,sizeof(key)-1)){
  size_t j=i+sizeof(key)-1;int neg=0;if(j<n&&p[j]=='-'){neg=1;j++;}if(j==n||p[j]<'0'||p[j]>'9')return 0;
  int v=0;while(j<n&&p[j]>='0'&&p[j]<='9'){int d=p[j++]-'0';if(v>(4095-d)/10)return 0;v=v*10+d;}
  if(found++)return 0;
  value=neg?-v:v;
 }
 return found?value:0;
}
static int prepare(unsigned char *h,size_t *size,int64_t result,const struct fde_record *b,const struct dc_pipeline *s,const struct dc_pipeline_result *r,int client_alive_observed){
 memset(h,0,FDG_MAX);*size=0;int64_t now=tick();if(now<result||now>=result+DC_PHASE_NS)return -ETIMEDOUT;
 memcpy(h,"FDG1",4);p32(h+4,1);p32(h+8,FDG_HEADER);p64(h+16,b->attempt);p64(h+24,(uint64_t)result);p64(h+32,(uint64_t)now);
#define I(o,v) p32(h+(o),(uint32_t)(v))
 I(40,r->stage);I(44,r->rc);I(52,r->loader.kind);I(56,r->loader.error);I(60,!!r->loader.timeout);I(64,!!r->loader.after_child_incomplete);I(68,r->loader.pid);I(72,r->loader.term_signal);
 I(76,r->readiness_rc);I(80,r->readiness_pid);I(84,!!r->readiness_reaped);I(88,!!r->readiness_unresolved);I(92,s->client.client_pid);I(96,!!s->client.launched);I(100,!!s->client.exec_confirmed);I(104,client_alive_observed);I(108,s->client.error);
 I(112,r->exported.completion_rc);I(116,r->exported.rc);I(120,r->exported.worker_pid);I(124,!!r->exported.worker_reaped);I(128,!!r->exported.worker_unresolved);
 uint32_t flags=0;
 if(r->ready.schema==1){const struct dc_ready *q=&r->ready;flags|=1u;I(144,q->schema);p64(h+152,q->attempt);p64(h+160,(uint64_t)q->observed_ns);
  const int32_t fields[]={q->decode_enabled,q->retention_ready,q->prepared,q->post_bind,q->run_claimed,q->run_done,q->run_errno,q->stage,q->stage_errno,q->error,q->ack,q->core_ready,q->admission,q->claimed,q->terminal,q->dma_stop_known,q->decoder_published,(int32_t)q->node_major,(int32_t)q->node_minor,q->decoder_registered,q->encoder_registered};
  for(unsigned i=0;i<21;i++)I(168+4*i,fields[i]);
 }
 memcpy(h+280,b->binding,192);
 int dir=root_open();size_t n[3]={0};uint64_t source[3]={0};int rc[3];size_t pos=FDG_HEADER;
 rc[0]=section(dir,"client-result.json",h+pos,FDG_RESULT_MAX,0,&n[0],&source[0],&flags,2u);if(!rc[0])I(108,json_errno(h+pos,n[0]));pos+=n[0];
 rc[1]=section(dir,"ioctl-trace.jsonl",h+pos,FDG_TRACE_MAX,1,&n[1],&source[1],&flags,4u);pos+=n[1];
 rc[2]=section(dir,"client-stderr.log",h+pos,FDG_STDERR_MAX,0,&n[2],&source[2],&flags,8u);pos+=n[2];
 if(dir>=0)close(dir);
 for(unsigned i=0;i<3;i++){I(252+4*i,n[i]);I(264+4*i,rc[i]);p64(h+472+8*i,source[i]);}
 I(48,flags);I(12,pos+32);int e=digest(h,pos,h+pos);if(e)return e;*size=pos+32;return 0;
#undef I
}
static int transfer(int fd,const unsigned char *p,size_t n,int64_t end){
 while(n){int64_t now=tick();if(now<0)return -EIO;if(now>=end)return -ETIMEDOUT;
  struct pollfd f={fd,POLLOUT,0};int ms=(int)((end-now)/1000000);if(ms>10)ms=10;if(ms<1)ms=1;int q=poll(&f,1,ms);
  if(q<0&&errno==EINTR)continue;
  if(q<0)return -errno;
  if(!q)continue;
  if(f.revents&(POLLERR|POLLHUP|POLLNVAL))return -EIO;
  now=tick();if(now<0)return -EIO;if(now>=end)return -ETIMEDOUT;
  ssize_t z=write(fd,p,n>4096?4096:n);if(z<0&&(errno==EINTR||errno==EAGAIN))continue;if(z<=0)return z<0?-errno:-EIO;p+=z;n-=(size_t)z;
 }return 0;
}
int fdg_emit_same_fd(int tty,int ipc,int64_t result,const struct fde_record *b,struct dc_pipeline *s,const struct dc_pipeline_result *r,struct fdg_outcome *o){
 if(!o)return -EINVAL;
 memset(o,0,sizeof(*o));int rc=-EINVAL;
 if(!s||!r||!b||!b->attempt||r->rc>=0||r->rc<-4095||r->stage<DC_STAGE_GUARD||r->stage>DC_STAGE_EXPORT||result<=0||result>INT64_MAX-DC_PHASE_NS)goto done;
 if(s->client.fde_tx_attempted||r->exported.tx_started||r->exported.bytes||s->client.exported){rc=-EPERM;goto done;}
 if(s->diagnostic_attempted){rc=-EALREADY;goto done;}
 int64_t now=tick(),end=result+DC_PHASE_NS;if(now<result||now>=end){rc=-ETIMEDOUT;goto done;}
 int flags=fcntl(tty,F_GETFL);if(flags<0){rc=-errno;goto done;}if(!(flags&O_NONBLOCK)){rc=-EINVAL;goto done;}
 s->diagnostic_attempted=1;o->attempted=1;
 /* Include supervision/reaping in <=1s; leave 25ms for final reaping checks. */
 int64_t budget=now+INT64_C(1000000000);if(budget>end)budget=end;int64_t prep_end=budget-INT64_C(25000000);
 int p[2];if(pipe2(p,O_NONBLOCK|O_CLOEXEC)){rc=-errno;goto done;}
 /* Numeric pre-fork owner observation; child closes all inherited pidfds. */
 int client_alive_observed=live(&s->client);
 pid_t child=fork();if(child<0){rc=-errno;close(p[0]);close(p[1]);goto done;}
 if(!child){close(p[0]);close(tty);if(ipc>=0)close(ipc);int tx=fcntl(p[1],F_DUPFD_CLOEXEC,10);if(tx<0)_exit(120);if(dup3(tx,3,O_CLOEXEC)<0)_exit(121);close(0);close(1);close(2);if(syscall(SYS_close_range,4u,~0u,0))_exit(122);
  unsigned char blob[FDG_MAX];size_t size=0;int e=prepare(blob,&size,result,b,s,r,client_alive_observed);if(!e)e=transfer(3,blob,size,prep_end);close(3);_exit(e?123:0);
 }
 close(p[1]);unsigned char blob[FDG_MAX];size_t got=0;int eof=0,status=0,reaped=0;rc=0;
 while(!eof){now=tick();if(now<0||now>=prep_end){rc=-ETIMEDOUT;break;}struct pollfd f={p[0],POLLIN|POLLHUP,0};int q=poll(&f,1,1);if(q<0&&errno==EINTR)continue;if(q<0){rc=-errno;break;}if(!q)continue;
  unsigned char extra;ssize_t z=read(p[0],got<sizeof(blob)?blob+got:&extra,got<sizeof(blob)?sizeof(blob)-got:1);if(z<0&&(errno==EINTR||errno==EAGAIN))continue;if(z<0){rc=-errno;break;}if(!z){eof=1;break;}if(got==sizeof(blob)){rc=-EFBIG;break;}got+=(size_t)z;
 }
 close(p[0]);if(rc)(void)kill(child,SIGKILL);
 for(;;){pid_t z=waitpid(child,&status,WNOHANG);if(z==child){reaped=1;break;}if(z<0&&errno!=EINTR)break;now=tick();if(now<0||now>=budget)break;(void)poll(NULL,0,1);}
 o->worker_reaped=reaped;o->worker_unresolved=!reaped;
 if(!rc&&(!reaped||!WIFEXITED(status)||WEXITSTATUS(status)||got<FDG_HEADER+32||memcmp(blob,"FDG1",4)||g32(blob+12)!=got))rc=-EPROTO;
 if(!rc){now=tick();if(now<result||now>=end)rc=-ETIMEDOUT;}
 if(!rc&&(s->client.fde_tx_attempted||r->exported.tx_started||r->exported.bytes))rc=-EPERM;
 if(!rc){o->tx_started=1;rc=transfer(tty,blob,got,end);if(!rc)o->bytes=got;}
 done:o->rc=rc;return rc;
}
