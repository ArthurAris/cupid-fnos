/* SPDX-License-Identifier: GPL-2.0-only */
/*
 * Copyright (c) 2022-2024 Qualcomm Innovation Center, Inc. All rights reserved.
 */

#ifndef __IRIS_CORE_H__
#define __IRIS_CORE_H__

#include <linux/types.h>
#include <linux/atomic.h>
#include <linux/spinlock.h>
#include <linux/refcount.h>
#include <linux/pm_domain.h>
#include <media/v4l2-device.h>
#include <linux/videodev2.h>

#include "iris_hfi_common.h"
#include "iris_hfi_queue.h"
#include "iris_platform_common.h"
#include "iris_resources.h"
#include "iris_state.h"


/* Retained-only diagnostic publication. Each valid value is immutable forever. */
#define IRIS_RELEASE_OBSERVER_ENABLED 0
#define IRIS_RELEASE_OBSERVER_TOKEN "iris_release_observer_token_public_experiment_v1"
#define IRIS_RELEASE_OBSERVER_PROPERTY "qcom,iris-release-observer-token"
#define IRIS_RELEASE_OBSERVER_SLOTS 4
struct iris_release_observer_request {
	u32 session_id, header_id, packet_id, type, port, flags;
	u32 index, addr_lo, addr_hi, size;
	u32 first_header_id, first_packet_id, first_valid;
	int first_rc, queue_rc;
};
struct iris_release_observer_reply {
	u32 session_id, header_id, packet_id, type, port, flags, payload;
	u32 buffer_type, buffer_flags, index, addr_lo, addr_hi, size;
	u32 addr_offset, data_offset, data_size;
};
struct iris_release_observer_slot {
	atomic_t				request_claim, request_valid;
	struct iris_release_observer_request	request;
	atomic_t				reply_claim, reply_valid;
	struct iris_release_observer_reply	reply;
};
static inline int iris_release_observer_index(u32 type)
{
	switch (type) {
	case BUF_BIN: return 0;
	case BUF_COMV: return 1;
	case BUF_NON_COMV: return 2;
	case BUF_LINE: return 3;
	default: return -1;
	}
}
static inline void iris_release_observer_publish_request(struct iris_release_observer_slot *slot,
	const struct iris_release_observer_request *value)
{
	if (atomic_cmpxchg(&slot->request_claim, 0, 1))
		return;
	slot->request = *value;
	/* queue_rc and all first-submission fields precede this single publication. */
	smp_store_release(&slot->request_valid.counter, 1);
}
static inline void iris_release_observer_publish_reply(struct iris_release_observer_slot *slot,
	const struct iris_release_observer_reply *value)
{
	if (atomic_cmpxchg(&slot->reply_claim, 0, 1))
		return;
	slot->reply = *value;
	smp_store_release(&slot->reply_valid.counter, 1);
}


#define IRIS_STOP_OBSERVER_ENABLED 0
#define IRIS_STOP_OBSERVER_TOKEN "iris_stop_observer_token_public_experiment_v1"
#define IRIS_STOP_OBSERVER_PROPERTY "qcom,iris-stop-observer-token"
#define IRIS_STOP_IOCTL _IOW('V', BASE_VIDIOC_PRIVATE + 7, struct iris_stop_command)
struct iris_stop_command {
	u32	magic, version, action, reserved;
};
struct iris_stop_app {
	u32 valid, header_id, packet_id, addr_lo, addr_hi, size;
	int queue_rc;
	u32 submissions, returned, readonly, rx_header, rx_packet, rx_flags, rx_buffer_flags;
};
struct iris_stop_snapshot {
	u32 queued[2], readonly[2], unknown[2], seen[2];
	struct iris_stop_app app[2][VIDEO_MAX_FRAME];
};
struct iris_stop_transaction {
	u32 request_header, request_packet, port;
	int queue_rc, wait_rc;
	u32 ack_header, ack_packet, ack_flags, ack;
};
struct iris_stop_evidence {
	atomic_t pre_valid, post_valid, result_valid;
	struct iris_stop_snapshot pre, post;
	struct iris_stop_transaction transaction[2];
	int result;
};

