/* SPDX-License-Identifier: GPL-2.0-only */
#define _GNU_SOURCE
#include "decode-work.h"
#include <errno.h>
#include <fcntl.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
struct cursor {const char *p;size_t n,i;};
static int literal(struct cursor *c,const char *s){size_t n=strlen(s);if(n>c->n-c->i||memcmp(c->p+c->i,s,n))return -EPROTO;c->i+=n;return 0;}
static int number(struct cursor *c,int low,int high,int *out){
 int neg=0;unsigned v=0;if(c->i<c->n&&c->p[c->i]=='-'){neg=1;c->i++;}
 size_t start=c->i;while(c->i<c->n&&c->p[c->i]>='0'&&c->p[c->i]<='9'){unsigned d=(unsigned)(c->p[c->i++]-'0');if(v>(1048575u-d)/10u)return -EPROTO;v=v*10+d;}
 if(c->i==start||(c->p[start]=='0'&&c->i!=start+1)||(neg&&!v))return -EPROTO;
 int val=neg?-(int)v:(int)v;if(val<low||val>high)return -EPROTO;*out=val;return 0;
}
int dc_ready_parse(const char *data,size_t size,uint64_t attempt,int64_t observed,struct dc_ready *out){
 if(!data||!size||size>DC_READY_MAX||!attempt||observed<=0||!out)return -EINVAL;
 struct dc_ready r={.schema=1,.attempt=attempt,.observed_ns=observed};struct cursor c={data,size,0};int major=0,minor=0;
#define L(s) do{if(literal(&c,s))return -EPROTO;}while(0)
#define F(key,lo,hi,field) do{L(key);if(number(&c,lo,hi,&r.field))return -EPROTO;}while(0)
 L("schema=1 ");F("decode_enabled=",0,1,decode_enabled);F(" retention_ready=",0,1,retention_ready);F(" prepared=",0,1,prepared);F(" post_bind=",0,1,post_bind);L("\n");
 F("run_claimed=",0,1,run_claimed);F(" run_done=",0,1,run_done);F(" run_errno=",-4095,0,run_errno);F(" stage=",0,8,stage);F(" stage_errno=",-4095,0,stage_errno);F(" error=",-4095,0,error);L("\n");
 F("ack=",0,1,ack);F(" core_ready=",0,1,core_ready);F(" admission=",0,1,admission);F(" claimed=",0,1,claimed);F(" terminal=",0,1,terminal);F(" dma_stop_known=",0,0,dma_stop_known);L("\n");
 F("decoder_published=",0,1,decoder_published);L(" dev_major=");if(number(&c,0,4095,&major))return -EPROTO;L(" dev_minor=");if(number(&c,0,1048575,&minor))return -EPROTO;
 F(" decoder_registered=",0,1,decoder_registered);F(" encoder_registered=",0,1,encoder_registered);L("\n");
#undef F
#undef L
 if(c.i!=c.n)return -EPROTO;
 r.node_major=(unsigned)major;r.node_minor=(unsigned)minor;*out=r;return 0;
}
int dc_ready_read(uint64_t attempt,struct dc_ready *out){
 static const char path[]="/sys/devices/platform/soc@0/aa00000.video-codec/decode_once_status";
 if(!out||!attempt)return -EINVAL;
 int fd=open(path,O_RDONLY|O_NONBLOCK|O_CLOEXEC|O_NOFOLLOW);if(fd<0)return -errno;
 char bytes[DC_READY_MAX+1];size_t n=0;int rc=0;
 while(n<sizeof(bytes)){ssize_t z=read(fd,bytes+n,sizeof(bytes)-n);if(z<0&&errno==EINTR)continue;if(z<0){rc=-errno;break;}if(!z)break;n+=(size_t)z;}
 close(fd);if(rc)return rc;if(n>DC_READY_MAX)return -EOVERFLOW;
 struct timespec t;if(clock_gettime(CLOCK_MONOTONIC,&t))return -errno;return dc_ready_parse(bytes,n,attempt,(int64_t)t.tv_sec*1000000000+t.tv_nsec,out);
}
