// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/bitfield.h>

#include "iris_hfi_gen2.h"
#include "iris_hfi_gen2_packet.h"
#include "iris_vpu_buffer.h"

#define UNSPECIFIED_COLOR_FORMAT 5
#define NUM_SYS_INIT_PACKETS 8

#define SYS_INIT_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	NUM_SYS_INIT_PACKETS * (sizeof(struct iris_hfi_packet) + sizeof(u32)))

#define SYS_IFPC_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	sizeof(struct iris_hfi_packet) + sizeof(u32))

#define SYS_NO_PAYLOAD_PKT_SIZE (sizeof(struct iris_hfi_header) + \
	sizeof(struct iris_hfi_packet))

static int iris_hfi_gen2_sys_init(struct iris_core *core)
{
	struct iris_hfi_header *hdr;
	struct iris_hfi_packet *packet;
	int ret;

	hdr = kzalloc(SYS_INIT_PKT_SIZE, GFP_KERNEL);
	if (!hdr)
		return -ENOMEM;

	if (core->packet_id > U32_MAX - NUM_SYS_INIT_PACKETS) {
		kfree(hdr);
		return -EOVERFLOW;
	}
	iris_hfi_gen2_packet_sys_init(core, hdr);
	packet = (struct iris_hfi_packet *)((u8 *)hdr + sizeof(*hdr));
	ret = iris_hfi_pending_begin(&core->init_pending, packet->packet_id,
				    HFI_CMD_INIT, HFI_PORT_NONE);
	if (ret) {
		kfree(hdr);
		return ret;
	}
	ret = iris_hfi_queue_cmd_write_locked(core, hdr, hdr->size);

	if (ret)
		iris_hfi_pending_cancel(&core->init_pending);
	kfree(hdr);

	return ret;
}

static int iris_hfi_gen2_sys_image_version(struct iris_core *core)
{
	struct iris_hfi_header *hdr;
	int ret;

	hdr = kzalloc(SYS_NO_PAYLOAD_PKT_SIZE, GFP_KERNEL);
	if (!hdr)
		return -ENOMEM;

	iris_hfi_gen2_packet_image_version(core, hdr);
	ret = iris_hfi_queue_cmd_write_locked(core, hdr, hdr->size);

	kfree(hdr);

	return ret;
}

static int iris_hfi_gen2_sys_interframe_powercollapse(struct iris_core *core)
{
	struct iris_hfi_header *hdr;
	int ret;

	hdr = kzalloc(SYS_IFPC_PKT_SIZE, GFP_KERNEL);
	if (!hdr)
		return -ENOMEM;

	iris_hfi_gen2_packet_sys_interframe_powercollapse(core, hdr);
	ret = iris_hfi_queue_cmd_write_locked(core, hdr, hdr->size);

	kfree(hdr);

	return ret;
}

static int iris_hfi_gen2_sys_pc_prep(struct iris_core *core)
{
	struct iris_hfi_header *hdr;
	int ret;

	hdr = kzalloc(SYS_NO_PAYLOAD_PKT_SIZE, GFP_KERNEL);
	if (!hdr)
		return -ENOMEM;

	iris_hfi_gen2_packet_sys_pc_prep(core, hdr);
	ret = iris_hfi_queue_cmd_write_locked(core, hdr, hdr->size);

	kfree(hdr);

	return ret;
}

static u32 iris_hfi_gen2_get_port(struct iris_inst *inst, u32 plane)
{
	if (inst->domain == DECODER) {
		switch (plane) {
		case V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE:
			return HFI_PORT_BITSTREAM;
		case V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE:
			return HFI_PORT_RAW;
		default:
			return HFI_PORT_NONE;
		}
	} else {
		switch (plane) {
		case V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE:
			return HFI_PORT_RAW;
		case V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE:
			return HFI_PORT_BITSTREAM;
		default:
			return HFI_PORT_NONE;
		}
	}
}

u32 iris_hfi_gen2_get_port_from_buf_type(struct iris_inst *inst,
						enum iris_buffer_type buffer_type)
{
	if (inst->domain == DECODER) {
		switch (buffer_type) {
		case BUF_INPUT:
		case BUF_BIN:
		case BUF_COMV:
		case BUF_NON_COMV:
		case BUF_LINE:
			return HFI_PORT_BITSTREAM;
		case BUF_OUTPUT:
		case BUF_DPB:
			return HFI_PORT_RAW;
		case BUF_PERSIST:
		default:
			return HFI_PORT_NONE;
		}
	} else {
		switch (buffer_type) {
		case BUF_INPUT:
		case BUF_VPSS:
			return HFI_PORT_RAW;
		case BUF_OUTPUT:
		case BUF_BIN:
		case BUF_COMV:
		case BUF_NON_COMV:
		case BUF_LINE:
		case BUF_SCRATCH_2:
			return HFI_PORT_BITSTREAM;
		case BUF_ARP:
		default:
			return HFI_PORT_NONE;
		}
	}
}

static bool iris_input_comv_sent(struct iris_inst *inst, struct iris_hfi_header *header)
{
	struct iris_core *core = inst->core;
	struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));
	u32 count;

	if (!READ_ONCE(core->decode_once) || inst->domain != DECODER ||
	    header->num_packets != 1 ||
	    header->size < sizeof(*header) + sizeof(*packet) + sizeof(u32) ||
	    packet->type != HFI_PROP_COMV_BUFFER_COUNT ||
	    packet->port != HFI_PORT_BITSTREAM || packet->payload_info != HFI_PAYLOAD_U32 ||
	    packet->size < sizeof(*packet) + sizeof(u32))
		return false;
	if (atomic_cmpxchg(&core->input_comv_tx_claim, 0, 1))
		return false;
	memcpy(&count, packet->payload, sizeof(count));
	atomic_set(&core->input_comv_count, (int)count);
	atomic_set(&core->input_comv_id, (int)packet->packet_id);
	smp_wmb();
	atomic_or(BIT(0), &core->input_comv_bits);
	return true;
}

static int iris_hfi_gen2_session_set_property(struct iris_inst *inst, u32 packet_type, u32 flag,
					      u32 plane, u32 payload_type, void *payload,
					      u32 payload_size)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	bool observe;
	int ret;

	iris_hfi_gen2_packet_session_property(inst,
					      packet_type,
					      flag,
					      plane,
					      payload_type,
					      payload,
					      payload_size);

	observe = iris_input_comv_sent(inst, inst_hfi_gen2->packet);
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				      inst_hfi_gen2->packet->size);
	if (observe && !ret)
		atomic_or(BIT(1), &inst->core->input_comv_bits);
	return ret;
}

