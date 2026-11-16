// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <media/v4l2-event.h>
#include <media/v4l2-mem2mem.h>

#include "iris_buffer.h"
#include "iris_instance.h"
#include "iris_power.h"
#include "iris_vpu_buffer.h"

#define PIXELS_4K 4096
#define MAX_WIDTH 4096
#define MAX_HEIGHT 2304
#define Y_STRIDE_ALIGN 128
#define UV_STRIDE_ALIGN 128
#define Y_SCANLINE_ALIGN 32
#define UV_SCANLINE_ALIGN 16
#define UV_SCANLINE_ALIGN_QC08C 32
#define Y_STRIDE_ALIGN_P010 256
#define UV_STRIDE_ALIGN_P010 256
#define Y_SCANLINE_ALIGN_QC10C 16
#define META_STRIDE_ALIGNED 64
#define META_SCANLINE_ALIGNED 16
#define NUM_MBS_4K (DIV_ROUND_UP(MAX_WIDTH, 16) * DIV_ROUND_UP(MAX_HEIGHT, 16))

/*
 * NV12:
 * YUV 4:2:0 image with a plane of 8 bit Y samples followed
 * by an interleaved U/V plane containing 8 bit 2x2 subsampled
 * colour difference samples.
 *
 * <-Y/UV_Stride (aligned to 128)->
 * <------- Width ------->
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  ^           ^
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  |           |
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  Height      |
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  |          y_scanlines (aligned to 32)
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  |           |
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  |           |
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  |           |
 * Y Y Y Y Y Y Y Y Y Y Y Y . . . .  V           |
 * . . . . . . . . . . . . . . . .              |
 * . . . . . . . . . . . . . . . .              |
 * . . . . . . . . . . . . . . . .              |
 * . . . . . . . . . . . . . . . .              V
 * U V U V U V U V U V U V . . . .  ^
 * U V U V U V U V U V U V . . . .  |
 * U V U V U V U V U V U V . . . .  |
 * U V U V U V U V U V U V . . . .  uv_scanlines (aligned to 16)
 * . . . . . . . . . . . . . . . .  |
 * . . . . . . . . . . . . . . . .  V
 * . . . . . . . . . . . . . . . .  --> Buffer size aligned to 4K
 *
 * y_stride : Width aligned to 128
 * uv_stride : Width aligned to 128
 * y_scanlines: Height aligned to 32
 * uv_scanlines: Height/2 aligned to 16
 * Total size = align((y_stride * y_scanlines
 *          + uv_stride * uv_scanlines , 4096)
 *
 * Note: All the alignments are hardware requirements.
 */
static u32 iris_yuv_buffer_size_nv12(struct iris_inst *inst)
{
	u32 y_plane, uv_plane, y_stride, uv_stride, y_scanlines, uv_scanlines;
	struct v4l2_format *f;

	if (inst->domain == DECODER)
		f = inst->fmt_dst;
	else
		f = inst->fmt_src;

	y_stride = ALIGN(f->fmt.pix_mp.width, Y_STRIDE_ALIGN);
	uv_stride = ALIGN(f->fmt.pix_mp.width, UV_STRIDE_ALIGN);
	y_scanlines = ALIGN(f->fmt.pix_mp.height, Y_SCANLINE_ALIGN);
	uv_scanlines = ALIGN((f->fmt.pix_mp.height + 1) >> 1, UV_SCANLINE_ALIGN);
	y_plane = y_stride * y_scanlines;
	uv_plane = uv_stride * uv_scanlines;

	return ALIGN(y_plane + uv_plane, PIXELS_4K);
}

