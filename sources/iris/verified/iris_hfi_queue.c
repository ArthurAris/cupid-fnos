// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#include <linux/pm_runtime.h>

#include "iris_core.h"
#include "iris_hfi_queue.h"
#include "iris_vpu_common.h"

static int iris_hfi_queue_write(struct iris_iface_q_info *qinfo, void *packet, u32 packet_size)
{
	struct iris_hfi_queue_header *queue = qinfo->qhdr;
	u32 write_idx = queue->write_idx * sizeof(u32);
	u32 read_idx = queue->read_idx * sizeof(u32);
	u32 empty_space, new_write_idx, residue;
	u32 *write_ptr;

	if (write_idx < read_idx)
		empty_space = read_idx - write_idx;
	else
		empty_space = IFACEQ_QUEUE_SIZE - (write_idx -  read_idx);
	if (empty_space < packet_size)
		return -ENOSPC;

	queue->tx_req =  0;

	new_write_idx = write_idx + packet_size;
	write_ptr = (u32 *)((u8 *)qinfo->kernel_vaddr + write_idx);

	if (write_ptr < (u32 *)qinfo->kernel_vaddr ||
	    write_ptr > (u32 *)(qinfo->kernel_vaddr +
	    IFACEQ_QUEUE_SIZE))
		return -EINVAL;

	if (new_write_idx < IFACEQ_QUEUE_SIZE) {
		memcpy(write_ptr, packet, packet_size);
	} else {
		residue = new_write_idx - IFACEQ_QUEUE_SIZE;
		memcpy(write_ptr, packet, (packet_size - residue));
		memcpy(qinfo->kernel_vaddr,
		       packet + (packet_size - residue), residue);
		new_write_idx = residue;
	}

	/* Make sure packet is written before updating the write index */
	mb();
	queue->write_idx = new_write_idx / sizeof(u32);

	/* Make sure write index is updated before an interrupt is raised */
	mb();

	return 0;
}

static int iris_hfi_queue_read(struct iris_iface_q_info *qinfo, void *packet,
			       u32 capacity, u32 *received)
{
	struct iris_hfi_queue_header *queue = qinfo->qhdr;
	u32 write_word = READ_ONCE(queue->write_idx);
	u32 read_word = READ_ONCE(queue->read_idx);
	u32 write_idx, read_idx, available, packet_size, first;

	if (!received || !packet || capacity < sizeof(u32))
		return -EINVAL;
	*received = 0;
	if (write_word >= IFACEQ_QUEUE_SIZE / sizeof(u32) ||
	    read_word >= IFACEQ_QUEUE_SIZE / sizeof(u32))
		return -EBADMSG;
	write_idx = write_word * sizeof(u32);
	read_idx = read_word * sizeof(u32);
	queue->rx_req = queue->queue_type == IFACEQ_MSGQ_ID;
	if (read_idx == write_idx) {
		mb();
		return -ENODATA;
	}
	/* Firmware publishes the write index after writing the record. */
	rmb();
	available = write_idx >= read_idx ? write_idx - read_idx :
		IFACEQ_QUEUE_SIZE - read_idx + write_idx;
	if (available < sizeof(u32))
		return -EBADMSG;
	memcpy(&packet_size, (u8 *)qinfo->kernel_vaddr + read_idx, sizeof(packet_size));
	if (packet_size < sizeof(u32) || packet_size % sizeof(u32) ||
	    packet_size > capacity || packet_size > IFACEQ_CORE_PKT_SIZE ||
	    packet_size > available)
		return -EBADMSG;
	first = min(packet_size, IFACEQ_QUEUE_SIZE - read_idx);
	memcpy(packet, (u8 *)qinfo->kernel_vaddr + read_idx, first);
	if (first != packet_size)
		memcpy((u8 *)packet + first, qinfo->kernel_vaddr, packet_size - first);
	queue->read_idx = ((read_idx + packet_size) % IFACEQ_QUEUE_SIZE) / sizeof(u32);
	*received = packet_size;
	mb();
	return 0;
}

int iris_hfi_queue_cmd_write_locked(struct iris_core *core, void *pkt, u32 pkt_size)
{
	struct iris_iface_q_info *q_info = &core->command_queue;

	if (core->state == IRIS_CORE_ERROR || core->state == IRIS_CORE_DEINIT)
		return -EINVAL;
	if (core->irq_managed && (core->terminal_shutdown || core->irq_quiescing ||
	    core->resources_releasing || core->irq_releasing || !core->hardware_owner_held ||
	    !core->queues_allocated || !core->power_prepared || !core->firmware_booted))
		return -ESHUTDOWN;

	if (!iris_hfi_queue_write(q_info, pkt, pkt_size)) {
		iris_vpu_raise_interrupt(core);
	} else {
		dev_err(core->dev, "queue full\n");
		return -ENODATA;
	}

	return 0;
}

