// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/pm_runtime.h>
#include <linux/interrupt.h>
#include <linux/workqueue.h>
#include <linux/slab.h>
#include <linux/lockdep.h>

#include "iris_core.h"
#include "iris_firmware.h"
#include "iris_state.h"
#include "iris_vpu_common.h"

static void iris_core_release(struct iris_core *core)
{
	/* CPU storage only. Device-managed hardware is not retained by these refs. */
	struct device *dev = core->dev;

	kfree(core->response_packet);
	mutex_destroy(&core->detach_lock);
	mutex_destroy(&core->remove_lock);
	mutex_destroy(&core->quiesce_lock);
	mutex_destroy(&core->lifecycle_lock);
	mutex_destroy(&core->lock);
	kfree(core);
	put_device(dev);
}

void iris_core_get(struct iris_core *core)
{
	refcount_inc(&core->owner_refs);
}

void iris_core_put(struct iris_core *core)
{
	if (refcount_dec_and_test(&core->owner_refs))
		iris_core_release(core);
}

void iris_core_pm_guard_get(struct iris_core *core)
{
	lockdep_assert_held(&core->lock);
	/* Terminal-only checked prototype: do not invoke unsupported autosuspend. */
	if (core->irq_managed && !core->pm_guard_held) {
		pm_runtime_get_noresume(core->dev);
		core->pm_guard_held = true;
	}
}

void iris_core_pm_guard_put(struct iris_core *core)
{
	lockdep_assert_held(&core->lock);
	if (core->pm_guard_held) {
		pm_runtime_put_noidle(core->dev);
		core->pm_guard_held = false;
	}
}

void iris_core_pm_put(struct iris_core *core)
{
	/* This retires an already-successful transient get, never a failed get. */
	if (core->irq_managed)
		pm_runtime_put_noidle(core->dev);
	else
		pm_runtime_put_sync(core->dev);
}

bool iris_decode_once_ready_locked(struct iris_core *core)
{
	lockdep_assert_held(&core->lock);
	return core->decode_once && core->decode_once_enabled &&
	       core->decode_once_retention_ready && core->init_only &&
	       core->init_only_attempted && core->init_only_post_bind &&
	       core->init_only_run_claimed && core->init_only_run_done &&
	       !core->init_only_run_error && !core->init_only_error &&
	       core->init_only_stage == IRIS_INIT_ONLY_RESPONSE &&
	       !core->init_only_status[IRIS_INIT_ONLY_RESPONSE] &&
	       core->init_acknowledged && core->state == IRIS_CORE_INIT &&
	       core->decoder_registered && core->v4l2_registered && core->vdev_dec &&
	       !core->encoder_registered && !core->vdev_enc &&
	       core->video_owners == 1 && core->hardware_owner_held &&
	       core->init_only_module_held && core->init_only_remove_blocked &&
	       !core->init_only_remove_seen && !core->init_only_terminal_done &&
	       !core->terminal_shutdown &&
	       core->runtime_pm_enabled && pm_runtime_enabled(core->dev) &&
	       core->dev->power.runtime_status == RPM_ACTIVE &&
	       atomic_read(&core->dev->power.usage_count) >= 1 &&
	       !core->irq_quiescing && !core->resources_releasing &&
	       !core->irq_releasing && core->irq_registered && core->irq_started &&
	       core->irq_accepting && core->queues_allocated && core->power_prepared &&
	       core->firmware_booted && core->pm_guard_held;
}

int iris_decode_once_admit_locked(struct iris_core *core)
{
	lockdep_assert_held(&core->lock);
	if (!iris_decode_once_ready_locked(core) || core->decode_once_claimed ||
	    core->open_inflight || core->open_owners || core->orphan_owners ||
	    !list_empty(&core->instances) || !list_empty(&core->orphan_instances))
		return -ESHUTDOWN;
	/* ACK admits only an attempted open; first claim sets core_ready. */
	core->decode_once_admission = true;
	core->removing = false;
	return 0;
}

int iris_core_open_get(struct iris_core *core)
{
	int ret = 0;

	mutex_lock(&core->lock);
	if (core->decode_once) {
		if (!core->decode_once_admission || core->removing ||
		    !iris_decode_once_ready_locked(core)) {
			ret = -ESHUTDOWN;
			goto unlock;
		}
		if (core->decode_once_claimed) {
			ret = -EALREADY;
			goto unlock;
		}
		if (core->open_inflight || core->open_owners || core->orphan_owners ||
		    !list_empty(&core->instances) || !list_empty(&core->orphan_instances)) {
			ret = -EBUSY;
			goto unlock;
		}
		/* Never clear this claim, including allocation/open failure and close. */
		core->decode_once_claimed = true;
		core->core_ready = true;
	} else if (core->init_only || core->removing || core->terminal_shutdown || core->irq_quiescing) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	iris_core_get(core);
	core->open_inflight++;
unlock:
	mutex_unlock(&core->lock);
	return ret;
}