/*
 * QC08C:
 * Compressed Macro-tile format for NV12.
 * Contains 4 planes in the following order -
 * (A) Y_Meta_Plane
 * (B) Y_UBWC_Plane
 * (C) UV_Meta_Plane
 * (D) UV_UBWC_Plane
 *
 * Y_Meta_Plane consists of meta information to decode compressed
 * tile data in Y_UBWC_Plane.
 * Y_UBWC_Plane consists of Y data in compressed macro-tile format.
 * UBWC decoder block will use the Y_Meta_Plane data together with
 * Y_UBWC_Plane data to produce loss-less uncompressed 8 bit Y samples.
 *
 * UV_Meta_Plane consists of meta information to decode compressed
 * tile data in UV_UBWC_Plane.
 * UV_UBWC_Plane consists of UV data in compressed macro-tile format.
 * UBWC decoder block will use UV_Meta_Plane data together with
 * UV_UBWC_Plane data to produce loss-less uncompressed 8 bit 2x2
 * subsampled color difference samples.
 *
 * Each tile in Y_UBWC_Plane/UV_UBWC_Plane is independently decodable
 * and randomly accessible. There is no dependency between tiles.
 *
 * <----- y_meta_stride ----> (aligned to 64)
 * <-------- Width ------>
 * M M M M M M M M M M M M . .      ^           ^
 * M M M M M M M M M M M M . .      |           |
 * M M M M M M M M M M M M . .      Height      |
 * M M M M M M M M M M M M . .      |         y_meta_scanlines  (aligned to 16)
 * M M M M M M M M M M M M . .      |           |
 * M M M M M M M M M M M M . .      |           |
 * M M M M M M M M M M M M . .      |           |
 * M M M M M M M M M M M M . .      V           |
 * . . . . . . . . . . . . . .                  |
 * . . . . . . . . . . . . . .                  |
 * . . . . . . . . . . . . . .      -------> Buffer size aligned to 4k
 * . . . . . . . . . . . . . .                  V
 * <--Compressed tile y_stride---> (aligned to 128)
 * <------- Width ------->
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  ^           ^
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  |           |
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  Height      |
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  |        Macro_tile y_scanlines (aligned to 32)
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  |           |
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  |           |
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  |           |
 * Y* Y* Y* Y* Y* Y* Y* Y* . . . .  V           |
 * . . . . . . . . . . . . . . . .              |
 * . . . . . . . . . . . . . . . .              |
 * . . . . . . . . . . . . . . . .  -------> Buffer size aligned to 4k
 * . . . . . . . . . . . . . . . .              V
 * <----- uv_meta_stride ---->  (aligned to 64)
 * M M M M M M M M M M M M . .      ^
 * M M M M M M M M M M M M . .      |
 * M M M M M M M M M M M M . .      |
 * M M M M M M M M M M M M . .      uv_meta_scanlines (aligned to 16)
 * . . . . . . . . . . . . . .      |
 * . . . . . . . . . . . . . .      V
 * . . . . . . . . . . . . . .      -------> Buffer size aligned to 4k
 * <--Compressed tile uv_stride---> (aligned to 128)
 * U* V* U* V* U* V* U* V* . . . .  ^
 * U* V* U* V* U* V* U* V* . . . .  |
 * U* V* U* V* U* V* U* V* . . . .  |
 * U* V* U* V* U* V* U* V* . . . .  uv_scanlines (aligned to 32)
 * . . . . . . . . . . . . . . . .  |
 * . . . . . . . . . . . . . . . .  V
 * . . . . . . . . . . . . . . . .  -------> Buffer size aligned to 4k
 *
 * y_stride: width aligned to 128
 * uv_stride: width aligned to 128
 * y_scanlines: height aligned to 32
 * uv_scanlines: height aligned to 32
 * y_plane: buffer size aligned to 4096
 * uv_plane: buffer size aligned to 4096
 * y_meta_stride: width aligned to 64
 * y_meta_scanlines: height aligned to 16
 * y_meta_plane: buffer size aligned to 4096
 * uv_meta_stride: width aligned to 64
 * uv_meta_scanlines: height aligned to 16
 * uv_meta_plane: buffer size aligned to 4096
 *
 * Total size = align( y_plane + uv_plane +
 *           y_meta_plane + uv_meta_plane, 4096)
 *
 * Note: All the alignments are hardware requirements.
 */
static u32 iris_yuv_buffer_size_p010(struct iris_inst *inst)
{
	u32 y_plane, uv_plane, y_stride, uv_stride, y_scanlines, uv_scanlines;
	struct v4l2_format *f;

	if (inst->domain == DECODER)
		f = inst->fmt_dst;
	else
		f = inst->fmt_src;

	y_stride = ALIGN(f->fmt.pix_mp.width * 2, Y_STRIDE_ALIGN_P010);
	uv_stride = ALIGN(f->fmt.pix_mp.width * 2, UV_STRIDE_ALIGN_P010);
	y_scanlines = ALIGN(f->fmt.pix_mp.height, Y_SCANLINE_ALIGN);
	uv_scanlines = ALIGN((f->fmt.pix_mp.height + 1) >> 1, UV_SCANLINE_ALIGN);
	y_plane = y_stride * y_scanlines;
	uv_plane = uv_stride * uv_scanlines;

	return ALIGN(y_plane + uv_plane, PIXELS_4K);
}

static u32 iris_yuv_buffer_size_qc10c(struct iris_inst *inst)
{
	u32 y_plane, uv_plane, y_stride, uv_stride;
	u32 uv_meta_stride, uv_meta_plane;
	u32 y_meta_stride, y_meta_plane;
	struct v4l2_format *f;

	if (inst->domain == DECODER)
		f = inst->fmt_dst;
	else
		f = inst->fmt_src;

	y_meta_stride = ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.width, 48),
			      META_STRIDE_ALIGNED);
	y_meta_plane = y_meta_stride * ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.height, 4),
					     META_SCANLINE_ALIGNED);
	y_meta_plane = ALIGN(y_meta_plane, PIXELS_4K);

	y_stride = ALIGN(f->fmt.pix_mp.width * 4 / 3, Y_STRIDE_ALIGN_P010);
	y_plane = ALIGN(y_stride * ALIGN(f->fmt.pix_mp.height, Y_SCANLINE_ALIGN_QC10C),
			PIXELS_4K);

	uv_meta_stride = ALIGN(DIV_ROUND_UP((f->fmt.pix_mp.width + 1) >> 1, 24),
			       META_STRIDE_ALIGNED);
	uv_meta_plane = uv_meta_stride *
			ALIGN(DIV_ROUND_UP((f->fmt.pix_mp.height + 1) >> 1, 4),
			      META_SCANLINE_ALIGNED);
	uv_meta_plane = ALIGN(uv_meta_plane, PIXELS_4K);

	uv_stride = ALIGN(f->fmt.pix_mp.width * 4 / 3, UV_STRIDE_ALIGN_P010);
	uv_plane = ALIGN(uv_stride * ALIGN((f->fmt.pix_mp.height + 1) >> 1, UV_SCANLINE_ALIGN),
			 PIXELS_4K);

	return ALIGN(y_meta_plane + y_plane + uv_meta_plane + uv_plane, PIXELS_4K);
}

