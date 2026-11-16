/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * acl-init.c - one-shot PID1 observer for FNOS Btrfs trimacl semantics.
 *
 * This program runs only inside the isolated QEMU VM built by
 * build-acl-oracle.py. It loads the pinned virtio_blk.ko without force,
 * verifies the single serial-tagged /dev/vda, mounts it with trimacl, applies
 * the documented observations, and prints raw rc/errno evidence.
 *
 * It never writes to another device, never deletes files, never uses POSIX
 * ACLs, never assumes an allow/deny precedence, and never claims acceptance.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <linux/fs.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/reboot.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/utsname.h>
#include <sys/wait.h>
#include <sys/xattr.h>

#ifndef SYS_finit_module
#define SYS_finit_module 273
#endif
#ifndef SYS_faccessat2
#define SYS_faccessat2 439
#endif
#ifndef RB_POWER_OFF
#define RB_POWER_OFF 0x4321fedc
#endif

#define ORACLE_RELEASE "6.18.18.c1147-trim"
#define ORACLE_TOKEN "cupid.oracle=acl-v2"
#define MODULE_PATH "/modules/virtio_blk.ko"
#define VOLUME_DEVICE "/dev/vda"
#define VOLUME_ROOT "/vol1"
#define TEST_DIR VOLUME_ROOT "/acl-oracle-v1"
#define SERIAL_PATH "/sys/block/vda/serial"
#define TEST_SERIAL "FNOS_ORACLE_TEST"
#define DATA_BYTES 268435456ULL
#define TRIMACL_DFD (-21582)
#define TRIMACL_QUERY_DFD (-21576)

/* Verified from the official UAPI header: struct sizes 4 and 20 bytes. */
#define TRIM_ACL_XATTR "system.trim_acl"
#define TRIM_ACL_VERSION 0x0001
#define TRIM_ACL_ALLOW 0x0001
#define TRIM_ACL_DENY 0x0002
#define TRIM_ACL_TAG_USER 0x0004
#define TRIM_ACL_TAG_GROUP 0x0008
#define TRIM_ACL_TAG_EVERYONE 0x0010
#define TRIM_ACL_UNDEFINED_ID 0xffffffffU
#define TRIM_ACL_INHERIT_SELF 0x0001
#define TRIM_ACL_INHERIT_FILE 0x0002
#define TRIM_ACL_INHERIT_DIR 0x0004

/* Verified from the official kernel fs.h, not from POSIX ACL constants. */
#define TRIM_MAY_EXEC 0x00000001U
#define TRIM_MAY_WRITE 0x00000002U
#define TRIM_MAY_READ 0x00000004U
#define TRIM_MAY_READ_ATTR 0x00000100U

#define TEST_UID_A 42001
#define TEST_UID_B 42002
#define ACL_MAX_ENTRIES 4

static void say(const char *fmt, ...)
{
	char buf[2048];
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

static void power_off(void) __attribute__((noreturn));
static void power_off(void)
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

static void fail_exit(const char *reason) __attribute__((noreturn));
static void fail_exit(const char *reason)
{
	say("FNOS_ACL_ORACLE_FAIL reason=%s\n", reason);
	power_off();
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
		say("MOUNT %-8s %s rc=0 errno=0 (none)\n", target, type);
		return 0;
	}
	err = errno;
	if (err == EBUSY) {
		say("MOUNT %-8s %s rc=-1 errno=%d (EBUSY already-mounted)\n",
		    target, type, err);
		return 0;
	}
	say("MOUNT %-8s %s rc=-1 errno=%d (%s)\n", target, type, err,
	    strerror(err));
	return -1;
}

static void put16(unsigned char *p, uint16_t value)
{
	p[0] = (unsigned char)value;
	p[1] = (unsigned char)(value >> 8);
}

static void put32(unsigned char *p, uint32_t value)
{
	p[0] = (unsigned char)value;
	p[1] = (unsigned char)(value >> 8);
	p[2] = (unsigned char)(value >> 16);
	p[3] = (unsigned char)(value >> 24);
}

struct acl_spec {
	uint16_t tag;
	uint32_t id;
	uint32_t perm;
	uint16_t inherit;
};

/* packed UAPI header 4 bytes plus entries of exactly 20 bytes each. */
static size_t encode_acl(unsigned char *buf, const struct acl_spec *spec,
			 size_t count)
{
	size_t i;
	unsigned char *p;

	if (count > ACL_MAX_ENTRIES)
		return 0;
	put16(buf, TRIM_ACL_VERSION);
	put16(buf + 2, 0);
	p = buf + 4;
	for (i = 0; i < count; i++) {
		put16(p, spec[i].tag);
		put16(p + 2, 0);
		put32(p + 4, spec[i].perm);
		put16(p + 8, spec[i].inherit);
		put16(p + 10, 0);
		put32(p + 12, spec[i].id);
		put32(p + 16, 0);
		p += 20;
	}
	return (size_t)(p - buf);
}

