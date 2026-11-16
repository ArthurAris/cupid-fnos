// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/pm_runtime.h>
#include <linux/module.h>
#include <linux/dma-buf.h>
#include <linux/uaccess.h>
#include <media/v4l2-event.h>
#include <media/v4l2-ioctl.h>
#include <media/v4l2-mem2mem.h>
#include <media/videobuf2-dma-contig.h>

#include "iris_vidc.h"
#include "iris_hfi_gen2.h"
#include "iris_instance.h"
#include "iris_vdec.h"
#include "iris_venc.h"
#include "iris_vb2.h"
#include "iris_vpu_buffer.h"
#include "iris_platform_common.h"

#define IRIS_DRV_NAME "iris_driver"
#define IRIS_BUS_NAME "platform:iris_icc"
#define STEP_WIDTH 1
#define STEP_HEIGHT 1

static void iris_v4l2_fh_init(struct iris_inst *inst, struct file *filp)
{
	if (inst->domain == ENCODER)
		v4l2_fh_init(&inst->fh, inst->core->vdev_enc);
	else if (inst->domain == DECODER)
		v4l2_fh_init(&inst->fh, inst->core->vdev_dec);
	inst->fh.ctrl_handler = &inst->ctrl_handler;
	v4l2_fh_add(&inst->fh, filp);
}

static void iris_v4l2_fh_deinit(struct iris_inst *inst, struct file *filp)
{
	v4l2_fh_del(&inst->fh, filp);
	inst->fh.ctrl_handler = NULL;
	v4l2_fh_exit(&inst->fh);
}

static int iris_add_session(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	struct iris_inst *iter;
	u32 count = 0;

	mutex_lock(&core->lock);

	list_for_each_entry(iter, &core->instances, list)
		count++;

	if (!core->core_ready || core->removing || core->terminal_shutdown || core->irq_quiescing || core->state != IRIS_CORE_INIT ||
	    count >= core->iris_platform_data->max_session_count) {
		mutex_unlock(&core->lock);
		return -ESHUTDOWN;
	}
	list_add_tail(&inst->list, &core->instances);

	mutex_unlock(&core->lock);
	return 0;
}

static void iris_remove_session(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	struct iris_inst *iter, *temp;

	mutex_lock(&core->lock);
	inst->response_closing = true;
	list_for_each_entry_safe(iter, temp, &core->instances, list) {
		if (iter->session_id == inst->session_id) {
			list_del_init(&iter->list);
			break;
		}
	}
	mutex_unlock(&core->lock);
}

static inline struct iris_inst *iris_get_inst(struct file *filp)
{
	return container_of(file_to_v4l2_fh(filp), struct iris_inst, fh);
}

static void iris_m2m_device_run(void *priv)
{
}

static void iris_m2m_job_abort(void *priv)
{
	struct iris_inst *inst = priv;
	struct v4l2_m2m_ctx *m2m_ctx = inst->m2m_ctx;

	v4l2_m2m_job_finish(inst->m2m_dev, m2m_ctx);
}

static const struct v4l2_m2m_ops iris_m2m_ops = {
	.device_run = iris_m2m_device_run,
	.job_abort = iris_m2m_job_abort,
};

static int
iris_m2m_queue_init(void *priv, struct vb2_queue *src_vq, struct vb2_queue *dst_vq)
{
	struct iris_inst *inst = priv;
	int ret;

	src_vq->type = V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE;
	src_vq->io_modes = VB2_MMAP | VB2_DMABUF;
	src_vq->timestamp_flags = V4L2_BUF_FLAG_TIMESTAMP_COPY;
	src_vq->ops = inst->core->iris_vb2_ops;
	src_vq->mem_ops = &vb2_dma_contig_memops;
	src_vq->drv_priv = inst;
	src_vq->buf_struct_size = sizeof(struct iris_buffer);
	src_vq->min_reqbufs_allocation = MIN_BUFFERS;
	src_vq->dev = inst->core->dev;
	src_vq->lock = &inst->ctx_q_lock;
	ret = vb2_queue_init(src_vq);
	if (ret)
		return ret;

	dst_vq->type = V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE;
	dst_vq->io_modes = VB2_MMAP | VB2_DMABUF;
	dst_vq->timestamp_flags = V4L2_BUF_FLAG_TIMESTAMP_COPY;
	dst_vq->ops = inst->core->iris_vb2_ops;
	dst_vq->mem_ops = &vb2_dma_contig_memops;
	dst_vq->drv_priv = inst;
	dst_vq->buf_struct_size = sizeof(struct iris_buffer);
	dst_vq->min_reqbufs_allocation = MIN_BUFFERS;
	dst_vq->dev = inst->core->dev;
	dst_vq->lock = &inst->ctx_q_lock;

	return vb2_queue_init(dst_vq);
}

static int iris_open_owned(struct file *filp)
{
	struct iris_core *core = video_drvdata(filp);
	struct video_device *vdev;
	struct iris_inst *inst;
	u32 session_type;
	int ret;

	vdev = video_devdata(filp);
	if (strcmp(vdev->name, "qcom-iris-decoder") == 0)
		session_type = DECODER;
	else if (strcmp(vdev->name, "qcom-iris-encoder") == 0)
		session_type = ENCODER;
	else
		return -EINVAL;

	if (READ_ONCE(core->decode_once)) {
		if (session_type != DECODER)
			return -EOPNOTSUPP;
		/* open_get already consumed the sole claim. The admission side's
		 * core_init attaches only its existing acknowledged core, no PM/INIT.
		 */
		ret = iris_core_init(core);
		if (ret)
			return ret;
	} else {
		ret = pm_runtime_resume_and_get(core->dev);
		if (ret < 0)
			return ret;

		ret = iris_core_init(core);
		if (ret) {
			dev_err(core->dev, "core init failed\n");
			iris_core_pm_put(core);
			return ret;
		}

		iris_core_pm_put(core);
	}

	inst = core->iris_platform_data->get_instance();
	if (IS_ERR_OR_NULL(inst))
		return inst ? PTR_ERR(inst) : -ENOMEM;

	inst->core = core;
	inst->domain = session_type;
	inst->session_id = hash32_ptr(inst);
	if (core->native_repeat) {
		mutex_lock(&core->lock);
		if (core->repeat_session_sequence == U32_MAX) {
			mutex_unlock(&core->lock);
			kfree(inst);
			return -EOVERFLOW;
		}
		inst->session_id = ++core->repeat_session_sequence;
		mutex_unlock(&core->lock);
	}
	inst->state = IRIS_INST_DEINIT;

	mutex_init(&inst->lock);
	mutex_init(&inst->ctx_q_lock);
	mutex_init(&inst->decode_ioctl_lock);
	init_completion(&inst->stop_app_returned);
	init_completion(&inst->repeat_internal_returned);
	inst->repeat_lifecycle_generation = 1;

	INIT_LIST_HEAD(&inst->buffers[BUF_BIN].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_ARP].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_COMV].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_NON_COMV].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_LINE].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_DPB].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_PERSIST].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_SCRATCH_1].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_SCRATCH_2].list);
	INIT_LIST_HEAD(&inst->buffers[BUF_VPSS].list);
	init_completion(&inst->completion);
	iris_hfi_pending_init(&inst->control_pending);
	refcount_set(&inst->response_refs, 1);
	init_completion(&inst->response_idle);
	inst->response_closing = false;
	inst->response_error_notified = false;
	init_completion(&inst->flush_completion);

	iris_v4l2_fh_init(inst, filp);

	inst->m2m_dev = v4l2_m2m_init(&iris_m2m_ops);
	if (IS_ERR_OR_NULL(inst->m2m_dev)) {
		ret = inst->m2m_dev ? PTR_ERR(inst->m2m_dev) : -ENOMEM;
		goto fail_v4l2_fh_deinit;
	}

	inst->m2m_ctx = v4l2_m2m_ctx_init(inst->m2m_dev, inst, iris_m2m_queue_init);
	if (IS_ERR_OR_NULL(inst->m2m_ctx)) {
		ret = inst->m2m_ctx ? PTR_ERR(inst->m2m_ctx) : -ENOMEM;
		goto fail_m2m_release;
	}

	if (inst->domain == DECODER)
		ret = iris_vdec_inst_init(inst);
	else if (inst->domain == ENCODER)
		ret = iris_venc_inst_init(inst);
	if (ret)
		goto fail_m2m_ctx_release;

	ret = iris_add_session(inst);
	if (ret)
		goto fail_inst_deinit;

	inst->fh.m2m_ctx = inst->m2m_ctx;

	return 0;

