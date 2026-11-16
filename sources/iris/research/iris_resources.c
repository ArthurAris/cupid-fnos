// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/clk.h>
#include <linux/interconnect.h>
#include <linux/pm_domain.h>
#include <linux/pm_opp.h>
#include <linux/pm_runtime.h>
#include <linux/reset.h>

#include "iris_core.h"
#include "iris_resources.h"

#define BW_THRESHOLD 50000

int iris_set_icc_bw(struct iris_core *core, unsigned long icc_bw)
{
	unsigned long bw_kbps = 0, bw_prev = 0;
	const struct icc_info *icc_tbl;
	u32 i;
	int ret;

	icc_tbl = core->iris_platform_data->icc_tbl;

	for (i = 0; i < core->icc_count; i++) {
		if (!strcmp(core->icc_tbl[i].name, "video-mem")) {
			bw_kbps = icc_bw;
			bw_prev = core->power.icc_bw;

			bw_kbps = clamp_t(typeof(bw_kbps), bw_kbps,
					  icc_tbl[i].bw_min_kbps, icc_tbl[i].bw_max_kbps);

			if (!core->power_progress.icc_dirty &&
			    (bw_kbps > bw_prev ? bw_kbps - bw_prev : bw_prev - bw_kbps) < BW_THRESHOLD && bw_prev)
				return 0;

			core->icc_tbl[i].avg_bw = bw_kbps;
			/* A failed bulk call may already have changed provider requests. */
			core->power_progress.icc_dirty = true;
			ret = icc_bulk_set_bw(core->icc_count, core->icc_tbl);
			if (ret)
				return ret;
			core->power_progress.icc_voted = true;
			core->power_progress.icc_dirty = false;
			core->power.icc_bw = bw_kbps;
			return 0;
		}
	}

	core->power_progress.icc_dirty = true;
	ret = icc_bulk_set_bw(core->icc_count, core->icc_tbl);
	if (!ret) {
		core->power_progress.icc_voted = true;
		core->power_progress.icc_dirty = false;
	}
	return ret;
}

int iris_unset_icc_bw(struct iris_core *core)
{
	u32 i;
	int ret;

	for (i = 0; i < core->icc_count; i++) {
		core->icc_tbl[i].avg_bw = 0;
		core->icc_tbl[i].peak_bw = 0;
	}

	core->power_progress.icc_dirty = true;
	ret = icc_bulk_set_bw(core->icc_count, core->icc_tbl);
	/* Only an actual successful unvote retires provider ownership. */
	if (!ret) {
		core->power.icc_bw = 0;
		core->power_progress.icc_voted = false;
		core->power_progress.icc_dirty = false;
	}

	return ret;
}

int iris_init_only_pm_ready(struct iris_core *core)
{
	struct iris_init_only_pm_progress *p = &core->init_only_pm;
	struct dev_pm_domain_list *ctrl = core->pmdomain_tbl;
	struct dev_pm_domain_list *opp = core->opp_pmdomain_tbl;
	u32 i;
	int ret;

	if (core->power_state_unknown || p->error) {
		ret = p->error ? p->error : core->power_error;
		if (!ret)
			ret = -ESHUTDOWN;
		goto failed;
	}
	if (!p->complete || !core->domains_attached || !core->opp_domains_attached ||
	    !core->opp_table_added || p->ctrl_count != 2 || p->opp_count != 2 ||
	    !ctrl || !opp || ctrl->num_pds != 2 || opp->num_pds != 2 ||
	    !ctrl->pd_devs || !opp->pd_devs || !opp->pd_links || !opp->opp_tokens) {
		ret = -EINVAL;
		goto failed;
	}
	for (i = 0; i < 2; i++) {
		if (IS_ERR_OR_NULL(ctrl->pd_devs[i]) || IS_ERR_OR_NULL(opp->pd_devs[i]) ||
		    !opp->pd_links[i] || !p->domains[i].acquired ||
		    !p->domains[i + 2].acquired || !p->domains[i + 2].token_held ||
		    !p->domains[i + 2].link_held) {
			ret = -EINVAL;
			goto failed;
		}
	}
	if (ctrl->pd_devs[0] == ctrl->pd_devs[1] || opp->pd_devs[0] == opp->pd_devs[1]) {
		ret = -EINVAL;
		goto failed;
	}
	return 0;
failed:
	core->power_state_unknown = true;
	if (!core->power_error)
		core->power_error = ret;
	return ret;
}

int iris_init_only_activate_opp_domains(struct iris_core *core)
{
	struct iris_init_only_pm_progress *p = &core->init_only_pm;
	struct iris_pm_get_progress *attempt;
	u32 i;
	int ret;

	ret = iris_init_only_pm_ready(core);
	if (ret)
		return ret;
	if (p->opp_active)
		return 0;
	for (i = 0; i < 2; i++) {
		attempt = &p->opp_pm_get[i];
		if (attempt->attempted) {
			ret = attempt->error ? attempt->error : -EALREADY;
			goto failed;
		}
		attempt->dev = core->opp_pmdomain_tbl->pd_devs[i];
		attempt->attempted = true;
		/* get_sync increments even on failure; never drop or retry that ref. */
		attempt->pm_get_held = true;
		WRITE_ONCE(core->init_only_provider_attempted, true);
		ret = pm_runtime_get_sync(attempt->dev);
		if (ret < 0) {
			attempt->error = ret;
			goto failed;
		}
	}
	p->opp_active = true;
	return 0;
failed:
	p->error = ret;
	core->power_state_unknown = true;
	if (!core->power_error)
		core->power_error = ret;
	return ret;
}

