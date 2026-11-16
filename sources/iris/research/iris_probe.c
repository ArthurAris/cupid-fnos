// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/clk.h>
#include <linux/interconnect.h>
#include <linux/module.h>
#include <linux/io.h>
#include <linux/ioport.h>
#include <linux/interrupt.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/pm_domain.h>
#include <linux/pm_opp.h>
#include <linux/pm_runtime.h>
#include <linux/reset.h>
#include <linux/property.h>
#include <linux/sysfs.h>
#include <linux/kdev_t.h>

static void iris_sys_error_handler(struct work_struct *work);
static struct platform_driver qcom_iris_driver;
struct iris_core;


#include "iris_core.h"
#include "iris_ctrls.h"
#include "iris_vidc.h"
#include "iris_vpu_common.h"

static int iris_register_video_device(struct iris_core *core, enum domain_type type);

static int iris_init_icc(struct iris_core *core)
{
	const struct icc_info *icc_tbl;
	u32 i = 0;
	int ret;

	icc_tbl = core->iris_platform_data->icc_tbl;

	core->icc_count = core->iris_platform_data->icc_tbl_size;
	core->icc_tbl = kzalloc(
				     sizeof(struct icc_bulk_data) * core->icc_count,
				     GFP_KERNEL);
	if (!core->icc_tbl)
		return -ENOMEM;

	for (i = 0; i < core->icc_count; i++) {
		core->icc_tbl[i].name = icc_tbl[i].name;
		core->icc_tbl[i].avg_bw = icc_tbl[i].bw_min_kbps;
		core->icc_tbl[i].peak_bw = 0;
	}

	ret = of_icc_bulk_get(core->dev, core->icc_count, core->icc_tbl);
	if (!ret)
		core->icc_acquired = true;
	return ret;
}

/* Allocate and publish every owner before the first provider-facing call. */
static int iris_init_only_alloc_pd_list(struct dev_pm_domain_list **owner)
{
	struct dev_pm_domain_list *list;

	list = kzalloc(sizeof(*list), GFP_KERNEL);
	*owner = list;
	if (!list)
		return -ENOMEM;
	list->pd_devs = kcalloc(2, sizeof(*list->pd_devs), GFP_KERNEL);
	if (!list->pd_devs)
		return -ENOMEM;
	list->pd_links = kcalloc(2, sizeof(*list->pd_links), GFP_KERNEL);
	if (!list->pd_links)
		return -ENOMEM;
	list->opp_tokens = kcalloc(2, sizeof(*list->opp_tokens), GFP_KERNEL);
	if (!list->opp_tokens)
		return -ENOMEM;
	return 0;
}

static int iris_init_only_power_domains(struct iris_core *core)
{
	static const char * const names[] = { "venus", "vcodec0", "mxc", "mmcx" };
	const struct iris_platform_data *pdata = core->iris_platform_data;
	struct iris_init_only_pm_progress *progress = &core->init_only_pm;
	const struct platform_clk_data *clk_tbl = pdata->clk_tbl;
	struct iris_pd_attach_progress *attempt;
	struct dev_pm_domain_list *list;
	struct dev_pm_opp_config config = {};
	struct device *pd;
	u32 i, index, clk_cnt = pdata->clk_tbl_size;
	int ret;

	if (progress->attempted)
		return progress->error ? progress->error : -EALREADY;
	progress->attempted = true;
	if (core->power_state_unknown) {
		ret = core->power_error ? core->power_error : -ESHUTDOWN;
		goto failed;
	}
	/* Exact SM8450 platform arrays: local required-OPP indices are 0/1. */
	if (pdata->pmdomain_tbl_size != 2 || pdata->opp_pd_tbl_size != 2 ||
	    !pdata->pmdomain_tbl || !pdata->opp_pd_tbl || (clk_cnt && !clk_tbl)) {
		ret = -EINVAL;
		goto failed;
	}
	for (i = 0; i < ARRAY_SIZE(names); i++) {
		const char *name = i < 2 ? pdata->pmdomain_tbl[i] : pdata->opp_pd_tbl[i - 2];

		if (!name || strcmp(name, names[i])) {
			ret = -EINVAL;
			goto failed;
		}
		progress->domains[i].name = names[i];
	}
	if (core->pmdomain_tbl || core->opp_pmdomain_tbl || core->opp_tokens ||
	    core->domains_attached || core->opp_domains_attached) {
		ret = -EALREADY;
		goto failed;
	}
	ret = iris_init_only_alloc_pd_list(&core->pmdomain_tbl);
	if (ret)
		goto failed;
	ret = iris_init_only_alloc_pd_list(&core->opp_pmdomain_tbl);
	if (ret)
		goto failed;
	core->opp_tokens = kcalloc(clk_cnt, sizeof(*core->opp_tokens), GFP_KERNEL);
	if (clk_cnt && !core->opp_tokens) {
		ret = -ENOMEM;
		goto failed;
	}
	progress->clk_token_held = kcalloc(clk_cnt, sizeof(*progress->clk_token_held), GFP_KERNEL);
	if (clk_cnt && !progress->clk_token_held) {
		ret = -ENOMEM;
		goto failed;
	}

	for (i = 0; i < ARRAY_SIZE(names); i++) {
		index = i < 2 ? i : i - 2;
		list = i < 2 ? core->pmdomain_tbl : core->opp_pmdomain_tbl;
		attempt = &progress->domains[i];
		attempt->attempted = true;
		/* By-name has provider callbacks/internal unwind and queued work. */
		WRITE_ONCE(core->init_only_provider_attempted, true);
		pd = dev_pm_domain_attach_by_name(core->dev, attempt->name);
		if (IS_ERR(pd)) {
			ret = PTR_ERR(pd);
			goto attach_failed;
		}
		if (!pd) {
			ret = -ENODEV;
			goto attach_failed;
		}
		list->pd_devs[index] = pd;
		attempt->acquired = true;
		/* num_pds is the retained prefix, never a declaration of completeness. */
		list->num_pds++;
		if (i < 2) {
			progress->ctrl_count++;
			if (progress->ctrl_count == 2)
				core->domains_attached = true;
			continue;
		}
		progress->opp_count++;
		config.required_dev = pd;
		config.required_dev_index = index;
		ret = dev_pm_opp_set_config(core->dev, &config);
		if (ret < 0)
			goto attach_failed;
		list->opp_tokens[index] = ret;
		attempt->token_held = true; /* Zero is a valid owned token. */
		attempt->link_attempted = true;
		list->pd_links[index] = device_link_add(core->dev, pd,
						      DL_FLAG_STATELESS | DL_FLAG_PM_RUNTIME);
		if (!list->pd_links[index]) {
			ret = -ENODEV;
			goto attach_failed;
		}
		attempt->link_held = true;
	}
	core->opp_domains_attached = true;
	for (i = 0; i < clk_cnt; i++) {
		if (clk_tbl[i].clk_type != IRIS_HW_CLK)
			continue;
		ret = dev_pm_opp_set_clkname(core->dev, clk_tbl[i].clk_name);
		if (ret < 0)
			goto failed;
		core->opp_tokens[core->opp_token_count] = ret;
		progress->clk_token_held[core->opp_token_count] = true;
		core->opp_token_count++;
	}
	ret = dev_pm_opp_of_add_table(core->dev);
	if (ret)
		goto failed;
	core->opp_table_added = true;
	progress->complete = true;
	return 0;

attach_failed:
	attempt->error = ret;
failed:
	progress->error = ret;
	core->power_state_unknown = true;
	if (!core->power_error)
		core->power_error = ret;
	/* STOP_RETAIN: no detach, link deletion, OPP clear, free or PM put. */
	return ret;
}