static int iris_hfi_gen2_set_raw_resolution(struct iris_inst *inst, u32 plane)
{
	u32 resolution = inst->fmt_src->fmt.pix_mp.width << 16 |
		inst->fmt_src->fmt.pix_mp.height;
	u32 port = iris_hfi_gen2_get_port(inst, plane);

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_RAW_RESOLUTION,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_32_PACKED,
						  &resolution,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_bitstream_resolution(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	enum hfi_packet_payload_info payload_type;
	u32 resolution, codec_align;

	if (inst->domain == DECODER) {
		resolution = inst->fmt_src->fmt.pix_mp.width << 16 |
			inst->fmt_src->fmt.pix_mp.height;
		inst_hfi_gen2->src_subcr_params.bitstream_resolution = resolution;
		payload_type = HFI_PAYLOAD_U32;
	} else {
		codec_align = inst->codec == V4L2_PIX_FMT_HEVC ? 32 : 16;
		resolution = ALIGN(inst->fmt_dst->fmt.pix_mp.width, codec_align) << 16 |
			ALIGN(inst->fmt_dst->fmt.pix_mp.height, codec_align);
		inst_hfi_gen2->dst_subcr_params.bitstream_resolution = resolution;
		payload_type = HFI_PAYLOAD_32_PACKED;
	}

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_BITSTREAM_RESOLUTION,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  payload_type,
						  &resolution,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_crop_offsets(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 bottom_offset, right_offset;
	u32 left_offset, top_offset;
	u32 payload[2];

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			bottom_offset = (inst->fmt_src->fmt.pix_mp.height - inst->crop.height);
			right_offset = (inst->fmt_src->fmt.pix_mp.width - inst->crop.width);
			left_offset = inst->crop.left;
			top_offset = inst->crop.top;
		} else {
			bottom_offset = (inst->fmt_dst->fmt.pix_mp.height - inst->compose.height);
			right_offset = (inst->fmt_dst->fmt.pix_mp.width - inst->compose.width);
			left_offset = inst->compose.left;
			top_offset = inst->compose.top;
		}
	} else {
		bottom_offset = (inst->fmt_src->fmt.pix_mp.height - inst->crop.height);
		right_offset = (inst->fmt_src->fmt.pix_mp.width - inst->crop.width);
		left_offset = inst->crop.left;
		top_offset = inst->crop.top;
	}

	payload[0] = FIELD_PREP(GENMASK(31, 16), left_offset) | top_offset;
	payload[1] = FIELD_PREP(GENMASK(31, 16), right_offset) | bottom_offset;
	inst_hfi_gen2->src_subcr_params.crop_offsets[0] = payload[0];
	inst_hfi_gen2->src_subcr_params.crop_offsets[1] = payload[1];

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_CROP_OFFSETS,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_64_PACKED,
						  &payload,
						  sizeof(u64));
}

