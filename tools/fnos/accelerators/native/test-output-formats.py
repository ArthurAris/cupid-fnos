#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Exercise real decoder enum/TRY/S_FMT, layout, stride and DPB mapping offline.

Framework queue/control calls are stubbed. These cases do not establish DRC
firmware acceptance, ten-bit negotiation or DMA correctness.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location('extract', HERE / 'test-iris-behavior.py')
EXTRACT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXTRACT)
WORK = None

PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32; typedef uint64_t u64;
#define BIT(n) (1u<<(n))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#define ALIGN(x,n) (((x)+(n)-1)&~((n)-1))
#define DIV_ROUND_UP(x,n) (((x)+(n)-1)/(n))
#define DECODER 1
#define ENCODER 2
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define V4L2_FIELD_ANY 0
#define V4L2_FIELD_NONE 1
#define V4L2_FMT_FLAG_COMPRESSED 1
#define V4L2_FMT_FLAG_DYN_RESOLUTION 2
#define V4L2_PIX_FMT_NV12 0x3231564e
#define V4L2_PIX_FMT_NV21 0x3132564e
#define V4L2_PIX_FMT_P010 0x30313050
#define V4L2_PIX_FMT_QC08C 0x43383051
#define V4L2_PIX_FMT_QC10C 0x43303151
#define V4L2_PIX_FMT_H264 0x34363248
#define V4L2_PIX_FMT_HEVC 0x43564548
#define V4L2_PIX_FMT_VP9 0x30395056
#define HFI_HOST_FLAGS_NONE 0
#define HFI_PAYLOAD_64_PACKED 13
struct plane { u32 bytesperline,sizeimage; };
struct v4l2_pix_format_mplane { u32 width,height,pixelformat,field,num_planes;
    struct plane plane_fmt[1]; u32 colorspace,xfer_func,ycbcr_enc,quantization; u32 reserved[8]; };
struct v4l2_format { u32 type; struct {struct v4l2_pix_format_mplane pix_mp;} fmt; };
struct v4l2_fmtdesc { u32 index,type,pixelformat,flags; };
struct vb2_queue { bool busy,streaming; };
struct v4l2_m2m_ctx { struct vb2_queue src,dst; };
struct iris_platform_data { bool prototype_restricted,no_opb; };
struct iris_core { struct iris_platform_data *iris_platform_data; };
struct iris_buffers { u32 min_count,size; };
struct crop { u32 top,left,width,height; };
struct iris_inst { struct iris_core *core; u32 domain,codec; struct v4l2_format *fmt_src,*fmt_dst;
    struct iris_buffers buffers[16]; struct crop crop; struct v4l2_m2m_ctx *m2m_ctx; };
static struct vb2_queue *v4l2_m2m_get_src_vq(struct v4l2_m2m_ctx *c) {return &c->src;}
static struct vb2_queue *v4l2_m2m_get_vq(struct v4l2_m2m_ctx *c,u32 type) {
 return type==9?&c->src:type==10?&c->dst:NULL; }