static int iris_init_power_domains(struct iris_core *core)
{
	const struct platform_clk_data *clk_tbl;
	u32 clk_cnt, i;
	int ret;

	struct dev_pm_domain_attach_data iris_pd_data = {
		.pd_names = core->iris_platform_data->pmdomain_tbl,
		.num_pd_names = core->iris_platform_data->pmdomain_tbl_size,
		.pd_flags = PD_FLAG_NO_DEV_LINK,
	};

	struct dev_pm_domain_attach_data iris_opp_pd_data = {
		.pd_names = core->iris_platform_data->opp_pd_tbl,
		.num_pd_names = core->iris_platform_data->opp_pd_tbl_size,
		.pd_flags = PD_FLAG_DEV_LINK_ON | PD_FLAG_REQUIRED_OPP,
	};

	ret = dev_pm_domain_attach_list(core->dev, &iris_pd_data, &core->pmdomain_tbl);
	if (ret < 0)
		return ret;

	core->domains_attached = true;
	ret = dev_pm_domain_attach_list(core->dev, &iris_opp_pd_data, &core->opp_pmdomain_tbl);
	if (ret < 0)
		return ret;

	core->opp_domains_attached = true;
	clk_tbl = core->iris_platform_data->clk_tbl;
	clk_cnt = core->iris_platform_data->clk_tbl_size;

	core->opp_tokens = kcalloc(clk_cnt, sizeof(*core->opp_tokens), GFP_KERNEL);
	if (clk_cnt && !core->opp_tokens)
		return -ENOMEM;
	for (i = 0; i < clk_cnt; i++) {
		if (clk_tbl[i].clk_type == IRIS_HW_CLK) {
			ret = dev_pm_opp_set_clkname(core->dev, clk_tbl[i].clk_name);
			if (ret < 0)
				return ret;
			core->opp_tokens[core->opp_token_count++] = ret;
		}
	}

	ret = dev_pm_opp_of_add_table(core->dev);
	if (!ret)
		core->opp_table_added = true;
	return ret;
}

static int iris_init_clocks(struct iris_core *core)
{
	int ret;

	ret = clk_bulk_get_all(core->dev, &core->clock_tbl);
	if (ret < 0)
		return ret;

	core->clk_count = ret;
	core->clocks_acquired = true;

	return 0;
}

static int iris_init_reset_table(struct iris_core *core,
				 struct reset_control_bulk_data **resets,
				 const char * const *rst_tbl, u32 rst_tbl_size)
{
	u32 i = 0;

	*resets = kzalloc(
			       sizeof(struct reset_control_bulk_data) * rst_tbl_size,
			       GFP_KERNEL);
	if (!*resets)
		return -ENOMEM;

	for (i = 0; i < rst_tbl_size; i++)
		(*resets)[i].id = rst_tbl[i];

	return reset_control_bulk_get_exclusive(core->dev, rst_tbl_size, *resets);
}

static int iris_init_resets(struct iris_core *core)
{
	int ret;

	ret = iris_init_reset_table(core, &core->resets,
				    core->iris_platform_data->clk_rst_tbl,
				    core->iris_platform_data->clk_rst_tbl_size);
	if (ret)
		return ret;

	core->reset_count = core->iris_platform_data->clk_rst_tbl_size;
	if (!core->iris_platform_data->controller_rst_tbl_size)
		return 0;

	ret = iris_init_reset_table(core, &core->controller_resets,
				     core->iris_platform_data->controller_rst_tbl,
				     core->iris_platform_data->controller_rst_tbl_size);
	if (!ret)
		core->controller_reset_count = core->iris_platform_data->controller_rst_tbl_size;
	return ret;
}

static int iris_init_resources(struct iris_core *core)
{
	int ret;

	ret = iris_init_icc(core);
	if (ret)
		return ret;

	if (READ_ONCE(core->init_only))
		ret = iris_init_only_power_domains(core);
	else
		ret = iris_init_power_domains(core);
	if (ret)
		return ret;

	ret = iris_init_clocks(core);
	if (ret)
		return ret;

	return iris_init_resets(core);
}

static void iris_release_resource_handles(struct iris_core *core)
{
	u32 i;

	if (core->controller_reset_count) {
		reset_control_bulk_put(core->controller_reset_count, core->controller_resets);
		core->controller_reset_count = 0;
	}
	kfree(core->controller_resets);
	core->controller_resets = NULL;
	if (core->reset_count) {
		reset_control_bulk_put(core->reset_count, core->resets);
		core->reset_count = 0;
	}
	kfree(core->resets);
	core->resets = NULL;
	if (core->clocks_acquired) {
		clk_bulk_put_all(core->clk_count, core->clock_tbl);
		core->clocks_acquired = false;
		core->clock_tbl = NULL;
		core->clk_count = 0;
	}
	if (core->opp_table_added) {
		dev_pm_opp_of_remove_table(core->dev);
		core->opp_table_added = false;
	}
	for (i = core->opp_token_count; i; i--)
		dev_pm_opp_clear_config(core->opp_tokens[i - 1]);
	core->opp_token_count = 0;
	kfree(core->opp_tokens);
	core->opp_tokens = NULL;
	if (core->opp_domains_attached) {
		dev_pm_domain_detach_list(core->opp_pmdomain_tbl);
		core->opp_domains_attached = false;
		core->opp_pmdomain_tbl = NULL;
	}
	if (core->domains_attached) {
		dev_pm_domain_detach_list(core->pmdomain_tbl);
		core->domains_attached = false;
		core->pmdomain_tbl = NULL;
	}
	if (core->icc_acquired) {
		icc_bulk_put(core->icc_count, core->icc_tbl);
		core->icc_acquired = false;
	}
	kfree(core->icc_tbl);
	core->icc_tbl = NULL;
	core->icc_count = 0;
}

static void iris_release_mmio(struct iris_core *core)
{
	if (core->mmio_mapped) {
		iounmap(core->reg_base);
		core->mmio_mapped = false;
		core->reg_base = NULL;
	}
	if (core->mmio_claimed) {
		release_mem_region(core->mmio_start, core->mmio_size);
		core->mmio_claimed = false;
	}
}

static int iris_acquire_mmio(struct iris_core *core, struct platform_device *pdev)
{
	struct resource *res = platform_get_resource(pdev, IORESOURCE_MEM, 0);

	if (!res || res->end < res->start)
		return -EINVAL;
	core->mmio_start = res->start;
	core->mmio_size = resource_size(res);
	if (!core->mmio_size)
		return -EINVAL;
	if (!request_mem_region(core->mmio_start, core->mmio_size, dev_name(core->dev)))
		return -EBUSY;
	core->mmio_claimed = true;
	core->reg_base = ioremap(core->mmio_start, core->mmio_size);
	if (!core->reg_base) {
		iris_release_mmio(core);
		return -ENOMEM;
	}
	core->mmio_mapped = true;
	return 0;
}

static int iris_request_irq(struct iris_core *core)
{
	unsigned long flags;
	int ret;

	/* Begin disabled; ops/list/packet initialization must precede registration. */
	ret = request_threaded_irq(core->irq, iris_hfi_isr, iris_hfi_isr_handler,
				   IRQF_TRIGGER_HIGH | IRQF_NO_AUTOEN, "iris", core);
	if (ret)
		return ret;
	mutex_lock(&core->lock);
	raw_spin_lock_irqsave(&core->irq_lock, flags);
	core->irq_registered = true;
	core->irq_managed = iris_vpu_has_checked_power(core);
	core->irq_initial_mask_held = core->irq_managed;
	core->irq_probe_unpublished = true;
	raw_spin_unlock_irqrestore(&core->irq_lock, flags);
	mutex_unlock(&core->lock);
	return 0;
}

static int iris_resource_release_gate(struct iris_core *core)
{
	if (READ_ONCE(core->init_only))
		return -EOPNOTSUPP;
	if (!core->removing || !core->remove_prepared || !core->terminal_shutdown ||
	    core->state != IRIS_CORE_DEINIT || !core->firmware_shutdown_verified ||
	    !core->irq_drain_verified || !core->recovery_drain_verified)
		return -EAGAIN;
	if (core->open_inflight || core->open_owners || core->orphan_owners ||
	    !list_empty(&core->instances) || !list_empty(&core->orphan_instances))
		return -EBUSY;
	if (core->firmware_may_run || core->queues_allocated || core->power_prepared ||
	    core->shutdown_pm_held || atomic_read(&core->dev->power.usage_count) != (core->pm_guard_held ? 1 : 0))
		return -EBUSY;
	if (core->power_state_unknown || core->power_irq_enabled || iris_power_progress_pending(core))
		return -EBUSY;
	return 0;
}

