#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline, fail-closed native H264 evidence checking (no device operations).

Evidence schema v1 is deliberately a conditional contract, NOT authentication of
physical hardware. Callers independently pin final version hashes, producer roles
and exact raw-file SHA256 receipts in a trust policy obtained out of band. Never
build that policy from the submitted evidence. JSON envelopes contain schema,
origin, producer {id, sha256}, versions, run_id, boot_id, nonce, kind and payload.
Even a structurally complete, receipt-pinned bundle never sets accepted/native/
hardware true. native_decode_verified describes conditional evidence acceptance
only with a separately supplied supervised-receipts policy; it does not certify
physical origin. Host-fixture policies can exercise every gate without that claim.
All references are relative regular files beneath one evidence directory; no
symlinks, reparse points, traversal, networking, executable loading or shell use.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import struct

SCHEMA = "native-decode-evidence-v1"
# A native client artifact is optional: the actual consumers are mediasrv and
# its bundled FFmpeg. Old bundles may retain an additional diagnostic client.
VERSIONS = ("candidate", "install", "kernel", "boot", "ffi", "ffmpeg", "mediasrv")
GATES = ("versions", "ffmpeg", "mediasrv", "repeat", "continuous", "lifecycle",
         "dma_contract_proof", "dma_reclaim", "negative", "reboot", "health", "backup_rollback")
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
HEX = re.compile(r"^[0-9a-f]{64}$")


class Invalid(ValueError):
    pass


def need(condition, message):
    if not condition:
        raise Invalid(message)


def integer(value, minimum=0):
    return type(value) is int and value >= minimum


def digest(data):
    if isinstance(data, Path):
        h = hashlib.sha256()
        with data.open("rb") as stream:
            before = os.fstat(stream.fileno())
            need(before.st_size <= 1024 * 1024 * 1024, "artifact exceeds 1GiB")
            total = 0
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                need(total <= 1024 * 1024 * 1024, "growing artifact")
                h.update(block)
            after = os.fstat(stream.fileno())
            need((before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns), "artifact changed")
        return h.hexdigest()
    return hashlib.sha256(data).hexdigest()


def strict_json(data):
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out, "duplicate JSON key")
            out[key] = value
        return out
    def constant(value):
        raise Invalid("nonfinite JSON number: " + value)
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


def safe_file(root, relative, limit=MAX_FILE):
    need(isinstance(relative, str) and 0 < len(relative) <= 512, "invalid file path")
    need(not any(x in relative for x in ("\\", ":", "\x00")), "unsafe file path")
    parts = relative.split("/")
    need(all(p not in ("", ".", "..") for p in parts), "unsafe file path")
    base = Path(root).absolute()
    # Check ancestors too: resolving first would conceal a symlink/reparse root.
    for p in list(reversed(base.parents)) + [base]:
        st = p.lstat()
        need(not stat.S_ISLNK(st.st_mode) and not getattr(st, "st_file_attributes", 0) & 0x400,
             "symlink/reparse evidence root")
    path = base
    for part in parts:
        path /= part
        st = path.lstat()
        need(not stat.S_ISLNK(st.st_mode) and not getattr(st, "st_file_attributes", 0) & 0x400,
             "symlink/reparse evidence file")
    need(stat.S_ISREG(st.st_mode), "not a regular evidence file")
    need(st.st_size <= limit, "evidence file too large")
    return path


class Bundle:
    def __init__(self, manifest, root, trust, expected):
        self.manifest, self.root, self.trust, self.expected = manifest, root, trust, expected
        self.cache, self.total = {}, 0
        self.run = manifest["run_id"]
        need(isinstance(self.run, str) and bool(self.run), "missing run identity")
        need(set(VERSIONS) <= set(expected) <= set(VERSIONS) | {"native"}, "externally pinned versions incomplete")
        need(all(isinstance(h, str) and HEX.fullmatch(h) for h in expected.values()), "bad version hash")
        need(manifest["versions"] == expected, "wrong final version")
        need(manifest["schema"] == SCHEMA and manifest["origin"] == "observed", "synthetic/unknown origin")
        need(trust["schema"] == "native-decode-trust-v1", "missing independent trust policy")
        need(trust["mode"] in ("host-fixture", "supervised-receipts"), "unknown trust mode")
        if trust["mode"] == "supervised-receipts":
            need(isinstance(trust["custody_id"], str) and bool(trust["custody_id"]), "external custody identity absent")

    def raw(self, ref, role):
        need(isinstance(ref, dict) and set(ref) == {"path", "sha256"}, "bad evidence reference")
        h = ref["sha256"]
        need(isinstance(h, str) and HEX.fullmatch(h), "bad evidence hash")
        receipt = self.trust["receipts"].get(h)
        need(isinstance(receipt, dict) and receipt.get("role") == role, "untrusted raw evidence/role")
        if self.trust["mode"] == "supervised-receipts":
            need(receipt.get("provenance") == "supervised-device-observation" and
                 receipt.get("custody_id") == self.trust["custody_id"],
                 "receipt lacks independent supervised custody/provenance")
        producer = self.trust["producers"].get(receipt.get("producer"))
        need(isinstance(producer, dict) and role in producer.get("roles", []), "untrusted producer role")
        need(isinstance(producer.get("sha256"), str) and HEX.fullmatch(producer["sha256"]), "producer hash absent")
        binary = role in ("artifact", "backup", "source", "frame")
        path = safe_file(self.root, ref["path"], 1024 * 1024 * 1024 if binary else MAX_FILE)
        key = str(path)
        if binary:
            cache_key = (key, h)
            if cache_key not in self.cache:
                need(digest(path) == h, "raw evidence hash mismatch")
                self.cache[cache_key] = True
            return path, receipt, producer
        if key not in self.cache:
            data = path.read_bytes()
            need(len(data) <= MAX_FILE, "growing evidence file")
            self.total += len(data)
            need(self.total <= MAX_TOTAL, "evidence bundle too large")
            self.cache[key] = data
        data = self.cache[key]
        need(digest(data) == h, "raw evidence hash mismatch")
        return data, receipt, producer

    def envelope(self, ref, role, kind):
        data, receipt, producer = self.raw(ref, role)
        e = strict_json(data)
        need(e["schema"] == SCHEMA and e["origin"] == "observed", "synthetic envelope")
        need(e["kind"] == kind and e["versions"] == self.expected and e["run_id"] == self.run,
             "envelope kind/run/version mismatch")
        need(e["producer"] == {"id": receipt["producer"], "sha256": producer["sha256"]},
             "producer identity mismatch")
        need(isinstance(e["boot_id"], str) and bool(e["boot_id"]) and
             isinstance(e["nonce"], str) and bool(e["nonce"]), "boot/nonce absent")
        need(receipt.get("boot_id") == e["boot_id"] and receipt.get("nonce") == e["nonce"],
             "receipt boot/nonce mismatch")
        return e


