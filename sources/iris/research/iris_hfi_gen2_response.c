// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <media/v4l2-mem2mem.h>

#include "iris_hfi_gen2.h"
#include "iris_hfi_gen2_defines.h"
#include "iris_hfi_gen2_packet.h"
#include "iris_vdec.h"
#include "iris_vpu_buffer.h"
#include "iris_vpu_common.h"

struct iris_hfi_gen2_core_hfi_range {
	u32 begin;
	u32 end;
	int (*handle)(struct iris_core *core, struct iris_hfi_packet *pkt);
};

struct iris_hfi_gen2_inst_hfi_range {
	u32 begin;
	u32 end;
	int (*handle)(struct iris_inst *inst, struct iris_hfi_packet *pkt);
};

struct iris_hfi_gen2_packet_handle {
	enum hfi_buffer_type type;
	int (*handle)(struct iris_inst *inst, struct iris_hfi_packet *pkt);
};

/* No pending-match, payload interpretation or ownership changes in this observer. */
static bool iris_stop_failure_active(struct iris_inst *inst)
{
	struct iris_stop_failure *e = &inst->core->stop_failure;

	if (!READ_ONCE(inst->core->stop_observer_enabled) ||
	    !READ_ONCE(inst->core->decode_once) || inst->domain != DECODER ||
	    !inst->stop_attempted || e->session != inst->session_id)
		return false;
	lockdep_assert_held(&inst->lock);
	return !smp_load_acquire(&e->valid.counter);
}

void iris_stop_failure_error(struct iris_inst *inst, u32 reason, int ret)
{
	struct iris_stop_failure *e = &inst->core->stop_failure;

	if (!iris_stop_failure_active(inst) || e->reason)
		return;
	e->reason = reason;
	e->actual_ret = ret;
	e->inst_state = inst->state;
	e->core_state = READ_ONCE(inst->core->state);
	e->sub_state = inst->sub_state;
	e->ordinal = e->current_ordinal;
	e->packet_id = e->current_id;
}

static void iris_stop_failure_cursor(struct iris_inst *inst, u32 ordinal, u32 id)
{
	if (!iris_stop_failure_active(inst))
		return;
	inst->core->stop_failure.current_ordinal = ordinal;
	inst->core->stop_failure.current_id = id;
}

static void iris_stop_failure_buffer_ret(struct iris_inst *inst, int ret)
{
	struct iris_stop_failure *e = &inst->core->stop_failure;
	u32 i;

	if (!iris_stop_failure_active(inst))
		return;
	for (i = 0; i < e->count; i++)
		if (e->packet[i].header == inst->stop_rx_header &&
		    e->packet[i].ordinal == e->current_ordinal &&
		    e->packet[i].id == e->current_id)
			e->packet[i].buffer_ret = ret;
}

static void iris_stop_failure_message(struct iris_inst *inst, struct iris_hfi_header *hdr)
{
	struct iris_stop_failure *e = &inst->core->stop_failure;
	struct iris_stop_failure_packet *v;
	struct iris_hfi_packet *pkt;
	struct iris_hfi_buffer *wire;
	u8 *cursor = (u8 *)hdr + sizeof(*hdr);
	u32 i;

	if (!iris_stop_failure_active(inst)) {
		if (READ_ONCE(inst->core->stop_observer_enabled) && inst->stop_attempted &&
		    e->session == inst->session_id && smp_load_acquire(&e->valid.counter))
			atomic_inc(&e->late_ignored);
		return;
	}
	/* Full message passed the original validator and session routing, inst-lock held. */
	if (!e->reason) {
		e->header = hdr->header_id;
		e->error_session = hdr->session_id;
		e->received = hdr->size;
		e->header_size = hdr->size;
		e->num_packets = hdr->num_packets;
	}
	iris_stop_failure_cursor(inst, U32_MAX, 0);
	for (i = 0; i < hdr->num_packets; i++) {
		pkt = (void *)cursor;
		if (e->count == IRIS_STOP_FAILURE_SLOTS) {
			if (e->overflow != U32_MAX)
				e->overflow++;
			cursor += pkt->size;
			continue;
		}
		v = &e->packet[e->count++];
		v->header = hdr->header_id;
		v->session = hdr->session_id;
		v->ordinal = i;
		v->id = pkt->packet_id;
		v->type = pkt->type;
		v->port = pkt->port;
		v->flags = pkt->flags;
		v->payload = pkt->payload_info;
		v->size = pkt->size;
		v->buffer_ret = -EINPROGRESS; /* Actual buffer validator not reached yet. */
		if (pkt->type == HFI_CMD_BUFFER && pkt->payload_info == HFI_PAYLOAD_STRUCTURE) {
			wire = (void *)pkt->payload;
			v->raw_bytes = 16;
			memcpy(v->raw, pkt->payload, 16);
			v->buffer_flags = wire->flags;
		} else if (pkt->size - sizeof(*pkt) >= sizeof(u32)) {
			v->raw_bytes = sizeof(u32);
			memcpy(v->raw, pkt->payload, sizeof(u32));
		}
		cursor += pkt->size;
	}
}

static void iris_stop_failure_malformed(struct iris_core *core, const void *response,
				       u32 received, int ret)
{
	struct iris_stop_failure *e = &core->stop_failure;
	struct iris_hfi_header bounded = {};
	struct iris_inst *inst;

	if (!READ_ONCE(core->stop_observer_enabled) || !READ_ONCE(core->decode_once) ||
	    !READ_ONCE(e->session))
		return;
	/* Copy complete basic fields only within the actual received bound. */
	if (received >= sizeof(bounded.size))
		memcpy(&bounded.size, response, sizeof(bounded.size));
	if (received >= offsetof(struct iris_hfi_header, header_id))
		memcpy(&bounded.session_id, (const u8 *)response + sizeof(u32), sizeof(u32));
	if (received >= offsetof(struct iris_hfi_header, reserved))
		memcpy(&bounded.header_id, (const u8 *)response + 2 * sizeof(u32), sizeof(u32));
	if (received >= sizeof(bounded))
		memcpy(&bounded.num_packets, (const u8 *)response +
		       offsetof(struct iris_hfi_header, num_packets), sizeof(u32));
	/* Associate with the isolated STOP window, not an invented malformed session route. */
	inst = iris_get_response_instance(core, READ_ONCE(e->session));
	if (!inst)
		return;
	mutex_lock(&inst->lock);
	if (iris_stop_failure_active(inst) && !e->reason) {
		e->header = bounded.header_id;
		e->error_session = bounded.session_id;
		e->received = received;
		e->header_size = bounded.size;
		e->num_packets = bounded.num_packets;
		iris_stop_failure_cursor(inst, U32_MAX, 0);
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_MALFORMED, ret);
	} else if (smp_load_acquire(&e->valid.counter)) {
		atomic_inc(&e->late_ignored);
	}
	mutex_unlock(&inst->lock);
	iris_put_response_instance(inst);
}

void iris_stop_failure_freeze(struct iris_inst *inst, int ret)
{
	struct iris_stop_failure *e = &inst->core->stop_failure;

	if (!iris_stop_failure_active(inst))
		return;
	if (ret && !e->reason) {
		iris_stop_failure_cursor(inst, U32_MAX, 0);
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_WAIT_RETURN, ret);
	}
	/* No instance pointer retained; every slot freezes with this single release. */
	smp_store_release(&e->valid.counter, 1);
}

static u32 iris_hfi_gen2_buf_type_to_driver(struct iris_inst *inst,
					    enum hfi_buffer_type buf_type)
{
	switch (buf_type) {
	case HFI_BUFFER_BITSTREAM:
		return BUF_INPUT;
	case HFI_BUFFER_RAW:
		return BUF_OUTPUT;
	case HFI_BUFFER_BIN:
		return BUF_BIN;
	case HFI_BUFFER_ARP:
		return BUF_ARP;
	case HFI_BUFFER_COMV:
		return BUF_COMV;
	case HFI_BUFFER_NON_COMV:
		return BUF_NON_COMV;
	case HFI_BUFFER_LINE:
		return BUF_LINE;
	case HFI_BUFFER_DPB:
		if (inst->domain == DECODER || inst->core->iris_platform_data->no_opb)
			return BUF_DPB;
		else
			return BUF_SCRATCH_2;
	case HFI_BUFFER_PERSIST:
		return BUF_PERSIST;
	case HFI_BUFFER_VPSS:
		return BUF_VPSS;
	default:
		return 0;
	}
}

static bool iris_hfi_gen2_is_valid_hfi_buffer_type(u32 buffer_type)
{
	switch (buffer_type) {
	case HFI_BUFFER_BITSTREAM:
	case HFI_BUFFER_RAW:
	case HFI_BUFFER_BIN:
	case HFI_BUFFER_ARP:
	case HFI_BUFFER_COMV:
	case HFI_BUFFER_NON_COMV:
	case HFI_BUFFER_LINE:
	case HFI_BUFFER_DPB:
	case HFI_BUFFER_PERSIST:
	case HFI_BUFFER_VPSS:
		return true;
	default:
		return false;
	}
}

static bool iris_hfi_gen2_is_valid_hfi_port(u32 port, u32 buffer_type)
{
	if (port == HFI_PORT_NONE)
		return buffer_type == HFI_BUFFER_PERSIST || buffer_type == HFI_BUFFER_ARP;
	if (buffer_type == HFI_BUFFER_BITSTREAM)
		return port == HFI_PORT_BITSTREAM;
	if (buffer_type == HFI_BUFFER_RAW)
		return port == HFI_PORT_RAW;

	return port == HFI_PORT_BITSTREAM || port == HFI_PORT_RAW;
}

static int iris_hfi_gen2_get_driver_buffer_flags(struct iris_inst *inst, u32 hfi_flags)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	u32 keyframe = HFI_GEN2_PICTURE_IDR | HFI_GEN2_PICTURE_I |
		HFI_GEN2_PICTURE_CRA | HFI_GEN2_PICTURE_BLA;
	u32 driver_flags = 0;

	if (inst_hfi_gen2->hfi_frame_info.picture_type & HFI_GEN2_PICTURE_NOSHOW)
		driver_flags |= V4L2_BUF_FLAG_ERROR;
	else if (inst_hfi_gen2->hfi_frame_info.picture_type & keyframe)
		driver_flags |= V4L2_BUF_FLAG_KEYFRAME;
	else if (inst_hfi_gen2->hfi_frame_info.picture_type & HFI_GEN2_PICTURE_P)
		driver_flags |= V4L2_BUF_FLAG_PFRAME;
	else if (inst_hfi_gen2->hfi_frame_info.picture_type & HFI_GEN2_PICTURE_B)
		driver_flags |= V4L2_BUF_FLAG_BFRAME;

	if (inst_hfi_gen2->hfi_frame_info.data_corrupt || inst_hfi_gen2->hfi_frame_info.overflow)
		driver_flags |= V4L2_BUF_FLAG_ERROR;

	if (hfi_flags & HFI_BUF_FW_FLAG_LAST ||
	    hfi_flags & HFI_BUF_FW_FLAG_PSC_LAST)
		driver_flags |= V4L2_BUF_FLAG_LAST;

	return driver_flags;
}

