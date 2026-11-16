// SPDX-License-Identifier: GPL-2.0-only
/* Observe completed MSM samples; never request a new sample or touch MMIO. */
#include <linux/module.h>
#include <linux/platform_device.h>
#include <linux/device.h>
#include <linux/kprobes.h>
#include <linux/devfreq.h>
#include <linux/ktime.h>
#include <linux/uaccess.h>
#include <linux/spinlock.h>
#include <linux/string.h>
#include "load-sample.h"

#define SAMPLE_MAX_AGE_NS (500ULL * NSEC_PER_MSEC)

static char *gpu_name;
module_param(gpu_name, charp, 0400);
MODULE_PARM_DESC(gpu_name, "Exact verified platform GPU device name");

static struct device *gpu_device;
static struct device *devfreq_device;
static struct module *gpu_owner;
static DEFINE_SPINLOCK(sample_lock);
static struct cupid_gpu_load latest;
static bool accepting;

struct sample_call {
	struct devfreq_dev_status *output;
	u64 generation;
};

static void invalidate_sample(bool disable)
{
	unsigned long flags;

	spin_lock_irqsave(&sample_lock, flags);
	latest.valid = 0;
	latest.generation++;
	if (disable)
		accepting = false;
	spin_unlock_irqrestore(&sample_lock, flags);
}

static int observe_enter(struct kretprobe_instance *instance, struct pt_regs *regs)
{
	struct sample_call *call = (struct sample_call *)instance->data;
	struct device *device = (void *)regs_get_kernel_argument(regs, 0);
	unsigned long flags;
	bool enabled;

	if (device != gpu_device)
		return 1;
	spin_lock_irqsave(&sample_lock, flags);
	enabled = accepting;
	call->generation = latest.generation;
	spin_unlock_irqrestore(&sample_lock, flags);
	call->output = (void *)regs_get_kernel_argument(regs, 1);
	return !enabled;
}
NOKPROBE_SYMBOL(observe_enter);

static int observe_return(struct kretprobe_instance *instance, struct pt_regs *regs)
{
	struct sample_call *call = (struct sample_call *)instance->data;
	struct devfreq_dev_status status;
	unsigned long flags;
	bool valid;

	valid = (long)regs_return_value(regs) == 0 &&
		!copy_from_kernel_nofault(&status, call->output, sizeof(status)) &&
		status.total_time && status.busy_time <= status.total_time;
	spin_lock_irqsave(&sample_lock, flags);
	if (accepting && call->generation == latest.generation) {
		latest.valid = valid;
		if (valid) {
			latest.busy = status.busy_time;
			latest.total = status.total_time;
			latest.observed_ns = ktime_get_mono_fast_ns();
		}
	}
	spin_unlock_irqrestore(&sample_lock, flags);
	return 0;
}
NOKPROBE_SYMBOL(observe_return);

static struct kretprobe sampling_probe = {
	.kp.symbol_name = "msm_devfreq_get_dev_status",
	.entry_handler = observe_enter,
	.handler = observe_return,
	.data_size = sizeof(struct sample_call),
	.maxactive = 32,
};

static void resume_samples(void)
{
	unsigned long flags;

	spin_lock_irqsave(&sample_lock, flags);
	latest.valid = 0;
	latest.generation++;
	accepting = true;
	spin_unlock_irqrestore(&sample_lock, flags);
}

static int observe_suspend(struct kprobe *probe, struct pt_regs *regs)
{
	struct devfreq *devfreq = (void *)regs_get_kernel_argument(regs, 0);

	if (devfreq && &devfreq->dev == devfreq_device)
		invalidate_sample(true);
	return 0;
}
NOKPROBE_SYMBOL(observe_suspend);

static int observe_resume(struct kprobe *probe, struct pt_regs *regs)
{
	struct devfreq *devfreq = (void *)regs_get_kernel_argument(regs, 0);

	if (devfreq && &devfreq->dev == devfreq_device)
		resume_samples();
	return 0;
}
NOKPROBE_SYMBOL(observe_resume);

static struct kprobe power_probes[] = {
	{ .symbol_name = "devfreq_suspend_device", .pre_handler = observe_suspend },
	{ .symbol_name = "devfreq_resume_device", .pre_handler = observe_resume },
};

static ssize_t gpu_busy_percentage_show(struct device *device,
				       struct device_attribute *attribute, char *buffer)
{
	struct cupid_gpu_load snapshot;
	unsigned long flags;
	int percent;

	spin_lock_irqsave(&sample_lock, flags);
	snapshot = latest;
	spin_unlock_irqrestore(&sample_lock, flags);
	percent = cupid_gpu_load_percent(&snapshot, ktime_get_mono_fast_ns(), SAMPLE_MAX_AGE_NS);
	if (percent < 0)
		return -EAGAIN;
	return sysfs_emit(buffer, "%d\n", percent);
}
static DEVICE_ATTR_RO(gpu_busy_percentage);