static void to_hex(const unsigned char *data, size_t len, char *out,
		   size_t cap)
{
	static const char digits[] = "0123456789abcdef";
	size_t i;

	if (cap < 2) {
		if (cap)
			out[0] = '\0';
		return;
	}
	for (i = 0; i < len && 2 * i + 2 < cap; i++) {
		out[2 * i] = digits[data[i] >> 4];
		out[2 * i + 1] = digits[data[i] & 0x0f];
	}
	out[2 * i] = '\0';
}

static int set_get_acl(const char *case_name, const char *path,
		       const struct acl_spec *spec, size_t count)
{
	unsigned char in[4 + 20 * ACL_MAX_ENTRIES];
	unsigned char out[256];
	char in_hex[2 * sizeof(in) + 1];
	char out_hex[2 * sizeof(out) + 1];
	size_t len = encode_acl(in, spec, count);
	ssize_t rc, got;
	int err, equal;

	if (len == 0) {
		say("CASE %s XATTR_ENCODE FAIL count=%zu\n", case_name, count);
		return -1;
	}
	to_hex(in, len, in_hex, sizeof(in_hex));
	errno = 0;
	rc = setxattr(path, TRIM_ACL_XATTR, in, len, 0);
	err = errno;
	say("CASE %s XATTR_SET path=%s len=%zu rc=%ld errno=%d (%s) hex=%s\n",
	    case_name, path, len, (long)rc, err,
	    err == 0 ? "none" : strerror(err), in_hex);
	if (rc != 0)
		return -1;
	errno = 0;
	got = getxattr(path, TRIM_ACL_XATTR, out, sizeof(out));
	err = errno;
	if (got > 0)
		to_hex(out, (size_t)got, out_hex, sizeof(out_hex));
	else
		out_hex[0] = '\0';
	say("CASE %s XATTR_GET path=%s rc=%ld errno=%d (%s) hex=%s\n",
	    case_name, path, (long)got, err,
	    err == 0 ? "none" : strerror(err), out_hex);
	if (got < 0)
		return -1;
	equal = got == (ssize_t)len && memcmp(out, in, len) == 0;
	if (!equal)
		say("CASE %s XATTR_READBACK_DIFF path=%s got=%ld set_len=%zu "
		    "equal=no note=normalization-observed-not-failure\n",
		    case_name, path, (long)got, len);
	return 0;
}

/* 32-byte query struct copied by the kernel from syscall arg2 (x1);
 * layout per H/include/linux/trimvfs.h:52-60 (docs/fnos-permission-abi.md):
 * filename ptr, buffer ptr, buffer_size, fd, flags; flags bit30 selects the
 * fd branch, otherwise the filename path is used. */
struct trimacl_query {
	uint64_t filename;
	uint64_t buffer;
	uint64_t buffer_size;
	int32_t fd;
	uint32_t flags;
};

/* Read-only getxattr of the raw user-space xattr. ENODATA/EACCES are
 * observations; any other errno is an infrastructure failure. */
static int read_raw_acl(const char *case_name, const char *path)
{
	unsigned char out[512];
	char out_hex[2 * sizeof(out) + 1];
	ssize_t got;
	int err;

	errno = 0;
	got = getxattr(path, TRIM_ACL_XATTR, out, sizeof(out));
	err = errno;
	if (got > 0)
		to_hex(out, (size_t)got, out_hex, sizeof(out_hex));
	else
		out_hex[0] = '\0';
	say("CASE %s XATTR_GET_RAW path=%s rc=%ld errno=%d (%s) hex=%s\n",
	    case_name, path, (long)got, err,
	    err == 0 ? "none" : strerror(err), out_hex);
	if (err != 0 && err != ENODATA && err != EACCES) {
		say("CASE %s XATTR_GET_RAW FAIL note=infrastructure-error\n",
		    case_name);
		return -1;
	}
	return 0;
}

/* dfd=-21576 expanded-ACL query with the 32-byte struct as syscall arg2;
 * distinct from the plain-path dfd=-21582 is_trimacl query. Any rc/errno is
 * recorded; semantics are not assumed and never gate success. */
