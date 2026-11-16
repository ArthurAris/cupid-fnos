// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/pm_runtime.h>
#include <linux/lockdep.h>

#include "iris_firmware.h"
#include "iris_core.h"
#include "iris_hfi_common.h"
#include "iris_vpu_common.h"

u32 iris_hfi_get_v4l2_color_primaries(u32 hfi_primaries)
{
	switch (hfi_primaries) {
	case HFI_PRIMARIES_RESERVED:
		return V4L2_COLORSPACE_DEFAULT;
	case HFI_PRIMARIES_BT709:
		return V4L2_COLORSPACE_REC709;
	case HFI_PRIMARIES_BT470_SYSTEM_M:
		return V4L2_COLORSPACE_470_SYSTEM_M;
	case HFI_PRIMARIES_BT470_SYSTEM_BG:
		return V4L2_COLORSPACE_470_SYSTEM_BG;
	case HFI_PRIMARIES_BT601_525:
		return V4L2_COLORSPACE_SMPTE170M;
	case HFI_PRIMARIES_SMPTE_ST240M:
		return V4L2_COLORSPACE_SMPTE240M;
	case HFI_PRIMARIES_BT2020:
		return V4L2_COLORSPACE_BT2020;
	case V4L2_COLORSPACE_DCI_P3:
		return HFI_PRIMARIES_SMPTE_RP431_2;
	default:
		return V4L2_COLORSPACE_DEFAULT;
	}
}

u32 iris_hfi_get_v4l2_transfer_char(u32 hfi_characterstics)
{
	switch (hfi_characterstics) {
	case HFI_TRANSFER_RESERVED:
		return V4L2_XFER_FUNC_DEFAULT;
	case HFI_TRANSFER_BT709:
		return V4L2_XFER_FUNC_709;
	case HFI_TRANSFER_SMPTE_ST240M:
		return V4L2_XFER_FUNC_SMPTE240M;
	case HFI_TRANSFER_SRGB_SYCC:
		return V4L2_XFER_FUNC_SRGB;
	case HFI_TRANSFER_SMPTE_ST2084_PQ:
		return V4L2_XFER_FUNC_SMPTE2084;
	default:
		return V4L2_XFER_FUNC_DEFAULT;
	}
}

u32 iris_hfi_get_v4l2_matrix_coefficients(u32 hfi_coefficients)
{
	switch (hfi_coefficients) {
	case HFI_MATRIX_COEFF_RESERVED:
		return V4L2_YCBCR_ENC_DEFAULT;
	case HFI_MATRIX_COEFF_BT709:
		return V4L2_YCBCR_ENC_709;
	case HFI_MATRIX_COEFF_BT470_SYS_BG_OR_BT601_625:
		return V4L2_YCBCR_ENC_XV601;
	case HFI_MATRIX_COEFF_BT601_525_BT1358_525_OR_625:
		return V4L2_YCBCR_ENC_601;
	case HFI_MATRIX_COEFF_SMPTE_ST240:
		return V4L2_YCBCR_ENC_SMPTE240M;
	case HFI_MATRIX_COEFF_BT2020_NON_CONSTANT:
		return V4L2_YCBCR_ENC_BT2020;
	case HFI_MATRIX_COEFF_BT2020_CONSTANT:
		return V4L2_YCBCR_ENC_BT2020_CONST_LUM;
	default:
		return V4L2_YCBCR_ENC_DEFAULT;
	}
}

int iris_hfi_core_init(struct iris_core *core)
{
	const struct iris_hfi_command_ops *hfi_ops = core->hfi_ops;
	int ret;

	ret = hfi_ops->sys_init(core);
	if (ret || core->init_only)
		return ret; /* One SYS_INIT only: no image-version or power-collapse. */

	ret = hfi_ops->sys_image_version(core);
	if (ret)
		return ret;

	return hfi_ops->sys_interframe_powercollapse(core);
}

int iris_irq_activate(struct iris_core *core)
{
	unsigned long flags;
	int ret = 0;

	lockdep_assert_held(&core->lock);
	/* Caller owns core->lock; this orders activation with terminal publication. */
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	if (!core->irq_registered || core->irq_releasing || !core->irq_managed || !core->hardware_owner_held ||
	    !core->queues_allocated || !core->power_prepared || !core->firmware_booted ||
	    core->state != IRIS_CORE_INIT || core->terminal_shutdown || core->irq_quiescing ||
	    core->resources_releasing || core->irq_started || !core->irq_initial_mask_held ||
	    core->irq_top_mask_held || core->quiesce_irq_mask_held) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	core->irq_initial_mask_held = false;
	core->irq_accepting = true;
	core->irq_started = true;
	core->power_irq_enabled = true;
unlock:
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	/* enable may invoke a chip bus lock: never call it under our raw spinlock. */
	if (!ret)
		enable_irq(core->irq);
	return ret;
}