fail_inst_deinit:
	/* Only successful instance init transfers handler ownership to open. */
	v4l2_ctrl_handler_free(&inst->ctrl_handler);
	if (inst->domain == DECODER)
		iris_vdec_inst_deinit(inst);
	else if (inst->domain == ENCODER)
		iris_venc_inst_deinit(inst);
fail_m2m_ctx_release:
	v4l2_m2m_ctx_release(inst->m2m_ctx);
fail_m2m_release:
	v4l2_m2m_release(inst->m2m_dev);
fail_v4l2_fh_deinit:
	iris_v4l2_fh_deinit(inst, filp);
	mutex_destroy(&inst->ctx_q_lock);
	mutex_destroy(&inst->lock);
	kfree(inst);

	return ret;
}

int iris_open(struct file *filp)
{
	struct iris_core *core = video_drvdata(filp);
	int ret;

	/* The registered video device holds a CPU core owner before this lookup. */
	ret = iris_core_open_get(core);
	if (ret)
		return ret;
	ret = iris_open_owned(filp);
	iris_core_open_finish(core, !ret);
	return ret;
}

static int iris_session_close_checked(struct iris_inst *inst)
{
	const struct iris_hfi_command_ops *hfi_ops = inst->core->hfi_ops;
	int ret;

	if (inst->state == IRIS_INST_DEINIT)
		return 0;
	if (iris_hfi_pending_active(&inst->control_pending))
		return -EBUSY;
	reinit_completion(&inst->completion);
	ret = hfi_ops->session_close(inst);
	if (ret)
		return ret;
	return iris_wait_for_session_response(inst, false);
}

static void iris_check_num_queued_internal_buffers(struct iris_inst *inst, u32 plane)
{
	const struct iris_platform_data *platform_data = inst->core->iris_platform_data;
	struct iris_buffer *buf, *next;
	struct iris_buffers *buffers;
	const u32 *internal_buf_type;
	u32 internal_buffer_count, i;
	u32 count = 0;

	if (V4L2_TYPE_IS_OUTPUT(plane)) {
		internal_buf_type = platform_data->dec_ip_int_buf_tbl;
		internal_buffer_count = platform_data->dec_ip_int_buf_tbl_size;
	} else {
		internal_buf_type = platform_data->dec_op_int_buf_tbl;
		internal_buffer_count = platform_data->dec_op_int_buf_tbl_size;
	}

	for (i = 0; i < internal_buffer_count; i++) {
		buffers = &inst->buffers[internal_buf_type[i]];
		count = 0;
		list_for_each_entry_safe(buf, next, &buffers->list, list)
			count++;
		if (count)
			dev_err(inst->core->dev, "%d buffer of type %d not released",
				count, internal_buf_type[i]);
	}

	if (inst->domain == DECODER)
		buffers = &inst->buffers[BUF_PERSIST];
	else
		buffers = &inst->buffers[BUF_ARP];

	count = 0;
	list_for_each_entry_safe(buf, next, &buffers->list, list)
		count++;
	if (count)
		dev_err(inst->core->dev, "%d buffer of type %d not released",
			count, inst->domain == DECODER ? BUF_PERSIST : BUF_ARP);
}

static int iris_orphan_reclaim_gate(struct iris_core *core)
{
	struct iris_inst *inst;

	if (!core->terminal_shutdown || core->state != IRIS_CORE_DEINIT || !core->firmware_shutdown_verified ||
	    !core->irq_drain_verified || !core->recovery_drain_verified)
		return -EAGAIN;
	if (core->power_state_unknown || core->power_irq_enabled || iris_power_progress_pending(core))
		return -EBUSY;
	if (!list_empty(&core->instances))
		return -EBUSY;
	list_for_each_entry(inst, &core->orphan_instances, list) {
		if (!inst->response_closing || !inst->file_detached ||
		    !inst->orphaned || refcount_read(&inst->response_refs))
			return -EBUSY;
	}
	return 0;
}

int iris_reclaim_orphan_instances(struct iris_core *core)
{
	LIST_HEAD(reclaim);
	struct iris_inst *inst, *next;
	int ret;

	/* Lifecycle lock must exclude reinitialization and a second reclaimer. */
	iris_core_get(core);
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	ret = iris_orphan_reclaim_gate(core);
	mutex_unlock(&core->lock);
	if (ret)
		goto unlock_lifecycle;

	/* Keep every orphan/framework owner until all fallible buffer frees pass. */
	list_for_each_entry(inst, &core->orphan_instances, list) {
		mutex_lock(&inst->lock);
		ret = iris_destroy_all_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
		if (!ret)
			ret = iris_destroy_all_internal_buffers(inst,
							       V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
		mutex_unlock(&inst->lock);
		if (ret)
			goto unlock_lifecycle;
	}
	/* lifecycle_lock excludes orphan publication and concurrent reclamation. */
	mutex_lock(&core->lock);
	list_splice_init(&core->orphan_instances, &reclaim);
	mutex_unlock(&core->lock);
	list_for_each_entry_safe(inst, next, &reclaim, list) {
		struct device *dev = core->dev;

		list_del_init(&inst->list);
		inst->orphaned = false;
		/* No response or firmware owner remains at the verified gate. */
		v4l2_ctrl_handler_free(&inst->ctrl_handler);
		v4l2_m2m_ctx_release(inst->m2m_ctx);
		v4l2_m2m_release(inst->m2m_dev);
		mutex_lock(&inst->lock);
		if (inst->domain == DECODER)
			iris_vdec_inst_deinit(inst);
		else if (inst->domain == ENCODER)
			iris_venc_inst_deinit(inst);
		mutex_unlock(&inst->lock);
		mutex_destroy(&inst->ctx_q_lock);
		mutex_destroy(&inst->lock);
		kfree(inst);
		put_device(dev);
		iris_core_orphan_put(core);
	}
	ret = 0;
unlock_lifecycle:
	mutex_unlock(&core->lifecycle_lock);
	iris_core_put(core);
	return ret;
}

/* Earliest release interception, before queue lookup or any framework callback.
 * Returns true even for broken admission: failing to retain a prior module pin
 * is an admission defect, never permission to free possibly-owned DMA here.
 * No wait/IRQ synchronization is attempted: those may not return. The separate
 * coordinator must observe/finish CPU draining; DMA remains unknown throughout.
 */
bool iris_decode_once_retain_close(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;

	if (!READ_ONCE(core->decode_once))
		return false;
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&inst->lock);
	mutex_lock(&core->lock);
	core->removing = true;
	core->core_ready = false;
	core->terminal_shutdown = true;
	core->irq_quiescing = true;
	core->recovery_blocked = true;
	core->state = IRIS_CORE_ERROR;
	core->firmware_shutdown_verified = false;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	/* Retain list membership and base response ref. Existing lookups reject
	 * response_closing. In-flight refs exit normally; no teardown depends on it.
	 */
	inst->response_closing = true;
	inst->state = IRIS_INST_ERROR;
	core->experiment_retained_inst = inst;
	core->experiment_release_retained = true;
	core->experiment_file_gone = true;
	mutex_unlock(&core->lock);
	mutex_unlock(&inst->lock);
	mutex_unlock(&core->lifecycle_lock);
	/* No fh_del/exit, m2m release, vb2 stop/done, list removal, PM/ref put.
	 * struct file itself is NOT resurrected; exact target fh has no file pointer.
	 */
	return true;
}

