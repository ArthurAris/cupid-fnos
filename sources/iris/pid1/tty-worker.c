/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "tty-worker.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <sys/statvfs.h>
#include <sys/random.h>
#include <sys/sysmacros.h>
#include <termios.h>
#include <time.h>
#include <unistd.h>
#include <linux/magic.h>

struct deadline { int64_t start, last; };
static int clock_ns(int64_t *ns)
{
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) < 0) return -1;
    if (ts.tv_sec < 0 || ts.tv_nsec < 0 || ts.tv_nsec >= 1000000000 ||
        ts.tv_sec > (INT64_MAX - ts.tv_nsec) / 1000000000) {
        errno = EOVERFLOW; return -1;
    }
    *ns = (int64_t)ts.tv_sec * 1000000000 + ts.tv_nsec;
    return 0;
}
static enum tw_status check_time(struct deadline *d)
{
    int64_t now;
    if (clock_ns(&now) < 0) return TW_CLOCK;
    if (now < d->last) { errno = 0; return TW_CLOCK; }
    d->last = now;
    if (now - d->start >= TW_WORKER_NS) { errno = ETIMEDOUT; return TW_TIMEOUT; }
    return TW_OK;
}

/* Every component is an ordinary directory opened relative to its pinned
 * parent. No dot/dotdot, parent symlinks, path truncation, or relative roots. */
