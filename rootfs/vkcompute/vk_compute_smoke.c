/* SPDX-License-Identifier: GPL-2.0-only */
/* vk-compute-smoke: the smallest headless Vulkan compute check for Turnip on the Quest 2.
 *
 * One dispatch over N uint32 (default 65536, max 4194304) of smoke.comp, a bounded fence wait
 * (default 5000 ms, max 10000 ms), then a CPU check of every element. No WSI, no display, no
 * swapchain. By default it refuses any device that is not Qualcomm (vendorID 0x5143), so a CPU
 * fallback (lavapipe) can never pass as a GPU result; --any-device is for host-side testing only.
 * Output: one line per physical device ("VKSMOKE dev ..."), then "VKSMOKE OK ..." (exit 0) or
 * "VKSMOKE FAIL <stage> ..." (exit 1: Vulkan error, 2: no usable device, 3: wrong results,
 * 4: fence timeout, 64: bad arguments). Seconds of GPU work at most: keep it that way (the
 * headset has no thermal headroom for sustained GPU load while running from RAM).
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <vulkan/vulkan.h>
#include "smoke_spv.h"

#define QCOM_VENDOR 0x5143u
#define FAIL(code, ...) do { printf("VKSMOKE FAIL " __VA_ARGS__); printf("\n"); rc = (code); goto out; } while (0)
#define VK(call, stage) do { VkResult r_ = (call); if (r_ != VK_SUCCESS) FAIL(1, "%s VkResult=%d", stage, (int)r_); } while (0)

static uint32_t expect(uint32_t i)
{
	uint32_t x = i * 2654435761u;
	for (int k = 0; k < 16; k++)
		x = (x ^ (x >> 13)) * 1103515245u + 12345u;
	return x;
}

static double now_ms(void)
{
	struct timespec t;
	clock_gettime(CLOCK_MONOTONIC, &t);
	return t.tv_sec * 1e3 + t.tv_nsec / 1e6;
}

int main(int argc, char **argv)
{
	uint32_t n = 65536, timeout_ms = 5000;
	int any = 0, rc = 0;
	for (int a = 1; a < argc; a++) {
		char *end = NULL;
		if (!strcmp(argv[a], "--any-device")) {
			any = 1;
		} else if (!strcmp(argv[a], "--n") && a + 1 < argc) {
			unsigned long v = strtoul(argv[++a], &end, 10);
			if (*end || v < 64 || v > 4194304) { printf("VKSMOKE FAIL args --n 64..4194304\n"); return 64; }
			n = (uint32_t)v;
		} else if (!strcmp(argv[a], "--timeout-ms") && a + 1 < argc) {
			unsigned long v = strtoul(argv[++a], &end, 10);
			if (*end || v < 100 || v > 10000) { printf("VKSMOKE FAIL args --timeout-ms 100..10000\n"); return 64; }
			timeout_ms = (uint32_t)v;
		} else {
			printf("usage: vk-compute-smoke [--n N] [--timeout-ms MS] [--any-device]\n");
			return 64;
		}
	}

	VkInstance inst = VK_NULL_HANDLE;
	VkDevice dev = VK_NULL_HANDLE;
	VkBuffer buf = VK_NULL_HANDLE;
	VkDeviceMemory mem = VK_NULL_HANDLE;
	VkShaderModule sm = VK_NULL_HANDLE;
	VkDescriptorSetLayout dsl = VK_NULL_HANDLE;
	VkPipelineLayout pl = VK_NULL_HANDLE;
	VkPipeline pipe = VK_NULL_HANDLE;
	VkDescriptorPool dp = VK_NULL_HANDLE;
	VkCommandPool cp = VK_NULL_HANDLE;
	VkFence fence = VK_NULL_HANDLE;
	VkPhysicalDevice pds[16], pd = VK_NULL_HANDLE;
	uint32_t npd = 16, qf = UINT32_MAX;
	VkPhysicalDeviceProperties props;
	double t0 = now_ms();

	VkApplicationInfo app = { .sType = VK_STRUCTURE_TYPE_APPLICATION_INFO, .pApplicationName = "vk-compute-smoke",
				  .apiVersion = VK_API_VERSION_1_1 };
	VkInstanceCreateInfo ici = { .sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO, .pApplicationInfo = &app };
	VK(vkCreateInstance(&ici, NULL, &inst), "vkCreateInstance");
	VkResult er = vkEnumeratePhysicalDevices(inst, &npd, pds);
	if (er != VK_SUCCESS && er != VK_INCOMPLETE)
		FAIL(1, "vkEnumeratePhysicalDevices VkResult=%d", (int)er);
	for (uint32_t i = 0; i < npd; i++) {
		VkPhysicalDeviceProperties p;
		vkGetPhysicalDeviceProperties(pds[i], &p);
		printf("VKSMOKE dev %u name=\"%s\" vendor=0x%04x device=0x%08x type=%d api=%u.%u.%u driver=0x%08x\n", i,
		       p.deviceName, p.vendorID, p.deviceID, (int)p.deviceType, VK_API_VERSION_MAJOR(p.apiVersion),
		       VK_API_VERSION_MINOR(p.apiVersion), VK_API_VERSION_PATCH(p.apiVersion), p.driverVersion);
		if (pd == VK_NULL_HANDLE && (any || p.vendorID == QCOM_VENDOR)) {
			uint32_t nq = 0;
			VkQueueFamilyProperties qp[16];
			vkGetPhysicalDeviceQueueFamilyProperties(pds[i], &nq, NULL);
			if (nq > 16)
				nq = 16;
			vkGetPhysicalDeviceQueueFamilyProperties(pds[i], &nq, qp);
			for (uint32_t q = 0; q < nq; q++)
				if (qp[q].queueFlags & VK_QUEUE_COMPUTE_BIT) {
					pd = pds[i];
					qf = q;
					props = p;
					break;
				}
		}
	}
	if (pd == VK_NULL_HANDLE)
		FAIL(2, "device none of %u is %s with a compute queue", npd, any ? "any device" : "Qualcomm (0x5143)");

	float prio = 1.0f;
	VkDeviceQueueCreateInfo qci = { .sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO, .queueFamilyIndex = qf,
					.queueCount = 1, .pQueuePriorities = &prio };
	VkDeviceCreateInfo dci = { .sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO, .queueCreateInfoCount = 1,
				   .pQueueCreateInfos = &qci };
	VK(vkCreateDevice(pd, &dci, NULL, &dev), "vkCreateDevice");
	VkQueue queue;
	vkGetDeviceQueue(dev, qf, 0, &queue);

	VkDeviceSize size = (VkDeviceSize)n * sizeof(uint32_t);
	VkBufferCreateInfo bci = { .sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO, .size = size,
				   .usage = VK_BUFFER_USAGE_STORAGE_BUFFER_BIT, .sharingMode = VK_SHARING_MODE_EXCLUSIVE };
	VK(vkCreateBuffer(dev, &bci, NULL, &buf), "vkCreateBuffer");
	VkMemoryRequirements mr;
	vkGetBufferMemoryRequirements(dev, buf, &mr);
	VkPhysicalDeviceMemoryProperties mp;
	vkGetPhysicalDeviceMemoryProperties(pd, &mp);
	uint32_t mt = UINT32_MAX;
	VkMemoryPropertyFlags want = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT;
	for (uint32_t i = 0; i < mp.memoryTypeCount; i++)
		if ((mr.memoryTypeBits & (1u << i)) && (mp.memoryTypes[i].propertyFlags & want) == want) {
			mt = i;
			break;
		}
	if (mt == UINT32_MAX)
		FAIL(1, "memory no host-visible coherent type");
	VkMemoryAllocateInfo mai = { .sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO, .allocationSize = mr.size,
				     .memoryTypeIndex = mt };
	VK(vkAllocateMemory(dev, &mai, NULL, &mem), "vkAllocateMemory");
	VK(vkBindBufferMemory(dev, buf, mem, 0), "vkBindBufferMemory");
	uint32_t *host = NULL;
	VK(vkMapMemory(dev, mem, 0, size, 0, (void **)&host), "vkMapMemory");
	memset(host, 0xA5, size);

	VkShaderModuleCreateInfo smci = { .sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO,
					  .codeSize = sizeof(smoke_spv), .pCode = smoke_spv };
	VK(vkCreateShaderModule(dev, &smci, NULL, &sm), "vkCreateShaderModule");
	VkDescriptorSetLayoutBinding b0 = { .binding = 0, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
					    .descriptorCount = 1, .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT };
	VkDescriptorSetLayoutCreateInfo dlci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_LAYOUT_CREATE_INFO,
						 .bindingCount = 1, .pBindings = &b0 };
	VK(vkCreateDescriptorSetLayout(dev, &dlci, NULL, &dsl), "vkCreateDescriptorSetLayout");
	VkPushConstantRange pcr = { .stageFlags = VK_SHADER_STAGE_COMPUTE_BIT, .offset = 0, .size = 4 };
	VkPipelineLayoutCreateInfo plci = { .sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO, .setLayoutCount = 1,
					    .pSetLayouts = &dsl, .pushConstantRangeCount = 1, .pPushConstantRanges = &pcr };
	VK(vkCreatePipelineLayout(dev, &plci, NULL, &pl), "vkCreatePipelineLayout");
	VkComputePipelineCreateInfo cpci = {
		.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO,
		.stage = { .sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO, .stage = VK_SHADER_STAGE_COMPUTE_BIT,
			   .module = sm, .pName = "main" },
		.layout = pl };
	VK(vkCreateComputePipelines(dev, VK_NULL_HANDLE, 1, &cpci, NULL, &pipe), "vkCreateComputePipelines");
	VkDescriptorPoolSize dps = { .type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER, .descriptorCount = 1 };
	VkDescriptorPoolCreateInfo dpci = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO, .maxSets = 1,
					    .poolSizeCount = 1, .pPoolSizes = &dps };
	VK(vkCreateDescriptorPool(dev, &dpci, NULL, &dp), "vkCreateDescriptorPool");
	VkDescriptorSet ds;
	VkDescriptorSetAllocateInfo dsai = { .sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO, .descriptorPool = dp,
					     .descriptorSetCount = 1, .pSetLayouts = &dsl };
	VK(vkAllocateDescriptorSets(dev, &dsai, &ds), "vkAllocateDescriptorSets");
	VkDescriptorBufferInfo dbi = { .buffer = buf, .offset = 0, .range = size };
	VkWriteDescriptorSet wds = { .sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET, .dstSet = ds, .dstBinding = 0,
				     .descriptorCount = 1, .descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
				     .pBufferInfo = &dbi };
	vkUpdateDescriptorSets(dev, 1, &wds, 0, NULL);

	VkCommandPoolCreateInfo cpi = { .sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO, .queueFamilyIndex = qf };
	VK(vkCreateCommandPool(dev, &cpi, NULL, &cp), "vkCreateCommandPool");
	VkCommandBuffer cb;
	VkCommandBufferAllocateInfo cbai = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO, .commandPool = cp,
					     .level = VK_COMMAND_BUFFER_LEVEL_PRIMARY, .commandBufferCount = 1 };
	VK(vkAllocateCommandBuffers(dev, &cbai, &cb), "vkAllocateCommandBuffers");
	VkCommandBufferBeginInfo cbbi = { .sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO,
					  .flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT };
	VK(vkBeginCommandBuffer(cb, &cbbi), "vkBeginCommandBuffer");
	vkCmdBindPipeline(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pipe);
	vkCmdBindDescriptorSets(cb, VK_PIPELINE_BIND_POINT_COMPUTE, pl, 0, 1, &ds, 0, NULL);
	vkCmdPushConstants(cb, pl, VK_SHADER_STAGE_COMPUTE_BIT, 0, 4, &n);
	vkCmdDispatch(cb, (n + 63) / 64, 1, 1);
	VkMemoryBarrier mb = { .sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER, .srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT,
			       .dstAccessMask = VK_ACCESS_HOST_READ_BIT };
	vkCmdPipelineBarrier(cb, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_HOST_BIT, 0, 1, &mb, 0, NULL, 0, NULL);
	VK(vkEndCommandBuffer(cb), "vkEndCommandBuffer");
	VkFenceCreateInfo fci = { .sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO };
	VK(vkCreateFence(dev, &fci, NULL, &fence), "vkCreateFence");
	VkSubmitInfo si = { .sType = VK_STRUCTURE_TYPE_SUBMIT_INFO, .commandBufferCount = 1, .pCommandBuffers = &cb };
	double ts = now_ms();
	VK(vkQueueSubmit(queue, 1, &si, fence), "vkQueueSubmit");
	VkResult wr = vkWaitForFences(dev, 1, &fence, VK_TRUE, (uint64_t)timeout_ms * 1000000ull);
	double te = now_ms();
	if (wr == VK_TIMEOUT)
		FAIL(4, "fence timeout after %u ms (device \"%s\")", timeout_ms, props.deviceName);
	if (wr != VK_SUCCESS)
		FAIL(1, "vkWaitForFences VkResult=%d", (int)wr);
	uint32_t bad = 0, first = UINT32_MAX;
	for (uint32_t i = 0; i < n; i++)
		if (host[i] != expect(i)) {
			if (first == UINT32_MAX)
				first = i;
			bad++;
		}
	if (bad)
		FAIL(3, "results %u of %u wrong (first at %u: got 0x%08x want 0x%08x)", bad, n, first, host[first],
		     expect(first));
	printf("VKSMOKE OK n=%u device=\"%s\" vendor=0x%04x gpu_ms=%.2f total_ms=%.1f\n", n, props.deviceName,
	       props.vendorID, te - ts, now_ms() - t0);
out:
	if (dev != VK_NULL_HANDLE) {
		vkDeviceWaitIdle(dev);
		if (fence) vkDestroyFence(dev, fence, NULL);
		if (cp) vkDestroyCommandPool(dev, cp, NULL);
		if (dp) vkDestroyDescriptorPool(dev, dp, NULL);
		if (pipe) vkDestroyPipeline(dev, pipe, NULL);
		if (pl) vkDestroyPipelineLayout(dev, pl, NULL);
		if (dsl) vkDestroyDescriptorSetLayout(dev, dsl, NULL);
		if (sm) vkDestroyShaderModule(dev, sm, NULL);
		if (mem) vkFreeMemory(dev, mem, NULL);
		if (buf) vkDestroyBuffer(dev, buf, NULL);
		vkDestroyDevice(dev, NULL);
	}
	if (inst != VK_NULL_HANDLE)
		vkDestroyInstance(inst, NULL);
	return rc;
}