int iris_init_only_pm_active(struct iris_core *core)
{
	struct iris_init_only_pm_progress *p = &core->init_only_pm;
	struct iris_pm_get_progress *attempt;
	struct device *pd;
	u32 i;
	int ret;

	ret = iris_init_only_pm_ready(core);
	if (ret)
		return ret;
	if (!p->opp_active || !core->power_progress.ctrl_domain_ref ||
	    !core->power_progress.hw_domain_ref) {
		ret = -EAGAIN;
		goto failed;
	}
	for (i = 0; i < 4; i++) {
		if (i < 2) {
			pd = core->pmdomain_tbl->pd_devs[i];
			attempt = i ? &core->power_progress.hw_pm_get :
				      &core->power_progress.ctrl_pm_get;
		} else {
			pd = core->opp_pmdomain_tbl->pd_devs[i - 2];
			attempt = &p->opp_pm_get[i - 2];
		}
		/* Exact helper active() also accepts disabled PM: require real ACTIVE. */
		if (!attempt->attempted || !attempt->pm_get_held || attempt->error ||
		    attempt->dev != pd || !pm_runtime_enabled(pd) ||
		    READ_ONCE(pd->power.runtime_status) != RPM_ACTIVE ||
		    atomic_read(&pd->power.usage_count) < 1) {
			ret = attempt->error ? attempt->error : -EAGAIN;
			goto failed;
		}
	}
	return 0;
failed:
	p->error = ret;
	core->power_state_unknown = true;
	if (!core->power_error)
		core->power_error = ret;
	return ret;
}

int iris_enable_power_domains(struct iris_core *core, struct device *pd_dev)
{
	struct iris_pm_get_progress *attempt = NULL;
	int ret;

	if (READ_ONCE(core->init_only)) {
		ret = iris_init_only_pm_ready(core);
		if (ret)
			return ret;
		/* Fixed CTRL/HW identity, one-shot even when the OPP step fails. */
		if (!core->pmdomain_tbl || core->pmdomain_tbl->num_pds != 2 ||
		    !core->pmdomain_tbl->pd_devs || !pd_dev ||
		    core->pmdomain_tbl->pd_devs[0] == core->pmdomain_tbl->pd_devs[1])
			return -EINVAL;
		if (pd_dev == core->pmdomain_tbl->pd_devs[0])
			attempt = &core->power_progress.ctrl_pm_get;
		else if (pd_dev == core->pmdomain_tbl->pd_devs[1])
			attempt = &core->power_progress.hw_pm_get;
		else
			return -EINVAL;
		if (attempt->attempted)
			return -EALREADY;
		if (core->power_state_unknown)
			return -ESHUTDOWN;
		attempt->dev = pd_dev;
		attempt->attempted = true;
		ret = iris_init_only_activate_opp_domains(core);
		if (ret)
			goto failed;
	}

	core->power_progress.opp_dirty = true;
	ret = dev_pm_opp_set_rate(core->dev, ULONG_MAX);
	core->power_opp_error = ret;
	if (ret)
		goto failed;

	/* Exact c1147 get_sync keeps its increment even on runtime-resume error. */
	if (attempt)
		attempt->pm_get_held = true;
	ret = pm_runtime_get_sync(pd_dev);
	if (ret < 0) {
		if (!attempt)
			pm_runtime_put_noidle(pd_dev);
		goto failed;
	}

	return 0;
failed:
	if (attempt) {
		attempt->error = ret;
		core->power_state_unknown = true;
		if (!core->power_error)
			core->power_error = ret;
	}
	return ret;
}

int iris_disable_power_domains(struct iris_core *core, struct device *pd_dev)
{
	int ret, put_ret;

	core->power_progress.opp_dirty = true;
	ret = dev_pm_opp_set_rate(core->dev, 0);
	core->power_opp_error = ret;
	if (!ret)
		core->power_progress.opp_dirty = false;
	/*
	 * The put consumes its reference even on failure. Keep its errno separate
	 * from OPP retirement, and normalize all nonnegative PM success values.
	 */
	put_ret = pm_runtime_put_sync(pd_dev);
	core->power_domain_put_error = put_ret < 0 ? put_ret : 0;
	if (ret)
		return ret;

	return core->power_domain_put_error;
}

static struct clk *iris_get_clk_by_type(struct iris_core *core, enum platform_clk_type clk_type)
{
	const struct platform_clk_data *clk_tbl;
	u32 clk_cnt, i, j;

	clk_tbl = core->iris_platform_data->clk_tbl;
	clk_cnt = core->iris_platform_data->clk_tbl_size;

	for (i = 0; i < clk_cnt; i++) {
		if (clk_tbl[i].clk_type == clk_type) {
			for (j = 0; core->clock_tbl && j < core->clk_count; j++) {
				if (!strcmp(core->clock_tbl[j].id, clk_tbl[i].clk_name))
					return core->clock_tbl[j].clk;
			}
		}
	}

	return NULL;
}

int iris_prepare_enable_clock(struct iris_core *core, enum platform_clk_type clk_type)
{
	struct clk *clock;

	clock = iris_get_clk_by_type(core, clk_type);
	if (!clock)
		return -EINVAL;

	return clk_prepare_enable(clock);
}

int iris_disable_unprepare_clock(struct iris_core *core, enum platform_clk_type clk_type)
{
	struct clk *clock;

	clock = iris_get_clk_by_type(core, clk_type);
	if (!clock)
		return -EINVAL;

	clk_disable_unprepare(clock);

	return 0;
}
