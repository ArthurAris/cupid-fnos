/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "ram-environment.h"
#include <errno.h>
#include <fcntl.h>
#include <linux/magic.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <unistd.h>

#define RAM_TOKEN "cupid.isolation=iris-decode-once-v1"
static int directory(int parent, const char *name)
{ return openat(parent, name, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC); }
static int bounded_text(int parent, const char *name, char *data, size_t size)
{
    struct stat st;
    int fd = openat(parent, name, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    int result = 0;
    size_t used = 0;
    if (fd < 0) return -errno;
    if (fstat(fd, &st)) { result = -errno; goto done; }
    if (!S_ISREG(st.st_mode)) { result = -EINVAL; goto done; }
    while (used < size - 1) {
        ssize_t n = read(fd, data + used, size - 1 - used);
        if (n < 0) { result = -errno; goto done; }
        if (!n) { data[used] = 0; goto done; }
        if (memchr(data + used, 0, (size_t)n)) { result = -EPROTO; goto done; }
        used += (size_t)n;
    }
    result = -EOVERFLOW;
done:
    if (close(fd) && !result) result = -errno;
    return result;
}
static int exact_cmdline(const char *text)
{
    unsigned tokens = 0;
    const char *p = text;
    while (*p) {
        p += strspn(p, " \t\r\n");
        size_t n = strcspn(p, " \t\r\n");
        if (!n) break;
        if (n == sizeof(RAM_TOKEN) - 1 && !memcmp(p, RAM_TOKEN, n)) tokens++;
        else if ((n >= sizeof("cupid.isolation=") - 1 &&
                  !memcmp(p, "cupid.isolation=", sizeof("cupid.isolation=") - 1)) ||
                 (n >= sizeof("sysrq_always_enabled") - 1 &&
                  !memcmp(p, "sysrq_always_enabled", sizeof("sysrq_always_enabled") - 1)))
            return -EPERM;
        p += n;
    }
    return tokens == 1 ? 0 : -EPERM;
}
static int guard(const char *root_path, int fixture)
{
    int root = -1, proc = -1, sys = -1, kernel = -1, fd = -1, result = 0;
    struct statfs fs;
    struct stat st;
    char cmdline[4096], text[32];
    if (!root_path || (!fixture && getpid() == 1)) return -EPERM;
    root = open(root_path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (root < 0) return -errno;
    proc = directory(root, "proc");
    if (proc < 0) { result = -errno; goto done; }
    if (fstatfs(proc, &fs)) { result = -errno; goto done; }
    if (!fixture && fs.f_type != PROC_SUPER_MAGIC) { result = -EXDEV; goto done; }
    result = bounded_text(proc, "cmdline", cmdline, sizeof cmdline);
    if (result) goto done;
    result = exact_cmdline(cmdline);
    if (result) goto done;
    sys = directory(proc, "sys");
    if (sys < 0) { result = -errno; goto done; }
    kernel = directory(sys, "kernel");
    if (kernel < 0) { result = -errno; goto done; }
    if (fstatfs(kernel, &fs)) { result = -errno; goto done; }
    if (!fixture && fs.f_type != PROC_SUPER_MAGIC) { result = -EXDEV; goto done; }
    fd = openat(kernel, "sysrq", O_WRONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) { result = -errno; goto done; }
    if (fstat(fd, &st)) { result = -errno; goto done; }
    if (!S_ISREG(st.st_mode)) { result = -EINVAL; goto done; }
    /* One fixed write, no host-selected value, retry, truncation or seek. */
    ssize_t written = write(fd, "0\n", 2);
    if (written != 2) { result = written < 0 ? -errno : -EIO; goto done; }
    if (close(fd)) { result = -errno; fd = -1; goto done; }
    fd = -1;
    result = bounded_text(kernel, "sysrq", text, sizeof text);
    if (!result && strcmp(text, "0\n")) result = -EPROTO;
done:
    if (fd >= 0 && close(fd) && !result) result = -errno;
    if (kernel >= 0 && close(kernel) && !result) result = -errno;
    if (sys >= 0 && close(sys) && !result) result = -errno;
    if (proc >= 0 && close(proc) && !result) result = -errno;
    if (root >= 0 && close(root) && !result) result = -errno;
    return result;
}
int iris_ram_environment_guard(void) { return guard("/", 0); }
#ifdef IRIS_RAM_ENV_TESTING
int iris_ram_environment_guard_test(const char *root) { return guard(root, 1); }
#endif