def identity(p):
    need(integer(p["pid"], 1) and integer(p["starttime"], 1), "PID/starttime missing")
    need(isinstance(p["jobid"], str) and bool(p["jobid"]), "job ID missing")
    return {k: p[k] for k in ("pid", "starttime", "jobid")}


def ioctl_result(event):
    """Raw libc return + errno (or a negative kernel errno), never booleans."""
    ret = event["return"]
    need(type(ret) is int and ret <= 0, "invalid ioctl return")
    if ret == 0:
        need(type(event.get("errno", 0)) is int and event.get("errno", 0) == 0, "successful ioctl with errno")
        return True
    err = event.get("errno") if ret == -1 else -ret
    need(integer(err, 1) and ("errno" not in event or type(event["errno"]) is int and event["errno"] == err), "negative ioctl errno absent/inconsistent")
    request = event["request"]
    need((request in ("VIDIOC_DQBUF", "VIDIOC_DQEVENT") and err == 11) or
         (request == "VIDIOC_DQEVENT" and err == 2), "unexpected negative ioctl")
    need(not any(k in event for k in ("event", "buffer", "frame", "source_change")),
         "empty nonblocking result carries an event/buffer")
    return False


def check_capture_timeline(p, events, oracle):
    """Exact OUTPUT setup then one CAPTURE S_FMT per SOURCE_CHANGE generation.

    Initial CAPTURE generation 0 can be provisional: no frames are invented for
    it. Each change must name the next generation, stop old CAPTURE, configure
    NV12 with the announced geometry, and resume before new-generation frames.
    OUTPUT remains streaming during DRC. Repeated arbitrary S_FMT is forbidden.
    """
    generation = 0
    output_fmt = capture_fmt = False
    streaming = {"OUTPUT": False, "CAPTURE": False}
    phase = "initial"
    pending = None
    geometry = None
    seen_frames = []
    generations = p["capture_generations"]
    need(isinstance(generations, list) and bool(generations), "capture generation oracle missing")
    by_gen = {}
    for spec in generations:
        g = spec["generation"]
        need(integer(g) and g not in by_gen, "duplicate/invalid oracle generation")
        lo, hi = spec["oracle_range"]
        need(integer(lo) and integer(hi, lo) and hi < len(oracle["frames"]), "generation oracle range invalid")
        by_gen[g] = oracle["frames"][lo:hi + 1]
    cursors = {g: 0 for g in by_gen}
    changes = []
    frame_records = []
    for event in events:
        op = event["op"]
        if op == "ioctl":
            if not ioctl_result(event):
                continue
            request = event["request"]
            need("event" not in event or request == "VIDIOC_DQEVENT", "event on non-event ioctl")
            if request == "VIDIOC_DQEVENT" and event.get("event") == "V4L2_EVENT_SOURCE_CHANGE":
                need(event["changes"] == ["RESOLUTION"] and phase == "running" and
                     event["generation"] == generation + 1 and streaming["OUTPUT"],
                     "SOURCE_CHANGE generation/order mismatch")
                size = event["size"]
                need(len(size) == 2 and all(integer(x, 2) and x % 2 == 0 for x in size), "bad source-change size")
                pending = size
                phase = "change-stop" if streaming["CAPTURE"] else "change-format"
                changes.append(event)
            elif request == "VIDIOC_S_FMT":
                queue = event["queue"]
                need(queue in streaming and event["generation"] == (generation + 1 if pending else generation),
                     "S_FMT generation mismatch")
                if queue == "OUTPUT":
                    need(not output_fmt and not capture_fmt and phase == "initial" and event["fourcc"] == "H264",
                         "source/capture codec ioctl mismatch")
                    output_fmt = True
                else:
                    need(output_fmt and not streaming["CAPTURE"] and event["fourcc"] == "NV12" and
                         ((not capture_fmt and phase == "initial") or phase == "change-format"),
                         "unexpected CAPTURE S_FMT/reconfiguration")
                    size = event["size"]
                    need(len(size) == 2 and all(integer(x, 2) and x % 2 == 0 for x in size), "bad capture geometry")
                    if pending:
                        need(size == pending, "S_FMT does not match SOURCE_CHANGE")
                        generation += 1
                        pending = None
                    geometry = size
                    capture_fmt = True
                    phase = "configured"
            elif request in ("VIDIOC_STREAMON", "VIDIOC_STREAMOFF"):
                queue = event["queue"]
                need(queue in streaming, "unknown streaming queue")
                need(event["generation"] == generation, "stream generation mismatch")
                if request == "VIDIOC_STREAMON":
                    need(not streaming[queue] and capture_fmt, "duplicate/premature STREAMON")
                    if queue == "CAPTURE":
                        need(phase == "configured", "CAPTURE resume without exact configuration")
                        phase = "running"
                    streaming[queue] = True
                else:
                    need(streaming[queue], "STREAMOFF without STREAMON")
                    if queue == "CAPTURE" and phase == "change-stop":
                        phase = "change-format"
                    else:
                        need(pending is None, "DRC output stopped before capture reconfiguration")
                    streaming[queue] = False
            elif request == "VIDIOC_DQEVENT":
                need(event.get("event") in ("V4L2_EVENT_EOS",), "unknown successful DQEVENT")
        elif op == "frame":
            need(phase == "running" and all(streaming.values()) and event["generation"] == generation,
                 "frame before stream resume/wrong capture generation")
            need(geometry == [event["width"], event["height"]], "frame not negotiated capture geometry")
            need(generation in by_gen and cursors[generation] < len(by_gen[generation]), "unoracled generation/frame")
            expected = by_gen[generation][cursors[generation]]
            need({k: event[k] for k in ("pts", "sha256", "width", "height")} == expected, "frame oracle/PTS mismatch")
            cursors[generation] += 1
            seen_frames.append(generation)
            frame_records.append(event)
    need(pending is None and phase != "change-format" and not any(streaming.values()),
         "incomplete capture reconfigure/stream teardown")
    need(set(by_gen) == set(seen_frames) and all(cursors[g] == len(by_gen[g]) for g in by_gen),
         "missing/extra generation frames")
    need(all(a["mono_ns"] < c["mono_ns"] and
             (a["generation"] != c["generation"] or a["pts"] < c["pts"])
             for a, c in zip(frame_records, frame_records[1:])), "nonmonotonic generation PTS/capture clock")
    return changes