static bool iris_hfi_gen2_validate_packet_payload(struct iris_hfi_packet *pkt)
{
	u32 payload_size = 0;

	switch (pkt->payload_info) {
	case HFI_PAYLOAD_U32:
	case HFI_PAYLOAD_S32:
	case HFI_PAYLOAD_Q16:
	case HFI_PAYLOAD_U32_ENUM:
	case HFI_PAYLOAD_32_PACKED:
		payload_size = 4;
		break;
	case HFI_PAYLOAD_U64:
	case HFI_PAYLOAD_S64:
	case HFI_PAYLOAD_64_PACKED:
		payload_size = 8;
		break;
	case HFI_PAYLOAD_STRUCTURE:
		if (pkt->type == HFI_CMD_BUFFER)
			payload_size = sizeof(struct iris_hfi_buffer);
		break;
	default:
		payload_size = 0;
		break;
	}

	if (pkt->size < sizeof(struct iris_hfi_packet) + payload_size)
		return false;

	return true;
}

static int iris_hfi_gen2_validate_packet(u8 *response_pkt, u8 *response_end)
{
	u32 size;

	if (response_pkt > response_end ||
	    response_end - response_pkt < (ptrdiff_t)sizeof(struct iris_hfi_packet))
		return -EINVAL;
	memcpy(&size, response_pkt, sizeof(size));
	if (size < sizeof(struct iris_hfi_packet) || size % sizeof(u32) ||
	    size > response_end - response_pkt)
		return -EINVAL;
	return 0;
}

static int iris_hfi_gen2_validate_hdr_packet(struct iris_core *core,
					     struct iris_hfi_header *hdr, u32 received)
{
	struct iris_hfi_packet *packet;
	u32 offset = sizeof(*hdr), i, needed;
	u8 *start = (u8 *)hdr;

	(void)core;
	if (received < sizeof(*hdr) || received > IFACEQ_CORE_PKT_SIZE ||
	    received % sizeof(u32) || hdr->size != received || !hdr->num_packets ||
	    hdr->num_packets > (received - sizeof(*hdr)) / sizeof(*packet))
		return -EINVAL;
	for (i = 0; i < hdr->num_packets; i++) {
		if (iris_hfi_gen2_validate_packet(start + offset, start + received))
			return -EINVAL;
		packet = (struct iris_hfi_packet *)(start + offset);
		if (packet->payload_info > HFI_PAYLOAD_64_PACKED ||
		    !iris_hfi_gen2_validate_packet_payload(packet))
			return -EINVAL;
		needed = 0;
		switch (packet->type) {
		case HFI_PROP_CROP_OFFSETS:
			needed = 2 * sizeof(u32);
			break;
		case HFI_PROP_IMAGE_VERSION:
			needed = IRIS_FW_VERSION_LENGTH - 1;
			break;
		case HFI_PROP_BITSTREAM_RESOLUTION:
		case HFI_PROP_LUMA_CHROMA_BIT_DEPTH:
		case HFI_PROP_CODED_FRAMES:
		case HFI_PROP_BUFFER_FW_MIN_OUTPUT_COUNT:
		case HFI_PROP_PIC_ORDER_CNT_TYPE:
		case HFI_PROP_SIGNAL_COLOR_INFO:
		case HFI_PROP_PROFILE:
		case HFI_PROP_LEVEL:
		case HFI_PROP_TIER:
		case HFI_PROP_PICTURE_TYPE:
			needed = sizeof(u32);
			break;
		case HFI_CMD_BUFFER:
			if (packet->payload_info != HFI_PAYLOAD_NONE) {
				if (packet->payload_info != HFI_PAYLOAD_STRUCTURE)
					return -EINVAL;
				needed = sizeof(struct iris_hfi_buffer);
			}
			break;
		default:
			break;
		}
		if (packet->size - sizeof(*packet) < needed)
			return -EINVAL;
		offset += packet->size;
	}
	return offset == received ? 0 : -EINVAL;
}

static int iris_hfi_gen2_handle_session_info(struct iris_inst *inst,
					     struct iris_hfi_packet *pkt)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_core *core = inst->core;
	int ret = 0;
	char *info;

	switch (pkt->type) {
	case HFI_INFO_UNSUPPORTED:
		info = "unsupported";
		break;
	case HFI_INFO_DATA_CORRUPT:
		info = "data corrupt";
		inst_hfi_gen2->hfi_frame_info.data_corrupt = 1;
		break;
	case HFI_INFO_BUFFER_OVERFLOW:
		info = "buffer overflow";
		inst_hfi_gen2->hfi_frame_info.overflow = 1;
		break;
	case HFI_INFO_HFI_FLAG_DRAIN_LAST:
		info = "drain last flag";
		ret = iris_inst_sub_state_change_drain_last(inst);
		break;
	case HFI_INFO_HFI_FLAG_PSC_LAST:
		info = "drc last flag";
		ret = iris_inst_sub_state_change_drc_last(inst);
		break;
	default:
		info = "unknown";
		break;
	}

	dev_dbg(core->dev, "session info received %#x: %s\n",
		pkt->type, info);

	return ret;
}

static void iris_hfi_gen2_fail_async(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *gen2 = to_iris_inst_hfi_gen2(inst);
	u32 i;

	if (iris_hfi_pending_active(&gen2->open_pending)) {
		if (gen2->open_status == -EINPROGRESS || !gen2->open_status)
			gen2->open_status = -EIO;
		complete(&gen2->open_done);
	}
	for (i = 0; i < ARRAY_SIZE(gen2->start_pending); i++) {
		if (iris_hfi_pending_active(&gen2->start_pending[i])) {
			if (gen2->start_status[i] == -EINPROGRESS || !gen2->start_status[i])
				gen2->start_status[i] = -EIO;
			complete(&gen2->start_done[i]);
		}
	}
}

static int iris_hfi_gen2_handle_session_error(struct iris_inst *inst,
					      struct iris_hfi_packet *pkt)
{
	struct iris_core *core = inst->core;
	char *error;

	switch (pkt->type) {
	case HFI_ERROR_MAX_SESSIONS:
		error = "exceeded max sessions";
		break;
	case HFI_ERROR_UNKNOWN_SESSION:
		error = "unknown session id";
		break;
	case HFI_ERROR_INVALID_STATE:
		error = "invalid operation for current state";
		break;
	case HFI_ERROR_INSUFFICIENT_RESOURCES:
		error = "insufficient resources";
		break;
	case HFI_ERROR_BUFFER_NOT_SET:
		error = "internal buffers not set";
		break;
	case HFI_ERROR_FATAL:
		error = "fatal error";
		break;
	case HFI_ERROR_STREAM_UNSUPPORTED:
		error = "unsupported stream";
		break;
	default:
		error = "unknown";
		break;
	}

	dev_err(core->dev, "session error received %#x: %s\n", pkt->type, error);
	iris_vb2_queue_error(inst);
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	iris_hfi_gen2_fail_async(inst);
	complete(&inst->completion);
	complete(&inst->flush_completion);

	return 0;
}

static int iris_hfi_gen2_handle_system_error(struct iris_core *core,
					     struct iris_hfi_packet *pkt)
{
	struct iris_inst *inst, *snapshot[16];
	u32 used, i;

	if (pkt)
		dev_err(core->dev, "received system error of type %#x\n", pkt->type);
	mutex_lock(&core->lock);
	core->core_ready = false;
	core->state = IRIS_CORE_ERROR;
	iris_hfi_pending_cancel(&core->init_pending);
	mutex_unlock(&core->lock);
	/* Fixed batches cannot fail allocation or omit a larger instance list. */
	for (;;) {
		used = 0;
		mutex_lock(&core->lock);
		list_for_each_entry(inst, &core->instances, list) {
			if (!inst->response_closing && !inst->response_error_notified) {
				inst->response_error_notified = true;
				refcount_inc(&inst->response_refs);
				snapshot[used++] = inst;
				if (used == ARRAY_SIZE(snapshot))
					break;
			}
		}
		mutex_unlock(&core->lock);
		if (!used)
			break;
		/* Never acquire an instance mutex while holding core->lock. */
		for (i = 0; i < used; i++) {
			inst = snapshot[i];
			mutex_lock(&inst->lock);
			if (!inst->response_closing) {
				iris_inst_change_state(inst, IRIS_INST_ERROR);
				iris_hfi_gen2_fail_async(inst);
				complete(&inst->completion);
				complete(&inst->flush_completion);
			}
			mutex_unlock(&inst->lock);
			iris_put_response_instance(inst);
		}
	}
	mutex_lock(&core->lock);
	if (!core->recovery_blocked && !core->terminal_shutdown)
		schedule_delayed_work(&core->sys_error_handler, msecs_to_jiffies(10));
	complete(&core->core_init_done);
	mutex_unlock(&core->lock);
	return 0;
}

static int iris_hfi_gen2_handle_system_init(struct iris_core *core,
					    struct iris_hfi_packet *pkt)
{
	mutex_lock(&core->lock);
	if (pkt->type != HFI_CMD_INIT || core->state != IRIS_CORE_INIT) {
		mutex_unlock(&core->lock);
		return -EINVAL;
	}
	if (iris_hfi_pending_match(&core->init_pending, pkt->packet_id, pkt->type, pkt->port)) {
		core->init_acknowledged = !!(pkt->flags & HFI_FW_FLAGS_SUCCESS);
		if (!core->init_acknowledged) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
		}
		complete(&core->core_init_done);
	}
	mutex_unlock(&core->lock);
	return 0;
}

static void iris_hfi_gen2_handle_session_close(struct iris_inst *inst,
					       struct iris_hfi_packet *pkt)
{
	if (inst->core->native_repeat &&
	    (inst->stop_rx_header != inst->repeat_close_header ||
	     pkt->packet_id != inst->repeat_close_packet ||
	     !(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
	     pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION)))
		return;
	if (!iris_hfi_pending_match(&inst->control_pending, pkt->packet_id, pkt->type, pkt->port))
		return;

	if (!(pkt->flags & HFI_FW_FLAGS_SUCCESS))
		iris_inst_change_state(inst, IRIS_INST_ERROR);
	complete(&inst->completion);
}

