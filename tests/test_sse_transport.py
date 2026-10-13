"""Use the actual HTTP adapter and detached owner with synthetic CLI streams."""
from __future__ import annotations
import ast
import asyncio
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import Mock
from fastapi.responses import StreamingResponse
from fastapi import HTTPException
from detached_chat import TurnManager, _extract

def load_chat(stream, directory):
    source = Path(__file__).resolve().parents[1] / 'app.py'
    nodes = [node for node in ast.parse(source.read_text(encoding='utf-8')).body
             if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
             and node.name in ('chat', '_turn_response')]
    for node in nodes: node.decorator_list = []
    async def resolve(_req):
        return 'synthetic prompt', 'synthetic-session', False, 'test-user'
    logger = Mock()
    namespace = {'asyncio': asyncio, 'json': json, 'StreamingResponse': StreamingResponse,
                 '_stream_agy': stream, '_resolve_prompt_and_session': resolve,
                 '_SSE_HEARTBEAT_SECONDS': 0.01, 'logger': logger, 'ChatRequest': object,
                 'web_turns': TurnManager(directory, logger), 'HTTPException': HTTPException}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(source), 'exec'), namespace)
    return namespace

def event(block):
    return json.loads(block.removeprefix('data: ').strip())

class SseTransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(); self.directory = Path(self.temporary.name)
    async def asyncTearDown(self):
        for job in getattr(self, 'namespace', {}).get('web_turns', types.SimpleNamespace(turns={})).turns.values():
            if not job.task.done(): await job.manager.stop(job.id)
        self.temporary.cleanup()
    async def response(self, stream):
        self.namespace = load_chat(stream, self.directory)
        request = types.SimpleNamespace(messages=[{}], model='selected-medium', cwd=None,
            message_snapshot=[{'role': 'user', 'content': 'synthetic prompt', 'attachments': [{'filename': 'test.txt'}]}],
            turn_id=None, model_label='Selected Medium')
        self.request = request
        response = await self.namespace['chat'](request)
        self.job = next(iter(self.namespace['web_turns'].turns.values())); return response

    async def test_idle_heartbeats_keep_one_producer_and_preserve_metadata(self):
        release, cleaned = asyncio.Event(), asyncio.Event(); reads, calls = [], []
        async def stream(**kwargs):
            reads.append(asyncio.current_task()); calls.append(kwargs)
            try:
                await release.wait()
                yield {'event': 'metadata', 'route_info': {'returned_models': ['future-exp-c']}}
            finally: cleaned.set()
        response = await self.response(stream); iterator = response.body_iterator
        self.assertEqual(event(await anext(iterator))['event'], 'start')
        self.assertEqual(event(await anext(iterator))['event'], 'snapshot')
        for _ in range(3): self.assertEqual(await asyncio.wait_for(anext(iterator), 1), ': keep-alive\n\n')
        self.assertEqual(len(reads), 1); self.assertFalse(reads[0].done())
        release.set(); frames = [event(block) async for block in iterator if block.startswith('data:')]
        self.assertEqual(frames[-1]['event'], 'done')
        self.assertEqual(self.job.assistant['routeInfo']['returned_models'], ['future-exp-c'])
        self.assertTrue(cleaned.is_set()); self.assertEqual(calls[0]['model'], 'selected-medium')
        self.assertEqual(calls[0]['conversation_id'], 'synthetic-session'); self.assertEqual(calls[0]['timeout'], 0)

    async def test_completed_stream_saves_answer_thought_tool_route_usage_and_attachments(self):
        async def stream(**_kwargs):
            yield {'event': 'thinking', 'thinking': 'incremental', 'step_index': 7}
            yield {'event': 'thinking', 'thinking': 'full thought', 'step_index': 7, 'mode': 'snapshot'}
            yield {'event': 'tool', 'tool': 'search_web'}
            yield {'event': 'delta', 'delta': '<think>inline</think>answer'}
            yield {'event': 'result', 'response': 'answer', 'usage': {'input_tokens': 1, 'output_tokens': 2}, 'duration_seconds': 1.2}
            yield {'event': 'metadata', 'route_info': {'returned_models': ['actual-route']}}
        response = await self.response(stream); frames = [event(block) async for block in response.body_iterator if block.startswith('data:')]
        self.assertEqual(frames[-1]['status'], 'complete')
        saved = json.loads((self.directory / 'synthetic-session.json').read_text()); answer = saved[-1]
        self.assertEqual(answer['content'], 'answer'); self.assertEqual(answer['thinking'], 'full thought\n\ninline')
        self.assertEqual(answer['tool'], 'search_web'); self.assertEqual(answer['routeInfo']['returned_models'], ['actual-route'])
        self.assertEqual(answer['model'], 'Selected Medium'); self.assertIn('3 tokens', answer['metrics'])
        self.assertEqual(saved[-2]['attachments'][0]['filename'], 'test.txt')

    async def test_failure_is_saved_and_logs_only_error_type(self):
        async def stream(**_kwargs):
            raise RuntimeError('synthetic failure'); yield
        response = await self.response(stream); frames = [event(block) async for block in response.body_iterator if block.startswith('data:')]
        self.assertEqual(frames[-1]['status'], 'error'); self.assertIn('synthetic failure', self.job.assistant['content'])
        self.namespace['logger'].warning.assert_called_once_with('Chat stream failed (%s)', 'RuntimeError')

    async def test_http_disconnect_does_not_cancel_worker_and_result_is_resumable(self):
        entered, release, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def stream(**_kwargs):
            entered.set()
            try:
                await release.wait(); yield {'event': 'delta', 'delta': 'finished after disconnect'}
            finally:
                await asyncio.sleep(0); cleaned.set()
        response = await self.response(stream)
        async def receive():
            await entered.wait(); return {'type': 'http.disconnect'}
        async def send(_message): pass
        await asyncio.wait_for(response({'type': 'http', 'asgi': {'spec_version': '2.3'}}, receive, send), 1)
        self.assertFalse(self.job.task.done()); self.assertFalse(cleaned.is_set())
        release.set(); await asyncio.wait_for(self.job.task, 1); self.assertTrue(cleaned.is_set())
        frames = [frame async for frame in self.job.events()]
        self.assertEqual(frames[1]['message']['content'], 'finished after disconnect'); self.assertEqual(frames[-1]['status'], 'complete')

    async def test_explicit_stop_awaits_cleanup_and_releases_worker(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def stream(**_kwargs):
            entered.set()
            try:
                await asyncio.Event().wait(); yield
            finally:
                await asyncio.sleep(0); cleaned.set()
        await self.response(stream); await entered.wait(); await self.namespace['web_turns'].stop(self.job.id)
        self.assertTrue(cleaned.is_set()); self.assertTrue(self.job.task.done()); self.assertEqual(self.job.assistant['status'], 'stopped')

    async def test_old_transport_snapshots_cannot_overwrite_active_or_completed_answer(self):
        release = asyncio.Event()
        async def stream(**_kwargs):
            await release.wait(); yield {'event': 'delta', 'delta': 'authoritative answer'}
        await self.response(stream); await asyncio.sleep(0); manager = self.namespace['web_turns']
        old = [{'role': 'assistant', 'content': 'partial', 'turnId': self.job.id, 'status': 'error'}]
        active = manager.protect(self.job.cid, old, self.job.id); self.assertEqual(active[-1]['status'], 'streaming')
        release.set(); await self.job.task
        final = manager.protect(self.job.cid, old, self.job.id)
        self.assertEqual(final[-1]['content'], 'authoritative answer'); self.assertEqual(final[-1]['status'], 'complete')
        self.assertEqual(manager.protect(self.job.cid, []), [])

    async def test_stop_before_worker_starts_and_stop_before_submission(self):
        async def stream(**_kwargs):
            await asyncio.Event().wait(); yield
        await self.response(stream); await self.namespace['web_turns'].stop(self.job.id); self.assertEqual(self.job.status, 'stopped')
        manager = self.namespace['web_turns']; reserved = 'a' * 32; await manager.stop(reserved)
        with self.assertRaisesRegex(ValueError, 'explicitly stopped'):
            manager.start(lambda: stream(), None, [], None, turn_id=reserved)

    async def test_same_conversation_cannot_start_another_turn_while_disconnected(self):
        async def stream(**_kwargs):
            await asyncio.Event().wait(); yield
        response = await self.response(stream)
        await anext(response.body_iterator); await response.body_iterator.aclose()
        with self.assertRaises(HTTPException) as caught:
            await self.namespace['chat'](self.request)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(len(self.namespace['web_turns'].turns), 1)

    async def test_limited_replay_replaces_expired_events_with_a_complete_snapshot(self):
        async def stream(**_kwargs):
            for _ in range(600): yield {'event': 'delta', 'delta': 'x'}
        await self.response(stream); await self.job.task
        self.assertLessEqual(len(self.job.history), 256)
        frames = [frame async for frame in self.job.events(cursor=1)]
        self.assertEqual(frames[1]['message']['content'], 'x' * 600)
        self.assertEqual(frames[-1]['status'], 'complete')

    def test_think_tag_examples_keep_literal_markdown_and_escape_rules(self):
        raw = '`<think>code</think>`\n```html\n<think>sample</think>\n```\n<think>real</think>answer'
        parsed = _extract(raw); self.assertEqual(parsed['thinking'], 'real'); self.assertIn('<think>sample</think>', parsed['content'])
        self.assertEqual(_extract('<thi', True)['content'], ''); self.assertEqual(_extract('<thi')['content'], '<thi')

if __name__ == '__main__': unittest.main()
