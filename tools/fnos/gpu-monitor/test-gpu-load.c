/* SPDX-License-Identifier: GPL-2.0-only */
/* Headless, bounded transfer/fence/readback test. No shaders, WSI or disk data. */
#define _GNU_SOURCE
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <time.h>
#include <vulkan/vulkan.h>

#define BUFFER_BYTES (4U * 1024U * 1024U)
#define FILLS_PER_BATCH 16U
#define LOAD_SECONDS 6U
#define FILL_WORD 0x6d3ac751U
#define FENCE_TIMEOUT_NS 3000000000ULL
#define MAX_DEVICES 16U
#define MAX_QUEUES 64U

static void vk_checked(VkResult result, const char *operation)
{
	if (result != VK_SUCCESS) {
		fprintf(stderr, "GPU_SUBMIT_FAIL operation=%s vk_result=%d\n", operation, result);
		fflush(stderr);
		/* Do not destroy a possibly hung device or wait indefinitely on failure. */
		_exit(1);
	}
}

static uint64_t monotonic_ns(void)
{
	struct timespec now;

	if (clock_gettime(CLOCK_MONOTONIC, &now))
		_exit(1);
	return (uint64_t)now.tv_sec * 1000000000ULL + now.tv_nsec;
}

int main(int argc, char **argv)
{
	VkInstance instance;
	VkPhysicalDevice physical = VK_NULL_HANDLE;
	VkDevice device;
	VkQueue queue;
	VkBuffer buffer;
	VkDeviceMemory memory;
	VkCommandPool pool;
	VkCommandBuffer command;
	VkFence fence;
	uint32_t device_count = MAX_DEVICES, queue_index = UINT32_MAX;
	VkPhysicalDevice devices[MAX_DEVICES];
	VkPhysicalDeviceProperties selected = {0};
	VkPhysicalDeviceMemoryProperties memory_properties;
	VkMemoryRequirements requirements;
	uint32_t memory_type = UINT32_MAX;
	VkMemoryPropertyFlags memory_flags = 0;
	void *mapped = NULL;
	bool software_rehearsal = false;

	setvbuf(stdout, NULL, _IOLBF, 0);
	if (argc == 2 && !strcmp(argv[1], "--software-rehearsal"))
		software_rehearsal = true;
	else if (argc != 1) {
		fprintf(stderr, "GPU_SUBMIT_FAIL invalid arguments\n");
		return 2;
	}
	printf("GPU_SUBMIT_BEGIN uid=%ld gid=%ld fence_timeout_ns=%llu mode=%s\n",
	       (long)getuid(), (long)getgid(), (unsigned long long)FENCE_TIMEOUT_NS,
	       software_rehearsal ? "SOFTWARE_REHEARSAL_NOT_PHONE_GPU" : "HARDWARE_ADRENO_REQUIRED");
	VkApplicationInfo application = {
		.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO,
		.pApplicationName = "cupid-headless-transfer-check",
		.apiVersion = VK_API_VERSION_1_0,
	};
	VkInstanceCreateInfo create_instance = {
		.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO,
		.pApplicationInfo = &application,
	};
	vk_checked(vkCreateInstance(&create_instance, NULL, &instance), "create-instance");
	vk_checked(vkEnumeratePhysicalDevices(instance, &device_count, devices), "enumerate-devices");
	if (!device_count || device_count > MAX_DEVICES) {
		fprintf(stderr, "GPU_SUBMIT_FAIL device count=%u\n", device_count);
		return 1;
	}
	for (uint32_t i = 0; i < device_count; i++) {
		VkPhysicalDeviceProperties properties;
		vkGetPhysicalDeviceProperties(devices[i], &properties);
		bool adreno = properties.vendorID == 0x5143 &&
			(properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU ||
			 properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU) &&
			strstr(properties.deviceName, "730");
		bool software = software_rehearsal && properties.deviceType == VK_PHYSICAL_DEVICE_TYPE_CPU;
		if (adreno || software) {
			if (physical != VK_NULL_HANDLE) {
				fprintf(stderr, "GPU_SUBMIT_FAIL ambiguous eligible devices\n");
				return 1;
			}
			physical = devices[i];
			selected = properties;
		}
	}
	if (physical == VK_NULL_HANDLE) {
		fprintf(stderr, "GPU_SUBMIT_FAIL no eligible device; software fallback rejected\n");
		return 1;
	}
	printf("GPU_SUBMIT_DEVICE name=%s vendor=%#x device=%#x type=%d\n",
	       selected.deviceName, selected.vendorID, selected.deviceID, selected.deviceType);
	uint32_t queue_count = MAX_QUEUES;
	VkQueueFamilyProperties queues[MAX_QUEUES];
	vkGetPhysicalDeviceQueueFamilyProperties(physical, &queue_count, queues);
	if (queue_count > MAX_QUEUES) {
		fprintf(stderr, "GPU_SUBMIT_FAIL queue count exceeds bound\n");
		return 1;
	}
	for (uint32_t i = 0; i < queue_count; i++) {
		if (queues[i].queueCount && (queues[i].queueFlags & VK_QUEUE_TRANSFER_BIT)) {
			queue_index = i;
			break;
		}
	}
	if (queue_index == UINT32_MAX) {
		fprintf(stderr, "GPU_SUBMIT_FAIL no transfer queue\n");
		return 1;
	}
	float priority = 1.0f;
	VkDeviceQueueCreateInfo create_queue = {
		.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,
		.queueFamilyIndex = queue_index, .queueCount = 1, .pQueuePriorities = &priority,
	};
	VkDeviceCreateInfo create_device = {
		.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,
		.queueCreateInfoCount = 1, .pQueueCreateInfos = &create_queue,
	};
	vk_checked(vkCreateDevice(physical, &create_device, NULL, &device), "create-device");
	vkGetDeviceQueue(device, queue_index, 0, &queue);
	VkBufferCreateInfo create_buffer = {
		.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,
		.size = BUFFER_BYTES,
		.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT,
		.sharingMode = VK_SHARING_MODE_EXCLUSIVE,
	};
	vk_checked(vkCreateBuffer(device, &create_buffer, NULL, &buffer), "create-buffer");
	vkGetBufferMemoryRequirements(device, buffer, &requirements);
	vkGetPhysicalDeviceMemoryProperties(physical, &memory_properties);
	for (uint32_t i = 0; i < memory_properties.memoryTypeCount && i < 32; i++) {
		if ((requirements.memoryTypeBits & (1U << i)) &&
		    (memory_properties.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) {
			memory_type = i;
			memory_flags = memory_properties.memoryTypes[i].propertyFlags;
			break;
		}
	}
	if (memory_type == UINT32_MAX || requirements.size < BUFFER_BYTES) {
		fprintf(stderr, "GPU_SUBMIT_FAIL host-visible allocation unavailable\n");
		return 1;
	}
	VkMemoryAllocateInfo allocate = {
		.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,
		.allocationSize = requirements.size, .memoryTypeIndex = memory_type,
	};
	vk_checked(vkAllocateMemory(device, &allocate, NULL, &memory), "allocate-memory");
	vk_checked(vkBindBufferMemory(device, buffer, memory, 0), "bind-buffer");
	vk_checked(vkMapMemory(device, memory, 0, VK_WHOLE_SIZE, 0, &mapped), "map-memory");
	memset(mapped, 0, BUFFER_BYTES);
	VkMappedMemoryRange range = {
		.sType = VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,
		.memory = memory, .offset = 0, .size = VK_WHOLE_SIZE,
	};
	if (!(memory_flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))
		vk_checked(vkFlushMappedMemoryRanges(device, 1, &range), "flush-host-zeroes");
	VkCommandPoolCreateInfo create_pool = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,
		.queueFamilyIndex = queue_index,
	};
	vk_checked(vkCreateCommandPool(device, &create_pool, NULL, &pool), "create-command-pool");
	VkCommandBufferAllocateInfo allocate_command = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,
		.commandPool = pool, .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1,
	};
	vk_checked(vkAllocateCommandBuffers(device, &allocate_command, &command), "allocate-command");
	VkCommandBufferBeginInfo begin = {
		.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
		.flags = 0,
	};
	vk_checked(vkBeginCommandBuffer(command, &begin), "begin-command");
	for (uint32_t i = 0; i < FILLS_PER_BATCH; i++) {
		vkCmdFillBuffer(command, buffer, 0, BUFFER_BYTES, FILL_WORD);
		VkBufferMemoryBarrier next_fill = {
			.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
			.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
			.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT,
			.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
			.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
			.buffer = buffer, .offset = 0, .size = BUFFER_BYTES,
		};
		vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT,
				     VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, NULL, 1, &next_fill, 0, NULL);
	}
	VkBufferMemoryBarrier barrier = {
		.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,
		.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT, .dstAccessMask = VK_ACCESS_HOST_READ_BIT,
		.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED, .dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED,
		.buffer = buffer, .offset = 0, .size = BUFFER_BYTES,
	};
	vkCmdPipelineBarrier(command, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_HOST_BIT,
			     0, 0, NULL, 1, &barrier, 0, NULL);
	vk_checked(vkEndCommandBuffer(command), "end-command");
	VkFenceCreateInfo create_fence = {.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};
	vk_checked(vkCreateFence(device, &create_fence, NULL, &fence), "create-fence");
	VkSubmitInfo submit = {
		.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO,
		.commandBufferCount = 1, .pCommandBuffers = &command,
	};
	printf("GPU_LOAD_IDLE_BEGIN mono_ns=%llu seconds=2\n", (unsigned long long)monotonic_ns());
	sleep(2);
	uint64_t start = monotonic_ns(), submissions = 0;
	printf("GPU_LOAD_ACTIVE_BEGIN mono_ns=%llu seconds=%u buffer_bytes=%u fills=%u\n",
	       (unsigned long long)start, LOAD_SECONDS, BUFFER_BYTES, FILLS_PER_BATCH);
	do {
		vk_checked(vkResetFences(device, 1, &fence), "reset-fence");
		vk_checked(vkQueueSubmit(queue, 1, &submit, fence), "queue-submit");
		vk_checked(vkWaitForFences(device, 1, &fence, VK_TRUE, FENCE_TIMEOUT_NS), "bounded-fence-wait");
		submissions++;
	} while (monotonic_ns() - start < LOAD_SECONDS * 1000000000ULL && submissions < 100000);
	printf("GPU_LOAD_ACTIVE_END mono_ns=%llu submissions=%llu\n",
	       (unsigned long long)monotonic_ns(), (unsigned long long)submissions);
	printf("GPU_SUBMIT_FENCE_SIGNALED\n");
	if (!(memory_flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT))
		vk_checked(vkInvalidateMappedMemoryRanges(device, 1, &range), "invalidate-readback");
	const uint32_t *words = mapped;
	for (uint32_t i = 0; i < BUFFER_BYTES / sizeof(*words); i++) {
		if (words[i] != FILL_WORD) {
			fprintf(stderr, "GPU_SUBMIT_FAIL readback index=%u actual=%#x expected=%#x\n",
				i, words[i], FILL_WORD);
			_exit(1);
		}
	}
	printf("GPU_SUBMIT_READBACK_MATCH words=%u\n", BUFFER_BYTES / 4);
	printf("GPU_LOAD_IDLE_AFTER mono_ns=%llu seconds=2\n", (unsigned long long)monotonic_ns());
	sleep(2);
	vkUnmapMemory(device, memory);
	vkDestroyFence(device, fence, NULL);
	vkDestroyCommandPool(device, pool, NULL);
	vkDestroyBuffer(device, buffer, NULL);
	vkFreeMemory(device, memory, NULL);
	vkDestroyDevice(device, NULL);
	vkDestroyInstance(instance, NULL);
	printf("%s NO_DISPLAY_NO_VIDEO\n", software_rehearsal ?
	       "GPU_SUBMIT_SOFTWARE_REHEARSAL_OK NOT_PHONE_GPU_ACCEPTANCE" :
	       "GPU_SUBMIT_HARDWARE_TRANSFER_FENCE_READBACK_OK NOT_DISPLAY_NOT_VIDEO");
	return 0;
}