static u32 iris_yuv_buffer_size_qc08c(struct iris_inst *inst)
{
	u32 y_plane, uv_plane, y_stride, uv_stride;
	struct v4l2_format *f = inst->fmt_dst;
	u32 uv_meta_stride, uv_meta_plane;
	u32 y_meta_stride, y_meta_plane;

	y_meta_stride = ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.width, META_STRIDE_ALIGNED >> 1),
			      META_STRIDE_ALIGNED);
	y_meta_plane = y_meta_stride * ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.height,
							  META_SCANLINE_ALIGNED >> 1),
					     META_SCANLINE_ALIGNED);
	y_meta_plane = ALIGN(y_meta_plane, PIXELS_4K);

	y_stride = ALIGN(f->fmt.pix_mp.width, Y_STRIDE_ALIGN);
	y_plane = ALIGN(y_stride * ALIGN(f->fmt.pix_mp.height, Y_SCANLINE_ALIGN), PIXELS_4K);

	uv_meta_stride = ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.width / 2, META_STRIDE_ALIGNED >> 2),
			       META_STRIDE_ALIGNED);
	uv_meta_plane = uv_meta_stride * ALIGN(DIV_ROUND_UP(f->fmt.pix_mp.height / 2,
							    META_SCANLINE_ALIGNED >> 1),
					       META_SCANLINE_ALIGNED);
	uv_meta_plane = ALIGN(uv_meta_plane, PIXELS_4K);

	uv_stride = ALIGN(f->fmt.pix_mp.width, UV_STRIDE_ALIGN);
	uv_plane = ALIGN(uv_stride * ALIGN(f->fmt.pix_mp.height / 2, UV_SCANLINE_ALIGN_QC08C),
			 PIXELS_4K);

	return ALIGN(y_meta_plane + y_plane + uv_meta_plane + uv_plane, PIXELS_4K);
}

static int iris_dec_bitstream_buffer_size(struct iris_inst *inst)
{
	struct platform_inst_caps *caps = inst->core->iris_platform_data->inst_caps;
	u32 base_res_mbs = NUM_MBS_4K;
	u64 frame_size;
	int num_mbs;
	u32 div_factor = 2;

	num_mbs = iris_get_mbpf(inst);
	if (num_mbs < 0)
		return num_mbs;
	if (num_mbs > NUM_MBS_4K) {
		div_factor = 4;
		base_res_mbs = caps->max_mbpf;
	} else {
		if (inst->codec == V4L2_PIX_FMT_VP9)
			div_factor = 1;
	}

	/*
	 * frame_size = YUVsize / div_factor
	 * where YUVsize = resolution_in_MBs * MBs_in_pixel * 3 / 2
	 */
	frame_size = (u64)base_res_mbs * (16 * 16) * 3 / 2 / div_factor;
	frame_size = ALIGN(frame_size, PIXELS_4K);
	if (frame_size > INT_MAX)
		return -EOVERFLOW;
	return (int)frame_size;
}

static u32 iris_enc_bitstream_buffer_size(struct iris_inst *inst)
{
	u32 aligned_width, aligned_height, bitstream_size, yuv_size;
	int bitrate_mode, frame_rc;
	struct v4l2_format *f;

	f = inst->fmt_dst;

	bitrate_mode = inst->fw_caps[BITRATE_MODE].value;
	frame_rc = inst->fw_caps[FRAME_RC_ENABLE].value;

	aligned_width = ALIGN(f->fmt.pix_mp.width, 32);
	aligned_height = ALIGN(f->fmt.pix_mp.height, 32);
	bitstream_size = aligned_width * aligned_height * 3;
	yuv_size = (aligned_width * aligned_height * 3) >> 1;
	if (aligned_width * aligned_height > (4096 * 2176))
		/* bitstream_size = 0.25 * yuv_size; */
		bitstream_size = (bitstream_size >> 3);
	else if (aligned_width * aligned_height > (1280 * 720))
		/* bitstream_size = 0.5 * yuv_size; */
		bitstream_size = (bitstream_size >> 2);

	if ((!frame_rc || bitrate_mode == V4L2_MPEG_VIDEO_BITRATE_MODE_CQ) &&
	    bitstream_size < yuv_size)
		bitstream_size = (bitstream_size << 1);

	return ALIGN(bitstream_size, 4096);
}

int iris_calc_capture_layout(u32 format, u32 width, u32 height,
			     struct iris_capture_layout *layout)
{
	struct iris_capture_layout result = {0};
	u64 stride, y, uv, meta_y = 0, meta_uv = 0, size;
	u32 w_align = 128, h_align = 32;

