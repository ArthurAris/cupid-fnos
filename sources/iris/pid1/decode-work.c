/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "decode-work.h"
#include "decode-assets.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/sysmacros.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#include "supervisor-sha256.h"
#if defined(DC_TESTING) && defined(DC_TEST_ROOT)
#define ROOT DC_TEST_ROOT
#else
#define ROOT "/run/iris-decode-evidence"
#endif
#define DC_NODE "video999"
static int64_t tick(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000||t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000)return -1;return (int64_t)t.tv_sec*1000000000+t.tv_nsec;}
int dc_ready_validate(const struct dc_ready *r,uint64_t attempt,int64_t now){
 if(!r||!attempt||r->schema!=1||r->attempt!=attempt||r->observed_ns<=0||now<r->observed_ns||now-r->observed_ns>DC_READY_FRESH_NS)return -EPROTO;
#define ONE(x) if(r->x!=1)return -EPERM
#define ZERO(x) if(r->x!=0)return -EPERM
 ONE(decode_enabled);ONE(retention_ready);ONE(prepared);ONE(post_bind);ONE(run_claimed);ONE(run_done);ZERO(run_errno);ZERO(error);
 if(r->stage!=7)return -EPERM;
 ZERO(stage_errno);ONE(ack);ZERO(core_ready);ONE(admission);ZERO(claimed);ZERO(terminal);ZERO(dma_stop_known);
 ONE(decoder_published);ONE(decoder_registered);ZERO(encoder_registered);
 if(!r->node_major||r->node_major>4095||r->node_minor>1048575)return -EPERM;
#undef ONE
#undef ZERO
 return 0;
}
/* Root anchored, no symlink components; all caller paths are local constants. */
static int fixed_open(const char *path,int lastflags){
 int fd=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);if(fd<0)return -1;
 char copy[256];size_t n=strlen(path);if(n>=sizeof(copy)||path[0]!='/'){close(fd);errno=EINVAL;return -1;}
 memcpy(copy,path+1,n);char *save=NULL,*part=strtok_r(copy,"/",&save);
 while(part){char *next=strtok_r(NULL,"/",&save);int f=openat(fd,part,O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK|(next?O_RDONLY|O_DIRECTORY:lastflags));int err=errno;close(fd);if(f<0){errno=err;return -1;}fd=f;part=next;}
 return fd;
}
static int checked_asset(const struct ms_record *r,int exec){
 int fd=fixed_open(r->ram_path,O_RDONLY);if(fd<0)return -errno;struct stat a,b;unsigned char sum[32];int rc=0;
 if(fstat(fd,&a))rc=-errno;
 else if(!S_ISREG(a.st_mode)||a.st_uid||a.st_nlink!=1||(a.st_mode&0022)||a.st_size<0||(uint64_t)a.st_size!=r->size)rc=-EPERM;
 if(!rc){int e=hash_fd(fd,r->size,sum);if(e)rc=-e;else if(memcmp(sum,r->sha256,32))rc=-EBADMSG;}
 if(!rc&&fstat(fd,&b))rc=-errno;
 if(!rc&&(a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec||a.st_ctim.tv_sec!=b.st_ctim.tv_sec||a.st_ctim.tv_nsec!=b.st_ctim.tv_nsec))rc=-ESTALE;
 if(!rc&&lseek(fd,0,SEEK_SET)<0)rc=-errno;
 if(rc||!exec){close(fd);return rc;}
 return fd;
}
/* fexecve gets original checked fd; final RAM assets must remain immutable.
 * Empty env and closed inherited descriptors remove tty/control/IPC access.
 * No setsid/setpgid: client remains in persistent owner's process group. */