static int iris_hfi_gen2_validate_buffer(struct iris_buffer *buf,
                                         struct iris_hfi_buffer *buffer,
                                         size_t submitted_size)
{
	if (!(buf->attr & BUF_ATTR_QUEUED) ||
	    (buf->attr & (BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE)) ||
	    buf->index != buffer->index || buf->device_addr != buffer->base_address ||
	    buffer->addr_offset || buffer->buffer_size > submitted_size ||
	    buffer->data_offset > buffer->buffer_size ||
	    buffer->data_size > buffer->buffer_size - buffer->data_offset ||
	    buffer->data_offset > buf->buffer_size ||
	    buffer->data_size > buf->buffer_size - buffer->data_offset)
		return -EINVAL;
	return 0;
}


static void iris_stop_observer_return(struct iris_inst *inst, struct iris_buffer *buffer,
	    struct iris_hfi_buffer *wire)
{
	struct iris_stop_app *app;

	if (!inst->core->stop_observer_enabled || inst->domain != DECODER || buffer->index >= VIDEO_MAX_FRAME)
		return;
	lockdep_assert_held(&inst->lock);
	app = &inst->stop_app[buffer->type == BUF_OUTPUT][buffer->index];
	app->returned++;
	app->readonly = !!(wire->flags & HFI_BUF_FW_FLAG_READONLY);
	app->rx_header = inst->stop_rx_header;
	app->rx_packet = inst->stop_rx_packet;
	app->rx_flags = inst->stop_rx_flags;
	app->rx_buffer_flags = wire->flags;
	if (inst->stop_attempted) {
		u32 plane, index;
		bool all = true;
		for (plane = 0; plane<2; plane++)
			for (index = 0; index<VIDEO_MAX_FRAME; index++)
		if (inst->stop_app[plane][index].submissions > inst->stop_app[plane][index].returned)
		all = false;
		if (all)complete(&inst->stop_app_returned);
	}
	/* Observation before attr mutation. Always continue original completion. */
}

static int iris_repeat_app_return(struct iris_inst *inst, struct iris_buffer *buf,
				  struct iris_hfi_buffer *wire);

static int iris_hfi_gen2_handle_input_buffer(struct iris_inst *inst,
					     struct iris_hfi_buffer *buffer)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct v4l2_m2m_buffer *m2m_buffer, *n;
	struct iris_buffer *buf;
	bool found = false;
	int ret;
	bool observe = READ_ONCE(inst->core->decode_once) && inst->domain == DECODER;

	if (observe)
		atomic_inc(&inst->core->decode_progress_in_enter);
	v4l2_m2m_for_each_src_buf_safe(m2m_ctx, m2m_buffer, n) {
		buf = to_iris_buffer(&m2m_buffer->vb);
		if (buf->index == buffer->index) {
			found = true;
			break;
		}
	}
	if (!found) {
		if (observe)
			atomic_inc(&inst->core->decode_progress_in_err);
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_INPUT_MISSING, -EINVAL);
		return -EINVAL;
	}

	ret = iris_hfi_gen2_validate_buffer(buf, buffer,
		inst->domain == DECODER ? ALIGN(buf->buffer_size, 256) : buf->buffer_size);
	iris_stop_failure_buffer_ret(inst, ret);
	if (ret) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_INPUT_VALIDATE, ret);
		if (observe)
			atomic_inc(&inst->core->decode_progress_in_err);
		return -EINVAL;
	}

	if ((inst->core->stop_observer_enabled || inst->core->native_repeat) &&
	    (buffer->flags & ~(HFI_BUF_FW_FLAG_READONLY | HFI_BUF_FW_FLAG_LAST | HFI_BUF_FW_FLAG_PSC_LAST))) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_INPUT_FLAGS, -EINVAL);
		return -EINVAL;
	}
	if (inst->core->native_repeat) {
		ret = iris_repeat_app_return(inst, buf, buffer);
		if (ret)
			return ret == 1 ? 0 : ret;
	}
	iris_stop_observer_return(inst,buf,buffer);
	buf->attr &= ~BUF_ATTR_QUEUED;
	buf->attr |= BUF_ATTR_DEQUEUED;

	buf->flags = iris_hfi_gen2_get_driver_buffer_flags(inst, buffer->flags);

	return 0;
}

static int iris_hfi_gen2_handle_output_buffer(struct iris_inst *inst,
					      struct iris_hfi_buffer *hfi_buffer)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct v4l2_m2m_buffer *m2m_buffer, *n;
	struct iris_buffer *buf;
	bool found = false;
	int ret;

	v4l2_m2m_for_each_dst_buf_safe(m2m_ctx, m2m_buffer, n) {
		buf = to_iris_buffer(&m2m_buffer->vb);
		if (buf->index == hfi_buffer->index &&
		    buf->device_addr == hfi_buffer->base_address) {
			found = true;
			break;
		}
	}
	if (!found) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_OUTPUT_MISSING, -EINVAL);
		return -EINVAL;
	}

	ret = iris_hfi_gen2_validate_buffer(buf, hfi_buffer, buf->buffer_size);
	iris_stop_failure_buffer_ret(inst, ret);
	if (ret) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_OUTPUT_VALIDATE, ret);
		return ret;
	}

	if ((inst->core->stop_observer_enabled || inst->core->native_repeat) &&
	    (hfi_buffer->flags & ~(HFI_BUF_FW_FLAG_READONLY | HFI_BUF_FW_FLAG_LAST | HFI_BUF_FW_FLAG_PSC_LAST))) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_OUTPUT_FLAGS, -EINVAL);
		return -EINVAL;
	}
	if (inst->core->native_repeat) {
		ret = iris_repeat_app_return(inst, buf, hfi_buffer);
		if (ret)
			return ret == 1 ? 0 : ret;
	}
	if (hfi_buffer->flags & HFI_BUF_FW_FLAG_LAST) {
		ret = iris_inst_sub_state_change_drain_last(inst);
		if (ret) {
			iris_stop_failure_error(inst, IRIS_STOP_FAILURE_OUTPUT_DRAIN, ret);
			return ret;
		}
	}

	if (hfi_buffer->flags & HFI_BUF_FW_FLAG_PSC_LAST) {
		ret = iris_inst_sub_state_change_drc_last(inst);
		if (ret) {
			iris_stop_failure_error(inst, IRIS_STOP_FAILURE_OUTPUT_DRC, ret);
			return ret;
		}
	}

	iris_stop_observer_return(inst,buf,hfi_buffer);
	buf->data_offset = hfi_buffer->data_offset;
	buf->data_size = hfi_buffer->data_size;
	buf->timestamp = hfi_buffer->timestamp;

	buf->attr &= ~BUF_ATTR_QUEUED;
	buf->attr |= BUF_ATTR_DEQUEUED;

	buf->flags = iris_hfi_gen2_get_driver_buffer_flags(inst, hfi_buffer->flags);

	if (!buf->data_size && inst->state == IRIS_INST_STREAMING &&
	    !(hfi_buffer->flags & (HFI_BUF_FW_FLAG_LAST | HFI_BUF_FW_FLAG_PSC_LAST))) {
		buf->flags |= V4L2_BUF_FLAG_ERROR;
	}

	return 0;
}

static int iris_hfi_gen2_handle_dequeue_buffers(struct iris_inst *inst)
{
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;
	struct v4l2_m2m_buffer *buffer, *n;
	struct iris_buffer *buf = NULL;
	int ret;

	v4l2_m2m_for_each_src_buf_safe(m2m_ctx, buffer, n) {
		buf = to_iris_buffer(&buffer->vb);
		if (buf->attr & BUF_ATTR_DEQUEUED) {
			buf->attr &= ~BUF_ATTR_DEQUEUED;
			if (!(buf->attr & BUF_ATTR_BUFFER_DONE)) {
				ret = iris_vb2_buffer_done(inst, buf);
				if (READ_ONCE(inst->core->decode_once) && inst->domain == DECODER) {
					if (ret)
						atomic_inc(&inst->core->decode_progress_in_err);
					else
						atomic_inc(&inst->core->decode_progress_in_done);
				}
				if (ret)
					return ret;
				buf->attr |= BUF_ATTR_BUFFER_DONE;
				if (inst->state == IRIS_INST_ERROR)
					return -EIO;
			}
		}
	}

	v4l2_m2m_for_each_dst_buf_safe(m2m_ctx, buffer, n) {
		buf = to_iris_buffer(&buffer->vb);
		if (buf->attr & BUF_ATTR_DEQUEUED) {
			buf->attr &= ~BUF_ATTR_DEQUEUED;
			if (!(buf->attr & BUF_ATTR_BUFFER_DONE)) {
				ret = iris_vb2_buffer_done(inst, buf);
				if (ret)
					return ret;
				buf->attr |= BUF_ATTR_BUFFER_DONE;
				if (inst->state == IRIS_INST_ERROR)
					return -EIO;
			}
		}
	}
	return 0;
}


/* Retained-only STOP return, not RELEASE_DONE or a DMA ownership handoff.
 * Exact valid duplicates are accepted only before the ACK while this STOP is active.
 * Each is recorded in the bounded failure packet log (raw type/index, buffer_ret == 0).
 * Cancelled/replaced transactions and frozen results never enter this path.
 */