static int iris_hfi_gen2_set_bit_depth(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 bitdepth = BIT_DEPTH_8;

	inst_hfi_gen2->src_subcr_params.bit_depth = bitdepth;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_LUMA_CHROMA_BIT_DEPTH,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32,
						  &bitdepth,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_coded_frames(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 coded_frames = 0;

	if (inst->fw_caps[CODED_FRAMES].value == CODED_FRAMES_PROGRESSIVE)
		coded_frames = HFI_BITMASK_FRAME_MBS_ONLY_FLAG;
	inst_hfi_gen2->src_subcr_params.coded_frames = coded_frames;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_CODED_FRAMES,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32,
						  &coded_frames,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_min_output_count(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 min_output = inst->buffers[BUF_OUTPUT].min_count;
	u32 port = iris_hfi_gen2_get_port(inst, plane);

	inst_hfi_gen2->src_subcr_params.fw_min_count = min_output;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_BUFFER_FW_MIN_OUTPUT_COUNT,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32,
						  &min_output,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_picture_order_count(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 poc = 0;

	inst_hfi_gen2->src_subcr_params.pic_order_cnt = poc;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_PIC_ORDER_CNT_TYPE,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32,
						  &poc,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_colorspace(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct v4l2_pix_format_mplane *pixmp = &inst->fmt_src->fmt.pix_mp;
	u32 video_signal_type_present_flag = 0, color_info;
	u32 matrix_coeff = HFI_MATRIX_COEFF_RESERVED;
	u32 video_format = UNSPECIFIED_COLOR_FORMAT;
	u32 full_range = V4L2_QUANTIZATION_DEFAULT;
	u32 transfer_char = HFI_TRANSFER_RESERVED;
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 colour_description_present_flag = 0;
	u32 primaries = HFI_PRIMARIES_RESERVED;

	if (pixmp->colorspace != V4L2_COLORSPACE_DEFAULT ||
	    pixmp->ycbcr_enc != V4L2_YCBCR_ENC_DEFAULT ||
	    pixmp->xfer_func != V4L2_XFER_FUNC_DEFAULT) {
		colour_description_present_flag = 1;
		video_signal_type_present_flag = 1;
		primaries = iris_hfi_gen2_get_color_primaries(pixmp->colorspace);
		matrix_coeff = iris_hfi_gen2_get_matrix_coefficients(pixmp->ycbcr_enc);
		transfer_char = iris_hfi_gen2_get_transfer_char(pixmp->xfer_func);
	}

	if (pixmp->quantization != V4L2_QUANTIZATION_DEFAULT) {
		video_signal_type_present_flag = 1;
		full_range = pixmp->quantization == V4L2_QUANTIZATION_FULL_RANGE ? 1 : 0;
	}

	color_info = iris_hfi_gen2_get_color_info(matrix_coeff, transfer_char, primaries,
						  colour_description_present_flag, full_range,
						  video_format, video_signal_type_present_flag);

	inst_hfi_gen2->src_subcr_params.color_info = color_info;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_SIGNAL_COLOR_INFO,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_32_PACKED,
						  &color_info,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_profile(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	u32 profile = 0;

	switch (inst->codec) {
	case V4L2_PIX_FMT_HEVC:
		profile = inst->fw_caps[PROFILE_HEVC].value;
		break;
	case V4L2_PIX_FMT_VP9:
		profile = inst->fw_caps[PROFILE_VP9].value;
		break;
	case V4L2_PIX_FMT_H264:
		profile = inst->fw_caps[PROFILE_H264].value;
		break;
	}

	inst_hfi_gen2->src_subcr_params.profile = profile;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_PROFILE,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32_ENUM,
						  &profile,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_level(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	u32 level = 0;

	switch (inst->codec) {
	case V4L2_PIX_FMT_HEVC:
		level = inst->fw_caps[LEVEL_HEVC].value;
		break;
	case V4L2_PIX_FMT_VP9:
		level = inst->fw_caps[LEVEL_VP9].value;
		break;
	case V4L2_PIX_FMT_H264:
		level = inst->fw_caps[LEVEL_H264].value;
		break;
	}

	inst_hfi_gen2->src_subcr_params.level = level;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_LEVEL,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32_ENUM,
						  &level,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_colorformat(struct iris_inst *inst, u32 plane)
{
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 hfi_colorformat, pixelformat;

	if (inst->core->iris_platform_data->prototype_restricted &&
	    (inst->domain == ENCODER || inst->fmt_dst->fmt.pix_mp.pixelformat != V4L2_PIX_FMT_NV12))
		return -EOPNOTSUPP;

	if (inst->domain == DECODER) {
		pixelformat = inst->fmt_dst->fmt.pix_mp.pixelformat;
		switch (pixelformat) {
		case V4L2_PIX_FMT_NV12:
			hfi_colorformat = HFI_COLOR_FMT_NV12;
			break;
		case V4L2_PIX_FMT_QC08C:
			hfi_colorformat = HFI_COLOR_FMT_NV12_UBWC;
			break;
		case V4L2_PIX_FMT_P010:
			hfi_colorformat = HFI_COLOR_FMT_P010;
			break;
		case V4L2_PIX_FMT_QC10C:
			hfi_colorformat = HFI_COLOR_FMT_TP10_UBWC;
			break;
		default:
			return -EINVAL;
		}
	} else {
		pixelformat = inst->fmt_src->fmt.pix_mp.pixelformat;
		switch (pixelformat) {
		case V4L2_PIX_FMT_NV12:
			hfi_colorformat = HFI_COLOR_FMT_NV12;
			break;
		case V4L2_PIX_FMT_QC08C:
			hfi_colorformat = HFI_COLOR_FMT_NV12_UBWC;
			break;
		default:
			return -EINVAL;
		}
	}

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_COLOR_FORMAT,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32_ENUM,
						  &hfi_colorformat,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_linear_stride_scanline(struct iris_inst *inst, u32 plane)
{
	u32 pixelformat, stride_y, stride_uv, scanline_y, scanline_uv;
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 payload[2];

	if (inst->domain == DECODER) {
		pixelformat = inst->fmt_dst->fmt.pix_mp.pixelformat;
		stride_y = inst->fmt_dst->fmt.pix_mp.plane_fmt[0].bytesperline;
		scanline_y = inst->fmt_dst->fmt.pix_mp.height;
	} else {
		pixelformat = inst->fmt_src->fmt.pix_mp.pixelformat;
		stride_y = ALIGN(inst->fmt_src->fmt.pix_mp.width, 128);
		scanline_y = ALIGN(inst->fmt_src->fmt.pix_mp.height, 32);
	}

	stride_uv = stride_y;
	scanline_uv = ALIGN(DIV_ROUND_UP(scanline_y, 2), 16);

	if (pixelformat != V4L2_PIX_FMT_NV12 &&
	    pixelformat != V4L2_PIX_FMT_P010)
		return 0;

	if (!stride_y || stride_y > 65535 || scanline_y > 65535 || scanline_uv > 65535)
		return -EINVAL;

	payload[0] = stride_y << 16 | scanline_y;
	payload[1] = stride_uv << 16 | scanline_uv;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_LINEAR_STRIDE_SCANLINE,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_64_PACKED,
						  &payload,
						  sizeof(u64));
}

static int iris_hfi_gen2_set_tier(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 port = iris_hfi_gen2_get_port(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	u32 tier = inst->fw_caps[TIER].value;

	inst_hfi_gen2->src_subcr_params.tier = tier;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_TIER,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_U32_ENUM,
						  &tier,
						  sizeof(u32));
}

static int iris_hfi_gen2_set_frame_rate(struct iris_inst *inst, u32 plane)
{
	u32 port = iris_hfi_gen2_get_port(inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
	u32 frame_rate = inst->frame_rate << 16;

	return iris_hfi_gen2_session_set_property(inst,
						  HFI_PROP_FRAME_RATE,
						  HFI_HOST_FLAGS_NONE,
						  port,
						  HFI_PAYLOAD_Q16,
						  &frame_rate,
						  sizeof(u32));
}

static int iris_hfi_gen2_session_set_config_params(struct iris_inst *inst, u32 plane)
{
	const struct iris_platform_data *pdata = inst->core->iris_platform_data;
	u32 config_params_size = 0, i, j;
	const u32 *config_params = NULL;
	int ret;

	static const struct iris_hfi_prop_type_handle prop_type_handle_arr[] = {
		{HFI_PROP_RAW_RESOLUTION,             iris_hfi_gen2_set_raw_resolution         },
		{HFI_PROP_BITSTREAM_RESOLUTION,       iris_hfi_gen2_set_bitstream_resolution   },
		{HFI_PROP_CROP_OFFSETS,               iris_hfi_gen2_set_crop_offsets           },
		{HFI_PROP_CODED_FRAMES,               iris_hfi_gen2_set_coded_frames           },
		{HFI_PROP_LUMA_CHROMA_BIT_DEPTH,      iris_hfi_gen2_set_bit_depth              },
		{HFI_PROP_BUFFER_FW_MIN_OUTPUT_COUNT, iris_hfi_gen2_set_min_output_count       },
		{HFI_PROP_PIC_ORDER_CNT_TYPE,         iris_hfi_gen2_set_picture_order_count    },
		{HFI_PROP_SIGNAL_COLOR_INFO,          iris_hfi_gen2_set_colorspace             },
		{HFI_PROP_PROFILE,                    iris_hfi_gen2_set_profile                },
		{HFI_PROP_LEVEL,                      iris_hfi_gen2_set_level                  },
		{HFI_PROP_COLOR_FORMAT,               iris_hfi_gen2_set_colorformat            },
		{HFI_PROP_LINEAR_STRIDE_SCANLINE,     iris_hfi_gen2_set_linear_stride_scanline },
		{HFI_PROP_TIER,                       iris_hfi_gen2_set_tier                   },
		{HFI_PROP_FRAME_RATE,                 iris_hfi_gen2_set_frame_rate             },
	};

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			if (inst->codec == V4L2_PIX_FMT_H264) {
				config_params = pdata->dec_input_config_params_default;
				config_params_size = pdata->dec_input_config_params_default_size;
			} else if (inst->codec == V4L2_PIX_FMT_HEVC) {
				config_params = pdata->dec_input_config_params_hevc;
				config_params_size = pdata->dec_input_config_params_hevc_size;
			} else if (inst->codec == V4L2_PIX_FMT_VP9) {
				config_params = pdata->dec_input_config_params_vp9;
				config_params_size = pdata->dec_input_config_params_vp9_size;
			} else {
				return -EINVAL;
			}
		} else {
			config_params = pdata->dec_output_config_params;
			config_params_size = pdata->dec_output_config_params_size;
		}
	} else {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			config_params = pdata->enc_input_config_params;
			config_params_size = pdata->enc_input_config_params_size;
		} else {
			config_params = pdata->enc_output_config_params;
			config_params_size = pdata->enc_output_config_params_size;
		}
	}

	if (!config_params || !config_params_size)
		return -EINVAL;

	for (i = 0; i < config_params_size; i++) {
		for (j = 0; j < ARRAY_SIZE(prop_type_handle_arr); j++) {
			if (prop_type_handle_arr[j].type == config_params[i]) {
				ret = prop_type_handle_arr[j].handle(inst, plane);
				if (ret)
					return ret;
				break;
			}
		}
	}

	return 0;
}

static int iris_hfi_gen2_session_set_codec(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 codec = 0;

	switch (inst->codec) {
	case V4L2_PIX_FMT_H264:
		if (inst->domain == ENCODER)
			codec = HFI_CODEC_ENCODE_AVC;
		else
			codec = HFI_CODEC_DECODE_AVC;
		break;
	case V4L2_PIX_FMT_HEVC:
		if (inst->domain == ENCODER)
			codec = HFI_CODEC_ENCODE_HEVC;
		else
			codec = HFI_CODEC_DECODE_HEVC;
		break;
	case V4L2_PIX_FMT_VP9:
		codec = HFI_CODEC_DECODE_VP9;
	}

	iris_hfi_gen2_packet_session_property(inst,
					      HFI_PROP_CODEC,
					      HFI_HOST_FLAGS_NONE,
					      HFI_PORT_NONE,
					      HFI_PAYLOAD_U32_ENUM,
					      &codec,
					      sizeof(u32));

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_session_set_default_header(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 default_header = false;

	iris_hfi_gen2_packet_session_property(inst,
					      HFI_PROP_DEC_DEFAULT_HEADER,
					      HFI_HOST_FLAGS_NONE,
					      HFI_PORT_BITSTREAM,
					      HFI_PAYLOAD_U32,
					      &default_header,
					      sizeof(u32));

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_wait_async(struct iris_inst *inst,
				    struct iris_hfi_pending *pending,
				    struct completion *done, int *status)
{
	u32 timeout = inst->core->iris_platform_data->hw_response_timeout;
	unsigned long completed;
	int ret;

	/* Match/error publication uses inst->lock; do not hold it while waiting. */
	mutex_unlock(&inst->lock);
	completed = wait_for_completion_timeout(done, msecs_to_jiffies(timeout));
	mutex_lock(&inst->lock);
	iris_hfi_pending_cancel(pending);
	if (!completed) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		if (*status == -EINPROGRESS || !*status)
			*status = -ETIMEDOUT;
		iris_vb2_queue_error(inst);
		return *status;
	}
	ret = *status;
	if (ret == -EINPROGRESS) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		*status = -EPROTO;
		iris_vb2_queue_error(inst);
		return -EPROTO;
	}
	if (inst->state == IRIS_INST_ERROR || inst->core->state != IRIS_CORE_INIT ||
	    inst->response_closing)
		return ret < 0 ? ret : -EIO;
	return ret;
}

static int iris_hfi_gen2_session_open(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_packet *packet;
	int ret;

	if (inst->state != IRIS_INST_DEINIT)
		return -EALREADY;

	inst_hfi_gen2->ipsc_properties_set = false;
	inst_hfi_gen2->opsc_properties_set = false;

	if (iris_hfi_pending_active(&inst_hfi_gen2->open_pending))
		return -EBUSY;
	reinit_completion(&inst_hfi_gen2->open_done);
	inst_hfi_gen2->open_status = -EINPROGRESS;

	inst_hfi_gen2->packet = kzalloc(4096, GFP_KERNEL);
	if (!inst_hfi_gen2->packet)
		return -ENOMEM;

	if (inst->core->packet_id == U32_MAX) {
		ret = -EOVERFLOW;
		goto fail_free_packet;
	}

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_OPEN,
					     HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED,
					     HFI_PORT_NONE,
					     0,
					     HFI_PAYLOAD_U32,
					     &inst->session_id,
					     sizeof(u32));

	packet = (struct iris_hfi_packet *)((u8 *)inst_hfi_gen2->packet + sizeof(struct iris_hfi_header));
	ret = iris_hfi_pending_begin(&inst_hfi_gen2->open_pending, packet->packet_id, packet->type, packet->port);
	if (ret)
		goto fail_free_packet;
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				       inst_hfi_gen2->packet->size);
	if (ret)
		goto fail_free_packet;

	ret = iris_hfi_gen2_session_set_codec(inst);
	if (ret)
		goto fail_free_packet;

	if (inst->domain == DECODER) {
		ret = iris_hfi_gen2_session_set_default_header(inst);
		if (ret)
			goto fail_free_packet;
	}

	ret = iris_hfi_gen2_wait_async(inst, &inst_hfi_gen2->open_pending,
				       &inst_hfi_gen2->open_done, &inst_hfi_gen2->open_status);
	if (ret)
		goto fail_free_packet;
	return 0;

fail_free_packet:
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	iris_hfi_pending_cancel(&inst_hfi_gen2->open_pending);
	kfree(inst_hfi_gen2->packet);
	inst_hfi_gen2->packet = NULL;

	return ret;
}

static int iris_hfi_gen2_session_close(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_packet *packet;
	int ret;

	if (!inst_hfi_gen2->packet)
		return -EINVAL;
	if (iris_hfi_pending_active(&inst_hfi_gen2->open_pending) ||
	    iris_hfi_pending_active(&inst_hfi_gen2->start_pending[0]) ||
	    iris_hfi_pending_active(&inst_hfi_gen2->start_pending[1]))
		return -EBUSY;

	if (inst_hfi_gen2->packet->size > 4096 || inst->core->packet_id == U32_MAX)
		return -EOVERFLOW;
	if (iris_hfi_pending_active(&inst->control_pending))
		return -EBUSY;

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_CLOSE,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED |
					     HFI_HOST_FLAGS_NON_DISCARDABLE),
					     HFI_PORT_NONE,
					     inst->session_id,
					     HFI_PAYLOAD_NONE,
					     NULL,
					     0);

	packet = (struct iris_hfi_packet *)((u8 *)inst_hfi_gen2->packet +
					 sizeof(struct iris_hfi_header));
	ret = iris_hfi_pending_begin(&inst->control_pending, packet->packet_id,
				    packet->type, packet->port);
	if (ret)
		return ret;
	if (inst->core->native_repeat) {
		inst->repeat_close_header = inst_hfi_gen2->packet->header_id;
		inst->repeat_close_packet = packet->packet_id;
	}
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				       inst_hfi_gen2->packet->size);

	if (ret)
		iris_hfi_pending_cancel(&inst->control_pending);
	if (!inst->core->native_repeat) {
		kfree(inst_hfi_gen2->packet);
		inst_hfi_gen2->packet = NULL;
	}

	return ret;
}

