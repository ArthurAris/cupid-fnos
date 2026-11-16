#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Fail-closed FUTURE native media job helper; default is prepare-only, no I/O.

Run on a Linux host with python3 -I -B; no login, enumeration, media DB writes,
fixture generation, device commands, settings changes or implicit requests.
Only existing testsrc2 fixtures in TESTDIR are readable. Main must first authorize
that directory in NAS settings and register the fixture through the real UI.

Sources: docs/fnos-native-decode-progress-20261115.md, saved frontend audit
0504b1159c884d399b41f45444c16fbe (stats callsite offset 405591, reqid below),
4787e9912182491ba3355fc840e59351 (play callsite offset 567360), frontend
14a77ac952fc83b785b65592c335e16d-BGXwY7Sz.js (decode enum offset 160883).
X-Trim-Client=web/X-Trim-Client-Version=631 is the caller-reported correction,
NOT source-verified in available local raw material. The saved NAS web frontend
external-reference/NAS-web-frontend-index-BERCugxW.js (SHA256 3e018b30dc8491601bbf1872d3034a255a633fcf89a40c99d0bc47fd1c669228)
has neither exact client-header string; its four 631 occurrences are SVG numbers.
The audited trim.media module is a different raw file, not saved under its exact
filename anywhere in the historical external evidence inventory (including ignored files). Both audit
scripts identify its audit-time phone path and pin its raw SHA in HEADER_SOURCE;
their summaries pin those scripts, but stdout contains only selected snippets.
The 0504 audit's {version:`631`} is a language-resource URL helper argument,
not an HTTP header. The 4787 audit only records Authorization merge.
Real execution is blocked ONLY by the missing exact module header assignment
(raw hash + byte offset/quote), not an additional manual producer approval.
Injected fake transports exercise conditional mechanics, not source verification.
localhost:8005/api/version is caller-pinned. Native POST route mappings and
decoder JSON pointer require main's actual authorized trace, never URL guessing.
Request budget includes ONE version preflight + ONE play + 1..3 stats = 3..5
HTTP requests maximum, retries zero; preparation issues zero requests.

Approval schema is emitted by default. --prepare-only --approval-file checks
raw response/trace receipts and local fixture pins without HTTP or auth reads.
--execute additionally needs separate external pins, Linux process identity,
an exact private 0600 JSON auth file {"Authorization": "<MC-token>"}, and output
beneath a private 0700 directory. Never supply a password/login file.

Receipt producer role is ONLY task-api-receipt; actual ioctl/frame/DMA trace
producer implementation is unknown and belongs to main. Backend method 6 is
conditional task evidence, NOT physical/native acceptance or no-CPU-fallback
proof. Encoding is independent; hardware encoding is not required. An issued
job may remain active: no guessed stream/quit URL, cancellation or retry.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import threading
import time
from urllib.parse import urlsplit

ORIGIN = "http://localhost:8005"
VERSION_URL = ORIGIN + "/api/version"
PLAY = "/v/api/v1/play/play"
STATS = "/v/api/v1/media/p"
TESTDIR = "/vol2/cupid-native-decode-test-20261115"
FIXTURES = (TESTDIR + "/h264-baseline-128.mp4", TESTDIR + "/h264-baseline-720p.mp4")
REQUIRED = ("media_guid", "video_guid", "video_encoder", "resolution", "bitrate",
            "startTimestamp", "audio_encoder", "audio_guid", "subtitle_guid", "channels")