void iris_core_open_finish(struct iris_core *core, bool success)
{
	mutex_lock(&core->lock);
	core->open_inflight--;
	if (success)
		core->open_owners++;
	mutex_unlock(&core->lock);
	if (!success)
		iris_core_put(core);
}

void iris_core_open_put(struct iris_core *core)
{
	mutex_lock(&core->lock);
	core->open_owners--;
	mutex_unlock(&core->lock);
	iris_core_put(core);
}

void iris_core_orphan_transfer(struct iris_core *core)
{
	/* The file owner becomes an orphan owner without dropping the core ref. */
	mutex_lock(&core->lock);
	core->open_owners--;
	core->orphan_owners++;
	mutex_unlock(&core->lock);
}

void iris_core_orphan_put(struct iris_core *core)
{
	mutex_lock(&core->lock);
	core->orphan_owners--;
	mutex_unlock(&core->lock);
	iris_core_put(core);
}

int iris_core_prepare_remove(struct iris_core *core)
{
	int ret;

	if (current_work() == &core->sys_error_handler.work)
		return -EDEADLK;
	if (in_interrupt())
		return -EWOULDBLOCK;
	mutex_lock(&core->remove_lock);
	mutex_lock(&core->lock);
	/* Revocation remains set on failure. A slow open cannot publish afterward. */
	core->removing = true;
	if (core->open_inflight || core->open_owners || core->orphan_owners ||
	    !list_empty(&core->instances) || !list_empty(&core->orphan_instances)) {
		mutex_unlock(&core->lock);
		ret = -EBUSY;
		goto unlock;
	}
	mutex_unlock(&core->lock);
	ret = iris_core_quiesce(core);
	if (!ret) {
		mutex_lock(&core->lock);
		core->remove_prepared = true;
		mutex_unlock(&core->lock);
	}
unlock:
	mutex_unlock(&core->remove_lock);
	return ret;
}