/* Public allocator export, same ownership operation used by EXPBUF. The exact
 * shipped c1147 vb2-dma-contig export increments allocator refcount; .put frees
 * backing only at zero. Never dereference allocator-private structures here.
 * Partial exports are retained too, and failure permanently consumes this queue.
 */
static int iris_decode_once_pin_queue(struct iris_inst *inst, struct vb2_queue *q)
{
	struct iris_decode_once_pins *pins;
	u32 slot, i, count;

	lockdep_assert_held(&inst->ctx_q_lock);
	if (!READ_ONCE(inst->core->decode_once))
		return -EOPNOTSUPP;
	if (q->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
		slot = 0;
	else if (q->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
		slot = 1;
	else
		return -EINVAL;
	pins = &inst->decode_pins[slot];
	if (pins->attempted)
		return pins->complete ? -EALREADY : pins->error;
	pins->attempted = true;
	pins->error = -EINVAL;
	if (q->memory != VB2_MEMORY_MMAP || q->mem_ops != &vb2_dma_contig_memops ||
	    !q->mem_ops->get_dmabuf || vb2_is_streaming(q) ||
	    !q->max_num_buffers || q->max_num_buffers > VIDEO_MAX_FRAME)
		return pins->error;
	count = vb2_get_num_buffers(q);
	if (!count || count > VIDEO_MAX_FRAME)
		return pins->error;
	for (i = 0; i < q->max_num_buffers; i++) {
		struct vb2_buffer *vb = vb2_get_buffer(q, i);
		struct dma_buf *dbuf;

		if (!vb)
			continue;
		if (vb->num_planes != 1 || !vb->planes[0].mem_priv ||
		    !vb->planes[0].length)
			return pins->error;
		dbuf = q->mem_ops->get_dmabuf(vb, vb->planes[0].mem_priv, O_RDWR);
		if (IS_ERR_OR_NULL(dbuf)) {
			pins->error = dbuf ? PTR_ERR(dbuf) : -ENOMEM;
			return pins->error;
		}
		/* No put on error/close; each successful export is an owned reference. */
		pins->dbuf[i] = dbuf;
		pins->vb[i] = vb;
		pins->mem_priv[i] = vb->planes[0].mem_priv;
		pins->count++;
	}
	if (pins->count != count)
		return pins->error;
	pins->complete = true;
	pins->error = 0;
	return 0;
}

static bool iris_decode_once_queue_pinned(struct iris_inst *inst, struct vb2_queue *q,
					 u32 slot)
{
	struct iris_decode_once_pins *pins = &inst->decode_pins[slot];
	u32 i, count = 0;

	lockdep_assert_held(&inst->ctx_q_lock);
	if (!pins->complete || pins->error || q->max_num_buffers > VIDEO_MAX_FRAME ||
	    q->memory != VB2_MEMORY_MMAP || q->mem_ops != &vb2_dma_contig_memops)
		return false;
	for (i = 0; i < q->max_num_buffers; i++) {
		struct vb2_buffer *vb = vb2_get_buffer(q, i);

		if (!vb)
			continue;
		if (!pins->dbuf[i] || pins->vb[i] != vb || vb->num_planes != 1 ||
		    pins->mem_priv[i] != vb->planes[0].mem_priv)
			return false;
		count++;
	}
	return count && count == pins->count && count == vb2_get_num_buffers(q);
}

bool iris_decode_once_backing_pinned_locked(struct iris_inst *inst, struct vb2_queue *q)
{
	u32 slot;

	lockdep_assert_held(&inst->ctx_q_lock);
	if (!READ_ONCE(inst->core->decode_once) || !q)
		return false;
	if (q->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
		slot = 0;
	else if (q->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
		slot = 1;
	else
		return false;
	return iris_decode_once_queue_pinned(inst, q, slot);
}

bool iris_decode_once_backing_pinned(struct iris_inst *inst, struct vb2_queue *q)
{
	bool ready;

	mutex_lock(&inst->ctx_q_lock);
	ready = iris_decode_once_backing_pinned_locked(inst, q);
	mutex_unlock(&inst->ctx_q_lock);
	/* Per-queue backing proof only, never global hook/DMA-stop readiness. */
	return ready;
}

static int iris_decode_once_reqbufs(struct file *filp, void *fh,
				    struct v4l2_requestbuffers *req)
{
	struct iris_inst *inst = iris_get_inst(filp);
	struct vb2_queue *q;
	u32 slot;
	int ret;

	if (inst->core->native_repeat) {
		q = v4l2_m2m_get_vq(inst->m2m_ctx, req->type);
		if (!q || (req->type != V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE &&
		    req->type != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE))
			return -EINVAL;
		if (!req->count && !IRIS_NATIVE_REPEAT_RECLAIM_ENABLED)
			return -EOPNOTSUPP;
		if (!req->count && inst->repeat_cpu_drained)
			return v4l2_m2m_ioctl_reqbufs(filp, fh, req);
		mutex_lock(&inst->lock);
		ret = iris_repeat_apps_owned(inst, !V4L2_TYPE_IS_OUTPUT(req->type)) ||
			vb2_is_streaming(q) || inst->stop_submit_blocked;
		mutex_unlock(&inst->lock);
		if (ret)
			return -EBUSY;
		/* Queue resizing/REQ0 is not claimed by this EOS+close-only candidate. */
		if (!req->count || vb2_get_num_buffers(q))
			return -EOPNOTSUPP;
	}
	if (!READ_ONCE(inst->core->decode_once))
		return v4l2_m2m_ioctl_reqbufs(filp, fh, req);
	if (req->type == V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
		slot = 0;
	else if (req->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE)
		slot = 1;
	else
		return -EINVAL;
	if (!req->count || req->count > VIDEO_MAX_FRAME || req->memory != V4L2_MEMORY_MMAP)
		return -EINVAL;
	/* Fixed caller; serialize ioctl entry separately before publishing any node.
	 * This precheck plus q lock is not itself a concurrency/admission solution.
	 */
	if (inst->decode_pins[slot].attempted)
		return -EBUSY;
	ret = v4l2_m2m_ioctl_reqbufs(filp, fh, req);
	if (ret)
		return ret;
	q = v4l2_m2m_get_vq(inst->m2m_ctx, req->type);
	ret = iris_decode_once_pin_queue(inst, q);
	return ret;
}


static void iris_stop_snapshot(struct iris_inst *inst, struct iris_stop_snapshot *snapshot)
{
	u32 plane, index;

	memset(snapshot, 0, sizeof(*snapshot));
	for (plane = 0; plane < 2; plane++)
		for (index = 0; index < VIDEO_MAX_FRAME; index++) {
		struct iris_stop_app *app = &inst->stop_app[plane][index];

		snapshot->app[plane][index] = *app;
		if (!app->valid)
			continue;
		snapshot->seen[plane] |= BIT(index);
		if (app->submissions > app->returned)
			snapshot->queued[plane] |= BIT(index);
		if (app->readonly)
			snapshot->readonly[plane] |= BIT(index);
		if (app->queue_rc)
			snapshot->unknown[plane] |= BIT(index);
	}
}

static long iris_stop_observer_command(struct iris_inst *inst, unsigned long arg)
{
	struct iris_stop_command command;
	struct iris_stop_evidence *proof = &inst->core->stop_evidence;
	int ret, plane;
	u32 index, input_submitted = 0, input_returned = 0;

	if (!READ_ONCE(inst->core->stop_observer_enabled) || !READ_ONCE(inst->core->decode_once))
		return -EOPNOTSUPP;
	if (inst->core->native_repeat)
		return -EOPNOTSUPP;
	if (copy_from_user(&command, (void __user *)arg, sizeof(command)))
		return -EFAULT;
	if (command.magic != 0x53544f50 || command.version != 1 || command.action != 1 || command.reserved)
		return -EINVAL;
	mutex_lock(&inst->lock);
	if (inst->stop_attempted) {
		ret = -EALREADY;
		goto unlock;
	}
	for (index = 0; index<VIDEO_MAX_FRAME; index++) {
		input_submitted += inst->stop_app[0][index].submissions;
		input_returned += inst->stop_app[0][index].returned;
		if (inst->stop_app[0][index].queue_rc || inst->stop_app[0][index].readonly) {
			ret=-EBUSY;
			goto unlock;
		}
	}
	if (inst->domain != DECODER || inst->state != IRIS_INST_STREAMING ||
		    inst->core->state != IRIS_CORE_INIT || inst->response_closing ||
	    inst->core->terminal_shutdown || !inst->decode_source_change_seen ||
	    inst->sequence_out != 3 || input_submitted != 3 || input_returned != 3 || !inst->stop_last_dq ||
	    !inst->decode_pins[0].complete || !inst->decode_pins[1].complete ||
	    (inst->sub_state & (IRIS_INST_SUB_DRC | IRIS_INST_SUB_DRC_LAST)) ||
	    iris_hfi_pending_active(&inst->control_pending) ||
	    iris_hfi_pending_active(&to_iris_inst_hfi_gen2(inst)->open_pending) ||
	    iris_hfi_pending_active(&to_iris_inst_hfi_gen2(inst)->start_pending[0]) ||
	    iris_hfi_pending_active(&to_iris_inst_hfi_gen2(inst)->start_pending[1])) {
		ret = -EBUSY;
		goto unlock;
	}
	/* Consume before the first actual HFI write. Failures never permit a retry. */
	reinit_completion(&inst->stop_app_returned);
	inst->stop_attempted = true;
	WRITE_ONCE(inst->core->stop_failure.session, inst->session_id);
	inst->core->stop_failure.current_ordinal = U32_MAX;
	inst->stop_submit_blocked = true;
	iris_stop_snapshot(inst, &proof->pre);
	smp_store_release(&proof->pre_valid.counter, 1);
	ret = 0;
	for (plane = 0; plane < 2; plane++) {
		ret = inst->core->hfi_ops->session_stop(inst, plane ?
		V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE : V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
		proof->transaction[plane].wait_rc = ret;
		if (ret)
			break;
	}
	if (!ret) {
		/* No q_lock held: response callbacks need inst->lock while STOP wait unlocks. */
		mutex_unlock(&inst->lock);
		wait_for_completion_timeout(&inst->stop_app_returned,
		    msecs_to_jiffies(inst->core->iris_platform_data->hw_response_timeout));
		mutex_lock(&inst->lock);
	}
	iris_stop_snapshot(inst, &proof->post);
	proof->result = ret;
	iris_stop_failure_freeze(inst, ret);
	smp_store_release(&proof->post_valid.counter, 1);
	smp_store_release(&proof->result_valid.counter, 1);
	/* Normal response callbacks remain live. No STOP ACK is a DMA handoff. */
unlock:
	mutex_unlock(&inst->lock);
	return ret;
}

static int iris_stop_observer_dqbuf(struct file *filp, void *fh, struct v4l2_buffer *buffer)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = v4l2_m2m_ioctl_dqbuf(filp, fh, buffer);

	if (!ret && (READ_ONCE(inst->core->stop_observer_enabled) || inst->core->native_repeat) &&
		    buffer->type == V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE &&
	    (buffer->flags & V4L2_BUF_FLAG_LAST)) {
		mutex_lock(&inst->lock);
		inst->stop_last_dq = true; /* Actual successful DQBUF, not a drain ACK/event. */
		mutex_unlock(&inst->lock);
	}
	return ret;
}

/* Only the fixed one-plane MMAP path is admitted. Serialize the complete
 * private ioctl so REQBUFS/pin cannot race STREAMON or another allocation call.
 * decode_once_retention_ready means reviewed safety hooks, not queue existence.
 */
static int iris_repeat_quiesce(struct iris_inst *inst);

static long iris_repeat_receipt_read(struct iris_inst *inst, unsigned long arg)
{
	struct iris_repeat_receipt receipt;
	struct iris_repeat_owner *owner = NULL;
	struct iris_buffer *buf;
	u32 ordinal = 0;

	if (!inst->core->native_repeat)
		return -EOPNOTSUPP;
	if (copy_from_user(&receipt, (void __user *)arg, sizeof(receipt)))
		return -EFAULT;
	if (receipt.version != 1 || (receipt.session && receipt.session != inst->session_id))
		return -ESTALE;
	mutex_lock(&inst->lock);
	receipt.buffer_index = receipt.index;
	receipt.retired = 0;
	if (receipt.kind < 2 && receipt.index < VIDEO_MAX_FRAME)
		owner = &inst->repeat_app[receipt.kind][receipt.index];
	else if (receipt.kind >= BUF_BIN && receipt.kind < BUF_TYPE_MAX)
		list_for_each_entry(buf, &inst->buffers[receipt.kind].list, list)
			if (ordinal++ == receipt.ordinal) {
				owner = buf->repeat_owner;
				receipt.buffer_index = buf->index;
				receipt.retired = buf->experiment_retired;
				break;
			}
	if (!owner && receipt.kind != U32_MAX) {
		mutex_unlock(&inst->lock);
		return -ENOENT;
	}
	if (owner) {
		receipt.session = inst->session_id;
		receipt.generation = owner->generation;
		receipt.header = owner->header;
		receipt.packet = owner->packet;
		receipt.type = owner->type;
		receipt.port = owner->port;
		receipt.size = owner->size;
		receipt.addr_lo = (u32)owner->addr;
		receipt.addr_hi = (u32)(owner->addr >> 32);
		receipt.request_header = owner->request_header;
		receipt.request_packet = owner->request_packet;
		receipt.request_generation = owner->request_generation;
		receipt.flags = owner->valid | (owner->fw_owned << 1) | (owner->unknown << 2) |
			(owner->readonly << 3) | (owner->release_requested << 4);
		receipt.rx_header = owner->rx_header;
		receipt.rx_packet = owner->rx_packet;
		receipt.rx_flags = owner->rx_flags;
		receipt.rx_buffer_flags = owner->rx_buffer_flags;
		receipt.rx_generation = owner->rx_generation;
		receipt.rx_result = owner->rx_result;
		receipt.submitted = owner->submitted;
		receipt.returned = owner->returned;
	} else {
		receipt.generation = 0;
		receipt.header = receipt.packet = receipt.type = receipt.port = 0;
		receipt.size = receipt.addr_lo = receipt.addr_hi = receipt.flags = 0;
		receipt.request_header = receipt.request_packet = receipt.request_generation = 0;
		receipt.submitted = receipt.returned = 0;
		receipt.rx_header = receipt.rx_packet = receipt.rx_flags = 0;
		receipt.rx_buffer_flags = receipt.rx_generation = receipt.rx_result = 0;
	}
	receipt.session = inst->session_id;
	receipt.lifecycle_generation = inst->repeat_lifecycle_generation;
	receipt.lifecycle_stage = inst->repeat_lifecycle_stage;
	receipt.ah = inst->repeat_stop[0].request_header;
	receipt.ap = inst->repeat_stop[0].request_packet;
	receipt.arh = inst->repeat_stop[0].ack_header;
	receipt.arp = inst->repeat_stop[0].ack_packet;
	receipt.arf = inst->repeat_stop[0].ack_flags;
	receipt.aa = inst->repeat_stop[0].ack;
	receipt.bh = inst->repeat_stop[1].request_header;
	receipt.bp = inst->repeat_stop[1].request_packet;
	receipt.brh = inst->repeat_stop[1].ack_header;
	receipt.brp = inst->repeat_stop[1].ack_packet;
	receipt.brf = inst->repeat_stop[1].ack_flags;
	receipt.ba = inst->repeat_stop[1].ack;
	receipt.stop_blocked = inst->stop_submit_blocked;
	receipt.close_attempted = inst->repeat_close_attempted;
	receipt.cpu_drained = inst->repeat_cpu_drained;
	receipt.orphaned = inst->orphaned;
	mutex_unlock(&inst->lock);
	return copy_to_user((void __user *)arg, &receipt, sizeof(receipt)) ? -EFAULT : 0;
}

static long iris_experiment_ioctl(struct file *filp, unsigned int cmd, unsigned long arg)
{
	struct iris_inst *inst = iris_get_inst(filp);
	long ret;

	if (cmd == IRIS_REPEAT_RECEIPT_IOCTL)
		return iris_repeat_receipt_read(inst, arg);
	if (cmd == IRIS_REPEAT_CONTROL_IOCTL) {
		struct iris_repeat_control control;

		if (!inst->core->native_repeat)
			return -EOPNOTSUPP;
		if (copy_from_user(&control, (void __user *)arg, sizeof(control)))
			return -EFAULT;
		if (control.version != 1 || control.session != inst->session_id ||
		    control.generation != inst->repeat_lifecycle_generation || control.action != 1)
			return -ESTALE;
		mutex_lock(&inst->decode_ioctl_lock);
		control.result = iris_repeat_quiesce(inst);
		control.stage = inst->repeat_lifecycle_stage;
		mutex_unlock(&inst->decode_ioctl_lock);
		if (copy_to_user((void __user *)arg, &control, sizeof(control)))
			return -EFAULT;
		return control.result;
	}

	/* No framework cancellation is admitted by the first-HOLD control. */
	if (inst->core->native_repeat && !IRIS_NATIVE_REPEAT_RECLAIM_ENABLED) {
		if (cmd == VIDIOC_STREAMOFF)
			return -EOPNOTSUPP;
		if (cmd == VIDIOC_REQBUFS) {
			struct v4l2_requestbuffers req;

			if (copy_from_user(&req, (void __user *)arg, sizeof(req)))
				return -EFAULT;
			if (!req.count)
				return -EOPNOTSUPP;
		}
	}
	if (cmd == IRIS_STOP_IOCTL) {
		mutex_lock(&inst->decode_ioctl_lock);
		ret = iris_stop_observer_command(inst,arg);
		mutex_unlock(&inst->decode_ioctl_lock);
		return ret;
	}
	if (!READ_ONCE(inst->core->decode_once))
		return video_ioctl2(filp, cmd, arg);
	mutex_lock(&inst->decode_ioctl_lock);
	if (inst->stop_submit_blocked && !(inst->core->native_repeat &&
	    inst->repeat_cpu_drained && (cmd == VIDIOC_STREAMOFF || cmd == VIDIOC_REQBUFS))) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	if (READ_ONCE(inst->core->terminal_shutdown)) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	switch (cmd) {
	case VIDIOC_STREAMON: {
		enum v4l2_buf_type type;
		struct vb2_queue *q;

		if (!READ_ONCE(inst->core->decode_once_retention_ready)) {
			ret = -EOPNOTSUPP;
			goto unlock;
		}
		if (get_user(type, (enum v4l2_buf_type __user *)arg)) {
			ret = -EFAULT;
			goto unlock;
		}
		q = v4l2_m2m_get_vq(inst->m2m_ctx, type);
		if (!iris_decode_once_backing_pinned(inst, q)) {
			ret = -EPERM;
			goto unlock;
		}
		break;
	}
	case VIDIOC_STREAMOFF:
		if (inst->core->native_repeat && inst->repeat_cpu_drained)
			break;
		ret = -EOPNOTSUPP;
		goto unlock;
	case VIDIOC_CREATE_BUFS:
	case VIDIOC_REMOVE_BUFS:
	case VIDIOC_EXPBUF:
		ret = -EOPNOTSUPP;
		goto unlock;
	default:
		break;
	}
	ret = video_ioctl2(filp, cmd, arg);
unlock:
	mutex_unlock(&inst->decode_ioctl_lock);
	return ret;
}

static int iris_repeat_quiesce(struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	unsigned long timeout = msecs_to_jiffies(core->iris_platform_data->hw_response_timeout);
	u32 plane;
	int ret = 0;

	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&inst->lock);
	if (inst->repeat_close_attempted) {
		mutex_unlock(&inst->lock);
		mutex_unlock(&core->lifecycle_lock);
		return -EALREADY;
	}
	inst->stop_submit_blocked = true;
	inst->repeat_close_attempted = true;
	inst->repeat_lifecycle_stage = 1;
	if (inst->state != IRIS_INST_STREAMING || core->state != IRIS_CORE_INIT ||
	    inst->response_closing || !inst->stop_last_dq || !inst->decode_source_change_seen ||
	    inst->sub_state & (IRIS_INST_SUB_DRC | IRIS_INST_SUB_DRC_LAST)) {
		ret = -EOPNOTSUPP;
		goto failed;
	}
	reinit_completion(&inst->stop_app_returned);
	inst->stop_attempted = true;
	for (plane = 0; plane < 2; plane++) {
		ret = core->hfi_ops->session_stop(inst, plane ?
			V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE : V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
		if (ret || !inst->repeat_stop[plane].ack) {
			if (!ret)
				ret = -EIO;
			goto failed;
		}
	}
	if (iris_repeat_apps_owned(inst, -1)) {
		mutex_unlock(&inst->lock);
		wait_for_completion_timeout(&inst->stop_app_returned, timeout);
		mutex_lock(&inst->lock);
	}
	if (iris_repeat_apps_owned(inst, -1) || inst->state == IRIS_INST_ERROR) {
		ret = -ETIMEDOUT;
		goto failed;
	}
	inst->repeat_lifecycle_stage = 2;
	reinit_completion(&inst->repeat_internal_returned);
	ret = iris_repeat_release_internal(inst);
	if (ret)
		goto failed;
	if (iris_repeat_internal_owned(inst)) {
		mutex_unlock(&inst->lock);
		wait_for_completion_timeout(&inst->repeat_internal_returned, timeout);
		mutex_lock(&inst->lock);
	}
	if (iris_repeat_internal_owned(inst) || inst->state == IRIS_INST_ERROR) {
		ret = -ETIMEDOUT;
		goto failed;
	}
	inst->repeat_lifecycle_stage = 3;
	ret = iris_session_close_checked(inst);
	if (ret)
		goto failed;
	inst->repeat_lifecycle_stage = 4;
	iris_remove_session(inst);
	mutex_unlock(&inst->lock);
	iris_put_response_instance(inst);
	if (!wait_for_completion_timeout(&inst->response_idle, timeout)) {
		/* Restore safe routing/base ownership if a CPU callback is still live. */
		mutex_lock(&core->lock);
		if (refcount_inc_not_zero(&inst->response_refs)) {
			inst->response_closing = false;
			list_add_tail(&inst->list, &core->instances);
			mutex_unlock(&core->lock);
			mutex_lock(&inst->lock);
			ret = -ETIMEDOUT;
			goto failed;
		}
		mutex_unlock(&core->lock);
		/* Zero refcount precedes complete(): observe the completion itself. */
		if (!wait_for_completion_timeout(&inst->response_idle, timeout)) {
			mutex_lock(&inst->lock);
			ret = -ETIMEDOUT;
			goto failed;
		}
	}
	mutex_lock(&inst->lock);
	inst->repeat_cpu_drained = true;
	inst->repeat_lifecycle_stage = 5;
	mutex_unlock(&inst->lock);
	mutex_unlock(&core->lifecycle_lock);
	return 0;

failed:
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	mutex_unlock(&inst->lock);
	mutex_unlock(&core->lifecycle_lock);
	return ret;
}

static int iris_repeat_close(struct file *filp, struct iris_inst *inst)
{
	struct iris_core *core = inst->core;
	struct iris_buffer *buf, *next;
	u32 type, plane, index;
	int ret = IRIS_NATIVE_REPEAT_RECLAIM_ENABLED ?
		(inst->repeat_cpu_drained ? 0 : iris_repeat_quiesce(inst)) : -EOPNOTSUPP;

	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&inst->lock);
	if (ret)
		goto orphan;
	/* Whole-session predicate and CPU drain precede every physical release. */
	for (type = BUF_BIN; type < BUF_TYPE_MAX; type++)
		list_for_each_entry_safe(buf, next, &inst->buffers[type].list, list) {
			ret = iris_destroy_internal_buffer(inst, buf);
			if (ret)
				goto orphan;
		}
	mutex_unlock(&inst->lock);
	for (plane = 0; plane < 2; plane++)
		for (index = 0; index < VIDEO_MAX_FRAME; index++)
			if (inst->decode_pins[plane].dbuf[index]) {
				dma_buf_put(inst->decode_pins[plane].dbuf[index]);
				inst->decode_pins[plane].dbuf[index] = NULL;
			}
	v4l2_m2m_ctx_release(inst->m2m_ctx);
	v4l2_m2m_release(inst->m2m_dev);
	v4l2_ctrl_handler_free(&inst->ctrl_handler);
	kfree(to_iris_inst_hfi_gen2(inst)->packet);
	to_iris_inst_hfi_gen2(inst)->packet = NULL;
	iris_vdec_inst_deinit(inst);
	iris_v4l2_fh_deinit(inst, filp);
	mutex_destroy(&inst->decode_ioctl_lock);
	mutex_destroy(&inst->ctx_q_lock);
	mutex_destroy(&inst->lock);
	kfree(inst);
	iris_core_open_put(core);
	mutex_unlock(&core->lifecycle_lock);
	return 0;

orphan:
	/* Failure never cancels queues. Detach the actual dying file, retaining
	 * event storage, allocator owners and safe lookup/base routing if live.
	 * A post-drain free refusal retains CPU-drained storage, without rerouting.
	 */
	inst->orphaned = true;
	inst->file_detached = true;
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	v4l2_fh_del(&inst->fh, filp);
	inst->fh.ctrl_handler = NULL;
	mutex_unlock(&inst->lock);
	iris_core_orphan_transfer(core);
	mutex_unlock(&core->lifecycle_lock);
	return ret;
}

int iris_close(struct file *filp)
{
	struct iris_inst *inst = iris_get_inst(filp);
	struct iris_core *core = inst->core;
	struct vb2_queue *src, *dst;
	int ret = 0, err;

	if (core->native_repeat)
		return iris_repeat_close(filp, inst);
	if (iris_decode_once_retain_close(inst))
		return 0;
	src = v4l2_m2m_get_src_vq(inst->m2m_ctx);
	dst = v4l2_m2m_get_dst_vq(inst->m2m_ctx);
	mutex_lock(&inst->core->lifecycle_lock);
	/* Finish vb2 stop callbacks while the session command packet is alive. */
	mutex_lock(&inst->ctx_q_lock);
	if (vb2_is_streaming(src)) {
		err = vb2_streamoff(src, src->type);
		if (err)
			ret = err;
	}
	if (vb2_is_streaming(dst)) {
		err = vb2_streamoff(dst, dst->type);
		if (err && !ret)
			ret = err;
	}
	mutex_unlock(&inst->ctx_q_lock);
	mutex_lock(&inst->lock);
	if (ret || inst->state == IRIS_INST_ERROR) {
		if (!ret)
			ret = -EIO;
		goto uncertain;
	}
	ret = iris_session_close_checked(inst);
	if (ret)
		goto uncertain;
	/* A CLOSE ACK does not retire firmware-owned DMA allocations. Keep the
	 * file/framework owner intact until every fallible buffer free succeeds. */
	ret = iris_destroy_all_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	if (!ret)
		ret = iris_destroy_all_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
	if (ret)
		goto uncertain;
	iris_inst_change_state(inst, IRIS_INST_DEINIT);
	iris_remove_session(inst);
	mutex_unlock(&inst->lock);
	/* Drain response owners before destroying any response-visible resources. */
	iris_put_response_instance(inst);
	wait_for_completion(&inst->response_idle);
	v4l2_ctrl_handler_free(&inst->ctrl_handler);
	v4l2_m2m_ctx_release(inst->m2m_ctx);
	v4l2_m2m_release(inst->m2m_dev);
	mutex_lock(&inst->lock);
	if (inst->domain == DECODER)
		iris_vdec_inst_deinit(inst);
	else if (inst->domain == ENCODER)
		iris_venc_inst_deinit(inst);
	iris_v4l2_fh_deinit(inst, filp);
	iris_check_num_queued_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE);
	iris_check_num_queued_internal_buffers(inst, V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE);
	mutex_unlock(&inst->lock);
	mutex_destroy(&inst->ctx_q_lock);
	mutex_destroy(&inst->lock);
	mutex_unlock(&inst->core->lifecycle_lock);
	kfree(inst);
	iris_core_open_put(core);
	return 0;

uncertain:
	/* File release cannot depend on a caller retry. Transfer ownership to core. */
	iris_inst_change_state(inst, IRIS_INST_ERROR);
	get_device(inst->core->dev);
	iris_remove_session(inst);
	mutex_unlock(&inst->lock);
	iris_put_response_instance(inst);
	wait_for_completion(&inst->response_idle);
	mutex_lock(&inst->lock);
	/* Detach the live file/fh only after all CPU response owners have exited. */
	iris_v4l2_fh_deinit(inst, filp);
	inst->file_detached = true;
	mutex_lock(&inst->core->lock);
	inst->orphaned = true;
	list_add_tail(&inst->list, &inst->core->orphan_instances);
	mutex_unlock(&inst->core->lock);
	mutex_unlock(&inst->lock);
	iris_core_orphan_transfer(core);
	mutex_unlock(&inst->core->lifecycle_lock);
	return ret;
}

static int iris_enum_fmt(struct file *filp, void *fh, struct v4l2_fmtdesc *f)
{
	struct iris_inst *inst = iris_get_inst(filp);

	if (inst->domain == DECODER)
		return iris_vdec_enum_fmt(inst, f);
	else if (inst->domain == ENCODER)
		return iris_venc_enum_fmt(inst, f);
	else
		return -EINVAL;
}

static int iris_try_fmt_vid_mplane(struct file *filp, void *fh, struct v4l2_format *f)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = 0;

	mutex_lock(&inst->lock);

	if (inst->domain == DECODER)
		ret = iris_vdec_try_fmt(inst, f);
	else if (inst->domain == ENCODER)
		ret = iris_venc_try_fmt(inst, f);

	mutex_unlock(&inst->lock);

	return ret;
}

static int iris_s_fmt_vid_mplane(struct file *filp, void *fh, struct v4l2_format *f)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = 0;

	mutex_lock(&inst->lock);

	if (inst->domain == DECODER)
		ret = iris_vdec_s_fmt(inst, f);
	else if (inst->domain == ENCODER)
		ret = iris_venc_s_fmt(inst, f);

	mutex_unlock(&inst->lock);

	return ret;
}

static int iris_g_fmt_vid_mplane(struct file *filp, void *fh, struct v4l2_format *f)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = 0;

	mutex_lock(&inst->lock);
	if (V4L2_TYPE_IS_OUTPUT(f->type))
		*f = *inst->fmt_src;
	else if (V4L2_TYPE_IS_CAPTURE(f->type))
		*f = *inst->fmt_dst;
	else
		ret = -EINVAL;

	mutex_unlock(&inst->lock);

	return ret;
}