static int iris_hfi_gen2_session_subscribe_mode(struct iris_inst *inst,
						u32 cmd, u32 plane, u32 payload_type,
						void *payload, u32 payload_size)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);

	iris_hfi_gen2_packet_session_command(inst,
					     cmd,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     payload_type,
					     payload,
					     payload_size);

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_subscribe_change_param(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct hfi_subscription_params subsc_params;
	u32 prop_type, payload_size, payload_type;
	struct iris_core *core = inst->core;
	const u32 *change_param = NULL;
	u32 change_param_size = 0;
	u32 payload[32] = {0};
	u32 hfi_port = 0, i;
	int ret;

	if (inst->domain == ENCODER)
		return 0;

	if ((V4L2_TYPE_IS_OUTPUT(plane) && inst_hfi_gen2->ipsc_properties_set) ||
	    (V4L2_TYPE_IS_CAPTURE(plane) && inst_hfi_gen2->opsc_properties_set)) {
		dev_err(core->dev, "invalid plane\n");
		return 0;
	}

	switch (inst->codec) {
	case V4L2_PIX_FMT_H264:
		change_param = core->iris_platform_data->dec_input_config_params_default;
		change_param_size =
			core->iris_platform_data->dec_input_config_params_default_size;
		break;
	case V4L2_PIX_FMT_HEVC:
		change_param = core->iris_platform_data->dec_input_config_params_hevc;
		change_param_size =
			core->iris_platform_data->dec_input_config_params_hevc_size;
		break;
	case V4L2_PIX_FMT_VP9:
		change_param = core->iris_platform_data->dec_input_config_params_vp9;
		change_param_size =
			core->iris_platform_data->dec_input_config_params_vp9_size;
		break;
	}

	payload[0] = HFI_MODE_PORT_SETTINGS_CHANGE;

	for (i = 0; i < change_param_size; i++)
		payload[i + 1] = change_param[i];

	ret = iris_hfi_gen2_session_subscribe_mode(inst,
						   HFI_CMD_SUBSCRIBE_MODE,
						   plane,
						   HFI_PAYLOAD_U32_ARRAY,
						   &payload[0],
						   ((change_param_size + 1) * sizeof(u32)));
	if (ret)
		return ret;

	if (V4L2_TYPE_IS_OUTPUT(plane)) {
		inst_hfi_gen2->ipsc_properties_set = true;
	} else {
		hfi_port = iris_hfi_gen2_get_port(inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
		memcpy(&inst_hfi_gen2->dst_subcr_params,
		       &inst_hfi_gen2->src_subcr_params,
		       sizeof(inst_hfi_gen2->src_subcr_params));
		subsc_params = inst_hfi_gen2->dst_subcr_params;
		for (i = 0; i < change_param_size; i++) {
			payload[0] = 0;
			payload[1] = 0;
			payload_size = 0;
			payload_type = 0;
			prop_type = change_param[i];
			switch (prop_type) {
			case HFI_PROP_BITSTREAM_RESOLUTION:
				payload[0] = subsc_params.bitstream_resolution;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_CROP_OFFSETS:
				payload[0] = subsc_params.crop_offsets[0];
				payload[1] = subsc_params.crop_offsets[1];
				payload_size = sizeof(u64);
				payload_type = HFI_PAYLOAD_64_PACKED;
				break;
			case HFI_PROP_CODED_FRAMES:
				payload[0] = subsc_params.coded_frames;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_LUMA_CHROMA_BIT_DEPTH:
				payload[0] = subsc_params.bit_depth;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_BUFFER_FW_MIN_OUTPUT_COUNT:
				payload[0] = subsc_params.fw_min_count;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_PIC_ORDER_CNT_TYPE:
				payload[0] = subsc_params.pic_order_cnt;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_SIGNAL_COLOR_INFO:
				payload[0] = subsc_params.color_info;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_PROFILE:
				payload[0] = subsc_params.profile;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_LEVEL:
				payload[0] = subsc_params.level;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			case HFI_PROP_TIER:
				payload[0] = subsc_params.tier;
				payload_size = sizeof(u32);
				payload_type = HFI_PAYLOAD_U32;
				break;
			default:
				prop_type = 0;
				ret = -EINVAL;
				break;
			}
			if (prop_type) {
				ret = iris_hfi_gen2_session_set_property(inst,
									 prop_type,
									 HFI_HOST_FLAGS_NONE,
									 hfi_port,
									 payload_type,
									 &payload,
									 payload_size);
				if (ret)
					return ret;
			}
		}
		inst_hfi_gen2->opsc_properties_set = true;
	}

	return 0;
}

static int iris_hfi_gen2_subscribe_property(struct iris_inst *inst, u32 plane)
{
	struct iris_core *core = inst->core;
	u32 subscribe_prop_size = 0, i;
	const u32 *subcribe_prop = NULL;
	u32 payload[32] = {0};

	payload[0] = HFI_MODE_PROPERTY;

	if (inst->domain == ENCODER)
		return 0;

	if (V4L2_TYPE_IS_OUTPUT(plane)) {
		subscribe_prop_size = core->iris_platform_data->dec_input_prop_size;
		subcribe_prop = core->iris_platform_data->dec_input_prop;
	} else {
		switch (inst->codec) {
		case V4L2_PIX_FMT_H264:
			subcribe_prop = core->iris_platform_data->dec_output_prop_avc;
			subscribe_prop_size =
				core->iris_platform_data->dec_output_prop_avc_size;
			break;
		case V4L2_PIX_FMT_HEVC:
			subcribe_prop = core->iris_platform_data->dec_output_prop_hevc;
			subscribe_prop_size =
				core->iris_platform_data->dec_output_prop_hevc_size;
			break;
		case V4L2_PIX_FMT_VP9:
			subcribe_prop = core->iris_platform_data->dec_output_prop_vp9;
			subscribe_prop_size =
				core->iris_platform_data->dec_output_prop_vp9_size;
			break;
		}
	}

	for (i = 0; i < subscribe_prop_size; i++)
		payload[i + 1] = subcribe_prop[i];

	return iris_hfi_gen2_session_subscribe_mode(inst,
						    HFI_CMD_SUBSCRIBE_MODE,
						    plane,
						    HFI_PAYLOAD_U32_ARRAY,
						    &payload[0],
						    (subscribe_prop_size + 1) * sizeof(u32));
}

static int iris_hfi_gen2_session_start(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_packet *packet;
	u32 port = iris_hfi_gen2_get_port(inst, plane);
	u32 index;
	int ret = 0;

	if (!inst_hfi_gen2->packet)
		return -EINVAL;
	if (inst->state == IRIS_INST_ERROR || inst->core->state != IRIS_CORE_INIT || inst->response_closing)
		return -EIO;
	if (port != HFI_PORT_BITSTREAM && port != HFI_PORT_RAW)
		return -EINVAL;
	index = port == HFI_PORT_RAW;
	if (iris_hfi_pending_active(&inst_hfi_gen2->start_pending[index]))
		return -EBUSY;

	reinit_completion(&inst_hfi_gen2->start_done[index]);
	inst_hfi_gen2->start_status[index] = -EINPROGRESS;

	ret = iris_hfi_gen2_subscribe_change_param(inst, plane);
	if (ret)
		return ret;

	ret = iris_hfi_gen2_subscribe_property(inst, plane);
	if (ret)
		return ret;

	if (inst->core->packet_id == U32_MAX)
		return -EOVERFLOW;

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_START,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     HFI_PAYLOAD_NONE,
					     NULL,
					     0);

	packet = (struct iris_hfi_packet *)((u8 *)inst_hfi_gen2->packet + sizeof(struct iris_hfi_header));
	ret = iris_hfi_pending_begin(&inst_hfi_gen2->start_pending[index], packet->packet_id, packet->type, packet->port);
	if (ret)
		return ret;
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
	if (ret) {
		iris_hfi_pending_cancel(&inst_hfi_gen2->start_pending[index]);
		return ret;
	}
	return iris_hfi_gen2_wait_async(inst, &inst_hfi_gen2->start_pending[index],
				       &inst_hfi_gen2->start_done[index],
				       &inst_hfi_gen2->start_status[index]);
}

