/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "fd-handoff.h"
#include "video-phase.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <string.h>
#include <sys/socket.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#define HF_MAGIC UINT32_C(0x48463131)
_Static_assert(sizeof(struct hf_wire)==80, "fixed local wire without padding");
static int64_t monotonic(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t) || t.tv_sec<0 || t.tv_nsec<0 || t.tv_nsec>=1000000000 || t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000) return -1;
    return (int64_t)t.tv_sec*1000000000+t.tv_nsec;
}
static int socket_valid(int fd, dev_t dev, ino_t ino)
{
    struct stat st; struct sockaddr_un_dummy { sa_family_t family; char path[108]; } address;
    socklen_t size=sizeof address, n=sizeof(int); int type, f=fcntl(fd,F_GETFL), flags=fcntl(fd,F_GETFD);
    if (f<0 || flags<0 || fstat(fd,&st) || getsockopt(fd,SOL_SOCKET,SO_TYPE,&type,&n) || getsockname(fd,(struct sockaddr *)&address,&size)) return -errno;
    if (!S_ISSOCK(st.st_mode) || st.st_dev!=dev || st.st_ino!=ino || type!=SOCK_SEQPACKET || address.family!=AF_UNIX || size!=sizeof(sa_family_t) || !(f&O_NONBLOCK) || !(flags&FD_CLOEXEC)) return -EPERM;
    return 0;
}
static int tty_valid(int fd, struct stat *st)
{
    struct termios t; int f=fcntl(fd,F_GETFL), flags=fcntl(fd,F_GETFD);
    if (f<0 || flags<0 || fstat(fd,st) || tcgetattr(fd,&t)) return -errno;
    if (!S_ISCHR(st->st_mode) || (f&(O_ACCMODE|O_NONBLOCK))!=(O_RDWR|O_NONBLOCK) || !(flags&FD_CLOEXEC) ||
        (t.c_iflag&(IGNBRK|BRKINT|PARMRK|ISTRIP|INLCR|IGNCR|ICRNL|IXON)) || (t.c_oflag&OPOST) ||
        (t.c_lflag&(ECHO|ECHONL|ICANON|ISIG|IEXTEN)) ||
        (t.c_cflag&(CSIZE|PARENB|CLOCAL|CREAD))!=(CS8|CLOCAL|CREAD) || t.c_cc[VMIN] || t.c_cc[VTIME]) return -EINVAL;
    struct pollfd p={fd,POLLIN,0};
    if (poll(&p,1,0)<0) return -errno;
    return p.revents&(POLLIN|POLLHUP|POLLERR|POLLNVAL) ? -EPROTO : 0;
}
int hf_pair(struct hf_channel *h)
{
    int fds[2]; struct stat rx,tx;
    if (!h) return -EINVAL;
    *h=(struct hf_channel){.rx=-1,.tx=-1};
    if (socketpair(AF_UNIX,SOCK_SEQPACKET|SOCK_NONBLOCK|SOCK_CLOEXEC,0,fds)) return -errno;
    if (fstat(fds[0],&rx) || fstat(fds[1],&tx)) {int e=errno;close(fds[0]);close(fds[1]);return -e;}
    h->rx=fds[0];h->tx=fds[1];h->rx_dev=rx.st_dev;h->rx_ino=rx.st_ino;h->tx_dev=tx.st_dev;h->tx_ino=tx.st_ino;
    return 0;
}
void hf_close_rx(struct hf_channel *h) { if(h&&h->rx>=0){close(h->rx);h->rx=-1;} }
void hf_close_tx(struct hf_channel *h) { if(h&&h->tx>=0){close(h->tx);h->tx=-1;} }
static int stats_valid(const struct sp_stats *s)
{ return s && s->attempts==1 && s->nonce_calls==0 && s->frames==2 && s->rx_bytes==88 && s->tx_bytes==103 && s->challenge_written==1 && s->ack_validated==1 && s->result_written==1; }
int hf_send_once(struct hf_channel *h, int *tty, const tw_result *r, int64_t stamp, int64_t deadline)
{
    int rc=-EINVAL, owned=tty?*tty:-1; struct stat st;
    if (tty) *tty=-1;
    if (!h || !tty) goto done;
    if (h->send_attempted) {rc=-EALREADY;goto done;}
    h->send_attempted=1;
    if (!r || r->status!=TW_OK || r->protocol_status!=SP_OK || r->error_no || !stats_valid(&r->stats)) {rc=-EPERM;goto done;}
    rc=socket_valid(h->tx,h->tx_dev,h->tx_ino);if(rc)goto done;
    rc=tty_valid(owned,&st);if(rc)goto done;
    int64_t now=monotonic();
    if (now<0 || stamp<=0 || stamp>now || stamp>INT64_MAX-VP_TERMINAL_NS) {rc=-EINVAL;goto done;}
    if (now>=deadline || now-stamp>=VP_TERMINAL_NS) {rc=-ETIMEDOUT;goto done;}
    struct hf_wire w={.magic=HF_MAGIC,.version=1,.status=SP_OK,.result_ns=stamp,.device=st.st_dev,.inode=st.st_ino,.rdev=st.st_rdev,
        .rx_bytes=(uint32_t)r->stats.rx_bytes,.tx_bytes=(uint32_t)r->stats.tx_bytes,.attempts=r->stats.attempts,.frames=r->stats.frames,.nonce_calls=r->stats.nonce_calls,
        .challenge_written=(uint32_t)r->stats.challenge_written,.ack_validated=(uint32_t)r->stats.ack_validated,.result_written=(uint32_t)r->stats.result_written};
    union {struct cmsghdr align; unsigned char bytes[CMSG_SPACE(sizeof(int))];} control={0};
    struct iovec iov={&w,sizeof w};struct msghdr msg={.msg_iov=&iov,.msg_iovlen=1,.msg_control=control.bytes,.msg_controllen=sizeof control.bytes};
    struct cmsghdr *c=CMSG_FIRSTHDR(&msg);c->cmsg_level=SOL_SOCKET;c->cmsg_type=SCM_RIGHTS;c->cmsg_len=CMSG_LEN(sizeof owned);memcpy(CMSG_DATA(c),&owned,sizeof owned);
    ssize_t n=sendmsg(h->tx,&msg,MSG_DONTWAIT|MSG_NOSIGNAL);
    rc=n==(ssize_t)sizeof w?0:n<0?-errno:-EPROTO;
    if (!rc && monotonic()>=deadline) rc=-ETIMEDOUT;
done:
    if(owned>=0)(void)close(owned);
    if(h)hf_close_tx(h);
    return rc;
}
/* Kernel-installed SCM_RIGHTS must be closed even for invalid/truncated packets. */
static void close_rights(struct msghdr *msg, struct hf_channel *quarantine)
{
    for(struct cmsghdr *c=CMSG_FIRSTHDR(msg);c;c=CMSG_NXTHDR(msg,c))
        if(c->cmsg_level==SOL_SOCKET&&c->cmsg_type==SCM_RIGHTS&&c->cmsg_len>=CMSG_LEN(0)) {
            size_t bytes=c->cmsg_len-CMSG_LEN(0);
            for(size_t i=0;i+sizeof(int)<=bytes;i+=sizeof(int)){int fd;memcpy(&fd,(unsigned char *)CMSG_DATA(c)+i,sizeof fd);if(fd>=0){if(quarantine){if(quarantine->quarantine_count<4)quarantine->quarantine[quarantine->quarantine_count++]=fd;}else close(fd);}}
        }
}
static ssize_t packet(int sock, struct hf_wire *w, struct msghdr *msg, struct iovec *iov, void *control, size_t size)
{
    memset(control,0,size);*iov=(struct iovec){w,sizeof *w};
    *msg=(struct msghdr){.msg_iov=iov,.msg_iovlen=1,.msg_control=control,.msg_controllen=size};
    return recvmsg(sock,msg,MSG_DONTWAIT|MSG_CMSG_CLOEXEC);
}
static int receive_once(struct hf_channel *h, int confirmed, int *tty, struct hf_receipt *receipt, int validate)
{
    int rc=-EINVAL,fd=-1;struct hf_wire w;struct iovec iov;struct msghdr msg;
    union {struct cmsghdr align; unsigned char bytes[CMSG_SPACE(sizeof(int))];} control;
    if(tty)*tty=-1;
    if(receipt)memset(receipt,0,sizeof *receipt);
    if(!h||!tty||!receipt)return -EINVAL;
    if(h->receive_attempted)return -EALREADY;
    if(confirmed!=1)return -EPERM; /* no receive before waitpid proof */
    h->receive_attempted=1;
    rc=socket_valid(h->rx,h->rx_dev,h->rx_ino);if(rc)goto done;
    ssize_t n=packet(h->rx,&w,&msg,&iov,control.bytes,sizeof control.bytes);
    if(n<0){rc=-errno;goto done;}
    struct cmsghdr *c=CMSG_FIRSTHDR(&msg);
    if(n!=(ssize_t)sizeof w || (msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC)) || !c || CMSG_NXTHDR(&msg,c) || c->cmsg_level!=SOL_SOCKET || c->cmsg_type!=SCM_RIGHTS || c->cmsg_len!=CMSG_LEN(sizeof fd)) {close_rights(&msg,validate?NULL:h);rc=-EPROTO;goto done;}
    memcpy(&fd,CMSG_DATA(c),sizeof fd);
    struct sp_stats s={.rx_bytes=w.rx_bytes,.tx_bytes=w.tx_bytes,.attempts=w.attempts,.frames=w.frames,.nonce_calls=w.nonce_calls,
        .challenge_written=(int)w.challenge_written,.ack_validated=(int)w.ack_validated,.result_written=(int)w.result_written};
    int64_t now=monotonic();
    if(w.magic!=HF_MAGIC||w.version!=1||w.status!=SP_OK||w.reserved||!stats_valid(&s)||now<0||w.result_ns<=0||w.result_ns>now||w.result_ns>INT64_MAX-VP_TERMINAL_NS){rc=-EPROTO;goto done;}
    if(now-w.result_ns>=VP_TERMINAL_NS){rc=-ETIMEDOUT;goto done;}
    struct hf_receipt admitted={.stats=s,.result_ns=w.result_ns,.device=w.device,.inode=w.inode,.rdev=w.rdev};
    if(validate) { rc=hf_validate_receipt(fd,&admitted);if(rc)goto done; }
    /* All other writer ends must have been closed before confirmed flow exit.
     * A duplicate packet OR a still-open writer (EAGAIN) is terminal failure. */
    struct hf_wire extra;ssize_t more=packet(h->rx,&extra,&msg,&iov,control.bytes,sizeof control.bytes);
    if(more!=0 || msg.msg_controllen || (msg.msg_flags&(MSG_TRUNC|MSG_CTRUNC))){if(more>=0)close_rights(&msg,validate?NULL:h);rc=-EPROTO;goto done;}
    *tty=fd;fd=-1;*receipt=admitted;rc=0;
done:
    if(fd>=0){if(validate)close(fd);else if(h->quarantine_count<4)h->quarantine[h->quarantine_count++]=fd;}
    /* Metadata failure retains the local rx socket too: queued/unseen rights
     * must not trigger indirect last-close tty callbacks in PID1. HOLD only. */
    if(validate || !rc)hf_close_rx(h);
    return rc;
}
int hf_receive_once(struct hf_channel *h,int confirmed,int *tty,struct hf_receipt *r)
{ return receive_once(h,confirmed,tty,r,1); }
int hf_receive_metadata_once(struct hf_channel *h,int confirmed,int *tty,struct hf_receipt *r)
{ return receive_once(h,confirmed,tty,r,0); }
int hf_validate_receipt(int fd,const struct hf_receipt *r)
{
    struct stat st;
    if(!r || !stats_valid(&r->stats))return -EPERM;
    int rc=tty_valid(fd,&st);if(rc)return rc;
    return (uint64_t)st.st_dev==r->device && (uint64_t)st.st_ino==r->inode && (uint64_t)st.st_rdev==r->rdev ? 0 : -EPROTO;
}
