// Run the maintained browser functions with synthetic streams; no network or tokens.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { test } from 'node:test';
import { webcrypto } from 'node:crypto';

const html = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
function section(start, end) {
  const first = html.indexOf(start), last = html.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `Missing browser function: ${start}`);
  return html.slice(first, last);
}
const browserCode = [
  section('    function normalizeMessageList(', '    function loadMessages('),
  section('    function appendError(', '    function refreshAssistantBody('),
  section('    function handleStreamEvent(', '    let pendingAttachments ='),
  section('    async function send()', '    async function stop()'),
  section('    async function stop()', '    // "New chat" now keeps old conversations'),
].join('\n');
const sse = events => events.map(event => `data: ${JSON.stringify(event)}\n\n`).join('');

async function runStream({ chunks = [], failure, beforeRead, fetchFailure, cancelFailure, httpFailure, initialCid = 'synthetic-cid' } = {}) {
  const storage = new Map(), requests = [], statuses = [], timers = [];
  let reads = 0, cancellations = 0, released = 0;
  const body = { classList: { add() {}, remove() {} } };
  const context = {
    console, AbortController, TextDecoder, Number, JSON, String, Map, crypto: webcrypto,
    setTimeout: callback => timers.push(callback), activeTurn: null,
    input: { value: 'synthetic prompt', style: {}, focus() {} },
    modelSel: { value: 'requested-model', selectedIndex: -1 },
    pendingAttachments: [], busy: false, activeController: null, conversationEpoch: 0,
    messages: [], currentConversationId: initialCid, currentCwd: null, userId: 'default',
    pendingNew: false, conversationWriteVersions: new Map(), messageSaveQueues: new Map(),
    messageSaveVersions: new Map(), messageSaveFailures: new Set(),
    localStorage: { setItem: (key, value) => storage.set(key, value), getItem: key => storage.get(key), removeItem: key => storage.delete(key) },
    msgKey: cid => 'agy_msgs_' + cid,
    document: { body: { getAttribute: () => 'on' } },
    chat: { querySelector: () => body },
    render() {}, updateComposer() {}, renderAttachmentsBar() {}, refreshAssistantBody() {},
    updateConversationPill() {}, loadConversations() {}, syncMessagesFromServer() {},
    scrollToBottom() {}, autoTitleConversation() {},
    setStatus: (...args) => statuses.push(args),
    syncAssistantText(message) {
      message.content = (message.finalResponse || message.rawText || '') + (message.errorText || '');
    },
    async apiFetch(url, options) {
      requests.push({ url, body: options?.body ? JSON.parse(options.body) : null });
      if (url !== '/api/chat') return { ok: true };
      if (fetchFailure) throw fetchFailure;
      if (httpFailure) return { ok: false, status: httpFailure.status, text: async () => httpFailure.detail };
      return { ok: true, body: { getReader: () => ({
        async read() {
          if (beforeRead) await beforeRead(context, reads);
          if (reads < chunks.length) return { value: Buffer.from(chunks[reads++]), done: false };
          reads++;
          if (failure) throw failure;
          return { done: true };
        },
        cancel() { cancellations++; if (cancelFailure) throw cancelFailure; return Promise.resolve(); },
        releaseLock() { released++; },
      }) } };
    },
  };
  vm.createContext(context);
  vm.runInContext(browserCode, context);
  await vm.runInContext('send()', context);
  await Promise.all(context.messageSaveQueues.values());
  const saved = JSON.parse(storage.get('agy_msgs_synthetic-cid') || '[]');
  return { context, saved, requests, statuses, reads, cancellations, released, timers, storage };
}

