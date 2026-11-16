/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * oracle-init.c - minimal read-only PID1 boot oracle for the FNOS kernel.
 *
 * Deliberately does NOT: write block devices, enter external-disk set mode,
 * initialize networking, start a shell, or start any FNOS service.
 * The only filesystem-changing operations are the three initramfs mounts.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/syscall.h>
#include <sys/utsname.h>

#ifndef RB_POWER_OFF
#define RB_POWER_OFF 0x4321fedc
#endif
#ifndef SYS_faccessat2
#define SYS_faccessat2 439
#endif

#define ORACLE_RELEASE "6.18.18.c1147-trim"
#define ORACLE_TOKEN "cupid.oracle=boot-v1"
#define TRIMACL_DFD (-21582)

static void say(const char *fmt, ...)
{
	char buf[1024];
	va_list ap;
	int n;

	va_start(ap, fmt);
	n = vsnprintf(buf, sizeof(buf), fmt, ap);
	va_end(ap);
	if (n <= 0)
		return;
	if ((size_t)n >= sizeof(buf))
		n = (int)sizeof(buf) - 1;
	if (write(STDOUT_FILENO, buf, (size_t)n) < 0)
		return;
}

static void __attribute__((noreturn)) power_off(void)
{
	int err;

	sync();
	errno = 0;
	if (reboot(RB_POWER_OFF) != 0) {
		err = errno;
		say("REBOOT RB_POWER_OFF FAIL errno=%d (%s)\n", err,
		    strerror(err));
	} else {
		say("REBOOT RB_POWER_OFF returned unexpectedly\n");
	}
	say("PID1_BLOCK waiting for parent timeout\n");
	for (;;)
		pause();
}

static ssize_t read_text(const char *path, char *buf, size_t cap)
{
	int fd;
	size_t off = 0;

	if (cap < 2)
		return -1;
	fd = open(path, O_RDONLY | O_CLOEXEC);
	if (fd < 0)
		return -1;
	while (off < cap - 1) {
		ssize_t n = read(fd, buf + off, cap - 1 - off);

		if (n < 0) {
			close(fd);
			return -1;
		}
		if (n == 0)
			break;
		off += (size_t)n;
	}
	if (close(fd) != 0)
		return -1;
	buf[off] = '\0';
	return (ssize_t)off;
}

/* Independent whitespace-delimited token, never a substring match. */
static int has_token(const char *s, const char *want)
{
	size_t want_len = strlen(want);

	while (*s != '\0') {
		const char *start;

		while (*s == ' ' || *s == '\t' || *s == '\n' || *s == '\r')
			s++;
		start = s;
		while (*s != '\0' && *s != ' ' && *s != '\t' &&
		       *s != '\n' && *s != '\r')
			s++;
		if ((size_t)(s - start) == want_len &&
		    memcmp(start, want, want_len) == 0)
			return 1;
	}
	return 0;
}

static int mount_one(const char *type, const char *target)
{
	int err;

	errno = 0;
	if (mount(type, target, type, 0, NULL) == 0) {
		say("MOUNT %-8s %s OK\n", target, type);
		return 0;
	}
	err = errno;
	if (err == EBUSY) {
		say("MOUNT %-8s %s EBUSY already-mounted\n", target, type);
		return 0;
	}
	say("MOUNT %-8s %s FAIL errno=%d (%s)\n", target, type, err,
	    strerror(err));
	return -1;
}

static void show_fs(const char *buf)
{
	const char *p = buf;

	while (*p != '\0') {
		const char *nl = strchr(p, '\n');
		size_t len = nl ? (size_t)(nl - p) : strlen(p);
		char line[256];

		if (len >= sizeof(line))
			len = sizeof(line) - 1;
		memcpy(line, p, len);
		line[len] = '\0';
		if (strstr(line, "proc") || strstr(line, "sysfs") ||
		    strstr(line, "devtmpfs") || strstr(line, "tmpfs") ||
		    strstr(line, "btrfs"))
			say("FILESYSTEMS %s\n", line);
		if (nl == NULL)
			break;
		p = nl + 1;
	}
}

/*
 * Read-only faccessat2 query. The special dfd is not a real directory fd;
 * its result is printed raw and is never assumed or required to have any
 * particular value by this probe.
 */
static long probe_faccess(int dfd, const char *tag)
{
	long rc;
	int err;

	errno = 0;
	rc = syscall(SYS_faccessat2, (long)dfd, "/", (long)F_OK, 0L);
	err = errno;
	say("FACCESSAT2 %-10s dfd=%d rc=%ld errno=%d (%s)\n", tag, dfd, rc,
	    err, err == 0 ? "none" : strerror(err));
	return rc;
}

int main(void)
{
	struct utsname u;
	char cmdline[4096];
	char fsbuf[8192];
	int err, version_ok, cmdline_ok, mounts_ok;
	long rc;

	if (getpid() != 1) {
		say("REFUSE pid=%ld is not PID 1\n", (long)getpid());
		say("FNOS_ORACLE_BOOT_FAIL\n");
		return 1;
	}

	if (uname(&u) != 0) {
		err = errno;
		say("UNAME FAIL errno=%d (%s)\n", err, strerror(err));
		say("FNOS_ORACLE_BOOT_FAIL\n");
		power_off();
	}
	say("ARCH %s MACHINE %s\n", u.sysname, u.machine);
	say("RELEASE %s\n", u.release);
	version_ok = strcmp(u.release, ORACLE_RELEASE) == 0;
	say("VERSION_IDENTITY %s expect=%s\n",
	    version_ok ? "MATCH" : "MISMATCH", ORACLE_RELEASE);

	mounts_ok = 1;
	if (mount_one("proc", "/proc") != 0)
		mounts_ok = 0;
	if (mount_one("sysfs", "/sys") != 0)
		mounts_ok = 0;
	if (mount_one("devtmpfs", "/dev") != 0)
		mounts_ok = 0;

	if (read_text("/proc/cmdline", cmdline, sizeof(cmdline)) < 0) {
		err = errno;
		say("CMDLINE_READ FAIL errno=%d (%s)\n", err, strerror(err));
		cmdline_ok = 0;
	} else {
		cmdline_ok = has_token(cmdline, ORACLE_TOKEN);
		say("CMDLINE_TOKEN %s token=%s\n",
		    cmdline_ok ? "PRESENT" : "ABSENT", ORACLE_TOKEN);
	}

	if (!mounts_ok || !version_ok || !cmdline_ok) {
		say("FNOS_ORACLE_BOOT_FAIL identity-or-mount-gate\n");
		power_off();
	}

	if (read_text("/proc/filesystems", fsbuf, sizeof(fsbuf)) < 0) {
		err = errno;
		say("FILESYSTEMS_READ FAIL errno=%d (%s)\n", err,
		    strerror(err));
	} else {
		show_fs(fsbuf);
	}

	rc = probe_faccess(AT_FDCWD, "baseline");
	(void)probe_faccess(TRIMACL_DFD, "is_trimacl");

	if (mounts_ok && version_ok && cmdline_ok && rc == 0) {
		say("FNOS_ORACLE_BOOT_OK boot-baseline-only; "
		    "NOT an ACL/trimacl acceptance\n");
	} else {
		say("FNOS_ORACLE_BOOT_FAIL mounts=%s version=%s cmdline=%s "
		    "baseline=%s\n",
		    mounts_ok ? "ok" : "fail",
		    version_ok ? "ok" : "fail",
		    cmdline_ok ? "ok" : "fail",
		    rc == 0 ? "ok" : "fail");
	}
	power_off();
}