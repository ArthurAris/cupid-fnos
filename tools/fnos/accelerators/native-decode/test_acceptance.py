#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Host-only contract fixtures. No device script, network or decoder is executed."""
import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import struct
import unittest

HERE = Path(__file__).absolute().parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A = load("native_acceptance_host", "acceptance.py")
C = load("native_capture_host", "capture-native.py")


class Fixture:
    """Observed-shaped payloads for parser tests; host-fixture trust forbids native claim."""
    def __init__(self, root):
        self.root = Path(root)
        self.counter = 0
        self.versions = {}
        self.trust = {"schema": "native-decode-trust-v1", "mode": "host-fixture",
                      "producers": {}, "receipts": {}}
        self.manifest = {"schema": A.SCHEMA, "origin": "observed", "run_id": "host-structural-test",
                         "versions": self.versions, "artifacts": {}, "sessions": []}
        for name in (*A.VERSIONS, "native"):
            ref = self.raw(("host-artifact-" + name).encode(), "artifact")
            self.versions[name] = ref["sha256"]
            self.manifest["artifacts"][name] = ref
        self.source = self.raw(b"host-only h264-shaped source, not an actual bitstream", "source")
        frame = self.raw(b"\x10\x10\x10\x10\x80\x80", "frame")
        self.frame = frame
        self.oracle = self.env("oracle", "frame-oracle", {
            "source_sha256": self.source["sha256"], "codec": "h264", "timebase": [1, 1],
            "producer_method": "known-answer-NV12",
            "frames": [{"pts": i, "sha256": frame["sha256"], "width": 2, "height": 2} for i in range(601)]})
        self.session_payloads = {}
        for i in range(30):
            self.session("n" + str(i), "native", "boot-before", 601 if i == 0 else 3)
        for boot in ("boot-before", "boot-after"):
            for kind in ("ffmpeg", "mediasrv"):
                self.session(kind + "-" + boot, kind, boot, 3)
        self.session("native-after", "native", "boot-after", 3)
        self.upgrade_capture()
        self.extras()
        self.upgrade_dma()

    def raw(self, data, role, boot="boot-before", nonce="host-fixture-nonce"):
        self.counter += 1
        name = "raw-%04d.json" % self.counter
        (self.root / name).write_bytes(data)
        h = A.digest(data)
        producer = "host-test-" + role
        ph = A.digest(producer.encode())
        if role in ("hfi-packet", "mmio-snapshot"):
            producer = "host-test-dma-provider"
            ph = A.digest(producer.encode())
        existing = self.trust["producers"].get(producer, {"roles": []})
        self.trust["producers"][producer] = {"sha256": ph, "roles": sorted(set(existing["roles"] + [role]))}
        self.trust["receipts"][h] = {"producer": producer, "role": role, "boot_id": boot, "nonce": nonce}
        return {"path": name, "sha256": h}

    def env(self, role, kind, payload, boot="boot-before", nonce="host-fixture-nonce"):
        producer = "host-test-" + role
        e = {"schema": A.SCHEMA, "origin": "observed", "producer": {"id": producer, "sha256": A.digest(producer.encode())},
             "versions": self.versions, "run_id": self.manifest["run_id"], "boot_id": boot,
             "nonce": nonce, "kind": kind, "payload": payload}
        return self.raw(json.dumps(e, sort_keys=True).encode(), role, boot, nonce)

    def session(self, sid, kind, boot, count):
        pid = 100 + len(self.session_payloads)
        ident = {"pid": pid, "starttime": pid * 100, "jobid": "job-" + sid}
        p = dict(ident, session_id=sid, consumer=kind, executable_sha256=self.versions[kind],
                 ffi_sha256=self.versions["ffi"], node={"path": "/dev/video0", "major": 81, "minor": 0, "type": "character"},
                 source=self.source, oracle=self.oracle, oracle_range=[0, count - 1], events=[])
        events = p["events"]
        def event(op, **kw):
            seq = len(events)
            e = {"op": op, "seq": seq, "mono_ns": seq * 1_000_000_000, "identity": ident, "dev_t": [81, 0]}
            e.update(kw)
            events.append(e)
        argv = C.ffmpeg_command("/private/host-fixture/input.h264", "/private/host-fixture/frames.nv12") if kind == "ffmpeg" else ["/usr/trim/bin/" + kind]
        event("exec", exe_sha256=p["executable_sha256"], argv=argv)
        event("open", node="/dev/video0")
        event("ioctl", request="VIDIOC_S_FMT", queue="OUTPUT", fourcc="H264", **{"return": 0})
        event("ioctl", request="VIDIOC_S_FMT", queue="CAPTURE", fourcc="NV12", **{"return": 0})
        event("decoder-open", codec="h264_v4l2m2m", backend="v4l2-m2m", direction="decode")
        event("ioctl", request="VIDIOC_STREAMON", **{"return": 0})
        for i in range(count):
            event("frame", pts=i, sha256=self.frame["sha256"], width=2, height=2,
                  raw=self.frame, decoder_seq=4, source_sha256=self.source["sha256"])
        event("ioctl", request="VIDIOC_STREAMOFF", **{"return": 0})
        event("decoder-close")
        event("close")
        event("exit", status=0)
        p["coverage"] = {"first_seq": 0, "last_seq": len(events) - 1, "decoder_opens": 1,
                         "decoded_frames": count, "dropped_events": 0}
        if kind == "mediasrv":
            request = self.raw(json.dumps({"jobid": ident["jobid"], "source_sha256": self.source["sha256"],
                                          "operation": "transcode", "transport": "rpc",
                                          "entrypoint": "HOST-FIXTURE-NOT-A-REAL-API"}).encode(), "media-request", boot)
            p["task"] = self.env("media-task", "real-media-task", {
                "decode_process": ident, "server_process": dict(ident, executable_sha256=self.versions["mediasrv"]),
                "request": request, "source_sha256": self.source["sha256"], "decoder": "h264_v4l2m2m",
                "events": ["request-received", "job-created", "decoder-attached", "job-completed"]}, boot)
        self.session_payloads[sid] = p
        self.manifest["sessions"].append(self.env("decode", "session", p, boot, sid))

    def upgrade_capture(self):
        for ref in self.manifest["sessions"]:
            def upgrade(e):
                p = e["payload"]
                for event in p["events"]:
                    if event["op"] in ("frame", "ioctl"):
                        event["generation"] = 0
                    if event["op"] == "ioctl" and event["request"] == "VIDIOC_S_FMT":
                        event["size"] = [2, 2]
                    if event["op"] == "ioctl" and event["request"] in ("VIDIOC_STREAMON", "VIDIOC_STREAMOFF"):
                        event["queue"] = "CAPTURE"
                on = copy.deepcopy(p["events"][5])
                on["queue"] = "OUTPUT"
                p["events"].insert(5, on)
                off = copy.deepcopy(next(x for x in p["events"] if x.get("request") == "VIDIOC_STREAMOFF"))
                off["queue"] = "OUTPUT"
                p["events"].insert(-3, off)
                p["capture_generations"] = [{"generation": 0, "oracle_range": p["oracle_range"]}]
                if p["session_id"] == "n2":
                    at = next(i for i, x in enumerate(p["events"]) if x["op"] == "frame" and x["pts"] == 2)
                    ident = {k: p[k] for k in ("pid", "starttime", "jobid")}
                    change = {"op": "ioctl", "identity": ident, "dev_t": [81, 0], "request": "VIDIOC_DQEVENT",
                              "event": "V4L2_EVENT_SOURCE_CHANGE", "changes": ["RESOLUTION"],
                              "generation": 1, "size": [4, 4], "return": 0}
                    stop = dict(change, request="VIDIOC_STREAMOFF", queue="CAPTURE", generation=0)
                    fmt = dict(change, request="VIDIOC_S_FMT", queue="CAPTURE", fourcc="NV12", generation=1)
                    start = dict(change, request="VIDIOC_STREAMON", queue="CAPTURE", generation=1)
                    for event in (stop, fmt, start):
                        for key in ("event", "changes"):
                            event.pop(key, None)
                    p["events"][at:at] = [change, stop, fmt, start]
                    new_raw = self.raw(b"\x10" * 16 + b"\x80" * 8, "frame")
                    p["events"][at + 4].update(generation=1, width=4, height=4, raw=new_raw, sha256=new_raw["sha256"])
                    new_frames = [{"pts": i, "width": 2, "height": 2, "sha256": self.frame["sha256"]} for i in range(2)]
                    new_frames.append({"pts": 2, "width": 4, "height": 4, "sha256": new_raw["sha256"]})
                    p["oracle"] = self.env("oracle", "frame-oracle", {"source_sha256": self.source["sha256"],
                        "codec": "h264", "timebase": [1, 1], "producer_method": "known-answer-NV12", "frames": new_frames})
                    p["capture_generations"] = [{"generation": 0, "oracle_range": [0, 1]}, {"generation": 1, "oracle_range": [2, 2]}]
                    for event in p["events"][at + 5:]:
                        if event["op"] == "ioctl":
                            event["generation"] = 1
                for seq, event in enumerate(p["events"]):
                    event.update(seq=seq, mono_ns=seq * 1_000_000_000)
                p["coverage"]["last_seq"] = len(p["events"]) - 1
                self.session_payloads[p["session_id"]] = p
            self.mutate(ref, upgrade)

    def upgrade_dma(self):
        # Fictitious host source/provider; never a real SM8450 safety guarantee.
        source = self.raw(b"HOST-ONLY ownership contract source NOT DEVICE CODE", "contract-source")
        provider = {"id": "host-test-dma-provider", "sha256": A.digest(b"host-test-dma-provider")}
        contract = {"schema": "native-dma-platform-contract-v1", "proof_type": "hfi-buffer-ownership-return",
            "platform": "qcom,sm8450/xiaomi,cupid", "provider": provider, "source": source,
            "source_locator": "HOST-ONLY:ownership-contract", "ownership_return": {
                "meaning": "device-ownership-return-no-further-access", "packet_type": "HOST_FIXTURE_BUFFER_DONE",
                "buffer_type": "HOST_FIXTURE_CAPTURE", "wire_layout": "le-4u32-2u64-4u32",
                "packet_type_id": 42, "buffer_type_id": 7, "host_owner_code": 1}}
        contract_ref = self.raw(json.dumps(contract, sort_keys=True).encode(), "platform-contract")
        self.trust["dma_contracts"] = {"host-contract": {"contract_sha256": contract_ref["sha256"],
            "source_sha256": source["sha256"], "proof_type": contract["proof_type"],
            "platform": contract["platform"], "provider": provider}}
        lifecycle = json.loads((self.root / self.manifest["lifecycle"]["path"]).read_bytes())
        for ref in self.manifest["sessions"]:
            e = json.loads((self.root / ref["path"]).read_bytes())
            p = e["payload"]
            ident = {k: p[k] for k in ("pid", "starttime", "jobid")}
            sid = p["session_id"]
            obj = {"buffer_id": sid + "-buffer", "iova": 4096, "bytes": 4096,
                   "submission_id": 1, "generation": 0, "session_cookie": p["starttime"]}
            def upgrade(envelope):
                events = envelope["payload"]["events"]
                template = {"identity": ident, "dev_t": [81, 0], "generation": 0}
                events.insert(7, dict(template, op="submit", object=obj, seq=0, mono_ns=6_500_000_000))
                close_at = next(i for i, x in enumerate(events) if x["op"] == "decoder-close")
                when = events[close_at]["mono_ns"]
                events[close_at:close_at] = [dict(template, op="submit-disabled", seq=0, mono_ns=when - 750_000_000),
                                           dict(template, op="buffer-release", object=obj, seq=0, mono_ns=when - 250_000_000)]
                for seq, event in enumerate(events):
                    event["seq"] = seq
                envelope["payload"]["coverage"]["last_seq"] = len(events) - 1
                self.session_payloads[sid] = envelope["payload"]
            self.mutate(ref, upgrade)
            events = self.session_payloads[sid]["events"]
            release = next(x for x in events if x["op"] == "buffer-release")
            proof = self.env("dma-provider", "dma-ownership-proof", {"contract_sha256": contract_ref["sha256"],
                "source_sha256": source["sha256"], "identity": ident, "session_id": sid, "dev_t": [81, 0],
                "generation": 0, "coverage": {"first_seq": 0, "last_seq": 0, "dropped_records": 0,
                    "session_first_seq": 0, "session_last_seq": events[-1]["seq"]},
                "records": [{"op": "hfi-buffer-return", "seq": 0, "mono_ns": release["mono_ns"] - 250_000_000,
                    "object": obj, "packet_type": "HOST_FIXTURE_BUFFER_DONE", "buffer_type": "HOST_FIXTURE_CAPTURE",
                    "status": 0, "owner_after": "host", "packet_id": 1,
                    "packet": self.raw(struct.pack("<IIIIQQIIII", 42, 1, obj["session_cookie"], 7,
                        obj["iova"], obj["bytes"], 1, 0, 0, 1), "hfi-packet", e["boot_id"], sid)}]}, e["boot_id"], sid)
            reclaim = next(x for x in lifecycle["payload"]["reclaim"] if x["session_id"] == sid)
            reclaim["dma"].update(proof_type="hfi-buffer-ownership-return", contract_id="host-contract",
                                   contract=contract_ref, evidence=proof)
        drc = next(x for x in lifecycle["payload"]["cases"] if x["case"] == "drc")
        events = self.session_payloads[drc["session_id"]]["events"]
        change = next(x for x in events if x.get("event") == "V4L2_EVENT_SOURCE_CHANGE")
        fmt = next(x for x in events if x.get("request") == "VIDIOC_S_FMT" and x.get("generation") == 1)
        frame = next(x for x in events if x["op"] == "frame" and x["generation"] == 1)
        for record, event in zip(drc["records"], (change, fmt, frame)):
            record.update(event_seq=event["seq"], mono_ns=event["mono_ns"])
        drc["records"][1]["generation"] = 1
        drc["records"][2]["frame_sha256"] = frame["sha256"]
        seek = next(x for x in lifecycle["payload"]["cases"] if x["case"] == "seek")
        frames = [x for x in self.session_payloads[seek["session_id"]]["events"] if x["op"] == "frame"]
        seek["records"][0].update(target_pts=frames[2]["pts"], mono_ns=frames[0]["mono_ns"])
        seek["records"][1]["mono_ns"] = frames[1]["mono_ns"]
        seek["records"][2].update(pts=frames[2]["pts"], event_seq=frames[2]["seq"],
            mono_ns=frames[2]["mono_ns"], frame_sha256=frames[2]["sha256"])
        self.mutate(self.manifest["lifecycle"], lambda e: e.update(lifecycle))

    def extras(self):
        cases = []
        names = {"seek": ["seek-request", "flush-done", "frame-after-seek"],
                 "flush": ["flush-request", "queues-drained", "decode-resumed"],
                 "drc": ["source-change", "capture-reconfigured", "frame-new-resolution"],
                 "error_cancel": ["input-error", "cancel-request", "cancel-complete"]}
        for i, (name, ops) in enumerate(names.items()):
            sid = "n" + str(i)
            p = self.session_payloads[sid]
            records = [{"op": op, "mono_ns": (6 + j) * 1_000_000_000} for j, op in enumerate(ops)]
            if name == "seek":
                records[0]["target_pts"] = 9
                records[2].update(pts=9, frame_sha256=self.frame["sha256"])
            elif name == "flush":
                records[0]["generation"] = 1
                records[1].update(queued_input=0, queued_capture=0)
                records[2]["generation"] = 2
            elif name == "drc":
                records[0]["old_size"] = [2, 2]
                records[1]["new_size"] = [4, 4]
                records[2].update(size=[4, 4], frame_sha256=self.frame["sha256"])
            else:
                records[0]["errno"] = 22
                records[2]["result"] = "cancelled"
            cases.append({"case": name, "session_id": sid, "identity": {k: p[k] for k in ("pid", "starttime", "jobid")},
                          "boot_id": "boot-before", "records": records})
        reclaim = []
        for sid, p in self.session_payloads.items():
            end = p["events"][-1]["mono_ns"]
            boot = "boot-after" if "after" in sid else "boot-before"
            reclaim.append({"session_id": sid, "identity": {k: p[k] for k in ("pid", "starttime", "jobid")},
                            "boot_id": boot, "allocated_ids": [sid + "-buffer"], "released_ids": [sid + "-buffer"],
                            "outstanding_ids": [], "open_fds_before": 5, "open_fds_after": 5,
                            "dma": {"proof_type": "iommu-unmap-after-idle", "mapped_ids": [sid + "-buffer"],
                                    "unmapped_ids": [sid + "-buffer"], "records": [
                                        {"op": op, "mono_ns": end - (3 - j) * 1_000_000_000}
                                        for j, op in enumerate(["engine-idle-observed", "iommu-unmap-complete", "buffer-released"])]}})
        self.manifest["lifecycle"] = self.env("lifecycle", "lifecycle", {"cases": cases, "reclaim": reclaim})
        self.manifest["negative"] = self.env("negative", "hardware-absent", {
            "pid": 900, "starttime": 900, "jobid": "absent-test", "executable_sha256": self.versions["native"],
            "ffi_sha256": self.versions["ffi"], "node_inventory": [], "open_errno": 2, "exit_code": 1,
            "frames": [], "decoder_dispatches": [], "result": "hardware-absent-refused"})
        self.manifest["reboot"] = self.env("reboot", "reboot-retest", {
            "before_boot_id": "boot-before", "after_boot_id": "boot-after",
            "before_sessions": ["n0", "ffmpeg-boot-before", "mediasrv-boot-before"],
            "after_sessions": ["native-after", "ffmpeg-boot-after", "mediasrv-boot-after"]})
        self.manifest["health"] = self.env("health", "original-system-health", {"snapshots": [
            {"stage": stage, "boot_id": boot, "services": {name: "active" for name in
             ("mediasrv", "trim-media", "storage", "network", "web")}, "faults": [],
             "storage_read_sha256": self.source["sha256"]}
            for stage, boot in (("baseline", "boot-before"), ("post-reboot", "boot-after"), ("post-rollback", "boot-rollback"))]})
        backup = self.raw(b"host-backup-not-a-real-image", "backup")
        h = backup["sha256"]
        self.manifest["deployment"] = self.env("deployment", "backup-rollback", {
            "backup": backup, "rollback_readback": backup, "host_sha256": h, "device_sha256": h,
            "baseline_sha256": h, "installed_versions": self.versions,
            "records": [{"op": op, "mono_ns": j, "install_sha256": self.versions["install"], "readback_sha256": h}
                        for j, op in enumerate(["backup-read", "dual-hash-verified", "install-verified", "rollback-readback-verified"])]})

    def mutate(self, ref, fn):
        envelope = json.loads((self.root / ref["path"]).read_bytes())
        old = ref["sha256"]
        fn(envelope)
        data = json.dumps(envelope, sort_keys=True).encode()
        (self.root / ref["path"]).write_bytes(data)
        ref["sha256"] = A.digest(data)
        self.trust["receipts"][ref["sha256"]] = copy.deepcopy(self.trust["receipts"][old])

    def verdict(self):
        return A.validate(self.manifest, self.root, self.trust, self.versions)


class AcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-host-test-")
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(self.tmp.name)

    def reject(self):
        result = self.f.verdict()
        self.assertFalse(result["conditional_accepted"], result)
        for field in ("accepted", "hardware", "native", "native_decode_verified", "origin_authenticated", "physical_proof"):
            self.assertFalse(result[field], field)
        self.assertTrue(result["errors"])

    def test_complete_host_fixture_conditional_only(self):
        result = self.f.verdict()
        self.assertTrue(result["conditional_accepted"], result)
        self.assertTrue(all(v == "conditional-pass" for v in result["gates"].values()))
        for field in ("accepted", "hardware", "native", "native_decode_verified", "origin_authenticated", "physical_proof"):
            self.assertFalse(result[field])
        self.assertIn("not physical", result["scope"])

    def test_synthetic_rejected(self):
        self.f.manifest["origin"] = "synthetic"
        self.reject()

    def test_synthetic_envelope_rejected_even_repinned(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e.update(origin="synthetic"))
        self.reject()

    def test_unknown_missing_skipped(self):
        for value in (None, "unknown", "missing", "skipped", True):
            with self.subTest(value=value):
                self.f.manifest["health"] = value
                self.reject()

    def test_boolean_claims_not_evidence(self):
        self.f.manifest.update(native=True, hardware=True, accepted=True, sessions=[])
        self.reject()

    def test_cpu_decoder_fallback_repinned(self):
        def mutant(e):
            e["payload"]["events"][4].update(codec="h264", backend="cpu")
        self.f.mutate(self.f.manifest["sessions"][0], mutant)
        self.reject()

    def test_fallback_event_repinned(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"]["events"][5].update(op="cpu-fallback"))
        self.reject()

    def test_codec_listing_is_not_decode(self):
        ref = self.f.manifest["sessions"][30]
        self.f.mutate(ref, lambda e: e["payload"]["events"][0].update(argv=[C.FFMPEG, "-decoders"]))
        self.reject()

    def test_unpinned_actual_ffmpeg_device(self):
        self.f.mutate(self.f.manifest["sessions"][30], lambda e: e["payload"]["events"][0]["argv"].__setitem__(e["payload"]["events"][0]["argv"].index("-device") + 1, "/dev/video9"))
        self.reject()

    def test_unknown_oracle_method(self):
        self.f.mutate(self.f.oracle, lambda e: e["payload"].update(producer_method="self-asserted"))
        self.reject()

    def test_large_artifact_streaming_and_size_guard(self):
        path = self.f.root / "streamed.bin"
        path.write_bytes(b"bounded streaming fixture")
        self.assertEqual(A.digest(path), A.digest(path.read_bytes()))
        with self.assertRaises(ValueError):
            A.safe_file(self.f.root, path.name, limit=1)

    def test_boolean_ioctl_return_is_not_zero(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"]["events"][2].update({"return": False}))
        self.reject()

    def test_independent_supervised_mode_does_not_bypass_gates(self):
        self.f.trust.update(mode="supervised-receipts", custody_id="HOST-TEST-NOT-A-REAL-CUSTODY")
        self.f.manifest["origin"] = "synthetic"
        self.reject()

    def test_host_fixture_receipts_cannot_be_promoted_to_native(self):
        self.f.trust.update(mode="supervised-receipts", custody_id="HOST-TEST-NOT-REAL")
        self.reject()

    def test_wrong_receipt_custody_even_with_provenance(self):
        self.f.trust.update(mode="supervised-receipts", custody_id="external-custody")
        for receipt in self.f.trust["receipts"].values():
            receipt.update(provenance="supervised-device-observation", custody_id="different-custody")
        self.reject()

    def test_symlink_evidence_refused(self):
        target_path = self.f.root / self.f.source["path"]
        link = self.f.root / "source-link"
        try:
            link.symlink_to(target_path)
        except OSError:
            self.skipTest("host symlink privilege unavailable")
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"]["source"].update(path=link.name))
        self.reject()

    def test_wrong_final_version(self):
        self.f.manifest["versions"] = dict(self.f.versions, kernel="0" * 64)
        self.reject()

    def test_wrong_envelope_version_repinned(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e.update(versions=dict(e["versions"], ffi="0" * 64)))
        self.reject()

    def test_running_wrong_native_executable(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"].update(executable_sha256="0" * 64))
        self.reject()

    def test_hash_tamper(self):
        p = self.f.root / self.f.manifest["sessions"][0]["path"]
        p.write_bytes(p.read_bytes() + b" ")
        self.reject()

    def test_producer_untrusted(self):
        self.f.trust["producers"] = {}
        self.reject()

    def test_receipts_untrusted(self):
        self.f.trust["receipts"] = {}
        self.reject()

    def test_producer_identity_mutant(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["producer"].update(id="arbitrary-trace"))
        self.reject()

    def test_receipt_boot_nonce_mutant(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e.update(nonce="replay"))
        self.reject()

    def test_pid_starttime_job_node_mutants(self):
        ref = self.f.manifest["sessions"][0]
        original = json.loads((self.f.root / ref["path"]).read_bytes())
        for field, val in (("pid", 1), ("starttime", 1), ("jobid", "wrong-job")):
            with self.subTest(field=field):
                self.f.mutate(ref, lambda e, k=field, v=val: e["payload"]["events"][6]["identity"].update({k: v}))
                self.reject()
                self.f.mutate(ref, lambda e: e.update(copy.deepcopy(original)))
        self.f.mutate(ref, lambda e: e["payload"]["events"][6].update(dev_t=[81, 9]))
        self.reject()

    def test_wrong_source_capture_codec(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"]["events"][2].update(fourcc="HEVC"))
        self.reject()

    def test_wrong_frame_pts(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: next(x for x in e["payload"]["events"] if x["op"] == "frame").update(pts=99))
        self.reject()

    def test_wrong_raw_frame_hash(self):
        (self.f.root / self.f.frame["path"]).write_bytes(b"wrong-frame")
        self.reject()

    def test_coverage_dropped_events(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: e["payload"]["coverage"].update(dropped_events=1))
        self.reject()

    def test_repeat_sessions_insufficient(self):
        self.f.manifest["sessions"].pop(29)
        self.f.manifest["sessions"] = [r for r in self.f.manifest["sessions"] if r != self.f.manifest["sessions"][-1]]
        self.reject()

    def test_replayed_session_process_job(self):
        self.f.manifest["sessions"].append(copy.deepcopy(self.f.manifest["sessions"][0]))
        self.reject()

    def test_continuous_time_short(self):
        def mutant(e):
            for x in e["payload"]["events"]:
                x["mono_ns"] //= 2
        self.f.mutate(self.f.manifest["sessions"][0], mutant)
        self.reject()

    def test_continuous_gap(self):
        def mutant(e):
            for x in e["payload"]["events"][10:]:
                x["mono_ns"] += 10_000_000_000
        self.f.mutate(self.f.manifest["sessions"][0], mutant)
        self.reject()

    def test_only_ffmpeg_no_mediasrv(self):
        self.f.manifest["sessions"] = [r for r in self.f.manifest["sessions"] if
            json.loads((self.f.root / r["path"]).read_bytes())["payload"]["consumer"] != "mediasrv"]
        self.reject()

    def test_task_missing_real_request(self):
        ref = self.f.session_payloads["mediasrv-boot-before"]["task"]
        self.f.mutate(ref, lambda e: e["payload"].update(request=True))
        self.reject()

    def test_task_wrong_job(self):
        ref = self.f.session_payloads["mediasrv-boot-before"]["task"]
        self.f.mutate(ref, lambda e: e["payload"]["decode_process"].update(jobid="unrelated"))
        self.reject()

    def test_dma_unknown_ack_only(self):
        ref = self.f.manifest["lifecycle"]
        for proof in ("unknown", "ACK-only", "firmware-ack"):
            with self.subTest(proof=proof):
                self.f.mutate(ref, lambda e, v=proof: e["payload"]["reclaim"][0]["dma"].update(proof_type=v))
                self.reject()

    def test_dma_release_order(self):
        self.f.mutate(self.f.manifest["lifecycle"], lambda e: e["payload"]["reclaim"][0]["dma"]["records"].reverse())
        self.reject()

    def test_resource_leak(self):
        self.f.mutate(self.f.manifest["lifecycle"], lambda e: e["payload"]["reclaim"][0].update(outstanding_ids=["leak"]))
        self.reject()

    def test_lifecycle_skip(self):
        self.f.mutate(self.f.manifest["lifecycle"], lambda e: e["payload"]["cases"].pop())
        self.reject()

    def test_negative_cpu_fallback(self):
        self.f.mutate(self.f.manifest["negative"], lambda e: e["payload"].update(decoder_dispatches=["h264"]))
        self.reject()

    def test_reboot_not_performed(self):
        self.f.mutate(self.f.manifest["reboot"], lambda e: e["payload"].update(after_boot_id="boot-before"))
        self.reject()

    def test_postreboot_real_task_not_retested(self):
        self.f.mutate(self.f.manifest["reboot"], lambda e: e["payload"].update(after_sessions=["native-after", "ffmpeg-boot-after"]))
        self.reject()

    def test_original_system_health_failure(self):
        self.f.mutate(self.f.manifest["health"], lambda e: e["payload"]["snapshots"][1]["services"].update(storage="failed"))
        self.reject()

    def test_backup_dual_hash_mismatch(self):
        self.f.mutate(self.f.manifest["deployment"], lambda e: e["payload"].update(device_sha256="0" * 64))
        self.reject()

    def test_backup_must_precede_write(self):
        self.f.mutate(self.f.manifest["deployment"], lambda e: e["payload"]["records"].reverse())
        self.reject()

    def test_unsafe_evidence_paths(self):
        for path in ("../outside", "/etc/passwd", "C:/secret", "x\\y", "a//b"):
            with self.subTest(path=path):
                self.f.manifest["artifacts"]["native"]["path"] = path
                self.reject()

    def test_strict_json(self):
        for data in (b'{"a":1,"a":2}', b'{"a":NaN}'):
            with self.subTest(data=data), self.assertRaises(ValueError):
                A.strict_json(data)


class ActualContractTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="native-host-contract-")
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(self.tmp.name)

    def check(self, accepted):
        r = self.f.verdict()
        self.assertEqual(r["conditional_accepted"], accepted, r)
        self.assertFalse(r["native_decode_verified"])
        self.assertFalse(r["hardware"])

    def proof_mutant(self, fn):
        ref = self.f.manifest["lifecycle"]
        lifecycle = json.loads((self.f.root / ref["path"]).read_bytes())
        proof = lifecycle["payload"]["reclaim"][0]["dma"]["evidence"]
        self.f.mutate(proof, lambda e: fn(e["payload"]))
        self.f.mutate(ref, lambda e: e.update(lifecycle))

    def test_empty_nonblocking_returns(self):
        for req, errno in (("VIDIOC_DQBUF", 11), ("VIDIOC_DQEVENT", 11), ("VIDIOC_DQEVENT", 2)):
            self.assertFalse(A.ioctl_result({"request": req, "return": -1, "errno": errno}))
        self.assertFalse(A.ioctl_result({"request": "VIDIOC_DQBUF", "return": -11}))

    def test_other_ioctl_negatives_refused(self):
        for req, err in (("VIDIOC_QBUF", 11), ("VIDIOC_S_FMT", 11), ("VIDIOC_DQBUF", 2), ("VIDIOC_DQEVENT", 5)):
            with self.subTest(req=req, err=err), self.assertRaises(ValueError):
                A.ioctl_result({"request": req, "return": -1, "errno": err})
        with self.assertRaises(ValueError):
            A.ioctl_result({"request": "VIDIOC_DQEVENT", "return": -1, "errno": 11, "event": "SOURCE_CHANGE"})

    def test_nonblocking_in_actual_timeline(self):
        ref = self.f.manifest["sessions"][0]
        def add(e):
            p = e["payload"]
            event = dict(p["events"][5], request="VIDIOC_DQEVENT", **{"return": -1, "errno": 11})
            p["events"].insert(6, event)
            event["mono_ns"] = 5_500_000_000
            for seq, x in enumerate(p["events"]):
                x["seq"] = seq
        self.f.mutate(ref, add)
        envelope = json.loads((self.f.root / ref["path"]).read_bytes())
        oracle = json.loads((self.f.root / envelope["payload"]["oracle"]["path"]).read_bytes())["payload"]
        A.check_capture_timeline(envelope["payload"], envelope["payload"]["events"], oracle)

    def test_drc_generation_skip_refused(self):
        self.f.mutate(self.f.manifest["sessions"][2], lambda e: next(x for x in e["payload"]["events"] if x.get("event") == "V4L2_EVENT_SOURCE_CHANGE").update(generation=2))
        self.check(False)

    def test_drc_missing_resume_refused(self):
        self.f.mutate(self.f.manifest["sessions"][2], lambda e: next(x for x in e["payload"]["events"] if x.get("request") == "VIDIOC_STREAMON" and x.get("generation") == 1).update(request="VIDIOC_QUERYCAP"))
        self.check(False)

    def test_drc_unannounced_capture_reconfiguration(self):
        self.f.mutate(self.f.manifest["sessions"][2], lambda e: next(x for x in e["payload"]["events"] if x.get("event") == "V4L2_EVENT_SOURCE_CHANGE").update(event="V4L2_EVENT_EOS"))
        self.check(False)

    def test_drc_wrong_generation_frame(self):
        self.f.mutate(self.f.manifest["sessions"][2], lambda e: next(x for x in e["payload"]["events"] if x["op"] == "frame" and x["generation"] == 1).update(generation=0))
        self.check(False)

    def test_drc_wrong_negotiated_geometry(self):
        self.f.mutate(self.f.manifest["sessions"][2], lambda e: next(x for x in e["payload"]["events"] if x.get("request") == "VIDIOC_S_FMT" and x.get("generation") == 1).update(size=[8, 8]))
        self.check(False)

    def test_unmap_idle_no_longer_sufficient(self):
        self.f.mutate(self.f.manifest["lifecycle"], lambda e: e["payload"]["reclaim"][0]["dma"].update(proof_type="iommu-unmap-after-idle"))
        self.check(False)

    def test_dma_contract_missing(self):
        self.f.trust["dma_contracts"] = {}
        self.check(False)

    def test_repeated_exact_source_change_generations(self):
        ref = self.f.manifest["sessions"][2]
        payload = json.loads((self.f.root / ref["path"]).read_bytes())["payload"]
        oracle = json.loads((self.f.root / payload["oracle"]["path"]).read_bytes())["payload"]
        events = copy.deepcopy(payload["events"])
        at = next(i for i, x in enumerate(events) if x.get("request") == "VIDIOC_STREAMOFF" and x["generation"] == 1)
        change = copy.deepcopy(next(x for x in events if x.get("event") == "V4L2_EVENT_SOURCE_CHANGE"))
        change.update(generation=2, size=[2, 2])
        stop = dict(change, request="VIDIOC_STREAMOFF", queue="CAPTURE", generation=1)
        fmt = dict(change, request="VIDIOC_S_FMT", queue="CAPTURE", fourcc="NV12")
        start = dict(change, request="VIDIOC_STREAMON", queue="CAPTURE")
        for row in (stop, fmt, start):
            row.pop("event"); row.pop("changes")
        frame = copy.deepcopy(next(x for x in events if x["op"] == "frame"))
        frame.update(generation=2, pts=3)
        events[at:at] = [change, stop, fmt, start, frame]
        for event in events[at + 5:]:
            if event["op"] == "ioctl":
                event["generation"] = 2
        for seq, event in enumerate(events):
            event.update(seq=seq, mono_ns=seq * 1_000_000_000)
        oracle["frames"].append({k: frame[k] for k in ("pts", "sha256", "width", "height")})
        payload["capture_generations"].append({"generation": 2, "oracle_range": [3, 3]})
        self.assertEqual(len(A.check_capture_timeline(payload, events, oracle)), 2)

    def mmio_fixture(self, value=1):
        ref = self.f.manifest["lifecycle"]
        lifecycle = json.loads((self.f.root / ref["path"]).read_bytes())
        reclaim = lifecycle["payload"]["reclaim"][0]
        dma = reclaim["dma"]
        contract = json.loads((self.f.root / dma["contract"]["path"]).read_bytes())
        contract.pop("ownership_return")
        contract.update(proof_type="platform-mmio-isolation", isolation={
            "meaning": "bus-master-isolated-no-further-access", "scope": "all-submitted-session-buffers",
            "wire_layout": "le-u64-address-u32-value", "registers": [{"address": 4096, "mask": 1, "expected": 1}]})
        new_contract = self.f.raw(json.dumps(contract, sort_keys=True).encode(), "platform-contract")
        dma.update(proof_type="platform-mmio-isolation", contract=new_contract, contract_id="host-mmio")
        self.f.trust["dma_contracts"]["host-mmio"] = dict(self.f.trust["dma_contracts"]["host-contract"],
            contract_sha256=new_contract["sha256"], proof_type="platform-mmio-isolation")
        def upgrade(p):
            record = p["records"][0]
            obj = record["object"]
            packet = record.pop("packet")
            when = record["mono_ns"]
            record.clear()
            record.update(op="mmio-read", seq=0, mono_ns=when, address=4096, value=value)
            p.update(contract_sha256=new_contract["sha256"], objects=[obj], snapshot=self.f.raw(
                struct.pack("<QI", 4096, value), "mmio-snapshot", "boot-before", "n0"))
        self.f.mutate(dma["evidence"], lambda e: upgrade(e["payload"]))
        self.f.mutate(ref, lambda e: e.update(lifecycle))

    def test_exact_mmio_contract_and_snapshot(self):
        self.mmio_fixture()
        self.check(True)

    def test_mmio_wrong_isolation_value(self):
        self.mmio_fixture(value=0)
        self.check(False)

    def test_dma_packet_provider_mismatch(self):
        def mutate(p):
            h = p["records"][0]["packet"]["sha256"]
            self.f.trust["receipts"][h]["producer"] = "host-test-decode"
        self.proof_mutant(mutate)
        self.check(False)

    def test_dma_packet_session_nonce_mismatch(self):
        self.proof_mutant(lambda p: self.f.trust["receipts"][p["records"][0]["packet"]["sha256"]].update(nonce="wrong-session"))
        self.check(False)

    def test_seek_not_an_oracle_capture(self):
        self.f.mutate(self.f.manifest["lifecycle"], lambda e: e["payload"]["cases"][0]["records"][2].update(event_seq=0))
        self.check(False)

    def test_dma_exact_source_pin(self):
        self.f.trust["dma_contracts"]["host-contract"]["source_sha256"] = "0" * 64
        self.check(False)

    def test_dma_provider_pin(self):
        self.f.trust["dma_contracts"]["host-contract"]["provider"] = {"id": "trace", "sha256": "0" * 64}
        self.check(False)

    def test_hfi_ack_not_buffer_return(self):
        self.proof_mutant(lambda p: p["records"][0].update(packet_type="ACK"))
        self.check(False)

    def test_hfi_object_iova_not_matching_submit(self):
        self.proof_mutant(lambda p: p["records"][0]["object"].update(iova=99999))
        self.check(False)

    def test_hfi_cookie_not_matching_submit(self):
        self.proof_mutant(lambda p: p["records"][0]["object"].update(session_cookie=99999))
        self.check(False)

    def test_hfi_raw_packet_mutant(self):
        def mutate(p):
            raw = p["records"][0]["packet"]
            (self.f.root / raw["path"]).write_bytes(b"X" * 48)
        self.proof_mutant(mutate)
        self.check(False)

    def test_hfi_no_raw_packet_refused(self):
        self.proof_mutant(lambda p: p["records"][0].pop("packet"))
        self.check(False)

    def test_hfi_return_after_buffer_release(self):
        self.proof_mutant(lambda p: p["records"][0].update(mono_ns=9999999999999))
        self.check(False)

    def test_generation_not_disabled(self):
        self.f.mutate(self.f.manifest["sessions"][0], lambda e: next(x for x in e["payload"]["events"] if x["op"] == "submit-disabled").update(generation=99))
        self.check(False)

    def test_real_mediasrv_can_own_repeat_continuous_lifecycle_no_native_client(self):
        # Fixture remains a host-only imagined task; this tests shape only.
        for ref in self.f.manifest["sessions"]:
            def change(e):
                p = e["payload"]
                if p["consumer"] != "native":
                    return
                p.update(consumer="mediasrv", executable_sha256=self.f.versions["mediasrv"])
                p["events"][0]["exe_sha256"] = self.f.versions["mediasrv"]
                ident = {k: p[k] for k in ("pid", "starttime", "jobid")}
                request = self.f.raw(json.dumps({"jobid": p["jobid"], "source_sha256": self.f.source["sha256"],
                    "operation": "transcode", "transport": "unix", "entrypoint": "HOST-ONLY-media/p"}).encode(), "media-request", e["boot_id"])
                p["task"] = self.f.env("media-task", "real-media-task", {"decode_process": ident,
                    "server_process": dict(ident, executable_sha256=self.f.versions["mediasrv"]), "request": request,
                    "source_sha256": self.f.source["sha256"], "decoder": "h264_v4l2m2m",
                    "events": ["request-received", "job-created", "decoder-attached", "job-completed"]}, e["boot_id"])
            self.f.mutate(ref, change)
        self.f.mutate(self.f.manifest["negative"], lambda e: e["payload"].update(executable_kind="ffmpeg", executable_sha256=self.f.versions["ffmpeg"]))
        self.f.versions.pop("native")
        self.f.manifest["artifacts"].pop("native")
        rewritten = {}
        def rewrite(ref):
            key = (ref["path"], ref["sha256"])
            if key in rewritten:
                ref.update(rewritten[key])
                return
            path = self.f.root / ref["path"]
            try:
                data = json.loads(path.read_bytes())
            except (ValueError, UnicodeError):
                return
            def walk(obj):
                if isinstance(obj, dict):
                    if set(obj) == {"path", "sha256"}:
                        rewrite(obj)
                    else:
                        for value in obj.values():
                            walk(value)
                elif isinstance(obj, list):
                    for value in obj:
                        walk(value)
            walk(data)
            if data.get("schema") == A.SCHEMA:
                data["versions"] = self.f.versions
            if data.get("kind") == "backup-rollback":
                data["payload"]["installed_versions"] = self.f.versions
            raw = json.dumps(data, sort_keys=True).encode()
            path.write_bytes(raw)
            old = ref["sha256"]
            ref["sha256"] = A.digest(raw)
            self.f.trust["receipts"][ref["sha256"]] = copy.deepcopy(self.f.trust["receipts"][old])
            rewritten[key] = copy.deepcopy(ref)
        for key in ("sessions", "lifecycle", "negative", "reboot", "health", "deployment"):
            for ref in self.f.manifest[key] if isinstance(self.f.manifest[key], list) else [self.f.manifest[key]]:
                rewrite(ref)
        self.check(True)


class CaptureHostTests(unittest.TestCase):
    def test_actual_decoder_command_input_position(self):
        args = C.ffmpeg_command("/private/input.h264", "/private/frame.nv12")
        self.assertEqual(args[args.index("-c:v") + 1], "h264_v4l2m2m")
        self.assertLess(args.index("-c:v"), args.index("-i"))
        self.assertIn("-n", args)
        self.assertIn("-nostdin", args)
        self.assertEqual(args[args.index("-t") + 1], "601")
        self.assertEqual(args[args.index("-capture_pix_fmt") + 1], "nv12")
        self.assertNotIn("-hwaccels", args)
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "file")

    def test_command_refuses_network_and_unsafe_path(self):
        for path in ("http://example/test", "file:/tmp/test", "/tmp/../secret", "/tmp/a\n", "-bad", "/tmp/a:b"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                C.ffmpeg_command(path, "/private/frame.nv12")
        for seconds in (0, 3601, True):
            with self.assertRaises(ValueError):
                C.ffmpeg_command("/private/in", "/private/out", seconds)

    def test_fixture_fixed_and_never_native(self):
        self.assertEqual(C.fixed_fixture(), C.fixed_fixture())
        self.assertEqual(C.fixed_fixture()["origin"], "synthetic")
        self.assertFalse(C.fixed_fixture()["hardware"])
        self.assertFalse(C.fixed_fixture()["native_decode_verified"])

    def test_dynamic_starttime_parser(self):
        fields = [b"S"] + [b"0"] * 18 + [b"987654"]
        self.assertEqual(C.process_starttime(b"123 (name with ) space) " + b" ".join(fields)), 987654)
        with self.assertRaises(ValueError):
            C.process_starttime(b"bad-stat")

    def test_private_path_required(self):
        with tempfile.TemporaryDirectory(prefix="public-host-test-") as root:
            with self.assertRaises(ValueError):
                C.checked_directory(root)
            private = Path(root) / "private"
            private.mkdir()
            self.assertEqual(C.checked_directory(private), private)

    def test_capture_has_no_shell_network_or_execution(self):
        source = (HERE / "capture-native.py").read_text()
        self.assertNotIn("subprocess", source.replace("or subprocess", ""))
        self.assertNotIn("socket.", source)
        self.assertNotIn("os.system", source)
        self.assertNotIn("/proc/1502", source)
        self.assertNotIn("/proc/4744", source)
        self.assertNotIn("fcntl", source)


if __name__ == "__main__":
    unittest.main()