void iris_irq_terminal_mask(struct iris_core *core)
{
	unsigned long flags;
	bool disable = false;

	/* Caller owns core->lock. Top-half never takes that mutex. */
	lockdep_assert_held(&core->lock);
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	if (!core->irq_registered || core->irq_releasing || !core->hardware_owner_held ||
	    core->resources_releasing) {
		raw_spin_unlock_irqrestore(&core->irq_lock, flags);
		return;
	}
	core->irq_accepting = false;
	if (!core->quiesce_irq_mask_held) {
		if (core->irq_managed && core->irq_initial_mask_held) {
			/* Transfer NO_AUTOEN ownership rather than acquire a duplicate mask. */
			core->irq_initial_mask_held = false;
		} else {
			disable = true;
		}
		core->quiesce_irq_mask_held = true;
	}
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	if (disable)
		disable_irq_nosync(core->irq);
}

void iris_irq_retire_top(struct iris_core *core)
{
	unsigned long flags;
	bool retire = false;

	/* After synchronize_irq, terminal owns the permanent mask. */
	lockdep_assert_held(&core->lock);
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	if (!core->irq_registered || core->irq_releasing || !core->hardware_owner_held ||
	    core->resources_releasing) {
		raw_spin_unlock_irqrestore(&core->irq_lock, flags);
		return;
	}
	if (core->irq_managed && core->quiesce_irq_mask_held &&
	    core->irq_drain_verified && core->irq_top_mask_held) {
		core->irq_top_mask_held = false;
		retire = true;
	}
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	if (retire)
		enable_irq(core->irq);
}

int iris_irq_unregister(struct iris_core *core)
{
	unsigned long flags;
	int ret = 0;
	bool registered;

	if (in_interrupt())
		return -EWOULDBLOCK;
	/* Caller owns a CPU reference and must not be a drained IRQ callback. */
	lockdep_assert_not_held(&core->lock);
	lockdep_assert_not_held(&core->lifecycle_lock);
	lockdep_assert_not_held(&core->irq_lock);
	mutex_lock(&core->lock);
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	registered = core->irq_registered;
	if (!registered)
		goto unlock;
	if (core->irq_releasing) {
		ret = -EBUSY;
		goto unlock;
	}
	/* An attempted init-only run retains IRQ/code ownership until reset. */
	if (core->init_only && core->init_only_attempted) {
		ret = -EBUSY;
		goto unlock;
	}
	/* Only unpublished probe unwind or a completed terminal release may free. */
	if (!((core->irq_probe_unpublished && core->removing && !core->irq_started) ||
	      (core->resources_releasing && core->remove_prepared && core->terminal_shutdown &&
	       core->irq_drain_verified && core->recovery_drain_verified))) {
		ret = -EBUSY;
		goto unlock;
	}
	if (!core->irq_probe_unpublished)
		lockdep_assert_held(&core->quiesce_lock);
	core->irq_accepting = false;
	core->irq_releasing = true;
unlock:
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	mutex_unlock(&core->lock);
	if (ret || !registered)
		return ret;
	/* Admission is revoked; all process helper decisions/chip calls were serialized. */
	synchronize_irq(core->irq);
	free_irq(core->irq, core);
	mutex_lock(&core->lock);
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	core->irq_registered = false;
	core->irq_started = false;
	core->irq_initial_mask_held = false;
	core->irq_top_mask_held = false;
	core->quiesce_irq_mask_held = false;
	core->power_irq_enabled = false;
	core->irq_probe_unpublished = false;
	/* Keep irq_releasing/terminal revocation: the object must not start again. */
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	mutex_unlock(&core->lock);
	return 0;
}

irqreturn_t iris_hfi_isr(int irq, void *data)
{
	struct iris_core *core = data;
	unsigned long flags;

	if (!core)
		return IRQ_NONE;
	if (!core->irq_managed) {
		disable_irq_nosync(irq);
		return IRQ_WAKE_THREAD;
	}
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	if (!core->irq_registered || core->irq_releasing || !core->irq_accepting || core->irq_top_mask_held || !core->irq_started) {
		raw_spin_unlock_irqrestore(&core->irq_lock, flags);
		return IRQ_HANDLED;
	}
	core->irq_top_mask_held = true;
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	/* The target chip must support a hard-IRQ disable; no driver raw lock is held. */
	disable_irq_nosync(irq);
	return IRQ_WAKE_THREAD;
}