static void iris_stop_observer_stop_request(struct iris_inst *inst,
				    struct iris_hfi_header *header, int queue_rc);

static int iris_hfi_gen2_session_stop(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_packet *packet;
	int ret = 0;

	if (!inst_hfi_gen2->packet)
		return -EINVAL;
	if (iris_hfi_pending_active(&inst_hfi_gen2->open_pending) ||
	    iris_hfi_pending_active(&inst_hfi_gen2->start_pending[0]) ||
	    iris_hfi_pending_active(&inst_hfi_gen2->start_pending[1]))
		return -EBUSY;

	if (inst_hfi_gen2->packet->size > 4096 || inst->core->packet_id == U32_MAX)
		return -EOVERFLOW;
	if (iris_hfi_pending_active(&inst->control_pending))
		return -EBUSY;

	reinit_completion(&inst->completion);

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_STOP,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED |
					     HFI_HOST_FLAGS_NON_DISCARDABLE),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     HFI_PAYLOAD_NONE,
					     NULL,
					     0);

	packet = (struct iris_hfi_packet *)((u8 *)inst_hfi_gen2->packet +
					 sizeof(struct iris_hfi_header));
	ret = iris_hfi_pending_begin(&inst->control_pending, packet->packet_id,
				    packet->type, packet->port);
	if (ret)
		return ret;
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				       inst_hfi_gen2->packet->size);
	iris_stop_observer_stop_request(inst, inst_hfi_gen2->packet, ret);
	if (ret) {
		iris_hfi_pending_cancel(&inst->control_pending);
		return ret;
	}

	return iris_wait_for_session_response(inst, false);
}