def check_session(b, ref):
    e = b.envelope(ref, "decode", "session")
    p = e["payload"]
    ident = identity(p)
    need(e["nonce"] == p["session_id"], "session nonce mismatch")
    kind = p["consumer"]
    need(kind in ("ffmpeg", "mediasrv", "native"), "unknown consumer")
    executable_kind = p.get("executable_kind", kind)
    need(executable_kind == kind or kind == "mediasrv" and executable_kind == "ffmpeg",
         "task executable role mismatch")
    need(p["executable_sha256"] == b.expected[executable_kind] and p["ffi_sha256"] == b.expected["ffi"],
         "running executable/FFI version mismatch")
    node = p["node"]
    need(re.fullmatch(r"/dev/video[0-9]+", node["path"]) and integer(node["major"], 1) and
         integer(node["minor"]), "invalid character node dev_t")
    need(node["type"] == "character", "not a character device")
    source, _, _ = b.raw(p["source"], "source")
    need(source.stat().st_size > 0, "empty input")
    oracle = b.envelope(p["oracle"], "oracle", "frame-oracle")["payload"]
    need(oracle["source_sha256"] == p["source"]["sha256"] and oracle["codec"] == "h264", "oracle/source mismatch")
    tb = oracle["timebase"]
    need(len(tb) == 2 and all(integer(x, 1) for x in tb), "invalid timebase")
    need(oracle["producer_method"] in ("independent-reference-decode", "known-answer-NV12"), "frame oracle not independent")
    # Each output generation carries its own independent oracle range. PTS can
    # restart after seek; cross-generation clocks still cannot go backwards.
    events = p["events"]
    need(isinstance(events, list) and 9 <= len(events) <= 100000, "events missing/oversized")
    seqs, times = [], []
    for event in events:
        need(event["identity"] == ident and event["dev_t"] == [node["major"], node["minor"]],
             "event PID/starttime/job/dev_t mismatch")
        need(integer(event["seq"]) and integer(event["mono_ns"]), "event clock absent")
        seqs.append(event["seq"])
        times.append(event["mono_ns"])
    need(all(a < c for a, c in zip(seqs, seqs[1:])) and times == sorted(times), "event order invalid")
    need(seqs == list(range(seqs[0], seqs[0] + len(seqs))), "trace gaps")
    need(events[0]["op"] == "exec" and events[-1]["op"] == "exit" and events[-1]["status"] == 0,
         "incomplete execution")
    need(events[0]["exe_sha256"] == p["executable_sha256"], "exec mismatch")
    argv = events[0]["argv"]
    need(isinstance(argv, list) and all(isinstance(a, str) for a in argv), "argv absent")
    if executable_kind == "ffmpeg":
        need("-device" in argv and argv[argv.index("-device") + 1] == node["path"] and
             "-capture_pix_fmt" in argv and argv[argv.index("-capture_pix_fmt") + 1] == "nv12",
             "actual decoder device/capture format not pinned")
        need("-i" in argv and "-c:v" in argv and argv.index("-c:v") < argv.index("-i") and
             argv[argv.index("-c:v") + 1] == "h264_v4l2m2m" and
             not any(x in argv for x in ("-decoders", "-encoders", "-hwaccels", "-version")),
             "not an actual forced H264 V4L2 decode command")
    opened = [x for x in events if x["op"] == "open"]
    need(len(opened) == 1 and opened[0]["node"] == node["path"], "node open absent")
    changes = check_capture_timeline(p, events, oracle)
    decoders = [x for x in events if x["op"] == "decoder-open"]
    need(len(decoders) == 1 and decoders[0]["codec"] == "h264_v4l2m2m" and
         decoders[0]["backend"] == "v4l2-m2m" and decoders[0]["direction"] == "decode",
         "CPU fallback/other decoder or backend")
    allowed = {"exec", "open", "ioctl", "decoder-open", "frame", "decoder-close", "close", "exit",
               "submit", "submit-disabled", "buffer-release"}
    need(all(x["op"] in allowed for x in events), "unknown/fallback event")
    frames = [x for x in events if x["op"] == "frame"]
    need(len(frames) >= 3, "insufficient decoded frames")
    for f in frames:
        need(integer(f["pts"]) and integer(f["width"], 2) and integer(f["height"], 2) and
             f["width"] % 2 == 0 and f["height"] % 2 == 0, "invalid frame geometry/PTS")
        raw, _, _ = b.raw(f["raw"], "frame")
        need(f["raw"]["sha256"] == f["sha256"] and raw.stat().st_size == f["width"] * f["height"] * 3 // 2,
             "raw NV12 frame mismatch")
        need(f["decoder_seq"] == decoders[0]["seq"] and f["source_sha256"] == p["source"]["sha256"],
             "frame decoder/input mismatch")
    # Capture state machine validates PTS within each generation. A discontinuity
    # cannot qualify as an uninterrupted 600-second continuous decode below.
    positions = {op: [i for i, x in enumerate(events) if x["op"] == op] for op in allowed}
    need(positions["decoder-close"] and positions["close"] and
         positions["open"][0] < positions["ioctl"][0] < positions["decoder-open"][0] < positions["frame"][0] and
         positions["frame"][-1] < positions["decoder-close"][0] < positions["close"][0], "decode lifetime order")
    coverage = p["coverage"]
    need(coverage == {"first_seq": seqs[0], "last_seq": seqs[-1], "decoder_opens": len(decoders),
                      "decoded_frames": len(frames), "dropped_events": 0}, "coverage incomplete")
    media_seconds = (frames[-1]["pts"] - frames[0]["pts"]) * tb[0] / tb[1]
    elapsed = (frames[-1]["mono_ns"] - frames[0]["mono_ns"]) / 1e9
    continuous = (not changes and media_seconds >= 600 and elapsed >= 600 and len(frames) >= 600 and
                  all((c["mono_ns"] - a["mono_ns"]) <= 2_000_000_000 and
                      (c["pts"] - a["pts"]) * tb[0] / tb[1] <= 2
                      for a, c in zip(frames, frames[1:])))
    if kind == "mediasrv":
        task = b.envelope(p["task"], "media-task", "real-media-task")
        need(task["boot_id"] == e["boot_id"], "task boot mismatch")
        t = task["payload"]
        need(identity(t["decode_process"]) == ident, "task decode process mismatch")
        identity(t["server_process"])
        need(t["server_process"]["executable_sha256"] == b.expected["mediasrv"], "task server version mismatch")
        request, _, _ = b.raw(t["request"], "media-request")
        r = strict_json(request)
        need(r["jobid"] == ident["jobid"] and r["source_sha256"] == p["source"]["sha256"], "actual task request mismatch")
        need(r["operation"] in ("playback-transcode", "transcode", "decode") and
             r["transport"] in ("http", "rpc", "unix") and isinstance(r["entrypoint"], str) and
             0 < len(r["entrypoint"]) < 512, "real task entry unknown")
        need(t["events"] == ["request-received", "job-created", "decoder-attached", "job-completed"] and
             t["decoder"] == "h264_v4l2m2m" and t["source_sha256"] == p["source"]["sha256"], "task not complete/native")
    return {"id": p["session_id"], "identity": ident, "boot": e["boot_id"], "nonce": e["nonce"],
            "consumer": kind, "continuous": continuous, "first_ns": times[0], "last_ns": times[-1],
            "events": events, "changes": changes, "node": node}