static int walk(int base, const char *relative)
{
    char component[NAME_MAX + 1];
    int fd = fcntl(base, F_DUPFD_CLOEXEC, 3);
    const char *p = relative;
    if (fd < 0) return -1;
    while (*p) {
        const char *end = strchr(p, '/');
        size_t n = end ? (size_t)(end - p) : strlen(p);
        int next, saved;
        if (!n || n > NAME_MAX || (n == 1 && p[0] == '.') ||
            (n == 2 && p[0] == '.' && p[1] == '.')) {
            close(fd); errno = EINVAL; return -1;
        }
        memcpy(component, p, n); component[n] = '\0';
        next = openat(fd, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        saved = errno;
        if (close(fd) < 0 && next >= 0) {
            saved = errno; close(next); next = -1;
        }
        if (next < 0) { errno = saved; return -1; }
        fd = next;
        if (!end) break;
        p = end + 1;
        if (!*p) { close(fd); errno = EINVAL; return -1; }
    }
    return fd;
}
static int root_open(const char *path)
{
    int root, fd, saved;
    size_t n = strnlen(path, PATH_MAX);
    if (n < 2 || n == PATH_MAX || path[0] != '/' || path[n-1] == '/') {
        errno = EINVAL; return -1;
    }
    root = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return -1;
    fd = walk(root, path + 1); saved = errno;
    if (close(root) < 0 && fd >= 0) { saved = errno; close(fd); fd = -1; }
    errno = saved; return fd;
}
static int provenance(int fd, long magic)
{
    struct statfs fs;
    struct statvfs vfs;
    if (fstatfs(fd, &fs) < 0 || fstatvfs(fd, &vfs) < 0) return -1;
    if (fs.f_type != magic || (magic == TMPFS_MAGIC && (vfs.f_flag & ST_NODEV))) {
        errno = EXDEV; return -1;
    }
    return 0;
}

/* Max 64 metadata bytes followed by observed EOF, not a truncated prefix.
 * Grammar: [0-9]+:[0-9]+ with at most one final LF. Linux dev_t bounds are
 * enforced before mknodat, with a makedev/major/minor round-trip check. */
static int metadata(int fd, dev_t *device, struct deadline *d)
{
    unsigned char text[65]; size_t used = 0, end, i = 0;
    unsigned int values[2] = {0, 0};
    struct stat st;
    if (fstat(fd, &st) < 0) return -1;
    if (!S_ISREG(st.st_mode)) { errno = EINVAL; return -1; }
    for (;;) {
        ssize_t n;
        enum tw_status time_status = check_time(d);
        if (time_status != TW_OK) return -1;
        n = read(fd, text + used, sizeof(text) - used);
        if (n < 0) { if (errno == EINTR) continue; return -1; }
        if (!n) break;
        used += (size_t)n;
        if (used > 64) { errno = EOVERFLOW; return -1; }
    }
    end = used;
    if (end && text[end-1] == '\n') --end;
    for (unsigned int part = 0; part < 2; ++part) {
        size_t begin = i;
        unsigned int max = part ? 1048575u : 4095u;
        while (i < end && text[i] >= '0' && text[i] <= '9') {
            unsigned int digit = (unsigned int)(text[i++] - '0');
            if (values[part] > (max - digit) / 10u) { errno = EOVERFLOW; return -1; }
            values[part] = values[part] * 10u + digit;
        }
        if (i == begin) { errno = EINVAL; return -1; }
        if (!part) {
            if (i == end || text[i++] != ':') { errno = EINVAL; return -1; }
        }
    }
    if (i != end) { errno = EINVAL; return -1; }
    *device = makedev(values[0], values[1]);
    if (major(*device) != values[0] || minor(*device) != values[1]) {
        errno = EOVERFLOW; return -1;
    }
    return 0;
}
static int real_named_node_open(int dirfd, const char *name, dev_t device, enum tw_status *failure)
{
    const int flags = O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW;
    struct stat before, after;
    int fd, saved;
    *failure = TW_NODE_CREATE;
    /* Never adopt an old node: EEXIST is terminal, no unlink/chmod fallback. */
    if (mknodat(dirfd, name, S_IFCHR | 0600, device) < 0) return -1;
    *failure = TW_NODE_VERIFY;
    if (fstatat(dirfd, name, &before, AT_SYMLINK_NOFOLLOW) < 0) return -1;
    if (!S_ISCHR(before.st_mode) || before.st_rdev != device ||
        (before.st_mode & 07777) != 0600 || before.st_nlink != 1) {
        errno = EINVAL; return -1;
    }
    *failure = TW_NODE_OPEN;
    fd = openat(dirfd, name, flags);
    if (fd < 0) return -1;
    *failure = TW_NODE_VERIFY;
    if (fstat(fd, &after) < 0) { saved = errno; close(fd); errno = saved; return -1; }
    if (before.st_dev != after.st_dev || before.st_ino != after.st_ino ||
        !S_ISCHR(after.st_mode) || after.st_rdev != device ||
        (after.st_mode & 07777) != 0600 || after.st_nlink != 1) {
        close(fd); errno = EINVAL; return -1;
    }
    return fd;
}
static int real_node_open(int dirfd, dev_t device, enum tw_status *failure)
{ return real_named_node_open(dirfd, "ttyGS0", device, failure); }
static int configure(int fd)
{
    struct termios t, verify;
    if (tcgetattr(fd, &t) < 0) return -1;
    cfmakeraw(&t);
    t.c_cflag |= CLOCAL | CREAD;
    t.c_cc[VMIN] = 0; t.c_cc[VTIME] = 0;
    if (tcsetattr(fd, TCSANOW, &t) < 0 || tcgetattr(fd, &verify) < 0) return -1;
    if ((verify.c_iflag & (IGNBRK | BRKINT | PARMRK | ISTRIP | INLCR | IGNCR | ICRNL | IXON)) ||
        (verify.c_oflag & OPOST) ||
        (verify.c_lflag & (ECHO | ECHONL | ICANON | ISIG | IEXTEN)) ||
        (verify.c_cflag & (CSIZE | PARENB | CLOCAL | CREAD)) != (CS8 | CLOCAL | CREAD) ||
        verify.c_cc[VMIN] || verify.c_cc[VTIME]) { errno = EINVAL; return -1; }
    return 0;
}

/* Wrap only fd I/O: retain sp_worker's six-second clock and frame parser,
 * while clamping polls and refusing new writes at the whole-child deadline.
 * O_NONBLOCK tty writes cannot raise SIGPIPE (no pipe/socket fd is accepted).
 * Blocking syscalls/hooks remain the external supervisor's responsibility. */
struct bounded_io { const sp_io *base; struct deadline *deadline; };
static int io_time(struct bounded_io *b)
{ return check_time(b->deadline) == TW_OK ? 0 : -1; }
static ssize_t bounded_read(void *p, int fd, void *buf, size_t n)
{
    struct bounded_io *b = p;
    if (io_time(b) < 0) return -1;
    return b->base ? b->base->read_fn(b->base->context, fd, buf, n) : read(fd, buf, n);
}
static ssize_t bounded_write(void *p, int fd, const void *buf, size_t n)
{
    struct bounded_io *b = p;
    if (io_time(b) < 0) return -1;
    return b->base ? b->base->write_fn(b->base->context, fd, buf, n) : write(fd, buf, n);
}
static int bounded_poll(void *p, struct pollfd *fds, nfds_t n, int ms)
{
    struct bounded_io *b = p;
    int remaining;
    if (io_time(b) < 0) return -1;
    remaining = (int)((TW_WORKER_NS - (b->deadline->last - b->deadline->start) + 999999) / 1000000);
    if (ms > remaining) ms = remaining;
    return b->base ? b->base->poll_fn(b->base->context, fds, n, ms) : poll(fds, n, ms);
}
static int bounded_clock(void *p, int64_t *ns)
{
    struct bounded_io *b = p;
    if (io_time(b) < 0) return -1;
    return b->base ? b->base->clock_fn(b->base->context, ns) : clock_ns(ns);
}
static ssize_t bounded_random(void *p, void *buf, size_t n, unsigned int flags)
{
    struct bounded_io *b = p;
    if (io_time(b) < 0) return -1;
    return b->base ? b->base->random_fn(b->base->context, buf, n, flags) : getrandom(buf, n, flags);
}

typedef int (*node_hook)(void *, int, const char *, int, mode_t, dev_t);
static enum tw_status exchange(const char *sysroot, const char *devroot,
                               const sp_io *io, node_hook hook, void *context,
                               tw_result *result, int *keep_fd, int64_t *result_ns)
{
    tw_result r = { .status = TW_INVALID, .protocol_status = SP_INVALID };
    struct deadline d;
    struct bounded_io bound = {io, &d};
    sp_io bounded = {&bound, bounded_read, bounded_write, bounded_poll, bounded_clock, bounded_random};
    int sysfd = -1, devfd = -1, classfd = -1, resolvedfd = -1, metafd = -1, ttyfd = -1;
    char classpath[PATH_MAX], canonical[PATH_MAX], prefix[PATH_MAX];
    struct stat st;
    dev_t device;
    int flags, saved = 0, retained = 0;
    enum tw_status timed;
#define FAIL(s) do { r.status = (s); saved = errno; goto done; } while (0)
#define TICK() do { timed = check_time(&d); if (timed != TW_OK) FAIL(timed); } while (0)
    if (keep_fd) *keep_fd = -1;
    if (result_ns) *result_ns = 0;
    if (!result || (!!keep_fd != !!result_ns)) return TW_INVALID;
    errno = 0;
    if (getpid() == 1) FAIL(TW_NOT_CHILD);
    if (io && (!io->read_fn || !io->write_fn || !io->poll_fn || !io->clock_fn || !io->random_fn))
        FAIL(TW_INVALID);
    /* Parent is responsible for fork/supervision; PID1 is the detectable ban. */
    if (!sysroot) sysroot = "/sys";
    if (!devroot) devroot = "/dev";
    if (!hook && (strcmp(sysroot, "/sys") || strcmp(devroot, "/dev"))) FAIL(TW_INVALID);
    if (clock_ns(&d.start) < 0) FAIL(TW_CLOCK);
    d.last = d.start;
    if ((sysfd = root_open(sysroot)) < 0 || (devfd = root_open(devroot)) < 0) FAIL(TW_PARENT_PATH);
    TICK();
    if (!hook && (provenance(sysfd, SYSFS_MAGIC) < 0 || provenance(devfd, TMPFS_MAGIC) < 0))
        FAIL(TW_PROVENANCE);
    if ((classfd = walk(sysfd, "class/tty")) < 0) FAIL(TW_PARENT_PATH);
    if (strlen(sysroot) + sizeof("/class/tty/ttyGS0") > sizeof(classpath) ||
        strlen(sysroot) + sizeof("/devices/") > sizeof(prefix)) { errno = ENAMETOOLONG; FAIL(TW_CLASS_PATH); }
    if (fstatat(classfd, "ttyGS0", &st, AT_SYMLINK_NOFOLLOW) < 0) FAIL(TW_CLASS_PATH);
    if (!S_ISLNK(st.st_mode)) { errno = EINVAL; FAIL(TW_CLASS_PATH); }
    strcpy(classpath, sysroot); strcat(classpath, "/class/tty/ttyGS0");
    strcpy(prefix, sysroot); strcat(prefix, "/devices/");
    if (!realpath(classpath, canonical)) FAIL(TW_CLASS_PATH);
    TICK();
    if (strncmp(canonical, prefix, strlen(prefix))) { errno = EXDEV; FAIL(TW_CLASS_PATH); }
    if ((resolvedfd = walk(sysfd, canonical + strlen(sysroot) + 1)) < 0) FAIL(TW_PARENT_PATH);
    metafd = openat(resolvedfd, "dev", O_RDONLY | O_NONBLOCK | O_NOFOLLOW | O_CLOEXEC);
    if (metafd < 0) FAIL(TW_METADATA);
    if (metadata(metafd, &device, &d) < 0) {
        saved = errno; timed = check_time(&d); errno = saved;
        FAIL(timed == TW_OK ? TW_METADATA : timed);
    }
    TICK();
    errno = 0;
    if (hook) {
        ttyfd = hook(context, devfd, "ttyGS0",
                     O_RDWR | O_NOCTTY | O_NONBLOCK | O_CLOEXEC | O_NOFOLLOW,
                     S_IFCHR | 0600, device);
        if (ttyfd < 0) FAIL(errno == EEXIST || errno == EPERM ? TW_NODE_CREATE : TW_NODE_OPEN);
    } else {
        enum tw_status failure;
        ttyfd = real_node_open(devfd, device, &failure);
        if (ttyfd < 0) FAIL(failure);
    }
    TICK();
    if (fstat(ttyfd, &st) < 0) FAIL(TW_NODE_VERIFY);
    if (!S_ISCHR(st.st_mode) || st.st_rdev != device) { errno = EINVAL; FAIL(TW_NODE_VERIFY); }
    flags = fcntl(ttyfd, F_GETFL);
    if (flags < 0) FAIL(TW_NODE_VERIFY);
    if ((flags & (O_ACCMODE | O_NONBLOCK)) != (O_RDWR | O_NONBLOCK)) { errno = EINVAL; FAIL(TW_NODE_VERIFY); }
    flags = fcntl(ttyfd, F_GETFD);
    if (flags < 0) FAIL(TW_NODE_VERIFY);
    if (!(flags & FD_CLOEXEC)) { errno = EINVAL; FAIL(TW_NODE_VERIFY); }
    if (configure(ttyfd) < 0) FAIL(TW_TERMIOS);
    TICK();
    errno = 0;
    r.protocol_status = sp_worker_stamped(ttyfd, ttyfd, &bounded, &r.stats, result_ns);
    /* sp_worker has status+stats, not a saved-errno contract. Do not report
     * incidental EAGAIN/EINTR left by successful I/O as a protocol error. */
    saved = (r.protocol_status == SP_IO || r.protocol_status == SP_NONCE_FAILURE ||
             r.protocol_status == SP_CLOCK) ? errno : 0;
    r.status = r.protocol_status == SP_OK ? TW_OK : TW_PROTOCOL;
    timed = check_time(&d);
    if (timed != TW_OK) FAIL(timed);
done:
    /* No subsequent tty writes, retries, flushing, draining or node removal. */
    {
        retained = keep_fd && r.status == TW_OK;
        int fds[] = {retained ? -1 : ttyfd, metafd, resolvedfd, classfd, devfd, sysfd};
        for (size_t i = 0; i < sizeof(fds)/sizeof(fds[0]); ++i) {
            if (fds[i] >= 0 && close(fds[i]) < 0 && r.status == TW_OK) {
                r.status = TW_CLOSE; saved = errno;
            }
        }
    }
    if (r.status == TW_OK) {
        timed = check_time(&d);
        if (timed != TW_OK) { r.status = timed; saved = errno; }
    }
    if (keep_fd) {
        if (r.status == TW_OK) *keep_fd = ttyfd;
        else { if (retained && ttyfd >= 0) (void)close(ttyfd); *result_ns = 0; }
    }
    r.error_no = saved;
    *result = r;
    errno = saved;
    return r.status;
#undef FAIL
#undef TICK
}

enum tw_status tw_exchange(const char *sysfsroot, const char *ramdevdir,
                           const sp_io *test_io, tw_result *result)
{ return exchange(sysfsroot, ramdevdir, test_io, NULL, NULL, result, NULL, NULL); }
enum tw_status tw_exchange_keep(tw_result *result, int *fd, int64_t *result_ns)
{ return exchange(NULL, NULL, NULL, NULL, NULL, result, fd, result_ns); }
#ifdef TW_TESTING
enum tw_status tw_exchange_test(const char *sysfsroot, const char *ramdevdir,
                                const sp_io *test_io,
                                const struct tw_test_ops *ops,
                                tw_result *result)
{
    if (!ops || !ops->node_open_fn || !sysfsroot || !ramdevdir) {
        if (result) *result = (tw_result){ .status = TW_INVALID, .protocol_status = SP_INVALID };
        return TW_INVALID;
    }
    return exchange(sysfsroot, ramdevdir, test_io, ops->node_open_fn, ops->context, result, NULL, NULL);
}
enum tw_status tw_exchange_keep_test(const char *sysfsroot, const char *ramdevdir,
                                    const sp_io *io, const struct tw_test_ops *ops,
                                    tw_result *result, int *fd, int64_t *result_ns)
{
    if (!ops || !ops->node_open_fn || !sysfsroot || !ramdevdir || !fd || !result_ns) return TW_INVALID;
    return exchange(sysfsroot, ramdevdir, io, ops->node_open_fn, ops->context, result, fd, result_ns);
}
#endif
const char *tw_status_name(enum tw_status status)
{
    static const char *const names[] = {
        "ok", "invalid", "not-child", "clock", "timeout", "parent-path",
        "provenance", "class-path", "metadata", "node-create", "node-open",
        "node-verify", "termios", "protocol", "close"
    };
    return (unsigned int)status < sizeof(names)/sizeof(names[0]) ? names[status] : "unknown";
}