/* STOP-attempt CPU evidence only; one inst-lock writer, one immutable publish. */
#define IRIS_STOP_FAILURE_SLOTS 8
enum iris_stop_failure_reason {
	IRIS_STOP_FAILURE_NONE,
	IRIS_STOP_FAILURE_MALFORMED,
	IRIS_STOP_FAILURE_CLOSING,
	IRIS_STOP_FAILURE_PREERROR,
	IRIS_STOP_FAILURE_SOURCE_CHANGE,
	IRIS_STOP_FAILURE_SESSION_FLAGS,
	IRIS_STOP_FAILURE_SESSION_TYPE,
	IRIS_STOP_FAILURE_CALLBACK_RET,
	IRIS_STOP_FAILURE_CALLBACK_STATE,
	IRIS_STOP_FAILURE_DEQUEUE_RET,
	IRIS_STOP_FAILURE_DEQUEUE_STATE,
	IRIS_STOP_FAILURE_BUFFER_PAYLOAD,
	IRIS_STOP_FAILURE_PACKET_FLAGS,
	IRIS_STOP_FAILURE_BUFFER_TYPE,
	IRIS_STOP_FAILURE_BUFFER_PORT,
	IRIS_STOP_FAILURE_INPUT_MISSING,
	IRIS_STOP_FAILURE_INPUT_VALIDATE,
	IRIS_STOP_FAILURE_INPUT_FLAGS,
	IRIS_STOP_FAILURE_OUTPUT_MISSING,
	IRIS_STOP_FAILURE_OUTPUT_VALIDATE,
	IRIS_STOP_FAILURE_OUTPUT_DRAIN,
	IRIS_STOP_FAILURE_OUTPUT_DRC,
	IRIS_STOP_FAILURE_OUTPUT_FLAGS,
	IRIS_STOP_FAILURE_STOP_SUCCESS,
	IRIS_STOP_FAILURE_STOP_PAUSE,
	IRIS_STOP_FAILURE_WAIT_RETURN,
};
struct iris_stop_failure_packet {
	u32 header, session, ordinal, id, type, port, flags, payload, size;
	u32 raw_bytes, raw[4], buffer_flags;
	int buffer_ret;
};
struct iris_stop_failure {
	atomic_t valid, late_ignored;
	u32 session, count, overflow, current_ordinal, current_id;
	u32 reason, inst_state, core_state, sub_state;
	int actual_ret;
	u32 header, error_session, ordinal, packet_id;
	u32 received, header_size, num_packets;
	struct iris_stop_failure_packet packet[IRIS_STOP_FAILURE_SLOTS];
};

struct platform_device;
struct iris_inst;
struct vb2_queue;

/* Installed hooks only; independent of the three execution/admission gates. */
#define IRIS_DECODE_ONCE_RETENTION_INSTALLED 1

struct icc_info {
	const char		*name;
	u32			bw_min_kbps;
	u32			bw_max_kbps;
};

#define IRIS_FW_VERSION_LENGTH		128
#define IFACEQ_CORE_PKT_SIZE		(1024 * 4)

enum domain_type {
	ENCODER	= BIT(0),
	DECODER	= BIT(1),
};

/**
 * struct iris_core - holds core parameters valid for all instances
 *
 * @dev: reference to device structure
 * @reg_base: IO memory base address
 * @irq: iris irq
 * @v4l2_dev: a holder for v4l2 device structure
 * @vdev_dec: iris video device structure for decoder
 * @vdev_enc: iris video device structure for encoder
 * @iris_v4l2_file_ops: iris v4l2 file ops
 * @iris_v4l2_ioctl_ops_dec: iris v4l2 ioctl ops for decoder
 * @iris_v4l2_ioctl_ops_enc: iris v4l2 ioctl ops for encoder
 * @iris_vb2_ops: iris vb2 ops
 * @icc_tbl: table of iris interconnects
 * @icc_count: count of iris interconnects
 * @pmdomain_tbl: table of iris power domains
 * @opp_pmdomain_tbl: table of opp power domains
 * @clock_tbl: table of iris clocks
 * @clk_count: count of iris clocks
 * @resets: table of iris reset clocks
 * @controller_resets: table of controller reset clocks
 * @iris_platform_data: a structure for platform data
 * @state: current state of core
 * @iface_q_table_daddr: device address for interface queue table memory
 * @sfr_daddr: device address for SFR (Sub System Failure Reason) register memory
 * @iface_q_table_vaddr: virtual address for interface queue table memory
 * @sfr_vaddr: virtual address for SFR (Sub System Failure Reason) register memory
 * @command_queue: shared interface queue to send commands to firmware
 * @message_queue: shared interface queue to receive responses from firmware
 * @debug_queue: shared interface queue to receive debug info from firmware
 * @lock: a lock for this strucure
 * @response_packet: a pointer to response packet from fw to driver
 * @header_id: id of packet header
 * @packet_id: id of packet
 * @power: a structure for clock and bw information
 * @hfi_ops: iris hfi command ops
 * @hfi_response_ops: iris hfi response ops
 * @core_init_done: structure of signal completion for system response
 * @intr_status: interrupt status
 * @sys_error_handler: a delayed work for handling system fatal error
 * @instances: a list_head of all instances
 * @inst_fw_caps_dec: an array of supported instance capabilities by decoder
 * @inst_fw_caps_enc: an array of supported instance capabilities by encoder
 */

