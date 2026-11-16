/* SPDX-License-Identifier: GPL-2.0-only */
/* One bounded compute dispatch; no WSI, storage data or CPU fallback. */
#define _GNU_SOURCE
#include <errno.h>
#include <signal.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <vulkan/vulkan.h>
#include "oracle.h"

static void failed(const char *operation)
{
	fprintf(stderr, "GPU_COMPUTE_FAIL operation=%s\n", operation);
	_exit(1);
}
static void checked(VkResult result, const char *operation)
{
	if (result != VK_SUCCESS) failed(operation);
}
static void expired(int signal_number)
{
	(void)signal_number;
	static const char message[] = "GPU_COMPUTE_FAIL process-deadline\n";
	ssize_t written = write(STDERR_FILENO, message, sizeof(message) - 1);
	(void)written;
	_exit(124);
}
static uint32_t *shader(const char *path, size_t *size)
{
	FILE *file = fopen(path, "rb");
	if (!file) failed("shader-open");
	if (fseek(file, 0, SEEK_END)) failed("shader-size");
	long length = ftell(file);
	if (length < 20 || length > 65536 || length % 4) failed("shader-length");
	rewind(file);
	uint32_t *data = malloc((size_t)length);
	if (!data || fread(data, 1, (size_t)length, file) != (size_t)length) failed("shader-read");
	fclose(file);
	if (data[0] != 0x07230203U || data[4] != 0) failed("spirv-header");
	*size = (size_t)length;
	return data;
}
struct storage { VkBuffer buffer; VkDeviceMemory memory; uint32_t *mapped; VkMemoryPropertyFlags flags; };
int main(int argc, char **argv)
{
	bool software = false;
	const char *shader_path = NULL;
	if (argc == 2) shader_path = argv[1];
	else if (argc == 3 && !strcmp(argv[1], "--software-rehearsal")) { software = true; shader_path = argv[2]; }
	else { fprintf(stderr, "Usage: test-gpu-compute [--software-rehearsal] <verified-add.spv>\n"); return 2; }
	setvbuf(stdout, NULL, _IOLBF, 0);
	struct sigaction action = { .sa_handler = expired };
	sigemptyset(&action.sa_mask);
	if (sigaction(SIGALRM, &action, NULL)) failed("deadline-handler");
	alarm(20);
	size_t code_size;
	uint32_t *code = shader(shader_path, &code_size);
	printf("GPU_COMPUTE_BEGIN uid=%ld mode=%s\n", (long)getuid(), software ? "SOFTWARE_NOT_HARDWARE_ACCEPTANCE" : "ADRENO730_REQUIRED");
	VkApplicationInfo app = { .sType=VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName="cupid-compute-check", .apiVersion=VK_API_VERSION_1_0 };
	VkInstanceCreateInfo instance_info = { .sType=VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo=&app };
	VkInstance instance;
	checked(vkCreateInstance(&instance_info,NULL,&instance),"create-instance");
	uint32_t count = 0;
	checked(vkEnumeratePhysicalDevices(instance,&count,NULL),"device-count");
	if (!count || count > 16) failed("device-count-bound");
	VkPhysicalDevice devices[16], physical = VK_NULL_HANDLE;
	checked(vkEnumeratePhysicalDevices(instance,&count,devices),"devices");
	VkPhysicalDeviceProperties properties = {0};
	for (uint32_t i=0;i<count;i++) {
		VkPhysicalDeviceProperties p;
		vkGetPhysicalDeviceProperties(devices[i],&p);
		bool eligible = software ? p.deviceType==VK_PHYSICAL_DEVICE_TYPE_CPU :
			p.vendorID==0x5143 && strstr(p.deviceName,"730") &&
			(p.deviceType==VK_PHYSICAL_DEVICE_TYPE_INTEGRATED_GPU || p.deviceType==VK_PHYSICAL_DEVICE_TYPE_DISCRETE_GPU);
		if (!eligible) continue;
		if (physical) failed("ambiguous-devices");
		physical=devices[i]; properties=p;
	}
	if (!physical) failed("no-eligible-device-no-fallback");
	printf("GPU_COMPUTE_DEVICE vendor=%#x name=%s\n",properties.vendorID,properties.deviceName);
	if (properties.limits.maxComputeWorkGroupInvocations < 64 || properties.limits.maxComputeWorkGroupSize[0]<64 || properties.limits.maxComputeWorkGroupCount[0]<4 || properties.limits.maxStorageBufferRange < STORAGE_WORDS*4) failed("compute-limits");
	uint32_t queues=0, queue_index=UINT32_MAX;
	vkGetPhysicalDeviceQueueFamilyProperties(physical,&queues,NULL);
	if (!queues || queues>64) failed("queue-bound");
	VkQueueFamilyProperties families[64];
	vkGetPhysicalDeviceQueueFamilyProperties(physical,&queues,families);
	for (uint32_t i=0;i<queues;i++) if (families[i].queueCount && (families[i].queueFlags & VK_QUEUE_COMPUTE_BIT)) {queue_index=i;break;}
	if (queue_index==UINT32_MAX) failed("no-compute-queue");
	float priority=1.0f;
	VkDeviceQueueCreateInfo queue_info={.sType=VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO,.queueFamilyIndex=queue_index,.queueCount=1,.pQueuePriorities=&priority};
	VkDeviceCreateInfo device_info={.sType=VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO,.queueCreateInfoCount=1,.pQueueCreateInfos=&queue_info};
	VkDevice device;
	checked(vkCreateDevice(physical,&device_info,NULL,&device),"create-device");
	VkQueue queue; vkGetDeviceQueue(device,queue_index,0,&queue);
	VkPhysicalDeviceMemoryProperties memory_properties;
	vkGetPhysicalDeviceMemoryProperties(physical,&memory_properties);
	struct storage buffers[3]={0};
	for (uint32_t n=0;n<3;n++) {
		VkBufferCreateInfo info={.sType=VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO,.size=STORAGE_WORDS*4,.usage=VK_BUFFER_USAGE_STORAGE_BUFFER_BIT,.sharingMode=VK_SHARING_MODE_EXCLUSIVE};
		checked(vkCreateBuffer(device,&info,NULL,&buffers[n].buffer),"create-buffer");
		VkMemoryRequirements requirements; vkGetBufferMemoryRequirements(device,buffers[n].buffer,&requirements);
		uint32_t index=UINT32_MAX;
		for (uint32_t i=0;i<memory_properties.memoryTypeCount;i++) if ((requirements.memoryTypeBits & (1U<<i)) && (memory_properties.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT)) {index=i;break;}
		if (index==UINT32_MAX || requirements.size < STORAGE_WORDS*4) failed("host-memory");
		buffers[n].flags=memory_properties.memoryTypes[index].propertyFlags;
		VkMemoryAllocateInfo allocation={.sType=VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO,.allocationSize=requirements.size,.memoryTypeIndex=index};
		checked(vkAllocateMemory(device,&allocation,NULL,&buffers[n].memory),"allocate");
		checked(vkBindBufferMemory(device,buffers[n].buffer,buffers[n].memory,0),"bind");
		checked(vkMapMemory(device,buffers[n].memory,0,VK_WHOLE_SIZE,0,(void**)&buffers[n].mapped),"map");
		for (uint32_t i=0;i<STORAGE_WORDS;i++) buffers[n].mapped[i]=n==0 ? input_a(i) : n==1 ? input_b(i) : SENTINEL;
		if (!(buffers[n].flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) {
			VkMappedMemoryRange range={.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,.memory=buffers[n].memory,.size=VK_WHOLE_SIZE};
			checked(vkFlushMappedMemoryRanges(device,1,&range),"flush");
		}
	}
	VkDescriptorSetLayoutBinding bindings[3]={0};
	for (uint32_t i=0;i<3;i++) bindings[i]=(VkDescriptorSetLayoutBinding){.binding=i,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=1,.stageFlags=VK_SHADER_STAGE_COMPUTE_BIT};
	VkDescriptorSetLayoutCreateInfo layout_info={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,.bindingCount=3,.pBindings=bindings};
	VkDescriptorSetLayout layout;checked(vkCreateDescriptorSetLayout(device,&layout_info,NULL,&layout),"descriptor-layout");
	VkDescriptorPoolSize pool_size={.type=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.descriptorCount=3};
	VkDescriptorPoolCreateInfo pool_info={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO,.maxSets=1,.poolSizeCount=1,.pPoolSizes=&pool_size};
	VkDescriptorPool descriptor_pool;checked(vkCreateDescriptorPool(device,&pool_info,NULL,&descriptor_pool),"descriptor-pool");
	VkDescriptorSetAllocateInfo set_info={.sType=VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO,.descriptorPool=descriptor_pool,.descriptorSetCount=1,.pSetLayouts=&layout};
	VkDescriptorSet set;checked(vkAllocateDescriptorSets(device,&set_info,&set),"descriptor-set");
	VkDescriptorBufferInfo buffer_infos[3]; VkWriteDescriptorSet writes[3];
	for (uint32_t i=0;i<3;i++) {
		buffer_infos[i]=(VkDescriptorBufferInfo){.buffer=buffers[i].buffer,.range=STORAGE_WORDS*4};
		writes[i]=(VkWriteDescriptorSet){.sType=VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET,.dstSet=set,.dstBinding=i,.descriptorCount=1,.descriptorType=VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,.pBufferInfo=&buffer_infos[i]};
	}
	vkUpdateDescriptorSets(device,3,writes,0,NULL);
	VkPipelineLayoutCreateInfo pipeline_layout_info={.sType=VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO,.setLayoutCount=1,.pSetLayouts=&layout};
	VkPipelineLayout pipeline_layout;checked(vkCreatePipelineLayout(device,&pipeline_layout_info,NULL,&pipeline_layout),"pipeline-layout");
	VkShaderModuleCreateInfo shader_info={.sType=VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,.codeSize=code_size,.pCode=code};
	VkShaderModule module;checked(vkCreateShaderModule(device,&shader_info,NULL,&module),"shader-module");
	VkComputePipelineCreateInfo pipeline_info={.sType=VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,.stage={.sType=VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO,.stage=VK_SHADER_STAGE_COMPUTE_BIT,.module=module,.pName="main"},.layout=pipeline_layout};
	VkPipeline pipeline;checked(vkCreateComputePipelines(device,VK_NULL_HANDLE,1,&pipeline_info,NULL,&pipeline),"compute-pipeline");
	VkCommandPoolCreateInfo command_pool_info={.sType=VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO,.queueFamilyIndex=queue_index};
	VkCommandPool command_pool;checked(vkCreateCommandPool(device,&command_pool_info,NULL,&command_pool),"command-pool");
	VkCommandBufferAllocateInfo command_info={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO,.commandPool=command_pool,.level=VK_COMMAND_BUFFER_LEVEL_PRIMARY,.commandBufferCount=1};
	VkCommandBuffer command;checked(vkAllocateCommandBuffers(device,&command_info,&command),"command");
	VkCommandBufferBeginInfo begin={.sType=VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};checked(vkBeginCommandBuffer(command,&begin),"begin");
	VkBufferMemoryBarrier barriers[3];
	for (uint32_t i=0;i<3;i++) barriers[i]=(VkBufferMemoryBarrier){.sType=VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER,.srcAccessMask=VK_ACCESS_HOST_WRITE_BIT,.dstAccessMask=i<2?VK_ACCESS_SHADER_READ_BIT:VK_ACCESS_SHADER_WRITE_BIT,.srcQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.dstQueueFamilyIndex=VK_QUEUE_FAMILY_IGNORED,.buffer=buffers[i].buffer,.size=STORAGE_WORDS*4};
	vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_HOST_BIT,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,0,0,NULL,3,barriers,0,NULL);
	vkCmdBindPipeline(command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline);
	vkCmdBindDescriptorSets(command,VK_PIPELINE_BIND_POINT_COMPUTE,pipeline_layout,0,1,&set,0,NULL);
	vkCmdDispatch(command,4,1,1);
	barriers[2].srcAccessMask=VK_ACCESS_SHADER_WRITE_BIT;barriers[2].dstAccessMask=VK_ACCESS_HOST_READ_BIT;
	vkCmdPipelineBarrier(command,VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT,VK_PIPELINE_STAGE_HOST_BIT,0,0,NULL,1,&barriers[2],0,NULL);
	checked(vkEndCommandBuffer(command),"end");
	VkFenceCreateInfo fence_info={.sType=VK_STRUCTURE_TYPE_FENCE_CREATE_INFO};VkFence fence;checked(vkCreateFence(device,&fence_info,NULL,&fence),"fence");
	VkSubmitInfo submit={.sType=VK_STRUCTURE_TYPE_SUBMIT_INFO,.commandBufferCount=1,.pCommandBuffers=&command};
	checked(vkQueueSubmit(queue,1,&submit,fence),"submit");checked(vkWaitForFences(device,1,&fence,VK_TRUE,3000000000ULL),"bounded-fence");
	if (!(buffers[2].flags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) {VkMappedMemoryRange range={.sType=VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE,.memory=buffers[2].memory,.size=VK_WHOLE_SIZE};checked(vkInvalidateMappedMemoryRanges(device,1,&range),"invalidate");}
	int mismatch=check_result(buffers[2].mapped,STORAGE_WORDS);
	if (mismatch) {fprintf(stderr,"GPU_COMPUTE_FAIL output-or-guard index=%d\n",mismatch-1);_exit(1);}
	printf("GPU_COMPUTE_%s_OK elements=%u guard_words=%u NOT_NPU_NOT_VIDEO\n",software?"SOFTWARE_REHEARSAL_NOT_HARDWARE":"HARDWARE_ADRENO_SHADER",ELEMENTS,STORAGE_WORDS-ELEMENTS);
	vkDestroyFence(device,fence,NULL);vkDestroyCommandPool(device,command_pool,NULL);vkDestroyPipeline(device,pipeline,NULL);vkDestroyShaderModule(device,module,NULL);vkDestroyPipelineLayout(device,pipeline_layout,NULL);vkDestroyDescriptorPool(device,descriptor_pool,NULL);vkDestroyDescriptorSetLayout(device,layout,NULL);
	for (uint32_t i=0;i<3;i++){vkUnmapMemory(device,buffers[i].memory);vkDestroyBuffer(device,buffers[i].buffer,NULL);vkFreeMemory(device,buffers[i].memory,NULL);}
	vkDestroyDevice(device,NULL);vkDestroyInstance(instance,NULL);free(code);alarm(0);return 0;
}