static void expand_query(const char *case_name, const char *path)
{
	unsigned char out[512];
	char out_hex[2 * sizeof(out) + 1];
	struct trimacl_query q;
	long rc;
	int err;

	memset(&q, 0, sizeof(q));
	memset(out, 0, sizeof(out));
	q.filename = (uint64_t)(uintptr_t)path;
	q.buffer = (uint64_t)(uintptr_t)out;
	q.buffer_size = sizeof(out);
	q.fd = -1;
	q.flags = 0;
	errno = 0;
	rc = syscall(SYS_faccessat2, (long)TRIMACL_QUERY_DFD, (long)&q, 0L, 0L);
	err = errno;
	if (err == 0 && rc > 0 && rc <= (long)sizeof(out))
		to_hex(out, (size_t)rc, out_hex, sizeof(out_hex));
	else
		out_hex[0] = '\0';
	say("CASE %s EXPAND_QUERY dfd=%d rc=%ld errno=%d (%s) hex=%s "
	    "note=unknown-semantics-observed-only\n", case_name,
	    TRIMACL_QUERY_DFD, rc, err, err == 0 ? "none" : strerror(err),
	    out_hex);
}

static int wait_for_block(void)
{
	struct timespec start, now;
	long elapsed_ms;

	clock_gettime(CLOCK_MONOTONIC, &start);
	for (;;) {
		struct stat st;

		errno = 0;
		if (stat(VOLUME_DEVICE, &st) == 0) {
			if (!S_ISBLK(st.st_mode)) {
				say("DEVICE_STAT %s rc=0 NOT_BLOCK mode=%o\n",
				    VOLUME_DEVICE, (unsigned)st.st_mode);
				return -1;
			}
			say("DEVICE_STAT %s rc=0 S_ISBLK yes\n", VOLUME_DEVICE);
			return 0;
		}
		if (errno != ENOENT) {
			say("DEVICE_STAT %s FAIL errno=%d (%s)\n",
			    VOLUME_DEVICE, errno, strerror(errno));
			return -1;
		}
		clock_gettime(CLOCK_MONOTONIC, &now);
		elapsed_ms = (now.tv_sec - start.tv_sec) * 1000L +
			     (now.tv_nsec - start.tv_nsec) / 1000000L;
		if (elapsed_ms >= 5000) {
			say("DEVICE_WAIT %s TIMEOUT after %ld ms\n",
			    VOLUME_DEVICE, elapsed_ms);
			return -1;
		}
		usleep(100000);
	}
}

static int check_device(void)
{
	struct stat st;
	unsigned long long bytes = 0;
	char serial[256];
	ssize_t n;
	int fd, rc, err;

	errno = 0;
	if (stat(VOLUME_DEVICE, &st) != 0 || !S_ISBLK(st.st_mode)) {
		err = errno;
		say("DEVICE_CHECK %s not a block device errno=%d (%s)\n",
		    VOLUME_DEVICE, err, err == 0 ? "none" : strerror(err));
		return -1;
	}
	fd = open(VOLUME_DEVICE, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		err = errno;
		say("DEVICE_OPEN %s FAIL errno=%d (%s)\n", VOLUME_DEVICE, err,
		    strerror(err));
		return -1;
	}
	errno = 0;
	rc = ioctl(fd, BLKGETSIZE64, &bytes);
	err = errno;
	close(fd);
	say("DEVICE_SIZE %s rc=%d errno=%d (%s) bytes=%llu expect=%llu\n",
	    VOLUME_DEVICE, rc, err, err == 0 ? "none" : strerror(err),
	    bytes, DATA_BYTES);
	if (rc != 0 || bytes != DATA_BYTES)
		return -1;

	errno = 0;
	n = read_text(SERIAL_PATH, serial, sizeof(serial));
	if (n < 0) {
		err = errno;
		say("DEVICE_SERIAL %s READ FAIL errno=%d (%s)\n", SERIAL_PATH,
		    err, strerror(err));
		return -1;
	}
	if (n > 0 && serial[n - 1] == '\n') {
		serial[--n] = '\0';
	}
	say("DEVICE_SERIAL %s bytes=%ld value=\"%s\" expect=\"%s\" match=%s\n",
	    SERIAL_PATH, (long)n, serial, TEST_SERIAL,
	    (n == (ssize_t)strlen(TEST_SERIAL) &&
	     memcmp(serial, TEST_SERIAL, (size_t)n) == 0) ? "yes" : "no");
	if (n != (ssize_t)strlen(TEST_SERIAL) ||
	    memcmp(serial, TEST_SERIAL, (size_t)n) != 0)
		return -1;
	return 0;
}