/**
 * struct iris_power_progress - checked ownership, including provider attempts
 *
 * Domain/clock refs are recorded after success. Provider attempts may change
 * hardware even on error: dirty flags are set before calling them and retired
 * only by a successful zero-rate/unvote. @icc_voted records a successful vote.
 */
/* Init-only one-shot attempts: PM refs are not verified resumed-domain refs. */
struct iris_pm_get_progress {
	struct device *dev;
	bool attempted;
	bool pm_get_held;
	int error;
};

/* By-name/helper internals can still invoke providers and queue power-off work. */
struct iris_pd_attach_progress {
	const char *name;
	bool attempted;
	bool acquired;
	bool token_held;
	bool link_attempted;
	bool link_held;
	int error;
};

struct iris_init_only_pm_progress {
	bool attempted;
	bool complete;
	bool opp_active;
	u32 ctrl_count;
	u32 opp_count;
	int error;
	struct iris_pd_attach_progress domains[4];
	struct iris_pm_get_progress opp_pm_get[2];
	bool *clk_token_held;
};

struct iris_power_progress {
	bool	icc_voted;
	bool	icc_dirty;
	bool	opp_dirty;
	bool	ctrl_domain_ref;
	bool	hw_domain_ref;
	bool	hwmode_set;
	bool	axi_clk_on;
	bool	ctrl_clk_on;
	bool	hw_clk_on;
	bool	controller_drained;
	struct iris_pm_get_progress ctrl_pm_get;
	struct iris_pm_get_progress hw_pm_get;
};

/* Init-only results are raw helper errno values; -ECANCELED means not attempted.
 * A terminal result of zero proves CPU IRQ/work drain only, never DMA stop.
 */
enum iris_init_only_stage {
	IRIS_INIT_ONLY_ADMISSION,
	IRIS_INIT_ONLY_QUEUES,
	IRIS_INIT_ONLY_POWER,
	IRIS_INIT_ONLY_FIRMWARE,
	IRIS_INIT_ONLY_BOOT,
	IRIS_INIT_ONLY_IRQ,
	IRIS_INIT_ONLY_SYS_INIT,
	IRIS_INIT_ONLY_RESPONSE,
	IRIS_INIT_ONLY_TERMINAL,
	IRIS_INIT_ONLY_STAGE_COUNT,
};

/* Fixed three-AU diagnostic slots, CPU content observed once before mem_prepare.
 * valid bit0: CPU hash/shape published; bit1: serialized HFI shape published.
 * No stored buffer/instance pointer, payload copy, or DMA address.
 */
struct iris_input_proof {
	atomic_t cpu_attempted;
	atomic_t hfi_attempted;
	atomic_t valid;
	u32 cpu_bytes;
	u32 cpu_offset;
	u32 cpu_alloc;
	u32 hash;
	u32 bytes;
	u32 offset;
	u32 alloc;
	u32 kind;
	u32 port;
	u32 flags;
	u32 addr_ok;
};

