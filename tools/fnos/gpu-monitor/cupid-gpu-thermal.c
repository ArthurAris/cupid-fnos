// SPDX-License-Identifier: GPL-2.0-only
/* Alias real TSENS readings for fnOS; do not own trips, cooling or GPU PM. */
#include <linux/device.h>
#include <linux/err.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/platform_device.h>
#include <linux/string.h>
#include <linux/thermal.h>

#ifdef CUPID_GPU_TEMP_VM
#define PROVIDER_NAME "cupid-tsens-fixture"
#define PROVIDER_DRIVER "cupid-tsens-fixture"
static const char *const source_names[] = { "cupid-gpu-top", "cupid-gpu-bottom" };
#else
#define PROVIDER_NAME "c263000.thermal-sensor"
#define PROVIDER_DRIVER "qcom-tsens"
static const char *const source_names[] = { "gpu-top-thermal", "gpu-bottom-thermal" };
#endif
#define BRIDGE_NAME "gpuss-cupid"

static struct device *provider;
static struct device_driver *source_driver;
static struct thermal_zone_device *sources[2];
static struct thermal_zone_device *bridge;
static bool invalidated;

static int match_provider(struct device *device, const void *name)
{
	return !strcmp(dev_name(device), name);
}

static int verify_source_nodes(void)
{
#ifndef CUPID_GPU_TEMP_VM
	unsigned int i;

	if (!of_machine_is_compatible("xiaomi,cupid") || !provider->of_node ||
	    !of_device_is_compatible(provider->of_node, "qcom,sm8450-tsens"))
		return -ENODEV;
	for (i = 0; i < ARRAY_SIZE(source_names); i++) {
		struct of_phandle_args args;
		struct device_node *node;
		char path[96];
		int error;

		snprintf(path, sizeof(path), "/thermal-zones/%s", source_names[i]);
		node = of_find_node_by_path(path);
		if (!node)
			return -ENODEV;
		if (!of_device_is_available(node)) {
			of_node_put(node);
			return -ENODEV;
		}
		error = of_parse_phandle_with_args(node, "thermal-sensors",
						  "#thermal-sensor-cells", 0, &args);
		of_node_put(node);
		if (error)
			return error;
		error = args.np == provider->of_node && args.args_count == 1 &&
			args.args[0] == 14 + i ? 0 : -ENODEV;
		of_node_put(args.np);
		if (error)
			return error;
	}
#endif
	return 0;
}

static int provider_event(struct notifier_block *notifier,
			  unsigned long event, void *data)
{
	if (data == provider && (event == BUS_NOTIFY_UNBIND_DRIVER ||
	    event == BUS_NOTIFY_UNBOUND_DRIVER || event == BUS_NOTIFY_DEL_DEVICE))
		WRITE_ONCE(invalidated, true);
	return NOTIFY_DONE;
}

static struct notifier_block provider_notifier = { .notifier_call = provider_event };

static int gpu_temperature(struct thermal_zone_device *zone, int *temperature)
{
	bool valid = false;
	int highest = 0, error = -ENODATA;
	unsigned int i;

	/* This callback holds the virtual zone lock. Never block on the provider. */
	if (!device_trylock(provider))
		return -EAGAIN;
	if (READ_ONCE(invalidated) || provider->driver != source_driver) {
		error = -ENODEV;
		goto unlock;
	}
	for (i = 0; i < ARRAY_SIZE(sources); i++) {
		int value, result = thermal_zone_get_temp(sources[i], &value);

		if (result) {
			error = result;
			continue;
		}
		if (!valid || value > highest)
			highest = value;
		valid = true;
	}
	if (valid) {
		*temperature = highest;
		error = 0;
	}
 unlock:
	device_unlock(provider);
	return error;
}

static const struct thermal_zone_device_ops bridge_ops = { .get_temp = gpu_temperature };
static const struct thermal_zone_params bridge_params = { .no_hwmon = true };

static int __init gpu_thermal_init(void)
{
	struct thermal_zone_device *existing;
	unsigned int i;
	int error;

	provider = bus_find_device(&platform_bus_type, NULL, PROVIDER_NAME, match_provider);
	if (!provider)
		return -ENODEV;
	error = bus_register_notifier(&platform_bus_type, &provider_notifier);
	if (error)
		goto put_provider;
	device_lock(provider);
	if (READ_ONCE(invalidated) || !provider->driver ||
	    strcmp(provider->driver->name, PROVIDER_DRIVER)) {
		error = -ENODEV;
		goto unlock;
	}
	error = verify_source_nodes();
	if (error)
		goto unlock;
	source_driver = provider->driver;
	/* TSENS registers/releases these zones under the provider device lock. */
	for (i = 0; i < ARRAY_SIZE(sources); i++) {
		sources[i] = thermal_zone_get_zone_by_name(source_names[i]);
		if (IS_ERR(sources[i])) {
			error = PTR_ERR(sources[i]);
			sources[i] = NULL;
			goto unlock;
		}
	}
	existing = thermal_zone_get_zone_by_name(BRIDGE_NAME);
	if (!IS_ERR(existing) || PTR_ERR(existing) != -ENODEV) {
		error = -EEXIST;
		goto unlock;
	}
	device_unlock(provider);
	/* No trips, cooling bindings or polling; temp reads call the existing driver. */
	bridge = thermal_tripless_zone_device_register(BRIDGE_NAME, NULL, &bridge_ops, &bridge_params);
	if (IS_ERR(bridge)) {
		error = PTR_ERR(bridge);
		bridge = NULL;
		goto unregister_notifier;
	}
	pr_info("cupid-gpu-thermal: read-only gpuss-cupid ready; maximum of real GPU TSENS zones\n");
	return 0;

 unlock:
	device_unlock(provider);
 unregister_notifier:
	WRITE_ONCE(invalidated, true);
	bus_unregister_notifier(&platform_bus_type, &provider_notifier);
 put_provider:
	put_device(provider);
	provider = NULL;
	return error;
}

static void __exit gpu_thermal_exit(void)
{
	WRITE_ONCE(invalidated, true);
	/* Unregister and drain readers without holding a provider lock. */
	thermal_zone_device_unregister(bridge);
	bus_unregister_notifier(&platform_bus_type, &provider_notifier);
	put_device(provider);
	pr_info("cupid-gpu-thermal: read-only temperature alias removed\n");
}

module_init(gpu_thermal_init);
module_exit(gpu_thermal_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("Read-only TSENS GPU temperature compatibility zone for fnOS");
