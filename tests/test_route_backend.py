"""Run actual route/chat/argv/stream functions offline; never import the service."""
from __future__ import annotations

import __future__
import ast
import asyncio
import codecs
import json
from pathlib import Path
import re
import tempfile
import types
import unittest
from unittest.mock import patch
import urllib.parse
import uuid


APP = Path(__file__).resolve().parents[1] / "app.py"
FUNCTIONS = {
    "_sanitize_token", "_new_route_info", "_metadata_url", "_response_evidence",
    "_merge_response_evidence", "_request_log_evidence", "_parse_ndjson_line",
    "_update_route_coverage", "_stream_agy", "_build_argv", "chat",
    "inspect_selected_route",
}


class Response:
    def __init__(self, body, **kwargs):
        self.body_iterator = body


class HTTPError(Exception):
    def __init__(self, status_code, detail):
        self.status_code = status_code
        super().__init__(detail)


class Reader:
    def __init__(self, chunks=(), block=False):
        self.chunks = list(chunks)
        self.block = block

    async def read(self, _):
        if self.chunks:
            return self.chunks.pop(0)
        if self.block:
            await asyncio.Event().wait()
        return b""


class Process:
    def __init__(self, objects=(), block=False, exit_code=0, newline=True):
        encoded = "\n".join(json.dumps(obj) for obj in objects)
        if encoded and newline:
            encoded += "\n"
        self.stdout = Reader([encoded.encode()] if encoded else [], block=block)
        self.stderr = Reader()
        self.returncode = None
        self.exit_code = exit_code
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        if self.returncode is None:
            self.returncode = self.exit_code
        return self.returncode


def load_functions(scratch, saved):
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

    async def save(user, cid):
        saved.append((user, cid))

    async def resolve(req):
        return "Synthetic prompt", None, False, "synthetic-user"

    ns = dict(json=json, Path=Path, re=re, urllib=urllib, uuid=uuid,
              tempfile=tempfile, asyncio=asyncio, codecs=codecs, SCRATCH=scratch,
              AGY_BIN="fake-agy", AGY_TIMEOUT=0, AGY_SANDBOX=False,
              AGY_OUTPUT_FORMAT="stream-json", AGY_PERSIST_CONVERSATIONS=True,
              _SSE_HEARTBEAT_SECONDS=15,
              _within_allowed=lambda cwd: cwd, _agy_environment=lambda: {},
              logger=types.SimpleNamespace(warning=lambda *args: None),
              StreamingResponse=Response, HTTPException=HTTPError,
              _resolve_prompt_and_session=resolve, save_conversation_async=save,
              read_session_models=lambda *args: [], read_session_thinking=lambda *args: [])
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(APP), "exec",
                 flags=__future__.annotations.compiler_flag), ns)
    return ns


def events(model="gemini-3.8-flash-exp-b", *, full=None, cid=None):
    evidence = {"modelVersion": model} if model else {}
    if full:
        evidence["response_model_full"] = full
    return [{"type": "init", "init": {"conversation_id": cid or str(uuid.uuid4())}},
            {"type": "step_update", "step_update": {
                "step_index": 7, "step_type": "agent_response", "text_delta": "OK",
                **evidence}},
            {"type": "result", "result": {"response": "OK", "status": "SUCCESS"}}]


class RouteBackendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="agy-route-offline-")
        self.scratch = Path(self.temp.name)
        self.saved = []
        self.ns = load_functions(self.scratch, self.saved)
        self.launches = []
        self.processes = []

    def tearDown(self):
        self.temp.cleanup()

    def launch(self, objects=None, **process_options):
        async def fake_launch(*argv, **kwargs):
            self.launches.append((list(argv), kwargs))
            process = Process(events() if objects is None else objects, **process_options)
            self.processes.append(process)
            return process
        return patch.object(asyncio, "create_subprocess_exec", fake_launch)

    async def chat(self, model, thinking):
        req = types.SimpleNamespace(messages=[object()], model=model,
                                    thinking=thinking, cwd=None)
        response = await self.ns["chat"](req)
        records = []
        async for chunk in response.body_iterator:
            records.append(json.loads(chunk.removeprefix("data: ").strip()))
        return records

    def assert_clean(self):
        self.assertFalse(list(self.scratch.glob("agy-route-*")))
        self.assertFalse(list(self.scratch.glob("agy-model-check-*")))

    async def test_thinking_on_and_off_preserve_every_explicit_effort_preset(self):
        for preset in ("low", "medium", "high"):
            for thinking in (False, True):
                with self.subTest(preset=preset, thinking=thinking), self.launch():
                    records = await self.chat("gemini-3.8-flash-" + preset, thinking)
                argv = self.launches[-1][0]
                self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-" + preset)
                self.assertNotIn("--effort", argv)
                self.assertEqual(records[-1]["event"], "done")
                self.assertEqual(records[-2]["route_info"]["returned_models"], ["gemini-3.8-flash-exp-b"])
        self.assert_clean()

    def test_argv_keeps_prompt_separate_and_manual_effort_is_still_explicit(self):
        argv = self.ns["_build_argv"]("gemini-3.8-flash-medium", prompt="--model fake",
                                     target_cwd=str(self.scratch), effort="medium")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")
        self.assertEqual(argv[-1], "-p=--model fake")
        self.assertNotIn("-p", argv)

    async def test_default_chat_does_not_inject_high_effort(self):
        with self.launch():
            await self.chat("default", True)
        argv = self.launches[-1][0]
        self.assertNotIn("--effort", argv)
        self.assertNotIn("--model", argv)

    async def test_historical_result_error_does_not_fail_completed_current_reply(self):
        objects = events()
        objects[-1]['result'].update(status='ERROR', error='Old capacity error')
        self.ns['read_session_historical_error'] = lambda *args: {'step_index': 2, 'error': 'Old capacity error'}
        self.ns['_agy_environment'] = lambda: {'HOME': str(self.scratch)}
        with self.launch(objects):
            records = await self.chat('gemini-3.8-flash-medium', True)
        self.assertFalse([record for record in records if record['event'] == 'error'])
        notice = next(record for record in records if record['event'] == 'notice')
        self.assertEqual(notice['scope'], 'historical')
        self.assertEqual(notice['detail'], 'Old capacity error')
        self.assertNotIn('error', notice)  # The existing UI treats any error field as a failure.
        self.assertTrue(any(record.get('response') == 'OK' for record in records))

    async def test_current_unknown_or_nonzero_exit_preserves_result_error(self):
        for proof, exit_code in ((None, 0), ({'step_index':2, 'error':'Old capacity error'}, 1)):
            objects = events()
            objects[-1]['result'].update(status='ERROR', error='Old capacity error')
            self.ns['read_session_historical_error'] = lambda *args: proof
            self.ns['_agy_environment'] = lambda: {'HOME': str(self.scratch)}
            with self.subTest(proof=proof,exit_code=exit_code), self.launch(objects,exit_code=exit_code):
                records = await self.chat('gemini-3.8-flash-medium', True)
            self.assertTrue(any(record.get('error') == 'Old capacity error' and record['event'] == 'error' for record in records))
            self.assertFalse(any(record['event'] == 'notice' for record in records))

    async def test_probe_has_own_cwd_no_resume_no_history_write_and_exact_evidence(self):
        with self.launch():
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="gemini-3.8-flash-medium"))
        argv, options = self.launches[-1]
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["returned_models"], ["gemini-3.8-flash-exp-b"])
        self.assertEqual(result["data"]["requested_model"], "gemini-3.8-flash-medium")
        self.assertEqual(self.saved, [])
        self.assertNotIn("--continue", argv)
        self.assertNotIn("--conversation", argv)
        self.assertNotIn("--effort", argv)
        self.assertTrue(Path(options["cwd"]).is_relative_to(self.scratch))
        self.assertTrue(Path(options["cwd"]).name.startswith("agy-model-check-"))
        self.assertIn("Do not use tools or access files.", argv[-3] if argv[-3].startswith("-p=") else " ".join(argv))
        self.assert_clean()

    async def test_probe_unterminated_last_init_does_not_register_history(self):
        cid = str(uuid.uuid4())
        with self.launch([{"type": "init", "init": {"conversation_id": cid}}], newline=False):
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
        self.assertEqual(self.saved, [])
        self.assertEqual(result["data"]["status"], "unavailable")
        self.assert_clean()

    async def test_normal_chat_still_registers_conversation(self):
        cid = str(uuid.uuid4())
        with self.launch(events(cid=cid)):
            await self.chat("medium", True)
        self.assertEqual(self.saved, [("synthetic-user", cid)])

    async def test_full_only_snake_and_camel_case_are_available_with_step_coverage(self):
        for field in ("response_model_full", "responseModelFull"):
            objects = events(model=None)
            objects[1]["step_update"][field] = "provider/gemini-3.8-flash-exp-b"
            with self.subTest(field=field), self.launch(objects):
                result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
            self.assertEqual(result["data"]["returned_models"], [])
            self.assertEqual(result["data"]["returned_model_fulls"], ["provider/gemini-3.8-flash-exp-b"])
            self.assertEqual(result["data"]["status"], "available")
            self.assertEqual(result["data"]["model_step_indices"], [7])

    async def test_previous_fn_probe_does_not_contaminate_new_exp_b_probe(self):
        with self.launch(events(model="gemini-3.8-flash-n")):
            first = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="high"))
        first_snapshot = json.dumps(first)
        with self.launch(events(model="gemini-3.8-flash-exp-b")):
            second = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
        self.assertEqual(first["data"]["returned_models"], ["gemini-3.8-flash-n"])
        self.assertEqual(second["data"]["returned_models"], ["gemini-3.8-flash-exp-b"])
        self.assertNotEqual(first["data"]["request_id"], second["data"]["request_id"])
        self.assertEqual(json.dumps(first), first_snapshot)

    async def test_missing_evidence_does_not_use_request_model_or_old_result(self):
        with self.launch(events(model=None)):
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="gemini-3.8-flash-medium"))
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["status"], "unavailable")
        self.assertEqual(result["data"]["returned_models"], [])
        self.assertIn("没有返回", result["message"])

    async def test_nonzero_exit_is_failed_while_retaining_current_evidence(self):
        with self.launch(exit_code=2):
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["returned_models"], ["gemini-3.8-flash-exp-b"])
        self.assert_clean()

    async def test_launch_failure_is_failed_and_cleans_probe_and_log_directories(self):
        async def fail(*args, **kwargs):
            raise OSError("synthetic launch failure")
        with patch.object(asyncio, "create_subprocess_exec", fail):
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
        self.assertFalse(result["ok"])
        self.assertEqual(result["data"]["returned_models"], [])
        self.assert_clean()

    async def test_probe_timeout_kills_process_and_removes_own_temp_files(self):
        real_wait_for = asyncio.wait_for
        async def brief_wait_for(awaitable, timeout):
            return await real_wait_for(awaitable, 0.02 if timeout == 90 else timeout)
        with self.launch(block=True), patch.object(asyncio, "wait_for", brief_wait_for):
            result = await self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium"))
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["message"])
        self.assertTrue(self.processes[-1].killed)
        self.assertEqual(self.saved, [])
        self.assert_clean()

    async def test_task_cancel_kills_process_without_history_registration(self):
        with self.launch(block=True):
            task = asyncio.create_task(self.ns["inspect_selected_route"](types.SimpleNamespace(model="medium")))
            while not self.processes:
                await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(self.processes[-1].killed)
        self.assertEqual(self.saved, [])
        self.assert_clean()

    async def test_empty_chat_is_rejected_before_any_process_launch(self):
        with self.launch(), self.assertRaises(HTTPError) as caught:
            await self.ns["chat"](types.SimpleNamespace(messages=[]))
        self.assertEqual(caught.exception.status_code, 422)
        self.assertEqual(self.launches, [])

    def enable_desktop(self):
        bridges = []
        class Bridge:
            def __init__(self, *args):
                self.args = args
                self.url = 'http://127.0.0.1:12345/synthetic-private-prefix'
                self.closed = False
                bridges.append(self)
            def close(self):
                self.closed = True
        self.ns['DesktopProfileProxy'] = Bridge
        self.ns['_agy_environment'] = lambda: {'AGY_UPSTREAM_PROFILE':'desktop',
            'AGY_DESKTOP_VERSION':'2.22.0', 'HOME':str(self.scratch)}
        return bridges

    async def test_desktop_profile_keeps_medium_and_raw_response_then_closes(self):
        bridges = self.enable_desktop()
        with self.launch():
            result = await self.ns['inspect_selected_route'](types.SimpleNamespace(model='gemini-3.8-flash-medium'))
        self.assertEqual(result['data']['returned_models'], ['gemini-3.8-flash-exp-b'])
        self.assertEqual(bridges[0].args, ('https://daily-cloudcode-pa.googleapis.com','2.22.0',0))
        self.assertEqual(self.launches[0][1]['env']['CLOUD_CODE_URL'], bridges[0].url)
        self.assertTrue(bridges[0].closed)
        self.assert_clean()

    async def test_desktop_profile_closes_when_native_launch_fails(self):
        bridges = self.enable_desktop()
        async def fail(*args, **kwargs): raise OSError('synthetic launch failure')
        with patch.object(asyncio,'create_subprocess_exec',fail):
            result = await self.ns['inspect_selected_route'](types.SimpleNamespace(model='medium'))
        self.assertFalse(result['ok'])
        self.assertTrue(bridges[0].closed)
        self.assert_clean()

    async def test_desktop_profile_closes_when_request_is_cancelled(self):
        bridges = self.enable_desktop()
        with self.launch(block=True):
            task = asyncio.create_task(self.ns['inspect_selected_route'](types.SimpleNamespace(model='medium')))
            while not self.processes: await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(bridges[0].closed)
        self.assertTrue(self.processes[0].killed)
        self.assert_clean()


if __name__ == "__main__":
    unittest.main(verbosity=2)
