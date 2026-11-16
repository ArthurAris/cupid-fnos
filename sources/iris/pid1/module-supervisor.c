/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "module-supervisor.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <stddef.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

/* SHA-256: FIPS 180-4 compression, fixed-width arithmetic; known-answer and
 * padding-boundary tests are provided by the companion host test harness.
 * Local code, no external crypto/runtime/download dependency. */
static const uint32_t K[64] = {
    0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
    0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
    0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
    0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
    0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
    0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
    0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
    0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
};
static uint32_t rr(uint32_t x, unsigned n) { return (x >> n) | (x << (32-n)); }
static void block(uint32_t h[8], const unsigned char p[64]) {
    uint32_t w[64], a=h[0], b=h[1], c=h[2], d=h[3], e=h[4], f=h[5], g=h[6], z=h[7];
    for (unsigned i=0; i<16; i++)
        w[i]=((uint32_t)p[4*i]<<24)|((uint32_t)p[4*i+1]<<16)|((uint32_t)p[4*i+2]<<8)|p[4*i+3];
    for (unsigned i=16; i<64; i++)
        w[i]=w[i-16]+(rr(w[i-15],7)^rr(w[i-15],18)^(w[i-15]>>3))+w[i-7]
             +(rr(w[i-2],17)^rr(w[i-2],19)^(w[i-2]>>10));
    for (unsigned i=0; i<64; i++) {
        uint32_t t=z+(rr(e,6)^rr(e,11)^rr(e,25))+((e&f)^(~e&g))+K[i]+w[i];
        uint32_t u=(rr(a,2)^rr(a,13)^rr(a,22))+((a&b)^(a&c)^(b&c));
        z=g; g=f; f=e; e=d+t; d=c; c=b; b=a; a=t+u;
    }
    h[0]+=a; h[1]+=b; h[2]+=c; h[3]+=d; h[4]+=e; h[5]+=f; h[6]+=g; h[7]+=z;
}
static int hash_fd(int fd, uint64_t size, unsigned char out[32]) {
    uint32_t h[8]={0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19};
    unsigned char p[64]; uint64_t left=size;
    while (left>=64) {
        ssize_t n=read(fd,p,64);
        if (n!=64) return n<0 ? errno : EIO;
        block(h,p); left-=64;
    }
    if (left && read(fd,p,(size_t)left)!=(ssize_t)left) return EIO;
    p[left]=0x80; memset(p+left+1,0,63-(size_t)left);
    if (left>=56) { block(h,p); memset(p,0,64); }
    for (unsigned i=0; i<8; i++) p[63-i]=(unsigned char)((size*8)>>(8*i));
    block(h,p);
    for (unsigned i=0; i<32; i++) out[i]=(unsigned char)(h[i/4]>>(24-8*(i%4)));
    return 0;
}
static int valid(const struct ms_record *r) {
    if (!r || !r->name[0] || !memchr(r->name,0,sizeof r->name) ||
        r->ram_path[0]!='/' || !memchr(r->ram_path,0,sizeof r->ram_path) ||
        !memchr(r->fixedoptions,0,sizeof r->fixedoptions) || r->size>MS_MAX_BYTES) return 0;
    for (const char *p=r->name; *p; p++)
        if (!((*p>='a'&&*p<='z')||(*p>='A'&&*p<='Z')||(*p>='0'&&*p<='9')||*p=='_'||*p=='-')) return 0;
    return 1;
}
static int checked_open(const char *path) {
    int dir=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if (dir<0) return -1;
    char copy[256];
    size_t length=strlen(path);
    if (length>=sizeof copy || path[0]!='/' || !path[1]) {close(dir);errno=EINVAL;return -1;}
    memcpy(copy,path+1,length);
    char *save=NULL, *part=strtok_r(copy,"/",&save);
    while (part) {
        char *next=strtok_r(NULL,"/",&save);
        if (!strcmp(part,".") || !strcmp(part,"..")) {close(dir);errno=EINVAL;return -1;}
        int fd=openat(dir,part,O_RDONLY|O_NOFOLLOW|O_CLOEXEC|O_NONBLOCK|(next?O_DIRECTORY:0));
        int error=errno;close(dir);
        if (fd<0) {errno=error;return -1;}
        if (!next) return fd;
        dir=fd;part=next;
    }
    close(dir);errno=EINVAL;return -1;
}
static int checked_load(const struct ms_record *r, ms_load_fn load, void *ctx) {
    int fd=checked_open(r->ram_path);
    if (fd<0) return errno;
    struct stat before,after; unsigned char digest[32]; int e=0;
    if (fstat(fd,&before)<0) e=errno;
    else if (!S_ISREG(before.st_mode) || before.st_nlink!=1 || before.st_size<0 || (uint64_t)before.st_size!=r->size) e=EINVAL;
    else e=hash_fd(fd,r->size,digest);
    if (!e && memcmp(digest,r->sha256,32)) e=EBADMSG;
    if (!e && fstat(fd,&after)<0) e=errno;
    if (!e && (before.st_dev!=after.st_dev || before.st_ino!=after.st_ino || before.st_size!=after.st_size ||
        before.st_mtim.tv_sec!=after.st_mtim.tv_sec || before.st_mtim.tv_nsec!=after.st_mtim.tv_nsec ||
        before.st_ctim.tv_sec!=after.st_ctim.tv_sec || before.st_ctim.tv_nsec!=after.st_ctim.tv_nsec)) e=ESTALE;
    if (!e && lseek(fd,0,SEEK_SET)<0) e=errno;
    if (!e) { errno=0; int rc=load(fd,r->fixedoptions,ctx); e=rc==0 ? 0 : (errno ? errno : EIO); }
    close(fd); return e;
}
static int real_clock(void *ctx, int64_t *ns) {
    struct timespec t; (void)ctx;
    if (clock_gettime(CLOCK_MONOTONIC,&t)<0) return -1;
    if (t.tv_sec<0 || t.tv_sec>INT64_MAX/1000000000) { errno=EOVERFLOW; return -1; }
    *ns=(int64_t)t.tv_sec*1000000000+t.tv_nsec; return 0;
}
#ifndef MS_NO_PRODUCTION
int ms_finit_module(int fd, const char *fixedoptions, void *unused) {
    (void)unused; return (int)syscall(SYS_finit_module,fd,fixedoptions,0);
}
#endif
struct wire { uint32_t magic; int32_t error; };
_Static_assert(sizeof(struct wire)==8,"fixed binary result");
static struct ms_outcome run_owned(struct ms_state *s, const struct ms_record *r, uint32_t module_ms,
                         int64_t total_ns, ms_load_fn load, ms_clock_fn clock, void *ctx, int tty, int observer_ipc) {
    struct ms_outcome o={.kind=MS_ERROR,.error=EINVAL};
    int fds[2]={-1,-1}, status=0, reaped=0; int64_t now=0, previous=0, deadline;
    if (!s) return o;
    if (s->stopped) return s->last; /* sticky: no open/fork/callback on later calls */
    if (!valid(r) || !load || !module_ms || total_ns<0) goto done;
    if (!clock) clock=real_clock;
    errno=0;
    if (clock(ctx,&now)<0 || now<0 || now>INT64_MAX-(int64_t)module_ms*1000000) {
        o.kind=MS_CLOCK; o.error=errno ? errno : EIO; goto done;
    }
    deadline=now+(int64_t)module_ms*1000000;
    if (total_ns<deadline) deadline=total_ns;
    previous=now;
    if (now>=deadline) { o.kind=MS_TIMEOUT; o.error=ETIMEDOUT; o.timeout=1; goto done; }
    if (pipe2(fds,O_CLOEXEC|O_NONBLOCK)<0) { o.error=errno; goto done; }
    o.pid=fork();
    if (o.pid<0) { o.error=errno; o.pid=0; goto done; }
    if (!o.pid) {
        close(fds[0]);
        if(tty>=0)close(tty);
        if(observer_ipc>=0)close(observer_ipc);
        /* Inherit verified observer group; never setsid/setpgid/exec. */
        struct wire w={0x4d535631u,checked_load(r,load,ctx)};
        ssize_t n=write(fds[1],&w,sizeof w); close(fds[1]);
        _exit(n==(ssize_t)sizeof w ? 0 : 112);
    }
    close(fds[1]); fds[1]=-1;
    for (;;) {
        errno=0;
        if (clock(ctx,&now)<0 || now<previous) { o.kind=MS_CLOCK; o.error=errno ? errno : EIO; break; }
        previous=now;
        if (now>=deadline) { o.kind=MS_TIMEOUT; o.error=ETIMEDOUT; o.timeout=1; break; }
        pid_t p=waitpid(o.pid,&status,WNOHANG);
        if (p<0 && errno!=EINTR) { o.kind=MS_CHILD; o.error=errno; break; }
        if (p==o.pid) {
            reaped=1;
            if (!WIFEXITED(status) || WEXITSTATUS(status)!=0) {
                o.kind=MS_CHILD; o.error=ECHILD;
                o.term_signal=WIFSIGNALED(status) ? WTERMSIG(status) : 0; break;
            }
            unsigned char buf[sizeof(struct wire)+1], extra; struct wire w;
            ssize_t n=read(fds[0],buf,sizeof buf);
            if (n!=(ssize_t)sizeof w || read(fds[0],&extra,1)!=0) {
                o.kind=MS_PROTOCOL; o.error=EPROTO; break;
            }
            memcpy(&w,buf,sizeof w);
            if (w.magic!=0x4d535631u || w.error<0) { o.kind=MS_PROTOCOL; o.error=EPROTO; break; }
            o.error=w.error; o.kind=w.error ? MS_ERROR : MS_OK; break;
        }
        int64_t remaining=deadline-now;
        int ms=remaining>=10000000 ? 10 : (int)((remaining+999999)/1000000);
        /* No pipe-readiness spin, heartbeat/progress callback or diagnostic IO. */
        if (poll(NULL,0,ms)<0 && errno!=EINTR) { o.error=errno; break; }
    }
    if (!reaped) {
        (void)kill(o.pid,SIGKILL); /* kill FIRST; exactly one nonblocking reap */
        if (waitpid(o.pid,&status,WNOHANG)==o.pid) reaped=1;
        o.after_child_incomplete=!reaped;
    }
done:
    if (fds[0]>=0) close(fds[0]);
    if (fds[1]>=0) close(fds[1]);
    s->last=o; if (o.kind!=MS_OK) s->stopped=1;
    return o;
}

struct ms_outcome ms_run(struct ms_state *s,const struct ms_record *r,uint32_t ms,int64_t deadline,ms_load_fn load,ms_clock_fn clock,void *ctx)
{ return run_owned(s,r,ms,deadline,load,clock,ctx,-1,-1); }
struct ms_outcome ms_run_owned(struct ms_state *s,const struct ms_record *r,uint32_t ms,int64_t deadline,ms_load_fn load,ms_clock_fn clock,void *ctx,int tty,int ipc)
{ return run_owned(s,r,ms,deadline,load,clock,ctx,tty,ipc); }