OPTIONAL = ("forced_sdr", "without_stream")
# Caller-reported correction only; equality against this proposal is NOT provenance.
HEADERS = {"Content-Type": "application/json", "X-Trim-Client": "web", "X-Trim-Client-Version": "631"}
HEADER_SOURCE = {
    "verified": False,
    "status": "blocked: audited trim.media raw module absent locally; saved NAS web raw contains no client header assignment",
    "proposal_origin": "caller-reported correction; not source-verified",
    "local_raw_checked": {
        "path": "external-reference/NAS-web-frontend-index-BERCugxW.js",
        "sha256": "3e018b30dc8491601bbf1872d3034a255a633fcf89a40c99d0bc47fd1c669228",
        "bytes": 2889074,
        "exact_header_string_counts": {"X-Trim-Client-Version": 0, "X-Trim-Client": 0},
        "631_byte_offsets": [2532268, 2535073, 2537486, 2540694],
        "631_context": "all SVG decimal coordinates; no header assignment",
    },
    "audited_raw_required": {
        "phone_path_at_audit": "/usr/local/apps/@appcenter/trim.media/static/assets/14a77ac952fc83b785b65592c335e16d-BGXwY7Sz.js",
        "sha256": "e244e703491c58f2e354be13a2dc00a6354ad352bb6519e90d7e28a188d9f0a6",
        "local_exact_filename_search": "historical external inventory including hidden/ignored files; no matching raw file",
        "header_assignment_byte_offset": None,
        "header_assignment_exact_quote": None,
        "availability": "audit-time phone source only; current phone state not queried",
    },
    "audit_script_pins": {
        "phone-native-request-contract.sh": "6fa92404231bb8fe996539f1d79626e876f4d881a71238bde53a3aa141a097b6",
        "phone-native-play-calls.sh": "3884b8821cd5a4f49b1a8f2360feeeb265f4954b2bbfe097dc51ecd40b795688",
    },
    "audits": {
        "iris-native-audit-0504b1159c884d399b41f45444c16fbe/stdout.txt":
            "8d044c0ea39fd47d237d07e1439da864c22a32208d23b4cc8d9daaa93155b2d3",
        "iris-native-audit-4787e9912182491ba3355fc840e59351/stdout.txt":
            "c5f996153d3e74aba6092eeb144680b3582495834519c30b8bac975d590a3bf9",
    },
    "observed_only": "0504 line8 offset124230 URL helper {version:`631`}; 4787 line6 offset168773 Authorization merge",
}
REQID = "1234567890ABCDEF"
LIMIT = 1024 * 1024
HEX = re.compile(r"[0-9a-f]{64}\Z")


class Refusal(ValueError):
    pass


def need(ok, message):
    if not ok:
        raise Refusal(message)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def pin(value):
    need(isinstance(value, str) and HEX.fullmatch(value), "invalid SHA256")
    return value


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def parse(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            need(key not in result, "duplicate JSON field")
            result[key] = value
        return result
    try:
        need(isinstance(raw, bytes) and len(raw) <= LIMIT, "JSON byte budget")
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(Refusal("nonfinite JSON")))
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise Refusal("invalid JSON") from exc


def shape(value, keys):
    need(isinstance(value, dict) and set(value) == set(keys), "unexpected schema fields")


def integer(value, low, high):
    return type(value) is int and low <= value <= high


def identifier(value, empty=False):
    need(isinstance(value, str) and (empty and value == "" or
         re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)), "invalid GUID/identifier; paths forbidden")
    return value


def pointer(value, path):
    need(isinstance(path, list) and 1 <= len(path) <= 12, "explicit observed JSON pointer required")
    for key in path:
        need(isinstance(key, str) and 0 < len(key) <= 80 and isinstance(value, dict) and key in value,
             "observed JSON pointer missing")
        value = value[key]
    return value


def local_url(url):
    need(isinstance(url, str) and re.fullmatch(r"http://localhost:8005/[A-Za-z0-9_/-]+", url),
         "exact localhost:8005 URL required; no query/userinfo/remote URL")
    parsed = urlsplit(url)
    need(all(p not in ("", ".", "..") for p in parsed.path.split("/")[1:]), "unsafe endpoint path")
    return parsed.path