int iris_core_quiesce(struct iris_core *core)
{
	int ret;

	/* Init-only retains every hardware/DMA owner until external reset. */
	if (READ_ONCE(core->init_only))
		return -EOPNOTSUPP;

	/* Never synchronously cancel this work from its own callback. */
	if (current_work() == &core->sys_error_handler.work)
		return -EDEADLK;
	if (in_interrupt())
		return -EWOULDBLOCK;
	mutex_lock(&core->quiesce_lock);
	mutex_lock(&core->lock);
	if (core->resources_releasing || !core->hardware_owner_held) {
		mutex_unlock(&core->lock);
		ret = -ESHUTDOWN;
		goto unlock;
	}
	if (core->open_inflight || core->open_owners || !list_empty(&core->instances)) {
		mutex_unlock(&core->lock);
		ret = -EBUSY;
		goto unlock;
	}
	if (!iris_vpu_has_checked_power(core)) {
		mutex_unlock(&core->lock);
		ret = -EOPNOTSUPP;
		goto unlock;
	}
	if (core->terminal_shutdown && core->state == IRIS_CORE_DEINIT &&
	    core->firmware_shutdown_verified && core->irq_drain_verified &&
	    core->recovery_drain_verified && !core->firmware_may_run &&
	    !core->queues_allocated && !core->power_prepared && !core->power_irq_enabled &&
	    !core->power_state_unknown && !iris_power_progress_pending(core) &&
	    !core->shutdown_pm_held) {
		mutex_unlock(&core->lock);
		ret = 0;
		goto unlock;
	}
	mutex_unlock(&core->lock);
	/* A failed shutdown retains this PM reference; retries must not get twice. */
	if (!core->shutdown_pm_held) {
		ret = pm_runtime_resume_and_get(core->dev);
		if (ret < 0)
			goto unlock;
		core->shutdown_pm_held = true;
	}
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	/* Recheck after PM resume, before irreversibly shutting down this core. */
	if (core->open_inflight || core->open_owners || !list_empty(&core->instances)) {
		mutex_unlock(&core->lock);
		mutex_unlock(&core->lifecycle_lock);
		pm_runtime_put_noidle(core->dev);
		core->shutdown_pm_held = false;
		ret = -EBUSY;
		goto unlock;
	}
	core->core_ready = false;
	core->terminal_shutdown = true;
	core->irq_quiescing = true;
	core->recovery_blocked = true;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	core->firmware_shutdown_verified = false;
	core->core_ready = false;
	core->state = IRIS_CORE_ERROR;
	iris_hfi_pending_cancel(&core->init_pending);
	complete(&core->core_init_done);
	/* Serialize this extra disable reference with the IRQ-thread enable tail. */
	iris_irq_terminal_mask(core);
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	/* IRQ and recovery callbacks may require core/instance/lifecycle locks. */
	synchronize_irq(core->irq);
	/* A false return means there was no pending work; it is not a failure. */
	cancel_delayed_work_sync(&core->sys_error_handler);
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	core->irq_drain_verified = true;
	core->recovery_drain_verified = true;
	iris_irq_retire_top(core);
	ret = core->firmware_may_run ? iris_fw_unload(core) : 0;
	if (ret)
		goto release_lifecycle;
	core->firmware_shutdown_verified = true;
	if (core->power_prepared || core->power_irq_enabled ||
	    core->power_state_unknown || iris_power_progress_pending(core)) {
		ret = iris_vpu_power_off_checked(core);
		core->power_cleanup_error = ret;
		if (ret)
			goto release_lifecycle;
		core->power_prepared = false;
	}
	if (core->power_state_unknown || core->power_irq_enabled ||
	    iris_power_progress_pending(core)) {
		ret = -EUCLEAN;
		goto release_lifecycle;
	}
	if (core->queues_allocated) {
		iris_hfi_queues_deinit(core);
		core->queues_allocated = false;
	}
	core->firmware_booted = false;
	core->state = IRIS_CORE_DEINIT;
release_lifecycle:
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	if (!ret) {
		/* Terminal core: do not run an autosuspend callback on freed queues. */
		pm_runtime_put_noidle(core->dev);
		core->shutdown_pm_held = false;
	}
unlock:
	mutex_unlock(&core->quiesce_lock);
	return ret;
}

int iris_core_deinit_checked(struct iris_core *core)
{
	int ret;

	if (READ_ONCE(core->init_only))
		return -EOPNOTSUPP;

	/* Never resume or autosuspend a terminal core via this legacy entry. */
	if (READ_ONCE(core->terminal_shutdown))
		return -ESHUTDOWN;
	if (READ_ONCE(core->irq_managed))
		return -EOPNOTSUPP; /* Only explicit terminal drain may shut down managed IRQ. */
	ret = pm_runtime_resume_and_get(core->dev);

	if (ret < 0)
		return ret;
	ret = 0;
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	if (core->terminal_shutdown) {
		ret = -ESHUTDOWN;
		goto unlock;
	}
	if (core->state != IRIS_CORE_DEINIT) {
		/* Secure shutdown failure leaves DMA ownership unproven: retain memory. */
		core->firmware_shutdown_verified = false;
		core->irq_drain_verified = false;
		core->recovery_drain_verified = false;
		ret = iris_fw_unload(core);
		if (ret) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
			goto unlock;
		}
		core->firmware_shutdown_verified = true;
		/* No verified IRQ/work-drain producer exists in this private candidate. */
		if (!core->irq_drain_verified || !core->recovery_drain_verified) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
			ret = -EAGAIN;
			goto unlock;
		}
		if (iris_vpu_has_checked_power(core)) {
			ret = iris_vpu_power_off_checked(core);
			if (ret) {
				core->core_ready = false;
				core->state = IRIS_CORE_ERROR;
				goto unlock;
			}
		} else {
			iris_vpu_power_off(core);
		}
		core->power_prepared = false;
		iris_hfi_queues_deinit(core);
		core->queues_allocated = false;
		core->state = IRIS_CORE_DEINIT;
	}
unlock:
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	pm_runtime_put_sync(core->dev);
	return ret;
}

void iris_core_deinit(struct iris_core *core)
{
	int ret = iris_core_deinit_checked(core);

	if (ret)
		dev_err(core->dev, "core shutdown unverified: %d\n", ret);
}

