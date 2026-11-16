/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "coordinator.h"
#include "tty-worker.h"
#include "gadget-worker.h"
#include "fd-handoff.h"
#include "decode-owner.h"
#include "runner-record.h"
#include "decode-bindings.h"
#include "ram-environment.h"
#include "module-records.h"
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#define TOKEN "cupid.isolation=iris-decode-once-v1"
#ifdef UC_VM_FAKE
/* VM-only loader seam: verify/hash all RAM files but never insert a module. */
static int vm_load(int fd,const char *options,void *context)
{ (void)fd;(void)options;(void)context;return 0; }
#define USB_LOAD_CALLBACK vm_load
#else
#define USB_LOAD_CALLBACK ms_finit_module
#endif
static struct hf_channel handoff;
static int usb_load_without_handoff(int fd,const char *options,void *context)
{ hf_close_tx(&handoff); return USB_LOAD_CALLBACK(fd,options,context); }
static struct uc_trace *trace;
static atomic_uint *observation_failed;
static atomic_uint *flow_exit_confirmed;
static atomic_uint *terminal_report_status;
static int terminal_observer_attempted;
static int diagnostic_tty_fd=-1;
static struct dco_result terminal_observer_result;
static struct dco_state decode_owner_state;
struct diagnostic_state { atomic_uint gadget_done, tty_done; struct gw_result gadget; tw_result tty; };
static struct diagnostic_state *diagnostic;
static void hold(void) { for (;;) pause(); }
static int64_t now_ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t) || t.tv_sec<0 ||
        t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000)
        return -1;
    return (int64_t)t.tv_sec*1000000000+t.tv_nsec;
}
static int bounded_text(const char *path,char *text,size_t size)
{
    int fd=open(path,O_RDONLY|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC);
    if (fd<0) return -errno;
    size_t pos=0;
    while (pos<size-1) {
        ssize_t n=read(fd,text+pos,size-1-pos);
        if (n<0) { int e=errno;close(fd);return -e; }
        if (!n) { text[pos]=0;close(fd);return 0; }
        pos+=(size_t)n;
    }
    close(fd);return -EOVERFLOW;
}
static int mount_api(const char *path,const char *type,unsigned long flags,const char *data)
{
    if (mkdir(path,0755) && errno!=EEXIST) return -errno;
    if (mount(type,path,type,flags,data)) return -errno;
    return 0;
}
static int token_exact(const char *p)
{
    while (*p) {
        p+=strspn(p," \t\r\n");
        size_t n=strcspn(p," \t\r\n");
        if (n==strlen(TOKEN)&&!memcmp(p,TOKEN,n)) return 1;
        p+=n;
    }
    return 0;
}
static int mount_option(const char *options,const char *word)
{
    size_t length=strlen(word);
    while (*options) {
        size_t n=strcspn(options,",");
        if (n==length&&!memcmp(options,word,n)) return 1;
        options+=n;
        if (*options==',') options++;
    }
    return 0;
}
static int mount_allowlist(char *text,int composite)
{
    unsigned root=0,proc=0,sys=0,dev=0,run=0,config=0;
    for (char *p=text;*p;) {
        char source[128],target[128],type[64],options[512];
        char *end=strchr(p,'\n');
        if (!end) return -EPROTO;
        *end=0;
        if (sscanf(p,"%127s %127s %63s %511s",source,target,type,options)!=4) return -EPROTO;
        if (strcmp(target,"/") && (!mount_option(options,"nosuid") || !mount_option(options,"noexec"))) return -EPERM;
        if (strcmp(target,"/") && strcmp(target,"/dev") && !mount_option(options,"nodev")) return -EPERM;
        if (!strcmp(target,"/dev") && mount_option(options,"nodev")) return -EPERM;
        if (!strcmp(source,"rootfs")&&!strcmp(target,"/")&&!strcmp(type,"rootfs")) root++;
        else if (!strcmp(source,"proc")&&!strcmp(target,"/proc")&&!strcmp(type,"proc")) proc++;
        else if (!strcmp(source,"sysfs")&&!strcmp(target,"/sys")&&!strcmp(type,"sysfs")) sys++;
        else if (!strcmp(source,"tmpfs")&&!strcmp(target,"/dev")&&!strcmp(type,"tmpfs")) dev++;
        else if (!strcmp(source,"tmpfs")&&!strcmp(target,"/run")&&!strcmp(type,"tmpfs")) run++;
        else if (composite&&!strcmp(source,"configfs")&&!strcmp(target,"/sys/kernel/config")&&!strcmp(type,"configfs")) config++;
        else return -EPERM;
        p=end+1;
    }
    return root==1&&proc==1&&sys==1&&dev==1&&run==1&&config==(unsigned)composite?0:-EPROTO;
}
static int serial_adapter(struct uc_serial_result *out,void *unused)
{
    (void)unused;
    tw_result r;
    int fd=-1; int64_t result_ns=0;
    enum tw_status status=tw_exchange_keep(&r,&fd,&result_ns);
    int64_t deadline=-1;
    unsigned count=atomic_load_explicit(&trace->shared.count,memory_order_acquire);
    if(count && count<=UC_MAX_RECORDS && trace->shared.record[count-1].stage==UC_SERIAL && trace->shared.record[count-1].event==UC_BEGIN)
        deadline=trace->shared.record[count-1].deadline_ns;
    int sent=hf_send_once(&handoff,&fd,&r,result_ns,deadline);
    if(status==TW_OK && sent) { status=TW_PROTOCOL; r.status=status; r.error_no=sent<0?-sent:EIO; }
    diagnostic->tty=r;
    atomic_store_explicit(&diagnostic->tty_done,1,memory_order_release);
    out->status=status==TW_OK?UC_SERIAL_OK:(int)status;
    out->error=r.error_no<0?EIO:r.error_no;
    out->challenge_written=r.stats.challenge_written;
    out->ack_validated=r.stats.ack_validated;
    out->result_written=r.stats.result_written;
    return status==TW_OK?0:-1;
}
static int mount_adapter(struct uc_step_result *out,int64_t deadline,void *unused)
{
    (void)unused;
    hf_close_tx(&handoff); /* mount callback must not retain writer */
    if (now_ns()<0 || now_ns()>=deadline) return -1;
    int r=mount_api("/proc","proc",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);
    if (!r) r=mount_api("/sys","sysfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL);
    if (!r) r=mount_api("/run","tmpfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,"size=1048576,mode=0700");
    if (!r) r=mount_api("/dev","tmpfs",MS_NOSUID|MS_NOEXEC,"size=65536,mode=0700");
    struct utsname uts;
    char cmdline[4096],mounts[8192];
    if (!r && (uname(&uts)||strcmp(uts.release,"6.18.18.c1147-trim")||strcmp(uts.machine,"aarch64"))) r=-EXDEV;
    if (!r) r=bounded_text("/proc/cmdline",cmdline,sizeof cmdline);
    if (!r&&!token_exact(cmdline)) r=-EPERM;
    if (!r) r=iris_ram_environment_guard();
    if (!r) r=bounded_text("/proc/self/mounts",mounts,sizeof mounts);
    if (!r) r=mount_allowlist(mounts,0);
    out->status=r?1:0;out->error=r<0?-r:0;out->complete=!r;out->verified=!r;
    return r?-1:0;

}
static int gadget_adapter(struct uc_step_result *out,int64_t deadline,void *unused)
{
    (void)unused;
    hf_close_tx(&handoff); /* gadget callback must not retain writer */
    struct gw_result result;
    int r=gw_setup(&result,deadline);
    out->status=result.status;out->error=result.error;
    out->complete=result.bound;out->verified=result.local_ready;
    if (!r) {
        char mounts[8192];
        r=bounded_text("/proc/self/mounts",mounts,sizeof mounts);
        if (!r) r=mount_allowlist(mounts,1);
        if (r) {out->status=1;out->error=r<0?-r:EIO;out->verified=0;}
    }
    diagnostic->gadget=result;
    atomic_store_explicit(&diagnostic->gadget_done,1,memory_order_release);
    return r?-1:0;
}

/* PID1 alone receives after normally reaped COMPLETE flow. No reopen path. */
static void terminal_report(void)
{
    int fd=-1;
    struct hf_receipt receipt;
    int confirmed=atomic_load_explicit(flow_exit_confirmed,memory_order_acquire)==1 &&
        !atomic_load_explicit(observation_failed,memory_order_acquire);
    if(!confirmed) {atomic_store(terminal_report_status,2);return;} /* retain socket/queued rights in failed HOLD */
    if(terminal_observer_attempted)return;
    terminal_observer_attempted=1;
    int rc=hf_receive_metadata_once(&handoff,confirmed,&fd,&receipt);
    if(!rc) { diagnostic_tty_fd=fd; rc=dco_run(&decode_owner_state,diagnostic_tty_fd,&receipt,&rp_runner_record,&decode_fde_record,&terminal_observer_result); }
    /* PID1 never validates/reads/writes tty. Original stays retained in HOLD;
     * persistent decoder owner/client remain held on every outcome; no reaper. */
    atomic_store_explicit(terminal_report_status,terminal_observer_result.unresolved?3u:rc?2u:1u,memory_order_release);
}

/* Observer never advances stages or opens a tty. Console I/O is a disposable child. */
static int emit(const char *text,size_t n)
{
    pid_t pid=fork();
    if (!pid) {
        int f=fcntl(1,F_GETFL);
        if (f<0||fcntl(1,F_SETFL,f|O_NONBLOCK)) _exit(1);
        size_t pos=0;
        for (unsigned tries=0;tries<16&&pos<n;tries++) {
            ssize_t r=write(1,text+pos,n-pos);
            if (r>0) pos+=(size_t)r;
            else if (r<0&&errno!=EINTR) _exit(1);
        }
        _exit(pos==n?0:1);
    }
    if (pid<0) return -1;
    int64_t start=now_ns();
    if (start<0) {kill(pid,SIGKILL);return -1;}
    for (;;) {
        int status;
        pid_t r=waitpid(pid,&status,WNOHANG);
        if (r==pid) return WIFEXITED(status)&&WEXITSTATUS(status)==0?0:-1;
        int64_t now=now_ns();
        if (r<0||now<start||now-start>=1500000000) {
            kill(pid,SIGKILL);(void)waitpid(pid,NULL,WNOHANG);return -1;
        }
        usleep(20000);
    }
}
static void observer(void)
{
    unsigned emitted=0;
    size_t bytes=0;
    int64_t started=now_ns();
    if (started<0) {atomic_store(observation_failed,1);uc_latch(trace,TS_FAILURE);hold();}
    for (;;) {
        unsigned state=atomic_load_explicit(&trace->shared.terminal,memory_order_acquire);
        unsigned count=atomic_load_explicit(&trace->shared.count,memory_order_acquire);
        int64_t now=now_ns();
        if (now<started) {atomic_store(observation_failed,1);uc_latch(trace,TS_FAILURE);hold();}
        if (state==TS_COMPLETE && !atomic_load_explicit(flow_exit_confirmed,memory_order_acquire)) {
            if (atomic_load_explicit(observation_failed,memory_order_acquire)) hold();
            usleep(20000);
            continue;
        }
        if (state==TS_ACTIVE && count) {
            const struct uc_record *last=&trace->shared.record[count-1];
            if (last->event==UC_BEGIN && now>=last->deadline_ns &&
                count==atomic_load_explicit(&trace->shared.count,memory_order_acquire) &&
                atomic_load_explicit(&trace->shared.terminal,memory_order_acquire)==TS_ACTIVE)
                uc_latch(trace,TS_STALE);
        }
        while (emitted<count) {
            struct uc_record r=trace->shared.record[emitted];
            char line[256];
            int n=snprintf(line,sizeof line,"USB_RAM_STAGE seq=%u stage=%d event=%d index=%u result=%d errno=%d\n",
                emitted,r.stage,r.event,r.module_index,r.result,r.error);
            if (n<0||(size_t)n>=sizeof line||bytes+(size_t)n>16384||emit(line,(size_t)n)) {
                atomic_store_explicit(observation_failed,1,memory_order_release);
                uc_latch(trace,TS_FAILURE);hold();
            }
            bytes+=(size_t)n;emitted++;
        }
        if (atomic_load_explicit(observation_failed,memory_order_acquire)) hold();
        if (state!=TS_ACTIVE) {
            const char *s=state==TS_COMPLETE?"COMPLETE":state==TS_STALE?"STALE":"FAILURE";
            char line[192];
            int n=snprintf(line,sizeof line,"USB_RAM_TERMINAL status=%s token=" TOKEN " modules=%u no_storage=1 hold=1\n",s,(unsigned)USB_MODULE_COUNT);
            if (n<=0||(size_t)n>=sizeof line||emit(line,(size_t)n))
                atomic_store_explicit(observation_failed,1,memory_order_release);
            hold();
        }
        usleep(20000);
    }
}
int main(void)
{
    if (getpid()!=1) return 1;
    trace=mmap(NULL,sizeof(*trace),PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if (trace==MAP_FAILED) hold();
    if (uc_init(trace)) hold();
    observation_failed=mmap(NULL,sizeof(*observation_failed),PROT_READ|PROT_WRITE,
                            MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if (observation_failed==MAP_FAILED) hold();
    atomic_init(observation_failed,0);
    flow_exit_confirmed=mmap(NULL,sizeof(*flow_exit_confirmed),PROT_READ|PROT_WRITE,
                            MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if (flow_exit_confirmed==MAP_FAILED) hold();
    atomic_init(flow_exit_confirmed,0);
    terminal_report_status=mmap(NULL,sizeof(*terminal_report_status),PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if (terminal_report_status==MAP_FAILED) hold();
    atomic_init(terminal_report_status,0);
    if (!atomic_is_lock_free(observation_failed)||!atomic_is_lock_free(flow_exit_confirmed)||!atomic_is_lock_free(terminal_report_status)) hold();
    diagnostic=mmap(NULL,sizeof(*diagnostic),PROT_READ|PROT_WRITE,MAP_SHARED|MAP_ANONYMOUS,-1,0);
    if (diagnostic==MAP_FAILED) hold();
    memset(diagnostic,0,sizeof(*diagnostic));
    atomic_init(&diagnostic->gadget_done,0);atomic_init(&diagnostic->tty_done,0);
    if (!atomic_is_lock_free(&diagnostic->gadget_done)||!atomic_is_lock_free(&diagnostic->tty_done)) hold();
    if(hf_pair(&handoff)) {uc_latch(trace,TS_FAILURE);hold();}
    pid_t pid=fork();
    if (!pid) { hf_close_rx(&handoff); hf_close_tx(&handoff); observer(); }
    if (pid<0) {uc_latch(trace,TS_FAILURE);hold();}
    pid_t flow=fork();
    if (!flow) {
        hf_close_rx(&handoff);
        const struct uc_hooks hooks={mount_adapter,gadget_adapter};
        unsigned state=uc_run_v2(trace,usb_modules,USB_MODULE_COUNT,usb_load_without_handoff,serial_adapter,NULL,NULL,&hooks);
        hf_close_tx(&handoff);
        _exit(state==TS_COMPLETE?0:1);
    }
    hf_close_tx(&handoff); /* PID1 receiver never owns sender endpoint */
    if (flow<0) {hf_close_rx(&handoff);uc_latch(trace,TS_FAILURE);hold();}
    int64_t started=now_ns();
    if (started<0) {kill(flow,SIGKILL);atomic_store(observation_failed,1);hold();}
    bool flow_done=false;
    for (;;) {
        int status;
        /* Persistent decoder owner/client/control FD are deliberately never
         * reaped, signalled or disposed by the generic PID1 flow observer. */
        pid_t observed=waitpid(pid,&status,WNOHANG);
        if (observed==pid || (observed<0&&errno!=EINTR)) {
            atomic_store_explicit(observation_failed,1,memory_order_release);
            uc_latch(trace,TS_FAILURE);
        }
        if (!flow_done) {
            pid_t result=waitpid(flow,&status,WNOHANG);
            if (result==flow) {
                flow_done=true;
                if (WIFEXITED(status)&&WEXITSTATUS(status)==0 &&
                    atomic_load_explicit(&trace->shared.terminal,memory_order_acquire)==TS_COMPLETE &&
                    !atomic_load_explicit(observation_failed,memory_order_acquire))
                    atomic_store_explicit(flow_exit_confirmed,1,memory_order_release);
                else if (!(WIFEXITED(status) && WEXITSTATUS(status)==1 &&
                           atomic_load_explicit(&trace->shared.terminal,memory_order_acquire)!=TS_COMPLETE)) {
                    atomic_store_explicit(observation_failed,1,memory_order_release);
                    uc_latch(trace,TS_FAILURE);
                }
                terminal_report();
            } else if (result<0&&errno!=EINTR) {
                atomic_store(observation_failed,1);uc_latch(trace,TS_FAILURE);flow_done=true;
            }
        }
        int64_t now=now_ns();
        if (now<started || (!flow_done && now-started>=122000000000)) {
            uc_latch(trace,TS_STALE);
            atomic_store(observation_failed,1);
            if (!flow_done) {kill(flow,SIGKILL);(void)waitpid(flow,NULL,WNOHANG);}
            hold();
        }
        usleep(20000);
    }
    return 1;
}
