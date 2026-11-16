// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/overflow.h>
#include <linux/pm_runtime.h>
#include <media/v4l2-mem2mem.h>

#include "iris_instance.h"
#include "iris_utils.h"

static u64 iris_mbpf(u32 width, u32 height)
{
	return DIV_ROUND_UP_ULL(width, 16) * DIV_ROUND_UP_ULL(height, 16);
}

bool iris_res_is_less_than(u32 width, u32 height,
			   u32 ref_width, u32 ref_height)
{
	u64 num_mbs = iris_mbpf(width, height);
	u32 max_side = max(ref_width, ref_height);

	if (num_mbs < iris_mbpf(ref_width, ref_height) &&
	    width < max_side &&
	    height < max_side)
		return true;

	return false;
}

int iris_get_mbpf(struct iris_inst *inst)
{
	struct v4l2_format *inp_f = inst->fmt_src;
	u32 height = max(inp_f->fmt.pix_mp.height, inst->crop.height);
	u32 width = max(inp_f->fmt.pix_mp.width, inst->crop.width);
	u64 mbpf;

	if (!width || !height)
		return -EINVAL;

	mbpf = iris_mbpf(width, height);
	if (mbpf > INT_MAX)
		return -EOVERFLOW;

	return mbpf;
}

bool iris_split_mode_enabled(struct iris_inst *inst)
{
	u32 pixelformat = inst->fmt_dst->fmt.pix_mp.pixelformat;

	if (inst->core->iris_platform_data->no_opb &&
	    pixelformat == V4L2_PIX_FMT_QC08C)
		return false;

	return pixelformat == V4L2_PIX_FMT_NV12 ||
		pixelformat == V4L2_PIX_FMT_QC08C ||
		pixelformat == V4L2_PIX_FMT_P010 ||
		pixelformat == V4L2_PIX_FMT_QC10C;
}

bool iris_fmt_is_8bit(u32 pixelformat)
{
	return pixelformat == V4L2_PIX_FMT_NV12 ||
		pixelformat == V4L2_PIX_FMT_QC08C;
}

bool iris_fmt_is_10bit(u32 pixelformat)
{
	return pixelformat == V4L2_PIX_FMT_P010 ||
		pixelformat == V4L2_PIX_FMT_QC10C;
}

void iris_helper_buffers_done(struct iris_inst *inst, unsigned int type,
			      enum vb2_buffer_state state)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct vb2_v4l2_buffer *buf;

	/* Error callbacks cannot turn a transport failure into a backing handoff. */
	if (inst->core->native_repeat && !inst->repeat_cpu_drained)
		return;
	if (V4L2_TYPE_IS_OUTPUT(type)) {
		while ((buf = v4l2_m2m_src_buf_remove(m2m_ctx)))
			v4l2_m2m_buf_done(buf, state);
	} else if (V4L2_TYPE_IS_CAPTURE(type)) {
		while ((buf = v4l2_m2m_dst_buf_remove(m2m_ctx)))
			v4l2_m2m_buf_done(buf, state);
	}
}

int iris_wait_for_session_response(struct iris_inst *inst, bool is_flush)
{
	struct iris_core *core = inst->core;
	u32 hw_response_timeout_val;
	struct completion *done;
	int ret;

	hw_response_timeout_val = core->iris_platform_data->hw_response_timeout;
	done = is_flush ? &inst->flush_completion : &inst->completion;

	mutex_unlock(&inst->lock);
	ret = wait_for_completion_timeout(done, msecs_to_jiffies(hw_response_timeout_val));
	mutex_lock(&inst->lock);
	if (!is_flush)
		iris_hfi_pending_cancel(&inst->control_pending);
	if (!ret) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		return -ETIMEDOUT;
	}

	return inst->state == IRIS_INST_ERROR || core->state == IRIS_CORE_ERROR ? -EIO : 0;
}

struct iris_inst *iris_get_response_instance(struct iris_core *core, u32 session_id)
{
	struct iris_inst *inst, *result = NULL;

	mutex_lock(&core->lock);
	list_for_each_entry(inst, &core->instances, list) {
		if (inst->session_id == session_id && !inst->response_closing) {
			refcount_inc(&inst->response_refs);
			result = inst;
			break;
		}
	}
	mutex_unlock(&core->lock);
	return result;
}

void iris_put_response_instance(struct iris_inst *inst)
{
	if (refcount_dec_and_test(&inst->response_refs))
		complete(&inst->response_idle);
}

struct iris_inst *iris_get_instance(struct iris_core *core, u32 session_id)
{
	struct iris_inst *inst;

	mutex_lock(&core->lock);
	list_for_each_entry(inst, &core->instances, list) {
		if (inst->session_id == session_id) {
			mutex_unlock(&core->lock);
			return inst;
		}
	}

	mutex_unlock(&core->lock);
	return NULL;
}

int iris_check_core_mbpf(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	struct iris_inst *instance;
	u32 total_mbpf = 0, mbpf;
	int ret = 0;

	mutex_lock(&core->lock);
	list_for_each_entry(instance, &core->instances, list) {
		ret = iris_get_mbpf(instance);
		if (ret < 0)
			goto unlock;
		mbpf = ret;
		ret = 0;
		if (check_add_overflow(total_mbpf, mbpf, &total_mbpf)) {
			ret = -EOVERFLOW;
			goto unlock;
		}
	}
	if (total_mbpf > core->iris_platform_data->max_core_mbpf)
		ret = -ENOMEM;

unlock:
	mutex_unlock(&core->lock);
	return ret;
}

int iris_check_core_mbps(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	struct iris_inst *instance;
	u32 total_mbps = 0, fps, mbpf, mbps;
	int ret = 0;

	mutex_lock(&core->lock);
	list_for_each_entry(instance, &core->instances, list) {
		ret = iris_get_mbpf(instance);
		if (ret < 0)
			goto unlock;
		mbpf = ret;
		ret = 0;
		fps = max(instance->frame_rate, instance->operating_rate);
		if (check_mul_overflow(mbpf, fps, &mbps) ||
		    check_add_overflow(total_mbps, mbps, &total_mbps)) {
			ret = -EOVERFLOW;
			goto unlock;
		}
	}
	if (total_mbps > core->iris_platform_data->max_core_mbps)
		ret = -ENOMEM;

unlock:
	mutex_unlock(&core->lock);
	return ret;
}