static int iris_wait_for_system_response(struct iris_core *core)
{
	u32 timeout = core->iris_platform_data->hw_response_timeout;
	unsigned long done;
	int ret;

	mutex_lock(&core->lock);
	if (core->state == IRIS_CORE_ERROR) {
		iris_hfi_pending_cancel(&core->init_pending);
		mutex_unlock(&core->lock);
		return -EIO;
	}
	mutex_unlock(&core->lock);
	done = wait_for_completion_timeout(&core->core_init_done, msecs_to_jiffies(timeout));
	/* Serialize cancellation with matching and completing the old INIT response. */
	mutex_lock(&core->lock);
	iris_hfi_pending_cancel(&core->init_pending);
	if (!done) {
		core->core_ready = false;
		core->state = IRIS_CORE_ERROR;
		ret = -ETIMEDOUT;
	} else {
		ret = core->state == IRIS_CORE_ERROR ? -EIO : 0;
	}
	mutex_unlock(&core->lock);
	return ret;
}

/* Private RAM experiment: no PM puts, secure shutdown, power-off or queue free.
 * Probe owns the active PM reference, binding and module self-reference for the
 * lifetime of the RAM boot.  This is a one-shot, not a reusable runtime core.
 */
static int iris_core_init_only(struct iris_core *core)
{
	int ret, i;

	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	if (core->init_only_attempted) {
		ret = -EALREADY;
		goto unlock;
	}
	core->init_only_attempted = true;
	for (i = 0; i < IRIS_INIT_ONLY_STAGE_COUNT; i++)
		core->init_only_status[i] = -ECANCELED;

	core->init_only_stage = IRIS_INIT_ONLY_ADMISSION;
	/* Missing code/remove lifetime protection is a recorded admission refusal. */
	ret = -EOPNOTSUPP;
	if (!core->init_only_module_held || !core->init_only_remove_blocked ||
	    !core->init_only_prepared || !core->init_only_post_bind ||
	    !core->init_only_run_claimed || !core->init_only_provider_attempted ||
	    core->init_only_remove_seen)
		goto admission;
	/* Removing stays set: even an accidental open must not create a session. */
	ret = -ESHUTDOWN;
	if (!core->init_only || !core->removing || core->terminal_shutdown ||
	    core->irq_quiescing || core->resources_releasing || core->irq_releasing ||
	    !core->hardware_owner_held || !core->irq_registered ||
	    !core->irq_managed || !core->runtime_pm_enabled || !core->pm_guard_held ||
	    !pm_runtime_active(core->dev))
		goto admission;
	ret = -EBUSY;
	if (core->open_inflight || core->open_owners || core->orphan_owners ||
	    core->encoder_registered || core->vdev_enc ||
	    (core->decode_once ?
	     (!core->decode_once_enabled || !core->decode_once_retention_ready ||
	      !core->decoder_registered || !core->v4l2_registered || !core->vdev_dec ||
	      core->video_owners != 1 || core->decode_once_admission || core->decode_once_claimed) :
	     (core->video_owners || core->decoder_registered || core->v4l2_registered || core->vdev_dec)) ||
	    !list_empty(&core->instances) || !list_empty(&core->orphan_instances) ||
	    core->state != IRIS_CORE_DEINIT || core->queues_allocated ||
	    core->power_prepared || core->firmware_touched || core->firmware_may_run ||
	    core->power_state_unknown || core->power_irq_enabled || core->irq_started ||
	    iris_power_progress_pending(core))
		goto admission;
	ret = !iris_vpu_has_checked_power(core) || !core->gen2_ack_supported ?
		-EOPNOTSUPP : 0;
admission:
	core->init_only_status[IRIS_INIT_ONLY_ADMISSION] = ret;
	if (ret)
		goto result;

	core->core_ready = false;
	core->init_acknowledged = false;
	core->firmware_booted = false;
	core->firmware_shutdown_verified = false;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	iris_hfi_pending_cancel(&core->init_pending);
	reinit_completion(&core->core_init_done);
	core->state = IRIS_CORE_INIT;

	core->init_only_stage = IRIS_INIT_ONLY_QUEUES;
	ret = iris_hfi_queues_init(core);
	core->init_only_status[IRIS_INIT_ONLY_QUEUES] = ret;
	if (ret)
		goto failed;
	core->queues_allocated = true;
	core->init_only_stage = IRIS_INIT_ONLY_POWER;
	ret = iris_vpu_power_on_checked(core);
	if (!ret)
		ret = iris_init_only_pm_active(core);
	core->init_only_status[IRIS_INIT_ONLY_POWER] = ret;
	if (ret)
		goto failed;
	core->power_prepared = true;
	core->init_only_stage = IRIS_INIT_ONLY_FIRMWARE;
	ret = iris_fw_load(core);
	core->init_only_status[IRIS_INIT_ONLY_FIRMWARE] = ret;
	if (ret)
		goto failed;
	core->init_only_stage = IRIS_INIT_ONLY_BOOT;
	ret = iris_vpu_boot_firmware(core);
	core->init_only_status[IRIS_INIT_ONLY_BOOT] = ret;
	if (ret)
		goto failed;
	core->firmware_booted = true;
	core->init_only_stage = IRIS_INIT_ONLY_IRQ;
	ret = iris_irq_activate(core);
	core->init_only_status[IRIS_INIT_ONLY_IRQ] = ret;
	if (ret)
		goto failed;
	core->init_only_stage = IRIS_INIT_ONLY_SYS_INIT;
	ret = iris_hfi_core_init(core);
	core->init_only_status[IRIS_INIT_ONLY_SYS_INIT] = ret;
	if (ret)
		goto failed;

	core->init_only_stage = IRIS_INIT_ONLY_RESPONSE;
	mutex_unlock(&core->lock);
	ret = iris_wait_for_system_response(core);
	mutex_lock(&core->lock);
	if (!ret && (!core->init_acknowledged || core->state != IRIS_CORE_INIT ||
		     core->terminal_shutdown || !core->removing))
		ret = -EPROTO;
	core->init_only_status[IRIS_INIT_ONLY_RESPONSE] = ret;
	if (ret)
		goto failed;
	/* ACK is recorded, but this core is never admitted to consumer commands. */
	core->core_ready = false;
	goto result;
failed:
	core->core_ready = false;
	core->state = IRIS_CORE_ERROR;
	iris_hfi_pending_cancel(&core->init_pending);
result:
	core->init_only_error = ret;
unlock:
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	return ret;
}