static int iris_hfi_gen2_session_pause(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_PAUSE,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     HFI_PAYLOAD_NONE,
					     NULL,
					     0);

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_session_resume_drc(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 payload = HFI_CMD_SETTINGS_CHANGE;
	bool bare = inst->core->iris_platform_data->no_opb;

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_RESUME,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     bare ? HFI_PAYLOAD_NONE : HFI_PAYLOAD_U32,
					     bare ? NULL : &payload,
					     bare ? 0 : sizeof(u32));

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_session_resume_drain(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 payload = HFI_CMD_DRAIN;

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_RESUME,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     HFI_PAYLOAD_U32,
					     &payload,
					     sizeof(u32));

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static int iris_hfi_gen2_session_drain(struct iris_inst *inst, u32 plane)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);

	if (!V4L2_TYPE_IS_OUTPUT(plane))
		return 0;

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_DRAIN,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED |
					     HFI_HOST_FLAGS_NON_DISCARDABLE),
					     iris_hfi_gen2_get_port(inst, plane),
					     inst->session_id,
					     HFI_PAYLOAD_NONE,
					     NULL,
					     0);

	return iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
					inst_hfi_gen2->packet->size);
}

static u32 iris_hfi_gen2_buf_type_from_driver(u32 domain, enum iris_buffer_type buffer_type)
{
	switch (buffer_type) {
	case BUF_INPUT:
		if (domain == DECODER)
			return HFI_BUFFER_BITSTREAM;
		else
			return HFI_BUFFER_RAW;
	case BUF_OUTPUT:
		if (domain == DECODER)
			return HFI_BUFFER_RAW;
		else
			return HFI_BUFFER_BITSTREAM;
	case BUF_BIN:
		return HFI_BUFFER_BIN;
	case BUF_COMV:
		return HFI_BUFFER_COMV;
	case BUF_NON_COMV:
		return HFI_BUFFER_NON_COMV;
	case BUF_LINE:
		return HFI_BUFFER_LINE;
	case BUF_DPB:
	case BUF_SCRATCH_2:
		return HFI_BUFFER_DPB;
	case BUF_PERSIST:
		return HFI_BUFFER_PERSIST;
	case BUF_ARP:
		return HFI_BUFFER_ARP;
	case BUF_VPSS:
		return HFI_BUFFER_VPSS;
	default:
		return 0;
	}
}

static int iris_set_num_comv(struct iris_inst *inst, struct iris_buffer *buffer)
{
	struct platform_inst_caps *caps;
	struct iris_core *core = inst->core;
	u32 num_comv;

	caps = core->iris_platform_data->inst_caps;
	num_comv = caps->num_comv;
	if (inst->domain == DECODER && core->iris_platform_data == &sm8450_data) {
		struct iris_buffer *allocated;
		u32 required;
		bool owned = false;
		int count = iris_sm8450_comv_count(inst);

		if (count < 0)
			return count;
		/* Validate this actual allocation, not mutable table size or another
		 * generation's buffer. An increased count/resolution needs new backing.
		 */
		list_for_each_entry(allocated, &inst->buffers[BUF_COMV].list, list) {
			if (allocated == buffer) {
				owned = true;
				break;
			}
		}
		if (!owned || !buffer->kvaddr)
			return -EINVAL;
		required = core->iris_platform_data->get_vpu_buffer_size(inst, BUF_COMV);
		if (!required)
			return -EINVAL;
		if (buffer->buffer_size < required)
			return -ENOSPC;
		num_comv = count;
	}

	/* This isolated firmware explicitly rejects COMV_COUNT; compatibility control only. */
	if (inst->domain == DECODER && core->iris_platform_data == &sm8450_data &&
	    READ_ONCE(core->decode_once))
		return 0;
	return core->hfi_ops->session_set_property(inst,
						   HFI_PROP_COMV_BUFFER_COUNT,
						   HFI_HOST_FLAGS_NONE,
						   HFI_PORT_BITSTREAM,
						   HFI_PAYLOAD_U32,
						   &num_comv, sizeof(u32));
}

static void iris_hfi_gen2_get_buffer(u32 domain, struct iris_buffer *buffer,
				     struct iris_hfi_buffer *buf, bool cb_flag)
{
	memset(buf, 0, sizeof(*buf));
	buf->type = iris_hfi_gen2_buf_type_from_driver(domain, buffer->type);
	buf->index = buffer->index;
	buf->base_address = buffer->device_addr;
	buf->addr_offset = 0;
	buf->buffer_size = buffer->buffer_size;

	if (domain == DECODER && buffer->type == BUF_INPUT)
		buf->buffer_size = ALIGN(buffer->buffer_size, 256);
	buf->data_offset = buffer->data_offset;
	buf->data_size = buffer->data_size;
	if (buffer->attr & BUF_ATTR_PENDING_RELEASE)
		buf->flags |= HFI_BUF_HOST_FLAG_RELEASE;
	if (cb_flag)
		buf->flags |= HFI_BUF_HOST_FLAGS_CB_NON_SECURE;
	buf->timestamp = buffer->timestamp;
}

