#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Linux offline tests. python3 -I -B /absolute/path/test_native_job.py.
No real network/process/device/media requests. All trace/response bytes are
explicitly synthetic, NOT evidence that native routes or physical decode work.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

SPEC = importlib.util.spec_from_file_location("native_job", Path(__file__).with_name("native-job.py"))
j = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(j)


# Caller-reported proposal, independently written (not copied from j.HEADERS).
# This is NOT a real source contract; the production source gate stays blocked.
PROPOSED_HEADERS = {"Content-Type": "application/json", "X-Trim-Client": "web",
                    "X-Trim-Client-Version": "631"}


def examples():
    body = dict(media_guid="synthetic-media", video_guid="synthetic-video", video_encoder="h264",
                resolution="720p", bitrate=1000000, startTimestamp=0, audio_encoder="",
                audio_guid="", subtitle_guid="", channels=0)
    media = j.encode({"code": 0, "data": {"media_guid": body["media_guid"]}})
    fixture = b"synthetic testsrc2 fixture bytes; never actual MP4/evidence"
    decoder = ["data", "video", "decoder_method"]
    trace = {"schema": "native-media-observed-routes-v1",
             "play": {"method": "POST", "frontend_path": j.PLAY, "url": j.ORIGIN + "/synthetic/play",
                      "headers": dict(PROPOSED_HEADERS), "body": copy.deepcopy(body)},
             "stats": {"method": "POST", "frontend_path": j.STATS, "url": j.ORIGIN + "/synthetic/stats",
                       "headers": dict(PROPOSED_HEADERS), "req": "media.transcodeStatis", "reqid": j.REQID,
                       "decoder_pointer": decoder}}
    trace_raw = j.encode(trace)
    version = b'{"synthetic_version":"not-a-device"}'
    pins = {"fixture_sha256": j.sha(fixture), "testmedia_id": body["media_guid"],
            "media_response_sha256": j.sha(media), "trace_sha256": j.sha(trace_raw),
            "version_sha256": j.sha(version), "exe_sha256": "e" * 64}
    approval = {"schema": "native-media-job-v1", "origin": j.ORIGIN, "version_sha256": j.sha(version),
                "expected_exe": {"pid": 7, "starttime": 123, "path": "/synthetic/trim-media", "sha256": "e" * 64},
                "fixture": {"path": j.FIXTURES[0], "source": "testsrc2", "sha256": j.sha(fixture)},
                "authorization": {"fixture_binding_verified_by_main": True, "media_pointer": ["data", "media_guid"],
                                  "testmedia_id": body["media_guid"], "fixture_sha256": j.sha(fixture), "response_sha256": j.sha(media)},
                "media_response_file": "/private/response.json", "trace_file": "/private/trace.json",
                "decoder_pointer": decoder, "body": body,
                "budget": {"play_requests": 1, "stats_requests": 2, "retries": 0, "timeout_seconds": 2, "total_seconds": 10}}
    return approval, media, trace_raw, fixture, pins, version


def reply(value, status=200, headers=None):
    return {"status": status, "headers": headers or [], "body": value if isinstance(value, bytes) else j.encode(value)}


def backend(method=6):
    return {"code": 0, "data": {"video": {"decoder_method": method, "encoder_method": 0}}}


class MemoryReceipts:
    def __init__(self):
        self.hashes, self.files = {}, {}

    def store(self, name, raw):
        if name in self.files:
            raise AssertionError("receipt overwrite")
        self.files[name] = raw
        self.hashes[name] = j.sha(raw)