def object_key(obj):
    need(isinstance(obj["buffer_id"], str) and bool(obj["buffer_id"]), "buffer identity absent")
    for key in ("iova", "bytes", "submission_id", "generation", "session_cookie"):
        need(integer(obj[key], 1 if key in ("bytes", "submission_id", "session_cookie") else 0), "invalid DMA object " + key)
    return tuple(obj[k] for k in ("buffer_id", "iova", "bytes", "submission_id", "generation", "session_cookie"))


def check_dma_generation_proof(b, s, reclaim):
    """No mapping removal/ACK/idle string is a DMA-safety contract.

    A separately pinned exact source AND provider must define either a specific
    HFI packet returning each submitted buffer's ownership, or exact platform
    MMIO isolation predicates. Proof bytes must be independently receipted from
    that provider. This validates evidence under that external platform contract,
    not arbitrary trace authentication or validity of a new hardware guarantee.
    """
    dma = reclaim["dma"]
    need(dma["proof_type"] in ("hfi-buffer-ownership-return", "platform-mmio-isolation"),
         "ACK-only/unmap/unknown DMA proof is not sufficient")
    pin = b.trust["dma_contracts"][dma["contract_id"]]
    contract_bytes, _, _ = b.raw(dma["contract"], "platform-contract")
    contract = strict_json(contract_bytes)
    need(dma["contract"]["sha256"] == pin["contract_sha256"] and
         contract["schema"] == "native-dma-platform-contract-v1" and
         contract["proof_type"] == dma["proof_type"] == pin["proof_type"] and
         contract["platform"] == pin["platform"] == "qcom,sm8450/xiaomi,cupid" and
         contract["provider"] == pin["provider"], "platform contract/provider not independently pinned")
    b.raw(contract["source"], "contract-source")
    need(contract["source"]["sha256"] == pin["source_sha256"] and
         isinstance(contract["source_locator"], str) and bool(contract["source_locator"]), "exact contract source absent")
    proof = b.envelope(dma["evidence"], "dma-provider", "dma-ownership-proof")
    need(proof["producer"] == pin["provider"] and proof["boot_id"] == s["boot"] and
         proof["nonce"] == s["nonce"], "DMA provider boot/session mismatch")
    p = proof["payload"]
    need(p["contract_sha256"] == pin["contract_sha256"] and p["source_sha256"] == pin["source_sha256"] and
         p["identity"] == s["identity"] and p["session_id"] == s["id"] and
         p["dev_t"] == [s["node"]["major"], s["node"]["minor"]], "DMA proof object/session provenance mismatch")
    generation = p["generation"]
    need(integer(generation), "DMA submit generation missing")
    events = s["events"]
    disabled = [x for x in events if x["op"] == "submit-disabled" and x["generation"] == generation]
    submits = [x for x in events if x["op"] == "submit" and x["generation"] == generation]
    releases = [x for x in events if x["op"] == "buffer-release" and x["generation"] == generation]
    need(len(disabled) == 1 and bool(submits), "exact submit-disable generation absent")
    stop = disabled[0]
    need(all(x["seq"] < stop["seq"] for x in submits) and
         all(x["seq"] > stop["seq"] for x in releases), "submitted generation resumes/releases too early")
    keys = [object_key(x["object"]) for x in submits]
    returned = [object_key(x["object"]) for x in releases]
    need(len(keys) == len(set(keys)) and sorted(keys) == sorted(returned) and
         {k[0] for k in keys} == set(reclaim["allocated_ids"]), "DMA objects missing/duplicate/unmatched")
    need(all(x["object"]["generation"] == generation for x in submits + releases), "DMA object generation mismatch")
    need(not any(x["op"] == "submit" and x["mono_ns"] >= stop["mono_ns"] and
                 x["generation"] == generation for x in events), "object resubmitted after generation closure")
    records = p["records"]
    need(isinstance(records, list) and bool(records), "DMA raw proof records absent")
    seqs = [x["seq"] for x in records]
    need(all(integer(x) for x in seqs) and seqs == list(range(seqs[0], seqs[0] + len(seqs))), "DMA proof record gaps/replay")
    need(p["coverage"] == {"first_seq": seqs[0], "last_seq": seqs[-1], "dropped_records": 0,
                           "session_first_seq": events[0]["seq"], "session_last_seq": events[-1]["seq"]},
         "DMA proof/submit coverage incomplete")
    times = [x["mono_ns"] for x in records]
    need(all(integer(x) for x in times) and all(a < c for a, c in zip(times, times[1:])) and
         stop["mono_ns"] < times[0] <= times[-1] < min(x["mono_ns"] for x in releases), "DMA proof after release/before submit closure")
    # Legacy annotation records may accompany the proof, but never establish it.
    if "records" in dma:
        ts = [x["mono_ns"] for x in dma["records"]]
        need(all(integer(x) for x in ts) and all(a < c for a, c in zip(ts, ts[1:])), "DMA annotation order invalid")
    if dma["proof_type"] == "hfi-buffer-ownership-return":
        rule = contract["ownership_return"]
        need(rule["meaning"] == "device-ownership-return-no-further-access" and
             isinstance(rule["packet_type"], str) and rule["packet_type"] not in ("ACK", "SESSION_ACK", "COMMAND_ACK") and
             isinstance(rule["buffer_type"], str) and bool(rule["buffer_type"]), "contract only promises ACK")
        need(len(records) == len(keys), "not per-buffer HFI return")
        matched = []
        for r in records:
            need(r["op"] == "hfi-buffer-return" and r["packet_type"] == rule["packet_type"] and
                 r["buffer_type"] == rule["buffer_type"] and type(r["status"]) is int and r["status"] == 0 and
                 r["owner_after"] == "host" and integer(r["packet_id"], 1), "HFI ownership return not contract packet")
            raw, receipt, producer = b.raw(r["packet"], "hfi-packet")
            need(receipt["producer"] == pin["provider"]["id"] and producer["sha256"] == pin["provider"]["sha256"] and
                 receipt["boot_id"] == s["boot"] and receipt["nonce"] == s["nonce"], "HFI raw packet provider/session mismatch")
            need(rule["wire_layout"] == "le-4u32-2u64-4u32" and len(raw) == 48, "unknown HFI wire layout")
            values = struct.unpack("<IIIIQQIIII", raw)
            obj = r["object"]
            need(values == (rule["packet_type_id"], r["packet_id"], obj["session_cookie"], rule["buffer_type_id"],
                 obj["iova"], obj["bytes"], obj["submission_id"], obj["generation"], 0, rule["host_owner_code"]),
                 "raw HFI packet not exact object ownership return")
            matched.append(object_key(r["object"]))
        need(sorted(matched) == sorted(keys) and len({r["packet_id"] for r in records}) == len(records),
             "HFI return cookie/address/submission mismatch or packet replay")
    else:
        rule = contract["isolation"]
        need(rule["meaning"] == "bus-master-isolated-no-further-access" and
             rule["scope"] == "all-submitted-session-buffers" and rule["registers"], "MMIO contract does not prove isolation")
        need(p["objects"] == [x["object"] for x in submits] and len(records) == len(rule["registers"]), "MMIO object/snapshot coverage absent")
        snapshot, receipt, producer = b.raw(p["snapshot"], "mmio-snapshot")
        need(receipt["producer"] == pin["provider"]["id"] and producer["sha256"] == pin["provider"]["sha256"] and
             receipt["boot_id"] == s["boot"] and receipt["nonce"] == s["nonce"], "MMIO raw provider/session mismatch")
        need(rule["wire_layout"] == "le-u64-address-u32-value" and len(snapshot) == 12 * len(records),
             "MMIO raw snapshot/layout incomplete")
        samples = list(struct.iter_unpack("<QI", snapshot))
        need(samples == [(r["address"], r["value"]) for r in records], "MMIO records not raw snapshot")
        for r, reg in zip(records, rule["registers"]):
            need(r["op"] == "mmio-read" and r["address"] == reg["address"] and
                 all(integer(v) for v in (r["value"], reg["address"], reg["mask"], reg["expected"])) and
                 reg["mask"] > 0 and r["value"] & reg["mask"] == reg["expected"], "MMIO exact isolation predicate mismatch")