test('done stops reading before a late network error and tolerates cancel failure', async () => {
  const result = await runStream({
    chunks: [sse([{ event: 'delta', delta: 'complete reply' }, { event: 'done' }])],
    failure: new TypeError('network error'), cancelFailure: new TypeError('cancel error'),
  });
  assert.equal(result.context.messages.at(-1).status, 'complete');
  assert.equal(result.context.messages.at(-1).content, 'complete reply');
  assert.equal(result.reads, 1);
  assert.equal(result.cancellations, 1);
  assert.equal(result.released, 1);
  assert.equal(result.saved.at(-1).status, 'complete');
});

test('[DONE] also terminates the stream', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'delta', delta: 'reply' }]) + 'data: [DONE]\n\n'],
    failure: new TypeError('network error') });
  assert.equal(result.context.messages.at(-1).status, 'complete');
  assert.equal(result.reads, 1);
});

test('done split across chunks is recognized, including CRLF', async () => {
  const raw = sse([{ event: 'delta', delta: '完整内容' }]) + 'data: {"event":"done"}\r\n\r\n';
  const bytes = Buffer.from(raw), split = bytes.indexOf(Buffer.from('完整')) + 1;
  const result = await runStream({ chunks: [bytes.subarray(0, split), bytes.subarray(split, bytes.length - 4), bytes.subarray(bytes.length - 4)],
    failure: new TypeError('network error') });
  assert.equal(result.context.messages.at(-1).status, 'complete');
  assert.equal(result.context.messages.at(-1).content, '完整内容');
  assert.equal(result.reads, 3);
});

test('EOF without done preserves partial text and resumes the existing background turn', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'delta', delta: 'partial reply' }])] });
  assert.equal(result.context.messages.at(-1).status, 'streaming');
  assert.match(result.context.messages.at(-1).content, /^partial reply/);
  assert.doesNotMatch(result.context.messages.at(-1).content, /连接中断/);
  assert.equal(result.saved.at(-1).status, 'streaming');
  assert.equal(result.context.busy, true);
  assert.equal(result.timers.length, 1);
});

test('transport failure preserves the original server error', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'error', error: 'synthetic upstream failure' }])],
    failure: new TypeError('network error') });
  assert.equal(result.context.messages.at(-1).status, 'error');
  assert.match(result.context.messages.at(-1).content, /synthetic upstream failure/);
  assert.doesNotMatch(result.context.messages.at(-1).content, /network error|Connection error|连接中断/);
});

test('HTTP errors preserve the server explanation instead of claiming a network failure', async () => {
  const result = await runStream({ httpFailure: { status: 409, detail: 'Synthetic request conflict' } });
  assert.equal(result.context.messages.at(-1).status, 'error');
  assert.match(result.context.messages.at(-1).content, /请求失败: Synthetic request conflict/);
  assert.doesNotMatch(result.context.messages.at(-1).content, /连接失败/);
});

test('fetch or read failure reconnects to the same turn without resubmitting a prompt', async () => {
  const fetchResult = await runStream({ fetchFailure: new TypeError('network error') });
  assert.equal(fetchResult.context.messages.at(-1).status, 'streaming');
  const readResult = await runStream({ failure: new TypeError('network error') });
  assert.equal(readResult.context.messages.at(-1).status, 'streaming');
  assert.equal(fetchResult.requests.filter(request => request.url === '/api/chat').length, 1);
  assert.equal(readResult.requests.filter(request => request.url === '/api/chat').length, 1);
});

test('manual stop retains the existing stopped status', async () => {
  const error = new Error('cancelled'); error.name = 'AbortError';
  const result = await runStream({ failure: error, beforeRead(context) { context.activeTurn.stopRequested = true; context.activeController.abort(); } });
  assert.equal(result.context.messages.at(-1).status, 'stopped');
  assert.match(result.context.messages.at(-1).content, /Stopped: Response cancelled/);
  assert.equal(result.saved.at(-1).status, 'stopped');
});