class FakeTransport:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def send(self, method, url, headers, body, timeout):
        self.calls.append((method, url, copy.deepcopy(headers), copy.deepcopy(body), timeout))
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class NativeJobTests(unittest.TestCase):
    def setUp(self):
        self.a, self.m, self.t, self.f, self.p, self.v = examples()
        self.network = patch.object(j.socket, "socket", side_effect=AssertionError("real socket forbidden"))
        self.network.start()
        self.addCleanup(self.network.stop)

    def plan(self):
        return j.prepare(self.a, self.m, self.t, self.f, self.p)

    def run_fake(self, responses, **kwargs):
        transport, receipts = FakeTransport(responses), MemoryReceipts()
        result = j.execute(self.plan(), "SYNTHETIC_SECRET", receipts, explicit=True,
                           transport=transport, observer=lambda _: None, **kwargs)
        return result, transport, receipts

    def test_default_has_no_io_or_requests(self):
        with patch.object(j, "read_file", side_effect=AssertionError("read forbidden")), patch.object(j, "LocalTransport", side_effect=AssertionError("transport forbidden")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(j.main([]), 0)
            self.assertEqual(json.loads(out.getvalue())["requests"], 0)

    def test_prepare_is_offline_and_conditional(self):
        plan = self.plan()
        self.assertEqual(plan["mode"], "prepare-only")
        self.assertFalse(plan["native_decode_verified"])
        self.assertEqual(plan["play"]["body"], self.a["body"])

    def test_exact_required_field_mapping(self):
        for key in j.REQUIRED:
            with self.subTest(key=key):
                body = dict(self.a["body"])
                body.pop(key)
                with self.assertRaises(j.Refusal):
                    j.request_builder(body, self.p["testmedia_id"])
        for key in ("path", "url", "gpu_acc", "cpu_allow_decoding", "decoder", "password"):
            with self.subTest(key=key), self.assertRaises(j.Refusal):
                j.request_builder(dict(self.a["body"], **{key: "x"}), self.p["testmedia_id"])

    def test_invalid_field_types_and_paths(self):
        for key, bad in (("media_guid", "/vol2/user/movie.mp4"), ("video_guid", "../file"), ("audio_guid", "file:///x"),
                         ("bitrate", True), ("channels", -1), ("startTimestamp", float("nan")),
                         ("startTimestamp", True), ("video_encoder", "x\r\nAuthorization: x"), ("resolution", "/1080")):
            with self.subTest(key=key), self.assertRaises(j.Refusal):
                j.request_builder(dict(self.a["body"], **{key: bad}), self.p["testmedia_id"])

    def test_conditional_fields_exact(self):
        body = dict(self.a["body"], forced_sdr=0, without_stream=True)
        self.assertEqual(j.request_builder(body, self.p["testmedia_id"])["body"], body)
        for key, value in (("forced_sdr", True), ("forced_sdr", 2), ("without_stream", False), ("without_stream", 1)):
            with self.subTest(key=key, value=value), self.assertRaises(j.Refusal):
                j.request_builder(dict(body, **{key: value}), self.p["testmedia_id"])

    def test_only_testsrc2_exact_fixture(self):
        for path in ("/vol2/user/movie.mp4", j.TESTDIR + "/../movie.mp4", j.TESTDIR + "/invented.mp4"):
            with self.subTest(path=path):
                self.a["fixture"]["path"] = path
                with self.assertRaises(j.Refusal):
                    self.plan()
        self.a["fixture"]["path"] = j.FIXTURES[0]
        self.a["fixture"]["source"] = "user media"
        with self.assertRaises(j.Refusal):
            self.plan()

    def test_fixture_sha_mismatch(self):
        self.f += b"changed"
        with self.assertRaises(j.Refusal):
            self.plan()

    def test_bad_media_and_receipt_binding(self):
        for key, value in (("testmedia_id", "other-media"), ("response_sha256", "a" * 64),
                           ("fixture_sha256", "b" * 64), ("fixture_binding_verified_by_main", False),
                           ("media_pointer", ["data", "guessed_guid"])):
            with self.subTest(key=key):
                old = self.a["authorization"][key]
                self.a["authorization"][key] = value
                with self.assertRaises(j.Refusal):
                    self.plan()
                self.a["authorization"][key] = old

    def test_actual_response_id_must_match(self):
        self.m = j.encode({"code": 0, "data": {"media_guid": "not-authorized-fixture"}})
        self.p["media_response_sha256"] = self.a["authorization"]["response_sha256"] = j.sha(self.m)
        with self.assertRaises(j.Refusal):
            self.plan()

    def test_no_guessed_route_and_wrong_request(self):
        original = j.parse(self.t)
        for role, key, bad in (("play", "url", "http://nas:8005/api/play/play"), ("play", "method", "GET"),
                               ("play", "frontend_path", "/play/play"), ("stats", "req", "media.list"),
                               ("stats", "reqid", "guessed"), ("play", "headers", {"version": "630"}),
                               ("stats", "decoder_pointer", ["data", "cpu_usage"]),
                               ("play", "body", dict(self.a["body"], bitrate=55))):
            with self.subTest(role=role, key=key):
                trace = copy.deepcopy(original)
                trace[role][key] = bad
                self.t = j.encode(trace)
                self.p["trace_sha256"] = j.sha(self.t)
                with self.assertRaises(j.Refusal):
                    self.plan()
        self.t = j.encode(original)
        self.p["trace_sha256"] = "f" * 64
        with self.assertRaises(j.Refusal):
            self.plan()

    def test_wrong_client_header_rejected_not_a_source_proof(self):
        # Re-pin each synthetic trace, so failure must be header validation,
        # not merely stale SHA. Equality to proposal NEVER clears source gap.
        wrong = (
            {"Content-Type": "application/json", "X-Trim-Client": "web", "version": "631"},
            {"Content-Type": "application/json", "X-Trim-Client": "web", "X-Trim-Client-Version": "630"},
            {"Content-Type": "application/json", "X-Trim-Client": "web"},
            dict(PROPOSED_HEADERS, version="631"),
            dict(PROPOSED_HEADERS, **{"X-Trim-Client": "native"}),
        )
        original = j.parse(self.t)
        for role in ("play", "stats"):
            for headers in wrong:
                with self.subTest(role=role, headers=headers):
                    trace = copy.deepcopy(original)
                    trace[role]["headers"] = headers
                    self.t = j.encode(trace)
                    self.p["trace_sha256"] = j.sha(self.t)
                    with self.assertRaisesRegex(j.Refusal, "incorrect observed native headers/route"):
                        self.plan()
        self.assertFalse(j.HEADER_SOURCE["verified"])

    def test_matching_proposed_headers_cannot_enable_real_execution(self):
        plan = self.plan()
        self.assertFalse(plan["execution_ready"])
        self.assertFalse(plan["header_source"]["verified"])
        self.assertEqual(plan["play"]["headers"], PROPOSED_HEADERS)
        # Even a caller-supplied verified flag is not accepted as source proof.
        plan["execution_ready"] = True
        with patch.dict(j.HEADER_SOURCE, {"verified": True}), patch.object(j, "check_process", side_effect=AssertionError("process read forbidden")):
            with self.assertRaisesRegex(j.Refusal, "not source-verified"):
                j.execute(plan, "secret", MemoryReceipts(), explicit=True)
            local = j.LocalTransport([("GET", j.VERSION_URL)])
            with self.assertRaisesRegex(j.Refusal, "not source-verified"):
                j.execute(plan, "secret", MemoryReceipts(), explicit=True, transport=local)
            with self.assertRaisesRegex(j.Refusal, "not source-verified"):
                local.send("GET", j.VERSION_URL, PROPOSED_HEADERS, None, 2)
        with patch.object(j, "read_file", side_effect=AssertionError("approval/auth read forbidden")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(j.main(["--execute", "--approval-file", "/private/unknown.json"]), 1)
            result = json.loads(out.getvalue())
            self.assertFalse(result["execution_ready"])
            self.assertFalse(result["header_source"]["verified"])

    def test_source_gap_names_actual_missing_raw_not_host_proof(self):
        # Metadata consistency only. The hashes/offsets were obtained by this
        # task's bounded raw read; this unit test is NOT provenance evidence.
        source = j.HEADER_SOURCE
        self.assertFalse(source["verified"])
        raw = source["audited_raw_required"]
        self.assertTrue(raw["phone_path_at_audit"].endswith("/14a77ac952fc83b785b65592c335e16d-BGXwY7Sz.js"))
        self.assertEqual(raw["sha256"], "e244e703491c58f2e354be13a2dc00a6354ad352bb6519e90d7e28a188d9f0a6")
        self.assertIsNone(raw["header_assignment_byte_offset"])
        self.assertIsNone(raw["header_assignment_exact_quote"])
        local = source["local_raw_checked"]
        self.assertNotEqual(local["sha256"], raw["sha256"])
        self.assertEqual(local["exact_header_string_counts"], {"X-Trim-Client-Version": 0, "X-Trim-Client": 0})
        self.assertEqual(local["631_byte_offsets"], [2532268, 2535073, 2537486, 2540694])

    def test_total_budget_includes_version_play_and_stats(self):
        for count in (1, 2, 3):
            with self.subTest(stats=count):
                self.a["budget"]["stats_requests"] = count
                plan = self.plan()
                expected = {"version": 1, "play": 1, "stats": count, "total": 2 + count, "retries": 0}
                self.assertEqual(plan["http_request_budget"], expected)
                responses = [reply(self.v), reply({"code": 0, "data": {"play_link": "link"}})] + [reply(backend()) for _ in range(count)]
                _, transport, receipts = self.run_fake(responses)
                self.assertEqual(len(transport.calls), 2 + count)
                self.assertEqual([call[0] for call in transport.calls], ["GET"] + ["POST"] * (1 + count))
                result = j.parse(receipts.files["result.json"])
                self.assertEqual(result["http_request_budget"], expected)
                self.assertEqual(result["http_requests_attempted"], 2 + count)
        self.assertEqual(j.schema()["http_request_budget"]["total_max"], 5)

    def test_strict_json(self):
        for raw in (b'{"code":0,"code":1}', b'{"x":NaN}', b'[] trailing', b'{"secret":', b'\xff'):
            with self.subTest(raw=raw), self.assertRaises(j.Refusal):
                j.parse(raw)

    def test_stats_wire_body_is_observed_contract(self):
        value = j.stats_builder("http://external.invalid/opaque-playlink")
        self.assertEqual(value["body"], {"req": "media.transcodeStatis", "reqid": j.REQID,
                                        "playLink": "http://external.invalid/opaque-playlink"})
        for invalid in (None, "", "x\nsecret"):
            with self.subTest(invalid=invalid), self.assertRaises(j.Refusal):
                j.stats_builder(invalid)

    def test_soft_qsv_other_backends_refused_even_output_success(self):
        for method in (0, 1, 2, 3, 4, 5, 7, "6", True, None):
            with self.subTest(method=method), self.assertRaises(j.Refusal):
                data = backend(method)
                data["data"]["output_success"] = True
                data["data"]["cpu_usage"] = 0
                j.stat_backend_check(data, self.a["decoder_pointer"])

    def test_decoder_not_encoder_cpu_or_listing(self):
        for data in ({"cpu_usage": 0}, {"decoder_list": [6]}, {"video": {"encoder_method": 7}},
                     {"output_success": True}, {"decode_method": 6}):
            with self.subTest(data=data), self.assertRaises(j.Refusal):
                j.stat_backend_check({"code": 0, "data": data}, self.a["decoder_pointer"])

    def test_hardware_encoding_independent_and_no_physical_claim(self):
        verdict = j.stat_backend_check(backend(), self.a["decoder_pointer"])
        self.assertTrue(verdict["task_backend_evidence"])
        for key in ("hardware", "physical", "native_decode_verified", "no_cpu_fallback_proven", "hardware_encoding_required"):
            self.assertFalse(verdict[key])
        alias = {"code": 0, "data": {"decode_method": 6}}
        self.assertTrue(j.stat_backend_check(alias, ["data", "decode_method"])["task_backend_evidence"])
        alias["data"]["video"] = {"decoder_method": 0}
        with self.assertRaises(j.Refusal):
            j.stat_backend_check(alias, ["data", "decode_method"])

    def test_backend_errors_and_false_success(self):
        for value in ({"code": False, "data": {"decode_method": 6}}, {"code": 1, "data": {}},
                      {"code": 0, "data": {"errno": 5, "decode_method": 6}},
                      {"code": 0, "data": {"non_fatal_errno": [1], "decode_method": 6}}):
            with self.subTest(value=value), self.assertRaises(j.Refusal):
                j.stat_backend_check(value, ["data", "decode_method"])

    def test_budget_mismatch(self):
        for key, value in (("play_requests", 2), ("play_requests", True), ("stats_requests", 0), ("stats_requests", 4),
                           ("retries", 1), ("timeout_seconds", 11), ("total_seconds", 31)):
            with self.subTest(key=key):
                old = self.a["budget"][key]
                self.a["budget"][key] = value
                with self.assertRaises(j.Refusal):
                    self.plan()
                self.a["budget"][key] = old

    def test_execute_exact_budget_and_original_private_receipts(self):
        result, transport, receipts = self.run_fake([reply(self.v), reply({"code": 0, "data": {"play_link": "opaque-link"}}), reply(backend()), reply(backend())])
        self.assertEqual([x[0] for x in transport.calls], ["GET", "POST", "POST", "POST"])
        self.assertEqual(transport.calls[0][1], j.VERSION_URL)
        self.assertEqual(transport.calls[1][3], self.a["body"])
        self.assertEqual(transport.calls[2][3]["playLink"], "opaque-link")
        self.assertEqual(transport.calls[1][2]["Authorization"], "SYNTHETIC_SECRET")
        self.assertIn(b"SYNTHETIC_SECRET", receipts.files["play-request.json"])
        self.assertEqual(receipts.hashes["play-response.bin"], j.sha(receipts.files["play-response.bin"]))
        self.assertFalse(result["native_decode_verified"])
        self.assertTrue(j.parse(receipts.files["result.json"])["task_may_remain_active"])

    def test_explicit_flag_required(self):
        transport = FakeTransport([])
        with self.assertRaises(j.Refusal):
            j.execute(self.plan(), "secret", MemoryReceipts(), transport=transport, observer=lambda _: None)
        self.assertEqual(transport.calls, [])

    def test_mutated_plan_refused_before_request(self):
        for edit in (lambda p: p["routes"]["play"].update(url="http://localhost:8005/guessed"),
                     lambda p: p["spec"]["body"].update(media_guid="other-media"),
                     lambda p: p["spec"]["budget"].update(retries=1)):
            with self.subTest(edit=edit):
                plan = self.plan()
                edit(plan)
                transport = FakeTransport([])
                with self.assertRaises(j.Refusal):
                    j.execute(plan, "secret", MemoryReceipts(), explicit=True, transport=transport, observer=lambda _: None)
                self.assertEqual(transport.calls, [])
                self.setUp_data_only()

    def setUp_data_only(self):
        self.a, self.m, self.t, self.f, self.p, self.v = examples()

    def test_live_version_mismatch_no_play(self):
        transport = FakeTransport([reply(b"wrong-version")])
        receipts = MemoryReceipts()
        with self.assertRaises(j.Refusal):
            j.execute(self.plan(), "secret", receipts, explicit=True, transport=transport, observer=lambda _: None)
        self.assertEqual(len(transport.calls), 1)
        self.assertFalse(j.parse(receipts.files["result.json"])["play_attempted"])

    def test_expected_exe_refused_before_http(self):
        transport = FakeTransport([])
        def refuse(_):
            raise j.Refusal("wrong process SHA")
        with self.assertRaises(j.Refusal):
            j.execute(self.plan(), "secret", MemoryReceipts(), explicit=True, transport=transport, observer=refuse)
        self.assertEqual(transport.calls, [])

    def test_timeout_zero_retry_preserves_request(self):
        transport = FakeTransport([reply(self.v), TimeoutError("SYNTHETIC_SECRET")])
        receipts = MemoryReceipts()
        with self.assertRaises(TimeoutError):
            j.execute(self.plan(), "secret", receipts, explicit=True, transport=transport, observer=lambda _: None)
        self.assertEqual(len(transport.calls), 2)
        self.assertIn("play-request.json", receipts.files)
        self.assertFalse(j.parse(receipts.files["result.json"])["completed"])

    def test_redirect_never_followed(self):
        for status in (301, 302, 307, 308):
            with self.subTest(status=status):
                transport = FakeTransport([reply(b"", status, [["Location", "http://external.invalid/secret"]])])
                with self.assertRaises(j.Refusal):
                    j.execute(self.plan(), "secret", MemoryReceipts(), explicit=True, transport=transport, observer=lambda _: None)
                self.assertEqual(len(transport.calls), 1)

    def test_later_stats_soft_fallback_refused_immediately(self):
        transport = FakeTransport([reply(self.v), reply({"code": 0, "data": {"play_link": "link"}}), reply(backend()), reply(backend(0))])
        receipts = MemoryReceipts()
        with self.assertRaises(j.Refusal):
            j.execute(self.plan(), "secret", receipts, explicit=True, transport=transport, observer=lambda _: None)
        self.assertEqual(len(transport.calls), 4)
        self.assertFalse(j.parse(receipts.files["result.json"])["completed"])

    def test_total_deadline_enforced(self):
        transport = FakeTransport([])
        ticks = iter([0, 11])
        with self.assertRaises(j.Refusal):
            j.execute(self.plan(), "secret", MemoryReceipts(), explicit=True, transport=transport, observer=lambda _: None, clock=lambda: next(ticks))
        self.assertEqual(transport.calls, [])

    def test_transport_mechanics_with_mocked_source_gate_is_not_source_proof(self):
        sock = MagicMock()
        sock.getpeername.return_value = ("127.0.0.1", 8005)
        conn = MagicMock()
        response = conn.getresponse.return_value
        response.status = 200
        response.getheaders.return_value = []
        response.read1.side_effect = [b"{}", b""]
        # Mock the blocking gate ONLY to exercise socket mechanics; all sockets fake.
        with patch.object(j, "require_verified_header_source"), patch.object(j.socket, "socket", return_value=sock), patch.object(j.socket, "getaddrinfo", side_effect=AssertionError("DNS forbidden")), patch.object(j.http.client, "HTTPConnection", return_value=conn), patch.object(j.threading, "Timer") as timer:
            value = j.LocalTransport([("GET", j.VERSION_URL)]).send("GET", j.VERSION_URL, PROPOSED_HEADERS, None, 2)
        self.assertEqual(value["body"], b"{}")
        sock.connect.assert_called_once_with(("127.0.0.1", 8005))
        conn.request.assert_called_once_with("GET", "/api/version", body=None, headers=PROPOSED_HEADERS)
        timer.return_value.cancel.assert_called_once()

    @patch.object(j, "require_verified_header_source")
    def test_transport_rejects_bad_url_method_before_socket(self, mocked_source_gate):
        for url in ("http://127.0.0.1:8005/api/version", "https://localhost:8005/api/version", "http://localhost:8005/api/version?x=1",
                    "http://localhost:8005/../x", "http://remote.invalid:8005/x", "http://localhost:8005//api/version"):
            with self.subTest(url=url), self.assertRaises(j.Refusal):
                j.LocalTransport([("GET", url)]).send("GET", url, {}, None, 2)
        with self.assertRaises(j.Refusal):
            j.LocalTransport([("GET", j.VERSION_URL)]).send("POST", j.VERSION_URL, {}, {}, 2)

    def test_auth_and_receipts_private_modes_links_and_password_refused(self):
        with tempfile.TemporaryDirectory(prefix="native-job-offline-") as temp:
            private = Path(temp) / "private"
            private.mkdir(mode=0o700)
            auth = private / "auth.json"
            auth.write_bytes(j.encode({"Authorization": "SYNTHETIC_SECRET"}))
            auth.chmod(0o600)
            self.assertEqual(j.load_auth(str(auth)), "SYNTHETIC_SECRET")
            auth.chmod(0o644)
            with self.assertRaises(j.Refusal):
                j.load_auth(str(auth))
            auth.chmod(0o600)
            auth.write_bytes(j.encode({"password": "never-print-me"}))
            with self.assertRaises(j.Refusal):
                j.load_auth(str(auth))
            auth.write_bytes(j.encode({"Authorization": "secret\r\ninjection"}))
            with self.assertRaises(j.Refusal):
                j.load_auth(str(auth))
            link = private / "linked.json"
            link.symlink_to(auth)
            with self.assertRaises((OSError, j.Refusal)):
                j.load_auth(str(link))
            hardlink = private / "hard.json"
            os.link(auth, hardlink)
            with self.assertRaises(j.Refusal):
                j.load_auth(str(hardlink))
            outside = Path(temp) / "public.json"
            outside.write_bytes(j.encode({"Authorization": "secret"}))
            outside.chmod(0o600)
            with self.assertRaises(j.Refusal):
                j.load_auth(str(outside))
            store = j.Receipts(str(private))
            try:
                store.store("request.json", b"private-original")
                with self.assertRaises(FileExistsError):
                    store.store("request.json", b"overwrite")
                with self.assertRaises(j.Refusal):
                    store.store("../public.json", b"bad")
                directory = next(p for p in private.iterdir() if p.is_dir())
                self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
                self.assertEqual((directory / "request.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual(store.hashes["request.json"], j.sha(b"private-original"))
            finally:
                store.close()

    def test_actual_process_reader_rejects_bad_sha_and_identity(self):
        image = b"synthetic executable"
        expected = self.a["expected_exe"]
        process_stat = b"7 (name with ) spaces) S " + b" ".join([b"0"] * 18 + [b"123"])
        st = type("FileStat", (), dict(st_dev=1, st_ino=2, st_mode=0o100600, st_size=len(image),
                                      st_mtime_ns=1, st_ctime_ns=1, st_nlink=1))()
        class Image(io.BytesIO):
            def fileno(self):
                return 42
        with patch.object(j.Path, "is_file", return_value=True), patch.object(j.Path, "read_bytes", return_value=process_stat), patch.object(j.Path, "open", side_effect=lambda *a, **k: Image(image)), patch.object(j.os, "readlink", return_value=expected["path"]), patch.object(j.os, "fstat", return_value=st):
            with self.assertRaises(j.Refusal):
                j.check_process(expected)
            expected["sha256"] = j.sha(image)
            j.check_process(expected)
            expected["starttime"] = 124
            with self.assertRaises(j.Refusal):
                j.check_process(expected)

    def test_cli_errors_redacted_and_no_real_http(self):
        out = io.StringIO()
        with patch.object(j, "read_file", side_effect=OSError("SYNTHETIC_SECRET password path GUID")), contextlib.redirect_stdout(out):
            self.assertEqual(j.main(["--execute", "--approval-file", "/private/synthetic.json"]), 1)
        self.assertNotIn("SYNTHETIC_SECRET", out.getvalue())
        self.assertNotIn("GUID", out.getvalue())
        self.assertFalse(json.loads(out.getvalue())["native_decode_verified"])


if __name__ == "__main__":
    unittest.main()