	if (!layout || !width || !height || width > 8192 || height > 8192)
		return -EINVAL;
	if (format == V4L2_PIX_FMT_QC10C) {
		w_align = 192;
		h_align = 16;
	} else if (format != V4L2_PIX_FMT_NV12 && format != V4L2_PIX_FMT_QC08C &&
		   format != V4L2_PIX_FMT_P010) {
		return -EINVAL;
	}
	result.width = DIV_ROUND_UP(width, w_align) * w_align;
	result.height = DIV_ROUND_UP(height, h_align) * h_align;
	stride = result.width;
	if (format == V4L2_PIX_FMT_P010)
		stride *= 2;
	else if (format == V4L2_PIX_FMT_QC10C)
		stride = ALIGN(stride * 4 / 3, 256);
	result.stride = stride;
	result.y_scanlines = result.height;
	result.uv_scanlines = ALIGN(DIV_ROUND_UP(result.height, 2),
				    format == V4L2_PIX_FMT_QC08C ? 32 : 16);
	y = stride * result.y_scanlines;
	uv = stride * result.uv_scanlines;
	if (format == V4L2_PIX_FMT_QC08C || format == V4L2_PIX_FMT_QC10C) {
		u32 tile_width = format == V4L2_PIX_FMT_QC08C ? 32 : 48;
		u32 tile_height = format == V4L2_PIX_FMT_QC08C ? 8 : 4;

		meta_y = (u64)ALIGN(DIV_ROUND_UP(result.width, tile_width), 64) *
			ALIGN(DIV_ROUND_UP(result.height, tile_height), 16);
		meta_uv = (u64)ALIGN(DIV_ROUND_UP(DIV_ROUND_UP(result.width, 2),
						 tile_width / 2), 64) *
			ALIGN(DIV_ROUND_UP(DIV_ROUND_UP(result.height, 2), tile_height), 16);
		y = ALIGN(y, 4096);
		uv = ALIGN(uv, 4096);
		meta_y = ALIGN(meta_y, 4096);
		meta_uv = ALIGN(meta_uv, 4096);
	}
	size = ALIGN(y + uv + meta_y + meta_uv, 4096);
	if (stride > 65535 || result.height > 65535 || size > 0xffffffffULL)
		return -EOVERFLOW;
	result.size = size;
	*layout = result;
	return 0;
}

int iris_apply_capture_layout(struct iris_inst *inst, u32 width, u32 height)
{
	struct v4l2_pix_format_mplane *fmt = &inst->fmt_dst->fmt.pix_mp;
	struct iris_capture_layout layout;
	int ret;

	if (inst->core->iris_platform_data->prototype_restricted &&
	    fmt->pixelformat != V4L2_PIX_FMT_NV12)
		return -EOPNOTSUPP;
	ret = iris_calc_capture_layout(fmt->pixelformat, width, height, &layout);
	if (ret)
		return ret;
	fmt->width = layout.width;
	fmt->height = layout.height;
	fmt->num_planes = 1;
	fmt->plane_fmt[0].bytesperline = layout.stride;
	fmt->plane_fmt[0].sizeimage = layout.size;
	inst->buffers[BUF_OUTPUT].size = layout.size;
	return 0;
}

int iris_get_buffer_size(struct iris_inst *inst,
			 enum iris_buffer_type buffer_type)
{
	if (inst->domain == DECODER) {
		switch (buffer_type) {
		case BUF_INPUT:
			return iris_dec_bitstream_buffer_size(inst);
		case BUF_OUTPUT: {
			struct v4l2_pix_format_mplane *fmt = &inst->fmt_dst->fmt.pix_mp;
			struct iris_capture_layout layout;
			int ret = iris_calc_capture_layout(fmt->pixelformat, fmt->width, fmt->height, &layout);

			return ret ? ret : (int)layout.size;
		}
		case BUF_DPB: {
			struct v4l2_pix_format_mplane *fmt = &inst->fmt_dst->fmt.pix_mp;

			if (!fmt->width || !fmt->height || fmt->width > 8192 || fmt->height > 8192)
				return -EINVAL;
			if (iris_fmt_is_10bit(inst->fmt_dst->fmt.pix_mp.pixelformat))
				return iris_yuv_buffer_size_qc10c(inst);
			return iris_yuv_buffer_size_qc08c(inst);
		}
		default:
			return 0;
		}
	}
	if (buffer_type == BUF_INPUT) {
		if (inst->fmt_src->fmt.pix_mp.pixelformat == V4L2_PIX_FMT_P010)
			return iris_yuv_buffer_size_p010(inst);
		return iris_yuv_buffer_size_nv12(inst);
	}
	if (buffer_type == BUF_OUTPUT)
		return iris_enc_bitstream_buffer_size(inst);
	return 0;
}

static int iris_validate_internal_buffer_table(const u32 *types, u32 count)
{
	u32 i, j;

	if (count > BUF_TYPE_MAX || (count && !types))
		return -EINVAL;
	for (i = 0; i < count; i++) {
		if (types[i] <= BUF_OUTPUT || types[i] >= BUF_TYPE_MAX)
			return -EINVAL;
		for (j = 0; j < i; j++)
			if (types[j] == types[i])
				return -EINVAL;
	}
	return 0;
}

/* Caller has validated the matching RELEASE_DONE, not merely buffer_done. */
int iris_mark_internal_buffer_returned(struct iris_buffer *buffer, bool readonly)
{
	if (!buffer || buffer->type <= BUF_OUTPUT || buffer->type >= BUF_TYPE_MAX ||
	    !(buffer->attr & BUF_ATTR_QUEUED) ||
	    (!readonly && !(buffer->attr & BUF_ATTR_PENDING_RELEASE)))
		return -EINVAL;
	if (readonly) {
		buffer->attr |= BUF_ATTR_READONLY;
		return 0;
	}
	buffer->attr &= ~(BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE |
			  BUF_ATTR_DEFERRED | BUF_ATTR_READONLY);
	buffer->attr |= BUF_ATTR_DEQUEUED;
	return 0;
}