static int iris_enum_framesizes(struct file *filp, void *fh,
				struct v4l2_frmsizeenum *fsize)
{
	struct iris_inst *inst = iris_get_inst(filp);
	struct platform_inst_caps *caps;
	int ret = 0;

	if (fsize->index)
		return -EINVAL;

	if (inst->domain == DECODER)
		ret = iris_vdec_validate_format(inst, fsize->pixel_format);
	else
		ret = iris_venc_validate_format(inst, fsize->pixel_format);

	if (ret)
		return ret;

	caps = inst->core->iris_platform_data->inst_caps;

	fsize->type = V4L2_FRMSIZE_TYPE_STEPWISE;
	fsize->stepwise.min_width = caps->min_frame_width;
	fsize->stepwise.max_width = caps->max_frame_width;
	fsize->stepwise.step_width = STEP_WIDTH;
	fsize->stepwise.min_height = caps->min_frame_height;
	fsize->stepwise.max_height = caps->max_frame_height;
	fsize->stepwise.step_height = STEP_HEIGHT;

	return ret;
}

static int iris_enum_frameintervals(struct file *filp, void *fh,
				    struct v4l2_frmivalenum *fival)

{
	struct iris_inst *inst = iris_get_inst(filp);
	struct iris_core *core = inst->core;
	struct platform_inst_caps *caps;
	u32 fps, mbpf;
	int ret = 0;