static ssize_t sample_status_show(struct device *device,
				 struct device_attribute *attribute, char *buffer)
{
	struct cupid_gpu_load snapshot;
	unsigned long flags;
	u64 now;

	spin_lock_irqsave(&sample_lock, flags);
	snapshot = latest;
	spin_unlock_irqrestore(&sample_lock, flags);
	now = ktime_get_mono_fast_ns();
	return sysfs_emit(buffer, "valid=%d busy=%llu total=%llu age_ns=%llu generation=%llu missed=%d\n",
		cupid_gpu_load_percent(&snapshot, now, SAMPLE_MAX_AGE_NS) >= 0,
		snapshot.busy, snapshot.total,
		now >= snapshot.observed_ns ? now - snapshot.observed_ns : U64_MAX,
		snapshot.generation, READ_ONCE(sampling_probe.nmissed));
}
static DEVICE_ATTR_RO(sample_status);

static struct attribute *compat_attributes[] = {
	&dev_attr_gpu_busy_percentage.attr,
	&dev_attr_sample_status.attr,
	NULL,
};
static const struct attribute_group compat_group = { .attrs = compat_attributes };

static int match_gpu(struct device *device, const void *name)
{
	return !strcmp(dev_name(device), name);
}

static int match_devfreq(struct device *device, const void *unused)
{
	return device->parent == gpu_device && device->class &&
		!strcmp(device->class->name, "devfreq") &&
		!strcmp(dev_name(device), gpu_name);
}

static int device_event(struct notifier_block *notifier, unsigned long event, void *data)
{
	if (data == gpu_device && (event == BUS_NOTIFY_UNBIND_DRIVER ||
	    event == BUS_NOTIFY_DEL_DEVICE || event == BUS_NOTIFY_UNBOUND_DRIVER))
		invalidate_sample(true);
	return NOTIFY_DONE;
}
static struct notifier_block device_notifier = { .notifier_call = device_event };

static int __init compat_init(void)
{
	int error;

	if (!gpu_name || !*gpu_name)
		return -EINVAL;
	gpu_device = bus_find_device(&platform_bus_type, NULL, gpu_name, match_gpu);
	if (!gpu_device)
		return -ENODEV;
	device_lock(gpu_device);
	if (!gpu_device->driver || strcmp(gpu_device->driver->name, "adreno")) {
		error = -ENODEV;
		goto unlock_gpu;
	}
	gpu_owner = gpu_device->driver->owner;
	if (!gpu_owner || !try_module_get(gpu_owner)) {
		error = -ENODEV;
		gpu_owner = NULL;
		goto unlock_gpu;
	}
	devfreq_device = device_find_child(gpu_device, NULL, match_devfreq);
	if (!devfreq_device) {
		error = -ENODEV;
		goto put_owner;
	}
	error = bus_register_notifier(&platform_bus_type, &device_notifier);
	if (error)
		goto put_child;
	accepting = true;
	error = register_kretprobe(&sampling_probe);
	if (error)
		goto remove_notifier;
	error = register_kprobes((struct kprobe *[]){ &power_probes[0], &power_probes[1] }, 2);
	if (error)
		goto remove_probe;
	error = sysfs_create_group(&devfreq_device->kobj, &compat_group);
	if (error)
		goto remove_power_probes;
	device_unlock(gpu_device);
	pr_info("cupid-gpu-monitor: RAM compatibility reader ready; source=completed-msm-samples\n");
	return 0;

remove_power_probes:
	unregister_kprobes((struct kprobe *[]){ &power_probes[0], &power_probes[1] }, 2);
remove_probe:
	unregister_kretprobe(&sampling_probe);
remove_notifier:
	invalidate_sample(true);
	bus_unregister_notifier(&platform_bus_type, &device_notifier);
put_child:
	put_device(devfreq_device);
	devfreq_device = NULL;
put_owner:
	module_put(gpu_owner);
	gpu_owner = NULL;
unlock_gpu:
	device_unlock(gpu_device);
	put_device(gpu_device);
	gpu_device = NULL;
	return error;
}

static void __exit compat_exit(void)
{
	invalidate_sample(true);
	sysfs_remove_group(&devfreq_device->kobj, &compat_group);
	unregister_kprobes((struct kprobe *[]){ &power_probes[0], &power_probes[1] }, 2);
	unregister_kretprobe(&sampling_probe);
	bus_unregister_notifier(&platform_bus_type, &device_notifier);
	put_device(devfreq_device);
	module_put(gpu_owner);
	put_device(gpu_device);
	pr_info("cupid-gpu-monitor: compatibility reader removed\n");
}

module_init(compat_init);
module_exit(compat_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("RAM-only prototype observing existing MSM utilization samples");