int iris_release_resources(struct iris_core *core)
{
	int ret;

	if (current_work() == &core->sys_error_handler.work)
		return -EDEADLK;
	if (in_interrupt())
		return -EWOULDBLOCK;
	/* Keep CPU storage while the independent hardware owner is dropped. */
	iris_core_get(core);
	mutex_lock(&core->remove_lock);
	mutex_lock(&core->quiesce_lock);
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	if (!core->hardware_owner_held) {
		ret = 0;
		goto reject;
	}
	ret = iris_resource_release_gate(core);
	if (ret)
		goto reject;
	core->resources_releasing = true;
	iris_core_pm_guard_put(core);
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	/* These waits must not hold locks required by IRQ/work/runtime callbacks. */
	if (core->runtime_pm_enabled) {
		pm_runtime_barrier(core->dev);
		pm_runtime_disable(core->dev);
		pm_runtime_dont_use_autosuspend(core->dev);
		core->runtime_pm_enabled = false;
	}
	/* A PM user appearing during the barrier must not lose its hardware handles. */
	if (atomic_read(&core->dev->power.usage_count)) {
		mutex_lock(&core->lifecycle_lock);
		mutex_lock(&core->lock);
		ret = -EBUSY;
		goto reject;
	}
	ret = iris_irq_unregister(core);
	if (ret) {
		mutex_lock(&core->lifecycle_lock);
		mutex_lock(&core->lock);
		goto reject;
	}
	cancel_delayed_work_sync(&core->sys_error_handler);
	iris_release_resource_handles(core);
	iris_release_mmio(core);
	mutex_lock(&core->lifecycle_lock);
	mutex_lock(&core->lock);
	core->hardware_owner_held = false;
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	iris_core_put(core);
	/* The immutable one-way terminal state keeps producer hardware work blocked. */
	mutex_unlock(&core->quiesce_lock);
	mutex_unlock(&core->remove_lock);
	iris_core_put(core);
	return 0;
reject:
	mutex_unlock(&core->lock);
	mutex_unlock(&core->lifecycle_lock);
	mutex_unlock(&core->quiesce_lock);
	mutex_unlock(&core->remove_lock);
	iris_core_put(core);
	return ret;
}

static void iris_video_device_release(struct video_device *vdev)
{
	struct iris_core *core = video_get_drvdata(vdev);

	mutex_lock(&core->lock);
	if (core->vdev_dec == vdev)
		core->vdev_dec = NULL;
	if (core->vdev_enc == vdev)
		core->vdev_enc = NULL;
	core->video_owners--;
	mutex_unlock(&core->lock);
	video_device_release(vdev);
	iris_core_put(core);
}
static void iris_binding_release(void *data)
{
	iris_core_put(data);
}

#define IRIS_INIT_ONLY_TOKEN "IRIS_INIT_ONLY_SM8450_RAM_20261114"
#define IRIS_INIT_ONLY_PROPERTY "qcom,iris-init-only-token"
/* Fail closed. Do not enable until inner power/firmware rollback and platform
 * device-unregister/devres/IOMMU lifetime have independent STOP_RETAIN proof.
 * suppress_bind_attrs and a void remove callback cannot veto device removal.
 */
#define IRIS_INIT_ONLY_EXECUTION_ENABLED 0
/* Independently closed: exact PM/provider/framework unwind is not evidenced.
 * The ordinary aggregate attachment is NOT an admitted init-only provider path.
 */
#define IRIS_INIT_ONLY_PROVIDER_ADMISSION_ENABLED 0
/* Separate token, compatible, runner and consumer gate; never enabled here. */
#define IRIS_DECODE_ONCE_ENABLED 0
#define IRIS_DECODE_ONCE_TOKEN "IRIS_DECODE_ONCE_SM8450_RAM_20261115_V1"
#define IRIS_DECODE_ONCE_PROPERTY "qcom,iris-decode-once-token"

static const char * const iris_init_only_stage_names[] = {
	"admission", "queues", "power", "firmware", "boot", "irq",
	"sys-init", "response", "terminal",
};

static ssize_t init_only_status_show(struct device *dev,
                                     struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	unsigned int stage;
	int i, len = 0;

	if (!core)
		return sysfs_emit(buf, "schema=2 unavailable=-19\n");
	/* One bounded numeric snapshot. No firmware strings or device identifiers. */
	mutex_lock(&core->lock);
	stage = (unsigned int)core->init_only_stage;
	if (stage >= IRIS_INIT_ONLY_STAGE_COUNT)
		stage = IRIS_INIT_ONLY_ADMISSION;
	/* Terminal CPU drain must not hide the first failed initialization stage. */
	for (i = 0; i < IRIS_INIT_ONLY_STAGE_COUNT; i++) {
		if (core->init_only_status[i] && core->init_only_status[i] != -ECANCELED) {
			stage = i;
			break;
		}
	}
	len += sysfs_emit_at(buf, len, "schema=2 execution_enabled=%u stage=%u stage_errno=%d error=%d\n",
			     IRIS_INIT_ONLY_EXECUTION_ENABLED, stage,
			     core->init_only_status[stage], core->init_only_error);
	len += sysfs_emit_at(buf, len,
			     "prepared=%u post_bind=%u run_claimed=%u run_done=%u run_errno=%d provider_attempted=%u\n",
			     core->init_only_prepared, core->init_only_post_bind,
			     core->init_only_run_claimed, core->init_only_run_done,
			     core->init_only_run_error, core->init_only_provider_attempted);
	len += sysfs_emit_at(buf, len,
			     "ack=%u terminal_cpu_drain=%u terminal_errno=%d dma_stop_known=0\n",
			     core->init_acknowledged, core->init_only_terminal_done,
			     core->init_only_status[IRIS_INIT_ONLY_TERMINAL]);
	len += sysfs_emit_at(buf, len,
			     "queues=%u power=%u fw_touched=%u fw_may_run=%u fw_booted=%u\n",
			     core->queues_allocated, core->power_prepared,
			     core->firmware_touched, core->firmware_may_run, core->firmware_booted);
	for (i = 0; i < IRIS_INIT_ONLY_STAGE_COUNT; i++)
		len += sysfs_emit_at(buf, len, "status_%s=%d\n",
				     iris_init_only_stage_names[i], core->init_only_status[i]);
	mutex_unlock(&core->lock);
	return len;
}
static DEVICE_ATTR_RO(init_only_status);

/* Decode readiness is a live RESPONSE-stage contract, not init-only terminal
 * success. A zero ready field is expected before the first admitted open.
 * Numeric node identity is the decoder's registered dev_t, not a /dev guess.
 */
static ssize_t decode_once_status_show(struct device *dev,
                                      struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	unsigned int stage, node_major = 0, node_minor = 0;
	bool node_bound;
	int stage_errno, len = 0;

	if (!core)
		return sysfs_emit(buf, "schema=1 unavailable=-19\n");
	mutex_lock(&core->lock);
	if (!core->decode_once) {
		mutex_unlock(&core->lock);
		return sysfs_emit(buf, "schema=1 unavailable=-95\n");
	}
	stage = (unsigned int)core->init_only_stage;
	stage_errno = stage < IRIS_INIT_ONLY_STAGE_COUNT ?
		core->init_only_status[stage] : -ERANGE;
	node_bound = core->decoder_registered && core->v4l2_registered && core->vdev_dec;
	if (node_bound) {
		node_major = MAJOR(core->vdev_dec->dev.devt);
		node_minor = MINOR(core->vdev_dec->dev.devt);
	}
	/* One core-lock snapshot; only scalar reads/macros and bounded formatting. */
	len += sysfs_emit_at(buf, len,
		"schema=1 decode_enabled=%u retention_ready=%u prepared=%u post_bind=%u\n",
		core->decode_once_enabled, core->decode_once_retention_ready,
		core->init_only_prepared, core->init_only_post_bind);
	len += sysfs_emit_at(buf, len,
		"run_claimed=%u run_done=%u run_errno=%d stage=%u stage_errno=%d error=%d\n",
		core->init_only_run_claimed, core->init_only_run_done,
		core->init_only_run_error, stage, stage_errno, core->init_only_error);
	len += sysfs_emit_at(buf, len,
		"ack=%u core_ready=%u admission=%u claimed=%u terminal=%u dma_stop_known=0\n",
		core->init_acknowledged, core->core_ready, core->decode_once_admission,
		core->decode_once_claimed, core->terminal_shutdown);
	len += sysfs_emit_at(buf, len,
		"decoder_published=%u dev_major=%u dev_minor=%u decoder_registered=%u encoder_registered=%u\n",
		node_bound, node_major, node_minor,
		core->decoder_registered, core->encoder_registered);
	mutex_unlock(&core->lock);
	return len;
}
static DEVICE_ATTR_RO(decode_once_status);

/* Non-transactional diagnostic snapshot, under 700 bytes at integer extremes.
 * session_rx includes matched OPEN/START replies, not input-buffer completion.
 * psc_seen counts matching packet types, not accepted source changes or frames.
 * rx_* count validated packets; last_* preserve the last packet's raw u32 fields.
 * in_done is a returned-success VB2 done call, not a decoded frame.
 * Queue indexes are word offsets read from retained CPU mappings, not MMIO.
 * queue_valid only says both allocated headers were present for this read;
 * equal indexes do not establish firmware consumption history or DMA safety.
 * Core storage is retained by the private lifetime contract; do not take locks,
 * follow an instance pointer, read MMIO, or issue any firmware operation here.
 */