def check_dma_proof(b, s, reclaim):
    dma = reclaim["dma"]
    refs = dma["evidence"] if isinstance(dma["evidence"], list) else [dma["evidence"]]
    need(bool(refs), "DMA generation proofs missing")
    observed = {x["generation"] for x in s["events"] if x["op"] == "submit"}
    covered, allocated = set(), set()
    for ref in refs:
        payload = b.envelope(ref, "dma-provider", "dma-ownership-proof")["payload"]
        g = payload["generation"]
        need(g not in covered and g in observed, "duplicate/unsubmitted DMA proof generation")
        covered.add(g)
        ids = {x["object"]["buffer_id"] for x in s["events"] if x["op"] == "submit" and x["generation"] == g}
        allocated |= ids
        partial = dict(reclaim, allocated_ids=sorted(ids), dma=dict(dma, evidence=ref))
        check_dma_generation_proof(b, s, partial)
    need(covered == observed and allocated == set(reclaim["allocated_ids"]), "DMA generation/allocated coverage incomplete")


def check_lifecycle(b, sessions):
    envelope = b.envelope(b.manifest["lifecycle"], "lifecycle", "lifecycle")
    p = envelope["payload"]
    cases = p["cases"]
    need({x["case"] for x in cases} == {"seek", "flush", "drc", "error_cancel"} and len(cases) == 4,
         "seek/flush/DRC/error cancellation missing")
    for c in cases:
        s = sessions[c["session_id"]]
        need(s["consumer"] in ("mediasrv", "ffmpeg", "native") and c["identity"] == s["identity"] and c["boot_id"] == s["boot"] == envelope["boot_id"],
             "lifecycle not exact native session")
        records = c["records"]
        patterns = {"seek": ["seek-request", "flush-done", "frame-after-seek"],
                    "flush": ["flush-request", "queues-drained", "decode-resumed"],
                    "drc": ["source-change", "capture-reconfigured", "frame-new-resolution"],
                    "error_cancel": ["input-error", "cancel-request", "cancel-complete"]}
        need([r["op"] for r in records] == patterns[c["case"]], "lifecycle payload unknown/skipped")
        need(all(integer(r["mono_ns"]) for r in records) and
             s["first_ns"] <= records[0]["mono_ns"] < records[1]["mono_ns"] < records[2]["mono_ns"] <= s["last_ns"],
             "lifecycle clock/session mismatch")
        if c["case"] == "seek":
            need(integer(records[0]["target_pts"]) and records[2]["pts"] == records[0]["target_pts"] and
                 HEX.fullmatch(records[2]["frame_sha256"]), "seek result absent")
            frame = next(x for x in s["events"] if x["seq"] == records[2]["event_seq"])
            need(frame["op"] == "frame" and frame["pts"] == records[2]["pts"] and
                 frame["sha256"] == records[2]["frame_sha256"] and frame["mono_ns"] == records[2]["mono_ns"],
                 "seek result not linked to captured oracle frame")
        elif c["case"] == "flush":
            need(records[1]["queued_input"] == 0 and records[1]["queued_capture"] == 0 and
                 records[2]["generation"] == records[0]["generation"] + 1, "flush did not drain/restart")
        elif c["case"] == "drc":
            old, new = records[0]["old_size"], records[1]["new_size"]
            need(old != new and all(integer(x, 2) for x in old + new) and records[2]["size"] == new and
                 HEX.fullmatch(records[2]["frame_sha256"]), "DRC frame result absent")
            change = next((x for x in s["changes"] if x["seq"] == records[0]["event_seq"]), None)
            need(change is not None and change["mono_ns"] == records[0]["mono_ns"] and
                 change["generation"] == records[1]["generation"], "DRC lifecycle not linked to SOURCE_CHANGE")
            fmt = next(x for x in s["events"] if x["seq"] == records[1]["event_seq"])
            frame = next(x for x in s["events"] if x["seq"] == records[2]["event_seq"])
            need(fmt["op"] == "ioctl" and fmt["request"] == "VIDIOC_S_FMT" and fmt["queue"] == "CAPTURE" and
                 fmt["size"] == new and fmt["generation"] == change["generation"] and
                 fmt["mono_ns"] == records[1]["mono_ns"] and frame["op"] == "frame" and
                 frame["generation"] == change["generation"] and frame["sha256"] == records[2]["frame_sha256"] and
                 frame["mono_ns"] == records[2]["mono_ns"], "DRC lifecycle frame/format not exact trace")
        else:
            need(integer(records[0]["errno"], 1) and records[2]["result"] == "cancelled", "error cancellation absent")
    reclaim = p["reclaim"]
    need({r["session_id"] for r in reclaim} == set(sessions) and len(reclaim) == len(sessions),
         "per-session resource reclaim missing")
    for r in reclaim:
        s = sessions[r["session_id"]]
        need(r["identity"] == s["identity"] and r["boot_id"] == s["boot"], "reclaim session mismatch")
        alloc, release = r["allocated_ids"], r["released_ids"]
        need(bool(alloc) and len(alloc) == len(set(alloc)) and sorted(alloc) == sorted(release) and
             r["outstanding_ids"] == [] and r["open_fds_after"] == r["open_fds_before"], "resources leaked/unknown")
        check_dma_proof(b, s, r)