static int create_test_file(const char *case_name, const char *path)
{
	struct stat st;
	int fd, err, rc;

	errno = 0;
	fd = open(path, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC, 0600);
	err = errno;
	if (fd < 0) {
		say("CASE %s FILE_CREATE path=%s rc=%d errno=%d (%s)\n",
		    case_name, path, fd, err, strerror(err));
		return -1;
	}
	errno = 0;
	rc = fchmod(fd, 0600);
	if (rc != 0) {
		err = errno;
		close(fd);
		say("CASE %s FILE_CHMOD path=%s rc=%d errno=%d (%s)\n",
		    case_name, path, rc, err, strerror(err));
		return -1;
	}
	errno = 0;
	if (fstat(fd, &st) != 0) {
		err = errno;
		close(fd);
		say("CASE %s FILE_STAT path=%s FAIL errno=%d (%s)\n",
		    case_name, path, err, strerror(err));
		return -1;
	}
	close(fd);
	say("CASE %s FILE_CREATE path=%s rc=0 errno=0 (none) uid=%u mode=%04o\n",
	    case_name, path, (unsigned)st.st_uid,
	    (unsigned)(st.st_mode & 07777));
	if (!S_ISREG(st.st_mode) || st.st_uid != 0 ||
	    (st.st_mode & 07777) != 0600)
		return -1;
	return 0;
}

struct child_result {
	long rc;
	int err;
	int stage;
	int stage_err;
};

static struct child_result *shared;

static int run_child_probe(const char *case_name, const char *path, uid_t uid,
			   int flags, int use_faccess, const gid_t *supp,
			   size_t supp_count, struct child_result *out)
{
	pid_t pid, waited;
	int status = 0, wait_err = 0;

	memset(out, 0, sizeof(*out));
	errno = 0;
	pid = fork();
	if (pid < 0) {
		say("CASE %s FORK FAIL errno=%d (%s)\n", case_name, errno,
		    strerror(errno));
		return -1;
	}
	if (pid == 0) {
		if (setgroups(supp_count, supp) != 0) {
			out->stage = 1;
			out->stage_err = errno;
			_exit(0);
		}
		if (setgid((gid_t)uid) != 0) {
			out->stage = 2;
			out->stage_err = errno;
			_exit(0);
		}
		if (setuid(uid) != 0) {
			out->stage = 3;
			out->stage_err = errno;
			_exit(0);
		}
		errno = 0;
		if (use_faccess)
			out->rc = syscall(SYS_faccessat2, (long)AT_FDCWD, path,
					  (long)X_OK, 0L);
		else
			out->rc = open(path, flags);
		out->err = errno;
		if (!use_faccess && out->rc >= 0)
			close((int)out->rc);
		_exit(0);
	}
	errno = 0;
	waited = waitpid(pid, &status, 0);
	wait_err = errno;
	if (waited != pid) {
		say("CASE %s WAIT pid=%ld rc=%ld errno=%d (%s)\n", case_name,
		    (long)pid, (long)waited, wait_err, strerror(wait_err));
		return -1;
	}
	if (out->stage != 0) {
		say("CASE %s CHILD_SETUP user=%u stage=%d rc=-1 errno=%d (%s)\n",
		    case_name, (unsigned)uid, out->stage, out->stage_err,
		    strerror(out->stage_err));
		return -1;
	}
	if (!WIFEXITED(status) || WEXITSTATUS(status) != 0) {
		say("CASE %s CHILD_EXIT user=%u status=0x%x\n", case_name,
		    (unsigned)uid, (unsigned)status);
		return -1;
	}
	return 0;
}

static int observe_open(const char *case_name, const char *path, uid_t uid,
			int flags, const char *flag_name)
{
	if (run_child_probe(case_name, path, uid, flags, 0, NULL, 0, shared)
	    != 0)
		return -1;
	say("CASE %s OPEN user=%u flags=%s rc=%ld errno=%d (%s)\n",
	    case_name, (unsigned)uid, flag_name, shared->rc, shared->err,
	    shared->err == 0 ? "none" : strerror(shared->err));
	return 0;
}

static int observe_open_supp(const char *case_name, const char *path,
			     uid_t uid, const gid_t *supp, size_t supp_count,
			     int flags, const char *flag_name)
{
	if (run_child_probe(case_name, path, uid, flags, 0, supp, supp_count,
			    shared) != 0)
		return -1;
	say("CASE %s OPEN_SUPP user=%u supp_count=%zu flags=%s rc=%ld "
	    "errno=%d (%s)\n", case_name, (unsigned)uid, supp_count,
	    flag_name, shared->rc, shared->err,
	    shared->err == 0 ? "none" : strerror(shared->err));
	return 0;
}

static int baseline_observe(const char *case_name, const char *path, uid_t uid)
{
	int denied;

	if (run_child_probe(case_name, path, uid, O_RDONLY, 0, NULL, 0,
	    shared) != 0)
		return -1;
	denied = shared->rc == -1 && shared->err == EACCES;
	say("CASE %s BASELINE user=%u flags=O_RDONLY rc=%ld errno=%d (%s) "
	    "expect=EACCES match=%s note=mode-baseline-observed-not-verdict\n",
	    case_name, (unsigned)uid, shared->rc, shared->err,
	    shared->err == 0 ? "none" : strerror(shared->err),
	    denied ? "yes" : "no");
	return 0;
}