	if (inst->domain == DECODER)
		return -ENOTTY;

	if (fival->index)
		return -EINVAL;

	ret = iris_venc_validate_format(inst, fival->pixel_format);
	if (ret)
		return ret;

	if (!fival->width || !fival->height)
		return -EINVAL;

	caps = inst->core->iris_platform_data->inst_caps;
	if (fival->width > caps->max_frame_width ||
	    fival->width < caps->min_frame_width ||
	    fival->height > caps->max_frame_height ||
	    fival->height < caps->min_frame_height)
		return -EINVAL;

	mbpf = NUM_MBS_PER_FRAME(fival->height, fival->width);
	fps = DIV_ROUND_UP(core->iris_platform_data->max_core_mbps, mbpf);

	fival->type = V4L2_FRMIVAL_TYPE_STEPWISE;
	fival->stepwise.min.numerator = 1;
	fival->stepwise.min.denominator =
			min_t(u32, fps, MAXIMUM_FPS);
	fival->stepwise.max.numerator = 1;
	fival->stepwise.max.denominator = 1;
	fival->stepwise.step.numerator = 1;
	fival->stepwise.step.denominator = MAXIMUM_FPS;

	return 0;
}

static int iris_querycap(struct file *filp, void *fh, struct v4l2_capability *cap)
{
	struct iris_inst *inst = iris_get_inst(filp);

	strscpy(cap->driver, IRIS_DRV_NAME, sizeof(cap->driver));

	if (inst->domain == DECODER)
		strscpy(cap->card, "Iris Decoder", sizeof(cap->card));
	else
		strscpy(cap->card, "Iris Encoder", sizeof(cap->card));

	return 0;
}

