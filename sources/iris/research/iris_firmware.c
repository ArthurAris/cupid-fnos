// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/firmware.h>
#include <linux/firmware/qcom/qcom_scm.h>
#include <linux/of_address.h>
#include <linux/of_reserved_mem.h>
#include <linux/soc/qcom/mdt_loader.h>

#include "iris_core.h"
#include "iris_firmware.h"

#define MAX_FIRMWARE_NAME_SIZE	128

static int iris_load_fw_to_memory(struct iris_core *core, const char *fw_name)
{
	u32 pas_id = core->iris_platform_data->pas_id;
	const struct firmware *firmware = NULL;
	struct device *dev = core->dev;
	struct reserved_mem *rmem;
	struct device_node *node;
	phys_addr_t mem_phys;
	size_t res_size;
	ssize_t fw_size;
	void *mem_virt;
	int ret;

	if (strlen(fw_name) >= MAX_FIRMWARE_NAME_SIZE - 4)
		return -EINVAL;

	node = of_parse_phandle(dev->of_node, "memory-region", 0);
	if (!node)
		return -EINVAL;

	rmem = of_reserved_mem_lookup(node);
	of_node_put(node);
	if (!rmem)
		return -EINVAL;

	mem_phys = rmem->base;
	res_size = rmem->size;

	ret = request_firmware(&firmware, fw_name, dev);
	if (ret)
		return ret;

	fw_size = qcom_mdt_get_size(firmware);
	if (fw_size < 0 || res_size < (size_t)fw_size) {
		ret = -EINVAL;
		goto err_release_fw;
	}

	mem_virt = memremap(mem_phys, res_size, MEMREMAP_WC);
	if (!mem_virt) {
		ret = -ENOMEM;
		goto err_release_fw;
	}

	/* Target secure-loader implementation is not fully evidenced: retain on error. */
	core->firmware_touched = true;
	core->firmware_may_run = true;
	core->firmware_shutdown_verified = false;
	ret = qcom_mdt_load(dev, firmware, fw_name,
			    pas_id, mem_virt, mem_phys, res_size, NULL);

	memunmap(mem_virt);
err_release_fw:
	release_firmware(firmware);

	return ret;
}

int iris_fw_load(struct iris_core *core)
{
	struct tz_cp_config *cp_config = core->iris_platform_data->tz_cp_config_data;
	const char *fwpath = NULL;
	int ret;

	ret = of_property_read_string_index(core->dev->of_node, "firmware-name", 0,
					    &fwpath);
	if (ret)
		fwpath = core->iris_platform_data->fwname;

	core->firmware_load_error = 0;
	if (READ_ONCE(core->init_only))
		core->firmware_shutdown_error = -ECANCELED;
	ret = iris_load_fw_to_memory(core, fwpath);
	if (ret) {
		dev_err(core->dev, "firmware download failed\n");
		core->firmware_load_error = ret;
		return ret;
	}

	ret = qcom_scm_pas_auth_and_reset(core->iris_platform_data->pas_id);
	if (ret)  {
		dev_err(core->dev, "auth and reset failed: %d\n", ret);
		core->firmware_load_error = ret;
		return ret;
	}

	ret = qcom_scm_mem_protect_video_var(cp_config->cp_start,
					     cp_config->cp_size,
					     cp_config->cp_nonpixel_start,
					     cp_config->cp_nonpixel_size);
	if (ret) {
		dev_err(core->dev, "protect memory failed\n");
		core->firmware_load_error = ret;
		if (READ_ONCE(core->init_only)) {
			/* STOP_RETAIN: secure/DMA state is unknown; never roll back here. */
			core->firmware_shutdown_verified = false;
			return ret;
		}
		/* Preserve both errors; no queue cleanup follows this helper's failure. */
		iris_fw_unload(core);
		return ret;
	}

	return ret;
}

int iris_fw_unload(struct iris_core *core)
{
	int ret;

	core->firmware_shutdown_verified = false;
	ret = qcom_scm_pas_shutdown(core->iris_platform_data->pas_id);
	core->firmware_shutdown_error = ret;
	if (!ret) {
		core->core_ready = false;
		core->firmware_booted = false;
		core->firmware_may_run = false;
		core->firmware_shutdown_verified = true;
	}
	return ret;
}

int iris_set_hw_state(struct iris_core *core, bool resume)
{
	return qcom_scm_set_remote_state(resume, 0);
}