static int observe_dir(const char *case_name, const char *path, uid_t uid)
{
	if (run_child_probe(case_name, path, uid, 0, 1, NULL, 0, shared)
	    != 0)
		return -1;
	say("CASE %s DIR_ACCESS user=%u op=faccessat2(X_OK) rc=%ld errno=%d "
	    "(%s)\n", case_name, (unsigned)uid, shared->rc, shared->err,
	    shared->err == 0 ? "none" : strerror(shared->err));
	return 0;
}

static int observe_pair(const char *case_name, const char *path)
{
	if (observe_open(case_name, path, TEST_UID_A, O_RDONLY, "O_RDONLY") != 0)
		return -1;
	if (observe_open(case_name, path, TEST_UID_A, O_WRONLY, "O_WRONLY") != 0)
		return -1;
	if (observe_open(case_name, path, TEST_UID_B, O_RDONLY, "O_RDONLY") != 0)
		return -1;
	if (observe_open(case_name, path, TEST_UID_B, O_WRONLY, "O_WRONLY") != 0)
		return -1;
	return 0;
}

static int baseline_pair(const char *case_name, const char *path)
{
	if (baseline_observe(case_name, path, TEST_UID_A) != 0)
		return -1;
	if (baseline_observe(case_name, path, TEST_UID_B) != 0)
		return -1;
	return 0;
}

static int observe_dir_pair(const char *case_name, const char *path)
{
	if (observe_dir(case_name, path, TEST_UID_A) != 0)
		return -1;
	if (observe_dir(case_name, path, TEST_UID_B) != 0)
		return -1;
	return 0;
}

static int extension_stopped;
static int inh_full_ready;

/* Same traversal rule the v1 root/test-dir prep uses. */
static const struct acl_spec root_spec[] = {
	{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_EVERYONE, TRIM_ACL_UNDEFINED_ID,
	  TRIM_MAY_EXEC | TRIM_MAY_READ_ATTR, TRIM_ACL_INHERIT_SELF },
};

struct inherit_paths {
	const char *subdir;
	const char *parent;
	const char *child_file;
	const char *child_dir;
	const char *grandchild_file;
};

#define INH_CASE(n) \
	{ TEST_DIR "/" n, TEST_DIR "/" n "/parent", \
	  TEST_DIR "/" n "/parent/child-file", \
	  TEST_DIR "/" n "/parent/child-dir", \
	  TEST_DIR "/" n "/parent/child-dir/grandchild-file" }

static const struct inherit_paths inherit_case_full =
	INH_CASE("inherit-self-file-dir");
static const struct inherit_paths inherit_case_file =
	INH_CASE("inherit-self-file");
static const struct inherit_paths inherit_case_dir =
	INH_CASE("inherit-self-dir");

/* Root creates one child node; no chmod and no setxattr afterwards. */
static int create_child_node(const char *case_name, const char *path,
			     int is_dir)
{
	struct stat st;
	int fd = -1, rc, err;

	errno = 0;
	if (is_dir)
		rc = mkdir(path, 0700);
	else {
		fd = open(path, O_CREAT | O_EXCL | O_WRONLY | O_CLOEXEC,
			  0600);
		rc = fd;
	}
	err = errno;
	if (rc < 0) {
		say("CASE %s CREATE path=%s is_dir=%d rc=%d errno=%d (%s)\n",
		    case_name, path, is_dir, rc, err,
		    err == 0 ? "none" : strerror(err));
		return -1;
	}
	if (!is_dir)
		close(fd);
	errno = 0;
	if (stat(path, &st) != 0) {
		err = errno;
		say("CASE %s STAT path=%s FAIL errno=%d (%s)\n", case_name,
		    path, err, strerror(err));
		return -1;
	}
	say("CASE %s CREATE path=%s is_dir=%d rc=0 errno=0 (none) uid=%u "
	    "mode=%04o note=no-chmod-no-acl\n", case_name, path, is_dir,
	    (unsigned)st.st_uid, (unsigned)(st.st_mode & 07777));
	return 0;
}