static void iris_fill_internal_buf_info(struct iris_inst *inst,
					enum iris_buffer_type buffer_type)
{
	struct iris_buffers *buffers;
	u32 size;
	int count;

	if (buffer_type <= BUF_OUTPUT || buffer_type >= BUF_TYPE_MAX)
		return;
	buffers = &inst->buffers[buffer_type];
	size = inst->core->iris_platform_data->get_vpu_buffer_size(inst, buffer_type);
	count = iris_vpu_buf_count(inst, buffer_type);
	/* Never turn a negative size/count into a large unsigned allocation. */
	buffers->size = size > INT_MAX ? 0 : size;
	buffers->min_count = count < 0 || count > VIDEO_MAX_FRAME ? VIDEO_MAX_FRAME + 1 : count;
}

void iris_get_internal_buffers(struct iris_inst *inst, u32 plane)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	const u32 *internal_buf_type;
	u32 internal_buffer_count, i;

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->dec_ip_int_buf_tbl;
			internal_buffer_count = platform_data->dec_ip_int_buf_tbl_size;
			if (iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count))
				return;
			for (i = 0; i < internal_buffer_count; i++)
				iris_fill_internal_buf_info(inst, internal_buf_type[i]);
		} else {
			internal_buf_type = platform_data->dec_op_int_buf_tbl;
			internal_buffer_count = platform_data->dec_op_int_buf_tbl_size;
			if (iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count))
				return;
			for (i = 0; i < internal_buffer_count; i++)
				iris_fill_internal_buf_info(inst, internal_buf_type[i]);
		}
	} else {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->enc_ip_int_buf_tbl;
			internal_buffer_count = platform_data->enc_ip_int_buf_tbl_size;
			if (iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count))
				return;
			for (i = 0; i < internal_buffer_count; i++)
				iris_fill_internal_buf_info(inst, internal_buf_type[i]);
		} else {
			internal_buf_type = platform_data->enc_op_int_buf_tbl;
			internal_buffer_count = platform_data->enc_op_int_buf_tbl_size;
			if (iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count))
				return;
			for (i = 0; i < internal_buffer_count; i++)
				iris_fill_internal_buf_info(inst, internal_buf_type[i]);
		}
	}
}

static int iris_create_internal_buffer(struct iris_inst *inst,
				       enum iris_buffer_type buffer_type, u32 index)
{
	struct iris_buffers *buffers;
	struct iris_core *core = inst->core;
	struct iris_buffer *buffer;

	if (buffer_type <= BUF_OUTPUT || buffer_type >= BUF_TYPE_MAX)
		return -EINVAL;
	buffers = &inst->buffers[buffer_type];
	if (buffers->size > INT_MAX)
		return -EOVERFLOW;
	if (!buffers->size)
		return 0;

	buffer = kzalloc(sizeof(*buffer), GFP_KERNEL);
	if (!buffer)
		return -ENOMEM;

	INIT_LIST_HEAD(&buffer->list);
	buffer->type = buffer_type;
	buffer->index = index;
	buffer->buffer_size = buffers->size;
	buffer->dma_attrs = DMA_ATTR_WRITE_COMBINE | DMA_ATTR_NO_KERNEL_MAPPING;

	buffer->kvaddr = dma_alloc_attrs(core->dev, buffer->buffer_size,
					 &buffer->device_addr, GFP_KERNEL, buffer->dma_attrs);
	if (!buffer->kvaddr) {
		kfree(buffer);
		return -ENOMEM;
	}

	list_add_tail(&buffer->list, &buffers->list);

	return 0;
}

int iris_create_internal_buffers(struct iris_inst *inst, u32 plane)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	u32 internal_buffer_count, i, j;
	struct iris_buffers *buffers;
	const u32 *internal_buf_type;
	struct list_head *original_tail[BUF_TYPE_MAX];
	int ret;

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->dec_ip_int_buf_tbl;
			internal_buffer_count = platform_data->dec_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->dec_op_int_buf_tbl;
			internal_buffer_count = platform_data->dec_op_int_buf_tbl_size;
		}
	} else {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->enc_ip_int_buf_tbl;
			internal_buffer_count = platform_data->enc_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->enc_op_int_buf_tbl;
			internal_buffer_count = platform_data->enc_op_int_buf_tbl_size;
		}
	}

	ret = iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count);
	if (ret)
		return ret;
	for (i = 0; i < internal_buffer_count; i++) {
		if (READ_ONCE(inst->core->decode_once)) {
			struct iris_buffer *buffer;
			u32 retained = 0;

			if (inst->decode_internal_generations[internal_buf_type[i]] >= 2)
				return -ESHUTDOWN;
			list_for_each_entry(buffer, &inst->buffers[internal_buf_type[i]].list, list)
				retained++;
			/* One initial plus one first-IPSC generation, finite RAM budget. */
			if (retained > VIDEO_MAX_FRAME * 2 ||
			    inst->buffers[internal_buf_type[i]].min_count > VIDEO_MAX_FRAME * 2 - retained)
				return -ENOSPC;
		}
		if (inst->buffers[internal_buf_type[i]].min_count > VIDEO_MAX_FRAME)
			return -EINVAL;
		original_tail[i] = inst->buffers[internal_buf_type[i]].list.prev;
	}
	/* Consume a finite generation attempt before allocation; failures never retry. */
	if (READ_ONCE(inst->core->decode_once))
		for (i = 0; i < internal_buffer_count; i++)
			inst->decode_internal_generations[internal_buf_type[i]]++;
	for (i = 0; i < internal_buffer_count; i++) {
		struct iris_buffer *buffer;
		u32 index = 0;

		buffers = &inst->buffers[internal_buf_type[i]];
		/* Old DRC allocations remain live: do not reuse their identifiers. */
		list_for_each_entry(buffer, &buffers->list, list) {
			if (buffer->index == U32_MAX) {
				ret = -EOVERFLOW;
				goto rollback;
			}
			index = max(index, buffer->index + 1);
		}
		if (buffers->min_count && index > U32_MAX - (buffers->min_count - 1)) {
			ret = -EOVERFLOW;
			goto rollback;
		}
		for (j = 0; j < buffers->min_count; j++, index++) {
			ret = iris_create_internal_buffer(inst, internal_buf_type[i], index);
			if (ret)
				goto rollback;
		}
	}

	return 0;