irqreturn_t iris_hfi_isr_handler(int irq, void *data)
{
	struct iris_core *core = data;
	unsigned long flags;
	bool retire = false, fault;

	if (!core)
		return IRQ_NONE;
	mutex_lock(&core->lock);
	if (core->irq_releasing || core->irq_quiescing || core->terminal_shutdown || core->resources_releasing ||
	    (core->irq_managed && (!core->irq_started || !core->firmware_booted ||
	     !core->queues_allocated || !core->power_prepared ||
	     core->state != IRIS_CORE_INIT))) {
		mutex_unlock(&core->lock);
		return IRQ_HANDLED;
	}
	pm_runtime_mark_last_busy(core->dev);
	iris_vpu_clear_interrupt(core);
	mutex_unlock(&core->lock);
	core->hfi_response_ops->hfi_response_handler(core);
	mutex_lock(&core->lock);
	if (core->irq_managed) {
		fault = iris_vpu_watchdog(core, core->intr_status);
		if (fault) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
		}
		raw_spin_lock_irqsave(&core->irq_lock, flags);
		if (core->state == IRIS_CORE_ERROR)
			core->irq_accepting = false;
		if (core->irq_top_mask_held && core->irq_accepting &&
		    !core->terminal_shutdown && !core->irq_quiescing &&
		    core->state == IRIS_CORE_INIT && !fault) {
			core->irq_top_mask_held = false;
			retire = true;
		}
		raw_spin_unlock_irqrestore(&core->irq_lock, flags);
		/* Fault/error tails retain TOP ownership until terminal drain retires it. */
		if (retire)
			enable_irq(irq);
	} else if (!core->irq_quiescing && !core->terminal_shutdown &&
		   !iris_vpu_watchdog(core, core->intr_status)) {
		enable_irq(irq);
	}
	mutex_unlock(&core->lock);
	return IRQ_HANDLED;
}
int iris_hfi_pm_suspend(struct iris_core *core)
{
	int ret;

	if (READ_ONCE(core->terminal_shutdown) || READ_ONCE(core->irq_quiescing))
		return -ESHUTDOWN;
	if (iris_vpu_has_checked_power(core)) {
		/* This private terminal-only path does not implement safe runtime recovery. */
		core->power_error = -EOPNOTSUPP;
		return -EOPNOTSUPP;
	}

	ret = iris_vpu_prepare_pc(core);
	if (ret) {
		pm_runtime_mark_last_busy(core->dev);
		ret = -EAGAIN;
		goto error;
	}

	ret = iris_set_hw_state(core, false);
	if (ret)
		goto error;

	if (iris_vpu_has_checked_power(core)) {
		ret = iris_vpu_power_off_checked(core);
		if (ret) {
			core->power_error = ret;
			pm_runtime_mark_last_busy(core->dev);
			goto error;
		}
	} else {
		iris_vpu_power_off(core);
	}

	return 0;

error:
	dev_err(core->dev, "failed to suspend\n");

	return ret;
}

int iris_hfi_pm_resume(struct iris_core *core)
{
	const struct iris_hfi_command_ops *ops = core->hfi_ops;
	int ret;

	if (READ_ONCE(core->terminal_shutdown) || READ_ONCE(core->irq_quiescing))
		return -ESHUTDOWN;
	if (iris_vpu_has_checked_power(core)) {
		/* This private terminal-only path does not implement safe runtime recovery. */
		core->power_error = -EOPNOTSUPP;
		return -EOPNOTSUPP;
	}

	if (iris_vpu_has_checked_power(core))
		ret = iris_vpu_power_on_checked(core);
	else
		ret = iris_vpu_power_on(core);
	if (ret)
		goto error;

	ret = iris_set_hw_state(core, true);
	if (ret)
		goto err_power_off;

	ret = iris_vpu_boot_firmware(core);
	if (ret)
		goto err_suspend_hw;

	ret = ops->sys_interframe_powercollapse(core);
	if (ret)
		goto err_suspend_hw;

	return 0;

err_suspend_hw:
	iris_set_hw_state(core, false);
err_power_off:
	if (iris_vpu_has_checked_power(core)) {
		if (iris_vpu_power_off_checked(core))
			core->power_state_unknown = true;
	} else {
		iris_vpu_power_off(core);
	}
error:
	dev_err(core->dev, "failed to resume\n");

	if (iris_vpu_has_checked_power(core) && ret) {
		core->power_error = ret;
		/* Checked platforms report the real step error. */
		return ret;
	}

	/* Legacy platforms have no checked contract; keep the historic errno. */
	return -EBUSY;
}