struct iris_core {
	struct device				*dev;
	void __iomem				*reg_base;
	int					irq;
	struct v4l2_device			v4l2_dev;
	struct video_device			*vdev_dec;
	struct video_device			*vdev_enc;
	const struct v4l2_file_operations	*iris_v4l2_file_ops;
	const struct v4l2_ioctl_ops		*iris_v4l2_ioctl_ops_dec;
	const struct v4l2_ioctl_ops		*iris_v4l2_ioctl_ops_enc;
	const struct vb2_ops			*iris_vb2_ops;
	struct icc_bulk_data			*icc_tbl;
	u32					icc_count;
	struct dev_pm_domain_list		*pmdomain_tbl;
	struct dev_pm_domain_list		*opp_pmdomain_tbl;
	struct clk_bulk_data			*clock_tbl;
	u32					clk_count;
	struct reset_control_bulk_data		*resets;
	struct reset_control_bulk_data		*controller_resets;
	const struct iris_platform_data		*iris_platform_data;
	enum iris_core_state			state;
	dma_addr_t				iface_q_table_daddr;
	dma_addr_t				sfr_daddr;
	void					*iface_q_table_vaddr;
	void					*sfr_vaddr;
	struct iris_iface_q_info		command_queue;
	struct iris_iface_q_info		message_queue;
	struct iris_iface_q_info		debug_queue;
	struct mutex				lock; /* lock for core related operations */
	u8					*response_packet;
	u32					header_id;
	u32					packet_id;
	struct iris_core_power			power;
	const struct iris_hfi_command_ops	*hfi_ops;
	const struct iris_hfi_response_ops	*hfi_response_ops;
	struct completion			core_init_done;
	struct iris_hfi_pending			init_pending;
	u32					intr_status;
	struct delayed_work			sys_error_handler;
	struct list_head			instances;
	struct list_head			orphan_instances;
	struct mutex				lifecycle_lock;
	bool					firmware_shutdown_verified;
	bool					irq_drain_verified;
	bool					recovery_drain_verified;
	struct mutex				quiesce_lock;
	bool					terminal_shutdown;
	bool					irq_quiescing;
	bool					recovery_blocked;
	bool					quiesce_irq_mask_held;
	bool					shutdown_pm_held;
	refcount_t				owner_refs;
	struct mutex				remove_lock;
	u32					open_inflight;
	u32					open_owners;
	u32					orphan_owners;
	bool					removing;
	bool					remove_prepared;
	bool					queues_allocated;
	bool					power_prepared;
	bool					firmware_touched;
	bool					firmware_may_run;
	int					firmware_load_error;
	int					firmware_shutdown_error;
	bool					hardware_owner_held;
	bool					resources_releasing;
	bool					irq_registered;
	bool					runtime_pm_enabled;
	bool					mmio_claimed;
	bool					mmio_mapped;
	resource_size_t				mmio_start;
	resource_size_t				mmio_size;
	bool					icc_acquired;
	bool					clocks_acquired;
	bool					domains_attached;
	bool					opp_domains_attached;
	bool					opp_table_added;
	int					*opp_tokens;
	u32					opp_token_count;
	u32					reset_count;
	u32					controller_reset_count;
	struct iris_init_only_pm_progress	init_only_pm;
	struct iris_power_progress		power_progress;
	bool					power_state_unknown;
	int					power_error;
	int					power_cleanup_error;
	int					power_opp_error;
	int					power_domain_put_error;
	bool					power_irq_enabled;
	struct mutex				detach_lock;
	u32					video_owners;
	bool					decoder_registered;
	bool					encoder_registered;
	bool					v4l2_registered;
	bool					frontend_detached;
	bool					detach_complete;
	raw_spinlock_t				irq_lock;
	bool					irq_managed;
	bool					irq_initial_mask_held;
	bool					irq_top_mask_held;
	bool					irq_accepting;
	bool					irq_started;
	bool					firmware_booted;
	bool					init_acknowledged;
	bool					gen2_ack_supported;
	bool					core_ready;
	bool					pm_guard_held;
	bool					irq_releasing;
	bool					irq_probe_unpublished;
	int					probe_cleanup_error;
	/* Set once by the private probe before execution; never publish consumers. */
	bool					init_only;
	/* Separate one-session experiment. init_only remains the retain policy. */
	bool                                    decode_once;
	bool                                    decode_once_enabled;
	/* Hooks installed before publication; backing pins are checked per queue. */
	bool                                    decode_once_retention_ready;
	bool                                    decode_once_admission;
	bool                                    decode_once_claimed;
	bool					experiment_release_retained;
	bool					experiment_file_gone;
	struct iris_inst			*experiment_retained_inst;
	bool					init_only_attempted;
	bool					init_only_terminal_done;
	bool					init_only_module_held;
	bool					init_only_remove_blocked;
	/* Two-phase coordinator state; remove_blocked is NOT a framework veto. */
	bool					init_only_prepared;
	bool					init_only_post_bind;
	bool					init_only_run_claimed;
	bool					init_only_run_done;
	bool					init_only_remove_seen;
	bool					init_only_provider_attempted;
	int					init_only_run_error;
	enum iris_init_only_stage		init_only_stage;
	int					init_only_status[IRIS_INIT_ONLY_STAGE_COUNT];
	int					init_only_error;
	/* encoder and decoder have overlapping caps, so two different arrays are required */
	struct platform_inst_fw_cap		inst_fw_caps_dec[INST_FW_CAP_MAX];
	struct platform_inst_fw_cap		inst_fw_caps_enc[INST_FW_CAP_MAX];
	/* Diagnostic-only tail: kzalloc-zeroed, never reset during the run.
	 * Scalars only; no instance pointer or DMA/frame-readiness claim.
	 */
	atomic_t				decode_progress_start_calls;
	atomic_t				decode_progress_start_rc;
	atomic_t				decode_progress_defer_before;
	atomic_t				decode_progress_defer_after;
	atomic_t				decode_progress_input_attempt;
	atomic_t				decode_progress_input_ok;
	atomic_t				decode_progress_input_first_error;
	atomic_t				decode_progress_session_rx;
	atomic_t				decode_progress_psc_seen;
	atomic_t				decode_progress_session_state;
	/* Response classification only; retains existing core field offsets. */
	atomic_t				decode_progress_rx_open;
	atomic_t				decode_progress_rx_start;
	atomic_t				decode_progress_rx_sub;
	atomic_t				decode_progress_rx_prop;
	atomic_t				decode_progress_rx_buf;
	atomic_t				decode_progress_rx_other;
	atomic_t				decode_progress_rx_err;
	atomic_t				decode_progress_in_enter;
	atomic_t				decode_progress_in_done;
	atomic_t				decode_progress_in_err;
	atomic_t				decode_progress_last_type;
	atomic_t				decode_progress_last_port;
	atomic_t				decode_progress_last_flags;
	/* Combined input/debug provenance, append-only diagnostic storage. */
	struct iris_input_proof			input_proof[3];
	atomic_t				input_internal_mask;
	atomic_t				input_debug_count;
	atomic_t				input_debug_level;
	atomic_t				input_debug_last;
	atomic_t				input_debug_bits;
	atomic_t				input_debug_unknown;
	atomic_t				input_last_info;
	atomic_t				input_last_property;
	/* First COMV request and first COMV-bearing reply: diagnostic only. */
	atomic_t				input_comv_tx_claim;
	atomic_t				input_comv_rx_claim;
	atomic_t				input_comv_count;
	atomic_t				input_comv_id;
	atomic_t				input_comv_bits;
	atomic_t				input_comv_flags;
	atomic_t				input_comv_payload;
	atomic_t				input_comv_value;
	/* Successful pre-firmware SW-mode request, not a live physical-state proof. */
	bool					swmode_request_held;
	/* Append-only CPU diagnostic storage; never a DMA ownership proof. */
	bool					release_observer_enabled;
	struct iris_release_observer_slot	release_observer[IRIS_RELEASE_OBSERVER_SLOTS];
	bool stop_observer_enabled;
	struct iris_stop_evidence stop_evidence;
	struct iris_stop_failure			stop_failure;
};