static int iris_g_selection(struct file *filp, void *fh, struct v4l2_selection *s)
{
	struct iris_inst *inst = iris_get_inst(filp);

	if (s->type != V4L2_BUF_TYPE_VIDEO_CAPTURE &&
	    inst->domain == DECODER)
		return -EINVAL;

	if (s->type != V4L2_BUF_TYPE_VIDEO_OUTPUT &&
	    inst->domain == ENCODER)
		return -EINVAL;

	if (inst->domain == DECODER) {
		switch (s->target) {
		case V4L2_SEL_TGT_CROP_BOUNDS:
		case V4L2_SEL_TGT_CROP_DEFAULT:
		case V4L2_SEL_TGT_CROP:
		case V4L2_SEL_TGT_COMPOSE_BOUNDS:
		case V4L2_SEL_TGT_COMPOSE_PADDED:
		case V4L2_SEL_TGT_COMPOSE_DEFAULT:
		case V4L2_SEL_TGT_COMPOSE:
			s->r.left = inst->crop.left;
			s->r.top = inst->crop.top;
			s->r.width = inst->crop.width;
			s->r.height = inst->crop.height;
			break;
		default:
			return -EINVAL;
		}
	} else if (inst->domain == ENCODER) {
		switch (s->target) {
		case V4L2_SEL_TGT_CROP_BOUNDS:
		case V4L2_SEL_TGT_CROP_DEFAULT:
			s->r.width = inst->fmt_src->fmt.pix_mp.width;
			s->r.height = inst->fmt_src->fmt.pix_mp.height;
			break;
		case V4L2_SEL_TGT_CROP:
			s->r.width = inst->crop.width;
			s->r.height = inst->crop.height;
			break;
		default:
			return -EINVAL;
		}
		s->r.left = inst->crop.left;
		s->r.top = inst->crop.top;
	}

	return 0;
}

