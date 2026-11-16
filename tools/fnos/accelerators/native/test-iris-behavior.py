#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Compile actual patched Iris function bodies with deterministic host stubs.

This tests C control flow and emitted packet arguments, not firmware acceptance,
DMA correctness, physical register behavior or full driver lifecycle.
"""
import argparse
import importlib.util
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
WORK = None


def definition(text, name):
    pattern = (r'^(?:static\s+(?:inline\s+)?)?(?:int|void|u32)\s+' + re.escape(name) +
               r'\s*\([^;]*?\)\s*\{')
    matches = list(re.finditer(pattern, text, re.M))
    if len(matches) != 1:
        raise RuntimeError('Unique C function definition required: ' + name)
    start = matches[0].start()
    pos = text.index('{', start)
    depth = 1
    end = pos + 1
    while depth and end < len(text):
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    if depth:
        raise RuntimeError('Unbalanced C definition')
    return text[start:end] + '\n'


def aggregate_definition(text, name):
    match = re.search(r'^(enum|struct)\s+' + re.escape(name) + r'\s*\{', text, re.M)
    if not match:
        raise RuntimeError('Unique C aggregate definition required: ' + name)
    start = match.start()
    pos = text.index('{', start)
    depth = 1
    end = pos + 1
    while depth and end < len(text):
        if text[end] == '{':
            depth += 1
        elif text[end] == '}':
            depth -= 1
        end += 1
    if depth:
        raise RuntimeError('Unbalanced C aggregate')
    return text[start:end + 1] + ';\n'


PREFIX = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
typedef uint32_t u32;
typedef uint64_t u64;
#define BIT(n) (1u << (n))
#define ALIGN(value, alignment) (((value) + (alignment) - 1) & ~((alignment) - 1))
#define DECODER 1
#define ENCODER 2
#define IRIS_INST_ERROR 5
#define IRIS_CORE_INIT 1
#define MIN_BUFFERS 4
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_PIX_FMT_NV12 0x3231564eu
#define V4L2_PIX_FMT_P010 0x30313050u
#define V4L2_PIX_FMT_QC08C 0x43383051u
#define V4L2_PIX_FMT_QC10C 0x43303151u
#define V4L2_PIX_FMT_NV21 0x3132564eu
#define IRIS_CTRL_CLK 0
#define IRIS_AXI_CLK 1
#define IRIS_CTRL_POWER_DOMAIN 0
#define STAGE 3
#define PIPE 4
#define CPU_CS_BASE_OFFS 0xa0000
#define WRAPPER_BASE_OFFS 0xb0000
struct v4l2_pix_format_mplane { u32 width, height, pixelformat; };
struct v4l2_format { struct { struct v4l2_pix_format_mplane pix_mp; } fmt; };
struct iris_buffers { u32 min_count, size; };
struct iris_platform_data { bool no_opb, no_tz_fifo_reset, prototype_restricted; };
struct test_domains { void *pd_devs[1]; };
struct iris_core { struct iris_platform_data *iris_platform_data; unsigned char *reg_base;
                   struct test_domains *pmdomain_tbl; u32 state; };
struct iris_packet { u32 size; };
struct iris_inst_hfi_gen2 { struct iris_packet *packet; };
struct iris_inst { struct iris_core *core; u32 domain, session_id;
                   struct iris_inst_hfi_gen2 *hfi;
                   struct v4l2_format *fmt_src, *fmt_dst;
                   u32 codec; u32 fw_min_count; u32 state;
                   struct iris_buffers buffers[16]; };
struct iris_buffer { u32 type, index, data_offset, attr; u64 device_addr, timestamp;
                     size_t buffer_size, data_size; };
#define to_iris_inst_hfi_gen2(inst) ((inst)->hfi)
'''

SUFFIX = r'''
static struct iris_hfi_buffer captured_buffer;
static u32 captured_cmd, captured_flags, captured_payload_type, captured_length;
static u32 captured_payload;
static bool captured_null;
static int queue_calls, queue_result;
static int captured_prop, prop_calls;
static u32 captured_prop_payload;
static int split_mode, output_count;
static u32 iris_hfi_gen2_get_port(struct iris_inst *inst, u32 plane)
{ (void)inst; return plane; }
static u32 iris_hfi_gen2_get_port_from_buf_type(struct iris_inst *inst, u32 type)
{ (void)inst; return type; }
static void iris_hfi_gen2_packet_session_command(struct iris_inst *inst, u32 cmd,
        u32 flags, u32 port, u32 session, u32 payload_type, void *payload, u32 length)
{
    (void)inst; (void)port; (void)session;
    captured_cmd = cmd; captured_flags = flags; captured_payload_type = payload_type;
    captured_length = length; captured_null = payload == NULL;
    if (payload && length == sizeof(u32)) memcpy(&captured_payload, payload, sizeof(u32));
    if (payload && length == sizeof(captured_buffer))
        memcpy(&captured_buffer, payload, sizeof(captured_buffer));
}
static int iris_hfi_queue_cmd_write(struct iris_core *core, void *packet, u32 size)
{ (void)core; (void)packet; (void)size; queue_calls++; return queue_result; }
static int iris_hfi_gen2_session_set_property(struct iris_inst *inst, u32 prop, u32 flags,
        u32 port, u32 payload_type, void *payload, u32 size)
{
    (void)inst; (void)flags; (void)port; (void)payload_type;
    captured_prop = prop; prop_calls++;
    if (payload && size == sizeof(u32)) memcpy(&captured_prop_payload, payload, sizeof(u32));
    return 0;
}
static int iris_set_num_comv(struct iris_inst *inst) { (void)inst; return 0; }
static bool iris_split_mode_enabled(struct iris_inst *inst)
{ (void)inst; return split_mode; }
static int output_min_count(struct iris_inst *inst) { (void)inst; return output_count; }
static int internal_buffer_count(struct iris_inst *inst, u32 type)
{ (void)inst; (void)type; return 1; }
static int events[32], event_count, fail_event;
static int event(int id)
{ events[event_count++] = id; return fail_event == id ? -77 : 0; }
static int iris_set_properties(struct iris_inst *inst, u32 plane)
{ (void)inst; (void)plane; return event(1); }
static int iris_alloc_and_queue_persist_bufs(struct iris_inst *inst, u32 type)
{ (void)inst; (void)type; return event(2); }
static int iris_set_stage(struct iris_inst *inst, u32 cap)
{ (void)inst; assert(cap == STAGE); return event(3); }
static int iris_set_pipe(struct iris_inst *inst, u32 cap)
{ (void)inst; assert(cap == PIPE); return event(4); }
static void iris_get_internal_buffers(struct iris_inst *inst, u32 plane)
{ (void)inst; (void)plane; (void)event(5); }
static int iris_destroy_dequeued_internal_buffers(struct iris_inst *inst, u32 plane)
{ (void)inst; (void)plane; return event(6); }
static int iris_create_internal_buffers(struct iris_inst *inst, u32 plane)
{ (void)inst; (void)plane; return event(7); }
static int iris_queue_internal_buffers(struct iris_inst *inst, u32 plane)
{ (void)inst; (void)plane; return event(8); }
static int iris_process_streamon_input(struct iris_inst *inst)
{ (void)inst; return event(9); }
static unsigned char registers[0xf0000];
static uintptr_t write_offsets[32];
static u32 write_values[32];
static int write_count, poll_count, fail_poll, clock_count, power_count;
static void writel(u32 value, void *address)
{
    assert(write_count < 32);
    write_offsets[write_count] = (uintptr_t)address - (uintptr_t)registers;
    write_values[write_count++] = value;
}
static int poll(void) { poll_count++; return poll_count == fail_poll ? -88 : 0; }
#define readl_poll_timeout(address, val, condition, delay, timeout) \
    ((void)(address), (void)(delay), (void)(timeout), (val) = 1, (void)(condition), poll())
static void iris_disable_unprepare_clock(struct iris_core *core, int type)
{ (void)core; (void)type; clock_count++; }
static int iris_disable_power_domains(struct iris_core *core, void *domain)
{ (void)core; (void)domain; power_count++; return 0; }
'''

MAIN = r'''
int main(int argc, char **argv)
{
    assert(argc == 2);
    int mode = atoi(argv[1]);
    struct iris_platform_data data = {0};
    struct test_domains domains = {0};
    struct iris_core core = {&data, registers, &domains, IRIS_CORE_INIT};
    struct iris_packet packet = {64};
    struct iris_inst_hfi_gen2 hfi = {&packet};
    struct v4l2_format src = {0}, dst = {0};
    struct iris_inst inst = {0};
    inst.core = &core; inst.domain = DECODER; inst.session_id = 9; inst.hfi = &hfi;
    inst.fmt_src = &src; inst.fmt_dst = &dst;
    struct iris_buffer buffer = {.type=BUF_INPUT, .index=2, .device_addr=0x90000000,
        .timestamp=77, .buffer_size=257, .data_offset=3, .data_size=91};
    if (mode <= 2) {
        data.no_opb = mode != 0;
        queue_result = mode == 2 ? -99 : 0;
        assert(iris_hfi_gen2_session_resume_drc(&inst, 9) == queue_result);
        assert(captured_cmd == HFI_CMD_RESUME);
        assert(captured_flags == (HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED));
        assert(captured_payload_type == (data.no_opb ? HFI_PAYLOAD_NONE : HFI_PAYLOAD_U32));
        assert(captured_length == (data.no_opb ? 0 : sizeof(u32)));
        assert(captured_null == data.no_opb);
        if (!data.no_opb) assert(captured_payload == HFI_CMD_SETTINGS_CHANGE);
        assert(queue_calls == 1);
    } else if ((mode >= 3 && mode <= 8) || mode == 27 || mode == 28) {
        data.no_opb = mode & 1;
        bool release = mode == 5 || mode == 6;
        if (mode == 7 || mode == 8) buffer.attr = BUF_ATTR_PENDING_RELEASE;
        queue_result = mode >= 27 ? -55 : 0;
        if (release) assert(iris_hfi_gen2_session_release_buffer(&inst, &buffer) == queue_result);
        else assert(iris_hfi_gen2_session_queue_buffer(&inst, &buffer) == queue_result);
        assert(captured_cmd == HFI_CMD_BUFFER);
        assert(captured_payload_type == HFI_PAYLOAD_STRUCTURE);
        assert(captured_length == sizeof(captured_buffer));
        assert(captured_buffer.base_address == buffer.device_addr);
        assert(captured_buffer.index == 2 && captured_buffer.timestamp == 77);
        assert(captured_buffer.buffer_size == 512);
        assert(captured_buffer.data_offset == 3 && captured_buffer.data_size == 91);
        assert(!!(captured_buffer.flags & HFI_BUF_HOST_FLAGS_CB_NON_SECURE) == !data.no_opb);
        assert(!!(captured_buffer.flags & HFI_BUF_HOST_FLAG_RELEASE) == (release || buffer.attr));
        assert(queue_calls == 1);
    } else if (mode >= 10 && mode <= 19) {
        data.no_opb = mode != 10;
        fail_event = mode == 10 || mode == 11 ? 0 : mode - 10;
        int ret = iris_vdec_streamon_input(&inst);
        assert(ret == (fail_event ? -77 : 0));
        if (mode == 10) {
            int expected[] = {1,2,5,6,7,8,9};
            assert(event_count == 7 && !memcmp(events, expected, sizeof(expected)));
        } else if (mode == 11) {
            int expected[] = {1,2,3,4,5,6,7,8,9};
            assert(event_count == 9 && !memcmp(events, expected, sizeof(expected)));
        } else {
            assert(events[event_count-1] == fail_event);
            if (fail_event < 9) assert(events[event_count-1] != 9);
        }
    } else if (mode >= 20 && mode <= 24) {
        data.no_tz_fifo_reset = mode != 20;
        fail_poll = mode >= 22 ? mode - 21 : 0;
        assert(iris_vpu_power_off_controller(&core) == 0);
        assert(clock_count == 2 && power_count == 1);
        int fifo = 0, tzclock = 0;
        for (int i=0; i<write_count; i++) {
            if (write_offsets[i] == WRAPPER_TZ_QNS4PDXFIFO_RESET) fifo++;
            if (write_offsets[i] == WRAPPER_TZ_CTL_AXI_CLOCK_CONFIG) tzclock++;
        }
        assert(fifo == (data.no_tz_fifo_reset ? 0 : 2));
        assert(tzclock == (data.no_tz_fifo_reset ? 0 : 2));
        if (!data.no_tz_fifo_reset) {
            assert(write_values[write_count-4] == (CTL_AXI_CLK_HALT | CTL_CLK_HALT));
            assert(write_values[write_count-3] == RESET_HIGH);
            assert(write_values[write_count-2] == 0 && write_values[write_count-1] == 0);
        }
        assert(poll_count == (fail_poll ? fail_poll : 3));
    } else if (mode == 25) {
        struct iris_hfi_buffer out;
        buffer.type = BUF_OUTPUT;
        buffer.attr = BUF_ATTR_PENDING_RELEASE;
        iris_hfi_gen2_get_buffer(ENCODER, &buffer, &out, false);
        assert(out.buffer_size == 257);
        assert(out.flags == HFI_BUF_HOST_FLAG_RELEASE);
        assert(out.type == HFI_BUFFER_BITSTREAM);
        for (int i=0; i<5; i++) assert(out.reserved[i] == 0);
    } else if (mode == 26) {
        fail_event = 1;
        data.no_opb = true;
        assert(iris_vdec_streamon_input(&inst) == -77);
        assert(event_count == 1 && events[0] == 1);
    } else if (mode == 30) {
        src.fmt.pix_mp.width = 1920; src.fmt.pix_mp.height = 1080;
        src.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
        assert(iris_vpu_enc_dpb_size(&inst) == 3158016u);
    } else if (mode == 31) {
        src.fmt.pix_mp.width = 1280; src.fmt.pix_mp.height = 720;
        src.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_P010;
        assert(iris_vpu_enc_dpb_size(&inst) == 2031616u);
    } else if (mode == 38) {
        src.fmt.pix_mp.width = 1; src.fmt.pix_mp.height = 1;
        src.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
        assert(iris_vpu_enc_dpb_size(&inst) == 9216u);
    } else if (mode == 32 || mode == 33 || mode == 34 || mode == 39) {
        inst.domain = mode == 34 ? DECODER : ENCODER;
        split_mode = mode != 39;
        output_count = 7;
        inst.fw_min_count = mode == 34 ? 3 : 5;
        data.no_opb = mode == 33 || mode == 39 || mode == 34;
        int count = iris_vpu_buf_count(&inst, BUF_DPB);
        if (mode == 32) assert(count == 0);
        else if (mode == 34) assert(count == 3);
        else assert(count == 2);
    } else if ((mode >= 35 && mode <= 37) || mode == 40) {
        data.no_opb = mode != 37;
        if (mode == 40) {
            inst.domain = ENCODER;
            src.fmt.pix_mp.pixelformat = V4L2_PIX_FMT_NV12;
        } else {
            inst.domain = DECODER;
            dst.fmt.pix_mp.pixelformat =
                (mode == 35 || mode == 40) ? V4L2_PIX_FMT_NV12 : V4L2_PIX_FMT_NV21;
        }
        int ret = iris_hfi_gen2_set_colorformat(&inst, 9);
        if (mode == 36 || mode == 37) {
            assert(ret == -EINVAL && prop_calls == 0);
        } else {
            assert(ret == 0 && prop_calls == 1);
            assert(captured_prop == HFI_PROP_COLOR_FORMAT);
            assert(captured_prop_payload == (u32)HFI_COLOR_FMT_NV12);
        }
    } else {
        return 2;
    }
    return 0;
}
'''


def create_fixture(work, output):
    hfi = (work / 'iris_hfi_gen2_command.c').read_text()
    power = (work / 'iris_vpu_common.c').read_text()
    header = (work / 'iris_hfi_gen2_defines.h').read_text()
    common = (work / 'iris_hfi_common.h').read_text()
    buffer_header = (work / 'iris_buffer.h').read_text()
    packet_header = (work / 'iris_hfi_gen2_packet.h').read_text()
    definitions = ''.join(line + '\n' for line in header.splitlines()
                          if re.match(r'#define (HFI_CMD_(RESUME|BUFFER|SETTINGS_CHANGE)|'
                                      r'HFI_PROP_COLOR_FORMAT)\s', line))
    definitions += aggregate_definition(common, 'hfi_packet_payload_info')
    definitions += aggregate_definition(common, 'hfi_packet_host_flags')
    definitions += aggregate_definition(header, 'hfi_buffer_type')
    definitions += aggregate_definition(header, 'hfi_buffer_host_flags')
    definitions += aggregate_definition(header, 'hfi_color_format')
    definitions += aggregate_definition(buffer_header, 'iris_buffer_type')
    definitions += aggregate_definition(buffer_header, 'iris_buffer_attributes')
    definitions += aggregate_definition(packet_header, 'iris_hfi_buffer')
    power_defines = ''.join(line + '\n' for line in power.splitlines() if line.startswith('#define '))
    functions = ''.join(definition(hfi, name) for name in (
        'iris_hfi_gen2_buf_type_from_driver', 'iris_hfi_gen2_get_buffer',
        'iris_hfi_gen2_session_resume_drc', 'iris_hfi_gen2_session_queue_buffer',
        'iris_hfi_gen2_session_release_buffer', 'iris_hfi_gen2_set_colorformat'))
    functions += definition((work / 'iris_vdec.c').read_text(), 'iris_vdec_streamon_input')
    functions += definition(power, 'iris_vpu_power_off_controller')
    vpu = (work / 'iris_vpu_buffer.c').read_text()
    vpu_header = (work / 'iris_vpu_buffer.h').read_text()
    required = ('SYSTEM_LAL_TILE10', 'IRIS_METADATA_STRIDE_MULTIPLE',
                'IRIS_METADATA_HEIGHT_MULTIPLE', 'HFI_COL_FMT_NV12C_Y_TILE_WIDTH',
                'HFI_COL_FMT_NV12C_Y_TILE_HEIGHT', 'HFI_COL_FMT_TP10C_Y_TILE_WIDTH',
                'HFI_COL_FMT_TP10C_Y_TILE_HEIGHT')
    for name in required:
        rows = re.findall(r'^#define\s+' + name + r'\s+(.+)$', vpu_header + '\n' + vpu, re.M)
        if len(rows) != 1:
            raise ValueError('Missing DPB source constant: ' + name)
        definitions += '#define ' + name + ' ' + rows[0] + '\n'
    functions += definition((work / 'iris_utils.c').read_text().replace('bool iris_fmt_is_10bit',
                            'u32 iris_fmt_is_10bit'), 'iris_fmt_is_10bit')
    for name in ('size_enc_ref_buffer', 'size_enc_ten_bit_ref_buffer',
                 'hfi_ubwc_calc_metadata_plane_stride', 'hfi_ubwc_metadata_plane_bufheight',
                 'hfi_ubwc_metadata_plane_buffer_size', 'hfi_buffer_dpb_enc'):
        normalized = vpu.replace('static inline\nu32 ' + name, 'static inline u32 ' + name)
        functions += definition(normalized, name)
    functions += definition(vpu, 'iris_vpu_enc_dpb_size')
    functions += definition((work / 'iris_vpu_buffer.c').read_text(), 'iris_vpu_dpb_count')
    functions += definition((work / 'iris_vpu_buffer.c').read_text(), 'iris_vpu_buf_count')
    source = output / 'actual-iris-paths.c'
    source.write_text(PREFIX + definitions + SUFFIX + power_defines + functions + MAIN)
    binary = output / 'actual-iris-paths'
    compile_fixture(source, binary)
    return binary


def compile_fixture(source, binary):
    result = subprocess.run(['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2',
                             str(source), '-o', str(binary)], capture_output=True, text=True,
                            timeout=30, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError('C behavior fixture compilation failed:\n' + result.stdout + result.stderr)


class ActualCBehaviorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-actual-c-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = create_fixture(WORK, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True, timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation_rejected(self, before, after, mode):
        with tempfile.TemporaryDirectory(prefix='iris-behavior-mutation-') as directory:
            path = Path(directory)
            source = (Path(self.temp.name) / 'actual-iris-paths.c').read_text()
            self.assertEqual(source.count(before), 1)
            target = path / 'mutated.c'
            target.write_text(source.replace(before, after, 1))
            binary = path / 'mutated'
            compile_fixture(target, binary)
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True, timeout=3)
            self.assertNotEqual(result.returncode, 0, 'Behavior oracle accepted a deliberate regression')

    def test_oracle_rejects_fifo_reset_regression(self):
        self.mutation_rejected('if (!core->iris_platform_data->no_tz_fifo_reset) {',
                               'if (1) {', 21)

    def test_oracle_rejects_resume_payload_regression(self):
        self.mutation_rejected('bool bare = inst->core->iris_platform_data->no_opb;',
                               'bool bare = false;', 1)

    def test_oracle_rejects_context_flag_regression(self):
        self.mutation_rejected('if (cb_flag)\n', 'if (cb_flag || !cb_flag)\n', 3)

    def test_oracle_rejects_stage_pipe_order_regression(self):
        source = (Path(self.temp.name) / 'actual-iris-paths.c').read_text()
        function = definition(source, 'iris_vdec_streamon_input')
        modified = function.replace('iris_set_stage(inst, STAGE)', '__SWAP__', 1)
        modified = modified.replace('iris_set_pipe(inst, PIPE)', 'iris_set_stage(inst, STAGE)', 1)
        modified = modified.replace('__SWAP__', 'iris_set_pipe(inst, PIPE)', 1)
        self.mutation_rejected(function, modified, 11)

    def test_oracle_rejects_dpb_count_regression(self):
        self.mutation_rejected('return inst->core->iris_platform_data->no_opb ? 2 : 0;',
                               'return 0;', 33)

    def test_oracle_rejects_unsupported_color_regression(self):
        self.mutation_rejected('case V4L2_PIX_FMT_QC10C:\n\t\t\thfi_colorformat = HFI_COLOR_FMT_TP10_UBWC;\n\t\t\tbreak;\n\t\tdefault:\n\t\t\treturn -EINVAL;',
                               'case V4L2_PIX_FMT_QC10C:\n\t\t\thfi_colorformat = HFI_COLOR_FMT_TP10_UBWC;\n\t\t\tbreak;\n\t\tdefault:\n\t\t\thfi_colorformat = HFI_COLOR_FMT_NV12;\n\t\t\tbreak;', 36)

def install_cases():
    names = {0: 'default_resume_payload', 1: 'vpu2_bare_resume', 2: 'resume_queue_error',
             3: 'vpu2_queue_context_flags', 4: 'default_queue_context_flags',
             5: 'vpu2_release_context_flags', 6: 'default_release_context_flags',
             7: 'vpu2_pending_release', 8: 'default_pending_release',
             10: 'default_start_order', 11: 'vpu2_stage_pipe_start_order',
             12: 'persist_error_stops_start', 13: 'stage_error_stops_start',
             14: 'pipe_error_stops_start', 16: 'destroy_error_stops_start',
             17: 'create_error_stops_start', 18: 'queue_error_stops_start',
             19: 'start_error_propagated', 20: 'default_fifo_reset_sequence',
             21: 'vpu2_skips_fifo_reset', 22: 'first_poll_error_cleanup',
             23: 'second_poll_error_cleanup', 24: 'third_poll_error_cleanup',
             25: 'encoder_buffer_alignment_and_flags', 26: 'property_error_stops_start',
             27: 'vpu2_buffer_queue_error_propagated', 28: 'default_buffer_queue_error_propagated',
             30: 'encoder_dpb_size_1080p_8bit', 31: 'encoder_dpb_size_720p_10bit',
             32: 'encoder_dpb_disabled_without_vpu2', 33: 'encoder_dpb_two_for_vpu2',
             34: 'decoder_dpb_uses_fw_min_count', 35: 'vpu2_nv12_color_format',
             36: 'vpu2_unsupported_color_format_refused', 37: 'default_unsupported_color_format_refused',
             38: 'encoder_dpb_size_minimum_frame', 39: 'encoder_dpb_independent_of_split_mode',
             40: 'vpu2_encoder_nv12_color_format'}
    for mode, name in names.items():
        def test(self, value=mode):
            self.run_case(value)
        setattr(ActualCBehaviorTests, 'test_' + name, test)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, help='Already patched Iris build directory')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='iris-behavior-sources-') as temporary:
        if args.work:
            WORK = args.work
        else:
            upstream = Path('/var/tmp/linux-src/linux-6.18.18')
            spec = importlib.util.spec_from_file_location('iris_builder', HERE / 'build.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            WORK = Path(temporary)
            builder.prepare_iris(upstream, WORK)
        install_cases()
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ActualCBehaviorTests))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())