static ssize_t decode_once_progress_show(struct device *dev,
					struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	struct iris_hfi_queue_header *cmd, *msg;
	u32 cmd_r = 0, cmd_w = 0, msg_r = 0, msg_w = 0;
	unsigned int queue_valid = 0;

	(void)attr;
	if (!core)
		return sysfs_emit(buf, "schema=1 unavailable=-19\n");
	if (!READ_ONCE(core->decode_once))
		return sysfs_emit(buf, "schema=1 unavailable=-95\n");
	/* A failed or not-yet-complete allocation never publishes usable headers.
	 * After successful private init these CPU mappings are retained until reset.
	 */
	if (READ_ONCE(core->queues_allocated)) {
		cmd = READ_ONCE(core->command_queue.qhdr);
		msg = READ_ONCE(core->message_queue.qhdr);
		if (cmd && msg) {
			cmd_r = READ_ONCE(cmd->read_idx);
			cmd_w = READ_ONCE(cmd->write_idx);
			msg_r = READ_ONCE(msg->read_idx);
			msg_w = READ_ONCE(msg->write_idx);
			queue_valid = 1;
		}
	}
	return sysfs_emit(buf,
		"schema=1 start_calls=%d start_rc=%d defer_before=%d defer_after=%d "
		"input_attempt=%d input_ok=%d input_first_error=%d session_rx=%d "
		"psc_seen=%d session_state=%d core_state=%d "
		"rx_open=%d rx_start=%d rx_sub=%d rx_prop=%d rx_buf=%d rx_other=%d "
		"rx_err=%d in_enter=%d in_done=%d in_err=%d "
		"last_type=%u last_port=%u last_flags=%u "
		"queue_valid=%u cmd_r=%u cmd_w=%u msg_r=%u msg_w=%u\n",
		atomic_read(&core->decode_progress_start_calls),
		atomic_read(&core->decode_progress_start_rc),
		atomic_read(&core->decode_progress_defer_before),
		atomic_read(&core->decode_progress_defer_after),
		atomic_read(&core->decode_progress_input_attempt),
		atomic_read(&core->decode_progress_input_ok),
		atomic_read(&core->decode_progress_input_first_error),
		atomic_read(&core->decode_progress_session_rx),
		atomic_read(&core->decode_progress_psc_seen),
		atomic_read(&core->decode_progress_session_state),
		(int)READ_ONCE(core->state),
		atomic_read(&core->decode_progress_rx_open),
		atomic_read(&core->decode_progress_rx_start),
		atomic_read(&core->decode_progress_rx_sub),
		atomic_read(&core->decode_progress_rx_prop),
		atomic_read(&core->decode_progress_rx_buf),
		atomic_read(&core->decode_progress_rx_other),
		atomic_read(&core->decode_progress_rx_err),
		atomic_read(&core->decode_progress_in_enter),
		atomic_read(&core->decode_progress_in_done),
		atomic_read(&core->decode_progress_in_err),
		(u32)atomic_read(&core->decode_progress_last_type),
		(u32)atomic_read(&core->decode_progress_last_port),
		(u32)atomic_read(&core->decode_progress_last_flags),
		queue_valid, cmd_r, cmd_w, msg_r, msg_w);
}
static DEVICE_ATTR_RO(decode_once_progress);

/* Immutable once-published slot scalars plus non-transactional debug counters.
 * v bit0=CPU hash/shape, bit1=serialized HFI shape. ok bits: nonzero address,
 * DMA-mask range, equality with CPU shape, zero addr_offset/matching index.
 * Hash means CPU bytes before DMA prepare, never firmware visibility or decode.
 */
static ssize_t decode_once_input_show(struct device *dev,
				     struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	u32 i, ca, cs = 0, ci = 0, cf = 0, ct = 0, cv = 0;
	int len;

	(void)attr;
	if (!core)
		return sysfs_emit(buf, "schema=1 unavailable=-19\n");
	if (!READ_ONCE(core->decode_once))
		return sysfs_emit(buf, "schema=1 unavailable=-95\n");
	ca = (u32)atomic_read(&core->input_comv_bits);
	smp_rmb();
	if (ca & BIT(0)) {
		cs = (u32)atomic_read(&core->input_comv_count);
		ci = (u32)atomic_read(&core->input_comv_id);
	}
	if (ca & BIT(2)) {
		cf = (u32)atomic_read(&core->input_comv_flags);
		ct = (u32)atomic_read(&core->input_comv_payload);
		cv = (u32)atomic_read(&core->input_comv_value);
	}
	len = sysfs_emit(buf,
		"schema=1 dn=%u dl=%u dr=%u da=%u du=%u im=%u info=%u prop=%u "
		"cs=%u ci=%u ca=%u cf=%u ct=%u cv=%u\n",
		(u32)atomic_read(&core->input_debug_count),
		(u32)atomic_read(&core->input_debug_level),
		(u32)atomic_read(&core->input_debug_last),
		(u32)atomic_read(&core->input_debug_bits),
		(u32)atomic_read(&core->input_debug_unknown),
		(u32)atomic_read(&core->input_internal_mask),
		(u32)atomic_read(&core->input_last_info),
		(u32)atomic_read(&core->input_last_property),
		cs, ci, ca, cf, ct, cv);
	for (i = 0; i < 3; i++) {
		struct iris_input_proof *p = &core->input_proof[i];
		u32 valid = (u32)atomic_read(&p->valid);
		u32 bytes = 0, offset = 0, alloc = 0, hash = 0;
		u32 kind = 0, port = 0, flags = 0, ok = 0;

		smp_rmb();
		if (valid & BIT(0))
			hash = READ_ONCE(p->hash);
		if (valid & BIT(1)) {
			bytes = READ_ONCE(p->bytes);
			offset = READ_ONCE(p->offset);
			alloc = READ_ONCE(p->alloc);
			kind = READ_ONCE(p->kind);
			port = READ_ONCE(p->port);
			flags = READ_ONCE(p->flags);
			ok = READ_ONCE(p->addr_ok);
		}
		len += sysfs_emit_at(buf, len,
			"i=%u v=%u b=%u o=%u a=%u h=%u k=%u p=%u f=%u ok=%u\n",
			i, valid, bytes, offset, alloc, hash, kind, port, flags, ok);
	}
	return len;
}
static DEVICE_ATTR_RO(decode_once_input);


static ssize_t decode_once_release_show(struct device *dev,
	struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	struct iris_release_observer_request request;
	struct iris_release_observer_reply reply;
	int len;
	u32 i, request_valid, reply_valid;

	(void)attr;
	if (!core || !READ_ONCE(core->decode_once))
		return sysfs_emit(buf, "schema=1 unavailable=-95\n");
	len = sysfs_emit(buf, "schema=1 enabled=%u retained_only=1 slots=4\n",
														    READ_ONCE(core->release_observer_enabled));
	for (i = 0; i < IRIS_RELEASE_OBSERVER_SLOTS; i++) {
		struct iris_release_observer_slot *slot = &core->release_observer[i];

		request = (struct iris_release_observer_request){};
		reply = (struct iris_release_observer_reply){};
		request_valid = smp_load_acquire(&slot->request_valid.counter);
		if (request_valid)
			request = slot->request;
		reply_valid = smp_load_acquire(&slot->reply_valid.counter);
		if (reply_valid)
			reply = slot->reply;
		len += sysfs_emit_at(buf, len,
			"slot=%u qv=%u qs=%u qh=%u qp=%u qt=%u qport=%u qflags=%u qi=%u qal=%u qah=%u qsize=%u qrc=%d "
			"fv=%u fh=%u fp=%u frc=%d rv=%u rs=%u rh=%u rp=%u rt=%u rport=%u rflags=%u rpayload=%u "
			"rbt=%u rbf=%u ri=%u ral=%u rah=%u rsize=%u rao=%u rdo=%u rds=%u\n",
			i, request_valid, request.session_id, request.header_id, request.packet_id,
			request.type, request.port, request.flags, request.index, request.addr_lo,
			request.addr_hi, request.size, request.queue_rc, request.first_valid,
			request.first_header_id, request.first_packet_id, request.first_rc,
			reply_valid, reply.session_id, reply.header_id, reply.packet_id, reply.type,
			reply.port, reply.flags, reply.payload, reply.buffer_type, reply.buffer_flags,
			reply.index, reply.addr_lo, reply.addr_hi, reply.size, reply.addr_offset,
			reply.data_offset, reply.data_size);
	}
	return len;
}
static DEVICE_ATTR_RO(decode_once_release);