static int iris_repeat_internal_return(struct iris_inst *inst, struct iris_hfi_packet *pkt)
{
	struct iris_hfi_buffer *wire = (void *)pkt->payload;
	struct iris_buffer *buf, *match = NULL;
	struct iris_stop_transaction *tx;
	struct iris_hfi_pending *pending = &inst->control_pending;
	u32 type, stop_header = 0, stop_packet = 0;
	unsigned long flags;
	bool release, bound;
	int ret;

	if (pkt->payload_info != HFI_PAYLOAD_STRUCTURE ||
	    pkt->size != sizeof(*pkt) + sizeof(*wire) ||
	    !(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
	    pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION))
		return -EINVAL;
	type = iris_hfi_gen2_buf_type_to_driver(inst, wire->type);
	if (type <= BUF_OUTPUT || type >= BUF_TYPE_MAX ||
	    (type != BUF_BIN && type != BUF_COMV && type != BUF_NON_COMV &&
	     type != BUF_LINE && type != BUF_DPB && type != BUF_PERSIST) ||
	    pkt->port != iris_hfi_gen2_get_port_from_buf_type(inst, type) ||
	    wire->addr_offset || wire->data_offset || wire->data_size ||
	    wire->flags & ~(HFI_BUF_FW_FLAG_RELEASE_DONE | HFI_BUF_FW_FLAG_READONLY))
		return -EINVAL;
	list_for_each_entry(buf, &inst->buffers[type].list, list)
		if (buf->type == type && buf->index == wire->index &&
		    buf->device_addr == wire->base_address && buf->buffer_size == wire->buffer_size) {
			if (match)
				return -EINVAL;
			match = buf;
		}
	if (!match || !match->repeat_owner)
		return -ESTALE;
	if (match->experiment_retired || !(match->attr & BUF_ATTR_QUEUED) ||
	    match->attr & (BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE | BUF_ATTR_DEFERRED |
			   BUF_ATTR_RETURNED) ||
	    !!(match->attr & BUF_ATTR_PENDING_RELEASE) != match->repeat_owner->release_requested ||
	    !!(match->attr & BUF_ATTR_READONLY) != match->repeat_owner->readonly ||
	    iris_hfi_gen2_validate_buffer(match, wire, match->buffer_size))
		return -ESTALE;
	release = !!(wire->flags & HFI_BUF_FW_FLAG_RELEASE_DONE);
	if (!release) {
		/* Measured STOP contract returns no buffer flags, unlike RELEASE_DONE. */
		if (wire->flags || match->attr & BUF_ATTR_PENDING_RELEASE)
			return -ESTALE;
		/* PERSIST STOP ownership has not been measured. Never infer its return. */
		if (type == BUF_PERSIST || !inst->stop_attempted)
			return -ESTALE;
		tx = &inst->repeat_stop[pkt->port == HFI_PORT_RAW];
		spin_lock_irqsave(&pending->lock, flags);
		bound = pending->active && !pending->responded && pending->type == HFI_CMD_STOP &&
			pending->packet_id == tx->request_packet && pending->port == pkt->port;
		spin_unlock_irqrestore(&pending->lock, flags);
		if (!bound || tx->queue_rc || tx->port != pkt->port)
			return -ESTALE;
		if (type != BUF_DPB) {
			stop_header = tx->request_header;
			stop_packet = tx->request_packet;
		}
	}
	ret = iris_repeat_owner_return(match->repeat_owner, inst->stop_rx_header, pkt->packet_id,
		wire->type, pkt->port, wire->base_address, wire->buffer_size, release,
		!!(wire->flags & HFI_BUF_FW_FLAG_READONLY), stop_header, stop_packet);
	match->repeat_owner->rx_header = inst->stop_rx_header;
	match->repeat_owner->rx_packet = pkt->packet_id;
	match->repeat_owner->rx_flags = pkt->flags;
	match->repeat_owner->rx_buffer_flags = wire->flags;
	match->repeat_owner->rx_generation = match->repeat_owner->generation;
	match->repeat_owner->rx_result = ret;
	if (ret < 0)
		return ret;
	if (ret) {
		match->attr |= BUF_ATTR_READONLY;
		return 0;
	}
	match->attr &= ~(BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE | BUF_ATTR_READONLY |
			 BUF_ATTR_DEFERRED | BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE);
	match->attr |= BUF_ATTR_RETURNED;
	/* PSC release retires this generation, not its linked physical storage. */
	if (release && !inst->stop_submit_blocked)
		match->experiment_retired = true;
	if (!iris_repeat_internal_owned(inst))
		complete(&inst->repeat_internal_returned);
	return 0;
}

static int iris_repeat_app_return(struct iris_inst *inst, struct iris_buffer *buf,
				  struct iris_hfi_buffer *wire)
{
	int ret;

	ret = iris_repeat_owner_return(&inst->repeat_app[buf->type == BUF_OUTPUT][buf->index],
		inst->stop_rx_header, inst->stop_rx_packet, wire->type,
		iris_hfi_gen2_get_port_from_buf_type(inst, buf->type), wire->base_address,
		wire->buffer_size, false, !!(wire->flags & HFI_BUF_FW_FLAG_READONLY), 0, 0);
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_header = inst->stop_rx_header;
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_packet = inst->stop_rx_packet;
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_flags = inst->stop_rx_flags;
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_buffer_flags = wire->flags;
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_generation =
		inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].generation;
	inst->repeat_app[buf->type == BUF_OUTPUT][buf->index].rx_result = ret;
	if (ret == 1)
		buf->attr |= BUF_ATTR_READONLY;
	else if (!ret)
		buf->attr &= ~BUF_ATTR_READONLY;
	if (!ret && !iris_repeat_apps_owned(inst, -1))
		complete(&inst->stop_app_returned);
	return ret;
}

static int iris_stop_internal_return_consume(struct iris_inst *inst,
					   struct iris_hfi_packet *pkt)
{
	struct iris_stop_transaction *tx;
	struct iris_hfi_pending *pending = &inst->control_pending;
	struct iris_hfi_buffer *wire;
	struct iris_buffer *buf, *match = NULL;
	unsigned long flags;
	bool bound, dpb;
	u32 type;

	if (!iris_stop_failure_active(inst) ||
	    smp_load_acquire(&inst->core->stop_evidence.result_valid.counter) ||
	    inst->response_closing || inst->state == IRIS_INST_ERROR ||
	    inst->core->state != IRIS_CORE_INIT || pkt->type != HFI_CMD_BUFFER ||
	    pkt->payload_info != HFI_PAYLOAD_STRUCTURE ||
	    pkt->size != sizeof(*pkt) + sizeof(*wire) ||
	    !(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
	    pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION) ||
	    (pkt->port != HFI_PORT_BITSTREAM && pkt->port != HFI_PORT_RAW))
		return 0;
	lockdep_assert_held(&inst->lock);
	wire = (void *)pkt->payload;
	dpb = wire->type == HFI_BUFFER_DPB && pkt->port == HFI_PORT_RAW;
	tx = &inst->core->stop_evidence.transaction[pkt->port == HFI_PORT_RAW];
	if (tx->queue_rc || tx->port != pkt->port || !tx->request_packet ||
	    (!dpb && (tx->request_header != inst->stop_rx_header ||
		      tx->request_packet != pkt->packet_id)))
		return 0;
	/* Peek under its lock: pending_match would consume the later STOP ACK. */
	spin_lock_irqsave(&pending->lock, flags);
	bound = pending->active && !pending->responded && pending->type == HFI_CMD_STOP &&
		pending->packet_id == tx->request_packet && pending->port == pkt->port;
	spin_unlock_irqrestore(&pending->lock, flags);
	if (!bound)
		return 0;
	/* RAW STOP returns DPB with its initial BUFFER identity, not the STOP ID.
	 * Keep this distinct from bitstream's four STOP-correlated internal returns.
	 * Neither path transfers ownership or releases physical backing here.
	 */
	if (!dpb && wire->type != HFI_BUFFER_LINE && wire->type != HFI_BUFFER_COMV &&
	    wire->type != HFI_BUFFER_NON_COMV && wire->type != HFI_BUFFER_BIN)
		return 0;
	if (wire->flags)
		return 0;
	type = iris_hfi_gen2_buf_type_to_driver(inst, wire->type);
	if (pkt->port != iris_hfi_gen2_get_port_from_buf_type(inst, type))
		return 0;
	list_for_each_entry(buf, &inst->buffers[type].list, list) {
		if (buf->type != type || buf->index != wire->index ||
		    buf->device_addr != wire->base_address)
			continue;
		if (dpb && (!buf->observer_first_valid || buf->observer_first_rc ||
			    buf->observer_first_header_id != inst->stop_rx_header ||
			    buf->observer_first_packet_id != pkt->packet_id))
			return 0;
		if (match || buf->experiment_retired ||
		    (buf->attr & (BUF_ATTR_PENDING_RELEASE | BUF_ATTR_DEQUEUED |
				  BUF_ATTR_BUFFER_DONE | BUF_ATTR_READONLY | BUF_ATTR_DEFERRED)) ||
		    wire->buffer_size != buf->buffer_size ||
		    iris_hfi_gen2_validate_buffer(buf, wire, buf->buffer_size))
			return 0;
		match = buf;
	}
	if (!match)
		return 0;
	iris_stop_failure_buffer_ret(inst, 0);
	/* No attr, ownership, retirement, allocation, PM or submission mutation. */
	return 1;
}

/* 1 consumes only a syntactically valid RELEASE_DONE for the exact frozen object.
 * 0 delegates untouched to normal handling. No observation is a DMA handoff.
 * Called after full-header validation, session routing and generic error checks.
 */
static int iris_release_observer_consume(struct iris_inst *inst,
	struct iris_hfi_header *header, struct iris_hfi_packet *pkt)
{
	struct iris_release_observer_request request;
	struct iris_release_observer_reply value = {};
	struct iris_release_observer_slot *slot;
	struct iris_hfi_buffer *wire;
	struct iris_buffer *buffer;
	u32 type;
	int index;

	if (!READ_ONCE(inst->core->release_observer_enabled) ||
	    !READ_ONCE(inst->core->decode_once) || inst->domain != DECODER ||
	    header->session_id != inst->session_id || inst->response_closing ||
	    inst->state == IRIS_INST_ERROR || inst->core->state != IRIS_CORE_INIT ||
	    pkt->type != HFI_CMD_BUFFER || pkt->payload_info != HFI_PAYLOAD_STRUCTURE ||
	    pkt->size != sizeof(*pkt) + sizeof(*wire) ||
	    !(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
	    pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION))
		return 0;
	lockdep_assert_held(&inst->lock);
	wire = (void *)((u8 *)pkt + sizeof(*pkt));
	if (!(wire->flags & HFI_BUF_FW_FLAG_RELEASE_DONE) ||
	    wire->flags & ~(HFI_BUF_FW_FLAG_RELEASE_DONE | HFI_BUF_FW_FLAG_READONLY) ||
	    wire->addr_offset || wire->data_offset || wire->data_size)
		return 0;
	type = iris_hfi_gen2_buf_type_to_driver(inst, wire->type);
	index = iris_release_observer_index(type);
	if (index < 0 || pkt->port != iris_hfi_gen2_get_port_from_buf_type(inst, type))
		return 0;
	slot = &inst->core->release_observer[index];
	if (!smp_load_acquire(&slot->request_valid.counter))
		return 0;
	request = slot->request;
	if (request.queue_rc || !request.first_valid || request.first_rc ||
	    request.session_id != header->session_id || request.type != HFI_CMD_BUFFER ||
	    request.port != pkt->port || request.index != wire->index ||
	    request.addr_lo != (u32)wire->base_address ||
	    request.addr_hi != (u32)(wire->base_address >> 32) || request.size != wire->buffer_size)
		return 0;
	list_for_each_entry(buffer, &inst->buffers[type].list, list) {
		if (buffer->type != type || buffer->index != wire->index ||
		    buffer->device_addr != wire->base_address || buffer->buffer_size != wire->buffer_size)
			continue;
		if (iris_hfi_gen2_validate_buffer(buffer, wire, buffer->buffer_size) ||
		    (buffer->attr & (BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE)) !=
		    (BUF_ATTR_QUEUED | BUF_ATTR_PENDING_RELEASE))
			return 0;
		value.session_id = header->session_id;
		value.header_id = header->header_id;
		value.packet_id = pkt->packet_id;
		value.type = pkt->type;
		value.port = pkt->port;
		value.flags = pkt->flags;
		value.payload = pkt->payload_info;
		value.buffer_type = wire->type;
		value.buffer_flags = wire->flags;
		value.index = wire->index;
		value.addr_lo = (u32)wire->base_address;
		value.addr_hi = (u32)(wire->base_address >> 32);
		value.size = wire->buffer_size;
		value.addr_offset = wire->addr_offset;
		value.data_offset = wire->data_offset;
		value.data_size = wire->data_size;
		iris_release_observer_publish_reply(slot, &value);
		/* Including valid duplicates: no queued/pending/readonly changes, no retire,
 * free, resubmit or PM operation. Wrong IDs are observations, not failures.
 */
		return 1;
	}
	return 0;
}

