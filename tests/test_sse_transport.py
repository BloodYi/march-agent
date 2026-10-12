"""Exercise the actual chat generator without importing app or reading user data."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
import types
import unittest
from unittest.mock import Mock

from fastapi.responses import StreamingResponse


def load_chat(stream):
    source = Path(__file__).resolve().parents[1] / "app.py"
    node = next(node for node in ast.parse(source.read_text(encoding="utf-8")).body
                if isinstance(node, ast.AsyncFunctionDef) and node.name == "chat")
    node.decorator_list = []

    async def resolve(_req):
        return "synthetic prompt", "synthetic-session", False, "test-user"

    namespace = {"asyncio": asyncio, "json": json, "StreamingResponse": StreamingResponse,
                 "_stream_agy": stream, "_resolve_prompt_and_session": resolve,
                 "_SSE_HEARTBEAT_SECONDS": 0.01, "logger": Mock(), "ChatRequest": object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(source), "exec"), namespace)
    return namespace


def event(block):
    return json.loads(block.removeprefix("data: ").strip())


class SseTransportTests(unittest.IsolatedAsyncioTestCase):
    async def response(self, stream):
        self.namespace = load_chat(stream)
        request = types.SimpleNamespace(messages=[{}], model="selected-medium", cwd=None)
        return await self.namespace["chat"](request)

    async def test_idle_heartbeats_keep_one_pending_read_and_preserve_metadata(self):
        release, cleaned = asyncio.Event(), asyncio.Event()
        reads, calls = [], []
        metadata = {"event": "metadata", "route_info": {"returned_models": ["future-exp-c"]}}

        async def stream(**kwargs):
            reads.append(asyncio.current_task()); calls.append(kwargs)
            try:
                await release.wait()
                yield metadata
            finally:
                cleaned.set()

        response = await self.response(stream)
        iterator = response.body_iterator
        self.assertEqual(event(await anext(iterator)), {"event": "start"})
        for _ in range(3):
            self.assertEqual(await asyncio.wait_for(anext(iterator), 1), ": keep-alive\n\n")
        self.assertEqual(len(reads), 1)
        self.assertFalse(reads[0].done())
        release.set()
        self.assertEqual(event(await anext(iterator)), metadata)
        self.assertEqual(event(await anext(iterator)), {"event": "done"})
        with self.assertRaises(StopAsyncIteration):
            await anext(iterator)
        self.assertTrue(cleaned.is_set())
        self.assertEqual(calls[0]["model"], "selected-medium")
        self.assertEqual(calls[0]["conversation_id"], "synthetic-session")

    async def test_completed_stream_keeps_start_result_and_done_format(self):
        result = {"event": "result", "response": "synthetic answer", "usage": {"total_tokens": 2}}

        async def stream(**_kwargs):
            yield result

        response = await self.response(stream)
        blocks = [block async for block in response.body_iterator]
        self.assertEqual([event(block) for block in blocks], [{"event": "start"}, result, {"event": "done"}])
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    async def test_failure_is_structured_and_logs_only_error_type(self):
        cleaned = asyncio.Event()

        async def stream(**_kwargs):
            try:
                raise RuntimeError("synthetic failure")
                yield  # An async generator, despite failing before its first event.
            finally:
                cleaned.set()

        response = await self.response(stream)
        frames = [event(block) async for block in response.body_iterator]
        self.assertEqual(frames, [{"event": "start"}, {"event": "error", "error": "synthetic failure"},
                                  {"event": "done"}])
        self.assertTrue(cleaned.is_set())
        self.namespace["logger"].warning.assert_called_once_with("Chat stream failed (%s)", "RuntimeError")

    async def test_cancel_during_wait_cancels_and_awaits_the_underlying_read(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        reads = []

        async def stream(**_kwargs):
            reads.append(asyncio.current_task()); entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                await asyncio.sleep(0)
                cleaned.set()

        response = await self.response(stream)
        iterator = response.body_iterator
        await anext(iterator)
        consumer = asyncio.create_task(anext(iterator))
        await asyncio.wait_for(entered.wait(), 1)
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertTrue(cleaned.is_set())
        self.assertTrue(reads[0].cancelled())
        await iterator.aclose()

    async def test_close_after_a_heartbeat_cleans_the_pending_read(self):
        cleaned, reads = asyncio.Event(), []

        async def stream(**_kwargs):
            reads.append(asyncio.current_task())
            try:
                await asyncio.Event().wait()
                yield
            finally:
                cleaned.set()

        response = await self.response(stream)
        iterator = response.body_iterator
        await anext(iterator)
        self.assertEqual(await anext(iterator), ": keep-alive\n\n")
        await iterator.aclose()
        self.assertTrue(cleaned.is_set())
        self.assertTrue(reads[0].cancelled())

    async def test_http_disconnect_allows_async_cleanup_to_finish(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        reads = []

        async def stream(**_kwargs):
            reads.append(asyncio.current_task()); entered.set()
            try:
                await asyncio.Event().wait()
                yield
            finally:
                # Model the actual process/proxy cleanup, which also awaits.
                await asyncio.sleep(0)
                cleaned.set()

        response = await self.response(stream)

        async def receive():
            await entered.wait()
            return {"type": "http.disconnect"}

        async def send(_message):
            pass

        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.3"}},
                                        receive, send), 1)
        self.assertTrue(cleaned.is_set())
        self.assertTrue(reads[0].cancelled())


if __name__ == "__main__":
    unittest.main()