int iris_hfi_queue_cmd_write(struct iris_core *core, void *pkt, u32 pkt_size)
{
	int ret;

	mutex_lock(&core->lock);
	if (core->irq_managed && (core->removing || core->terminal_shutdown ||
	    core->irq_quiescing || core->resources_releasing || core->irq_releasing ||
	    core->state != IRIS_CORE_INIT || !core->core_ready ||
	    !core->hardware_owner_held)) {
		mutex_unlock(&core->lock);
		return -ESHUTDOWN;
	}
	if (core->decode_once) {
		/* Retained run owns power/PM already; commands cannot resume or put it. */
		if (!iris_decode_once_ready_locked(core) ||
		    !core->decode_once_admission || !core->decode_once_claimed ||
		    !pm_runtime_enabled(core->dev) ||
		    core->dev->power.runtime_status != RPM_ACTIVE ||
		    atomic_read(&core->dev->power.usage_count) < 1)
			ret = -ESHUTDOWN;
		else
			ret = iris_hfi_queue_cmd_write_locked(core, pkt, pkt_size);
		mutex_unlock(&core->lock);
		return ret;
	}
	mutex_unlock(&core->lock);
	ret = pm_runtime_resume_and_get(core->dev);
	if (ret < 0)
		return ret;
	mutex_lock(&core->lock);
	if (core->irq_managed && (core->removing || core->terminal_shutdown ||
	    core->irq_quiescing || core->resources_releasing || core->irq_releasing ||
	    core->state != IRIS_CORE_INIT || !core->core_ready ||
	    !core->hardware_owner_held))
		ret = -ESHUTDOWN;
	else
		ret = iris_hfi_queue_cmd_write_locked(core, pkt, pkt_size);
	mutex_unlock(&core->lock);
	if (core->irq_managed)
		iris_core_pm_put(core);
	else if (!ret)
		pm_runtime_put_autosuspend(core->dev);
	else
		iris_core_pm_put(core);
	return ret;
}
int iris_hfi_queue_msg_read_len(struct iris_core *core, void *pkt, u32 capacity, u32 *received)
{
	int ret;

	if (!received)
		return -EINVAL;
	*received = 0;
	mutex_lock(&core->lock);
	if (core->state != IRIS_CORE_INIT)
		ret = -EINVAL;
	else
		ret = iris_hfi_queue_read(&core->message_queue, pkt, capacity, received);
	mutex_unlock(&core->lock);
	return ret;
}

int iris_hfi_queue_msg_read(struct iris_core *core, void *pkt)
{
	u32 received;

	return iris_hfi_queue_msg_read_len(core, pkt, IFACEQ_CORE_PKT_SIZE, &received);
}

int iris_hfi_queue_dbg_read_len(struct iris_core *core, void *pkt, u32 capacity, u32 *received)
{
	int ret;

	if (!received)
		return -EINVAL;
	*received = 0;
	mutex_lock(&core->lock);
	if (core->state != IRIS_CORE_INIT)
		ret = -EINVAL;
	else
		ret = iris_hfi_queue_read(&core->debug_queue, pkt, capacity, received);
	mutex_unlock(&core->lock);
	return ret;
}

int iris_hfi_queue_dbg_read(struct iris_core *core, void *pkt)
{
	u32 received;

	return iris_hfi_queue_dbg_read_len(core, pkt, IFACEQ_CORE_PKT_SIZE, &received);
}

static void iris_hfi_queue_set_header(struct iris_core *core, u32 queue_id,
				      struct iris_iface_q_info *iface_q)
{
	(void)core;
	iface_q->qhdr->status = 0x1;
	iface_q->qhdr->start_addr = iface_q->device_addr;
	iface_q->qhdr->header_type = IFACEQ_DFLT_QHDR;
	iface_q->qhdr->queue_type = queue_id;
	iface_q->qhdr->q_size = IFACEQ_QUEUE_SIZE / sizeof(u32);
	iface_q->qhdr->pkt_size = 0; /* variable packet size */
	iface_q->qhdr->rx_wm = 0x1;
	iface_q->qhdr->tx_wm = 0x1;
	iface_q->qhdr->rx_req = 0x1;
	iface_q->qhdr->tx_req = 0x0;
	iface_q->qhdr->rx_irq_status = 0x0;
	iface_q->qhdr->tx_irq_status = 0x0;
	iface_q->qhdr->read_idx = 0x0;
	iface_q->qhdr->write_idx = 0x0;

	/*
	 * Set receive request to zero on debug queue as there is no
	 * need of interrupt from video hardware for debug messages
	 */
	if (queue_id == IFACEQ_DBGQ_ID)
		iface_q->qhdr->rx_req = 0;
}