static void iris_input_proof_serialized(struct iris_inst *inst,
					struct iris_buffer *buffer,
					struct iris_hfi_header *header)
{
	struct iris_core *core = inst->core;
	struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));
	struct iris_hfi_buffer *hfi = (void *)((u8 *)packet + sizeof(*packet));
	struct iris_input_proof *proof;
	u64 mask;
	u32 ok = 0;

	if (!READ_ONCE(core->decode_once) || inst->domain != DECODER)
		return;
	if (buffer->type > BUF_OUTPUT && buffer->type < BUF_TYPE_MAX) {
		atomic_or(BIT(buffer->type), &core->input_internal_mask);
		return;
	}
	if (buffer->type != BUF_INPUT || buffer->index >= 3)
		return;
	proof = &core->input_proof[buffer->index];
	if (atomic_cmpxchg(&proof->hfi_attempted, 0, 1))
		return;
	/* Observe the actual serialized packet, never read input DMA after submit. */
	if (header->num_packets != 1 ||
	    header->size != sizeof(*header) + sizeof(*packet) + sizeof(*hfi) ||
	    packet->type != HFI_CMD_BUFFER || packet->payload_info != HFI_PAYLOAD_STRUCTURE ||
	    packet->size != sizeof(*packet) + sizeof(*hfi))
		return;
	mask = core->iris_platform_data->dma_mask;
	if (hfi->base_address)
		ok |= BIT(0);
	if (hfi->base_address && hfi->buffer_size && hfi->base_address <= mask &&
	    hfi->buffer_size - 1 <= mask - hfi->base_address)
		ok |= BIT(1);
	if (atomic_read(&proof->valid) & BIT(0)) {
		smp_rmb();
		if (hfi->data_size == READ_ONCE(proof->cpu_bytes) &&
		    hfi->data_offset == READ_ONCE(proof->cpu_offset) &&
		    hfi->buffer_size == READ_ONCE(proof->cpu_alloc))
			ok |= BIT(2);
	}
	if (!hfi->addr_offset && hfi->index == buffer->index)
		ok |= BIT(3);
	WRITE_ONCE(proof->bytes, hfi->data_size);
	WRITE_ONCE(proof->offset, hfi->data_offset);
	WRITE_ONCE(proof->alloc, hfi->buffer_size);
	WRITE_ONCE(proof->kind, hfi->type);
	WRITE_ONCE(proof->port, packet->port);
	WRITE_ONCE(proof->flags, hfi->flags);
	WRITE_ONCE(proof->addr_ok, ok);
	smp_wmb();
	atomic_or(BIT(1), &proof->valid);
}



static void iris_stop_observer_submit(struct iris_inst *inst, struct iris_buffer *buffer,
	    struct iris_hfi_header *header, int queue_rc)
{
	struct iris_stop_app *app;
	struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));
	u32 plane;

	if (!READ_ONCE(inst->core->stop_observer_enabled) || inst->domain != DECODER ||
		    (buffer->type != BUF_INPUT && buffer->type != BUF_OUTPUT) || buffer->index >= VIDEO_MAX_FRAME)
	return;
	lockdep_assert_held(&inst->lock);
	plane = buffer->type == BUF_OUTPUT;
	app = &inst->stop_app[plane][buffer->index];
	if (!app->valid) {
		app->header_id = header->header_id;
		app->packet_id = packet->packet_id;
		app->addr_lo = (u32)buffer->device_addr;
		app->addr_hi = (u32)((u64)buffer->device_addr >> 32);
		app->size = buffer->buffer_size;
		app->valid = 1;
	}
	app->queue_rc = queue_rc;
	app->submissions++;
}

static void iris_stop_observer_stop_request(struct iris_inst *inst,
	    struct iris_hfi_header *header, int queue_rc)
{
	struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));
	struct iris_stop_transaction *transaction;

	if (inst->core->native_repeat) {
		struct iris_stop_transaction *tx = &inst->repeat_stop[packet->port == HFI_PORT_RAW];

		tx->request_header = header->header_id;
		tx->request_packet = packet->packet_id;
		tx->port = packet->port;
		tx->queue_rc = queue_rc;
	}
	if (!inst->core->stop_observer_enabled || !inst->stop_attempted)
		return;
	transaction = &inst->core->stop_evidence.transaction[packet->port == HFI_PORT_RAW];
	transaction->request_header = header->header_id;
	transaction->request_packet = packet->packet_id;
	transaction->port = packet->port;
	transaction->queue_rc = queue_rc;
}

static void iris_release_observer_first_submit(struct iris_inst *inst,
	struct iris_buffer *buffer, struct iris_hfi_header *header, int queue_rc)
{
	struct iris_hfi_packet *pkt = (void *)((u8 *)header + sizeof(*header));

	if (!READ_ONCE(inst->core->release_observer_enabled) ||
	    !READ_ONCE(inst->core->decode_once) || inst->domain != DECODER ||
	    buffer->type <= BUF_OUTPUT || buffer->type >= BUF_TYPE_MAX ||
	    buffer->observer_first_valid)
		return;
	lockdep_assert_held(&inst->lock);
	buffer->observer_first_header_id = header->header_id;
	buffer->observer_first_packet_id = pkt->packet_id;
	buffer->observer_first_rc = queue_rc;
	/* Instance lock protects this immutable per-allocation first-attempt value. */
	buffer->observer_first_valid = true;
}

static void iris_release_observer_request(struct iris_inst *inst,
	struct iris_buffer *buffer, struct iris_hfi_header *header, int queue_rc)
{
	struct iris_hfi_packet *pkt = (void *)((u8 *)header + sizeof(*header));
	struct iris_release_observer_request value = {};
	int index = iris_release_observer_index(buffer->type);

	if (!READ_ONCE(inst->core->release_observer_enabled) ||
	    !READ_ONCE(inst->core->decode_once) || inst->domain != DECODER || index < 0 ||
	    !inst->decode_source_change_seen || !(inst->sub_state & IRIS_INST_SUB_FIRST_IPSC) ||
	    inst->state != IRIS_INST_INPUT_STREAMING)
		return;
	lockdep_assert_held(&inst->lock);
	value.session_id = header->session_id;
	value.header_id = header->header_id;
	value.packet_id = pkt->packet_id;
	value.type = pkt->type;
	value.port = pkt->port;
	value.flags = pkt->flags;
	value.index = buffer->index;
	value.addr_lo = (u32)buffer->device_addr;
	value.addr_hi = (u32)((u64)buffer->device_addr >> 32);
	value.size = buffer->buffer_size;
	value.first_header_id = buffer->observer_first_header_id;
	value.first_packet_id = buffer->observer_first_packet_id;
	value.first_rc = buffer->observer_first_rc;
	value.first_valid = buffer->observer_first_valid;
	value.queue_rc = queue_rc;
	iris_release_observer_publish_request(&inst->core->release_observer[index], &value);
}