static int iris_s_selection(struct file *filp, void *fh, struct v4l2_selection *s)
{
	struct iris_inst *inst = iris_get_inst(filp);

	if (inst->domain == DECODER)
		return -EINVAL;
	else if (inst->domain == ENCODER)
		return iris_venc_s_selection(inst, s);

	return -EINVAL;
}

static int iris_subscribe_event(struct v4l2_fh *fh, const struct v4l2_event_subscription *sub)
{
	struct iris_inst *inst = container_of(fh, struct iris_inst, fh);

	if (inst->domain == DECODER)
		return iris_vdec_subscribe_event(inst, sub);
	else if (inst->domain == ENCODER)
		return iris_venc_subscribe_event(inst, sub);

	return -EINVAL;
}

static int iris_s_parm(struct file *filp, void *fh, struct v4l2_streamparm *a)
{
	struct iris_inst *inst = iris_get_inst(filp);

	if (a->type != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE &&
	    a->type != V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
		return -EINVAL;

	if (inst->domain == ENCODER)
		return iris_venc_s_param(inst, a);
	else
		return -EINVAL;
}

static int iris_g_parm(struct file *filp, void *fh, struct v4l2_streamparm *a)
{
	struct iris_inst *inst = iris_get_inst(filp);

	if (a->type != V4L2_BUF_TYPE_VIDEO_CAPTURE_MPLANE &&
	    a->type != V4L2_BUF_TYPE_VIDEO_OUTPUT_MPLANE)
		return -EINVAL;

	if (inst->domain == ENCODER)
		return iris_venc_g_param(inst, a);
	else
		return -EINVAL;
}

static int iris_dec_cmd(struct file *filp, void *fh,
			struct v4l2_decoder_cmd *dec)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = 0;

	mutex_lock(&inst->lock);

	/* EOS drain is supported; resume/flush/seek is explicitly not claimed. */
	if (inst->core->native_repeat && dec->cmd != V4L2_DEC_CMD_STOP) {
		ret = -EOPNOTSUPP;
		goto unlock;
	}
	if (dec->cmd != V4L2_DEC_CMD_STOP && dec->cmd != V4L2_DEC_CMD_START) {
		ret = -EINVAL;
		goto unlock;
	}

	if (inst->state == IRIS_INST_DEINIT)
		goto unlock;

	if (!iris_allow_cmd(inst, dec->cmd)) {
		ret = -EBUSY;
		goto unlock;
	}

	if (dec->cmd == V4L2_DEC_CMD_START)
		ret = iris_vdec_start_cmd(inst);
	else if (dec->cmd == V4L2_DEC_CMD_STOP)
		ret = iris_vdec_stop_cmd(inst);
	else
		ret = -EINVAL;

unlock:
	mutex_unlock(&inst->lock);

	return ret;
}

