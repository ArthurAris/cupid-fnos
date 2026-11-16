#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Byte-exact HFI packet/lifecycle oracle built from the actual patched C code.

This compiles the real 6.18.18 serializer and selected command wrappers with
deterministic host stubs, then asserts wire bytes and command sequencing. It does
not establish firmware acceptance, DMA correctness or physical register behavior.
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
typedef unsigned char u8;
typedef int spinlock_t;
#define spin_lock_init(lock) (*(lock)=0)
#define spin_lock_irqsave(lock, flags) ((void)(lock),(flags)=0)
#define spin_unlock_irqrestore(lock, flags) ((void)(lock),(void)(flags))
#define U32_MAX UINT32_MAX
/* TRACKER */

#define BIT(n) (1u << (n))
#define GFP_KERNEL 0
#define DECODER 1
#define ENCODER 2
#define IRIS_INST_DEINIT 0
#define V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE 9
#define V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE 10
#define V4L2_TYPE_IS_OUTPUT(type) ((type) == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
#define HFI_VIDEO_ARCH_LX 0x1
#define NUM_SYS_INIT_PACKETS 8

struct completion { int done; };
#define reinit_completion(c) ((c)->done = 0)

struct ubwc_config_data;

struct iris_platform_data {
	struct ubwc_config_data *ubwc_config;
	bool no_opb;
};

struct iris_core {
	struct iris_platform_data *iris_platform_data;
	u32 header_id;
	u32 packet_id;
	struct iris_hfi_pending init_pending;
};

struct iris_inst_hfi_gen2 {
	struct iris_hfi_header *packet;
	bool ipsc_properties_set;
	bool opsc_properties_set;
	struct iris_hfi_pending open_pending, start_pending[2];
};

struct iris_inst {
	struct iris_core *core;
	struct iris_inst_hfi_gen2 *hfi;
	u32 session_id;
	u32 state;
	u32 domain;
	struct completion completion;
	struct iris_hfi_pending control_pending;
};

#define to_iris_inst_hfi_gen2(inst) ((inst)->hfi)
'''

SUFFIX = r'''
static void *kzalloc(size_t size, int flags)
{
	(void)flags;
	return calloc(1, size);
}

#define kfree free

#define SYS_INIT_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	NUM_SYS_INIT_PACKETS * (sizeof(struct iris_hfi_packet) + sizeof(u32)))
#define SYS_IFPC_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	sizeof(struct iris_hfi_packet) + sizeof(u32))
#define SYS_NO_PAYLOAD_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	sizeof(struct iris_hfi_packet))

static unsigned char cap[16][4096];
static u32 cap_len[16];
static u32 cap_calls;
static int queue_result;
static int set_codec_calls, default_header_calls;
static int subscribe_input_calls, subscribe_output_calls, wait_calls;
static int wait_result;
static u32 subscribe_input_plane, subscribe_output_plane;

static int capture_packet(void *packet, u32 size)
{
	assert(cap_calls < 16);
	assert(size <= 4096);
	memcpy(cap[cap_calls], packet, size);
	cap_len[cap_calls] = size;
	cap_calls++;
	return queue_result;
}

static int iris_hfi_queue_cmd_write(struct iris_core *core, void *packet, u32 size)
{
	(void)core;
	return capture_packet(packet, size);
}

static int iris_hfi_queue_cmd_write_locked(struct iris_core *core, void *packet, u32 size)
{
	(void)core;
	return capture_packet(packet, size);
}

static int iris_hfi_gen2_session_set_codec(struct iris_inst *inst)
{
	(void)inst;
	set_codec_calls++;
	return 0;
}

static int iris_hfi_gen2_session_set_default_header(struct iris_inst *inst)
{
	(void)inst;
	default_header_calls++;
	return 0;
}

static int iris_hfi_gen2_subscribe_change_param(struct iris_inst *inst, u32 plane)
{
	(void)inst;
	subscribe_input_calls++;
	subscribe_input_plane = plane;
	return 0;
}

static int iris_hfi_gen2_subscribe_property(struct iris_inst *inst, u32 plane)
{
	(void)inst;
	subscribe_output_calls++;
	subscribe_output_plane = plane;
	return 0;
}

static int iris_wait_for_session_response(struct iris_inst *inst, bool error)
{
	(void)error;
	iris_hfi_pending_cancel(&inst->control_pending);
	wait_calls++;
	return wait_result;
}

