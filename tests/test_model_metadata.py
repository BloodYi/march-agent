"""Offline metadata regressions: extract functions without importing app or running agy."""
from __future__ import annotations

import __future__
import ast
import asyncio
import codecs
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import types
import unittest
from unittest.mock import patch
import urllib.parse
import uuid


APP = Path(__file__).resolve().parents[1] / "app.py"
_module = {"__name__": "offline_agy_metadata"}
exec(compile(APP.with_name("agy_metadata.py").read_text(encoding="utf-8"),
             str(APP.with_name("agy_metadata.py")), "exec"), _module)
metadata = types.SimpleNamespace(**_module)
FUNCTIONS = {"_new_route_info", "_metadata_url", "_response_evidence",
             "_merge_response_evidence", "_request_log_evidence", "_parse_ndjson_line",
             "_update_route_coverage", "_stream_agy", "openai_chat"}


def load_functions(scratch):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in FUNCTIONS:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in {"_RETURNED_MODEL_FIELDS", "_RESPONSE_ID_FIELDS"}
            for target in node.targets
        ):
            nodes.append(node)
    namespace = dict(json=json, Path=Path, re=re, urllib=urllib, uuid=uuid, time=time,
                     tempfile=tempfile, asyncio=asyncio, codecs=codecs, SCRATCH=scratch,
                     AGY_TIMEOUT=0, AGY_OUTPUT_FORMAT="stream-json", AGY_PERSIST_CONVERSATIONS=False,
                     _within_allowed=lambda cwd: cwd, _build_argv=lambda *a, **k: ["fake-agy"],
                     _agy_environment=lambda: {}, logger=types.SimpleNamespace(warning=lambda *a: None),
                     JSONResponse=lambda value: value)
    namespace["read_session_models"] = metadata.read_session_models
    namespace["read_session_thinking"] = metadata.read_session_thinking
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    return namespace


def varint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number, value, wire=2):
    return varint((number << 3) | wire) + (varint(value) if wire == 0 else varint(len(value)) + value)


def generation(model="returned", steps=(7,), execution="run-a", full=None, packed=True):
    chat = field(19, model.encode())
    if full is not None:
        chat += field(22, full.encode())
    indices = field(2, b"".join(varint(index) for index in steps)) if packed else b"".join(
        field(2, index, 0) for index in steps)
    return field(1, chat) + indices + field(4, execution.encode())


class FakeReader:
    def __init__(self, chunks=(), block=False):
        self.chunks = list(chunks)
        self.block = block

    async def read(self, size):
        if self.chunks:
            return self.chunks.pop(0)
        if self.block:
            await asyncio.Event().wait()
        return b""