static int run_inheritance_case(const char *case_name,
				const struct inherit_paths *p,
				uint16_t inherit_flags)
{
	const struct acl_spec parent_spec[] = {
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_EVERYONE,
		  TRIM_ACL_UNDEFINED_ID,
		  TRIM_MAY_EXEC | TRIM_MAY_READ_ATTR,
		  TRIM_ACL_INHERIT_SELF },
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_USER, TEST_UID_A,
		  TRIM_MAY_READ | TRIM_MAY_EXEC | TRIM_MAY_READ_ATTR,
		  inherit_flags },
	};
	const char *const nodes[4] = { p->parent, p->child_file, p->child_dir,
				       p->grandchild_file };
	const char *const dirs2[2] = { p->parent, p->child_dir };
	const char *const files2[2] = { p->child_file, p->grandchild_file };
	size_t i;

	if (extension_stopped) {
		say("CASE %s SKIPPED note=extension-stopped-earlier\n",
		    case_name);
		return 0;
	}
	if (create_child_node(case_name, p->subdir, 1) != 0)
		goto case_fail;
	if (set_get_acl(case_name, p->subdir, root_spec, 1) != 0)
		return -1;
	if (create_child_node(case_name, p->parent, 1) != 0)
		goto case_fail;
	if (set_get_acl(case_name, p->parent, parent_spec, 2) != 0)
		return -1;
	if (create_child_node(case_name, p->child_file, 0) != 0 ||
	    create_child_node(case_name, p->child_dir, 1) != 0 ||
	    create_child_node(case_name, p->grandchild_file, 0) != 0)
		goto case_fail;
	for (i = 0; i < 4; i++) {
		if (read_raw_acl(case_name, nodes[i]) != 0)
			return -1;
		expand_query(case_name, nodes[i]);
	}
	for (i = 0; i < 2; i++) {
		if (observe_dir(case_name, dirs2[i], TEST_UID_A) != 0 ||
		    observe_dir(case_name, dirs2[i], TEST_UID_B) != 0 ||
		    observe_open(case_name, files2[i], TEST_UID_A, O_RDONLY,
				 "O_RDONLY") != 0 ||
		    observe_open(case_name, files2[i], TEST_UID_B, O_RDONLY,
				 "O_RDONLY") != 0)
			return -1;
	}
	if (inherit_flags == (TRIM_ACL_INHERIT_SELF | TRIM_ACL_INHERIT_FILE |
			      TRIM_ACL_INHERIT_DIR))
		inh_full_ready = 1;
	return 0;
case_fail:
	say("CASE %s SETUP_FAIL note=unknown-permission-observed-"
	    "extension-stopped\n", case_name);
	extension_stopped = 1;
	return 0;
}

static int run_group_case(const char *case_name, const char *baseline_name,
			  const char *path)
{
	static const gid_t supp_43001[] = { 43001 };
	static const int modes[2] = { O_RDONLY, O_WRONLY };
	static const char *const mode_names[2] = { "O_RDONLY", "O_WRONLY" };
	const struct acl_spec group_spec[] = {
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_GROUP, 43001,
		  TRIM_MAY_READ, TRIM_ACL_INHERIT_SELF },
		{ TRIM_ACL_DENY | TRIM_ACL_TAG_USER, TEST_UID_A,
		  TRIM_MAY_READ, TRIM_ACL_INHERIT_SELF },
	};
	size_t i;

	if (extension_stopped) {
		say("CASE %s SKIPPED note=extension-stopped-earlier\n",
		    case_name);
		return 0;
	}
	if (create_test_file(case_name, path) != 0)
		goto case_fail;
	if (baseline_pair(baseline_name, path) != 0)
		return -1;
	if (set_get_acl(case_name, path, group_spec, 2) != 0)
		return -1;
	for (i = 0; i < 2; i++) {
		if (observe_open_supp(case_name, path, TEST_UID_A, supp_43001,
				      1, modes[i], mode_names[i]) != 0 ||
		    observe_open_supp(case_name, path, TEST_UID_B, supp_43001,
				      1, modes[i], mode_names[i]) != 0 ||
		    observe_open_supp(case_name, path, TEST_UID_B, NULL, 0,
				      modes[i], mode_names[i]) != 0)
			return -1;
	}
	return 0;
case_fail:
	say("CASE %s SETUP_FAIL note=unknown-permission-observed-"
	    "extension-stopped\n", case_name);
	extension_stopped = 1;
	return 0;
}