static void
iris_hfi_queue_init(struct iris_core *core, u32 queue_id, struct iris_iface_q_info *iface_q)
{
	struct iris_hfi_queue_table_header *q_tbl_hdr = core->iface_q_table_vaddr;
	u32 offset = sizeof(*q_tbl_hdr) + (queue_id * IFACEQ_QUEUE_SIZE);

	iface_q->device_addr = core->iface_q_table_daddr + offset;
	iface_q->kernel_vaddr =
			(void *)((char *)core->iface_q_table_vaddr + offset);
	iface_q->qhdr = &q_tbl_hdr->q_hdr[queue_id];

	iris_hfi_queue_set_header(core, queue_id, iface_q);
}

static void iris_hfi_queue_deinit(struct iris_iface_q_info *iface_q)
{
	iface_q->qhdr = NULL;
	iface_q->kernel_vaddr = NULL;
	iface_q->device_addr = 0;
}

int iris_hfi_queues_init(struct iris_core *core)
{
	struct iris_hfi_queue_table_header *q_tbl_hdr;
	u32 queue_size;

	/* Iris hardware requires 4K queue alignment */
	queue_size = ALIGN((sizeof(*q_tbl_hdr) + (IFACEQ_QUEUE_SIZE * IFACEQ_NUMQ)), SZ_4K);
	core->iface_q_table_vaddr = dma_alloc_attrs(core->dev, queue_size,
						    &core->iface_q_table_daddr,
						    GFP_KERNEL, DMA_ATTR_WRITE_COMBINE);
	if (!core->iface_q_table_vaddr) {
		dev_err(core->dev, "queues alloc and map failed\n");
		return -ENOMEM;
	}

	core->sfr_vaddr = dma_alloc_attrs(core->dev, SFR_SIZE,
					  &core->sfr_daddr,
					  GFP_KERNEL, DMA_ATTR_WRITE_COMBINE);
	if (!core->sfr_vaddr) {
		dev_err(core->dev, "sfr alloc and map failed\n");
		dma_free_attrs(core->dev, queue_size, core->iface_q_table_vaddr,
			       core->iface_q_table_daddr, DMA_ATTR_WRITE_COMBINE);
		core->iface_q_table_vaddr = NULL;
		core->iface_q_table_daddr = 0;
		core->sfr_daddr = 0;
		return -ENOMEM;
	}

	iris_hfi_queue_init(core, IFACEQ_CMDQ_ID, &core->command_queue);
	iris_hfi_queue_init(core, IFACEQ_MSGQ_ID, &core->message_queue);
	iris_hfi_queue_init(core, IFACEQ_DBGQ_ID, &core->debug_queue);

	q_tbl_hdr = (struct iris_hfi_queue_table_header *)core->iface_q_table_vaddr;
	q_tbl_hdr->version = 0;
	q_tbl_hdr->device_addr = (void *)core;
	strscpy(q_tbl_hdr->name, "iris-hfi-queues", sizeof(q_tbl_hdr->name));
	q_tbl_hdr->size = sizeof(*q_tbl_hdr);
	q_tbl_hdr->qhdr0_offset = sizeof(*q_tbl_hdr) -
		(IFACEQ_NUMQ * sizeof(struct iris_hfi_queue_header));
	q_tbl_hdr->qhdr_size = sizeof(q_tbl_hdr->q_hdr[0]);
	q_tbl_hdr->num_q = IFACEQ_NUMQ;
	q_tbl_hdr->num_active_q = IFACEQ_NUMQ;

	 /* Write sfr size in first word to be used by firmware */
	*((u32 *)core->sfr_vaddr) = SFR_SIZE;

	return 0;
}

void iris_hfi_queues_deinit(struct iris_core *core)
{
	u32 queue_size;

	if (!core->iface_q_table_vaddr)
		return;

	iris_hfi_queue_deinit(&core->debug_queue);
	iris_hfi_queue_deinit(&core->message_queue);
	iris_hfi_queue_deinit(&core->command_queue);

	dma_free_attrs(core->dev, SFR_SIZE, core->sfr_vaddr,
		       core->sfr_daddr, DMA_ATTR_WRITE_COMBINE);

	core->sfr_vaddr = NULL;
	core->sfr_daddr = 0;

	queue_size = ALIGN(sizeof(struct iris_hfi_queue_table_header) +
		(IFACEQ_QUEUE_SIZE * IFACEQ_NUMQ), SZ_4K);

	dma_free_attrs(core->dev, queue_size, core->iface_q_table_vaddr,
		       core->iface_q_table_daddr, DMA_ATTR_WRITE_COMBINE);

	core->iface_q_table_vaddr = NULL;
	core->iface_q_table_daddr = 0;
}