static ssize_t stop_failure_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	const struct iris_stop_failure *e;
	const struct iris_stop_failure_packet *p;
	u32 valid, i;
	int len;

	(void)attr;
	if (!core)
		return sysfs_emit(buf, "v=0 unavailable=-19\n");
	e = &core->stop_failure;
	valid = smp_load_acquire(&e->valid.counter);
	if (!valid)
		return sysfs_emit(buf, "v=0\n");
	/* Slot data is immutable. late is a separate live CPU-only counter. Raw words,
	 * not firmware error codes; -EINPROGRESS means buffer validation not reached.
	 */
	len = sysfs_emit(buf,
		"v=1 s=%x n=%x ov=%x late=%x r=%x ret=%x is=%x cs=%x sub=%x h=%x es=%x o=%x id=%x rx=%x hs=%x np=%x\n",
		e->session, e->count, e->overflow, (u32)atomic_read(&e->late_ignored),
		e->reason, (u32)e->actual_ret, e->inst_state, e->core_state, e->sub_state,
		e->header, e->error_session, e->ordinal, e->packet_id,
		e->received, e->header_size, e->num_packets);
	for (i = 0; i < e->count && i < IRIS_STOP_FAILURE_SLOTS; i++) {
		p = &e->packet[i];
		len += sysfs_emit_at(buf, len,
			"%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%08x%08x%08x%08x,%x,%x\n",
			p->header, p->session, p->ordinal, p->id, p->type, p->port,
			p->flags, p->payload, p->size, p->raw_bytes,
			p->raw[0], p->raw[1], p->raw[2], p->raw[3],
			p->buffer_flags, (u32)p->buffer_ret);
	}
	return len;
}
static DEVICE_ATTR_RO(stop_failure_status);

static ssize_t stop_observer_status_show(struct device *dev, struct device_attribute *attr, char *buf)
{
	struct iris_core *core = dev_get_drvdata(dev);
	struct iris_stop_evidence *e;
	struct iris_stop_transaction a = {}, b = {};
	u32 valid;

	(void)attr;
	if (!core)
		return sysfs_emit(buf, "schema=1 unavailable=-19\n");
	e = &core->stop_evidence;
	valid = smp_load_acquire(&e->result_valid.counter);
	if (valid) { a = e->transaction[0]; b = e->transaction[1]; }
		return sysfs_emit(buf,
	    "schema=1 enabled=%u valid=%u result=%d ah=%u ap=%u aq=%d aw=%d aa=%u arh=%u arp=%u arf=%u "
	"bh=%u bp=%u bq=%d bw=%d ba=%u brh=%u brp=%u brf=%u retained=1\n",
	    READ_ONCE(core->stop_observer_enabled), valid, valid ? e->result : 0,
	    a.request_header, a.request_packet, a.queue_rc, a.wait_rc, a.ack, a.ack_header, a.ack_packet, a.ack_flags,
	    b.request_header, b.request_packet, b.queue_rc, b.wait_rc, b.ack, b.ack_header, b.ack_packet, b.ack_flags);
}
static DEVICE_ATTR_RO(stop_observer_status);

static ssize_t iris_stop_snapshot_show(struct device *dev, char *buf, u32 plane, bool post)
{
	struct iris_core *core = dev_get_drvdata(dev);
	const struct iris_stop_snapshot *snapshot;
	struct iris_stop_app record;
	struct iris_stop_evidence *e;
	u32 valid, i;
	int len;

	if (!core)
		return sysfs_emit(buf, "schema=1 unavailable=-19\n");
	e = &core->stop_evidence;
	valid = post ? smp_load_acquire(&e->post_valid.counter) : smp_load_acquire(&e->pre_valid.counter);
	snapshot = post ? &e->post : &e->pre;
	len = sysfs_emit(buf,"v=%u d=%u p=%u s=%08x q=%08x r=%08x u=%08x\n",
	    valid, plane, post, valid ? snapshot->seen[plane]:0, valid ? snapshot->queued[plane]:0, valid ? snapshot->readonly[plane]:0, valid ? snapshot->unknown[plane]:0);
	if (!valid)return len;
	for (i = 0; i<VIDEO_MAX_FRAME; i++) {
		record = snapshot->app[plane][i];
		struct iris_stop_app *a = &record;
		if (!a->valid) continue;
			len += sysfs_emit_at(buf, len,"%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%x,%x\n",
		    i, a->header_id, a->packet_id, a->addr_hi, a->addr_lo, a->size, a->submissions, a->returned, a->readonly, (u32)a->queue_rc,
		    a->rx_header, a->rx_packet, a->rx_flags, a->rx_buffer_flags);
	}
	return len;
}
#define IRIS_STOP_ATTR(name, plane, post) \
static ssize_t name##_show(struct device *d, struct device_attribute *a, char *b) \
{ (void)a; return iris_stop_snapshot_show(d, b, plane, post); } \
static DEVICE_ATTR_RO(name)
IRIS_STOP_ATTR(stop_pre_input, 0, false);
IRIS_STOP_ATTR(stop_pre_output, 1, false);
IRIS_STOP_ATTR(stop_post_input, 0, true);
IRIS_STOP_ATTR(stop_post_output, 1, true);
static struct attribute *iris_stop_attrs[] = {
	&dev_attr_stop_observer_status.attr, &dev_attr_stop_pre_input.attr, &dev_attr_stop_pre_output.attr,
	    &dev_attr_stop_post_input.attr, &dev_attr_stop_post_output.attr,
	    &dev_attr_stop_failure_status.attr, NULL,
};
static const struct attribute_group iris_stop_group = { .attrs = iris_stop_attrs };

/* PREPARE-only: all fallible attribute creation precedes provider execution. */
static int iris_experiment_status_create(struct iris_core *core)
{
	int ret;

	ret = device_create_file(core->dev, &dev_attr_init_only_status);
	if (ret)
		return ret;
	if (core->decode_once) {
		ret = device_create_file(core->dev, &dev_attr_decode_once_status);
		if (ret) {
			device_remove_file(core->dev, &dev_attr_init_only_status);
			return ret;
		}
		ret = device_create_file(core->dev, &dev_attr_decode_once_progress);
		if (ret) {
			/* PREPARE-only CPU attributes; no provider or DMA owner release. */
			device_remove_file(core->dev, &dev_attr_decode_once_status);
			device_remove_file(core->dev, &dev_attr_init_only_status);
			return ret;
		}
		ret = device_create_file(core->dev, &dev_attr_decode_once_release);
		if (ret) {
			device_remove_file(core->dev, &dev_attr_decode_once_progress);
			device_remove_file(core->dev, &dev_attr_decode_once_status);
			device_remove_file(core->dev, &dev_attr_init_only_status);
			return ret;
		}
		ret = sysfs_create_group(&core->dev->kobj, &iris_stop_group);
		if (ret)
			return ret; /* PREPARE-only CPU attributes; retained error owner. */
		ret = device_create_file(core->dev, &dev_attr_decode_once_input);
		if (ret) {
			device_remove_file(core->dev, &dev_attr_decode_once_release);
			device_remove_file(core->dev, &dev_attr_decode_once_progress);
			device_remove_file(core->dev, &dev_attr_decode_once_status);
			device_remove_file(core->dev, &dev_attr_init_only_status);
			return ret;
		}
	}
	return 0;
}

static void iris_experiment_status_remove(struct iris_core *core)
{
	if (core->decode_once) {
		sysfs_remove_group(&core->dev->kobj,&iris_stop_group);
		device_remove_file(core->dev, &dev_attr_decode_once_release);
		device_remove_file(core->dev, &dev_attr_decode_once_input);
		device_remove_file(core->dev, &dev_attr_decode_once_progress);
		device_remove_file(core->dev, &dev_attr_decode_once_status);
	}
	device_remove_file(core->dev, &dev_attr_init_only_status);
}


static bool iris_is_init_only_device(struct device *dev)
{
	const char *token;

	return dev->of_node &&
	       of_device_is_compatible(dev->of_node, "qcom,sm8450-iris-init-only") &&
	       device_property_read_string(dev, IRIS_INIT_ONLY_PROPERTY, &token) == 0 &&
	       !strcmp(token, IRIS_INIT_ONLY_TOKEN);
}

static bool iris_is_native_repeat_device(struct device *dev)
{
	const char *token;

	return IRIS_NATIVE_REPEAT_ENABLED && dev->of_node &&
		of_device_is_compatible(dev->of_node, "qcom,sm8450-iris-decode-once") &&
		!of_device_is_compatible(dev->of_node, "qcom,sm8450-iris-init-only") &&
		device_property_read_string(dev, IRIS_NATIVE_REPEAT_PROPERTY, &token) == 0 &&
		!strcmp(token, IRIS_NATIVE_REPEAT_TOKEN);
}