def check_negative(b):
    p = b.envelope(b.manifest["negative"], "negative", "hardware-absent")["payload"]
    identity(p)
    need(p["executable_sha256"] == b.expected[p.get("executable_kind", "native")] and p["ffi_sha256"] == b.expected["ffi"], "negative version mismatch")
    need(p["node_inventory"] == [] and p["open_errno"] == 2 and p["exit_code"] != 0 and
         integer(p["exit_code"], 1) and p["frames"] == [] and p["decoder_dispatches"] == [] and
         p["result"] == "hardware-absent-refused", "absent hardware silently decoded/fell back")


def check_reboot(b, sessions):
    envelope = b.envelope(b.manifest["reboot"], "reboot", "reboot-retest")
    p = envelope["payload"]
    need(envelope["boot_id"] == p["before_boot_id"], "reboot producer boot mismatch")
    need(p["before_boot_id"] != p["after_boot_id"], "no reboot")
    for field, boot in (("before_sessions", p["before_boot_id"]), ("after_sessions", p["after_boot_id"])):
        ss = [sessions[sid] for sid in p[field]]
        need({"ffmpeg", "mediasrv"} <= {s["consumer"] for s in ss} and
             all(s["boot"] == boot for s in ss), "reboot native/FFmpeg/real task retest missing")
    return p