int main(void)
{
	struct utsname u;
	char cmdline[4096];
	int err, rc, fd, version_ok, machine_ok, token_ok, mounts_ok;
	const struct acl_spec allow_a[] = {
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_USER, TEST_UID_A,
		  TRIM_MAY_READ, TRIM_ACL_INHERIT_SELF },
	};
	const struct acl_spec everyone_then_deny[] = {
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_EVERYONE,
		  TRIM_ACL_UNDEFINED_ID, TRIM_MAY_READ,
		  TRIM_ACL_INHERIT_SELF },
		{ TRIM_ACL_DENY | TRIM_ACL_TAG_USER, TEST_UID_A,
		  TRIM_MAY_READ, TRIM_ACL_INHERIT_SELF },
	};
	const struct acl_spec deny_then_everyone[] = {
		{ TRIM_ACL_DENY | TRIM_ACL_TAG_USER, TEST_UID_A,
		  TRIM_MAY_READ, TRIM_ACL_INHERIT_SELF },
		{ TRIM_ACL_ALLOW | TRIM_ACL_TAG_EVERYONE,
		  TRIM_ACL_UNDEFINED_ID, TRIM_MAY_READ,
		  TRIM_ACL_INHERIT_SELF },
	};
	const char *case1 = TEST_DIR "/case1-allow-read-42001";
	const char *case2a = TEST_DIR "/case2-order-everyone-allow-then-42001-deny";
	const char *case2b = TEST_DIR "/case2-order-42001-deny-then-everyone-allow";
	const char *case3 = TEST_DIR "/case3-group-43001-allow-42001-deny";

	if (getpid() != 1) {
		say("REFUSE pid=%ld is not PID 1\n", (long)getpid());
		return 1;
	}
	say("FNOS_ACL_ORACLE_START case-set=acl-v2\n");

	mounts_ok = 1;
	if (mount_one("proc", "/proc") != 0)
		mounts_ok = 0;
	if (mount_one("sysfs", "/sys") != 0)
		mounts_ok = 0;
	if (mount_one("devtmpfs", "/dev") != 0)
		mounts_ok = 0;
	if (!mounts_ok)
		fail_exit("mounts");

	if (uname(&u) != 0) {
		err = errno;
		say("UNAME FAIL errno=%d (%s)\n", err, strerror(err));
		fail_exit("uname");
	}
	version_ok = strcmp(u.release, ORACLE_RELEASE) == 0;
	machine_ok = strcmp(u.machine, "aarch64") == 0;
	say("ARCH machine=%s expect=aarch64 match=%s\n", u.machine,
	    machine_ok ? "yes" : "no");
	say("RELEASE %s expect=%s match=%s\n", u.release, ORACLE_RELEASE,
	    version_ok ? "yes" : "no");

	errno = 0;
	if (read_text("/proc/cmdline", cmdline, sizeof(cmdline)) < 0) {
		err = errno;
		say("CMDLINE_READ FAIL errno=%d (%s)\n", err, strerror(err));
		token_ok = 0;
	} else {
		token_ok = has_token(cmdline, ORACLE_TOKEN);
		say("CMDLINE_TOKEN %s token=\"%s\"\n",
		    token_ok ? "PRESENT" : "ABSENT", ORACLE_TOKEN);
	}
	if (!version_ok || !machine_ok || !token_ok)
		fail_exit("identity");

	errno = 0;
	fd = open(MODULE_PATH, O_RDONLY | O_CLOEXEC);
	if (fd < 0) {
		err = errno;
		say("MODULE_OPEN %s FAIL errno=%d (%s)\n", MODULE_PATH, err,
		    strerror(err));
		fail_exit("module-open");
	}
	errno = 0;
	rc = (int)syscall(SYS_finit_module, fd, "", 0);
	err = errno;
	close(fd);
	say("MODULE_LOAD %s flags=0 force=no rc=%d errno=%d (%s)\n",
	    MODULE_PATH, rc, err, err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("module-load");

	if (wait_for_block() != 0)
		fail_exit("device-wait");
	if (check_device() != 0)
		fail_exit("device-identity");

	errno = 0;
	rc = mount(VOLUME_DEVICE, VOLUME_ROOT, "btrfs", 0, "trimacl");
	err = errno;
	say("MOUNT %s source=%s type=btrfs opt=trimacl rc=%d errno=%d (%s)\n",
	    VOLUME_ROOT, VOLUME_DEVICE, rc, err,
	    err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("btrfs-mount");

	errno = 0;
	rc = (int)syscall(SYS_faccessat2, (long)TRIMACL_DFD, VOLUME_ROOT,
			  (long)F_OK, 0L);
	err = errno;
	say("IS_TRIMACL path=%s dfd=%d argv2=plain-path-string rc=%d errno=%d "
	    "(%s) note=raw-result-no-semantics-assumed\n", VOLUME_ROOT,
	    TRIMACL_DFD, rc, err, err == 0 ? "none" : strerror(err));

	errno = 0;
	rc = mkdir(TEST_DIR, 0777);
	err = errno;
	if (rc == 0) {
		errno = 0;
		rc = chmod(TEST_DIR, 0777);
		err = errno;
	}
	say("CASE setup MKDIR path=%s rc=%d errno=%d (%s)\n", TEST_DIR, rc,
	    err, err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("test-dir");

	shared = mmap(NULL, sizeof(*shared), PROT_READ | PROT_WRITE,
		      MAP_SHARED | MAP_ANONYMOUS, -1, 0);
	if (shared == MAP_FAILED)
		fail_exit("child-result-mmap");

	if (set_get_acl("root-prep", VOLUME_ROOT, root_spec, 1) != 0)
		fail_exit("root-prep-xattr");
	if (set_get_acl("test-dir-prep", TEST_DIR, root_spec, 1) != 0)
		fail_exit("test-dir-xattr");
	if (observe_dir_pair("root-traverse", VOLUME_ROOT) != 0)
		fail_exit("root-traverse-child");
	if (observe_dir_pair("test-dir-traverse", TEST_DIR) != 0)
		fail_exit("test-dir-traverse-child");

	if (create_test_file("case1", case1) != 0)
		fail_exit("case1-create");
	if (baseline_pair("case1-baseline", case1) != 0)
		fail_exit("case1-baseline-child");
	if (set_get_acl("case1", case1, allow_a, 1) != 0)
		fail_exit("case1-xattr");
	if (observe_pair("case1", case1) != 0)
		fail_exit("case1-observe");

	if (create_test_file("case2-order-ab", case2a) != 0)
		fail_exit("case2a-create");
	if (baseline_pair("case2-order-ab-baseline", case2a) != 0)
		fail_exit("case2a-baseline-child");
	if (set_get_acl("case2-order-ab", case2a, everyone_then_deny, 2) != 0)
		fail_exit("case2a-xattr");
	if (observe_pair("case2-order-ab", case2a) != 0)
		fail_exit("case2a-observe");

	if (create_test_file("case2-order-ba", case2b) != 0)
		fail_exit("case2b-create");
	if (baseline_pair("case2-order-ba-baseline", case2b) != 0)
		fail_exit("case2b-baseline-child");
	if (set_get_acl("case2-order-ba", case2b, deny_then_everyone, 2) != 0)
		fail_exit("case2b-xattr");
	if (observe_pair("case2-order-ba", case2b) != 0)
		fail_exit("case2b-observe");
	if (run_inheritance_case("inherit-self-file-dir", &inherit_case_full,
				 TRIM_ACL_INHERIT_SELF | TRIM_ACL_INHERIT_FILE |
				 TRIM_ACL_INHERIT_DIR) < 0)
		fail_exit("inherit-full");
	if (run_inheritance_case("inherit-self-file", &inherit_case_file,
				 TRIM_ACL_INHERIT_SELF |
				 TRIM_ACL_INHERIT_FILE) < 0)
		fail_exit("inherit-file");
	if (run_inheritance_case("inherit-self-dir", &inherit_case_dir,
				 TRIM_ACL_INHERIT_SELF |
				 TRIM_ACL_INHERIT_DIR) < 0)
		fail_exit("inherit-dir");
	if (run_group_case("case3-group", "case3-group-baseline", case3) < 0)
		fail_exit("case3-group");

	sync();
	errno = 0;
	rc = umount2(VOLUME_ROOT, 0);
	err = errno;
	say("UMOUNT %s rc=%d errno=%d (%s)\n", VOLUME_ROOT, rc, err,
	    err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("umount");
	errno = 0;
	rc = mount(VOLUME_DEVICE, VOLUME_ROOT, "btrfs", 0, "trimacl");
	err = errno;
	say("REMOUNT %s opt=trimacl rc=%d errno=%d (%s) "
	    "note=same-verified-vda-no-new-setxattr\n", VOLUME_ROOT, rc, err,
	    err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("btrfs-remount");
	{
		const char *const rnames[3] = { "remount-case1",
			"remount-case2ab", "remount-case2ba" };
		const char *const rpaths[3] = { case1, case2a, case2b };
		size_t ri;

		for (ri = 0; ri < 3; ri++) {
			if (observe_open(rnames[ri], rpaths[ri], TEST_UID_A,
					 O_RDONLY, "O_RDONLY") != 0 ||
			    observe_open(rnames[ri], rpaths[ri], TEST_UID_B,
					 O_RDONLY, "O_RDONLY") != 0)
				fail_exit("remount-observe");
		}
	}
	if (inh_full_ready &&
	    (observe_open("remount-inh-full-child",
			  inherit_case_full.child_file, TEST_UID_A, O_RDONLY,
			  "O_RDONLY") != 0 ||
	     observe_open("remount-inh-full-child",
			  inherit_case_full.child_file, TEST_UID_B, O_RDONLY,
			  "O_RDONLY") != 0))
		fail_exit("remount-inh-observe");
	sync();
	errno = 0;
	rc = umount2(VOLUME_ROOT, 0);
	err = errno;
	say("UMOUNT %s pass=final rc=%d errno=%d (%s)\n", VOLUME_ROOT, rc, err,
	    err == 0 ? "none" : strerror(err));
	if (rc != 0)
		fail_exit("umount-final");

	say("FNOS_ACL_ORACLE_OBSERVATIONS_COMPLETE "
	    "NOT_COMPATIBILITY_ACCEPTANCE\n");
	power_off();
}