static bool iris_is_decode_once_device(struct device *dev)
{
	const char *token;

	return dev->of_node &&
	       of_device_is_compatible(dev->of_node, "qcom,sm8450-iris-decode-once") &&
	       !of_device_is_compatible(dev->of_node, "qcom,sm8450-iris-init-only") &&
	       device_property_read_string(dev, IRIS_DECODE_ONCE_PROPERTY, &token) == 0 &&
	       !strcmp(token, IRIS_DECODE_ONCE_TOKEN) &&
	       (!device_property_present(dev, IRIS_NATIVE_REPEAT_PROPERTY) ||
		iris_is_native_repeat_device(dev));
}


static bool iris_is_release_observer_device(struct device *dev)
{
	const char *token;

	return IRIS_RELEASE_OBSERVER_ENABLED && iris_is_decode_once_device(dev) &&
				    device_property_read_string(dev, IRIS_RELEASE_OBSERVER_PROPERTY, &token) == 0 &&
				    !strcmp(token, IRIS_RELEASE_OBSERVER_TOKEN);
}

static bool iris_is_stop_observer_device(struct device *dev)
{
	const char *token;
	return IRIS_STOP_OBSERVER_ENABLED && iris_is_decode_once_device(dev) &&
	    device_property_read_string(dev, IRIS_STOP_OBSERVER_PROPERTY, &token)==0 &&
	    !strcmp(token, IRIS_STOP_OBSERVER_TOKEN);
}

static bool iris_init_only_lifetime_hold(struct iris_core *core)
{
	/* Intentional unmatched reference: this module lives until the RAM boot ends. */
	if (!try_module_get(THIS_MODULE))
		return false;
	WRITE_ONCE(core->init_only_module_held, true);
	WRITE_ONCE(core->init_only_remove_blocked, true);
	WRITE_ONCE(core->init_only, true);
	WRITE_ONCE(core->removing, true);
	return true;
}

static int iris_init_only_probe(struct platform_device *pdev,
				struct iris_core **corep)
{
	struct device *dev = &pdev->dev;
	struct iris_core *core;
	int ret, i;

	if (!iris_is_init_only_device(dev) && !iris_is_decode_once_device(dev))
		return -EOPNOTSUPP;
	core = kzalloc(sizeof(*core), GFP_KERNEL);
	if (!core)
		return -ENOMEM;
	core->dev = get_device(dev);
	refcount_set(&core->owner_refs, 1);
	core->state = IRIS_CORE_DEINIT;
	mutex_init(&core->lock);
	mutex_init(&core->lifecycle_lock);
	mutex_init(&core->quiesce_lock);
	mutex_init(&core->remove_lock);
	mutex_init(&core->detach_lock);
	raw_spin_lock_init(&core->irq_lock);
	INIT_LIST_HEAD(&core->instances);
	INIT_LIST_HEAD(&core->orphan_instances);
	INIT_DELAYED_WORK(&core->sys_error_handler, iris_sys_error_handler);
	init_completion(&core->core_init_done);
	iris_hfi_pending_init(&core->init_pending);
	for (i = 0; i < IRIS_INIT_ONLY_STAGE_COUNT; i++)
		core->init_only_status[i] = -ECANCELED;
	core->response_packet = kzalloc(IFACEQ_CORE_PKT_SIZE, GFP_KERNEL);
	if (!core->response_packet) {
		ret = -ENOMEM;
		goto err_core;
	}
	core->decode_once = iris_is_decode_once_device(dev);
	core->native_repeat = core->decode_once && iris_is_native_repeat_device(dev);
	core->release_observer_enabled = iris_is_release_observer_device(dev);
	core->stop_observer_enabled = iris_is_stop_observer_device(dev);
	core->decode_once_enabled = core->decode_once && IRIS_DECODE_ONCE_ENABLED;
	/* Hooks readiness is set after the lifetime pin, before any publication. */
	core->iris_platform_data = &sm8450_data;
	iris_init_ops(core);
	core->iris_platform_data->init_hfi_command_ops(core);
	core->iris_platform_data->init_hfi_response_ops(core);
	iris_session_init_caps(core);
	core->init_only_stage = IRIS_INIT_ONLY_ADMISSION;
	core->init_only_status[IRIS_INIT_ONLY_ADMISSION] = 0;
	/* PREPARE only: retain CPU storage/code before publication. These pins do
	 * not prove registration, power readiness or either queue's DMA backing.
	 */
	if (!iris_init_only_lifetime_hold(core)) {
		ret = -ENODEV;
		goto err_core;
	}
	core->decode_once_retention_ready = core->decode_once &&
		IRIS_DECODE_ONCE_RETENTION_INSTALLED;
	platform_set_drvdata(pdev, core);
	ret = iris_experiment_status_create(core);
	if (ret)
		goto err_retained;
	core->hardware_owner_held = true;
	iris_core_get(core);
	core->init_only_prepared = true;
	core->init_only_run_error = -ECANCELED;
	*corep = core;
	return 0;
err_retained:
	platform_set_drvdata(pdev, NULL);
	/* The experiment pin is unmatched, including PREPARE publication errors. */
	return ret;
err_core:
	platform_set_drvdata(pdev, NULL);
	iris_core_put(core);
	return ret;
}

/* Last fallible frontend work; caller holds the one-shot device owner. */
static int iris_decode_once_publish(struct iris_core *core)
{
	int ret;

	mutex_lock(&core->lock);
	if (!core->decode_once || !core->decode_once_enabled ||
	    !core->decode_once_retention_ready || !core->init_only ||
	    !core->removing || core->firmware_touched || core->firmware_may_run ||
	    core->init_only_attempted || core->terminal_shutdown ||
	    core->v4l2_registered || core->decoder_registered ||
	    core->encoder_registered || core->vdev_dec || core->vdev_enc) {
		mutex_unlock(&core->lock);
		return -ESHUTDOWN;
	}
	mutex_unlock(&core->lock);
	ret = v4l2_device_register(core->dev, &core->v4l2_dev);
	if (ret)
		return ret;
	core->v4l2_registered = true;
	/* Even failed registration preserves its driver-owned prefix. */
	return iris_register_video_device(core, DECODER);
}

/* Kernel-internal isolated-owner entry, never a sysfs store or host command.
 * Call in process context AFTER registration/probe/binding has committed; do
 * not hold device_lock(). The isolated owner must keep this static pdev and all
 * suppliers registered and prohibit suspend/shutdown/retry until external reset.
 * device_lock serializes this invocation with ordinary binding/removal, but is
 * not a lifetime veto after return. No owner implementation/proof is supplied.
 */
