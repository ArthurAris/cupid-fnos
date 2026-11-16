/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "gadget-worker.h"
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/statfs.h>
#include <time.h>
#include <unistd.h>

#define G "/sys/kernel/config/usb_gadget/cupid_v20"
#define C G "/configs/c.1"
#define R G "/functions/rndis.usb0"
#define A G "/functions/acm.usb0"
#define UDC "a600000.usb"
struct operation { const char *kind, *path, *value; };
struct operations_io {
    int (*operation)(const char *, const char *, const char *, void *);
    int64_t (*now)(void *);
    void *context;
};
static int64_t monotonic(void *unused)
{
    (void)unused;
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC,&t) || t.tv_sec<0 || t.tv_nsec<0 ||
        t.tv_nsec>=1000000000 || t.tv_sec>(INT64_MAX-t.tv_nsec)/1000000000) return -1;
    return (int64_t)t.tv_sec*1000000000+t.tv_nsec;
}
/* All paths below are fixed. Only sysfs class links are resolved explicitly. */
static int open_fixed(const char *path,int flags)
{
    char copy[PATH_MAX];
    if (strlen(path)>=sizeof copy || path[0]!='/') {errno=EINVAL;return -1;}
    strcpy(copy,path);
    int dir=open("/",O_RDONLY|O_DIRECTORY|O_CLOEXEC);
    if (dir<0) return -1;
    char *save=NULL,*part=strtok_r(copy+1,"/",&save);
    while (part) {
        char *next=strtok_r(NULL,"/",&save);
        int fd=openat(dir,part,(next?O_RDONLY|O_DIRECTORY:flags)|O_CLOEXEC|O_NOFOLLOW|O_NONBLOCK);
        int error=errno;close(dir);
        if (fd<0) {errno=error;return -1;}
        dir=fd;part=next;
    }
    return dir;
}
static int read_equal(const char *path,const char *value,int allow_empty)
{
    char text[256];
    int fd=open_fixed(path,O_RDONLY);
    if (fd<0) return -errno;
    ssize_t n=read(fd,text,sizeof text);
    int error=errno;close(fd);
    if (n<0) return -error;
    if (allow_empty && (n==0 || (n==1&&text[0]=='\n'))) return 0;
    return n==(ssize_t)strlen(value)&&!memcmp(text,value,(size_t)n)?0:-EPROTO;
}
static int udc_path(char *target,size_t capacity)
{
    char resolved[PATH_MAX];
    if (!realpath("/sys/class/udc/" UDC,resolved)) return -errno;
    if (strncmp(resolved,"/sys/devices/",13) || strlen(resolved)+10>=capacity) return -EPERM;
    snprintf(target,capacity,"%s/function",resolved);
    struct statfs fs;
    if (statfs(resolved,&fs) || fs.f_type!=0x62656572) return -EXDEV;
    return 0;
}
static int production_operation(const char *kind,const char *path,const char *value,void *unused)
{
    (void)unused;
    if (!strcmp(kind,"unused")) {
        char actual[PATH_MAX];
        int r=udc_path(actual,sizeof actual);
        return r?r:read_equal(actual,"",1);
    }
    if (!strcmp(kind,"mount")) {
        struct stat st;
        if (lstat(path,&st) || !S_ISDIR(st.st_mode)) return -ENOTDIR;
        if (mount("configfs",path,"configfs",MS_NOSUID|MS_NODEV|MS_NOEXEC,NULL)) return -errno;
        struct statfs fs;
        return statfs(path,&fs)||fs.f_type!=0x62656570?-EXDEV:0;
    }
    if (!strcmp(kind,"mkdir")) return mkdir(path,0755)?-errno:0;
    if (!strcmp(kind,"link")) return symlink(value,path)?-errno:0;
    if (!strcmp(kind,"write")||!strcmp(kind,"bind")) {
        int fd=open_fixed(path,O_WRONLY);
        if (fd<0) return -errno;
        ssize_t n=write(fd,value,strlen(value));
        int error=errno;close(fd);
        return n==(ssize_t)strlen(value)?0:n<0?-error:-EIO;
    }
    if (!strcmp(kind,"read")) return read_equal(path,value,0);
    if (!strcmp(kind,"ready")) {
        char target[PATH_MAX];
        if (!realpath("/sys/class/tty/ttyGS0",target) || strncmp(target,"/sys/devices/",13)) return -ENODEV;
        if (!realpath("/sys/class/net/usb0",target) || strncmp(target,"/sys/devices/",13)) return -ENODEV;
        return 0;
    }
    return -EINVAL;
}
static int setup(struct gw_result *result,int64_t deadline,const struct operations_io *io)
{
    static const struct operation operations[]={
        {"unused","/sys/class/udc/" UDC,NULL},
        {"mount","/sys/kernel/config",NULL},
        {"mkdir",G,NULL},{"mkdir",G "/strings/0x409",NULL},
        {"mkdir",C,NULL},{"mkdir",C "/strings/0x409",NULL},
        {"mkdir",R,NULL},{"mkdir",A,NULL},
        {"read",A "/port_num","0\n"},
        {"write",G "/idVendor","0x1d6b\n"},{"write",G "/idProduct","0x0104\n"},
        {"write",G "/bcdDevice","0x0020\n"},{"write",G "/bcdUSB","0x0200\n"},
        {"write",G "/bDeviceClass","0xef\n"},{"write",G "/bDeviceSubClass","0x02\n"},
        {"write",G "/bDeviceProtocol","0x01\n"},
        {"write",G "/strings/0x409/serialnumber","cupid-mainline\n"},
        {"write",G "/strings/0x409/manufacturer","Linux\n"},
        {"write",G "/strings/0x409/product","Cupid RNDIS + ACM\n"},
        {"write",C "/strings/0x409/configuration","RNDIS + ACM\n"},
        {"write",C "/bmAttributes","0xc0\n"},{"write",C "/MaxPower","2\n"},
        {"write",R "/host_addr","02:00:00:00:42:01\n"},
        {"write",R "/dev_addr","02:00:00:00:42:02\n"},
        {"write",R "/ifname","usb%d\n"},{"write",R "/class","0xe0\n"},
        {"write",R "/subclass","0x01\n"},{"write",R "/protocol","0x03\n"},
        {"write",A "/protocol","0x01\n"},{"write",G "/os_desc/use","1\n"},
        {"write",G "/os_desc/b_vendor_code","0xcd\n"},
        {"write",G "/os_desc/qw_sign","MSFT100\n"},
        {"write",R "/os_desc/interface.rndis/compatible_id","RNDIS\n"},
        {"write",R "/os_desc/interface.rndis/sub_compatible_id","5162001\n"},
        {"link",C "/rndis.usb0",R},{"link",C "/acm.usb0",A},
        {"link",G "/os_desc/c.1",C},{"bind",G "/UDC",UDC},
        {"read",G "/UDC",UDC "\n"},{"ready",NULL,NULL}
    };
    if (!result) return -1;
    memset(result,0,sizeof *result);
    result->status=1;
    if (!io||!io->operation||!io->now||deadline<=0) {result->error=EINVAL;return -1;}
    int64_t previous=io->now(io->context);
    for (size_t i=0;i<sizeof operations/sizeof operations[0];i++) {
        result->operation_index=(uint32_t)i;
        int64_t now=io->now(io->context);
        if (previous<0||now<previous||now>=deadline) {result->error=now<previous?EIO:ETIMEDOUT;return -1;}
        previous=now;
        int r=io->operation(operations[i].kind,operations[i].path,operations[i].value,io->context);
        if (r) {result->error=r<0?-r:EIO;return -1;}
        if (!strcmp(operations[i].kind,"bind")) result->bound=1;
    }
    int64_t finish=io->now(io->context);
    if (finish<previous||finish>=deadline) {result->error=ETIMEDOUT;return -1;}
    result->status=0;result->local_ready=1;
    return 0;
}
int gw_setup(struct gw_result *result,int64_t deadline)
{
    const struct operations_io io={production_operation,monotonic,NULL};
    return setup(result,deadline,&io);
}
#ifdef GW_TESTING
int gw_setup_test(struct gw_result *result,int64_t deadline,const struct gw_io *test)
{
    if (!test) return -1;
    const struct operations_io io={test->operation,test->now,test->context};
    return setup(result,deadline,&io);
}
#endif