int iris_core_init_only_terminal(struct iris_core *core)
{
	int ret = 0;

	if (!READ_ONCE(core->init_only))
		return -EOPNOTSUPP;
	if (current_work() == &core->sys_error_handler.work)
		return -EDEADLK;
	if (in_interrupt())
		return -EWOULDBLOCK;
	mutex_lock(&core->quiesce_lock);
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	if (core->init_only_terminal_done)
		goto unlock;
	core->decode_once_admission = false;
	core->init_only_stage = IRIS_INIT_ONLY_TERMINAL;
	if (!core->init_only_attempted || !core->hardware_owner_held ||
	    !core->irq_registered || !core->irq_managed || core->irq_releasing ||
	    core->resources_releasing) {
		ret = -ESHUTDOWN;
		goto record;
	}
	core->removing = true;
	core->core_ready = false;
	core->terminal_shutdown = true;
	core->irq_quiescing = true;
	core->recovery_blocked = true;
	core->state = IRIS_CORE_ERROR;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	/* CPU drain never establishes a secure/DMA shutdown proof. */
	core->firmware_shutdown_verified = false;
	iris_hfi_pending_cancel(&core->init_pending);
	complete(&core->core_init_done);
	iris_irq_terminal_mask(core);
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	synchronize_irq(core->irq);
	cancel_delayed_work_sync(&core->sys_error_handler);

	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	core->irq_drain_verified = true;
	core->recovery_drain_verified = true;
	core->init_only_terminal_done = true;
	/* Keep TOP/terminal mask references, IRQ, DMA, firmware, power and PM. */
record:
	core->init_only_status[IRIS_INIT_ONLY_TERMINAL] = ret;
unlock:
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	mutex_unlock(&core->quiesce_lock);
	return ret;
}