static int iris_experiment_run(struct platform_device *pdev, bool decode_once)
{
	struct device *dev;
	struct iris_core *core;
	int ret, terminal_ret;

	if (!pdev)
		return -EINVAL;
	if (in_interrupt())
		return -EWOULDBLOCK;
	dev = &pdev->dev;
	device_lock(dev);
	/* Binding commit is checked independently of probe's drvdata publication. */
	if (!device_is_registered(dev) || !device_is_bound(dev) ||
	    dev->driver != &qcom_iris_driver.driver) {
		ret = -ENODEV;
		goto unlock_device;
	}
	core = platform_get_drvdata(pdev);
	if (!core || core->decode_once != decode_once ||
	    (decode_once ? !iris_is_decode_once_device(dev) : !iris_is_init_only_device(dev))) {
		ret = -EOPNOTSUPP;
		goto unlock_device;
	}
	if (current_work() == &core->sys_error_handler.work) {
		ret = -EDEADLK;
		goto unlock_device;
	}
	mutex_lock(&core->lock);
	if (core->init_only_run_claimed) {
		ret = -EALREADY;
		goto unlock_core;
	}
	if (!core->init_only_prepared || !core->init_only ||
	    !core->init_only_module_held || !core->init_only_remove_blocked ||
	    !core->hardware_owner_held || !core->removing ||
	    core->init_only_remove_seen || core->terminal_shutdown ||
	    core->resources_releasing || core->init_only_attempted) {
		ret = -ESHUTDOWN;
		goto unlock_core;
	}
	/* Only a valid post-bind invocation consumes the one-shot, even when closed.
	 * Pre-bind/wrong-device refusals do not mutate the previous result.
	 */
	core->init_only_post_bind = true;
	core->init_only_run_claimed = true;
	core->init_only_stage = IRIS_INIT_ONLY_ADMISSION;
	mutex_unlock(&core->lock);
	if (!IRIS_INIT_ONLY_EXECUTION_ENABLED ||
	    !IRIS_INIT_ONLY_PROVIDER_ADMISSION_ENABLED ||
	    (decode_once && (!IRIS_DECODE_ONCE_ENABLED ||
	     !core->decode_once_enabled || !core->decode_once_retention_ready))) {
		ret = -EOPNOTSUPP;
		goto retain_admission;
	}
	/* Unreachable in this side candidate. Kept only to exercise the coordinator
	 * with host seams; aggregate provider internals remain explicitly unadmitted.
	 */
	WRITE_ONCE(core->init_only_provider_attempted, true);
	ret = iris_acquire_mmio(core, pdev);
	if (ret)
		goto retain_admission;
	ret = iris_init_resources(core);
	if (ret)
		goto retain_admission;
	core->irq = platform_get_irq(pdev, 0);
	if (core->irq < 0) {
		ret = core->irq;
		goto retain_admission;
	}
	ret = iris_request_irq(core);
	if (ret)
		goto retain_admission;
	ret = dma_set_mask_and_coherent(dev, core->iris_platform_data->dma_mask);
	if (ret)
		goto retain_admission;
	dma_set_max_seg_size(dev, DMA_BIT_MASK(32));
	dma_set_seg_boundary(dev, DMA_BIT_MASK(32));
	ret = pm_runtime_set_active(dev);
	if (ret)
		goto retain_admission;
	if (!decode_once) {
		pm_runtime_set_autosuspend_delay(dev, AUTOSUSPEND_DELAY_VALUE);
		pm_runtime_use_autosuspend(dev);
	}
	pm_runtime_enable(dev);
	core->runtime_pm_enabled = true;
	core->irq_probe_unpublished = false;
	mutex_lock(&core->lock);
	iris_core_pm_guard_get(core);
	mutex_unlock(&core->lock);

	/* Last fallible frontend operations BEFORE any firmware touch. Opens remain
	 * blocked by removing/admission until a checked matching SYS_INIT ACK.
	 * A failure retains the acquired prefix; no failed-probe/devres unwind.
	 */
	if (decode_once) {
		ret = iris_decode_once_publish(core);
		if (ret)
			goto retain_admission;
	}
	ret = iris_core_init(core);
	/* A successful decode ACK keeps control/frame response processing alive. */
	if (ret || !decode_once) {
		terminal_ret = iris_core_init_only_terminal(core);
		if (!ret)
			ret = terminal_ret;
	}
	goto record_run;
retain_admission:
	mutex_lock(&core->lock);
	core->init_only_status[IRIS_INIT_ONLY_ADMISSION] = ret;
	mutex_unlock(&core->lock);
record_run:
	/* Return the real error without any driver/framework failed-probe unwind.
	 * Preserve every acquired/ambiguous owner; terminal is CPU drain only.
	 */
	mutex_lock(&core->lock);
	if (ret && !core->init_only_error)
		core->init_only_error = ret;
	core->init_only_run_error = ret;
	core->init_only_run_done = true;
	if (!ret && decode_once) {
		ret = iris_decode_once_admit_locked(core);
		if (ret) {
			core->init_only_run_error = ret;
			core->init_only_error = ret;
		}
	}
unlock_core:
	mutex_unlock(&core->lock);
unlock_device:
	device_unlock(dev);
	return ret;
}
int iris_init_only_run(struct platform_device *pdev)
{
	return iris_experiment_run(pdev, false);
}
EXPORT_SYMBOL_GPL(iris_init_only_run);

int iris_decode_once_run(struct platform_device *pdev)
{
	return iris_experiment_run(pdev, true);
}
EXPORT_SYMBOL_GPL(iris_decode_once_run);

static int iris_register_video_device(struct iris_core *core, enum domain_type type)
{
	struct video_device *vdev;
	int ret;

	if (core->decode_once && type != DECODER)
		return -EOPNOTSUPP;
	vdev = video_device_alloc();
	if (!vdev)
		return -ENOMEM;

	mutex_lock(&core->lock);
	core->video_owners++;
	mutex_unlock(&core->lock);
	iris_core_get(core);
	video_set_drvdata(vdev, core);
	vdev->release = iris_video_device_release;
	vdev->fops = core->iris_v4l2_file_ops;
	vdev->vfl_dir = VFL_DIR_M2M;
	vdev->v4l2_dev = &core->v4l2_dev;
	vdev->device_caps = V4L2_CAP_VIDEO_M2M_MPLANE | V4L2_CAP_STREAMING;

	if (type == DECODER) {
		strscpy(vdev->name, "qcom-iris-decoder", sizeof(vdev->name));
		vdev->ioctl_ops = core->iris_v4l2_ioctl_ops_dec;
		core->vdev_dec = vdev;
	} else if (type == ENCODER) {
		strscpy(vdev->name, "qcom-iris-encoder", sizeof(vdev->name));
		vdev->ioctl_ops = core->iris_v4l2_ioctl_ops_enc;
		core->vdev_enc = vdev;
	} else {
		ret = -EINVAL;
		goto err_vdev_release;
	}

	ret = video_register_device(vdev, VFL_TYPE_VIDEO, -1);
	if (ret)
		goto err_vdev_release;

	if (type == DECODER)
		core->decoder_registered = true;
	else
		core->encoder_registered = true;
	return 0;

err_vdev_release:
	/* Decode publication is pre-firmware; retain even its failed CPU prefix. */
	if (!core->decode_once)
		iris_video_device_release(vdev);

	return ret;
}

int iris_device_detach(struct iris_core *core)
{
	struct video_device *dec = NULL, *enc = NULL;
	int ret;

	if (READ_ONCE(core->init_only))
		return -EOPNOTSUPP;
	if (current_work() == &core->sys_error_handler.work)
		return -EDEADLK;
	if (in_interrupt())
		return -EWOULDBLOCK;
	/* This caller must already own a core reference; hold one through callbacks. */
	iris_core_get(core);
	mutex_lock(&core->detach_lock);
	mutex_lock(&core->lock);
	if (core->detach_complete) {
		ret = 0;
		goto unlock_core;
	}
	core->removing = true;
	/* Each registered reference is dropped once, outside callback-required locks. */
	if (!core->frontend_detached) {
		core->frontend_detached = true;
		if (core->decoder_registered) {
			dec = core->vdev_dec;
			core->decoder_registered = false;
		}
		if (core->encoder_registered) {
			enc = core->vdev_enc;
			core->encoder_registered = false;
		}
	}
	mutex_unlock(&core->lock);
	video_unregister_device(dec);
	video_unregister_device(enc);
	mutex_lock(&core->lock);
	/* unregister is not a drain: release/mmap/video owners may remain. */
	if (core->open_inflight || core->open_owners ||
	    core->video_owners || !list_empty(&core->instances)) {
		ret = -EBUSY;
		goto unlock_core;
	}
	/* Never call the unverified void power contract from this coordinator. */
	if (!iris_vpu_has_checked_power(core)) {
		ret = -EOPNOTSUPP;
		goto unlock_core;
	}
	if (core->power_state_unknown) {
		ret = -EUCLEAN;
		goto unlock_core;
	}
	mutex_unlock(&core->lock);
	/* A failed release may have disabled PM; don't attempt a fresh resume. */
	if (!core->remove_prepared) {
		/* Detached orphans may only retire after actual terminal shutdown/drain. */
		ret = iris_core_quiesce(core);
		if (ret)
			goto unlock;
		ret = iris_reclaim_orphan_instances(core);
		if (ret)
			goto unlock;
		ret = iris_core_prepare_remove(core);
		if (ret)
			goto unlock;
	}
	ret = iris_release_resources(core);
	if (ret)
		goto unlock;
	if (core->v4l2_registered) {
		v4l2_device_unregister(&core->v4l2_dev);
		core->v4l2_registered = false;
	}
	mutex_lock(&core->lock);
	core->detach_complete = true;
	ret = 0;
unlock_core:
	mutex_unlock(&core->lock);
unlock:
	mutex_unlock(&core->detach_lock);
	iris_core_put(core);
	return ret;
}

static void iris_remove(struct platform_device *pdev)
{
	struct iris_core *core;

	core = platform_get_drvdata(pdev);
	if (!core)
		return;
	if (READ_ONCE(core->init_only)) {
		/* Observation, not a veto. Never let a later rebind authorize RUN. */
		mutex_lock(&core->lock);
		core->init_only_remove_seen = true;
		mutex_unlock(&core->lock);
		return;
	}

	iris_core_deinit(core);

	video_unregister_device(core->vdev_dec);
	video_unregister_device(core->vdev_enc);

	v4l2_device_unregister(&core->v4l2_dev);

	/* CPU core storage lives until the final binding/video/file/orphan owner. */
}