class FakeProcess:
    def __init__(self, objects=(), block=False):
        data = "".join(json.dumps(obj) + "\n" for obj in objects).encode()
        self.stdout = FakeReader([data] if data else [], block=block)
        self.stderr = FakeReader()
        self.returncode = None

    def kill(self):
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class ModelMetadataTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-metadata-tests-")
        self.ns = load_functions(Path(self.temp.name))

    def tearDown(self):
        self.temp.cleanup()

    def evidence(self, obj, info=None):
        if info is None:
            info = self.ns["_new_route_info"]("request-a", "gemini-3.6-flash")
        self.ns["_merge_response_evidence"](info, self.ns["_response_evidence"](obj))
        return info

    def database(self, cid, generations, steps=((7, "run-a"),), payloads=None):
        path = Path(self.temp.name) / ".gemini" / "antigravity-cli" / "conversations" / (cid + ".db")
        path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(path) as connection:
            connection.executescript("CREATE TABLE gen_metadata(idx INTEGER PRIMARY KEY, data BLOB);"
                                     "CREATE TABLE steps(idx INTEGER PRIMARY KEY, metadata BLOB, step_payload BLOB);")
            connection.executemany("INSERT INTO gen_metadata VALUES(?, ?)", enumerate(generations))
            connection.executemany("INSERT INTO steps VALUES(?, ?, ?)",
                                   [(index, field(12, execution.encode()), (payloads or {}).get(index))
                                    for index, execution in steps])
        connection.close()
        return path

    def test_session_metadata_exact_numeric_paths_and_both_index_encodings(self):
        for packed in (True, False):
            blob = generation(full="provider/models/returned-full", packed=packed)
            blob += field(3, field(19, b"config-spoof")) + field(20, b"response_model: text-spoof")
            records = metadata.generation_models(blob, {7}, {7: "run-a"})
            self.assertEqual([r["value"] for r in records], ["returned", "provider/models/returned-full"])
            self.assertEqual([r["field"].rsplit(".", 1)[-1] for r in records],
                             ["response_model", "response_model_full"])
            self.assertTrue(all(r["source"] == "agy_session_db" and r["step_indices"] == [7] for r in records))

    def test_session_model_requires_all_current_steps_and_matching_execution(self):
        for blob, observed, executions in (
            (generation(steps=()), {7}, {7: "run-a"}),
            (generation(steps=(1, 7)), {7}, {7: "run-a"}),
            (generation(), set(), {}),
            (generation(), {7}, {7: "other-run"}),
            (generation(), {7}, {}),
            (generation(), {7}, None),
        ):
            self.assertEqual(metadata.generation_models(blob, observed, executions), [])

    def test_large_generation_envelopes_keep_only_verified_model_fields(self):
        padding = field(8, b"unrelated payload " * 80000)
        for inside_chat in (False, True):
            with self.subTest(inside_chat=inside_chat):
                blob = (field(1, field(19, b"future-exp-z") + padding)
                        + field(2, b"\x07") + field(4, b"run-a")) if inside_chat else (
                            generation("future-exp-z") + padding)
                self.assertGreater(len(blob), metadata.MAX_BLOB)
                records = metadata.generation_models(blob, {7}, {7: "run-a"})
                self.assertEqual([r["value"] for r in records], ["future-exp-z"])
                self.assertEqual(metadata.generation_models(blob, {7}, {7: "other-run"}), [])
                with self.assertRaises(ValueError):
                    metadata._fields(blob, {19})

    def test_large_generation_envelopes_are_read_from_session_database(self):
        cid = str(uuid.uuid4())
        chat = field(19, b"future-exp-z") + field(8, b"x" * (metadata.MAX_BLOB + 200000))
        blob = field(1, chat) + field(2, b"\x07") + field(4, b"run-a")
        path = self.database(cid, [generation("history", (1,), "old-run"), blob],
                             ((1, "old-run"), (7, "run-a")))
        before = path.read_bytes()
        records = metadata.read_session_models(self.temp.name, cid, {7})
        self.assertEqual([r["value"] for r in records], ["future-exp-z"])
        self.assertEqual(path.read_bytes(), before)

    def test_generation_envelope_limit_remains_bounded(self):
        blob = generation("oversized") + field(8, b"x" * (8 * metadata.MAX_BLOB))
        self.assertEqual(metadata.generation_models(blob, {7}, {7: "run-a"}), [])
        cid = str(uuid.uuid4())
        self.database(cid, [blob, generation("within-limit")])
        self.assertEqual([r["value"] for r in metadata.read_session_models(self.temp.name, cid, {7})],
                         ["within-limit"])

    def test_generation_scan_total_budget_stays_sixteen_mebibytes(self):
        blob = generation("within-row-limit") + field(8, b"x" * (6 * metadata.MAX_BLOB))
        cid = str(uuid.uuid4())
        self.database(cid, [blob, blob, blob])
        self.assertEqual(metadata.read_session_models(self.temp.name, cid, {7}), [])

    def test_session_model_rejects_malformed_wire_and_string_limits(self):
        bad = [b"\x80", b"\x0a\x20short", b"\x0b", b"\x10" + b"\xff" * 11,
               field(1, field(19, b"\xff")) + field(2, b"\x07"),
               generation(model="m" * 513), b"\x08\x00" * (metadata.MAX_FIELDS + 1),
               generation(steps=(7,) * (metadata.MAX_STEPS + 1)),
               b"x" * (metadata.MAX_BLOB + 1)]
        for blob in bad:
            self.assertEqual(metadata.generation_models(blob, {7}, {7: "run-a"}), [])

    def test_session_database_history_and_other_conversations_are_isolated(self):
        cid_a, cid_b = str(uuid.uuid4()), str(uuid.uuid4())
        self.database(cid_a, [generation("history", (1,), "old-run"), generation("current")],
                      ((1, "old-run"), (7, "run-a")))
        self.database(cid_b, [generation("other-conversation")])
        records = metadata.read_session_models(self.temp.name, cid_a, {7})
        self.assertEqual([r["value"] for r in records], ["current"])
        self.assertEqual(metadata.read_session_models(self.temp.name, cid_a, {8}), [])
        self.assertEqual(metadata.read_session_models(self.temp.name, "../outside", {7}), [])

    def test_session_conflicts_are_retained_and_full_names_stay_separate(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation("first", full="provider/first"), generation("second", full="provider/second")])
        info = self.ns["_new_route_info"]("request-a")
        self.ns["_merge_response_evidence"](info, metadata.read_session_models(self.temp.name, cid, {7}))
        self.assertEqual(info["returned_models"], ["first", "second"])
        self.assertEqual(info["returned_model_fulls"], ["provider/first", "provider/second"])
        self.assertEqual(info["status"], "conflict")
        info = self.ns["_new_route_info"]("request-b")
        self.ns["_merge_response_evidence"](info, metadata.generation_models(generation("same", full="full-a"), {7}, {7: "run-a"}))
        self.ns["_merge_response_evidence"](info, metadata.generation_models(generation("same", full="full-b"), {7}, {7: "run-a"}))
        self.assertEqual(info["status"], "conflict")

    def test_session_reads_are_read_only_and_missing_schema_is_unavailable(self):
        cid = str(uuid.uuid4())
        path = self.database(cid, [generation()])
        before = path.read_bytes()
        self.assertEqual(len(metadata.read_session_models(self.temp.name, cid, {7})), 1)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(Path(str(path) + "-journal").exists())
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE gen_metadata SET data=42")
        connection.close()
        self.assertEqual(metadata.read_session_models(self.temp.name, cid, {7}), [])
        with sqlite3.connect(path) as connection:
            connection.execute("DROP TABLE gen_metadata")
        connection.close()
        self.assertEqual(metadata.read_session_models(self.temp.name, cid, {7}), [])
        self.assertEqual(metadata.read_session_models(self.temp.name, str(uuid.uuid4()), {7}), [])

    def test_session_row_limit_is_closed_even_with_last_row_wrong_type(self):
        cid = str(uuid.uuid4())
        path = self.database(cid, [generation()])
        with sqlite3.connect(path) as connection:
            connection.executemany("INSERT INTO gen_metadata VALUES(?, ?)",
                                   [(index, b"") for index in range(1, metadata.MAX_ROWS)])
            connection.execute("INSERT INTO gen_metadata VALUES(?, ?)", (metadata.MAX_ROWS, 42))
        connection.close()
        self.assertEqual(metadata.read_session_models(self.temp.name, cid, {7}), [])

    def test_native_thinking_reads_only_planner_field_three_and_preserves_full_text(self):
        text = "Native thinking\n\t" + "long thought " * 60
        planner = (field(3, text.encode()) + field(14, b"SIGNATURE_SECRET")
                   + field(16, b"RAW_THINKING_SECRET") + field(9, field(3, b"NESTED_SECRET")))
        payload = field(20, planner) + field(3, b"ROOT_THOUGHT_SECRET") + field(21, field(3, b"OTHER_SECRET"))
        self.assertEqual(metadata.planner_thinking(payload), text)
        for payload in (field(20, field(14, b"SIGNATURE_SECRET")),
                        field(20, field(16, b"RAW_THINKING_SECRET")),
                        field(3, b"ROOT_THOUGHT_SECRET"),
                        field(20, field(9, field(3, b"NESTED_SECRET")))):
            self.assertIsNone(metadata.planner_thinking(payload))

    def test_native_thinking_empty_malformed_and_large_fields_are_not_emitted(self):
        payloads = [field(20, field(3, b"")), field(20, field(3, b" \n\t")),
                    field(20, field(3, b"\xff")), field(20, field(3, 7, 0)),
                    field(20, field(3, b"one") + field(3, b"two")),
                    field(20, field(3, b"one")) + field(20, field(3, b"two")),
                    field(20, b"\x1a\x20short"), field(20, field(3, b"bad\x00text")),
                    field(20, field(3, b"x" * metadata.MAX_BLOB))]
        for payload in payloads:
            self.assertIsNone(metadata.planner_thinking(payload))

    def test_native_thinking_is_current_session_step_and_execution_only_without_model_dependency(self):
        cid_a, cid_b = str(uuid.uuid4()), str(uuid.uuid4())
        no_model = field(1, b"") + field(2, b"\x07") + field(4, b"run-a")
        path = self.database(cid_a, [generation("old", (1,), "old-run"), no_model,
                              generation("wrong-execution", (8,), "wrong-run"), generation("tool", (9,), "tool-run")],
                      ((1, "old-run"), (7, "run-a"), (8, "different-run"), (9, "tool-run")),
                      {index: field(20, field(3, text.encode())) for index, text in
                       ((1, "OLD_SECRET"), (7, "current thought"), (8, "UNLINKED_SECRET"), (9, "TOOL_SECRET"))})
        self.database(cid_b, [generation()], payloads={7: field(20, field(3, b"OTHER_SESSION_SECRET"))})
        events = metadata.read_session_thinking(self.temp.name, cid_a, {7, 8}, {7, 8, 9})
        self.assertEqual(events, [{"event": "thinking", "mode": "snapshot", "step_index": 7,
                                  "thinking": "current thought", "source": "agy_session_db"}])
        self.assertEqual(metadata.read_session_models(self.temp.name, cid_a, {7}), [])
        self.assertNotIn("SECRET", json.dumps(events))
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE gen_metadata SET data=? WHERE idx=1", (field(2, b"\x07") + field(4, b"run-a"),))
        connection.close()
        self.assertEqual(metadata.read_session_thinking(self.temp.name, cid_a, {7, 8}, {7, 8, 9}), events)
        self.assertEqual(metadata.read_session_models(self.temp.name, cid_a, {7}), [])

        snapshot_cid = str(uuid.uuid4())
        snapshot_path = self.database(snapshot_cid, [generation()], payloads={7: field(20, field(3, b"old snapshot"))})
        connection = sqlite3.connect(snapshot_path)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.close()
        original_connect, changed = sqlite3.connect, []

        def connect_with_concurrent_writer(*args, **kwargs):
            reader = original_connect(*args, **kwargs)

            def trace(query):
                if "FROM gen_metadata LIMIT" in query and not changed:
                    writer = original_connect(snapshot_path)
                    try:
                        writer.execute("UPDATE steps SET metadata=?, step_payload=? WHERE idx=7",
                                       (field(12, b"new-run"), field(20, field(3, b"NEW_SNAPSHOT_SECRET"))))
                        writer.execute("UPDATE gen_metadata SET data=?", (generation(execution="new-run"),))
                        writer.commit()
                        changed.append(True)
                    finally:
                        writer.close()

            reader.set_trace_callback(trace)
            return reader

        with patch.object(sqlite3, "connect", connect_with_concurrent_writer):
            snapshots = metadata.read_session_thinking(self.temp.name, snapshot_cid, {7})
        self.assertEqual(changed, [True])
        self.assertEqual([event["thinking"] for event in snapshots], ["old snapshot"])

    def test_missing_metadata_does_not_verify_selected_or_reported_model(self):
        info = self.evidence({"type": "init", "init": {"model": "gemini-3.8-flash-high"}})
        self.evidence({"type": "result", "result": {"model": "cli-generic-model"}}, info)
        self.assertEqual(info["requested_model"], "gemini-3.6-flash")
        self.assertEqual(info["cli_config_model"], "gemini-3.8-flash-high")
        self.assertEqual(info["cli_reported_model"], "cli-generic-model")
        self.assertEqual(info["returned_models"], [])
        self.assertEqual(info["status"], "unavailable")

    def test_different_alias_keeps_exact_returned_value_and_source(self):
        info = self.evidence({"type": "result", "result": {
            "response_metadata": {"modelVersion": "gemini-3.8-fn"}}})
        self.assertEqual(info["returned_models"], ["gemini-3.8-fn"])
        self.assertEqual(info["status"], "available")
        self.assertIn({"field": "result.response_metadata.modelVersion", "value": "gemini-3.8-fn",
                       "source": "cli_result"}, info["evidence"])

    def test_conflicting_returned_models_are_not_collapsed(self):
        info = self.evidence({"type": "step_update", "step_update": {"response_model": "first"}})
        self.evidence({"type": "result", "result": {"model_version": "second"}}, info)
        self.assertEqual(info["returned_models"], ["first", "second"])
        self.assertEqual(info["status"], "conflict")

    def test_only_known_metadata_scopes_are_read(self):
        for obj in ({"modelVersion": "root"},
                    {"result": {"modelVersion": "root-result"}},
                    {"step_update": {"response_model": "root-step"}},
                    {"type": "result", "result": {"responseModel": "result"}},
                    {"type": "step_update", "step_update": {"response_metadata": {"model_version": "step"}}},
                    {"type": "metadata", "response_metadata": {"response_model": "metadata"}}):
            self.assertEqual(len(self.evidence(obj)["returned_models"]), 1)
        for obj in ({"type": "init", "init": {"modelVersion": "config"}},
                    {"type": "result", "result": {"response": {"modelVersion": "body"},
                                                    "config": {"response_model": "config"}}},
                    {"type": "tool_call", "response_model": "tool"},
                    {"type": "thinking", "modelVersion": "thought"}):
            self.assertEqual(self.evidence(obj)["returned_models"], [])

    def test_parser_preserves_metadata_without_a_text_event(self):
        events = self.ns["_parse_ndjson_line"](json.dumps({
            "type": "step_update", "step_update": {"modelVersion": "raw-model", "responseId": "resp"}}))
        self.assertEqual(events[0]["event"], "metadata")
        self.assertEqual({r["value"] for r in events[0]["_response_evidence"]}, {"raw-model", "resp"})

    def test_only_step_container_model_metadata_has_step_coverage(self):
        for nested in (True, False):
            obj = {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response"}}
            (obj["step_update"] if nested else obj)["modelVersion"] = "raw-model"
            events = self.ns["_parse_ndjson_line"](json.dumps(obj))
            info = self.ns["_new_route_info"]("request-a")
            self.ns["_merge_response_evidence"](info, events[0]["_response_evidence"])
            self.ns["_update_route_coverage"](info, {7})
            self.assertEqual(info["status"], "available" if nested else "partial")
            self.assertEqual(info["model_step_indices"], [7] if nested else [])

    def test_thinking_delta_and_result_thinking_do_not_enter_body(self):
        cases = [{"type": "thinking", "delta": "thought"},
                 {"type": "step_update", "step_update": {"step_type": "thinking", "delta": "thought"}},
                 {"type": "step_update", "step_update": {"step_type": "agent_response", "thinking_delta": "thought"}},
                 {"type": "result", "result": {"thinking": "thought", "response": "body"}}]
        for obj in cases:
            events = self.ns["_parse_ndjson_line"](json.dumps(obj))
            thinking = [e for e in events if e["event"] == "thinking"]
            self.assertEqual([e["thinking"] for e in thinking], ["thought"])
            self.assertTrue(all("delta" not in e for e in thinking))
            self.assertFalse(any(e.get("delta") == "thought" for e in events))

    def test_metadata_url_removes_secrets_and_preserves_endpoint(self):
        clean = self.ns["_metadata_url"]("https://user:pass@[::1]:8443/v1/stream?key=secret#fragment")
        self.assertEqual(clean, "https://[::1]:8443/v1/stream")
        for value in ("file:///private", "https://host:bad/path", "https://[bad/path"):
            self.assertIsNone(self.ns["_metadata_url"](value))

    def test_request_log_allowlist_and_plural_values(self):
        log = Path(self.temp.name) / "request.log"
        log.write_text("selected override: fake-model modelVersion: fake\n"
                       "prompt: streamGenerateContent ResponseID: fake Trace: body-trace URL: https://body/text\n"
                       "[streamGenerateContent] ResponseID: first\n"
                       "[streamGenerateContent] ResponseID: second\n", encoding="utf-8")
        info = self.ns["_new_route_info"]("request-a")
        self.ns["_merge_response_evidence"](info, self.ns["_request_log_evidence"](log))
        self.evidence({"type": "metadata", "response_metadata": {"trace_id": "trace-a", "upstream_url": "https://user:pass@one/api?secret=yes"}}, info)
        self.evidence({"type": "metadata", "response_metadata": {"trace_id": "trace-b", "upstream_url": "https://two/api#secret"}}, info)
        self.assertEqual(info["response_ids"], ["first", "second"])
        self.assertEqual(info["trace_ids"], ["trace-a", "trace-b"])
        self.assertEqual(info["upstream_urls"], ["https://one/api", "https://two/api"])
        self.assertEqual(info["returned_models"], [])
        self.assertNotIn("secret", json.dumps(info))

    async def collect(self, objects, log_text="", *, block=False, fail=False):
        captured = {}

        async def launch(*argv, **kwargs):
            captured["log_path"] = Path(argv[argv.index("--log-file") + 1])
            captured["log_path"].write_text(log_text, encoding="utf-8")
            if fail:
                raise OSError("synthetic launch failure")
            captured["process"] = FakeProcess(objects, block=block)
            return captured["process"]

        captured["events"] = []
        with patch.object(asyncio, "create_subprocess_exec", launch):
            try:
                async for event in self.ns["_stream_agy"]("synthetic prompt", "requested"):
                    captured["events"].append(event)
            except OSError as error:
                captured["error"] = str(error)
        self.assertFalse(captured["log_path"].parent.exists())
        return captured

    async def test_stream_history_request_isolation_and_final_snapshot(self):
        a = await self.collect([{"type": "init", "init": {"model": "configured-a"}},
                                {"type": "result", "result": {"modelVersion": "returned-a"}}],
                               "[streamGenerateContent] ResponseID: response-a\n")
        old_snapshot = json.dumps(a["events"])
        b = await self.collect([{"type": "init", "init": {"model": "configured-b"}},
                                {"type": "result", "result": {"response": "body"}}], "[streamGenerateContent] ResponseID: response-b\n")
        info_a = a["events"][-1]["route_info"]
        info_b = b["events"][-1]["route_info"]
        self.assertEqual(a["events"][-1]["event"], "metadata")
        self.assertEqual(a["events"][0]["route_info"]["returned_models"], [])
        self.assertEqual(info_a["returned_models"], ["returned-a"])
        self.assertEqual(info_b["returned_models"], [])
        self.assertEqual(info_b["response_ids"], ["response-b"])
        self.assertNotEqual(info_a["request_id"], info_b["request_id"])
        self.assertNotEqual(a["log_path"], b["log_path"])
        self.assertEqual(json.dumps(a["events"]), old_snapshot)
        self.assertTrue(all("_response_evidence" not in e for e in a["events"]))

    async def test_stream_reads_only_observed_response_steps_from_actual_cli_home(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation("current-model", full="provider/current-model"),
                            generation("old-model", (1,), "old-run"),
                            generation("planning-model", (8,), "planning-run")],
                      ((1, "old-run"), (7, "run-a"), (8, "planning-run")))
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        capture = await self.collect([
            {"type": "init", "init": {"conversation_id": cid, "model": "configured"}},
            {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "body"}},
            {"type": "step_update", "step_update": {"step_index": 8, "step_type": "planning"}},
            {"type": "result", "result": {"response": "body"}}])
        info = capture["events"][-1]["route_info"]
        self.assertEqual(info["returned_models"], ["current-model"])
        self.assertEqual(info["returned_model_fulls"], ["provider/current-model"])
        self.assertTrue(all("_step_index" not in e and "_step_type" not in e for e in capture["events"]))
        self.assertEqual(capture["events"][0]["route_info"]["returned_models"], [])

    async def test_only_unambiguous_original_cli_init_can_select_session_database(self):
        cid_a, cid_b = str(uuid.uuid4()), str(uuid.uuid4())
        self.database(cid_a, [generation("model-a")])
        self.database(cid_b, [generation("model-b")])
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        step = {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "body"}}
        cases = [([step, {"type": "metadata", "conversation_id": cid_b}], []),
                 ([{"type": "init", "conversation_id": cid_a}, step,
                   {"type": "metadata", "conversation_id": cid_b}], ["model-a"]),
                 ([{"type": "init", "conversation_id": cid_a}, step,
                   {"type": "init", "conversation_id": cid_b}], [])]
        for objects, expected in cases:
            captured = await self.collect(objects)
            self.assertEqual(captured["events"][-1]["route_info"]["returned_models"], expected)
            self.assertTrue(all("_cli_init_cid" not in event for event in captured["events"]))

    async def test_partial_model_coverage_counts_response_steps_and_excludes_tools(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation("only-step-seven")])
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        objects = [{"type": "init", "conversation_id": cid},
                   {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "a"}},
                   {"type": "step_update", "step_update": {"step_index": 8, "step_type": "agent_response", "text_delta": "b"}},
                   {"type": "step_update", "step_update": {"step_index": 9, "step_type": "tool", "tool_name": "test"}}]
        capture = await self.collect(objects)
        info = capture["events"][-1]["route_info"]
        self.assertEqual(info["status"], "partial")
        self.assertEqual(info["returned_models"], ["only-step-seven"])
        self.assertEqual(info["observed_step_indices"], [7, 8])
        self.assertEqual(info["model_step_indices"], [7])
        self.assertEqual(info["uncovered_step_indices"], [8])

    async def test_response_without_valid_step_index_keeps_coverage_unknown(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation("known-model")])
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        for invalid in (None, -1, True, "8"):
            captured = await self.collect([
                {"type": "init", "conversation_id": cid},
                {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "known"}},
                {"type": "step_update", "step_update": {"step_index": invalid, "step_type": "agent_response", "text_delta": "unassociated"}}])
            info = captured["events"][-1]["route_info"]
            self.assertEqual(info["status"], "partial")
            self.assertTrue(info["coverage_unknown"])
            self.assertEqual(info["observed_step_indices"], [7])
            self.assertEqual(info["uncovered_step_indices"], [])

    async def test_optional_metadata_failure_does_not_discard_completed_body(self):
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}

        def failed_read(*args):
            raise RuntimeError("synthetic optional evidence failure")

        self.ns["read_session_models"] = failed_read
        capture = await self.collect([
            {"type": "init", "conversation_id": str(uuid.uuid4())},
            {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "body"}},
            {"type": "result", "result": {"response": "body"}}])
        self.assertEqual(capture["events"][-1]["route_info"]["status"], "unavailable")
        self.assertTrue(any(event.get("delta") == "body" for event in capture["events"]))
        self.assertNotIn("error", capture)

    async def test_structured_thinking_has_step_index_and_native_snapshot_precedes_final_metadata(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation("first", (7,), "run-a"), generation("second", (8,), "run-b")],
                      ((7, "run-a"), (8, "run-b")),
                      {7: field(20, field(3, b"complete thought seven")),
                       8: field(20, field(3, b"complete thought eight"))})
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        captured = await self.collect([
            {"type": "init", "conversation_id": cid},
            {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "body", "thinking_delta": "increment seven"}},
            {"type": "step_update", "step_update": {"step_index": 8, "step_type": "agent_response", "text_delta": "more", "thinking_delta": "increment eight"}}])
        thoughts = [event for event in captured["events"] if event["event"] == "thinking"]
        self.assertEqual([(event["step_index"], event["thinking"]) for event in thoughts[:2]],
                         [(7, "increment seven"), (8, "increment eight")])
        self.assertTrue(all("mode" not in event and "delta" not in event for event in thoughts[:2]))
        self.assertEqual([(event["step_index"], event["mode"]) for event in thoughts[2:]], [(7, "snapshot"), (8, "snapshot")])
        self.assertEqual(captured["events"][-1]["event"], "metadata")
        self.assertEqual(captured["events"][-2], thoughts[-1])
        self.assertEqual("".join(event.get("delta", "") for event in captured["events"]), "bodymore")

    async def test_timeout_keeps_body_error_and_metadata_with_native_thinking_or_read_failure(self):
        cid = str(uuid.uuid4())
        self.database(cid, [generation()], payloads={7: field(20, field(3, b"available thought") + field(14, b"SIGNATURE_SECRET"))})
        self.ns["_agy_environment"] = lambda: {"HOME": self.temp.name}
        self.ns["AGY_TIMEOUT"] = 0.03
        objects = [{"type": "init", "conversation_id": cid},
                   {"type": "step_update", "step_update": {"step_index": 7, "step_type": "agent_response", "text_delta": "answer"}}]
        captured = await self.collect(objects, block=True)
        events = captured["events"]
        self.assertEqual("".join(event.get("delta", "") for event in events), "answer")
        self.assertTrue(any(event["event"] == "error" for event in events))
        self.assertEqual(events[-2]["thinking"], "available thought")
        self.assertEqual(events[-2]["mode"], "snapshot")
        self.assertEqual(events[-1]["event"], "metadata")
        self.assertNotIn("SIGNATURE_SECRET", json.dumps(events))

        def failed_read(*args):
            raise OSError("synthetic thinking read failure")

        self.ns["read_session_thinking"] = failed_read
        captured = await self.collect(objects, block=True)
        self.assertTrue(any(event["event"] == "error" for event in captured["events"]))
        self.assertEqual(captured["events"][-1]["event"], "metadata")
        self.assertEqual("".join(event.get("delta", "") for event in captured["events"]), "answer")
        self.assertFalse(any(event["event"] == "thinking" for event in captured["events"]))

    async def test_timeout_cleans_request_log_and_sends_metadata(self):
        self.ns["AGY_TIMEOUT"] = 0.01
        capture = await self.collect([], "[streamGenerateContent] ResponseID: timed-response\n", block=True)
        self.assertEqual(capture["process"].returncode, -9)
        self.assertEqual(capture["events"][-1]["route_info"]["response_ids"], ["timed-response"])
        self.assertTrue(any(e["event"] == "error" for e in capture["events"]))

    async def test_start_failure_cleans_request_log(self):
        capture = await self.collect([], fail=True)
        self.assertIn("synthetic launch failure", capture["error"])
        self.assertEqual(capture["events"][-1]["route_info"]["status"], "unavailable")

    async def test_closed_stream_cleans_and_reaps_process(self):
        log_paths, processes = [], []

        async def launch(*argv, **kwargs):
            log_paths.append(Path(argv[argv.index("--log-file") + 1]))
            processes.append(FakeProcess([{"type": "init", "init": {"model": "configured"}}], block=True))
            return processes[-1]

        with patch.object(asyncio, "create_subprocess_exec", launch):
            stream = self.ns["_stream_agy"]("synthetic", "requested")
            self.assertEqual((await anext(stream))["event"], "init")
            await stream.aclose()
        self.assertFalse(log_paths[0].parent.exists())
        self.assertEqual(processes[0].returncode, -9)

    async def test_cancel_during_blocked_read_cleans_and_reaps_process(self):
        launched = asyncio.Event()
        captured = {}

        async def launch(*argv, **kwargs):
            captured["log_path"] = Path(argv[argv.index("--log-file") + 1])
            captured["process"] = FakeProcess(block=True)
            launched.set()
            return captured["process"]

        async def consume():
            async for event in self.ns["_stream_agy"]("synthetic", "requested"):
                pass

        with patch.object(asyncio, "create_subprocess_exec", launch):
            task = asyncio.create_task(consume())
            await asyncio.wait_for(launched.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(captured["log_path"].parent.exists())
        self.assertEqual(captured["process"].returncode, -9)

    async def test_openai_compatibility_model_is_separate_from_evidence(self):
        async def resolve(req):
            return "synthetic", None, False, "test"

        async def stream(**kwargs):
            yield {"event": "delta", "delta": "body"}
            yield {"event": "thinking", "thinking": "private thought"}
            yield {"event": "metadata", "route_info": self.evidence({"modelVersion": "returned"})}

        self.ns["_resolve_prompt_and_session"] = resolve
        self.ns["_stream_agy"] = stream
        req = types.SimpleNamespace(messages=[{}], model="requested", cwd=None)
        result = await self.ns["openai_chat"](req)
        self.assertEqual(result["model"], "requested")
        self.assertEqual(result["requested_model"], "requested")
        self.assertEqual(result["response_metadata"]["returned_models"], ["returned"])
        self.assertEqual(result["choices"][0]["message"]["content"], "body")


if __name__ == "__main__":
    unittest.main(verbosity=2)