static int iris_hfi_gen2_handle_release_internal_buffer(struct iris_inst *inst,
						struct iris_hfi_packet *pkt,
						struct iris_hfi_buffer *buffer)
{
	u32 buf_type = iris_hfi_gen2_buf_type_to_driver(inst, buffer->type);
	struct iris_buffers *buffers;
	struct iris_buffer *buf;
	int ret;

	if (inst->core->native_repeat)
		return -EINVAL;

	/* Observer accepts only bound valid replies before this fallback. Never
	 * let an unconsumed/malformed internal reply clear retained ownership.
	 * Returning an error preserves the normal generic session-failure path.
	 */
	if (READ_ONCE(inst->core->release_observer_enabled))
		return -EINVAL;

	/* Validate before indexing: unknown HFI types must never select list zero. */
	if (buf_type <= BUF_OUTPUT || buf_type >= BUF_TYPE_MAX ||
	    (buf_type == BUF_VPSS && inst->domain != ENCODER) ||
	    (buf_type == BUF_ARP && inst->domain != ENCODER) ||
	    (buf_type == BUF_PERSIST && inst->domain != DECODER) ||
	    pkt->port != iris_hfi_gen2_get_port_from_buf_type(inst, buf_type))
		return -EINVAL;

	buffers = &inst->buffers[buf_type];
	list_for_each_entry(buf, &buffers->list, list) {
		if (buf->type != buf_type || buf->index != buffer->index ||
		    buf->device_addr != buffer->base_address)
			continue;
		if (iris_hfi_gen2_validate_buffer(buf, buffer, buf->buffer_size) ||
		    buffer->buffer_size != buf->buffer_size ||
		    buffer->flags & ~(HFI_BUF_FW_FLAG_RELEASE_DONE | HFI_BUF_FW_FLAG_READONLY))
			return -EINVAL;
		/* READONLY is not a DMA-ownership handoff, even with RELEASE_DONE. */
		if (buffer->flags & HFI_BUF_FW_FLAG_READONLY) {
			ret = iris_mark_internal_buffer_returned(buf, true);
			return ret ? ret : -EOPNOTSUPP;
		}
		if (!(buffer->flags & HFI_BUF_FW_FLAG_RELEASE_DONE) ||
		    !(buf->attr & BUF_ATTR_PENDING_RELEASE))
			return -EINVAL;
		ret = iris_mark_internal_buffer_returned(buf, false);
		if (ret)
			return ret;
		if (READ_ONCE(inst->core->decode_once)) {
			/* Exact validated RELEASE_DONE retires this generation for reuse,
			 * not its physical allocation. Keep list/index/backing until reset.
			 */
			buf->experiment_retired = true;
			return 0;
		}
		return iris_destroy_internal_buffer(inst, buf);
	}
	return -EINVAL;
}

static int iris_hfi_gen2_handle_session_stop(struct iris_inst *inst,
					     struct iris_hfi_packet *pkt)
{
	int ret = 0;
	if (inst->core->native_repeat) {
		struct iris_stop_transaction *tx;

		if (pkt->port != HFI_PORT_RAW && pkt->port != HFI_PORT_BITSTREAM)
			return -EINVAL;
		tx = &inst->repeat_stop[pkt->port == HFI_PORT_RAW];
		if (tx->queue_rc || tx->request_header != inst->stop_rx_header ||
		    tx->request_packet != pkt->packet_id || tx->port != pkt->port ||
		    !(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
		    pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION))
			return -ESTALE;
		tx->ack_header = inst->stop_rx_header;
		tx->ack_packet = pkt->packet_id;
		tx->ack_flags = pkt->flags;
		tx->ack = 1;
	}

	if (!iris_hfi_pending_match(&inst->control_pending, pkt->packet_id, pkt->type, pkt->port))
		return 0;


	if (inst->core->stop_observer_enabled && inst->stop_attempted &&
	    !smp_load_acquire(&inst->core->stop_evidence.result_valid.counter)) {
		struct iris_stop_transaction *tx = &inst->core->stop_evidence.transaction[pkt->port == HFI_PORT_RAW];

		tx->ack_header = inst->stop_rx_header;
		tx->ack_packet = pkt->packet_id;
		tx->ack_flags = pkt->flags;
		tx->ack = 1;
	}

	if (!(pkt->flags & HFI_FW_FLAGS_SUCCESS)) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_STOP_SUCCESS, -EIO);
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		complete(&inst->completion);
		return -EIO;
	}

	if (pkt->port == HFI_PORT_RAW)
		ret = iris_inst_sub_state_change_pause(inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
	else if (pkt->port == HFI_PORT_BITSTREAM)
		ret = iris_inst_sub_state_change_pause(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);

	if (ret)
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_STOP_PAUSE, ret);
	complete(&inst->completion);

	return ret;
}

static int iris_hfi_gen2_handle_session_buffer(struct iris_inst *inst,
					       struct iris_hfi_packet *pkt)
{
	struct iris_hfi_buffer *buffer;

	if (pkt->payload_info != HFI_PAYLOAD_STRUCTURE ||
	    !iris_hfi_gen2_validate_packet_payload(pkt)) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_BUFFER_PAYLOAD, -EINVAL);
		return -EINVAL;
	}

	if (inst->core->stop_observer_enabled || inst->core->native_repeat) {
		if (!(pkt->flags & HFI_FW_FLAGS_SUCCESS) ||
		    (pkt->flags & ~(HFI_FW_FLAGS_SUCCESS | HFI_FW_FLAGS_INFORMATION))) {
			iris_stop_failure_error(inst, IRIS_STOP_FAILURE_PACKET_FLAGS, -EINVAL);
			return -EINVAL;
		}
		inst->stop_rx_packet = pkt->packet_id;
		inst->stop_rx_flags = pkt->flags;
	}
	buffer = (struct iris_hfi_buffer *)((u8 *)pkt + sizeof(*pkt));
	if (!iris_hfi_gen2_is_valid_hfi_buffer_type(buffer->type)) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_BUFFER_TYPE, -EINVAL);
		return -EINVAL;
	}

	if (!iris_hfi_gen2_is_valid_hfi_port(pkt->port, buffer->type)) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_BUFFER_PORT, -EINVAL);
		return -EINVAL;
	}

	if (inst->domain == DECODER) {
		if (buffer->type == HFI_BUFFER_BITSTREAM)
			return iris_hfi_gen2_handle_input_buffer(inst, buffer);
		else if (buffer->type == HFI_BUFFER_RAW)
			return iris_hfi_gen2_handle_output_buffer(inst, buffer);
		else
			return iris_hfi_gen2_handle_release_internal_buffer(inst, pkt, buffer);
	} else {
		if (buffer->type == HFI_BUFFER_RAW)
			return iris_hfi_gen2_handle_input_buffer(inst, buffer);
		else if (buffer->type == HFI_BUFFER_BITSTREAM)
			return iris_hfi_gen2_handle_output_buffer(inst, buffer);
		else
			return iris_hfi_gen2_handle_release_internal_buffer(inst, pkt, buffer);
	}
	return 0;
}

static int iris_hfi_gen2_handle_session_drain(struct iris_inst *inst,
					      struct iris_hfi_packet *pkt)
{
	int ret = 0;

	if (!(pkt->flags & HFI_FW_FLAGS_SUCCESS)) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		return 0;
	}

	if (inst->sub_state & IRIS_INST_SUB_DRAIN)
		ret = iris_inst_change_sub_state(inst, 0, IRIS_INST_SUB_INPUT_PAUSE);

	return ret;
}