static void iris_sys_error_handler(struct work_struct *work)
{
	struct iris_core *core =
			container_of(work, struct iris_core, sys_error_handler.work);

	/* Private terminal prototype: no restart races with reclaim. */
	mutex_lock(&core->lock);
	if (!core->recovery_blocked && !core->terminal_shutdown) {
		core->core_ready = false;
		core->state = IRIS_CORE_ERROR;
		iris_hfi_pending_cancel(&core->init_pending);
		complete(&core->core_init_done);
	}
	mutex_unlock(&core->lock);
}

static int iris_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct iris_core *core;
	int ret;

	if (of_device_get_match_data(dev) == &sm8450_data) {
		if (!iris_is_init_only_device(dev) && !iris_is_decode_once_device(dev))
			return dev_err_probe(dev, -EOPNOTSUPP,
					     "SM8450 init-only admission token missing\n");
		ret = iris_init_only_probe(pdev, &core);
		if (ret)
			return dev_err_probe(dev, ret, "SM8450 init-only setup failed\n");
		return 0;
	}
	core = kzalloc(sizeof(*core), GFP_KERNEL);
	if (!core)
		return -ENOMEM;
	core->dev = get_device(dev);
	refcount_set(&core->owner_refs, 1);
	core->removing = true;
	core->state = IRIS_CORE_DEINIT;
	mutex_init(&core->lock);
	mutex_init(&core->lifecycle_lock);
	mutex_init(&core->quiesce_lock);
	mutex_init(&core->remove_lock);
	mutex_init(&core->detach_lock);
	raw_spin_lock_init(&core->irq_lock);
	INIT_LIST_HEAD(&core->instances);
	INIT_LIST_HEAD(&core->orphan_instances);
	INIT_DELAYED_WORK(&core->sys_error_handler, iris_sys_error_handler);
	init_completion(&core->core_init_done);
	iris_hfi_pending_init(&core->init_pending);
	/* This action owns only CPU storage, never hardware release. */
	ret = devm_add_action_or_reset(dev, iris_binding_release, core);
	if (ret)
		return ret;
	core->response_packet = kzalloc(IFACEQ_CORE_PKT_SIZE, GFP_KERNEL);
	if (!core->response_packet)
		return -ENOMEM;
	core->iris_platform_data = of_device_get_match_data(dev);
	iris_init_ops(core);
	core->iris_platform_data->init_hfi_command_ops(core);
	core->iris_platform_data->init_hfi_response_ops(core);
	iris_session_init_caps(core);
	iris_core_get(core);
	core->hardware_owner_held = true;
	ret = iris_acquire_mmio(core, pdev);
	if (ret)
		goto err_hardware;
	ret = iris_init_resources(core);
	if (ret)
		goto err_resources;
	core->irq = platform_get_irq(pdev, 0);
	if (core->irq < 0) {
		ret = core->irq;
		goto err_resources;
	}
	ret = iris_request_irq(core);
	if (ret)
		goto err_resources;
	ret = dma_set_mask_and_coherent(dev, core->iris_platform_data->dma_mask);
	if (ret)
		goto err_irq;
	dma_set_max_seg_size(dev, DMA_BIT_MASK(32));
	dma_set_seg_boundary(dev, DMA_BIT_MASK(32));
	platform_set_drvdata(pdev, core);
	pm_runtime_set_autosuspend_delay(dev, AUTOSUSPEND_DELAY_VALUE);
	pm_runtime_use_autosuspend(dev);
	pm_runtime_enable(dev);
	core->runtime_pm_enabled = true;
	mutex_lock(&core->lock);
	iris_core_pm_guard_get(core);
	mutex_unlock(&core->lock);
	ret = v4l2_device_register(dev, &core->v4l2_dev);
	if (ret)
		goto err_pm;
	core->v4l2_registered = true;
	ret = iris_register_video_device(core, DECODER);
	if (ret)
		goto err_v4l2;
	ret = iris_register_video_device(core, ENCODER);
	if (ret)
		goto err_video;
	mutex_lock(&core->lock);
	core->irq_probe_unpublished = false;
	core->removing = false;
	mutex_unlock(&core->lock);
	return 0;
err_video:
	core->decoder_registered = false;
	video_unregister_device(core->vdev_dec);
err_v4l2:
	v4l2_device_unregister(&core->v4l2_dev);
	core->v4l2_registered = false;
err_pm:
	mutex_lock(&core->lock);
	iris_core_pm_guard_put(core);
	mutex_unlock(&core->lock);
	pm_runtime_barrier(dev);
	pm_runtime_disable(dev);
	pm_runtime_dont_use_autosuspend(dev);
	core->runtime_pm_enabled = false;
	platform_set_drvdata(pdev, NULL);
err_irq:
	/* Preserve the initiating error separately if unregister cannot finish. */
	core->probe_cleanup_error = iris_irq_unregister(core);
	if (core->probe_cleanup_error) {
		dev_err(core->dev, "probe IRQ cleanup unverified: %d\n", core->probe_cleanup_error);
		return ret;
	}
	cancel_delayed_work_sync(&core->sys_error_handler);
err_resources:
	iris_release_resource_handles(core);
	iris_release_mmio(core);
err_hardware:
	core->hardware_owner_held = false;
	iris_core_put(core);
	return ret;
}
static int __maybe_unused iris_pm_suspend(struct device *dev)
{
	struct iris_core *core;
	int ret = 0;

	core = dev_get_drvdata(dev);
	if (!core)
		return 0;

	mutex_lock(&core->lock);
	if (core->resources_releasing || !core->hardware_owner_held || core->state != IRIS_CORE_INIT)
		goto exit;

	if (core->decode_once) {
		ret = -EOPNOTSUPP;
		goto exit;
	}
	ret = iris_hfi_pm_suspend(core);

exit:
	mutex_unlock(&core->lock);

	return ret;
}

static int __maybe_unused iris_pm_resume(struct device *dev)
{
	struct iris_core *core;
	int ret = 0;

	core = dev_get_drvdata(dev);
	if (!core)
		return 0;

	mutex_lock(&core->lock);
	if (core->resources_releasing || !core->hardware_owner_held || core->state != IRIS_CORE_INIT)
		goto exit;

	if (core->decode_once) {
		ret = -EOPNOTSUPP;
		goto exit;
	}
	ret = iris_hfi_pm_resume(core);
	pm_runtime_mark_last_busy(core->dev);

exit:
	mutex_unlock(&core->lock);

	return ret;
}

static int __maybe_unused iris_system_suspend(struct device *dev)
{
	struct iris_core *core = dev_get_drvdata(dev);

	if (core && core->decode_once)
		return -EOPNOTSUPP;
	return pm_runtime_force_suspend(dev);
}

static int __maybe_unused iris_system_resume(struct device *dev)
{
	struct iris_core *core = dev_get_drvdata(dev);

	if (core && core->decode_once)
		return -EOPNOTSUPP;
	return pm_runtime_force_resume(dev);
}

static const struct dev_pm_ops iris_pm_ops = {
	SET_SYSTEM_SLEEP_PM_OPS(iris_system_suspend, iris_system_resume)
	SET_RUNTIME_PM_OPS(iris_pm_suspend, iris_pm_resume, NULL)
};

static const struct of_device_id iris_dt_match[] = {
	{
		.compatible = "qcom,qcs8300-iris",
		.data = &qcs8300_data,
	},
#if (!IS_ENABLED(CONFIG_VIDEO_QCOM_VENUS))
	{
		.compatible = "qcom,sm8250-venus",
		.data = &sm8250_data,
	},
#endif
	{
		.compatible = "qcom,sm8450-iris-decode-once",
		.data = &sm8450_data,
	},
	{
		.compatible = "qcom,sm8450-iris-init-only",
		.data = &sm8450_data,
	},
	{
		.compatible = "qcom,sm8450-iris",
		.data = &sm8450_data,
	},
	{
		.compatible = "qcom,sm8550-iris",
		.data = &sm8550_data,
	},
	{
		.compatible = "qcom,sm8650-iris",
		.data = &sm8650_data,
	},
	{
		.compatible = "qcom,sm8750-iris",
		.data = &sm8750_data,
	},
	{ },
};
MODULE_DEVICE_TABLE(of, iris_dt_match);

static struct platform_driver qcom_iris_driver = {
	.probe = iris_probe,
	.remove = iris_remove,
	.driver = {
		.name = "qcom-iris",
		.suppress_bind_attrs = true,
		.of_match_table = iris_dt_match,
		.pm = &iris_pm_ops,
	},
};

module_platform_driver(qcom_iris_driver);
MODULE_DESCRIPTION("Qualcomm iris video driver");
MODULE_LICENSE("GPL");