static const struct iris_hfi_header *header_at(const void *buffer)
{
	return (const struct iris_hfi_header *)buffer;
}

static const struct iris_hfi_packet *packet_at(const void *buffer, u32 offset)
{
	return (const struct iris_hfi_packet *)((const unsigned char *)buffer + offset);
}

static u32 packet_u32(const struct iris_hfi_packet *packet)
{
	u32 value;

	memcpy(&value, packet->payload, sizeof(value));
	return value;
}

static void check_header(const void *buffer, u32 size, u32 session, u32 header_id, u32 count)
{
	const struct iris_hfi_header *header = header_at(buffer);

	assert(header->size == size);
	assert(header->session_id == session);
	assert(header->header_id == header_id);
	assert(header->num_packets == count);
}

static void check_packet(const struct iris_hfi_packet *packet, u32 size, u32 type, u32 flags,
			 u32 payload, u32 port, u32 packet_id)
{
	assert(packet->size == size);
	assert(packet->type == type);
	assert(packet->flags == flags);
	assert(packet->payload_info == payload);
	assert(packet->port == port);
	assert(packet->packet_id == packet_id);
}

static u32 walk_packets(const void *buffer, u32 size)
{
	u32 offset = sizeof(struct iris_hfi_header);

	while (offset < size) {
		const struct iris_hfi_packet *packet = packet_at(buffer, offset);

		assert(packet->size >= sizeof(*packet));
		offset += packet->size;
	}
	return offset;
}
'''

MAIN = r'''
int main(int argc, char **argv)
{
	assert(argc == 2);
	int mode = atoi(argv[1]);
	struct ubwc_config_data ubwc = {8, 32, 16, 0, 1, 1, 1};
	struct iris_platform_data data = { &ubwc, false };
	struct iris_core core = { .iris_platform_data = &data };
	struct iris_inst_hfi_gen2 hfi = {0};
	struct iris_inst inst = {0};
	unsigned char packet_buffer[4096];

	inst.core = &core;
	inst.hfi = &hfi;
	inst.session_id = 9;
	inst.state = IRIS_INST_DEINIT;
	inst.domain = DECODER;
	iris_hfi_pending_init(&core.init_pending);
	iris_hfi_pending_init(&inst.control_pending);

	if (mode == 0) {
		assert(iris_hfi_gen2_sys_init(&core) == 0);
		assert(cap_calls == 1 && cap_len[0] == 320);
		check_header(cap[0], 320, 0, 0, 8);
		u32 types[8] = { HFI_CMD_INIT, HFI_PROP_UBWC_MAX_CHANNELS,
			HFI_PROP_UBWC_MAL_LENGTH, HFI_PROP_UBWC_HBB,
			HFI_PROP_UBWC_BANK_SWZL_LEVEL1, HFI_PROP_UBWC_BANK_SWZL_LEVEL2,
			HFI_PROP_UBWC_BANK_SWZL_LEVEL3, HFI_PROP_UBWC_BANK_SPREADING };
		u32 values[8] = { HFI_VIDEO_ARCH_LX, 8, 32, 16, 0, 1, 1, 1 };
		u32 offset = sizeof(struct iris_hfi_header);
		for (u32 i = 0; i < 8; i++) {
			const struct iris_hfi_packet *packet = packet_at(cap[0], offset);
			u32 flags = i == 0 ? (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					      HFI_HOST_FLAGS_INTR_REQUIRED |
					      HFI_HOST_FLAGS_NON_DISCARDABLE) :
					     HFI_HOST_FLAGS_NONE;
			check_packet(packet, 36, types[i], flags, HFI_PAYLOAD_U32,
				     HFI_PORT_NONE, i);
			assert(packet_u32(packet) == values[i]);
			offset += packet->size;
		}
		assert(offset == 320 && walk_packets(cap[0], 320) == 320);
		assert(core.header_id == 1 && core.packet_id == 8);
	} else if (mode == 1) {
		assert(iris_hfi_gen2_sys_image_version(&core) == 0);
		assert(cap_calls == 1 && cap_len[0] == 64);
		check_header(cap[0], 64, 0, 0, 1);
		const struct iris_hfi_packet *packet = packet_at(cap[0], 32);
		check_packet(packet, 32, HFI_PROP_IMAGE_VERSION,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED |
			     HFI_HOST_FLAGS_GET_PROPERTY, HFI_PAYLOAD_NONE, HFI_PORT_NONE, 0);
		assert(walk_packets(cap[0], 64) == 64);
		assert(core.header_id == 1 && core.packet_id == 1);
	} else if (mode == 2) {
		u32 payload = 0xdeadbeef;
		memset(packet_buffer, 0xa5, sizeof(packet_buffer));
		hfi.packet = (struct iris_hfi_header *)packet_buffer;
		iris_hfi_gen2_packet_session_command(&inst, HFI_CMD_START,
			HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			HFI_PORT_BITSTREAM, 7, HFI_PAYLOAD_U32, &payload, sizeof(payload));
		check_header(packet_buffer, 68, 7, 0, 1);
		const struct iris_hfi_packet *packet = packet_at(packet_buffer, 32);
		check_packet(packet, 36, HFI_CMD_START,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_U32, HFI_PORT_BITSTREAM, 0);
		assert(packet_u32(packet) == payload);
		assert(walk_packets(packet_buffer, 68) == 68);
	} else if (mode == 3) {
		unsigned char payload[16];
		for (u32 i = 0; i < sizeof(payload); i++)
			payload[i] = (unsigned char)(0x10 + i);
		hfi.packet = (struct iris_hfi_header *)packet_buffer;
		iris_hfi_gen2_packet_session_property(&inst, HFI_PROP_UBWC_HBB,
			HFI_HOST_FLAGS_NONE, HFI_PORT_RAW, HFI_PAYLOAD_STRUCTURE,
			payload, sizeof(payload));
		check_header(packet_buffer, 80, 9, 0, 1);
		const struct iris_hfi_packet *packet = packet_at(packet_buffer, 32);
		check_packet(packet, 48, HFI_PROP_UBWC_HBB, HFI_HOST_FLAGS_NONE,
			     HFI_PAYLOAD_STRUCTURE, HFI_PORT_RAW, 0);
		assert(memcmp(packet->payload, payload, sizeof(payload)) == 0);
		assert(walk_packets(packet_buffer, 80) == 80);
	} else if (mode == 4) {
		assert(iris_hfi_gen2_sys_interframe_powercollapse(&core) == 0);
		assert(iris_hfi_gen2_sys_pc_prep(&core) == 0);
		assert(cap_calls == 2);
		assert(cap_len[0] == 68 && cap_len[1] == 64);
		check_header(cap[0], 68, 0, 0, 1);
		check_packet(packet_at(cap[0], 32), 36, HFI_PROP_INTRA_FRAME_POWER_COLLAPSE,
			     HFI_HOST_FLAGS_NONE, HFI_PAYLOAD_U32, HFI_PORT_NONE, 0);
		assert(packet_u32(packet_at(cap[0], 32)) == 1);
		check_header(cap[1], 64, 0, 1, 1);
		check_packet(packet_at(cap[1], 32), 32, HFI_CMD_POWER_COLLAPSE,
			     HFI_HOST_FLAGS_NONE, HFI_PAYLOAD_NONE, HFI_PORT_NONE, 1);
	} else if (mode == 5) {
		assert(iris_hfi_gen2_session_open(&inst) == 0);
		assert(cap_calls == 1 && cap_len[0] == 68);
		check_header(cap[0], 68, 0, 0, 1);
		check_packet(packet_at(cap[0], 32), 36, HFI_CMD_OPEN,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_U32, HFI_PORT_NONE, 0);
		assert(packet_u32(packet_at(cap[0], 32)) == 9);
		assert(hfi.packet != NULL);
		assert(set_codec_calls == 1 && default_header_calls == 1);
		inst.state = 1;
		assert(iris_hfi_gen2_session_close(&inst) == 0);
		assert(cap_calls == 2 && cap_len[1] == 64);
		check_header(cap[1], 64, 9, 1, 1);
		check_packet(packet_at(cap[1], 32), 32, HFI_CMD_CLOSE,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED |
			     HFI_HOST_FLAGS_NON_DISCARDABLE, HFI_PAYLOAD_NONE, HFI_PORT_NONE, 1);
		assert(hfi.packet == NULL);
	} else if (mode == 6) {
		hfi.packet = (struct iris_hfi_header *)packet_buffer;
		assert(iris_hfi_gen2_session_start(&inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) == 0);
		assert(subscribe_input_calls == 1 && subscribe_output_calls == 1);
		assert(subscribe_input_plane == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
		assert(subscribe_output_plane == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
		assert(cap_calls == 1 && cap_len[0] == 64);
		check_header(cap[0], 64, 9, 0, 1);
		check_packet(packet_at(cap[0], 32), 32, HFI_CMD_START,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_NONE, HFI_PORT_BITSTREAM, 0);
		assert(iris_hfi_gen2_session_stop(&inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) == 0);
		assert(wait_calls == 1);
		assert(cap_calls == 2 && cap_len[1] == 64);
		check_packet(packet_at(cap[1], 32), 32, HFI_CMD_STOP,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED |
			     HFI_HOST_FLAGS_NON_DISCARDABLE, HFI_PAYLOAD_NONE, HFI_PORT_BITSTREAM, 1);
		assert(iris_hfi_gen2_session_drain(&inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) == 0);
		assert(cap_calls == 2);
		assert(iris_hfi_gen2_session_drain(&inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) == 0);
		assert(cap_calls == 3 && cap_len[2] == 64);
		check_packet(packet_at(cap[2], 32), 32, HFI_CMD_DRAIN,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED |
			     HFI_HOST_FLAGS_NON_DISCARDABLE, HFI_PAYLOAD_NONE, HFI_PORT_BITSTREAM, 2);
		assert(iris_hfi_gen2_session_pause(&inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) == 0);
		assert(cap_calls == 4 && cap_len[3] == 64);
		check_packet(packet_at(cap[3], 32), 32, HFI_CMD_PAUSE,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_NONE, HFI_PORT_RAW, 3);
		assert(iris_hfi_gen2_session_resume_drc(&inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) == 0);
		assert(cap_calls == 5 && cap_len[4] == 68);
		check_packet(packet_at(cap[4], 32), 36, HFI_CMD_RESUME,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_U32, HFI_PORT_RAW, 4);
		assert(packet_u32(packet_at(cap[4], 32)) == HFI_CMD_SETTINGS_CHANGE);
		assert(iris_hfi_gen2_session_resume_drain(&inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE) == 0);
		assert(cap_calls == 6 && cap_len[5] == 68);
		check_packet(packet_at(cap[5], 32), 36, HFI_CMD_RESUME,
			     HFI_HOST_FLAGS_RESPONSE_REQUIRED | HFI_HOST_FLAGS_INTR_REQUIRED,
			     HFI_PAYLOAD_U32, HFI_PORT_RAW, 5);
		assert(packet_u32(packet_at(cap[5], 32)) == HFI_CMD_DRAIN);
	} else if (mode == 7) {
		queue_result = -5;
		assert(iris_hfi_gen2_sys_init(&core) == -5);
		assert(cap_calls == 1);
		assert(iris_hfi_gen2_session_open(&inst) == -5);
		assert(hfi.packet == NULL && set_codec_calls == 0);
		queue_result = 0;
		assert(iris_hfi_gen2_session_open(&inst) == 0);
		assert(hfi.packet != NULL);
		queue_result = -5;
		assert(iris_hfi_gen2_session_close(&inst) == -5);
		assert(hfi.packet == NULL);
		hfi.packet = (struct iris_hfi_header *)kzalloc(4096, GFP_KERNEL);
		assert(hfi.packet != NULL);
		queue_result = 0;
		wait_result = -6;
		assert(iris_hfi_gen2_session_stop(&inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE) == -6);
		kfree(hfi.packet);
		hfi.packet = NULL;
	} else if (mode == 8) {
		core.header_id = 0xffffffffu;
		core.packet_id = 0xfffffffeu;
		assert(iris_hfi_gen2_sys_image_version(&core) == 0);
		check_header(cap[0], 64, 0, 0xffffffffu, 1);
		assert(packet_at(cap[0], 32)->packet_id == 0xfffffffeu);
		assert(core.header_id == 0 && core.packet_id == 0xffffffffu);
	} else if (mode == 9) {
		ubwc.max_channels = 4;
		ubwc.mal_length = 64;
		ubwc.highest_bank_bit = 15;
		ubwc.bank_swzl_level = 1;
		ubwc.bank_swz2_level = 0;
		ubwc.bank_swz3_level = 0;
		ubwc.bank_spreading = 0;
		assert(iris_hfi_gen2_sys_init(&core) == 0);
		u32 values[8] = { HFI_VIDEO_ARCH_LX, 4, 64, 15, 1, 0, 0, 0 };
		u32 offset = sizeof(struct iris_hfi_header);
		for (u32 i = 0; i < 8; i++) {
			const struct iris_hfi_packet *packet = packet_at(cap[0], offset);
			assert(packet_u32(packet) == values[i]);
			offset += packet->size;
		}
	} else if (mode == 10) {
		assert(iris_hfi_gen2_sys_init(&core) == 0);
		iris_hfi_pending_cancel(&core.init_pending);
		assert(iris_hfi_gen2_sys_init(&core) == 0);
		assert(cap_calls == 2 && cap_len[0] == 320 && cap_len[1] == 320);
		check_header(cap[1], 320, 0, 1, 8);
		assert(packet_at(cap[1], 32)->packet_id == 8);
		assert(core.header_id == 2 && core.packet_id == 16);
	} else {
		return 2;
	}
	return 0;
}
'''


def create_fixture(work, output):
    packet = (work / 'iris_hfi_gen2_packet.c').read_text()
    command = (work / 'iris_hfi_gen2_command.c').read_text()
    common = (work / 'iris_hfi_common.h').read_text()
    packet_header = (work / 'iris_hfi_gen2_packet.h').read_text()
    defines = (work / 'iris_hfi_gen2_defines.h').read_text()
    platform = (work / 'iris_platform_common.h').read_text()

    definitions = aggregate_definition(common, 'hfi_packet_port_type')
    definitions += aggregate_definition(common, 'hfi_packet_payload_info')
    definitions += aggregate_definition(common, 'hfi_packet_host_flags')
    definitions += aggregate_definition(packet_header, 'iris_hfi_header')
    definitions += aggregate_definition(packet_header, 'iris_hfi_packet')
    definitions += aggregate_definition(platform, 'ubwc_config_data')

    wanted = re.compile(r'#define (HFI_CMD_(INIT|POWER_COLLAPSE|OPEN|CLOSE|START|STOP|'
                        r'DRAIN|RESUME|PAUSE|SETTINGS_CHANGE)|'
                        r'HFI_PROP_(UBWC_MAX_CHANNELS|UBWC_MAL_LENGTH|UBWC_HBB|'
                        r'UBWC_BANK_SWZL_LEVEL1|UBWC_BANK_SWZL_LEVEL2|UBWC_BANK_SWZL_LEVEL3|'
                        r'UBWC_BANK_SPREADING|IMAGE_VERSION|INTRA_FRAME_POWER_COLLAPSE))\s')
    definitions += ''.join(line + '\n' for line in defines.splitlines() if wanted.match(line))

    functions = ''.join(definition(packet, name) for name in (
        'iris_hfi_gen2_create_header', 'iris_hfi_gen2_create_packet',
        'iris_hfi_gen2_packet_sys_init', 'iris_hfi_gen2_packet_image_version',
        'iris_hfi_gen2_packet_session_command', 'iris_hfi_gen2_packet_session_property',
        'iris_hfi_gen2_packet_sys_interframe_powercollapse',
        'iris_hfi_gen2_packet_sys_pc_prep'))
    functions += ''.join(definition(command, name) for name in (
        'iris_hfi_gen2_get_port', 'iris_hfi_gen2_sys_init',
        'iris_hfi_gen2_sys_image_version', 'iris_hfi_gen2_sys_interframe_powercollapse',
        'iris_hfi_gen2_sys_pc_prep', 'iris_hfi_gen2_session_open',
        'iris_hfi_gen2_session_close', 'iris_hfi_gen2_session_start',
        'iris_hfi_gen2_session_stop', 'iris_hfi_gen2_session_pause',
        'iris_hfi_gen2_session_drain', 'iris_hfi_gen2_session_resume_drc',
        'iris_hfi_gen2_session_resume_drain'))

    source = output / 'actual-hfi-lifecycle.c'
    start = common.index('struct iris_hfi_pending {')
    tracker = common[start:common.index('enum hfi_packet_port_type {', start)]
    source.write_text(PREFIX.replace('/* TRACKER */', tracker) + definitions + SUFFIX + functions + MAIN)
    binary = output / 'actual-hfi-lifecycle'
    compile_fixture(source, binary)
    return binary


def compile_fixture(source, binary):
    result = subprocess.run(['/usr/bin/gcc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2',
                             str(source), '-o', str(binary)], capture_output=True, text=True,
                            timeout=30, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C'})
    if result.returncode:
        raise RuntimeError('HFI lifecycle fixture compilation failed:\n' +
                           result.stdout + result.stderr)


class HfiLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='iris-hfi-lifecycle-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.binary = create_fixture(WORK, Path(cls.temp.name))

    def run_case(self, mode):
        result = subprocess.run([str(self.binary), str(mode)], capture_output=True, text=True,
                                timeout=3)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def mutation_rejected(self, before, after, mode):
        with tempfile.TemporaryDirectory(prefix='iris-hfi-mutation-') as directory:
            path = Path(directory)
            source = (Path(self.temp.name) / 'actual-hfi-lifecycle.c').read_text()
            self.assertEqual(source.count(before), 1)
            target = path / 'mutated.c'
            target.write_text(source.replace(before, after, 1))
            binary = path / 'mutated'
            compile_fixture(target, binary)
            result = subprocess.run([str(binary), str(mode)], capture_output=True, text=True,
                                    timeout=3)
            self.assertNotEqual(result.returncode, 0,
                                'HFI oracle accepted a deliberate regression')

    def test_sys_init_exact_bytes(self):
        self.run_case(0)

    def test_image_version_exact_bytes(self):
        self.run_case(1)

    def test_session_command_payload(self):
        self.run_case(2)

    def test_session_property_payload(self):
        self.run_case(3)

    def test_system_power_sequence(self):
        self.run_case(4)

    def test_session_open_close(self):
        self.run_case(5)

    def test_stream_lifecycle_sequence(self):
        self.run_case(6)

    def test_queue_error_propagation(self):
        self.run_case(7)

    def test_id_wraparound(self):
        self.run_case(8)

    def test_ubwc_payload_variation(self):
        self.run_case(9)

    def test_repeated_sys_init_ids(self):
        self.run_case(10)

    def test_oracle_rejects_header_size_regression(self):
        self.mutation_rejected('hdr->size = sizeof(*hdr);', 'hdr->size = sizeof(u32);', 0)

    def test_oracle_rejects_packet_size_regression(self):
        self.mutation_rejected('pkt->size = pkt_size;', 'pkt->size = sizeof(*pkt);', 2)

    def test_oracle_rejects_missing_packet_count(self):
        self.mutation_rejected('hdr->num_packets++;', 'hdr->num_packets += 0;', 0)

    def test_oracle_rejects_header_accumulation(self):
        self.mutation_rejected('hdr->size += pkt->size;', 'hdr->size += sizeof(*pkt);', 3)

    def test_oracle_rejects_truncated_payload(self):
        self.mutation_rejected('memcpy(&pkt->payload[0], payload, payload_size);',
                               'memcpy(&pkt->payload[0], payload + 1, payload_size - 1);', 2)

    def test_oracle_rejects_arch_regression(self):
        self.mutation_rejected('payload = HFI_VIDEO_ARCH_LX;', 'payload = 0;', 0)

    def test_oracle_rejects_init_flag_regression(self):
        self.mutation_rejected('HFI_HOST_FLAGS_RESPONSE_REQUIRED |\n'
                               '\t\t\t\t    HFI_HOST_FLAGS_INTR_REQUIRED |\n'
                               '\t\t\t\t    HFI_HOST_FLAGS_NON_DISCARDABLE),',
                               'HFI_HOST_FLAGS_NONE),', 0)

    def test_oracle_rejects_open_payload_regression(self):
        self.mutation_rejected('&inst->session_id,', '&inst->domain,', 5)

    def test_oracle_rejects_drain_plane_guard(self):
        self.mutation_rejected('if (!V4L2_TYPE_IS_OUTPUT(plane))',
                               'if (0)', 6)

    def test_oracle_rejects_resume_payload_regression(self):
        self.mutation_rejected('u32 payload = HFI_CMD_SETTINGS_CHANGE;',
                               'u32 payload = HFI_CMD_DRAIN;', 6)


def main():
    global WORK
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--work', type=Path, help='Already patched Iris build directory')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='iris-hfi-sources-') as temporary:
        if args.work:
            WORK = args.work
        else:
            upstream = Path('/var/tmp/linux-src/linux-6.18.18')
            spec = importlib.util.spec_from_file_location('iris_builder', HERE / 'build.py')
            builder = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(builder)
            WORK = Path(temporary)
            builder.prepare_iris(upstream, WORK)
        result = unittest.TextTestRunner(verbosity=2).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(HfiLifecycleTests))
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