static int spawn(int executable,int out,int err,pid_t *pid,int *pidfd,int64_t deadline){
 int p[2];if(pipe2(p,O_NONBLOCK|O_CLOEXEC))return -errno;
 pid_t child=fork();if(child<0){int e=errno;close(p[0]);close(p[1]);return -e;}
 if(!child){
  close(p[0]);int ex=fcntl(executable,F_DUPFD_CLOEXEC,10),status=fcntl(p[1],F_DUPFD_CLOEXEC,10);
  if(ex<0||status<0)_exit(125);
  if(dup2(out,1)<0||dup2(err,2)<0||dup3(ex,3,O_CLOEXEC)<0||dup3(status,4,O_CLOEXEC)<0)_exit(125);
  close(0);
  if(syscall(SYS_close_range,5u,~0u,0))_exit(125);
  char *const argv[]={"iris-decode-client","--execute","--require-retain-safe-driver","--evidence-dir",ROOT,"--device","/dev/" DC_NODE,"--driver","iris_driver","--input","/payload/iris-input.fir","--oracle","/payload/iris-oracle.fnv","--deadline","20",NULL};
  char *const env[]={NULL};fexecve(3,argv,env);int error=errno;ssize_t sent=write(4,&error,sizeof(error));_exit(sent==(ssize_t)sizeof(error)?126:127);
 }
 close(p[1]);*pid=child;*pidfd=(int)syscall(SYS_pidfd_open,child,0);int rc=0;size_t got=0;int error=0;
 if(*pidfd<0)rc=-errno;
 while(!rc){int64_t n=tick();if(n<0){rc=-EIO;break;}if(n>=deadline){rc=-ETIMEDOUT;break;}
  struct pollfd f={p[0],POLLIN|POLLHUP,0};int ms=(int)((deadline-n)/1000000);if(ms<1)ms=1;int q=poll(&f,1,ms);if(q<0&&errno==EINTR)continue;if(q<0){rc=-errno;break;}if(!q)continue;
  ssize_t z=read(p[0],(unsigned char *)&error+got,sizeof(error)-got);if(z<0&&(errno==EINTR||errno==EAGAIN))continue;if(z<0){rc=-errno;break;}if(!z){if(got)rc=-EPROTO;break;}got+=(size_t)z;if(got==sizeof(error)){rc=-(error>0&&error<=4095?error:EPROTO);break;}
 }
 close(p[0]);return rc;
}
static int create_root(void){
 int run=fixed_open("/run",O_RDONLY|O_DIRECTORY);if(run<0)return -errno;
 if(mkdirat(run,"iris-decode-evidence",0700)){int e=errno;close(run);return -e;}
 int d=openat(run,"iris-decode-evidence",O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC);int e=errno;close(run);if(d<0)return -e;return d;
}
static int alive(const struct dc_state *s){
 if(s->client_pid<=0||s->client_pidfd<0)return 0;
 struct pollfd p={s->client_pidfd,POLLIN,0};return poll(&p,1,0)==0;
}
int dc_start(struct dc_state *s,const struct dc_ready *ready,const struct fde_record *record,int64_t result){
 if(!s)return -EINVAL;
 if(s->attempted)return -EALREADY;
 s->attempted=1;s->client_pidfd=-1;
 int rc=-EINVAL;int64_t n=tick();if(!record||!record->attempt||result<=0||result>INT64_MAX-DC_PHASE_NS||n<result)goto fail;
 s->attempt=record->attempt;s->bindings=*record;s->deadline_ns=result+DC_PHASE_NS;s->completion_deadline_ns=result+DC_COMPLETION_NS;if(n>=s->deadline_ns){rc=-ETIMEDOUT;goto fail;}
 rc=dc_ready_validate(ready,record->attempt,n);if(rc)goto fail;
 if(memcmp(record->binding[1],dc_client_record.sha256,32)||memcmp(record->binding[3],dc_stream_record.sha256,32)||memcmp(record->binding[4],dc_oracle_record.sha256,32)){rc=-EBADMSG;goto fail;}
 rc=checked_asset(&dc_stream_record,0);if(rc)goto fail;rc=checked_asset(&dc_oracle_record,0);if(rc)goto fail;
 int executable=checked_asset(&dc_client_record,1);if(executable<0){rc=executable;goto fail;}
 /* Recheck freshness after IO, immediately before creating node/fork. */
 n=tick();rc=dc_ready_validate(ready,record->attempt,n);if(rc||n>=s->deadline_ns){if(!rc)rc=-ETIMEDOUT;close(executable);goto fail;}
 int dev=fixed_open("/dev",O_RDONLY|O_DIRECTORY);if(dev<0){rc=-errno;close(executable);goto fail;}
 if(mknodat(dev,DC_NODE,S_IFCHR|0600,makedev(ready->node_major,ready->node_minor))){rc=-errno;close(dev);close(executable);goto fail;}
 struct stat st;if(fstatat(dev,DC_NODE,&st,AT_SYMLINK_NOFOLLOW)||!S_ISCHR(st.st_mode)||st.st_uid||st.st_rdev!=makedev(ready->node_major,ready->node_minor)){rc=-EPERM;close(dev);close(executable);goto fail;}close(dev);
 int dir=create_root();if(dir<0){rc=dir;close(executable);goto fail;}
 int out=openat(dir,"client-result.json",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);
 int err=openat(dir,"client-stderr.log",O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW|O_CLOEXEC,0600);close(dir);
 if(out<0||err<0){rc=-errno;if(out>=0)close(out);if(err>=0)close(err);close(executable);goto fail;}
 int64_t handshake=tick()+INT64_C(1000000000);if(handshake>s->deadline_ns)handshake=s->deadline_ns;
 rc=spawn(executable,out,err,&s->client_pid,&s->client_pidfd,handshake);close(executable);close(out);close(err);
 s->launched=s->client_pid>0;if(!rc&&!alive(s))rc=-ECHILD;
 if(!rc){s->exec_confirmed=1;return 0;}
fail:s->error=rc;return rc;
}
static int small_file(int dir,const char *name,unsigned char *buf,size_t max,size_t *len){
 int f=openat(dir,name,O_RDONLY|O_NONBLOCK|O_NOFOLLOW|O_CLOEXEC);if(f<0)return errno==ENOENT?-EAGAIN:-errno;
 struct stat a,b;int rc=0;size_t p=0;if(fstat(f,&a))rc=-errno;
 else if(!S_ISREG(a.st_mode)||a.st_nlink!=1||a.st_size<0||(uint64_t)a.st_size>max)rc=-EPROTO;
 while(!rc&&p<(size_t)a.st_size){ssize_t n=read(f,buf+p,(size_t)a.st_size-p);if(n<0&&errno==EINTR)continue;if(n<=0){rc=-EAGAIN;break;}p+=(size_t)n;}
 if(!rc&&fstat(f,&b))rc=-errno;
 if(!rc&&(a.st_size!=b.st_size||a.st_mtim.tv_sec!=b.st_mtim.tv_sec||a.st_mtim.tv_nsec!=b.st_mtim.tv_nsec))rc=-EAGAIN;
 close(f);if(!rc)*len=p;return rc;
}
static int completion(int dir){
 unsigned char text[1025],trace[FDE_TRACE_MAX];size_t n=0,nt=0;int rc=small_file(dir,"client-result.json",text,1024,&n);if(rc)return rc;if(!n)return -EAGAIN;
 if(text[n-1]!='\n')return -EAGAIN;
 text[n]=0;unsigned capture=0;int consumed=0;
 const char *fmt="{\"hardwaredecode\":false,\"status\":\"content_match_not_hardware_proof\",\"frames\":3,\"drc\":1,\"output_dq\":3,\"capture_dq\":%u,\"eos\":true,\"deadline\":false,\"cleanup_ok\":false,\"errno\":0,\"software_fallback\":false,\"hardware_task_proven\":false}\n%n";
 if(sscanf((char *)text,fmt,&capture,&consumed)!=1||consumed!=(int)n||capture<3||capture>16)return -EPROTO;
 rc=small_file(dir,"ioctl-trace.jsonl",trace,sizeof(trace),&nt);if(rc)return rc;
 static const char marker[]="{\"kind\":\"hold_request_external_stop_retain\",\"frames\":3,\"pts_seen_mask\":7,\"errno\":0,\"eos\":true,\"hardware_decode_verified\":false,\"dma_stop_verified\":false,\"kernel_stop_retain_verified\":false}\n";
 size_t m=sizeof(marker)-1;if(nt<m||trace[nt-1]!='\n')return -EAGAIN;
 if(memcmp(trace+nt-m,marker,m))return memmem(trace,nt,"hold_request_external_stop_retain",32)?-EPROTO:-EAGAIN;
 if(nt>m&&trace[nt-m-1]!='\n')return -EPROTO;
 if(memmem(trace,nt-m,"hold_request_external_stop_retain",32))return -EPROTO;
 return 0;
}
struct proof_header {uint32_t magic;int32_t error;uint32_t bytes;int32_t completion_rc;};
static int pipe_write(int fd,const void *v,size_t n,int64_t end){const unsigned char *p=v;while(n){int64_t now=tick();if(now<0||now>=end)return -ETIMEDOUT;ssize_t z=write(fd,p,n>4096?4096:n);if(z<0&&(errno==EAGAIN||errno==EINTR)){struct pollfd f={fd,POLLOUT,0};(void)poll(&f,1,10);continue;}if(z<=0)return -EIO;p+=z;n-=(size_t)z;}return 0;}
int dc_export_same_fd(struct dc_state *s,int tty,const struct fde_record *record,struct dc_export *out){
 if(!out)return -EINVAL;
 memset(out,0,sizeof(*out));int rc=-EINVAL;
 if(!s||!record||s->attempt!=record->attempt||memcmp(s->bindings.binding,record->binding,sizeof(record->binding))||!s->exec_confirmed||s->error)goto done;
 if(s->export_attempted){rc=-EALREADY;goto done;}s->export_attempted=1;
 if(!alive(s)){rc=-ECHILD;goto done;}
 int64_t proof_end=s->completion_deadline_ns+INT64_C(1000000000);if(proof_end>s->deadline_ns)proof_end=s->deadline_ns;
 int p[2];if(pipe2(p,O_NONBLOCK|O_CLOEXEC)){rc=-errno;goto done;}
 pid_t worker=fork();if(worker<0){rc=-errno;close(p[0]);close(p[1]);goto done;}
 if(!worker){
  close(p[0]);int tx=fcntl(p[1],F_DUPFD_CLOEXEC,10);if(tx<0)_exit(120);if(dup3(tx,3,O_CLOEXEC)<0)_exit(121);
  close(0);close(1);close(2);if(syscall(SYS_close_range,4u,~0u,0))_exit(122);
  static struct fde_blob blob;int err=0,completion_rc=0;int dir=fixed_open(ROOT,O_RDONLY|O_DIRECTORY);if(dir<0)err=-errno;
  while(!err){err=completion(dir);if(err!=-EAGAIN)break;int64_t t=tick();if(t<0||t>=s->completion_deadline_ns){err=t<0?-EIO:-ETIMEDOUT;break;}struct timespec delay={0,10000000};nanosleep(&delay,NULL);err=0;}
  completion_rc=err;
  if(dir>=0)close(dir);
  if(!err){
#ifdef DC_TESTING
   int fixture=fixed_open(ROOT,O_RDONLY|O_DIRECTORY);int e=fixture<0?errno:fde_test_prepare(fixture,record,&blob);if(fixture>=0)close(fixture);
#else
   int e=fde_prepare(record,&blob);
#endif
   if(e)err=-e;
  }
  struct proof_header h={UINT32_C(0x44504331),err,err?0:(uint32_t)blob.size,completion_rc};
  int sent=pipe_write(3,&h,sizeof(h),proof_end);if(!sent&&!err)sent=pipe_write(3,blob.bytes,blob.size,proof_end);
  close(3);_exit(sent?123:0);
 }
 close(p[1]);out->worker_pid=worker;
 struct {struct proof_header header;struct fde_blob blob;} received;memset(&received,0,sizeof(received));
 size_t got=0,expected=sizeof(received.header);int eof=0,status=0,reaped=0;rc=0;
 while(!eof){int64_t n=tick();if(n<0||n>=proof_end){rc=-ETIMEDOUT;break;}if(!alive(s)){rc=-ECHILD;break;}
  struct pollfd f={p[0],POLLIN|POLLHUP,0};int q=poll(&f,1,10);if(q<0&&errno==EINTR)continue;if(q<0){rc=-errno;break;}if(!q)continue;
  unsigned char extra;void *dest=got<expected?(unsigned char *)&received+got:&extra;size_t cap=got<expected?expected-got:1;
  ssize_t z=read(p[0],dest,cap);if(z<0&&(errno==EAGAIN||errno==EINTR))continue;if(z<0){rc=-errno;break;}if(!z){eof=1;break;}
  if(got==expected&&expected>sizeof(received.header)){rc=-EPROTO;break;}got+=(size_t)z;
  if(got==sizeof(received.header)&&expected==sizeof(received.header)){
   if(received.header.magic!=UINT32_C(0x44504331)||received.header.completion_rc>0||received.header.completion_rc<-4095||received.header.error>0||received.header.error<-4095||received.header.bytes>FDE_MAX||(received.header.error&&received.header.bytes)){rc=-EPROTO;break;}
   if(!received.header.error&&received.header.bytes<FDE_HEADER+32){rc=-EPROTO;break;}
   expected+=received.header.bytes;
  }
 }
 close(p[0]);
 if(!rc&&got!=expected)rc=-EPROTO;
 /* Only proof worker is killable; it never opened video or inherited tty. */
 if(rc)(void)kill(worker,SIGKILL);
 for(unsigned i=0;i<20;i++){pid_t w=waitpid(worker,&status,WNOHANG);if(w==worker){reaped=1;break;}if(w<0&&errno!=EINTR)break;struct timespec t={0,1000000};nanosleep(&t,NULL);}
 out->worker_reaped=reaped;out->worker_unresolved=!reaped;if(!reaped&&!rc)rc=-ECHILD;
 if(!rc&&(!WIFEXITED(status)||WEXITSTATUS(status)))rc=-EPROTO;
 if(!rc)out->completion_rc=received.header.completion_rc;
 if(rc==-ETIMEDOUT&&got<sizeof(received.header))out->completion_rc=-ETIMEDOUT;
 if(!rc&&received.header.error)rc=received.header.error;
 if(!rc&&!alive(s))rc=-ECHILD;
 if(!rc){received.blob.size=received.header.bytes;out->tx_started=1;s->fde_tx_attempted=1;int e=fde_write_same_fd(tty,&received.blob,s->deadline_ns);if(e)rc=-e;else{out->bytes=received.blob.size;s->exported=1;}}
 out->client_retained=alive(s);
 done:out->rc=rc;if(s&&rc)s->error=rc;return rc;
}
#ifdef DC_TESTING
int dc_test_completion(int d){return completion(d);}
int dc_test_spawn(int ex,int out,int err,pid_t *pid,int *pidfd){return spawn(ex,out,err,pid,pidfd,tick()+INT64_C(1000000000));}
#endif
