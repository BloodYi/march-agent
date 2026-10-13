"""Own web turns independently of an HTTP reader; persist their existing UI data."""
from __future__ import annotations

import asyncio
from collections import deque
import copy
import json
from pathlib import Path
import re
import time
import uuid


def _extract(raw, streaming=False):
    """Match the browser's think-tag rules, including literal Markdown examples."""
    content, thought, thoughts = [], [], []
    depth, ticks, fence, tagged, index = 0, 0, None, False, 0
    def append(value):
        (thought if depth else content).append(value)
    def flush():
        value = ''.join(thought).strip()
        if value and value != '...':
            thoughts.append(value)
        thought.clear()
    while index < len(raw):
        tail = raw[index:]
        if not ticks and (index == 0 or raw[index - 1] == '\n'):
            quote = re.match(r'^(?: {0,3}>[ \t]?)+', tail)
            quote_depth = quote[0].count('>') if quote else 0
            if fence and fence[2] > quote_depth:
                fence = None
            marker = re.match(r'^(?: {0,3}>[ \t]?)* {0,3}(`{3,}|~{3,})([^\n]*)(?:\n|$)', tail)
            if marker:
                if not fence:
                    fence = (marker[1][0], len(marker[1]), quote_depth)
                elif marker[1][0] == fence[0] and len(marker[1]) >= fence[1] and not marker[2].strip():
                    fence = None
                append(marker[0]); index += len(marker[0]); continue
            indented = re.match(r'^(?: {4}|\t)[^\n]*(?:\n|$)', tail) if not fence else None
            if indented:
                append(indented[0]); index += len(indented[0]); continue
        if fence:
            append(raw[index]); index += 1; continue
        if not ticks and raw[index] == '\\' and index + 1 < len(raw):
            append(raw[index:index + 2]); index += 2; continue
        if raw[index] == '`':
            marker = re.match(r'^`+', tail)[0]
            append(marker)
            if not ticks and (streaming or any(len(value) == len(marker) for value in re.findall(r'`+', tail[len(marker):]))):
                ticks = len(marker)
            elif ticks == len(marker):
                ticks = 0
            index += len(marker); continue
        if not ticks and raw[index] == '<':
            tag = re.match(r'^</?think>', tail, re.I)
            if tag and (not tag[0].startswith('</') or depth):
                tagged = True
                if tag[0].startswith('</'):
                    depth -= 1
                    if not depth:
                        flush()
                else:
                    depth += 1
                index += len(tag[0]); continue
            if streaming and any(value.startswith(tail.lower()) for value in ('<think>', '</think>')):
                break
        append(raw[index]); index += 1
    if depth:
        flush()
    value = ''.join(content)
    return {'content': value.lstrip('\n') if tagged else value, 'thinking': '\n\n'.join(thoughts)}