def check_health(b, reboot):
    p = b.envelope(b.manifest["health"], "health", "original-system-health")["payload"]
    snapshots = p["snapshots"]
    need([s["stage"] for s in snapshots] == ["baseline", "post-reboot", "post-rollback"], "health stages incomplete")
    for s in snapshots:
        need(set(s["services"]) >= {"mediasrv", "trim-media", "storage", "network", "web"} and
             all(v == "active" for v in s["services"].values()) and s["faults"] == [] and
             s["storage_read_sha256"] == snapshots[0]["storage_read_sha256"] and
             HEX.fullmatch(s["storage_read_sha256"]), "original system unhealthy/unknown")
    need(snapshots[0]["boot_id"] == reboot["before_boot_id"] and
         snapshots[1]["boot_id"] == reboot["after_boot_id"], "health reboot mismatch")


def check_deploy(b):
    p = b.envelope(b.manifest["deployment"], "deployment", "backup-rollback")["payload"]
    backup, _, _ = b.raw(p["backup"], "backup")
    readback, _, _ = b.raw(p["rollback_readback"], "backup")
    h = p["backup"]["sha256"]
    need(p["rollback_readback"]["sha256"] == h and p["host_sha256"] == p["device_sha256"] == p["baseline_sha256"] == h,
         "backup dual-end/readback hash mismatch")
    need(p["installed_versions"] == b.expected, "install different candidate")
    stages = p["records"]
    need([x["op"] for x in stages] == ["backup-read", "dual-hash-verified", "install-verified", "rollback-readback-verified"],
         "backup/rollback actual records incomplete")
    need(all(integer(x["mono_ns"]) for x in stages) and
         all(a["mono_ns"] < c["mono_ns"] for a, c in zip(stages, stages[1:])), "backup after installation")
    need(stages[2]["install_sha256"] == b.expected["install"] and stages[3]["readback_sha256"] == h,
         "installation/rollback payload mismatch")


