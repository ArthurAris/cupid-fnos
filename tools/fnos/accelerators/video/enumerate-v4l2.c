/* SPDX-License-Identifier: GPL-2.0-only */
/* Capability queries only: never changes formats, buffers, controls or STREAMON. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/videodev2.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>

static int query(int fd, unsigned long request, void *arg)
{
	int result;
	do { result=ioctl(fd,request,arg); } while (result<0 && errno==EINTR);
	return result;
}
static void formats(int fd, enum v4l2_buf_type type)
{
	for (unsigned int i=0;i<64;i++) {
		struct v4l2_fmtdesc desc={.index=i,.type=type};
		if (query(fd,VIDIOC_ENUM_FMT,&desc)<0) {
			if (errno!=EINVAL) fprintf(stderr,"ENUM_FMT_FAIL type=%u errno=%d\n",type,errno);
			return;
		}
		printf("FORMAT queue=%u index=%u fourcc=%c%c%c%c compressed=%u description=%.*s\n",type,i,
			desc.pixelformat&255,(desc.pixelformat>>8)&255,(desc.pixelformat>>16)&255,(desc.pixelformat>>24)&255,
			!!(desc.flags&V4L2_FMT_FLAG_COMPRESSED),(int)sizeof(desc.description),desc.description);
	}
	fprintf(stderr,"ENUM_FMT_CAP_REACHED type=%u\n",type);
}
int main(int argc,char **argv)
{
	if (argc!=2 || strncmp(argv[1],"/dev/video",10)) {fprintf(stderr,"Usage: enumerate-v4l2 /dev/videoN\n");return 2;}
	for (const char *p=argv[1]+10;*p;p++) if (*p<'0'||*p>'9') return 2;
	if (!argv[1][10]) return 2;
	alarm(10);
	int fd=open(argv[1],O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);
	if (fd<0) {fprintf(stderr,"OPEN_FAIL errno=%d\n",errno);return 1;}
	struct stat info;
	if (fstat(fd,&info)<0 || !S_ISCHR(info.st_mode)) {close(fd);return 1;}
	struct v4l2_capability caps={0};
	if (query(fd,VIDIOC_QUERYCAP,&caps)<0) {fprintf(stderr,"QUERYCAP_FAIL errno=%d\n",errno);close(fd);return 1;}
	unsigned int effective=caps.capabilities & V4L2_CAP_DEVICE_CAPS ? caps.device_caps : caps.capabilities;
	printf("V4L2_CAPABILITY driver=%.*s card=%.*s caps=%#x m2m=%u streaming=%u\n",(int)sizeof(caps.driver),caps.driver,(int)sizeof(caps.card),caps.card,effective,
		!!(effective&(V4L2_CAP_VIDEO_M2M|V4L2_CAP_VIDEO_M2M_MPLANE)),!!(effective&V4L2_CAP_STREAMING));
	formats(fd,V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);formats(fd,V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
	close(fd);printf("V4L2_ENUMERATION_ONLY NO_CODEC_TASK_NO_STREAMON\n");return 0;
}