rollback:
	/* Only this call's unsubmitted suffix may be reclaimed. */
	for (i = 0; i < internal_buffer_count; i++) {
		struct list_head *head = &inst->buffers[internal_buf_type[i]].list;

		while (head->prev != original_tail[i]) {
			struct iris_buffer *buffer = list_last_entry(head, struct iris_buffer, list);

			/* A refused reclamation must not spin on the same tail. */
			if (iris_destroy_internal_buffer(inst, buffer))
				return -EBUSY;
		}
	}
	return ret;
}

int iris_queue_buffer(struct iris_inst *inst, struct iris_buffer *buf)
{
	const struct iris_hfi_command_ops *hfi_ops = inst->core->hfi_ops;
	int ret;

	if (buf->experiment_retired)
		return -ESHUTDOWN;
	ret = hfi_ops->session_queue_buf(inst, buf);
	if (ret)
		return ret;

	buf->attr &= ~BUF_ATTR_DEFERRED;
	buf->attr |= BUF_ATTR_QUEUED;

	return 0;
}

int iris_queue_internal_deferred_buffers(struct iris_inst *inst, enum iris_buffer_type buffer_type)
{
	struct iris_buffer *buffer, *next;
	struct iris_buffers *buffers;
	int ret = 0;

	if (buffer_type <= BUF_OUTPUT || buffer_type >= BUF_TYPE_MAX)
		return -EINVAL;
	buffers = &inst->buffers[buffer_type];
	list_for_each_entry_safe(buffer, next, &buffers->list, list) {
		if (buffer->experiment_retired)
			continue;
		if (buffer->attr & (BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY))
			continue;
		if (buffer->attr & BUF_ATTR_QUEUED)
			continue;

		if (buffer->attr & BUF_ATTR_DEFERRED) {
			ret = iris_queue_buffer(inst, buffer);
			if (ret)
				return ret;
		}
	}

	return ret;
}

int iris_queue_internal_buffers(struct iris_inst *inst, u32 plane)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	struct iris_buffer *buffer, *next;
	struct iris_buffers *buffers;
	const u32 *internal_buf_type;
	u32 internal_buffer_count, i;
	int ret;

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->dec_ip_int_buf_tbl;
			internal_buffer_count = platform_data->dec_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->dec_op_int_buf_tbl;
			internal_buffer_count = platform_data->dec_op_int_buf_tbl_size;
		}
	} else {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->enc_ip_int_buf_tbl;
			internal_buffer_count = platform_data->enc_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->enc_op_int_buf_tbl;
			internal_buffer_count = platform_data->enc_op_int_buf_tbl_size;
		}
	}

	ret = iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count);
	if (ret)
		return ret;
	for (i = 0; i < internal_buffer_count; i++) {
		buffers = &inst->buffers[internal_buf_type[i]];
		list_for_each_entry_safe(buffer, next, &buffers->list, list) {
			if (buffer->experiment_retired)
				continue;
			if (buffer->attr & (BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY))
				continue;
			if (buffer->attr & BUF_ATTR_QUEUED)
				continue;
			if (buffer->type == BUF_DPB && inst->state != IRIS_INST_STREAMING) {
				buffer->attr |= BUF_ATTR_DEFERRED;
				continue;
			}
			ret = iris_queue_buffer(inst, buffer);
			if (ret)
				return ret;
		}
	}

	return 0;
}

int iris_destroy_internal_buffer(struct iris_inst *inst, struct iris_buffer *buffer)
{
	struct iris_core *core = inst->core;

	/* RAM-only ownership: never release internal DPB/COMV/PERSIST on errors. */
	if (READ_ONCE(core->decode_once))
		return -EBUSY;

	if (buffer->attr & (BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY))
		return -EBUSY;
	list_del(&buffer->list);
	dma_free_attrs(core->dev, buffer->buffer_size, buffer->kvaddr,
		       buffer->device_addr, buffer->dma_attrs);
	kfree(buffer);

	return 0;
}

