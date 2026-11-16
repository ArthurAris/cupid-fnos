/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "decode-owner.h"
#include "decode-assets.h"
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>
#define DCO_MAGIC UINT32_C(0x44434f31)
enum { W_READY=1, W_GO=2, W_DONE=3 };
/* Local numeric protocol. Never a kernel-status or evidence parser. */
struct wire {
 uint32_t magic, version, type, issues;
 uint64_t attempt;
 int64_t deadline;
 int32_t owner, session, group, rc, client, retained;
 uint64_t bytes;
};
_Static_assert(sizeof(struct wire)<=128,"small fixed SEQPACKET record");
struct context {
 const struct ms_record *runner;
 const struct fde_record *record;
#ifdef DC_OWNER_TESTING
 const struct dco_test *test;
#endif
};
static int64_t tick(void)
{
 struct timespec t;
 if(clock_gettime(CLOCK_MONOTONIC,&t)||t.tv_sec<0||t.tv_nsec<0||
    t.tv_nsec>=1000000000||t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000)return -1;
 return (int64_t)t.tv_sec*1000000000+t.tv_nsec;
}
static void hold(void){for(;;)(void)pause();}
static int packet_send(int fd,const struct wire *w)
{
 ssize_t n=send(fd,w,sizeof(*w),MSG_DONTWAIT|MSG_NOSIGNAL);
 return n==(ssize_t)sizeof(*w)?0:n<0?-errno:-EPROTO;
}
static int packet_receive(int fd,struct wire *w,pid_t expected)
{
 union {struct cmsghdr align;unsigned char bytes[CMSG_SPACE(sizeof(struct ucred))+CMSG_SPACE(sizeof(int)*16)];} control;
 struct iovec i={w,sizeof(*w)};
 struct msghdr m={.msg_iov=&i,.msg_iovlen=1,.msg_control=control.bytes,.msg_controllen=sizeof(control.bytes)};
 ssize_t n=recvmsg(fd,&m,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);
 if(n<0)return -errno;
 int credentials=0,bad=0;
 for(struct cmsghdr *c=CMSG_FIRSTHDR(&m);c;c=CMSG_NXTHDR(&m,c)){
  if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SCM_RIGHTS){
   if(c->cmsg_len>=CMSG_LEN(0)){
    size_t count=(c->cmsg_len-CMSG_LEN(0))/sizeof(int);
    for(size_t j=0;j<count;j++){int received;memcpy(&received,(unsigned char *)CMSG_DATA(c)+j*sizeof(int),sizeof(int));close(received);}
   }
   bad=1;
  }else if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SCM_CREDENTIALS&&c->cmsg_len==CMSG_LEN(sizeof(struct ucred))){
   struct ucred u;memcpy(&u,CMSG_DATA(c),sizeof(u));
   if(u.pid!=expected)bad=1;
   credentials++;
  }else bad=1;
 }
 if(n!=(ssize_t)sizeof(*w)||(m.msg_flags&(MSG_TRUNC|MSG_CTRUNC))||bad||credentials!=1)return -EPROTO;
 return 0;
}
static int base_valid(const struct wire *w,uint32_t type,pid_t owner,uint64_t attempt,int64_t deadline)
{
 return w->magic==DCO_MAGIC&&w->version==1&&w->type==type&&!(w->issues&~DCO_ISSUES_ALL)&&
  w->attempt==attempt&&w->deadline==deadline&&w->owner==owner&&
  w->session==owner&&w->group==owner;
}
static int empty_payload(const struct wire *w)
{return !w->issues&&!w->rc&&!w->client&&!w->retained&&!w->bytes;}
static int live_child(pid_t pid,int *exited)
{
 siginfo_t i={0};
 if(waitid(P_PID,(id_t)pid,&i,WEXITED|WNOWAIT|WNOHANG))return errno==EINTR?-EAGAIN:-ECHILD;
 if(i.si_pid==pid){*exited=1;return -ECHILD;}
 return i.si_pid?-EPROTO:0;
}
static int terminal_valid(const struct wire *w)
{
 if(w->rc>0||w->rc<-4095||w->client<0||w->retained<0||w->retained>1||w->bytes>FDE_MAX)return 0;
 if(w->retained&&w->client<=0)return 0;
 if(!w->rc&&(w->issues||!w->retained||!w->bytes))return 0;
 return 1;
}
static int receipt_valid(const struct hf_receipt *r)
{
 if(!r||r->result_ns<=0||r->result_ns>INT64_MAX-DCO_PHASE_NS)return 0;
 const struct sp_stats *s=&r->stats;
 return s->challenge_written==1&&s->ack_validated==1&&s->result_written==1&&
  s->attempts==1&&s->frames>0&&s->frames<=SP_MAX_FRAMES&&s->nonce_calls==0&&
  s->rx_bytes>0&&s->rx_bytes<=SP_MAX_RX&&s->tx_bytes>0&&s->tx_bytes<=SP_MAX_TX;
}
static int pins_valid(const struct ms_record *r,const struct fde_record *f)
{
 static const unsigned char runner_hash[32]={0x5e,0xbb,0x8e,0xb8,0x77,0xf5,0xd9,0x17,0x9c,0xd6,0x9a,0xca,0x1a,0x4e,0x0e,0x62,0xe3,0x2e,0x63,0x36,0x9b,0x12,0x44,0xb0,0x80,0xb7,0xf1,0x0b,0xca,0xb9,0xb7,0xf2};
 if(!r||!f)return -ENOENT;
 if(memcmp(r->name,"cupid_iris_decode_runner",sizeof("cupid_iris_decode_runner"))||
  memcmp(r->ram_path,"/payload/cupid_iris_decode_runner.ko",sizeof("/payload/cupid_iris_decode_runner.ko"))||
  r->size!=370248||r->fixedoptions[0]||memcmp(r->sha256,runner_hash,32)||!f->attempt)return -EPERM;
 for(unsigned i=0;i<6;i++){unsigned value=0;for(unsigned j=0;j<32;j++)value|=f->binding[i][j];if(!value)return -ENOENT;}
 if(memcmp(f->binding[1],dc_client_record.sha256,32)||memcmp(f->binding[3],dc_stream_record.sha256,32)||memcmp(f->binding[4],dc_oracle_record.sha256,32))return -EBADMSG;
 return 0;
}
static int perform(int tty,const struct hf_receipt *receipt,int ipc,struct dc_pipeline *s,struct dc_pipeline_result *r,const struct context *c)
{
#ifdef DC_OWNER_TESTING
 if(c->test)return c->test->work(tty,receipt,s,r,c->test->context);
#endif
#if DCO_PRODUCTION_PINNED
 return dc_run_after_result(tty,receipt,c->runner,c->record,ipc,s,r);
#else
 (void)tty;(void)receipt;(void)ipc;(void)s;(void)r;(void)c;return -ENOENT;
#endif
}
static void child(int sock,int tty,const struct hf_receipt *receipt,int64_t deadline,uint64_t attempt,const struct context *c,pid_t parent)
{
 if(setsid()!=getpid())hold();
 struct wire w={.magic=DCO_MAGIC,.version=1,.type=W_READY,.attempt=attempt,
  .deadline=deadline,.owner=getpid(),.session=getsid(0),.group=getpgrp()};
#ifdef DC_OWNER_TESTING
 if(c->test&&c->test->fault==DCO_TEST_NO_READY)hold();
 if(c->test&&c->test->fault==DCO_TEST_BAD_READY)w.version=2;
#endif
 if(packet_send(sock,&w))hold();
 for(;;){
  int64_t n=tick();if(n<0||n>=deadline)hold();
  struct wire go={0};int rc=packet_receive(sock,&go,parent);
  if(!rc){if(!base_valid(&go,W_GO,getpid(),attempt,deadline)||!empty_payload(&go))hold();break;}
  if(rc!=-EAGAIN&&rc!=-EWOULDBLOCK&&rc!=-EINTR)hold();
  (void)poll(NULL,0,1);
 }
 int64_t before=tick();if(before<receipt->result_ns||before>=deadline)hold();
 struct dc_pipeline state={0};struct dc_pipeline_result result={0};
 int rc=perform(tty,receipt,sock,&state,&result,c);
#ifdef DC_OWNER_TESTING
 if(c->test&&c->test->fault==DCO_TEST_EXIT_BEFORE_DONE)_exit(23);
#endif
 /* One bounded local numeric packet; no tty IO or kernel interpretation here.
  * Keep state, inherited tty, client pidfd and IPC alive on this stack in HOLD. */
 w.type=W_DONE;w.rc=rc;w.client=state.client.client_pid;
 w.retained=result.exported.client_retained;w.bytes=result.exported.bytes;
 w.issues=(result.loader.timeout?DCO_ISSUE_LOADER_TIMEOUT:0u)|
  (result.loader.after_child_incomplete?DCO_ISSUE_LOADER_UNRESOLVED:0u)|
  (result.readiness_unresolved?DCO_ISSUE_READINESS_UNRESOLVED:0u)|
  (result.exported.worker_unresolved?DCO_ISSUE_EXPORT_UNRESOLVED:0u)|
  ((state.client.launched&&(!state.client.exec_confirmed||!w.retained))?DCO_ISSUE_CLIENT_UNCONFIRMED:0u);
#ifdef DC_OWNER_TESTING
 if(c->test&&c->test->fault==DCO_TEST_BAD_DONE)w.rc=1;
#endif
 (void)packet_send(sock,&w);
#ifdef DC_OWNER_TESTING
 if(c->test&&c->test->fault==DCO_TEST_DUP_DONE)(void)packet_send(sock,&w);
#endif
 hold();
}
static int __attribute__((unused)) observe(struct dco_state *s,int tty,const struct hf_receipt *receipt,const struct context *c,
 int64_t budget,uint64_t attempt,struct dco_result *r)
{
 int ipc[2]={-1,-1},rc=0;int64_t n=tick(),last=n;
 if(tty<0||!receipt_valid(receipt)||n<receipt->result_ns)return r->rc=-EINVAL;
 int64_t deadline=receipt->result_ns+budget;r->deadline_ns=deadline;
 if(n>=deadline){r->timed_out=1;return r->rc=-ETIMEDOUT;}
 struct sigaction action;
 if(sigaction(SIGCHLD,NULL,&action))return r->rc=-errno;
 if(action.sa_handler==SIG_IGN||(action.sa_flags&SA_NOCLDWAIT))return r->rc=-EPERM;
 if(socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0,ipc))return r->rc=-errno;
 int yes=1;
 if(setsockopt(ipc[0],SOL_SOCKET,SO_PASSCRED,&yes,sizeof(yes))||setsockopt(ipc[1],SOL_SOCKET,SO_PASSCRED,&yes,sizeof(yes))){rc=-errno;close(ipc[0]);close(ipc[1]);return r->rc=rc;}
 pid_t parent=getpid(),pid=fork();
 if(pid<0){rc=-errno;close(ipc[0]);close(ipc[1]);return r->rc=rc;}
 if(!pid){close(ipc[0]);child(ipc[1],tty,receipt,deadline,attempt,c,parent);_exit(127);}
 close(ipc[1]);s->owner_pid=pid;s->control_fd=ipc[0];r->owner_pid=pid;r->identity_retained=1;r->unresolved=1;
 for(;;){
  n=tick();if(n<0||n<last){rc=-EIO;break;}last=n;
  if(n>=deadline){rc=-ETIMEDOUT;r->timed_out=1;break;}
  rc=live_child(pid,&r->owner_exited);if(rc==-EAGAIN)continue;if(rc)break;
  struct wire w={0};rc=packet_receive(ipc[0],&w,pid);
  if(!rc){
   if(!r->go_sent){
    if(!base_valid(&w,W_READY,pid,attempt,deadline)||!empty_payload(&w)||getpgid(pid)!=pid||getsid(pid)!=pid||pid==getpgrp()){rc=-EPROTO;break;}
    r->group_verified=1;
    n=tick();if(n<last||n<0){rc=-EIO;break;}last=n;
    if(n>=deadline){rc=-ETIMEDOUT;r->timed_out=1;break;}
    w.type=W_GO;rc=packet_send(ipc[0],&w);if(rc)break;r->go_sent=1;
   }else{
    if(!base_valid(&w,W_DONE,pid,attempt,deadline)||!terminal_valid(&w)){rc=-EPROTO;break;}
    /* A live HOLD is required. No EOF requirement; immediately queued duplicate
     * packets fail. Future silence is not inferred from this one observation. */
    unsigned char extra;ssize_t z=recv(ipc[0],&extra,1,MSG_DONTWAIT|MSG_PEEK);
    if(z>=0||(errno!=EAGAIN&&errno!=EWOULDBLOCK)){rc=-EPROTO;break;}
    rc=live_child(pid,&r->owner_exited);if(rc)break;
    n=tick();if(n<last||n<0){rc=-EIO;break;}
    if(n>=deadline){rc=-ETIMEDOUT;r->timed_out=1;break;}
    r->done_received=1;r->inner_issues=w.issues;r->client_pid=w.client;r->client_retained=w.retained;r->evidence_bytes=w.bytes;rc=w.rc;break;
   }
  }else if(rc!=-EAGAIN&&rc!=-EWOULDBLOCK&&rc!=-EINTR)break;
  (void)poll(NULL,0,1);
 }
 int exited=r->owner_exited;int live=live_child(pid,&exited);r->owner_exited=exited;r->owner_live=!live;
 /* Never signal, reap, close control, close tty, emit fallback, or retry. A
  * zombie retains PID identity too; kernel release hooks remain a prerequisite.
  * unresolved is always true: numeric DONE cannot establish DMA quiescence. */
 r->rc=rc;return rc;
}
static int begin(struct dco_state *s,struct dco_result *r)
{
 if(!s||!r)return -EINVAL;
 *r=(struct dco_result){0};
 if(s->attempted)return r->rc=-EALREADY;
 s->attempted=1;s->control_fd=-1;r->attempted=1;return 0;
}
int dco_run(struct dco_state *s,int tty,const struct hf_receipt *receipt,
 const struct ms_record *runner,const struct fde_record *record,struct dco_result *r)
{
 int rc=begin(s,r);if(rc)return rc;
 rc=pins_valid(runner,record);if(rc)return r->rc=rc;
#if DCO_PRODUCTION_PINNED
 struct context c={.runner=runner,.record=record};
 return observe(s,tty,receipt,&c,DCO_PHASE_NS,record->attempt,r);
#else
 (void)tty;(void)receipt;return r->rc=-ENOENT;
#endif
}
#ifdef DC_OWNER_TESTING
int dco_run_test(struct dco_state *s,int tty,const struct hf_receipt *receipt,
 const struct dco_test *test,struct dco_result *r)
{
 int rc=begin(s,r);if(rc)return rc;
 if(!test||!test->work||test->budget_ns<=0||test->budget_ns>DCO_PHASE_NS||test->fault<DCO_TEST_NORMAL||test->fault>DCO_TEST_NO_READY)return r->rc=-EINVAL;
 struct context c={.test=test};return observe(s,tty,receipt,&c,test->budget_ns,1,r);
}
#endif
