#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Cold checked POWER must use full original provider flow, not session fastpath."""
import argparse,pathlib,json,hashlib,shutil,difflib
p=argparse.ArgumentParser();p.add_argument('baseline',type=pathlib.Path);p.add_argument('output',type=pathlib.Path);a=p.parse_args();sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest();assert sha(a.baseline/'source-manifest.json')=='cfdd036fa69c3b1f22815a206f544a91cc039423fce1814fb5e6bf36dcf5680e';m=json.loads((a.baseline/'source-manifest.json').read_text());before={p.name:sha(p)for p in(a.baseline/'driver').iterdir()if p.is_file()};assert before==m['after']and len(before)==57;assert not a.output.exists();a.output.mkdir(parents=True);shutil.copytree(a.baseline/'driver',a.output/'driver');f=a.output/'driver/iris_vpu_common.c';t=f.read_text();old='''int iris_vpu_power_on_checked(struct iris_core *core)
{
	const struct vpu_ops *ops = core->iris_platform_data->vpu_ops;

	if (core->native_repeat)
		return iris_repeat_power_held(core);

	if (!ops->power_on_checked)''';new='''int iris_vpu_power_on_checked(struct iris_core *core)
{
	const struct vpu_ops *ops = core->iris_platform_data->vpu_ops;

	/* Cold INIT owns core->lock and has no session. Only the ordinary
	 * session power path may validate an already-held firmware power owner.
	 */
	if (!ops->power_on_checked)''';assert t.count(old)==1;t=t.replace(old,new);f.write_text(t,newline='\n');after={p.name:sha(p)for p in(a.output/'driver').iterdir()if p.is_file()};changed=[n for n in before if before[n]!=after[n]];assert changed==['iris_vpu_common.c'];patch=''.join(difflib.unified_diff((a.baseline/'driver/iris_vpu_common.c').read_text().splitlines(True),t.splitlines(True),fromfile='a/iris_vpu_common.c',tofile='b/iris_vpu_common.c'));(a.output/'cold-power.patch').write_text(patch,newline='\n');out={'schema':'iris-cold-checked-power-successor-1','baseline':str(a.baseline),'baseline_manifest_sha256':sha(a.baseline/'source-manifest.json'),'before':before,'after':after,'files':57,'changed_files':changed,'patch_sha256':sha(a.output/'cold-power.patch'),'native_gate':1,'reclaim_gate':0,'abi_unchanged':True,'ownership_safety_three_gates_unchanged':True,'scope':'one variable: cold checked power no longer uses session-held native fastpath; all checked hardware provider steps preserved','device_accessed':False,'kernel_built':False,'native_verified':False,'dma_proven':False};(a.output/'source-manifest.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps({'output':str(a.output),'patch_sha256':out['patch_sha256']}))
