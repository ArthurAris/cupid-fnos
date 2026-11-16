/* SPDX-License-Identifier: GPL-2.0-only */
/* Private offline implementation draft; no loader or gate transition. */
#ifndef FD_HANDOFF_H
#define FD_HANDOFF_H
#include "tty-worker.h"
#include <stdint.h>
#include <sys/stat.h>
struct hf_channel { int rx, tx, send_attempted, receive_attempted; dev_t rx_dev, tx_dev; ino_t rx_ino, tx_ino; int quarantine[4]; unsigned quarantine_count; };
struct hf_receipt { struct sp_stats stats; int64_t result_ns; uint64_t device, inode, rdev; };
struct hf_wire {
    uint32_t magic, version, status, reserved;
    int64_t result_ns;
    uint64_t device, inode, rdev;
    uint32_t rx_bytes, tx_bytes, attempts, frames, nonce_calls;
    uint32_t challenge_written, ack_validated, result_written;
};
int hf_pair(struct hf_channel *);
void hf_close_rx(struct hf_channel *);
void hf_close_tx(struct hf_channel *);
/* Consumes *tty on every call; single sendmsg only, no retry or tty write. */
int hf_send_once(struct hf_channel *, int *tty, const tw_result *, int64_t result_ns, int64_t serial_deadline_ns);
/* Requires confirmed normally exited COMPLETE flow before receive. EOF required;
 * rejects queued duplicate packets and closes all received rights on failure. */
int hf_receive_once(struct hf_channel *, int flow_confirmed, int *tty, struct hf_receipt *);
/* PID1-safe split: no syscall on received tty. Validate only in bounded observer. */
int hf_receive_metadata_once(struct hf_channel *, int flow_confirmed, int *tty, struct hf_receipt *);
int hf_validate_receipt(int tty, const struct hf_receipt *);
#endif
