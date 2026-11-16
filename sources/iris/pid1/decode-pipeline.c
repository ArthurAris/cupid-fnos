/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "decode-pipeline.h"
#include "ram-environment.h"
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <signal.h>
#include <string.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
static int64_t stamp(void){struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0||t.tv_nsec<0||t.tv_nsec>=1000000000||t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000)return -1;return (int64_t)t.tv_sec*1000000000+t.tv_nsec;}
struct ready_wire {int rc;struct dc_ready report;};
static int read_ready(uint64_t attempt,int tty,int ipc,int64_t overall,struct dc_ready *out,struct dc_pipeline_result *ledger){
 int p[2];if(pipe2(p,O_NONBLOCK|O_CLOEXEC))return -errno;
 int64_t deadline=stamp()+INT64_C(1000000000);if(deadline>overall)deadline=overall;
 pid_t child=fork();if(child<0){int e=errno;close(p[0]);close(p[1]);return -e;}
 if(!child){close(p[0]);close(tty);if(ipc>=0)close(ipc);int tx=fcntl(p[1],F_DUPFD_CLOEXEC,10);if(tx<0)_exit(120);if(dup3(tx,3,O_CLOEXEC)<0)_exit(121);close(0);close(1);close(2);if(syscall(SYS_close_range,4u,~0u,0))_exit(122);
  struct ready_wire w={0};w.rc=dc_ready_read(attempt,&w.report);ssize_t z=write(3,&w,sizeof(w));close(3);_exit(z==(ssize_t)sizeof(w)?0:123);}
 ledger->readiness_pid=child;
 close(p[1]);struct ready_wire w={0};size_t n=0;int rc=0,eof=0,status=0,reaped=0;
 while(!eof){if(stamp()>=deadline){rc=-ETIMEDOUT;break;}struct pollfd f={p[0],POLLIN|POLLHUP,0};int q=poll(&f,1,10);if(q<0&&errno==EINTR)continue;if(q<0){rc=-errno;break;}if(!q)continue;
  unsigned char extra;ssize_t z=read(p[0],n<sizeof(w)?(unsigned char *)&w+n:&extra,n<sizeof(w)?sizeof(w)-n:1);
  if(z<0&&(errno==EAGAIN||errno==EINTR))continue;
  if(z<0){rc=-errno;break;}if(!z){eof=1;break;}if(n==sizeof(w)){rc=-EPROTO;break;}n+=(size_t)z;
 }
 close(p[0]);if(rc)(void)kill(child,SIGKILL);
 for(unsigned i=0;i<20;i++){pid_t z=waitpid(child,&status,WNOHANG);if(z==child){reaped=1;break;}if(z<0&&errno!=EINTR)break;struct timespec t={0,1000000};nanosleep(&t,NULL);}
 ledger->readiness_reaped=reaped;ledger->readiness_unresolved=!reaped;
 int64_t finished=stamp();if(!rc&&(finished<0||finished>=deadline))rc=finished<0?-EIO:-ETIMEDOUT;
 if(!rc&&(!reaped||!WIFEXITED(status)||WEXITSTATUS(status)||n!=sizeof(w)))rc=-EPROTO;
 if(!rc&&w.rc)rc=w.rc<0&&w.rc>=-4095?w.rc:-EPROTO;
 if(!rc)*out=w.report;
 return rc;
}
int dc_run_after_result(int tty,const struct hf_receipt *receipt,const struct ms_record *runner,const struct fde_record *bindings,int ipc,struct dc_pipeline *state,struct dc_pipeline_result *out){
 if(!state||!out)return -EINVAL;
 memset(out,0,sizeof(*out));out->stage=DC_STAGE_GUARD;if(state->attempted){out->rc=-EALREADY;return out->rc;}state->attempted=1;
 int receipt_verified=0;
 int rc=-EPERM;if(getpid()==1||getsid(0)!=getpid()||getpgrp()!=getpid()||ipc<0)goto done;
 rc=hf_validate_receipt(tty,receipt);if(rc)goto done;receipt_verified=1;
 if(!runner||strcmp(runner->name,"cupid_iris_decode_runner")||runner->fixedoptions[0]||!bindings){rc=-EPERM;goto done;}
 int64_t now=stamp();if(now<0||receipt->result_ns>INT64_MAX-DC_PHASE_NS){rc=-EIO;goto done;}
 int64_t total=receipt->result_ns+DC_PHASE_NS,load_end=receipt->result_ns+INT64_C(8000000000);
 if(now>=load_end){rc=-ETIMEDOUT;goto done;}
 rc=iris_ram_environment_guard();if(rc)goto done;
 now=stamp();if(now<0||now>=load_end){rc=now<0?-EIO:-ETIMEDOUT;goto done;}
 out->stage=DC_STAGE_LOAD;
 struct ms_state ms={0};out->loader=ms_run_owned(&ms,runner,8000,load_end,ms_finit_module,NULL,NULL,tty,ipc);
 if(out->loader.kind!=MS_OK){rc=-(out->loader.error>0&&out->loader.error<=4095?out->loader.error:EIO);goto done;}
 out->stage=DC_STAGE_READY;
 rc=read_ready(bindings->attempt,tty,ipc,total,&out->ready,out);out->readiness_rc=rc;if(rc)goto done;
 out->stage=DC_STAGE_START;
 rc=dc_start(&state->client,&out->ready,bindings,receipt->result_ns);if(rc)goto done;
 out->stage=DC_STAGE_EXPORT;
 rc=dc_export_same_fd(&state->client,tty,bindings,&out->exported);
 done:out->rc=rc;if(rc&&receipt_verified&&bindings&&bindings->attempt)(void)fdg_emit_same_fd(tty,ipc,receipt->result_ns,bindings,state,out,&out->diagnostic);return rc;
}