static int iris_hfi_gen2_read_input_subcr_params(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct v4l2_pix_format_mplane *pixmp_ip = &inst->fmt_src->fmt.pix_mp;
	struct v4l2_pix_format_mplane *pixmp_op = &inst->fmt_dst->fmt.pix_mp;
	u32 primaries, matrix_coeff, transfer_char;
	struct hfi_subscription_params subsc_params;
	u32 colour_description_present_flag;
	u32 video_signal_type_present_flag;
	struct iris_core *core = inst->core;
	u32 full_range, width, height;
	struct vb2_queue *dst_q;
	struct v4l2_ctrl *ctrl;

	subsc_params = inst_hfi_gen2->src_subcr_params;
	width = (subsc_params.bitstream_resolution &
		HFI_BITMASK_BITSTREAM_WIDTH) >> 16;
	height = subsc_params.bitstream_resolution &
		HFI_BITMASK_BITSTREAM_HEIGHT;

	if (!width || !height || width > 8192 || height > 8192 ||
	    subsc_params.bit_depth != BIT_DEPTH_8 ||
	    !(subsc_params.coded_frames & HFI_BITMASK_FRAME_MBS_ONLY_FLAG) ||
	    (subsc_params.crop_offsets[0] & 0xffff) +
	    (subsc_params.crop_offsets[1] & 0xffff) >= height ||
	    (subsc_params.crop_offsets[0] >> 16) +
	    (subsc_params.crop_offsets[1] >> 16) >= width) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		return -EINVAL;
	}
	pixmp_ip->width = width;
	pixmp_ip->height = height;

	if (iris_apply_capture_layout(inst, width, height)) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		return -EINVAL;
	}

	matrix_coeff = subsc_params.color_info & 0xFF;
	transfer_char = (subsc_params.color_info & 0xFF00) >> 8;
	primaries = (subsc_params.color_info & 0xFF0000) >> 16;
	colour_description_present_flag =
		(subsc_params.color_info & 0x1000000) >> 24;
	full_range = (subsc_params.color_info & 0x2000000) >> 25;
	video_signal_type_present_flag =
		(subsc_params.color_info & 0x20000000) >> 29;

	pixmp_op->colorspace = V4L2_COLORSPACE_DEFAULT;
	pixmp_op->xfer_func = V4L2_XFER_FUNC_DEFAULT;
	pixmp_op->ycbcr_enc = V4L2_YCBCR_ENC_DEFAULT;
	pixmp_op->quantization = V4L2_QUANTIZATION_DEFAULT;

	if (video_signal_type_present_flag) {
		pixmp_op->quantization =
			full_range ?
			V4L2_QUANTIZATION_FULL_RANGE :
			V4L2_QUANTIZATION_LIM_RANGE;
		if (colour_description_present_flag) {
			pixmp_op->colorspace =
				iris_hfi_get_v4l2_color_primaries(primaries);
			pixmp_op->xfer_func =
				iris_hfi_get_v4l2_transfer_char(transfer_char);
			pixmp_op->ycbcr_enc =
				iris_hfi_get_v4l2_matrix_coefficients(matrix_coeff);
		}
	}

	pixmp_ip->colorspace = pixmp_op->colorspace;
	pixmp_ip->xfer_func = pixmp_op->xfer_func;
	pixmp_ip->ycbcr_enc = pixmp_op->ycbcr_enc;
	pixmp_ip->quantization = pixmp_op->quantization;

	inst->crop.top = subsc_params.crop_offsets[0] & 0xFFFF;
	inst->crop.left = (subsc_params.crop_offsets[0] >> 16) & 0xFFFF;
	inst->crop.height = pixmp_ip->height -
		(subsc_params.crop_offsets[1] & 0xFFFF) - inst->crop.top;
	inst->crop.width = pixmp_ip->width -
		((subsc_params.crop_offsets[1] >> 16) & 0xFFFF) - inst->crop.left;

	switch (inst->codec) {
	case V4L2_PIX_FMT_HEVC:
		inst->fw_caps[PROFILE_HEVC].value = subsc_params.profile;
		inst->fw_caps[LEVEL_HEVC].value = subsc_params.level;
		break;
	case V4L2_PIX_FMT_VP9:
		inst->fw_caps[PROFILE_VP9].value = subsc_params.profile;
		inst->fw_caps[LEVEL_VP9].value = subsc_params.level;
		break;
	case V4L2_PIX_FMT_H264:
		inst->fw_caps[PROFILE_H264].value = subsc_params.profile;
		inst->fw_caps[LEVEL_H264].value = subsc_params.level;
		break;
	}

	inst->fw_caps[POC].value = subsc_params.pic_order_cnt;
	inst->fw_caps[TIER].value = subsc_params.tier;

	if (subsc_params.bit_depth != BIT_DEPTH_8 ||
	    !(subsc_params.coded_frames & HFI_BITMASK_FRAME_MBS_ONLY_FLAG)) {
		dev_err(core->dev, "unsupported content, bit depth: %x, pic_struct = %x\n",
			subsc_params.bit_depth, subsc_params.coded_frames);
		iris_inst_change_state(inst, IRIS_INST_ERROR);
	}

	inst->fw_min_count = subsc_params.fw_min_count;
	inst->buffers[BUF_OUTPUT].min_count = iris_vpu_buf_count(inst, BUF_OUTPUT);
	inst->buffers[BUF_OUTPUT].size = pixmp_op->plane_fmt[0].sizeimage;
	ctrl = v4l2_ctrl_find(&inst->ctrl_handler, V4L2_CID_MIN_BUFFERS_FOR_CAPTURE);
	if (ctrl)
		v4l2_ctrl_s_ctrl(ctrl, inst->buffers[BUF_OUTPUT].min_count);

	dst_q = v4l2_m2m_get_dst_vq(inst->m2m_ctx);
	dst_q->min_reqbufs_allocation = inst->buffers[BUF_OUTPUT].min_count;
	return 0;
}

static int iris_hfi_gen2_handle_src_change(struct iris_inst *inst,
					   struct iris_hfi_packet *pkt)
{
	int ret;

	if (pkt->port != HFI_PORT_BITSTREAM)
		return 0;

	ret = iris_inst_sub_state_change_drc(inst);
	if (ret)
		return ret;

	ret = iris_hfi_gen2_read_input_subcr_params(inst);
	if (ret)
		return ret;
	iris_vdec_src_change(inst);

	return 0;
}

static int iris_hfi_gen2_handle_session_async(struct iris_inst *inst,
					     struct iris_hfi_packet *pkt)
{
	struct iris_inst_hfi_gen2 *gen2 = to_iris_inst_hfi_gen2(inst);
	struct iris_hfi_pending *pending;
	struct completion *done;
	int *status;

	if (pkt->type == HFI_CMD_OPEN) {
		pending = &gen2->open_pending;
		done = &gen2->open_done;
		status = &gen2->open_status;
	} else if (pkt->type == HFI_CMD_START) {
		u32 index;

		if (pkt->port != HFI_PORT_BITSTREAM && pkt->port != HFI_PORT_RAW)
			return -EINVAL;
		index = pkt->port == HFI_PORT_RAW;
		pending = &gen2->start_pending[index];
		done = &gen2->start_done[index];
		status = &gen2->start_status[index];
	} else {
		return -EINVAL;
	}
	if (!iris_hfi_pending_match(pending, pkt->packet_id, pkt->type, pkt->port))
		return 0;
	*status = pkt->flags & HFI_FW_FLAGS_SUCCESS ? 0 : -EIO;
	if (*status) {
		iris_inst_change_state(inst, IRIS_INST_ERROR);
		iris_vb2_queue_error(inst);
		complete(&inst->completion);
		complete(&inst->flush_completion);
	}
	/* Retain transaction ownership until the waiter has reconciled its result. */
	complete(done);
	return *status;
}

static int iris_hfi_gen2_handle_session_command(struct iris_inst *inst,
						struct iris_hfi_packet *pkt)
{
	int ret = 0;

	switch (pkt->type) {
	case HFI_CMD_OPEN:
	case HFI_CMD_START:
		ret = iris_hfi_gen2_handle_session_async(inst, pkt);
		break;
	case HFI_CMD_CLOSE:
		iris_hfi_gen2_handle_session_close(inst, pkt);
		break;
	case HFI_CMD_STOP:
		ret = iris_hfi_gen2_handle_session_stop(inst, pkt);
		break;
	case HFI_CMD_BUFFER:
		ret = iris_hfi_gen2_handle_session_buffer(inst, pkt);
		break;
	case HFI_CMD_SETTINGS_CHANGE:
		ret = iris_hfi_gen2_handle_src_change(inst, pkt);
		break;
	case HFI_CMD_DRAIN:
		ret = iris_hfi_gen2_handle_session_drain(inst, pkt);
		break;
	default:
		break;
	}

	return ret;
}

static int iris_hfi_gen2_handle_session_property(struct iris_inst *inst,
						 struct iris_hfi_packet *pkt)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);

	if (pkt->flags & HFI_FW_FLAGS_INFORMATION)
		return 0;

	switch (pkt->type) {
	case HFI_PROP_BITSTREAM_RESOLUTION:
		inst_hfi_gen2->src_subcr_params.bitstream_resolution = pkt->payload[0];
		break;
	case HFI_PROP_CROP_OFFSETS:
		inst_hfi_gen2->src_subcr_params.crop_offsets[0] = pkt->payload[0];
		inst_hfi_gen2->src_subcr_params.crop_offsets[1] = pkt->payload[1];
		break;
	case HFI_PROP_LUMA_CHROMA_BIT_DEPTH:
		inst_hfi_gen2->src_subcr_params.bit_depth = pkt->payload[0];
		break;
	case HFI_PROP_CODED_FRAMES:
		inst_hfi_gen2->src_subcr_params.coded_frames = pkt->payload[0];
		break;
	case HFI_PROP_BUFFER_FW_MIN_OUTPUT_COUNT:
		inst_hfi_gen2->src_subcr_params.fw_min_count = pkt->payload[0];
		break;
	case HFI_PROP_PIC_ORDER_CNT_TYPE:
		inst_hfi_gen2->src_subcr_params.pic_order_cnt = pkt->payload[0];
		break;
	case HFI_PROP_SIGNAL_COLOR_INFO:
		inst_hfi_gen2->src_subcr_params.color_info = pkt->payload[0];
		break;
	case HFI_PROP_PROFILE:
		inst_hfi_gen2->src_subcr_params.profile = pkt->payload[0];
		break;
	case HFI_PROP_LEVEL:
		inst_hfi_gen2->src_subcr_params.level = pkt->payload[0];
		break;
	case HFI_PROP_TIER:
		inst_hfi_gen2->src_subcr_params.tier = pkt->payload[0];
		break;
	case HFI_PROP_PICTURE_TYPE:
		inst_hfi_gen2->hfi_frame_info.picture_type = pkt->payload[0];
		break;
	case HFI_PROP_NO_OUTPUT:
		inst_hfi_gen2->hfi_frame_info.no_output = 1;
		break;
	case HFI_PROP_QUALITY_MODE:
	case HFI_PROP_STAGE:
	case HFI_PROP_PIPE:
	default:
		break;
	}

	return 0;
}

static int iris_hfi_gen2_handle_image_version_property(struct iris_core *core,
						       struct iris_hfi_packet *pkt)
{
	u8 *str_image_version = (u8 *)pkt + sizeof(*pkt);
	u32 req_bytes = pkt->size - sizeof(*pkt);
	char fw_version[IRIS_FW_VERSION_LENGTH];
	u32 i;

	if (req_bytes < IRIS_FW_VERSION_LENGTH - 1)
		return -EINVAL;

	for (i = 0; i < IRIS_FW_VERSION_LENGTH - 1; i++) {
		if (str_image_version[i] != '\0')
			fw_version[i] = str_image_version[i];
		else
			fw_version[i] = ' ';
	}
	fw_version[i] = '\0';
	dev_dbg(core->dev, "firmware version: %s\n", fw_version);

	return 0;
}

static int iris_hfi_gen2_handle_system_property(struct iris_core *core,
						struct iris_hfi_packet *pkt)
{
	switch (pkt->type) {
	case HFI_PROP_IMAGE_VERSION:
		return iris_hfi_gen2_handle_image_version_property(core, pkt);
	default:
		return 0;
	}
}

static int iris_hfi_gen2_handle_system_response(struct iris_core *core,
						struct iris_hfi_header *hdr)
{
	u8 *start_pkt = (u8 *)((u8 *)hdr + sizeof(*hdr));
	struct iris_hfi_packet *packet;
	u32 i, j;
	u8 *pkt;
	int ret;
	static const struct iris_hfi_gen2_core_hfi_range range[] = {
		{HFI_SYSTEM_ERROR_BEGIN, HFI_SYSTEM_ERROR_END, iris_hfi_gen2_handle_system_error },
		{HFI_PROP_BEGIN,         HFI_PROP_END, iris_hfi_gen2_handle_system_property },
		{HFI_CMD_BEGIN,          HFI_CMD_END, iris_hfi_gen2_handle_system_init },
	};