def request_builder(body, media_id):
    """Exact frontend field mapping. Values must be copied from authorized trace."""
    need(isinstance(body, dict) and set(REQUIRED) <= set(body) <= set(REQUIRED + OPTIONAL),
         "play required fields missing or unknown request fields")
    identifier(media_id)
    need(body["media_guid"] == media_id, "wrong authorized test media ID")
    for key in ("media_guid", "video_guid"):
        identifier(body[key])
    for key in ("audio_guid", "subtitle_guid"):
        identifier(body[key], empty=True)
    for key in ("video_encoder", "audio_encoder"):
        need(isinstance(body[key], str) and len(body[key]) <= 80 and
             re.fullmatch(r"[A-Za-z0-9_-]*", body[key]), "invalid encoder field")
    resolution = body["resolution"]
    need(integer(resolution, 0, 8192) or isinstance(resolution, str) and
         re.fullmatch(r"[0-9]{1,5}(?:[xX][0-9]{1,5}|[pP])?", resolution), "invalid resolution field")
    need(integer(body["bitrate"], 0, 1000000000) and integer(body["channels"], 0, 32), "invalid bitrate/channels")
    ts = body["startTimestamp"]
    need(type(ts) in (int, float) and math.isfinite(ts) and 0 <= ts <= 86400, "invalid startTimestamp")
    if "forced_sdr" in body:
        need(integer(body["forced_sdr"], 0, 1), "forced_sdr must be numeric 0/1")
    if "without_stream" in body:
        need(body["without_stream"] is True, "without_stream only when observed true")
    return {"method": "POST", "frontend_path": PLAY, "headers": dict(HEADERS), "body": dict(body)}


def stats_builder(play_link):
    # play_link is an opaque identifier/URL from the actual reply, NEVER fetched.
    need(isinstance(play_link, str) and 0 < len(play_link) <= 4096 and
         all(32 <= ord(c) < 127 for c in play_link), "invalid reply play_link")
    return {"method": "POST", "frontend_path": STATS, "headers": dict(HEADERS),
            "body": {"req": "media.transcodeStatis", "reqid": REQID, "playLink": play_link}}


def response_data(response):
    need(isinstance(response, dict) and type(response.get("code")) is int and response["code"] == 0 and
         isinstance(response.get("data"), dict), "API success/data absent")
    data = response["data"]
    need("errno" not in data or type(data["errno"]) is int and data["errno"] == 0, "backend errno")
    need(not data.get("non_fatal_errno"), "backend nonfatal errors; no success inference")
    return data


def stat_backend_check(response, decoder_pointer):
    """Observed decoder key required. CPU/list/output/encoder never substitutes."""
    response_data(response)
    need(isinstance(decoder_pointer, list) and decoder_pointer and decoder_pointer[0] == "data" and
         decoder_pointer[-1] in ("decoder_method", "decode_method"), "decoder field mapping unknown")
    method = pointer(response, decoder_pointer)
    # Reject contradictory aliases, including a successful output plus Soft=0.
    def scan(value, depth=0):
        need(depth <= 16, "stats nesting budget")
        if isinstance(value, dict):
            for key, item in value.items():
                if key in ("decoder_method", "decode_method"):
                    need(type(item) is int and item == 6, "software/foreign/ambiguous decode backend refused")
                scan(item, depth + 1)
        elif isinstance(value, list):
            for item in value:
                scan(item, depth + 1)
    scan(response)
    need(type(method) is int and method == 6, "V4L2_M2M task evidence absent")
    return {"decoder_method": 6, "backend": "v4l2m2m", "task_backend_evidence": True,
            "hardware": False, "physical": False, "native_decode_verified": False,
            "no_cpu_fallback_proven": False, "hardware_encoding_required": False}