static int iris_hfi_gen2_session_queue_buffer(struct iris_inst *inst, struct iris_buffer *buffer)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_buffer hfi_buffer;
	u32 port;
	int ret;

	if (inst->core->native_repeat && (inst->core->repeat_id_exhausted ||
	    inst->core->header_id == U32_MAX || inst->core->packet_id == U32_MAX))
		return -EOVERFLOW;

	if (inst->core->native_repeat) {
		struct iris_repeat_owner *owner;

		if (buffer->type <= BUF_OUTPUT) {
			if (buffer->index >= VIDEO_MAX_FRAME)
				return -EINVAL;
			owner = &inst->repeat_app[buffer->type == BUF_OUTPUT][buffer->index];
		} else {
			owner = buffer->repeat_owner;
		}
		if (!owner || owner->fw_owned || owner->unknown || owner->readonly ||
		    owner->generation == U32_MAX)
			return -EBUSY;
	}
	if (inst->stop_submit_blocked)
		return -ESHUTDOWN;
	iris_hfi_gen2_get_buffer(inst->domain, buffer, &hfi_buffer,
				 !inst->core->iris_platform_data->no_opb);
	if (buffer->type == BUF_COMV) {
		ret = iris_set_num_comv(inst, buffer);
		if (ret)
			return ret;
	}

	port = iris_hfi_gen2_get_port_from_buf_type(inst, buffer->type);
	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_BUFFER,
					     HFI_HOST_FLAGS_INTR_REQUIRED,
					     port,
					     inst->session_id,
					     HFI_PAYLOAD_STRUCTURE,
					     &hfi_buffer,
					     sizeof(hfi_buffer));

	iris_input_proof_serialized(inst, buffer, inst_hfi_gen2->packet);
	if (READ_ONCE(inst->core->decode_once) && inst->domain == DECODER &&
	    buffer->type == BUF_INPUT)
		atomic_inc(&inst->core->decode_progress_input_attempt);
	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				      inst_hfi_gen2->packet->size);
	if (inst->core->native_repeat) {
		struct iris_hfi_header *header = inst_hfi_gen2->packet;
		struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));
		struct iris_repeat_owner *owner = buffer->type <= BUF_OUTPUT ?
			&inst->repeat_app[buffer->type == BUF_OUTPUT][buffer->index] : buffer->repeat_owner;

		/* inst->lock excludes response handling until this actual write is recorded. */
		lockdep_assert_held(&inst->lock);
		ret = iris_repeat_owner_submit(owner, header->header_id, packet->packet_id,
			hfi_buffer.type, packet->port, hfi_buffer.base_address, hfi_buffer.buffer_size, ret);
		if (ret) {
			owner->unknown = true;
			inst->stop_submit_blocked = true;
		}
		if (!ret)
			buffer->attr &= ~(BUF_ATTR_RETURNED | BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE);
	}
	iris_stop_observer_submit(inst, buffer, inst_hfi_gen2->packet, ret);
	iris_release_observer_first_submit(inst, buffer, inst_hfi_gen2->packet, ret);
	if (READ_ONCE(inst->core->decode_once) && inst->domain == DECODER &&
	    buffer->type == BUF_INPUT) {
		/* Host queue-write success only, never firmware consumption/DMA proof. */
		if (!ret)
			atomic_inc(&inst->core->decode_progress_input_ok);
		else
			atomic_cmpxchg(&inst->core->decode_progress_input_first_error, 0, ret);
		atomic_set(&inst->core->decode_progress_session_state, READ_ONCE(inst->state));
	}
	return ret;
}

static int iris_hfi_gen2_session_release_buffer(struct iris_inst *inst, struct iris_buffer *buffer)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_buffer hfi_buffer;
	u32 port;
	int ret;

	if (inst->core->native_repeat && (inst->core->repeat_id_exhausted ||
	    inst->core->header_id == U32_MAX || inst->core->packet_id == U32_MAX))
		return -EOVERFLOW;

	if (inst->core->native_repeat && (!buffer->repeat_owner ||
	    !buffer->repeat_owner->fw_owned || buffer->repeat_owner->unknown ||
	    buffer->repeat_owner->release_requested))
		return -EBUSY;
	if (!inst_hfi_gen2->packet || inst->response_closing ||
	    inst->state == IRIS_INST_ERROR || inst->core->state != IRIS_CORE_INIT)
		return -EIO;
	if (buffer->type <= BUF_OUTPUT || buffer->type >= BUF_TYPE_MAX ||
	    !iris_hfi_gen2_buf_type_from_driver(inst->domain, buffer->type) ||
	    (buffer->type == BUF_VPSS && inst->domain != ENCODER) ||
	    !(buffer->attr & BUF_ATTR_QUEUED) ||
	    buffer->attr & (BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE | BUF_ATTR_PENDING_RELEASE) ||
	    !buffer->device_addr || !buffer->buffer_size || buffer->buffer_size > U32_MAX)
		return -EINVAL;

	iris_hfi_gen2_get_buffer(inst->domain, buffer, &hfi_buffer,
				 !inst->core->iris_platform_data->no_opb);
	hfi_buffer.flags |= HFI_BUF_HOST_FLAG_RELEASE;
	port = iris_hfi_gen2_get_port_from_buf_type(inst, buffer->type);

	iris_hfi_gen2_packet_session_command(inst,
					     HFI_CMD_BUFFER,
					     (HFI_HOST_FLAGS_RESPONSE_REQUIRED |
					     HFI_HOST_FLAGS_INTR_REQUIRED),
					     port,
					     inst->session_id,
					     HFI_PAYLOAD_STRUCTURE,
					     &hfi_buffer,
					     sizeof(hfi_buffer));

	ret = iris_hfi_queue_cmd_write(inst->core, inst_hfi_gen2->packet,
				      inst_hfi_gen2->packet->size);
	if (inst->core->native_repeat) {
		struct iris_repeat_owner *owner = buffer->repeat_owner;
		struct iris_hfi_header *header = inst_hfi_gen2->packet;
		struct iris_hfi_packet *packet = (void *)((u8 *)header + sizeof(*header));

		lockdep_assert_held(&inst->lock);
		if (ret) {
			owner->unknown = true;
			inst->stop_submit_blocked = true;
		} else {
			owner->request_header = header->header_id;
			owner->request_packet = packet->packet_id;
			owner->request_generation = owner->generation;
			owner->release_requested = true;
		}
	}
	iris_release_observer_request(inst, buffer, inst_hfi_gen2->packet, ret);
	return ret;
}

static const struct iris_hfi_command_ops iris_hfi_gen2_command_ops = {
	.sys_init = iris_hfi_gen2_sys_init,
	.sys_image_version = iris_hfi_gen2_sys_image_version,
	.sys_interframe_powercollapse = iris_hfi_gen2_sys_interframe_powercollapse,
	.sys_pc_prep = iris_hfi_gen2_sys_pc_prep,
	.session_open = iris_hfi_gen2_session_open,
	.session_set_config_params = iris_hfi_gen2_session_set_config_params,
	.session_set_property = iris_hfi_gen2_session_set_property,
	.session_start = iris_hfi_gen2_session_start,
	.session_queue_buf = iris_hfi_gen2_session_queue_buffer,
	.session_release_buf = iris_hfi_gen2_session_release_buffer,
	.session_pause = iris_hfi_gen2_session_pause,
	.session_resume_drc = iris_hfi_gen2_session_resume_drc,
	.session_stop = iris_hfi_gen2_session_stop,
	.session_drain = iris_hfi_gen2_session_drain,
	.session_resume_drain = iris_hfi_gen2_session_resume_drain,
	.session_close = iris_hfi_gen2_session_close,
};

void iris_hfi_gen2_command_ops_init(struct iris_core *core)
{
	core->hfi_ops = &iris_hfi_gen2_command_ops;
}

struct iris_inst *iris_hfi_gen2_get_instance(void)
{
	struct iris_inst_hfi_gen2 *inst = kzalloc(sizeof(*inst), GFP_KERNEL);

	if (!inst)
		return NULL;
	iris_hfi_pending_init(&inst->open_pending);
	iris_hfi_pending_init(&inst->start_pending[0]);
	iris_hfi_pending_init(&inst->start_pending[1]);
	init_completion(&inst->open_done);
	init_completion(&inst->start_done[0]);
	init_completion(&inst->start_done[1]);
	return &inst->inst;
}