	for (i = 0; i < ARRAY_SIZE(range); i++) {
		pkt = start_pkt;
		for (j = 0; j < hdr->num_packets; j++) {
			packet = (struct iris_hfi_packet *)pkt;
			if (packet->flags & HFI_FW_FLAGS_SYSTEM_ERROR) {
				ret = iris_hfi_gen2_handle_system_error(core, packet);
				return ret;
			}

			if (packet->type > range[i].begin && packet->type < range[i].end) {
				ret = range[i].handle(core, packet);
				if (ret)
					return ret;

				if (packet->type >  HFI_SYSTEM_ERROR_BEGIN &&
				    packet->type < HFI_SYSTEM_ERROR_END)
					return 0;
			}
			pkt += packet->size;
		}
	}

	return 0;
}

static void iris_hfi_gen2_init_src_change_param(struct iris_inst *inst)
{
	struct iris_inst_hfi_gen2 *inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	struct v4l2_pix_format_mplane *pixmp_ip = &inst->fmt_src->fmt.pix_mp;
	struct v4l2_pix_format_mplane *pixmp_op = &inst->fmt_dst->fmt.pix_mp;
	u32 bottom_offset = (pixmp_ip->height - inst->crop.height);
	u32 right_offset = (pixmp_ip->width - inst->crop.width);
	struct hfi_subscription_params *subsc_params;
	u32 primaries, matrix_coeff, transfer_char;
	u32 colour_description_present_flag = 0;
	u32 video_signal_type_present_flag = 0;
	u32 full_range, video_format = 0;
	u32 left_offset = inst->crop.left;
	u32 top_offset = inst->crop.top;

	subsc_params = &inst_hfi_gen2->src_subcr_params;
	subsc_params->bitstream_resolution =
		pixmp_ip->width << 16 | pixmp_ip->height;
	subsc_params->crop_offsets[0] =
			left_offset << 16 | top_offset;
	subsc_params->crop_offsets[1] =
			right_offset << 16 | bottom_offset;
	subsc_params->fw_min_count = inst->buffers[BUF_OUTPUT].min_count;

	primaries = iris_hfi_gen2_get_color_primaries(pixmp_op->colorspace);
	matrix_coeff = iris_hfi_gen2_get_matrix_coefficients(pixmp_op->ycbcr_enc);
	transfer_char = iris_hfi_gen2_get_transfer_char(pixmp_op->xfer_func);
	full_range = pixmp_op->quantization == V4L2_QUANTIZATION_FULL_RANGE ? 1 : 0;
	subsc_params->color_info =
		iris_hfi_gen2_get_color_info(matrix_coeff, transfer_char, primaries,
					     colour_description_present_flag,
					     full_range, video_format,
					     video_signal_type_present_flag);

	switch (inst->codec) {
	case V4L2_PIX_FMT_HEVC:
		subsc_params->profile = inst->fw_caps[PROFILE_HEVC].value;
		subsc_params->level = inst->fw_caps[LEVEL_HEVC].value;
		break;
	case V4L2_PIX_FMT_VP9:
		subsc_params->profile = inst->fw_caps[PROFILE_VP9].value;
		subsc_params->level = inst->fw_caps[LEVEL_VP9].value;
		break;
	case V4L2_PIX_FMT_H264:
		subsc_params->profile = inst->fw_caps[PROFILE_H264].value;
		subsc_params->level = inst->fw_caps[LEVEL_H264].value;
		break;
	}

	subsc_params->pic_order_cnt = inst->fw_caps[POC].value;
	subsc_params->bit_depth = inst->fw_caps[BIT_DEPTH].value;
	if (inst->fw_caps[CODED_FRAMES].value ==
			CODED_FRAMES_PROGRESSIVE)
		subsc_params->coded_frames = HFI_BITMASK_FRAME_MBS_ONLY_FLAG;
	else
		subsc_params->coded_frames = 0;
}

static void iris_hfi_gen2_session_failure(struct iris_inst *inst)
{
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	iris_vb2_queue_error(inst);
	iris_hfi_gen2_fail_async(inst);
	complete(&inst->completion);
	complete(&inst->flush_completion);
}

/* Caller holds inst->lock. Scan the entire validated message before any
 * property/subscription/layout mutation, including two changes in one message.
 * The one initial IPSC is serviced by CAPTURE STREAMON, never decoder START.
 */
static int iris_decode_once_source_change_admit(struct iris_inst *inst,
					      struct iris_hfi_header *hdr)
{
	struct iris_core *core = inst->core;
	struct iris_hfi_packet *packet;
	u8 *cursor = (u8 *)hdr + sizeof(*hdr);
	u32 i, changes = 0;

	lockdep_assert_held(&inst->lock);
	if (!READ_ONCE(core->decode_once))
		return 0;
	for (i = 0; i < hdr->num_packets; i++) {
		packet = (struct iris_hfi_packet *)cursor;
		if (packet->type == HFI_CMD_SETTINGS_CHANGE &&
		    packet->port == HFI_PORT_BITSTREAM)
			changes++;
		cursor += packet->size;
	}
	/* Count observed validated BITSTREAM PSC packets, even if admission rejects. */
	atomic_add(changes, &core->decode_progress_psc_seen);
	if (!changes)
		return 0;
	if (changes == 1 && !inst->decode_source_change_seen &&
	    inst->state == IRIS_INST_INPUT_STREAMING &&
	    !inst->decode_pins[1].attempted) {
		inst->decode_source_change_seen = true;
		return 0;
	}
	if (core->native_repeat) {
		inst->stop_submit_blocked = true;
		return -EOPNOTSUPP;
	}
	/* STOP_RETAIN: no HFI resume/stop, allocator release or provider unwind. */
	mutex_lock(&core->lock);
	core->decode_once_admission = false;
	core->removing = true;
	core->core_ready = false;
	core->terminal_shutdown = true;
	core->irq_quiescing = true;
	core->recovery_blocked = true;
	core->state = IRIS_CORE_ERROR;
	core->firmware_shutdown_verified = false;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	inst->response_closing = true;
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	mutex_unlock(&core->lock);
	return -EOPNOTSUPP;
}

static void iris_input_comv_reply(struct iris_inst *inst, struct iris_hfi_header *header)
{
	struct iris_core *core = inst->core;
	struct iris_hfi_packet *packet, *comv = NULL;
	u8 *cursor = (u8 *)header + sizeof(*header);
	u32 i, count = 0, bits = BIT(2), value = 0;
	bool unsupported = false;

	if (!READ_ONCE(core->decode_once) || inst->domain != DECODER)
		return;
	lockdep_assert_held(&inst->lock);
	/* Already validated complete header, matched session; never join headers. */
	for (i = 0; i < header->num_packets; i++) {
		packet = (void *)cursor;
		if (packet->type == HFI_INFO_UNSUPPORTED)
			unsupported = true;
		if (packet->type == HFI_PROP_COMV_BUFFER_COUNT) {
			if (!comv)
				comv = packet;
			count++;
		}
		cursor += packet->size;
	}
	if (!comv || atomic_cmpxchg(&core->input_comv_rx_claim, 0, 1))
		return;
	if (unsupported)
		bits |= BIT(3);
	if (count > 1)
		bits |= BIT(6);
	if (atomic_read(&core->input_comv_bits) & BIT(0)) {
		smp_rmb();
		if (comv->packet_id == (u32)atomic_read(&core->input_comv_id))
			bits |= BIT(4);
	}
	if (comv->payload_info == HFI_PAYLOAD_U32 &&
	    comv->size >= sizeof(*comv) + sizeof(value)) {
		memcpy(&value, comv->payload, sizeof(value));
		bits |= BIT(5);
	}
	atomic_set(&core->input_comv_flags, (int)comv->flags);
	atomic_set(&core->input_comv_payload, (int)comv->payload_info);
	atomic_set(&core->input_comv_value, (int)value);
	smp_wmb();
	atomic_or(bits, &core->input_comv_bits);
}

static void iris_decode_progress_classify(struct iris_inst *inst,
					  struct iris_hfi_header *hdr)
{
	struct iris_core *core = inst->core;
	struct iris_hfi_packet *packet;
	u8 *cursor = (u8 *)hdr + sizeof(*hdr);
	u32 i;

	if (!READ_ONCE(core->decode_once))
		return;
	lockdep_assert_held(&inst->lock);
	/* The caller already validated the complete header and every packet.
	 * Classify before processing, even if an earlier packet causes rejection.
	 * Read headers only; do not expose payload, addresses or session identity.
	 */
	for (i = 0; i < hdr->num_packets; i++) {
		packet = (struct iris_hfi_packet *)cursor;
		atomic_set(&core->decode_progress_last_type, (int)packet->type);
		atomic_set(&core->decode_progress_last_port, (int)packet->port);
		atomic_set(&core->decode_progress_last_flags, (int)packet->flags);
		if (packet->type > HFI_INFORMATION_BEGIN && packet->type < HFI_INFORMATION_END)
			atomic_set(&core->input_last_info, (int)packet->type);
		if (packet->type > HFI_PROP_BEGIN && packet->type < HFI_PROP_END)
			atomic_set(&core->input_last_property, (int)packet->type);
		if (packet->type == HFI_CMD_OPEN)
			atomic_inc(&core->decode_progress_rx_open);
		else if (packet->type == HFI_CMD_START)
			atomic_inc(&core->decode_progress_rx_start);
		else if (packet->type == HFI_CMD_SUBSCRIBE_MODE)
			atomic_inc(&core->decode_progress_rx_sub);
		else if (packet->type > HFI_PROP_BEGIN && packet->type < HFI_PROP_END)
			atomic_inc(&core->decode_progress_rx_prop);
		else if (packet->type == HFI_CMD_BUFFER)
			atomic_inc(&core->decode_progress_rx_buf);
		else
			atomic_inc(&core->decode_progress_rx_other);
		if (packet->flags & (HFI_FW_FLAGS_SESSION_ERROR | HFI_FW_FLAGS_SYSTEM_ERROR) ||
		    (packet->type > HFI_SESSION_ERROR_BEGIN && packet->type < HFI_SESSION_ERROR_END) ||
		    (packet->type > HFI_SYSTEM_ERROR_BEGIN && packet->type < HFI_SYSTEM_ERROR_END))
			atomic_inc(&core->decode_progress_rx_err);
		cursor += packet->size;
	}
}

static int iris_hfi_gen2_handle_session_response(struct iris_core *core,
						 struct iris_hfi_header *hdr)
{
	u8 *pkt = (u8 *)((u8 *)hdr + sizeof(*hdr));
	struct iris_inst_hfi_gen2 *inst_hfi_gen2;
	struct iris_hfi_packet *packet;
	struct iris_inst *inst;
	bool dequeue = false;
	int ret = 0;
	u32 i, j;
	static const struct iris_hfi_gen2_inst_hfi_range range[] = {
		{HFI_SESSION_ERROR_BEGIN, HFI_SESSION_ERROR_END,
		 iris_hfi_gen2_handle_session_error},
		{HFI_INFORMATION_BEGIN, HFI_INFORMATION_END,
		 iris_hfi_gen2_handle_session_info},
		{HFI_PROP_BEGIN, HFI_PROP_END,
		 iris_hfi_gen2_handle_session_property},
		{HFI_CMD_BEGIN, HFI_CMD_END,
		 iris_hfi_gen2_handle_session_command },
	};