test('the stop button sends an explicit stop request instead of only aborting HTTP', async () => {
  const error = new Error('cancelled'); error.name = 'AbortError';
  const result = await runStream({ failure: error, async beforeRead(context) { await context.stop(); } });
  assert.equal(result.requests.filter(request => /\/api\/turns\/[a-f0-9]{32}\/stop$/.test(request.url)).length, 1);
  assert.equal(result.context.messages.at(-1).status, 'stopped');
});

test('a reconnect consumes the saved server snapshot without submitting the prompt again', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'delta', delta: 'partial' }])] });
  let requests = 0;
  const turnId = result.context.activeTurn.id;
  result.context.apiFetch = async url => {
    if (url.startsWith('/api/conversations/')) return {ok:true};
    assert.equal(url, '/api/turns/' + turnId + '/events'); requests++;
    const bytes = Buffer.from(sse([
      {event:'snapshot',conversation_id:'synthetic-cid',message:{role:'assistant',content:'completed on server',rawText:'completed on server',status:'complete',turnId}},
      {event:'done',status:'complete'},
    ]));
    return {ok:true,body:{getReader:()=>({read:async()=>({value:bytes,done:false}),cancel:async()=>{},releaseLock(){}})}};
  };
  await result.timers[0]();
  assert.equal(requests, 1);
  assert.equal(result.context.messages.at(-1).content, 'completed on server');
  assert.equal(result.context.messages.at(-1).status, 'complete');
  assert.equal(result.context.busy, false);
  assert.equal(result.requests.filter(request => request.url === '/api/chat').length, 1);
});

test('switching chats leaves the older background turn alive without unlocking the newer view', async () => {
  const error = new Error('cancelled'); error.name = 'AbortError';
  const nextMessages = [{ role: 'user', content: 'new conversation' }];
  const nextController = new AbortController();
  const result = await runStream({ failure: error, beforeRead(context) {
    context.messages = nextMessages; context.currentConversationId = 'other-cid';
    context.activeController = nextController; context.busy = true;
  } });
  assert.equal(result.context.messages, nextMessages);
  assert.equal(result.context.currentConversationId, 'other-cid');
  assert.equal(result.context.activeController, nextController);
  assert.equal(result.context.busy, true);
  assert.equal(result.saved.at(-1).status, 'streaming');
});

test('a fast completed initial snapshot initializes the new conversation ID and saves its result', async () => {
  const result = await runStream({ initialCid: null, chunks: [sse([
    { event: 'start', conversation_id: 'fast-cid' },
    { event: 'snapshot', conversation_id: 'fast-cid', message: {
      role: 'assistant', content: 'fast result', rawText: 'fast result', status: 'complete',
      routeInfo: { returned_models: ['actual-exp-z'] },
    } },
    { event: 'done', status: 'complete' },
  ])] });
  assert.equal(result.context.currentConversationId, 'fast-cid');
  assert.equal(result.context.messages.at(-1).content, 'fast result');
  assert.equal(JSON.parse(result.storage.get('agy_msgs_fast-cid')).at(-1).content, 'fast result');
});

test('the running placeholder survives normalization and preserves its task identity', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'done' }])] });
  const normalized = vm.runInContext(`normalizeMessageList([{role:'assistant',content:'',turnId:'abc',status:'streaming'}],true)`, result.context);
  assert.equal(normalized[0].status, 'streaming');
  assert.equal(normalized[0].turnId, 'abc');
});

test('saved statuses recover without changing legacy messages', async () => {
  const result = await runStream({ chunks: [sse([{ event: 'done' }])] });
  const normalized = vm.runInContext(`normalizeMessageList([
    {role:'assistant',content:'completed',status:'complete'},
    {role:'assistant',content:'failed',status:'error'},
    {role:'assistant',content:'cancelled',status:'stopped'},
    {role:'assistant',content:'unfinished',status:'streaming'},
    {role:'assistant',content:'legacy'}])`, result.context);
  assert.deepEqual(Array.from(normalized, message => message.status), ['complete', 'error', 'stopped', 'error', undefined]);
});