static int iris_destroy_internal_buffers(struct iris_inst *inst, u32 plane, bool force)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	struct iris_buffer *buf, *next;
	struct iris_buffers *buffers;
	const u32 *internal_buf_type;
	u32 i, len;
	int ret, result = 0;

	if (inst->domain == DECODER) {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->dec_ip_int_buf_tbl;
			len = platform_data->dec_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->dec_op_int_buf_tbl;
			len = platform_data->dec_op_int_buf_tbl_size;
		}
	} else {
		if (V4L2_TYPE_IS_OUTPUT(plane)) {
			internal_buf_type = platform_data->enc_ip_int_buf_tbl;
			len = platform_data->enc_ip_int_buf_tbl_size;
		} else {
			internal_buf_type = platform_data->enc_op_int_buf_tbl;
			len = platform_data->enc_op_int_buf_tbl_size;
		}
	}

	ret = iris_validate_internal_buffer_table(internal_buf_type, len);
	if (ret)
		return ret;
	for (i = 0; i < len; i++) {
		buffers = &inst->buffers[internal_buf_type[i]];
		list_for_each_entry_safe(buf, next, &buffers->list, list) {
			if (buf->experiment_retired)
				continue;
			/* No force override without a trusted DMA ownership return. */
			if (buf->attr & (BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY)) {
				if (force)
					result = -EBUSY;
				continue;
			}
			ret = iris_destroy_internal_buffer(inst, buf);
			if (ret)
				result = ret;
		}
	}

	if (force) {
		if (inst->domain == DECODER)
			buffers = &inst->buffers[BUF_PERSIST];
		else
			buffers = &inst->buffers[BUF_ARP];

		list_for_each_entry_safe(buf, next, &buffers->list, list) {
			if (buf->experiment_retired)
				continue;
			ret = iris_destroy_internal_buffer(inst, buf);
			if (ret)
				result = ret;
		}
	}

	return result;
}

int iris_destroy_all_internal_buffers(struct iris_inst *inst, u32 plane)
{
	return iris_destroy_internal_buffers(inst, plane, true);
}

int iris_destroy_dequeued_internal_buffers(struct iris_inst *inst, u32 plane)
{
	return iris_destroy_internal_buffers(inst, plane, false);
}

static int iris_release_internal_buffers(struct iris_inst *inst,
					 enum iris_buffer_type buffer_type)
{
	const struct iris_hfi_command_ops *hfi_ops = inst->core->hfi_ops;
	struct iris_buffers *buffers;
	struct iris_buffer *buffer, *next;
	int ret;

	if (buffer_type <= BUF_OUTPUT || buffer_type >= BUF_TYPE_MAX)
		return -EINVAL;
	buffers = &inst->buffers[buffer_type];
	list_for_each_entry_safe(buffer, next, &buffers->list, list) {
		if (buffer->experiment_retired)
			continue;
		if (buffer->attr & (BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY))
			continue;
		if (!(buffer->attr & BUF_ATTR_QUEUED))
			continue;
		ret = hfi_ops->session_release_buf(inst, buffer);
		if (ret)
			return ret;
		buffer->attr |= BUF_ATTR_PENDING_RELEASE;
	}

	return 0;
}

static int iris_release_input_internal_buffers(struct iris_inst *inst)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	const u32 *internal_buf_type;
	u32 internal_buffer_count, i;
	int ret;

	if (inst->domain == DECODER) {
		internal_buf_type = platform_data->dec_ip_int_buf_tbl;
		internal_buffer_count = platform_data->dec_ip_int_buf_tbl_size;
	} else {
		internal_buf_type = platform_data->enc_ip_int_buf_tbl;
		internal_buffer_count = platform_data->enc_ip_int_buf_tbl_size;
	}

	ret = iris_validate_internal_buffer_table(internal_buf_type, internal_buffer_count);
	if (ret)
		return ret;
	for (i = 0; i < internal_buffer_count; i++) {
		ret = iris_release_internal_buffers(inst, internal_buf_type[i]);
		if (ret)
			return ret;
	}

	return 0;
}

int iris_alloc_and_queue_persist_bufs(struct iris_inst *inst, enum iris_buffer_type buffer_type)
{
	struct iris_buffers *buffers;
	struct iris_buffer *buffer, *next;
	int ret;
	u32 i;

	if (buffer_type <= BUF_OUTPUT || buffer_type >= BUF_TYPE_MAX)
		return -EINVAL;
	buffers = &inst->buffers[buffer_type];
	if (list_empty(&buffers->list)) {
		iris_fill_internal_buf_info(inst, buffer_type);
		if (buffers->min_count > VIDEO_MAX_FRAME)
			return -EINVAL;
		for (i = 0; i < buffers->min_count; i++) {
			ret = iris_create_internal_buffer(inst, buffer_type, i);
			if (ret) {
				list_for_each_entry_safe(buffer, next, &buffers->list, list) {
		if (buffer->experiment_retired)
			continue;
					if (iris_destroy_internal_buffer(inst, buffer))
						return -EBUSY;
				}
				return ret;
			}
		}
	}

	list_for_each_entry_safe(buffer, next, &buffers->list, list) {
		if (buffer->experiment_retired)
			continue;
		if (buffer->attr & (BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY))
			continue;
		if (buffer->attr & BUF_ATTR_QUEUED)
			continue;
		ret = iris_queue_buffer(inst, buffer);
		if (ret)
			return ret;
	}

	return 0;
}

int iris_alloc_and_queue_input_int_bufs(struct iris_inst *inst)
{
	int ret;

	iris_get_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);

	ret = iris_release_input_internal_buffers(inst);
	if (ret)
		return ret;

	/* Only unsubmitted old-generation allocations can be reclaimed here. */
	ret = iris_destroy_dequeued_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	if (ret)
		return ret;
	ret = iris_create_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	if (ret)
		return ret;

	return iris_queue_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
}