	inst = iris_get_response_instance(core, hdr->session_id);
	if (!inst)
		return -EINVAL;
	if (READ_ONCE(core->decode_once))
		atomic_inc(&core->decode_progress_session_rx);

	mutex_lock(&inst->lock);
	inst->stop_rx_header = hdr->header_id;
	iris_stop_failure_message(inst, hdr);
	iris_input_comv_reply(inst, hdr);
	iris_decode_progress_classify(inst, hdr);
	if (inst->response_closing) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_CLOSING, -ESHUTDOWN);
		if (READ_ONCE(core->decode_once))
			atomic_set(&core->decode_progress_session_state, READ_ONCE(inst->state));
		mutex_unlock(&inst->lock);
		iris_put_response_instance(inst);
		return -ESHUTDOWN;
	}
	if (inst->state == IRIS_INST_ERROR) {
		ret = -EIO;
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_PREERROR, ret);
		goto exit;
	}
	ret = iris_decode_once_source_change_admit(inst, hdr);
	if (ret) {
		iris_stop_failure_error(inst, IRIS_STOP_FAILURE_SOURCE_CHANGE, ret);
		iris_hfi_gen2_session_failure(inst);
		goto exit;
	}
	for (i = 0; i < hdr->num_packets; i++) {
		packet = (struct iris_hfi_packet *)pkt;
		if ((packet->flags & HFI_FW_FLAGS_SESSION_ERROR) ||
		    (packet->type > HFI_SESSION_ERROR_BEGIN && packet->type < HFI_SESSION_ERROR_END)) {
			iris_stop_failure_cursor(inst, i, packet->packet_id);
			iris_stop_failure_error(inst, packet->flags & HFI_FW_FLAGS_SESSION_ERROR ?
				IRIS_STOP_FAILURE_SESSION_FLAGS : IRIS_STOP_FAILURE_SESSION_TYPE, -EIO);
			iris_hfi_gen2_handle_session_error(inst, packet);
			ret = -EIO;
			goto exit;
		}
		pkt += packet->size;
	}
	pkt = (u8 *)hdr + sizeof(*hdr);
	inst_hfi_gen2 = to_iris_inst_hfi_gen2(inst);
	memset(&inst_hfi_gen2->hfi_frame_info, 0, sizeof(struct iris_hfi_frame_info));

	for (i = 0; i < hdr->num_packets; i++) {
		packet = (struct iris_hfi_packet *)pkt;
		if (packet->type == HFI_CMD_SETTINGS_CHANGE) {
			if (packet->port == HFI_PORT_BITSTREAM) {
				iris_hfi_gen2_init_src_change_param(inst);
				break;
			}
		}
		pkt += packet->size;
	}

	pkt = (u8 *)((u8 *)hdr + sizeof(*hdr));
	for (i = 0; i < ARRAY_SIZE(range); i++) {
		pkt = (u8 *)((u8 *)hdr + sizeof(*hdr));
		for (j = 0; j < hdr->num_packets; j++) {
			packet = (struct iris_hfi_packet *)pkt;
			if (packet->type > range[i].begin && packet->type < range[i].end) {
				dequeue |= (packet->type == HFI_CMD_BUFFER);
				iris_stop_failure_cursor(inst, j, packet->packet_id);
				if (core->native_repeat && packet->type == HFI_CMD_BUFFER) {
					struct iris_hfi_buffer *wire;

					/* Validate the complete body before reading even wire->type.
					 * Native replies never enter retained observers/legacy release.
					 */
					if (packet->payload_info != HFI_PAYLOAD_STRUCTURE ||
					    packet->size != sizeof(*packet) + sizeof(*wire)) {
						iris_hfi_gen2_session_failure(inst);
						ret = -EINVAL;
						goto exit;
					}
					wire = (void *)packet->payload;
					if (wire->type == HFI_BUFFER_BITSTREAM || wire->type == HFI_BUFFER_RAW)
						ret = iris_hfi_gen2_handle_session_buffer(inst, packet);
					else
						ret = iris_repeat_internal_return(inst, packet);
					if (ret || inst->state == IRIS_INST_ERROR) {
						if (!ret)
							ret = -EIO;
						iris_hfi_gen2_session_failure(inst);
						goto exit;
					}
					pkt += packet->size;
					continue;
				}
				if (packet->type == HFI_CMD_BUFFER &&
				    iris_stop_internal_return_consume(inst, packet)) {
					pkt += packet->size;
					continue;
				}
				if (packet->type == HFI_CMD_BUFFER &&
				    iris_release_observer_consume(inst, hdr, packet)) {
					pkt += packet->size;
					continue;
				}
				ret = range[i].handle(inst, packet);
				if (ret || inst->state == IRIS_INST_ERROR) {
					iris_stop_failure_error(inst, ret ? IRIS_STOP_FAILURE_CALLBACK_RET :
						IRIS_STOP_FAILURE_CALLBACK_STATE, ret);
					if (!ret)
						ret = -EIO;
					iris_hfi_gen2_session_failure(inst);
					goto exit;
				}
			}
			pkt += packet->size;
		}
	}

	if (dequeue) {
		iris_stop_failure_cursor(inst, U32_MAX, 0);
		ret = iris_hfi_gen2_handle_dequeue_buffers(inst);
		if (ret || inst->state == IRIS_INST_ERROR) {
			iris_stop_failure_error(inst, ret ? IRIS_STOP_FAILURE_DEQUEUE_RET :
				IRIS_STOP_FAILURE_DEQUEUE_STATE, ret);
			if (!ret)
				ret = -EIO;
			iris_hfi_gen2_session_failure(inst);
		}
	}

exit:
	if (READ_ONCE(core->decode_once))
		atomic_set(&core->decode_progress_session_state, READ_ONCE(inst->state));
	mutex_unlock(&inst->lock);
	iris_put_response_instance(inst);

	return ret;
}

static int iris_hfi_gen2_handle_response(struct iris_core *core, void *response, u32 received)
{
	struct iris_hfi_header *hdr = (struct iris_hfi_header *)response;
	int ret;

	ret = iris_hfi_gen2_validate_hdr_packet(core, hdr, received);
	if (ret) {
		iris_stop_failure_malformed(core, response, received, ret);
		return iris_hfi_gen2_handle_system_error(core, NULL);
	}

	if (!hdr->session_id)
		return iris_hfi_gen2_handle_system_response(core, hdr);
	else
		return iris_hfi_gen2_handle_session_response(core, hdr);
}

static void iris_input_proof_debug(struct iris_core *core, const u8 *record, u32 received)
{
	static const char * const words[] = {
		"error", "fail", "unsupported", "invalid", "timeout", "fault",
		"smmu", "iommu", "buffer", "memory", "clock", "power", "assert",
		"decode", "codec", "hfi",
	};
	const struct hfi_debug_header *header = (const void *)record;
	char text[513];
	u32 i, j, n, bits = 0, unknown = 0;

	if (!READ_ONCE(core->decode_once))
		return;
	/* Called only for an already-drained complete record. Store no raw text. */
	if (received < sizeof(*header) || header->size != received ||
	    received >= IFACEQ_CORE_PKT_SIZE)
		return;
	/* Match the existing drain's text start, after the header plus one byte. */
	n = received > sizeof(*header) + 1 ? received - sizeof(*header) - 1 : 0;
	if (n > 512) {
		n = 512;
		unknown |= BIT(1);
	}
	for (i = 0; i < n; i++) {
		u8 c = record[sizeof(*header) + 1 + i];

		if (!c)
			break;
		if (c >= 'A' && c <= 'Z')
			c += 'a' - 'A';
		if ((c < 32 && c != '\t' && c != '\n' && c != '\r') || c > 126) {
			c = ' ';
			unknown |= BIT(2);
		}
		text[i] = c;
	}
	text[i] = '\0';
	for (j = 0; j < ARRAY_SIZE(words); j++)
		if (strstr(text, words[j]))
			bits |= BIT(j);
	if (!bits)
		unknown |= BIT(0);
	atomic_inc(&core->input_debug_count);
	atomic_set(&core->input_debug_level, (int)header->debug_level);
	atomic_set(&core->input_debug_last, (int)bits);
	atomic_or(bits, &core->input_debug_bits);
	atomic_or(unknown, &core->input_debug_unknown);
}

static void iris_hfi_gen2_flush_debug_queue(struct iris_core *core, u8 *packet)
{
	struct hfi_debug_header *pkt;
	u8 *log;
	u32 received;

	while (!iris_hfi_queue_dbg_read_len(core, packet, IFACEQ_CORE_PKT_SIZE, &received)) {
		pkt = (struct hfi_debug_header *)packet;

		if (received < sizeof(*pkt) || pkt->size != received)
			continue;

		if (pkt->size >= IFACEQ_CORE_PKT_SIZE)
			continue;

		iris_input_proof_debug(core, packet, received);
		packet[pkt->size] = '\0';
		log = (u8 *)packet + sizeof(*pkt) + 1;
		dev_dbg(core->dev, "%s", log);
	}
}

static void iris_hfi_gen2_response_handler(struct iris_core *core)
{
	if (iris_vpu_watchdog(core, core->intr_status)) {
		struct iris_hfi_packet pkt = {.type = HFI_SYS_ERROR_WD_TIMEOUT};

		dev_err(core->dev, "cpu watchdog error received\n");
		iris_hfi_gen2_handle_system_error(core, &pkt);

		return;
	}

	for (;;) {
		u32 received;
		int ret;

		memset(core->response_packet, 0, IFACEQ_CORE_PKT_SIZE);
		ret = iris_hfi_queue_msg_read_len(core, core->response_packet,
					      IFACEQ_CORE_PKT_SIZE, &received);
		if (ret) {
			if (ret != -ENODATA)
				iris_hfi_gen2_handle_system_error(core, NULL);
			break;
		}
		iris_hfi_gen2_handle_response(core, core->response_packet, received);
	}

	iris_hfi_gen2_flush_debug_queue(core, core->response_packet);
}

static const struct iris_hfi_response_ops iris_hfi_gen2_response_ops = {
	.hfi_response_handler = iris_hfi_gen2_response_handler,
};

void iris_hfi_gen2_response_ops_init(struct iris_core *core)
{
	core->hfi_response_ops = &iris_hfi_gen2_response_ops;
	core->gen2_ack_supported = true;
}