static bool vb2_is_streaming(struct vb2_queue *q) {return q->streaming;}
static bool vb2_is_busy(struct vb2_queue *q) {return q->busy;}
static int iris_vpu_buf_count(struct iris_inst *i,u32 type) {(void)i;(void)type;return 4;}
static u32 iris_dec_bitstream_buffer_size(struct iris_inst *i) {(void)i;return 4096;}
static u32 iris_enc_bitstream_buffer_size(struct iris_inst *i) {(void)i;return 4096;}
static bool iris_fmt_is_10bit(u32 f) {return f==V4L2_PIX_FMT_P010||f==V4L2_PIX_FMT_QC10C;}
static u32 captured[2], prop_calls;
static u32 iris_hfi_gen2_get_port(struct iris_inst *i,u32 plane) {(void)i;return plane;}
static int iris_hfi_gen2_session_set_property(struct iris_inst *i,u32 prop,u32 flags,u32 port,
    u32 payload_type,void *payload,u32 size) {
 (void)i;(void)prop;(void)flags;(void)port;assert(payload_type==13);assert(size==8);
 memcpy(captured,payload,8);prop_calls++;return 0;
}
'''

MAIN = r'''
int main(int argc,char **argv) {
 assert(argc==2);int mode=atoi(argv[1]);
 struct iris_platform_data pd={0};struct iris_core core={&pd};
 struct v4l2_format src={.type=9,.fmt.pix_mp={.width=1920,.height=1080,.pixelformat=V4L2_PIX_FMT_H264}};
 struct v4l2_format dst={.type=10,.fmt.pix_mp={.width=1920,.height=1088,.pixelformat=V4L2_PIX_FMT_NV12}};
 struct v4l2_m2m_ctx ctx={0};struct iris_inst i={.core=&core,.domain=DECODER,.fmt_src=&src,.fmt_dst=&dst,.m2m_ctx=&ctx};
 struct iris_capture_layout l={0};struct v4l2_format req={.type=10,.fmt.pix_mp={.width=1920,.height=1080,.pixelformat=V4L2_PIX_FMT_P010}};
 if(mode<5) {
  u32 width[]={128,192,640,1920,2048};assert(!iris_calc_capture_layout(V4L2_PIX_FMT_QC10C,width[mode],1080,&l));
  assert(l.width==DIV_ROUND_UP(width[mode],192)*192);assert(l.width%192==0);
  assert(l.stride==ALIGN(l.width*4/3,256));assert(l.size%4096==0);
 } else if(mode==5||mode==6) {
  assert(!iris_calc_capture_layout(V4L2_PIX_FMT_P010,mode==5?1920:1280,mode==5?1080:720,&l));
  assert(l.size==(mode==5?6266880u:2826240u));assert(l.stride==2*l.width);
 } else if(mode==7||mode==8||mode==9) {
  memset(&l,0xa5,sizeof(l));struct iris_capture_layout saved=l;
  assert(iris_calc_capture_layout(mode==9?V4L2_PIX_FMT_NV21:V4L2_PIX_FMT_NV12,mode==7?0:mode==8?UINT32_MAX:128,720,&l)==-EINVAL);
  assert(!memcmp(&l,&saved,sizeof(l)));
 } else if(mode==10) {
  assert(!iris_vdec_try_fmt(&i,&req));assert(req.fmt.pix_mp.width==1920);
  assert(req.fmt.pix_mp.plane_fmt[0].bytesperline==3840);assert(req.fmt.pix_mp.plane_fmt[0].sizeimage==6266880);
 } else if(mode==11) {
  assert(!iris_vdec_s_fmt(&i,&req));assert(dst.fmt.pix_mp.plane_fmt[0].bytesperline==3840);
  assert(i.buffers[BUF_OUTPUT].size==6266880);assert(req.fmt.pix_mp.plane_fmt[0].sizeimage==6266880);
  assert(!iris_hfi_gen2_set_linear_stride_scanline(&i,10));assert(prop_calls==1);
  assert(captured[0]==(3840u<<16|1088u));assert(captured[1]==(3840u<<16|544u));
 } else if(mode==12) {
  req.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_QC10C;
  assert(!iris_vdec_s_fmt(&i,&req));assert(dst.fmt.pix_mp.width==1920);assert(dst.fmt.pix_mp.height==1088);
  assert(dst.fmt.pix_mp.plane_fmt[0].bytesperline==2560);
 } else if(mode==13) {
  req.fmt.pix_mp.width=1919;req.fmt.pix_mp.height=1081;req.fmt.pix_mp.pixelformat=V4L2_PIX_FMT_NV12;
  assert(!iris_vdec_s_fmt(&i,&req));assert(dst.fmt.pix_mp.width==1920&&dst.fmt.pix_mp.height==1088);
  assert(dst.fmt.pix_mp.plane_fmt[0].bytesperline==1920);assert(i.buffers[BUF_OUTPUT].size==dst.fmt.pix_mp.plane_fmt[0].sizeimage);
 } else if(mode==14) {
  req.fmt.pix_mp.width=0;assert(iris_vdec_s_fmt(&i,&req)==-EINVAL);
 } else if(mode==15) {
  pd.prototype_restricted=true;assert(iris_vdec_try_fmt(&i,&req)==-EOPNOTSUPP);assert(iris_vdec_s_fmt(&i,&req)==-EOPNOTSUPP);
 } else if(mode==16) {
  pd.prototype_restricted=true;struct v4l2_fmtdesc d={.type=10,.index=1};assert(iris_vdec_enum_fmt(&i,&d)==-EINVAL);
  d.index=0;assert(!iris_vdec_enum_fmt(&i,&d));assert(d.pixelformat==V4L2_PIX_FMT_NV12);
 } else if(mode==17) {
  pd.prototype_restricted=true;assert(iris_vdec_validate_format(&i,V4L2_PIX_FMT_P010)==-EINVAL);
  assert(!iris_vdec_validate_format(&i,V4L2_PIX_FMT_NV12));
 } else if(mode==18) {
  assert(!iris_vdec_s_fmt(&i,&req));src.fmt.pix_mp.width=1279;src.fmt.pix_mp.height=719;
  assert(!iris_apply_capture_layout(&i,src.fmt.pix_mp.width,src.fmt.pix_mp.height));
  assert(dst.fmt.pix_mp.width==1280&&dst.fmt.pix_mp.height==736);
  assert(dst.fmt.pix_mp.plane_fmt[0].bytesperline==2560);assert(dst.fmt.pix_mp.plane_fmt[0].sizeimage==2826240);
 } else if(mode==19) {
  assert(!iris_vdec_s_fmt(&i,&req));struct v4l2_format input={.type=9,.fmt.pix_mp={.width=1280,.height=720,.pixelformat=V4L2_PIX_FMT_H264}};
  assert(!iris_vdec_s_fmt(&i,&input));assert(dst.fmt.pix_mp.plane_fmt[0].bytesperline==2560);
  assert(dst.fmt.pix_mp.plane_fmt[0].sizeimage==2826240&&i.buffers[BUF_OUTPUT].size==2826240);
 } else if(mode==20) {
  i.domain=ENCODER;pd.no_opb=true;assert(iris_hfi_gen2_buf_type_to_driver(&i,HFI_BUFFER_DPB)==BUF_DPB);
  pd.no_opb=false;assert(iris_hfi_gen2_buf_type_to_driver(&i,HFI_BUFFER_DPB)==BUF_SCRATCH_2);
 } else if(mode==21) {
  ctx.dst.busy=true;assert(iris_vdec_s_fmt(&i,&req)==-EBUSY);
 } else if(mode==22) {
  req.fmt.pix_mp.width=UINT32_MAX;assert(iris_vdec_try_fmt(&i,&req)==-EINVAL);
 } else if(mode==23) {
  dst.fmt.pix_mp.plane_fmt[0].bytesperline=0;assert(iris_hfi_gen2_set_linear_stride_scanline(&i,10)==-EINVAL);assert(prop_calls==0);
 } else return 2;
 return 0;
}
'''


def definition(text, name):
    pattern = r'^(?:static\s+)?(?:const\s+struct\s+iris_fmt\s*\*\s*|(?:int|u32|void|bool)\s+)' + re.escape(name) + r'\s*\([^;]*?\)\s*\{'
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise ValueError('Missing or ambiguous C definition: ' + name)
    start = matches[0].start()
    opening = text.index('{', start)
    depth, end = 1, opening + 1
    while depth:
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    return text[start:end] + '\n'


def fixture(work, output, sanitize=False):
    buffer = (work / 'iris_buffer.c').read_text()
    vdec = (work / 'iris_vdec.c').read_text()
    instance = (work / 'iris_instance.h').read_text()
    defs = ''.join(EXTRACT.aggregate_definition(instance, name) for name in ('iris_fmt_type', 'iris_fmt_type_cap', 'iris_fmt'))
    defs += EXTRACT.aggregate_definition((work / 'iris_buffer.h').read_text(), 'iris_capture_layout')
    defs += EXTRACT.aggregate_definition((work / 'iris_buffer.h').read_text(), 'iris_buffer_type')
    defs += EXTRACT.aggregate_definition((work / 'iris_hfi_gen2_defines.h').read_text(), 'hfi_buffer_type')
    defs += ''.join(line + '\n' for line in buffer.splitlines() if line.startswith('#define '))
    defs += '#define DEFAULT_WIDTH 320\n#define DEFAULT_HEIGHT 240\n#define HFI_PROP_LINEAR_STRIDE_SCANLINE 0x03000104\n'
    for name in ('iris_vdec_formats', 'iris_vdec_formats_cap'):
        match = re.search(r'^static const (?:struct iris_fmt|u32) ' + name + r'\[\] = \{.*?^\};', vdec, re.M | re.S)
        if not match:
            raise ValueError('Missing actual format table')
        defs += match.group() + '\n'
    functions = ''.join(definition(buffer, name) for name in ('iris_yuv_buffer_size_nv12',
        'iris_yuv_buffer_size_p010', 'iris_yuv_buffer_size_qc10c', 'iris_yuv_buffer_size_qc08c',
        'iris_calc_capture_layout', 'iris_apply_capture_layout', 'iris_get_buffer_size'))
    functions += ''.join(definition(vdec, name) for name in ('check_format', 'find_format', 'find_format_by_index',
        'iris_vdec_enum_fmt', 'iris_vdec_try_fmt', 'iris_vdec_s_fmt', 'iris_vdec_validate_format'))
    functions += definition((work / 'iris_hfi_gen2_command.c').read_text(), 'iris_hfi_gen2_set_linear_stride_scanline')
    functions += definition((work / 'iris_hfi_gen2_response.c').read_text(), 'iris_hfi_gen2_buf_type_to_driver')
    source = output / 'actual-layout.c'
    source.write_text(PREFIX + defs + functions + MAIN)
    binary = output / 'actual-layout'
    args = ['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O1', str(source), '-o', str(binary)]
    if sanitize:
        args.extend(['-fsanitize=address,undefined', '-fno-omit-frame-pointer'])
    result = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise RuntimeError(result.stdout + result.stderr)
    return binary


class OutputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-layout-tests-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = fixture(WORK, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


for mode in range(24):
    def test(self, value=mode):
        self.run_case(value)
    setattr(OutputTests, 'test_case_%02d' % mode, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--sanitize', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='iris-layout-inputs-') as directory:
        if args.work:
            WORK = args.work
        else:
            spec = importlib.util.spec_from_file_location('builder', HERE / 'build.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            WORK = Path(directory)
            builder.prepare_iris(Path('/var/tmp/linux-src/linux-6.18.18'), WORK)
        if args.sanitize:
            binary = fixture(WORK, Path(directory), True)
            for mode in range(24):
                result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=5)
                if result.returncode:
                    print(result.stdout + result.stderr)
                    return 1
            print('24 ASan/UBSan layout cases passed')
            return 0
        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(OutputTests))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