static int iris_enc_cmd(struct file *filp, void *fh,
			struct v4l2_encoder_cmd *enc)
{
	struct iris_inst *inst = iris_get_inst(filp);
	int ret = 0;

	mutex_lock(&inst->lock);

	if (enc->cmd != V4L2_ENC_CMD_STOP && enc->cmd != V4L2_ENC_CMD_START) {
		ret = -EINVAL;
		goto unlock;
	}

	if (inst->state == IRIS_INST_DEINIT)
		goto unlock;

	if (!iris_allow_cmd(inst, enc->cmd)) {
		ret = -EBUSY;
		goto unlock;
	}

	if (enc->cmd == V4L2_ENC_CMD_START)
		ret = iris_venc_start_cmd(inst);
	else if (enc->cmd == V4L2_ENC_CMD_STOP)
		ret = iris_venc_stop_cmd(inst);
	else
		ret = -EINVAL;

unlock:
	mutex_unlock(&inst->lock);

	return ret;
}

static struct v4l2_file_operations iris_v4l2_file_ops = {
	.owner                          = THIS_MODULE,
	.open                           = iris_open,
	.release                        = iris_close,
	.unlocked_ioctl                 = iris_experiment_ioctl,
	.poll                           = v4l2_m2m_fop_poll,
	.mmap                           = v4l2_m2m_fop_mmap,
};

static const struct vb2_ops iris_vb2_ops = {
	.buf_init                       = iris_vb2_buf_init,
	.queue_setup                    = iris_vb2_queue_setup,
	.start_streaming                = iris_vb2_start_streaming,
	.stop_streaming                 = iris_vb2_stop_streaming,
	.buf_prepare                    = iris_vb2_buf_prepare,
	.buf_out_validate               = iris_vb2_buf_out_validate,
	.buf_queue                      = iris_vb2_buf_queue,
};

static const struct v4l2_ioctl_ops iris_v4l2_ioctl_ops_dec = {
	.vidioc_enum_fmt_vid_cap        = iris_enum_fmt,
	.vidioc_enum_fmt_vid_out        = iris_enum_fmt,
	.vidioc_try_fmt_vid_cap_mplane  = iris_try_fmt_vid_mplane,
	.vidioc_try_fmt_vid_out_mplane  = iris_try_fmt_vid_mplane,
	.vidioc_s_fmt_vid_cap_mplane    = iris_s_fmt_vid_mplane,
	.vidioc_s_fmt_vid_out_mplane    = iris_s_fmt_vid_mplane,
	.vidioc_g_fmt_vid_cap_mplane    = iris_g_fmt_vid_mplane,
	.vidioc_g_fmt_vid_out_mplane    = iris_g_fmt_vid_mplane,
	.vidioc_enum_framesizes         = iris_enum_framesizes,
	.vidioc_reqbufs                 = iris_decode_once_reqbufs,
	.vidioc_querybuf                = v4l2_m2m_ioctl_querybuf,
	.vidioc_create_bufs             = v4l2_m2m_ioctl_create_bufs,
	.vidioc_prepare_buf             = v4l2_m2m_ioctl_prepare_buf,
	.vidioc_expbuf                  = v4l2_m2m_ioctl_expbuf,
	.vidioc_qbuf                    = v4l2_m2m_ioctl_qbuf,
	.vidioc_dqbuf                   = iris_stop_observer_dqbuf,
	.vidioc_remove_bufs             = v4l2_m2m_ioctl_remove_bufs,
	.vidioc_querycap                = iris_querycap,
	.vidioc_g_selection             = iris_g_selection,
	.vidioc_subscribe_event         = iris_subscribe_event,
	.vidioc_unsubscribe_event       = v4l2_event_unsubscribe,
	.vidioc_streamon                = v4l2_m2m_ioctl_streamon,
	.vidioc_streamoff               = v4l2_m2m_ioctl_streamoff,
	.vidioc_try_decoder_cmd         = v4l2_m2m_ioctl_try_decoder_cmd,
	.vidioc_decoder_cmd             = iris_dec_cmd,
};

static const struct v4l2_ioctl_ops iris_v4l2_ioctl_ops_enc = {
	.vidioc_enum_fmt_vid_cap        = iris_enum_fmt,
	.vidioc_enum_fmt_vid_out        = iris_enum_fmt,
	.vidioc_try_fmt_vid_cap_mplane  = iris_try_fmt_vid_mplane,
	.vidioc_try_fmt_vid_out_mplane  = iris_try_fmt_vid_mplane,
	.vidioc_s_fmt_vid_cap_mplane    = iris_s_fmt_vid_mplane,
	.vidioc_s_fmt_vid_out_mplane    = iris_s_fmt_vid_mplane,
	.vidioc_g_fmt_vid_cap_mplane    = iris_g_fmt_vid_mplane,
	.vidioc_g_fmt_vid_out_mplane    = iris_g_fmt_vid_mplane,
	.vidioc_enum_framesizes         = iris_enum_framesizes,
	.vidioc_enum_frameintervals     = iris_enum_frameintervals,
	.vidioc_querycap                = iris_querycap,
	.vidioc_subscribe_event         = iris_subscribe_event,
	.vidioc_unsubscribe_event       = v4l2_event_unsubscribe,
	.vidioc_g_selection             = iris_g_selection,
	.vidioc_s_selection             = iris_s_selection,
	.vidioc_s_parm                  = iris_s_parm,
	.vidioc_g_parm                  = iris_g_parm,
	.vidioc_streamon                = v4l2_m2m_ioctl_streamon,
	.vidioc_streamoff               = v4l2_m2m_ioctl_streamoff,
	.vidioc_reqbufs                 = v4l2_m2m_ioctl_reqbufs,
	.vidioc_querybuf                = v4l2_m2m_ioctl_querybuf,
	.vidioc_create_bufs             = v4l2_m2m_ioctl_create_bufs,
	.vidioc_prepare_buf             = v4l2_m2m_ioctl_prepare_buf,
	.vidioc_expbuf                  = v4l2_m2m_ioctl_expbuf,
	.vidioc_qbuf                    = v4l2_m2m_ioctl_qbuf,
	.vidioc_dqbuf                   = iris_stop_observer_dqbuf,
	.vidioc_remove_bufs             = v4l2_m2m_ioctl_remove_bufs,
	.vidioc_try_encoder_cmd         = v4l2_m2m_ioctl_try_encoder_cmd,
	.vidioc_encoder_cmd             = iris_enc_cmd,
};

void iris_init_ops(struct iris_core *core)
{
	core->iris_v4l2_file_ops = &iris_v4l2_file_ops;
	core->iris_vb2_ops = &iris_vb2_ops;
	core->iris_v4l2_ioctl_ops_dec = &iris_v4l2_ioctl_ops_dec;
	core->iris_v4l2_ioctl_ops_enc = &iris_v4l2_ioctl_ops_enc;
}