void iris_stop_failure_error(struct iris_inst *inst, u32 reason, int ret);
void iris_stop_failure_freeze(struct iris_inst *inst, int ret);
bool iris_decode_once_retain_close(struct iris_inst *inst);
bool iris_decode_once_backing_pinned(struct iris_inst *inst, struct vb2_queue *q);
bool iris_decode_once_backing_pinned_locked(struct iris_inst *inst, struct vb2_queue *q);
int iris_core_init(struct iris_core *core);
int iris_core_init_only_terminal(struct iris_core *core);
/* Kernel-only isolated-owner RUN; process context, caller must not hold device_lock. */
int iris_init_only_run(struct platform_device *pdev);
int iris_decode_once_run(struct platform_device *pdev);
/* Caller holds core->lock. No hardware/provider calls or allocations. */
bool iris_decode_once_ready_locked(struct iris_core *core);
int iris_decode_once_admit_locked(struct iris_core *core);
void iris_core_deinit(struct iris_core *core);
int iris_core_deinit_checked(struct iris_core *core);
int iris_core_quiesce(struct iris_core *core);
void iris_core_get(struct iris_core *core);
void iris_core_put(struct iris_core *core);
int iris_core_open_get(struct iris_core *core);
void iris_core_open_finish(struct iris_core *core, bool success);
void iris_core_open_put(struct iris_core *core);
void iris_core_orphan_transfer(struct iris_core *core);
void iris_core_orphan_put(struct iris_core *core);
int iris_core_prepare_remove(struct iris_core *core);
int iris_release_resources(struct iris_core *core);
int iris_device_detach(struct iris_core *core);
bool iris_power_progress_pending(struct iris_core *core);
int iris_irq_activate(struct iris_core *core);
void iris_irq_terminal_mask(struct iris_core *core);
void iris_irq_retire_top(struct iris_core *core);
int iris_irq_unregister(struct iris_core *core);
void iris_core_pm_guard_get(struct iris_core *core);
void iris_core_pm_guard_put(struct iris_core *core);
void iris_core_pm_put(struct iris_core *core);

#endif