int iris_queue_deferred_buffers(struct iris_inst *inst, enum iris_buffer_type buf_type)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct v4l2_m2m_buffer *buffer, *n;
	struct iris_buffer *buf;
	int ret;

	if (inst->state == IRIS_INST_ERROR || inst->core->state != IRIS_CORE_INIT)
		return -EIO;
	ret = iris_scale_power(inst);
	if (ret)
		return ret;

	if (buf_type == BUF_INPUT) {
		v4l2_m2m_for_each_src_buf_safe(m2m_ctx, buffer, n) {
			buf = to_iris_buffer(&buffer->vb);
			if (!(buf->attr & BUF_ATTR_DEFERRED))
				continue;
			ret = iris_queue_buffer(inst, buf);
			if (ret)
				return ret;
		}
	} else {
		v4l2_m2m_for_each_dst_buf_safe(m2m_ctx, buffer, n) {
			buf = to_iris_buffer(&buffer->vb);
			if (!(buf->attr & BUF_ATTR_DEFERRED))
				continue;
			ret = iris_queue_buffer(inst, buf);
			if (ret)
				return ret;
		}
	}

	return 0;
}

void iris_vb2_queue_error(struct iris_inst *inst)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct vb2_queue *q;

	q = v4l2_m2m_get_src_vq(m2m_ctx);
	vb2_queue_error(q);
	q = v4l2_m2m_get_dst_vq(m2m_ctx);
	vb2_queue_error(q);
}

static struct vb2_v4l2_buffer *
iris_helper_find_buf(struct iris_inst *inst, u32 type, u32 idx)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;

	if (V4L2_TYPE_IS_OUTPUT(type))
		return v4l2_m2m_src_buf_remove_by_idx(m2m_ctx, idx);
	else
		return v4l2_m2m_dst_buf_remove_by_idx(m2m_ctx, idx);
}

static void iris_get_ts_metadata(struct iris_inst *inst, u64 timestamp_ns,
				 struct vb2_v4l2_buffer *vbuf)
{
	u32 mask = V4L2_BUF_FLAG_TIMECODE | V4L2_BUF_FLAG_TSTAMP_SRC_MASK;
	u32 i;

	for (i = 0; i < ARRAY_SIZE(inst->tss); ++i) {
		if (inst->tss[i].ts_ns != timestamp_ns)
			continue;

		vbuf->flags &= ~mask;
		vbuf->flags |= inst->tss[i].flags;
		vbuf->timecode = inst->tss[i].tc;
		return;
	}

	vbuf->flags &= ~mask;
	vbuf->flags |= inst->tss[inst->metadata_idx].flags;
	vbuf->timecode = inst->tss[inst->metadata_idx].tc;
}

int iris_vb2_buffer_done(struct iris_inst *inst, struct iris_buffer *buf)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct vb2_v4l2_buffer *vbuf;
	struct vb2_buffer *vb2;
	u32 type, state;

	switch (buf->type) {
	case BUF_INPUT:
		type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
		break;
	case BUF_OUTPUT:
		type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
		break;
	default:
		return 0; /* Internal DPB Buffers */
	}

	vbuf = iris_helper_find_buf(inst, type, buf->index);
	if (!vbuf)
		return -EINVAL;

	vb2 = &vbuf->vb2_buf;

	if (V4L2_TYPE_IS_CAPTURE(type) &&
	    (vb2->num_planes != 1 || buf->data_offset > vb2_plane_size(vb2, 0) ||
	     buf->data_size > vb2_plane_size(vb2, 0) - buf->data_offset)) {
		buf->flags |= V4L2_BUF_FLAG_ERROR;
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		iris_vb2_queue_error(inst);
	}
	vbuf->flags |= buf->flags;

	if (buf->flags & V4L2_BUF_FLAG_ERROR) {
		vbuf->flags &= ~V4L2_BUF_FLAG_LAST;
		state = VB2_BUF_STATE_ERROR;
		vb2->planes[0].data_offset = 0;
		vb2_set_plane_payload(vb2, 0, 0);
		vb2->timestamp = 0;
		v4l2_m2m_buf_done(vbuf, state);
		return 0;
	}

	if (V4L2_TYPE_IS_CAPTURE(type)) {
		vb2->planes[0].data_offset = buf->data_offset;
		vb2_set_plane_payload(vb2, 0, buf->data_offset + buf->data_size);
		vbuf->sequence = inst->sequence_cap++;
		iris_get_ts_metadata(inst, buf->timestamp, vbuf);
	} else {
		vbuf->sequence = inst->sequence_out++;
	}

	if (vbuf->flags & V4L2_BUF_FLAG_LAST) {
		if (!v4l2_m2m_has_stopped(m2m_ctx) &&
		    (!(inst->sub_state & (IRIS_INST_SUB_DRC | IRIS_INST_SUB_DRC_LAST)) ||
		     (inst->sub_state & (IRIS_INST_SUB_DRAIN | IRIS_INST_SUB_DRAIN_LAST)))) {
			const struct v4l2_event ev = { .type = V4L2_EVENT_EOS };

			v4l2_event_queue_fh(&inst->fh, &ev);
			v4l2_m2m_mark_stopped(m2m_ctx);
		}
		inst->last_buffer_dequeued = true;
	}

	state = VB2_BUF_STATE_DONE;
	vb2->timestamp = buf->timestamp;
	v4l2_m2m_buf_done(vbuf, state);

	return 0;
}
