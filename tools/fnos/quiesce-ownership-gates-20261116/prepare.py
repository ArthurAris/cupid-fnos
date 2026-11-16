#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Freeze a new ownership-gate-only successor. Run Python -I -B with paths."""
import argparse,pathlib,json,hashlib,shutil,difflib
p=argparse.ArgumentParser();p.add_argument('baseline',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
assert sha(a.baseline/'source-manifest.json')=='c4efc0ed52e0e408c196f030cca967508d5b5f25002abfdaacdc5405ddcb39ca'
b=json.loads((a.baseline/'source-manifest.json').read_text());before={p.name:sha(p)for p in(a.baseline/'driver').iterdir()if p.is_file()};assert len(before)==57 and before==b['after'];assert not a.output.exists();a.output.mkdir(parents=True);shutil.copytree(a.baseline/'driver',a.output/'driver');d=a.output/'driver'
def edit(n,old,new):
 f=d/n;t=f.read_text();assert t.count(old)==1,(n,old[:120],t.count(old));f.write_text(t.replace(old,new),newline='\n')
# Public cancellation is separately unadmitted during first-HOLD observation.
edit('iris_vidc.c','\t\tif (!req->count && inst->repeat_cpu_drained)\n\t\t\treturn v4l2_m2m_ioctl_reqbufs(filp, fh, req);','''		if (!req->count && !IRIS_NATIVE_REPEAT_RECLAIM_ENABLED)
			return -EOPNOTSUPP;
		if (!req->count && inst->repeat_cpu_drained)
			return v4l2_m2m_ioctl_reqbufs(filp, fh, req);''')
edit('iris_vidc.c','\tif (cmd == IRIS_STOP_IOCTL) {','''	/* No framework cancellation is admitted by the first-HOLD control. */
	if (inst->core->native_repeat && !IRIS_NATIVE_REPEAT_RECLAIM_ENABLED) {
		if (cmd == VIDIOC_STREAMOFF)
			return -EOPNOTSUPP;
		if (cmd == VIDIOC_REQBUFS) {
			struct v4l2_requestbuffers req;

			if (copy_from_user(&req, (void __user *)arg, sizeof(req)))
				return -EFAULT;
			if (!req.count)
				return -EOPNOTSUPP;
		}
	}
	if (cmd == IRIS_STOP_IOCTL) {''')
edit('iris_vb2.c','\t\tif (inst->core->native_repeat && inst->repeat_cpu_drained)\n\t\t\tiris_helper_buffers_done(inst, q->type, VB2_BUF_STATE_ERROR);','\t\tif (inst->core->native_repeat && IRIS_NATIVE_REPEAT_RECLAIM_ENABLED &&\n\t\t    inst->repeat_cpu_drained)\n\t\t\tiris_helper_buffers_done(inst, q->type, VB2_BUF_STATE_ERROR);')
# Ledger is not permission to normalize inconsistent physical allocation attrs.
edit('iris_hfi_gen2_response.c','\tif (!match || !match->repeat_owner)\n\t\treturn -ESTALE;\n\trelease = !!(wire->flags & HFI_BUF_FW_FLAG_RELEASE_DONE);','''	if (!match || !match->repeat_owner)
		return -ESTALE;
	if (match->experiment_retired || !(match->attr & BUF_ATTR_QUEUED) ||
	    match->attr & (BUF_ATTR_DEQUEUED | BUF_ATTR_BUFFER_DONE | BUF_ATTR_DEFERRED |
			   BUF_ATTR_RETURNED) ||
	    !!(match->attr & BUF_ATTR_PENDING_RELEASE) != match->repeat_owner->release_requested ||
	    !!(match->attr & BUF_ATTR_READONLY) != match->repeat_owner->readonly ||
	    iris_hfi_gen2_validate_buffer(match, wire, match->buffer_size))
		return -ESTALE;
	release = !!(wire->flags & HFI_BUF_FW_FLAG_RELEASE_DONE);''')
edit('iris_hfi_gen2_response.c','\tif (!release) {\n\t\t/* PERSIST STOP ownership has not been measured.', '''	if (!release) {
		/* Measured STOP contract returns no buffer flags, unlike RELEASE_DONE. */
		if (wire->flags || match->attr & BUF_ATTR_PENDING_RELEASE)
			return -ESTALE;
		/* PERSIST STOP ownership has not been measured.''')
old='''				if (core->native_repeat && packet->type == HFI_CMD_BUFFER &&
				    packet->payload_info == HFI_PAYLOAD_STRUCTURE &&
				    packet->size == sizeof(*packet) + sizeof(struct iris_hfi_buffer) &&
				    ((struct iris_hfi_buffer *)packet->payload)->type != HFI_BUFFER_BITSTREAM &&
				    ((struct iris_hfi_buffer *)packet->payload)->type != HFI_BUFFER_RAW) {
					ret = iris_repeat_internal_return(inst, packet);
					if (ret) {
						iris_hfi_gen2_session_failure(inst);
						goto exit;
					}
					pkt += packet->size;
					continue;
				}'''
new='''				if (core->native_repeat && packet->type == HFI_CMD_BUFFER) {
					struct iris_hfi_buffer *wire;

					/* Validate the complete body before reading even wire->type.
					 * Native replies never enter retained observers/legacy release.
					 */
					if (packet->payload_info != HFI_PAYLOAD_STRUCTURE ||
					    packet->size != sizeof(*packet) + sizeof(*wire)) {
						iris_hfi_gen2_session_failure(inst);
						ret = -EINVAL;
						goto exit;
					}
					wire = (void *)packet->payload;
					if (wire->type == HFI_BUFFER_BITSTREAM || wire->type == HFI_BUFFER_RAW)
						ret = iris_hfi_gen2_handle_session_buffer(inst, packet);
					else
						ret = iris_repeat_internal_return(inst, packet);
					if (ret || inst->state == IRIS_INST_ERROR) {
						if (!ret)
							ret = -EIO;
						iris_hfi_gen2_session_failure(inst);
						goto exit;
					}
					pkt += packet->size;
					continue;
				}'''
edit('iris_hfi_gen2_response.c',old,new)
# Defensive direct handler entry rejects native internal replies before legacy logic.
edit('iris_hfi_gen2_response.c','\t/* Observer accepts only bound valid replies before this fallback. Never','\tif (inst->core->native_repeat)\n\t\treturn -EINVAL;\n\n\t/* Observer accepts only bound valid replies before this fallback. Never')
after={p.name:sha(p)for p in d.iterdir()if p.is_file()};changed=[n for n in sorted(before)if before[n]!=after[n]];assert changed==['iris_hfi_gen2_response.c','iris_vb2.c','iris_vidc.c']
patch=''.join(''.join(difflib.unified_diff((a.baseline/'driver'/n).read_text().splitlines(True),(d/n).read_text().splitlines(True),fromfile='a/'+n,tofile='b/'+n))for n in changed);(a.output/'ownership-gates.patch').write_text(patch,newline='\n')
m={'schema':'iris-quiesce-ownership-gates-successor-1','baseline':str(a.baseline),'baseline_manifest_sha256':sha(a.baseline/'source-manifest.json'),'before':before,'after':after,'files':57,'changed_files':changed,'patch_sha256':sha(a.output/'ownership-gates.patch'),'native_gate':1,'reclaim_gate':0,'scope':'three ownership safety gates only; fixed firstPSC/EOS control+receipt+HOLD','device_accessed':False,'kernel_built':False,'physical_dma_proven':False,'native_verified':False,'gaps':['Whole GCC12 module build and CRC/source binding remain main','No real PERSIST/STOP/CLOSE/CPUdrain evidence','Full VB2 framework and device ioctls not executed','CLOSEACK tuple not exported: receipt cannot establish fullquiesce','Only single caller/H264 NV12 firstPSC; no DRC/seek/multiuser/reclaim']};(a.output/'source-manifest.json').write_text(json.dumps(m,ensure_ascii=False,indent=2)+'\n');print(json.dumps({'output':str(a.output),'changed':changed,'patch_sha256':m['patch_sha256']},ensure_ascii=False))