int iris_core_init(struct iris_core *core)
{
	int ret, shutdown_ret;

	if (READ_ONCE(core->decode_once)) {
		/* Open reuses the one acknowledged INIT; never resume/reload/retry. */
		mutex_lock(&core->lock);
		if (core->init_only_attempted) {
			ret = iris_decode_once_ready_locked(core) &&
			      core->decode_once_admission && core->decode_once_claimed &&
			      core->core_ready && !core->removing ? 0 : -EALREADY;
			mutex_unlock(&core->lock);
			return ret;
		}
		mutex_unlock(&core->lock);
		return iris_core_init_only(core);
	}
	if (READ_ONCE(core->init_only))
		return iris_core_init_only(core);

	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	if (core->removing || core->terminal_shutdown) {
		ret = -ESHUTDOWN;
		goto exit;
	}
	if (!list_empty(&core->orphan_instances)) {
		ret = -EBUSY;
		goto exit;
	}
	if (core->irq_managed && !core->gen2_ack_supported) {
		ret = -EOPNOTSUPP;
		goto exit;
	}
	if (core->state == IRIS_CORE_INIT) {
		ret = core->core_ready ? 0 : -EAGAIN;
		goto exit;
	} else if (core->state == IRIS_CORE_ERROR) {
		ret = -EIO;
		goto exit;
	}

	core->core_ready = false;
	core->init_acknowledged = false;
	core->firmware_booted = false;
	core->firmware_shutdown_verified = false;
	core->irq_drain_verified = false;
	core->recovery_drain_verified = false;
	iris_hfi_pending_cancel(&core->init_pending);
	reinit_completion(&core->core_init_done);
	core->state = IRIS_CORE_INIT;

	ret = iris_hfi_queues_init(core);
	if (ret)
		goto error;

	core->queues_allocated = true;
	if (iris_vpu_has_checked_power(core))
		ret = iris_vpu_power_on_checked(core);
	else
		ret = iris_vpu_power_on(core);
	if (ret) {
		/*
		 * Any unknown state or unretired progress means the hardware may
		 * still be clocked; keeping the queues is the only safe option.
		 */
		if (core->power_state_unknown || core->power_irq_enabled || iris_power_progress_pending(core)) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
			goto exit;
		}
		goto error_queue_deinit;
	}

	core->power_prepared = true;
	ret = iris_fw_load(core);
	if (ret) {
		/* A secure-loader attempt is not proof that firmware never ran. */
		if (core->firmware_touched) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
			goto exit;
		}
		goto error_power_off;
	}

	ret = iris_vpu_boot_firmware(core);
	if (ret)
		goto error_unload_fw;

	core->firmware_booted = true;
	if (core->irq_managed) {
		ret = iris_irq_activate(core);
		if (ret)
			goto error_unload_fw;
	}
	ret = iris_hfi_core_init(core);
	if (ret)
		goto error_unload_fw;

	mutex_unlock(&core->lock);

	ret = iris_wait_for_system_response(core);
	if (!ret) {
		mutex_lock(&core->lock);
		if (!core->init_acknowledged || core->state != IRIS_CORE_INIT ||
		    core->terminal_shutdown || core->removing) {
			ret = -EPROTO;
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
		} else {
			core->core_ready = true;
		}
		mutex_unlock(&core->lock);
		if (!ret) {
			mutex_unlock(&core->lifecycle_lock);
			return 0;
		}
	}
	mutex_lock(&core->lock);
	if (core->state == IRIS_CORE_DEINIT)
		goto exit;
	goto error_unload_fw;

error_unload_fw:
	if (core->irq_managed) {
		/* Retain ownership; only the explicit terminal path drains before shutdown. */
		core->core_ready = false;
		core->state = IRIS_CORE_ERROR;
		goto exit;
	}
	shutdown_ret = iris_fw_unload(core);
	core->firmware_shutdown_verified = !shutdown_ret;
	if (shutdown_ret || !core->irq_drain_verified || !core->recovery_drain_verified) {
		core->core_ready = false;
		core->state = IRIS_CORE_ERROR;
		if (!ret)
			ret = shutdown_ret ? shutdown_ret : -EAGAIN;
		goto exit;
	}
error_power_off:
	/* Only never-started, never-touched checked hardware permits inline rollback. */
	if (!core->firmware_touched && !core->firmware_may_run &&
	    !core->power_irq_enabled && iris_vpu_has_checked_power(core)) {
		shutdown_ret = iris_vpu_power_off_checked(core);
		core->power_cleanup_error = shutdown_ret;
		if (shutdown_ret || core->power_state_unknown ||
		    iris_power_progress_pending(core)) {
			core->core_ready = false;
			core->state = IRIS_CORE_ERROR;
			goto exit;
		}
	} else if (core->power_prepared || core->power_irq_enabled ||
		   core->power_state_unknown || iris_power_progress_pending(core)) {
		/* Legacy/untracked power is not proof of a clean, safe queue-free state. */
		core->core_ready = false;
		core->state = IRIS_CORE_ERROR;
		goto exit;
	}
	core->power_prepared = false;
error_queue_deinit:
	iris_hfi_queues_deinit(core);
	core->queues_allocated = false;
error:
	core->state = IRIS_CORE_DEINIT;
exit:
	mutex_unlock(&core->lock);

	mutex_unlock(&core->lifecycle_lock);
	return ret;
}