# Directory-FD traversal avoids source symlinks, hardlinks and path substitution.
def open_parent(path, private=False):
    need(os.name == "posix" and hasattr(os, "O_NOFOLLOW"), "Linux safe file access required")
    p = Path(path)
    need(p.is_absolute() and ".." not in p.parts and "\\" not in str(p), "absolute safe path required")
    need(not private or "private" in p.parts, "source/output must be beneath private")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in p.parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
            st = os.fstat(fd)
            need(not st.st_mode & 0o022 or st.st_mode & stat.S_ISVTX, "writable source ancestor")
        st = os.fstat(fd)
        need(st.st_uid == os.geteuid() and not st.st_mode & 0o022, "unsafe containing directory")
        if private:
            need(stat.S_IMODE(st.st_mode) == 0o700, "private containing directory must be 0700")
        return fd, p.name
    except BaseException:
        os.close(fd)
        raise


def stable(st):
    return (st.st_dev, st.st_ino, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_nlink)


def read_file(path, limit=LIMIT, private=False):
    directory, name = open_parent(path, private)
    fd = None
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(fd)
        need(stat.S_ISREG(before.st_mode) and before.st_nlink == 1 and before.st_uid == os.geteuid() and
             not before.st_mode & 0o022 and before.st_size <= limit, "unsafe/oversize source file")
        need(not private or stat.S_IMODE(before.st_mode) == 0o600, "private file must be 0600")
        data = bytearray()
        while len(data) <= limit:
            block = os.read(fd, min(65536, limit + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        need(len(data) == before.st_size and stable(before) == stable(os.fstat(fd)) ==
             stable(os.stat(name, dir_fd=directory, follow_symlinks=False)), "source changed during read")
        return bytes(data)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(directory)


def load_auth(path):
    value = parse(read_file(path, 8192, private=True))
    shape(value, ("Authorization",))  # No passwords, cookies, login or guessed Bearer prefix.
    token = value["Authorization"]
    need(isinstance(token, str) and 0 < len(token) <= 4096 and
         all(33 <= ord(c) < 127 for c in token), "invalid MC-token header")
    return token


def prepare(spec, media_raw, trace_raw, fixture_raw, pins):
    shape(spec, ("schema", "origin", "version_sha256", "expected_exe", "fixture", "authorization",
                 "media_response_file", "trace_file", "decoder_pointer", "body", "budget"))
    need(spec["schema"] == "native-media-job-v1" and spec["origin"] == ORIGIN, "wrong schema/origin")
    shape(pins, ("fixture_sha256", "testmedia_id", "media_response_sha256", "trace_sha256", "version_sha256", "exe_sha256"))
    for key in pins:
        pin(pins[key]) if key != "testmedia_id" else identifier(pins[key])
    shape(spec["fixture"], ("path", "source", "sha256"))
    f = spec["fixture"]
    need(f["path"] in FIXTURES and f["source"] == "testsrc2", "only authorized generated testsrc2 fixture allowed")
    need(sha(fixture_raw) == f["sha256"] == pins["fixture_sha256"], "fixture SHA mismatch")
    shape(spec["authorization"], ("fixture_binding_verified_by_main", "media_pointer", "testmedia_id", "fixture_sha256", "response_sha256"))
    a = spec["authorization"]
    need(a["fixture_binding_verified_by_main"] is True and a["testmedia_id"] == pins["testmedia_id"] and
         a["fixture_sha256"] == pins["fixture_sha256"] and sha(media_raw) == a["response_sha256"] == pins["media_response_sha256"],
         "independent fixture/media authorization binding absent")
    media = parse(media_raw)
    response_data(media)
    need(pointer(media, a["media_pointer"]) == pins["testmedia_id"], "media ID absent from actual authorized response")
    need(sha(trace_raw) == pins["trace_sha256"], "actual trace SHA mismatch")
    trace = parse(trace_raw)
    shape(trace, ("schema", "play", "stats"))
    need(trace["schema"] == "native-media-observed-routes-v1", "actual native route trace required")
    play = request_builder(spec["body"], pins["testmedia_id"])
    for role, frontend in (("play", PLAY), ("stats", STATS)):
        route = trace[role]
        shape(route, ("method", "frontend_path", "url", "headers", "body") if role == "play" else
              ("method", "frontend_path", "url", "headers", "decoder_pointer", "req", "reqid"))
        need(route["method"] == "POST" and route["frontend_path"] == frontend, "incorrect observed method/frontend route")
        local_url(route["url"])
        need(route["url"] != VERSION_URL and route["headers"] == HEADERS, "incorrect observed native headers/route")
    need(trace["play"]["url"] != trace["stats"]["url"], "ambiguous native route mapping")
    need(encode(trace["play"]["body"]) == encode(play["body"]), "request body differs from actual authorized trace")
    need(trace["stats"]["req"] == "media.transcodeStatis" and trace["stats"]["reqid"] == REQID and
         trace["stats"]["decoder_pointer"] == spec["decoder_pointer"] and
         isinstance(spec["decoder_pointer"], list) and 2 <= len(spec["decoder_pointer"]) <= 12 and
         all(isinstance(k, str) and 0 < len(k) <= 80 for k in spec["decoder_pointer"]) and
         spec["decoder_pointer"][0] == "data" and
         spec["decoder_pointer"][-1] in ("decoder_method", "decode_method"), "incorrect/unknown stats mapping")
    shape(spec["expected_exe"], ("pid", "starttime", "path", "sha256"))
    e = spec["expected_exe"]
    need(integer(e["pid"], 1, 4194304) and integer(e["starttime"], 1, 2**63 - 1) and
         isinstance(e["path"], str) and re.fullmatch(r"/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+", e["path"]) and
         all(p not in (".", "..") for p in e["path"].split("/")) and
         e["sha256"] == pins["exe_sha256"] and spec["version_sha256"] == pins["version_sha256"], "expected version/executable mismatch")
    shape(spec["budget"], ("play_requests", "stats_requests", "retries", "timeout_seconds", "total_seconds"))
    b = spec["budget"]
    need(type(b["play_requests"]) is int and b["play_requests"] == 1 and type(b["retries"]) is int and b["retries"] == 0 and
         integer(b["stats_requests"], 1, 3) and integer(b["timeout_seconds"], 1, 10) and integer(b["total_seconds"], 1, 30),
         "request/retry/time budget invalid")
    plan = {"play": play, "routes": trace, "spec": spec, "pins": dict(pins), "mode": "prepare-only",
            "hardware": False, "native_decode_verified": False, "producer_role": "task-api-receipt-only",
            "execution_ready": False, "header_source": HEADER_SOURCE,
            "http_request_budget": {"version": 1, "play": 1, "stats": b["stats_requests"],
                                    "total": 2 + b["stats_requests"], "retries": 0}}
    plan["contract_sha256"] = sha(encode({k: plan[k] for k in ("play", "routes", "spec", "pins")}))
    return plan


def check_process(expected):
    """Only one main-selected PID; intentional /proc/PID/exe read, never execute."""
    need(os.name == "posix" and Path("/proc/self/stat").is_file(), "Linux host required")
    base = Path("/proc") / str(expected["pid"])
    def identity():
        data = (base / "stat").read_bytes()
        end = data.rfind(b")")
        fields = data[end + 2:].split()
        need(end >= 0 and len(fields) >= 20 and fields[19].isdigit(), "invalid process identity")
        need(int(fields[19]) == expected["starttime"] and os.readlink(base / "exe") == expected["path"], "process/exe identity mismatch")
    identity()
    with (base / "exe").open("rb") as stream:
        before = os.fstat(stream.fileno())
        need(stat.S_ISREG(before.st_mode) and before.st_size <= 64 * LIMIT, "executable size/type")
        raw = stream.read(64 * LIMIT + 1)
        need(len(raw) == before.st_size and stable(before) == stable(os.fstat(stream.fileno())), "executable changed")
    identity()
    need(sha(raw) == expected["sha256"], "live executable SHA mismatch")


def require_verified_header_source():
    # Available saved raw is the NAS frontend, not the audited trim.media module.
    # This missing-source guard closes after the exact audited raw assignment is
    # actually reviewed and recorded with raw SHA, byte offset and exact quote.
    # Do not substitute synthetic equality or a self-reported producer approval.
    raise Refusal("exact client header assignment not source-verified: audited raw module absent locally; real HTTP disabled")


class LocalTransport:
    """No proxy, redirects, TLS/Internet, reconnect or application retry."""
    def __init__(self, allowed):
        self.allowed = frozenset(allowed)

    def send(self, method, url, headers, body, timeout):
        require_verified_header_source()
        need((method, url) in self.allowed, "unapproved transport request")
        path = local_url(url)
        need(0 < timeout <= 10, "transport timeout invalid")
        raw = None if body is None else encode(body)
        need(raw is None or len(raw) <= LIMIT, "request byte budget")
        conn = http.client.HTTPConnection("localhost", 8005, timeout=timeout)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        def expire():
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            sock.close()
        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        deadline = time.monotonic() + timeout
        try:
            # Never resolve localhost via DNS/hosts; use a fixed loopback socket.
            conn.sock = sock
            sock.settimeout(timeout)
            timer.start()
            sock.connect(("127.0.0.1", 8005))
            need(ipaddress.ip_address(sock.getpeername()[0]).is_loopback, "nonloopback peer")
            conn.request(method, path, body=raw, headers=headers)
            response = conn.getresponse()
            chunks = bytearray()
            while len(chunks) <= LIMIT:
                remaining = deadline - time.monotonic()
                need(remaining > 0, "response timeout")
                sock.settimeout(remaining)
                block = response.read1(min(65536, LIMIT + 1 - len(chunks)))
                if not block:
                    break
                chunks.extend(block)
            need(len(chunks) <= LIMIT, "response byte budget")
            return {"status": response.status, "headers": list(response.getheaders()), "body": bytes(chunks)}
        finally:
            timer.cancel()
            conn.close()
            sock.close()


class Receipts:
    def __init__(self, root):
        parent, _ = open_parent(str(Path(root) / "unused"), private=True)
        try:
            name = "native-job-" + os.urandom(12).hex()
            os.mkdir(name, 0o700, dir_fd=parent)
            self.fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        finally:
            os.close(parent)
        self.hashes = {}

    def store(self, name, raw):
        need(re.fullmatch(r"[a-z0-9-]+\.(json|bin)", name), "invalid receipt name")
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        self.hashes[name] = sha(raw)

    def close(self):
        os.close(self.fd)


def execute(plan, token, receipts, *, explicit=False, transport=None, observer=check_process, clock=time.monotonic):
    need(explicit is True, "execute requires explicit authorization")
    if transport is None or isinstance(transport, LocalTransport):
        require_verified_header_source()
    need(plan.get("contract_sha256") == sha(encode({k: plan[k] for k in ("play", "routes", "spec", "pins")})),
         "prepared contract changed")
    # Revalidate mutable plans before even an injected transport can send.
    spec, pins, trace = plan["spec"], plan["pins"], plan["routes"]
    need(spec["origin"] == ORIGIN and pins["version_sha256"] == spec["version_sha256"] and
         pins["exe_sha256"] == spec["expected_exe"]["sha256"], "changed execution identity")
    need(plan["play"] == request_builder(spec["body"], pins["testmedia_id"]) and
         encode(trace["play"]["body"]) == encode(spec["body"]), "changed request plan")
    need(isinstance(token, str) and 0 < len(token) <= 4096 and all(33 <= ord(c) < 127 for c in token), "invalid auth header")
    b = spec["budget"]
    need(type(b["play_requests"]) is int and b["play_requests"] == 1 and type(b["retries"]) is int and b["retries"] == 0 and
         integer(b["stats_requests"], 1, 3) and integer(b["timeout_seconds"], 1, 10) and integer(b["total_seconds"], 1, 30), "changed execution budget")
    for role, frontend in (("play", PLAY), ("stats", STATS)):
        route = trace[role]
        need(route["method"] == "POST" and route["frontend_path"] == frontend and route["headers"] == HEADERS and
             route["url"] != VERSION_URL, "changed observed route/headers")
        local_url(route["url"])
    need(trace["play"]["url"] != trace["stats"]["url"] and trace["stats"]["req"] == "media.transcodeStatis" and
         trace["stats"]["reqid"] == REQID and trace["stats"]["decoder_pointer"] == spec["decoder_pointer"] and
         isinstance(spec["decoder_pointer"], list) and len(spec["decoder_pointer"]) >= 2 and
         spec["decoder_pointer"][0] == "data" and spec["decoder_pointer"][-1] in ("decoder_method", "decode_method"),
         "changed observed stats mapping")
    allowed = [("GET", VERSION_URL)] + [("POST", trace[r]["url"]) for r in ("play", "stats")]
    transport = transport or LocalTransport(allowed)
    deadline = clock() + b["total_seconds"]
    headers = dict(HEADERS, Authorization=token)
    attempted_play = False
    completed = False
    verdict = None
    request_count = 0
    def send(label, method, url, body):
        nonlocal request_count
        need((method, url) in allowed, "incorrect request")
        local_url(url)
        observer(spec["expected_exe"])
        remaining = deadline - clock()
        need(remaining > 0, "total timeout")
        receipts.store(label + "-request.json", encode({"method": method, "url": url, "headers": headers, "body": body}))
        request_count += 1  # Count attempts even when transport times out.
        need(request_count <= 2 + b["stats_requests"], "total HTTP request budget exceeded")
        response = transport.send(method, url, headers, body, min(b["timeout_seconds"], remaining))
        shape(response, ("status", "headers", "body"))
        need(isinstance(response["body"], bytes) and len(response["body"]) <= LIMIT, "response byte budget")
        receipts.store(label + "-response.bin", response["body"])
        receipts.store(label + "-receipt.json", encode({"status": response["status"], "headers": response["headers"],
                       "request_sha256": receipts.hashes[label + "-request.json"], "response_sha256": sha(response["body"])}))
        need(clock() < deadline, "total timeout")
        need(type(response["status"]) is int and response["status"] == 200, "HTTP/redirect refused; retry zero")
        return response["body"]
    try:
        raw = send("version", "GET", VERSION_URL, None)
        need(sha(raw) == pins["version_sha256"], "live /api/version response mismatch")
        attempted_play = True  # Conservative: request may reach server even on timeout.
        play = parse(send("play", "POST", trace["play"]["url"], plan["play"]["body"]))
        link = response_data(play).get("play_link")
        stats = stats_builder(link)
        for index in range(b["stats_requests"]):
            raw = send("stats-" + str(index), "POST", trace["stats"]["url"], stats["body"])
            verdict = stat_backend_check(parse(raw), spec["decoder_pointer"])
        completed = True
        return verdict
    finally:
        receipts.store("result.json", encode({"completed": completed, "play_attempted": attempted_play,
                       "task_may_remain_active": attempted_play, "verdict": verdict,
                       "hardware": False, "native_decode_verified": False, "retry": 0,
                       "http_request_budget": {"version": 1, "play": 1, "stats": b["stats_requests"],
                                               "total": 2 + b["stats_requests"], "retries": 0},
                       "http_requests_attempted": request_count, "header_source": HEADER_SOURCE,
                       "producer_role": "task-api-receipt-only", "trace_producer_implementation": "unknown; main-owned",
                       "artifacts_sha256": dict(receipts.hashes)}))


def schema():
    return {"mode": "prepare-only", "requests": 0, "hardware": False, "native_decode_verified": False,
            "schema": "native-media-job-v1", "origin": ORIGIN, "version_url": VERSION_URL,
            "required_play_fields": list(REQUIRED), "optional_play_fields": list(OPTIONAL), "headers": HEADERS,
            "header_source": HEADER_SOURCE, "execution_ready": False,
            "http_request_budget": {"version": 1, "play": 1, "stats_min": 1, "stats_max": 3,
                                    "total_min": 3, "total_max": 5, "retries": 0},
            "approved_fixture_paths": list(FIXTURES), "stats": {"req": "media.transcodeStatis", "reqid": REQID},
            "approval_fields": ["schema", "origin", "version_sha256", "expected_exe", "fixture", "authorization",
                                "media_response_file", "trace_file", "decoder_pointer", "body", "budget"],
            "external_pins": ["fixture_sha256", "testmedia_id", "media_response_sha256", "trace_sha256", "version_sha256", "exe_sha256"],
            "unknown": "actual authorized media response/route trace and ioctl/frame/DMA producer; never guess",
            "binding": "main independently verifies actual response media GUID belongs to exact testsrc2 fixture SHA",
            "auth_file_format": "private 0600 JSON with only Authorization copied from cookieTrim-MC-token; never password",
            "budget": "one version preflight + one play + 1..3 stats, retry 0, total <=30s; no stream fetch/quit",
            "native_acceptance": "not performed; method6 only conditional task evidence, no CPU fallback proof"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--approval-file")
    parser.add_argument("--auth-file")
    parser.add_argument("--private-root")
    for name in ("fixture-sha256", "testmedia-id", "media-response-sha256", "trace-sha256", "version-sha256", "exe-sha256"):
        parser.add_argument("--" + name)
    args = parser.parse_args(argv)
    if not args.execute and not args.prepare_only:
        print(json.dumps(schema(), sort_keys=True))
        return 0
    receipts = None
    try:
        if args.execute:
            require_verified_header_source()  # Before approval/auth reads, receipts or preflight HTTP.
        need(args.approval_file, "explicit private approval required")
        approval_raw = read_file(args.approval_file, private=True)
        spec = parse(approval_raw)
        # Validate paths before reading anything nominated by untrusted approval.
        need(isinstance(spec, dict) and isinstance(spec.get("fixture"), dict) and spec["fixture"].get("path") in FIXTURES,
             "unapproved media path")
        pins = {k: getattr(args, k) for k in ("fixture_sha256", "testmedia_id", "media_response_sha256", "trace_sha256", "version_sha256", "exe_sha256")}
        media_raw = read_file(spec["media_response_file"], private=True)
        trace_raw = read_file(spec["trace_file"], private=True)
        plan = prepare(spec, media_raw, trace_raw, read_file(spec["fixture"]["path"], 32 * LIMIT), pins)
        if not args.execute:
            print(json.dumps({"mode": "prepare-only", "inputs_checked": True, "requests": 0,
                              "execution_ready": False, "header_source": HEADER_SOURCE,
                              "http_request_budget": plan["http_request_budget"],
                              "hardware": False, "native_decode_verified": False}))
            return 0
        need(args.auth_file and args.private_root, "private auth and new receipt directory required")
        token = load_auth(args.auth_file)
        receipts = Receipts(args.private_root)
        receipts.store("approval.json", approval_raw)
        receipts.store("authorized-media-response.bin", media_raw)
        receipts.store("observed-route-trace.bin", trace_raw)
        receipts.store("external-pins.json", encode(pins))
        execute(plan, token, receipts, explicit=True)
        print(json.dumps({"mode": "execute", "task_backend_evidence": True, "hardware": False,
                          "native_decode_verified": False, "private_receipt_sha256": receipts.hashes.get("result.json"),
                          "producer_sha256": sha(Path(__file__).read_bytes())}))
        return 0
    except (OSError, ValueError, KeyError, TypeError, RecursionError, http.client.HTTPException):
        # Never expose exception contents, token, request values, paths or GUIDs.
        print(json.dumps({"error": "native job refused/incomplete; no automatic retry", "hardware": False,
                          "execution_ready": False, "header_source": HEADER_SOURCE,
                          "native_decode_verified": False, "private_receipt_sha256": receipts.hashes.get("result.json") if receipts else None}))
        return 1
    finally:
        if receipts:
            receipts.close()


if __name__ == "__main__":
    raise SystemExit(main())