class WebTurn:
    def __init__(self, manager, turn_id, stream, conversation_id, messages, model, model_label):
        self.manager, self.id, self.stream, self.cid = manager, turn_id, stream, conversation_id
        self.messages = copy.deepcopy(messages)
        if self.messages:
            self.messages[-1]['turnId'] = turn_id
        self.assistant = {'role': 'assistant', 'content': '', 'thinking': '', 'thoughtOpen': True,
                          'model': model_label, 'requestedModel': model or 'default',
                          'turnId': turn_id, 'status': 'streaming', 'rawText': '', 'finalResponse': '',
                          'eventThinking': '', 'thinkingOrder': [], 'errorText': ''}
        self.messages.append(self.assistant)
        self.status, self.sequence, self.history = 'running', 0, deque(maxlen=256)
        self.changed, self.saved_at, self.task, self.updated_at = asyncio.Event(), 0, None, time.time()
        self.stop_requested = False

    def info(self):
        return {'id': self.id, 'conversation_id': self.cid, 'status': self.status}

    def snapshot(self):
        return {'event': 'snapshot', 'turn_id': self.id, 'conversation_id': self.cid,
                'cursor': self.sequence, 'message': copy.deepcopy(self.assistant)}

    def publish(self, event):
        self.sequence += 1
        item = dict(event, turn_id=self.id, cursor=self.sequence)
        self.history.append((self.sequence, item))
        self.updated_at = time.time()
        self.changed.set()

    def save(self, force=False):
        if not self.cid or (not force and time.monotonic() - self.saved_at < 1):
            return
        # Synchronous replace serializes with API snapshot writes on this event loop.
        path = self.manager.directory / (self.cid + '.json')
        temporary = path.with_name(path.name + '.' + self.id + '.tmp')
        temporary.write_text(json.dumps(self.manager.serializable(self.messages), ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        self.saved_at = time.monotonic()

    def text(self):
        message = self.assistant
        raw, final = message['rawText'], message['finalResponse']
        streamed, complete = _extract(raw, self.status == 'running'), _extract(final)
        inline = complete['thinking'] if raw and final.startswith(raw) else streamed['thinking']
        final_thought = complete['thinking']
        if final_thought and not (final and raw.endswith(final)):
            if not inline or final_thought.startswith(inline):
                inline = final_thought
            elif not inline.startswith(final_thought):
                inline += '\n\n' + final_thought
        separate = message['eventThinking'].strip()
        message['thinking'] = '\n\n'.join(value for value in ('' if separate == '...' else separate, inline) if value)
        message['content'] = (complete['content'] or streamed['content']) + message['errorText']

    def absorb(self, event):
        message, kind = self.assistant, event.get('event')
        if kind == 'init' and event.get('conversation_id'):
            cid = event['conversation_id']
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,200}', cid):
                raise ValueError('Invalid conversation ID returned by CLI')
            self.cid = cid
            self.manager.by_conversation[cid] = self.id
        if event.get('route_info'):
            message['routeInfo'] = copy.deepcopy(event['route_info'])
        if kind == 'delta':
            message['rawText'] += str(event.get('delta') or event.get('text') or '')
            message['status'] = 'streaming'
        elif kind == 'thinking':
            chunk = str(event.get('thinking', event.get('delta', '')))
            step = event.get('step_index')
            order = message['thinkingOrder']
            if isinstance(step, int) and not isinstance(step, bool) and step >= 0:
                part = next((value for value in order if value.get('step_index') == step), None)
                if part is None:
                    part = {'step_index': step, 'text': ''}; order.append(part)
                part['text'] = chunk if event.get('mode') == 'snapshot' else part['text'] + chunk
            elif chunk:
                if order and 'step_index' not in order[-1]:
                    order[-1]['text'] += chunk
                else:
                    order.append({'text': chunk})
            message['eventThinking'] = '\n\n'.join(value['text'] for value in order if value['text'])
            if event.get('mode') != 'snapshot':
                message['status'] = 'streaming'
        elif kind == 'tool' or event.get('tool'):
            message['tool'] = event.get('tool') or event.get('name') or 'executing'
        elif kind == 'result':
            if event.get('response'):
                message['finalResponse'] = str(event['response'])
            usage = event.get('usage')
            if usage:
                total = usage.get('total_tokens', usage.get('input_tokens', 0) + usage.get('output_tokens', 0))
                details = []
                for key, label in (('input_tokens', '输入'), ('output_tokens', '输出'), ('thinking_tokens', '思考'), ('cache_read_tokens', '缓存')):
                    if key in usage and (key in ('input_tokens', 'output_tokens') or usage[key]):
                        details.append(f'{label} {usage[key]:,}')
                parts = [f'{total:,} tokens'] + ([f"({' / '.join(details)})"] if details else [])
                if event.get('duration_seconds'):
                    parts.append(f"· ⏱️ {event['duration_seconds']:.1f}s")
                message['metrics'] = ' '.join(parts)
        elif kind == 'error' or event.get('error'):
            message['status'] = 'error'
            value = f"_Error: {event.get('error') or 'Unknown error'}_"
            if value not in message['errorText']:
                message['errorText'] += '\n\n' + value
        self.text()

    async def run(self):
        outcome = 'error'
        try:
            self.save(True)
            async for event in self.stream:
                self.absorb(event)
                self.save(event.get('event') == 'init')
                self.publish(event)
            outcome = 'error' if self.assistant['status'] == 'error' else 'complete'
        except asyncio.CancelledError:
            outcome = 'stopped'
            self.assistant['errorText'] += '\n\n_Stopped: Response cancelled_'
        except Exception as error:
            outcome = 'error'
            self.absorb({'event': 'error', 'error': str(error)})
            self.publish({'event': 'error', 'error': str(error)})
            self.manager.logger.warning('Chat stream failed (%s)', type(error).__name__)
        finally:
            try:
                await self.stream.aclose()
            finally:
                self.status = outcome
                self.assistant['status'] = self.status
                self.text()
                try:
                    self.save(True)
                except Exception:
                    self.manager.logger.exception('Could not save background reply')
                self.publish(self.snapshot())
                self.publish({'event': 'done', 'status': self.status})

    async def events(self, cursor=0, heartbeat=15):
        yield dict({'event': 'start', 'turn_id': self.id, 'conversation_id': self.cid}, cursor=cursor)
        if not cursor or (self.history and cursor < self.history[0][0] - 1):
            yield self.snapshot()
            cursor = self.sequence
        while True:
            self.changed.clear()
            if self.history and cursor < self.history[0][0] - 1:
                yield self.snapshot()
                cursor = self.sequence
            available = [(number, event) for number, event in self.history if number > cursor]
            for number, event in available:
                cursor = number
                yield event
            if self.status != 'running' and cursor >= self.sequence:
                if not available or available[-1][1].get('event') != 'done':
                    yield {'event': 'done', 'turn_id': self.id, 'status': self.status, 'cursor': cursor}
                return
            try:
                await asyncio.wait_for(self.changed.wait(), heartbeat)
            except asyncio.TimeoutError:
                yield None


class TurnManager:
    def __init__(self, directory: Path, logger):
        self.directory, self.logger = directory, logger
        self.turns, self.by_conversation = {}, {}
        self.cancelled_ids = {}

    @staticmethod
    def serializable(messages):
        runtime = {'rawText', 'finalResponse', 'eventThinking', 'thinkingOrder', 'thinkingByStep', 'errorText', 'isThinking', '_id'}
        return [{key: value for key, value in message.items() if key not in runtime} for message in messages]

    def start(self, stream_factory, cid, messages, model, model_label='', turn_id=None):
        self.prune()
        turn_id = turn_id or uuid.uuid4().hex
        if not re.fullmatch(r'[a-f0-9]{32}', turn_id):
            raise ValueError('Invalid turn ID')
        if turn_id in self.turns:
            raise ValueError('This turn has already been submitted')
        if turn_id in self.cancelled_ids:
            raise ValueError('This turn was explicitly stopped before starting')
        previous = self.for_conversation(cid)
        if previous and previous.status == 'running':
            raise ValueError('This conversation is still running')
        job = WebTurn(self, turn_id, stream_factory(), cid, messages, model, model_label)
        self.turns[turn_id] = job
        if cid:
            self.by_conversation[cid] = turn_id
        job.task = asyncio.create_task(job.run(), name='agy-web-turn-' + turn_id)
        return job

    def for_conversation(self, cid):
        return self.turns.get(self.by_conversation.get(cid)) if cid else None

    def protect(self, cid, messages, transport_turn_id=None):
        job = self.for_conversation(cid)
        if not job or (job.status != 'running' and transport_turn_id != job.id):
            return messages
        # A reader's partial/error snapshot must not replace the server-owned reply.
        result = copy.deepcopy(messages)
        current_user = next((value for value in job.messages if value.get('turnId') == job.id and value.get('role') == 'user'), None)
        incoming = next((value for value in result if value.get('turnId') == job.id and value.get('role') == 'assistant'), None)
        answer = copy.deepcopy(job.assistant)
        if incoming and 'thoughtOpen' in incoming:
            answer['thoughtOpen'] = incoming['thoughtOpen']
        result = [value for value in result if not (value.get('turnId') == job.id and value.get('role') == 'assistant')]
        if current_user and not any(value.get('turnId') == job.id and value.get('role') == 'user' for value in result):
            result.append(copy.deepcopy(current_user))
        result.append(answer)
        job.messages = result
        job.assistant = answer
        return result

    async def stop(self, turn_id):
        job = self.turns.get(turn_id)
        if not job:
            if not re.fullmatch(r'[a-f0-9]{32}', turn_id):
                return None
            self.cancelled_ids[turn_id] = time.time()
            return {'id': turn_id, 'conversation_id': None, 'status': 'stopped'}
        if job.status == 'running' and not job.stop_requested:
            job.stop_requested = True
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                # Stop may arrive before asyncio first enters the worker.
                job.status = job.assistant['status'] = 'stopped'
                job.assistant['errorText'] += '\n\n_Stopped: Response cancelled_'
                job.text()
                job.save(True)
                job.publish(job.snapshot())
                job.publish({'event': 'done', 'status': 'stopped'})
        return job.info()

    def prune(self):
        for turn_id, created in list(self.cancelled_ids.items()):
            if time.time() - created > 3600:
                self.cancelled_ids.pop(turn_id)
        for turn_id, job in list(self.turns.items()):
            if job.status != 'running' and time.time() - job.updated_at > 3600:
                self.turns.pop(turn_id)
                if self.by_conversation.get(job.cid) == turn_id:
                    self.by_conversation.pop(job.cid)