def validate(manifest, root, trust, expected_versions):
    result = {"schema": "native-decode-verdict-v1", "accepted": False, "hardware": False, "native": False,
              "conditional_accepted": False, "native_decode_verified": False,
              "origin_authenticated": False, "physical_proof": False,
              "scope": "conditional offline evidence acceptance under externally pinned receipts/producers; "
              "not physical-source authentication or real hardware acceptance", "gates": {}, "errors": []}
    try:
        b = Bundle(manifest, root, trust, expected_versions)
        for name in expected_versions:
            raw, _, _ = b.raw(manifest["artifacts"][name], "artifact")
            need(manifest["artifacts"][name]["sha256"] == expected_versions[name], "artifact version mismatch: " + name)
        result["gates"]["versions"] = "conditional-pass"
        sessions = {}
        for ref in manifest["sessions"]:
            s = check_session(b, ref)
            need(isinstance(s["id"], str) and s["id"] not in sessions, "duplicate session")
            need(not any((s["boot"], s["identity"]) ==
                         (x["boot"], x["identity"]) or
                         (s["boot"], s["identity"]["jobid"]) == (x["boot"], x["identity"]["jobid"])
                         for x in sessions.values()), "replayed process/job session")
            sessions[s["id"]] = s
        for kind in ("ffmpeg", "mediasrv"):
            need(any(s["consumer"] == kind for s in sessions.values()), "missing actual " + kind + " decode")
            result["gates"][kind] = "conditional-pass"
        native = list(sessions.values())
        need(len(native) >= 30, "fewer than 30 native repeat sessions")
        result["gates"]["repeat"] = "conditional-pass"
        need(any(s["continuous"] for s in native), "native continuous decode shorter than 600 seconds/gappy")
        result["gates"]["continuous"] = "conditional-pass"
        check_lifecycle(b, sessions)
        result["gates"]["lifecycle"] = result["gates"]["dma_reclaim"] = result["gates"]["dma_contract_proof"] = "conditional-pass"
        check_negative(b)
        result["gates"]["negative"] = "conditional-pass"
        reboot = check_reboot(b, sessions)
        result["gates"]["reboot"] = "conditional-pass"
        check_health(b, reboot)
        result["gates"]["health"] = "conditional-pass"
        check_deploy(b)
        result["gates"]["backup_rollback"] = "conditional-pass"
        result["conditional_accepted"] = True
        result["native_decode_verified"] = trust["mode"] == "supervised-receipts"
    except (Invalid, KeyError, TypeError, ValueError, OSError, IndexError, OverflowError, RecursionError, StopIteration) as exc:
        result["errors"].append(str(exc) or type(exc).__name__)
    for gate in GATES:
        result["gates"].setdefault(gate, "not-accepted")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True)
    parser.add_argument("--manifest", required=True, help="relative evidence filename")
    parser.add_argument("--trust", required=True, help="independent host trust policy")
    parser.add_argument("--expected", required=True, help="independently pinned final-version JSON")
    args = parser.parse_args()
    try:
        manifest = strict_json(safe_file(args.root, args.manifest).read_bytes())
        def load_external(path):
            p = Path(path).absolute()
            return strict_json(safe_file(p.parent, p.name).read_bytes())
        result = validate(manifest, args.root, load_external(args.trust), load_external(args.expected))
    except (OSError, ValueError, TypeError, RecursionError, StopIteration) as exc:
        result = {"accepted": False, "hardware": False, "native": False, "conditional_accepted": False,
                  "errors": [str(exc)], "scope": "offline only"}
    print(json.dumps(result, sort_keys=True))
    # Never emit a success exit code mistaken for unconditional hardware acceptance.
    return 2 if result["conditional_accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
