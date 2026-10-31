import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const { createLumiController, localGermanVoices, mount } = require('../dashboard/lumi-voice.js');
const csrf = 'a'.repeat(64);
const turn = 'b'.repeat(32);
const ok = value => ({ ok: true, json: async () => value });
const tick = () => new Promise(resolve => setImmediate(resolve));

function fixture({ recognitionFactory = null, synthesis = null, utteranceFactory = null, responses = [] } = {}) {
  const calls = [];
  const timers = [];
  const records = [];
  const hints = [];
  const fetch = async (path, options) => {
    calls.push({ path, options });
    if (path === '/api/lumi/status') return ok({ enabled: true });
    if (path === '/api/lumi/session') return ok({ csrf, state: 'ready', controls: false, memory: 'session' });
    if (path === '/api/lumi/end') return ok({ ended: true, pending: false });
    if (path === '/api/lumi/turn') return ok({ turn_id: turn, state: 'pending' });
    if (path === '/api/lumi/result') return responses.length ? responses.shift() : ok({ state: 'pending' });
    throw Error(path);
  };
  const controller = createLumiController({ fetch,
    recognitionFactory, synthesis, utteranceFactory,
    randomHex: () => 'c'.repeat(32),
    setTimer: fn => { timers.push(fn); return timers.length; },
    clearTimer: () => {},
    view: { update: state => records.push(state), status: (s, h) => hints.push([s, h]), hint: h => hints.push(['hint', h]), interim: () => {} }
  });
  return { controller, calls, timers, records, hints };
}

test('queued requests are posted once, after confirmed completion, with CSRF', async () => {
  const f = fixture({ responses: [ok({ state: 'pending' }), ok({ state: 'done', answer: 'Ja' }), ok({ state: 'done', answer: 'Weiter' })] });
  await f.controller.checkEnabled();
  await f.controller.start();
  assert.equal(f.controller.submit('Erster Auftrag'), true);
  assert.equal(f.controller.submit('Zweiter Auftrag'), true);
  await tick();
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/turn').length, 1);
  f.timers.shift()();
  await tick();
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/turn').length, 2);
  const posts = f.calls.filter(c => c.path === '/api/lumi/turn');
  assert.deepEqual(posts.map(c => JSON.parse(c.options.body).text), ['Erster Auftrag', 'Zweiter Auftrag']);
  assert.ok(posts.every(c => c.options.headers['X-Lumi-CSRF'] === csrf && c.options.headers['X-Lumi-Client'] === '1' && c.options.credentials === 'same-origin' && c.options.redirect === 'error'));
  assert.equal(f.controller.snapshot().transcript.length, 4);
});

test('ending during an outstanding result suppresses late answers and queued posts', async () => {
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const f = fixture({ responses: [pending] });
  await f.controller.checkEnabled(); await f.controller.start();
  f.controller.submit('eins'); f.controller.submit('zwei');
  await tick();
  await f.controller.end();
  release(ok({ state: 'done', answer: 'Späte Antwort' }));
  await tick();
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/turn').length, 1);
  assert.deepEqual(f.controller.snapshot().transcript.map(t => t.text), ['eins', 'zwei']);
  assert.equal(f.controller.snapshot().queued, 0);
  assert.equal(f.controller.snapshot().current, false);
  assert.ok(f.hints.some(([_, h]) => h && h.includes('Bereits übermittelte Aufträge')));
});

test('a deliberate new start does not present the old transcript as current context', async () => {
  const f = fixture({ responses: [ok({ state: 'done', answer: 'Alte Antwort' })] });
  await f.controller.checkEnabled(); await f.controller.start();
  f.controller.submit('Alte Frage'); await tick();
  assert.equal(f.controller.snapshot().transcript.length, 2);
  await f.controller.end(); await f.controller.start();
  assert.equal(f.controller.snapshot().active, true);
  assert.deepEqual(f.controller.snapshot().transcript, []);
});

test('ambiguous POST is never resent and clears queued work', async () => {
  const calls = [];
  const hints = [];
  const controller = createLumiController({
    fetch: async (path, options) => {
      calls.push(path);
      if (path === '/api/lumi/status') return ok({ enabled: true });
      if (path === '/api/lumi/session') return ok({ csrf, state: 'ready' });
      if (path === '/api/lumi/turn') throw Error('connection lost after send');
      return ok({});
    },
    view: { update() {}, status: (_, h) => hints.push(h), hint() {}, interim() {} },
    randomHex: () => 'c'.repeat(32)
  });
  await controller.checkEnabled(); await controller.start();
  controller.submit('eins'); controller.submit('zwei');
  await tick();
  assert.equal(calls.filter(p => p === '/api/lumi/turn').length, 1);
  assert.equal(controller.snapshot().queued, 0);
  assert.equal(controller.snapshot().current, false);
  assert.equal(controller.snapshot().paused, true);
  await controller.start();
  assert.equal(calls.filter(p => p === '/api/lumi/session').length, 1);
  await controller.end();
  assert.ok(hints.some(h => h && h.includes('Bereits übermittelte Aufträge')));
});

test('start waits for end response; timeout unlocks it without reviving old callbacks', async () => {
  let releaseEnd;
  const endResponse = new Promise(resolve => { releaseEnd = resolve; });
  const paths = [];
  const requestTimers = [];
  const controller = createLumiController({
    fetch: async path => {
      paths.push(path);
      if (path === '/api/lumi/status') return ok({ enabled: true });
      if (path === '/api/lumi/session') return ok({ csrf, state: 'ready' });
      if (path === '/api/lumi/end') return endResponse;
      return ok({});
    },
    setRequestTimer: fn => { requestTimers.push(fn); return requestTimers.length; },
    clearRequestTimer() {}, randomHex: () => 'c'.repeat(32),
    view: { update() {}, status() {}, hint() {}, interim() {} }
  });
  await controller.checkEnabled(); await controller.start();
  const closing = controller.end();
  assert.equal(controller.snapshot().ending, true);
  await controller.start();
  assert.equal(paths.filter(p => p === '/api/lumi/session').length, 1);
  requestTimers.at(-1)();
  await closing;
  assert.equal(controller.snapshot().ending, false);
  await controller.start();
  assert.equal(paths.filter(p => p === '/api/lumi/session').length, 2);
  releaseEnd(ok({ ended: true, pending: false }));
  await tick();
  assert.equal(controller.snapshot().active, true);
});

test('end during opening waits for the returned session before closing it', async () => {
  let releaseSession;
  const sessionResponse = new Promise(resolve => { releaseSession = resolve; });
  const paths = [];
  const controller = createLumiController({
    fetch: async (path, options) => {
      paths.push({ path, options });
      if (path === '/api/lumi/status') return ok({ enabled: true });
      if (path === '/api/lumi/session') return sessionResponse;
      if (path === '/api/lumi/end') return ok({ ended: true, pending: false });
      return ok({});
    },
    view: { update() {}, status() {}, hint() {}, interim() {} }
  });
  await controller.checkEnabled();
  const opening = controller.start();
  const closing = controller.end();
  await controller.start();
  assert.equal(paths.filter(c => c.path === '/api/lumi/session').length, 1);
  releaseSession(ok({ csrf, state: 'ready' }));
  await Promise.all([opening, closing]);
  assert.equal(paths.filter(c => c.path === '/api/lumi/end').length, 1);
  assert.equal(paths.find(c => c.path === '/api/lumi/end').options.headers['X-Lumi-CSRF'], csrf);
  assert.equal(controller.snapshot().active, false);
});

test('hiding during session opening cannot activate the microphone later', async () => {
  let releaseSession;
  const response = new Promise(resolve => { releaseSession = resolve; });
  const paths = [];
  let microphoneConstructed = 0;
  const c = createLumiController({
    fetch: async path => {
      paths.push(path);
      if (path.endsWith('status')) return ok({ enabled: true });
      if (path.endsWith('session')) return response;
      return ok({ ended: true, pending: false });
    },
    recognitionFactory: () => { microphoneConstructed++; throw Error('must not capture'); },
    view: { update() {}, status() {}, hint() {}, interim() {} }
  });
  await c.checkEnabled();
  const opening = c.start();
  c.hide();
  releaseSession(ok({ csrf, state: 'ready' }));
  await opening; await tick();
  assert.equal(microphoneConstructed, 0);
  assert.equal(c.snapshot().active, false);
  assert.equal(paths.filter(p => p.endsWith('/end')).length, 1);
});

test('explicit resume drains queued messages after a confirmed hidden result', async () => {
  const f = fixture({ responses: [ok({ state: 'done', answer: 'eins' }), ok({ state: 'done', answer: 'zwei' })] });
  await f.controller.checkEnabled(); await f.controller.start();
  f.controller.submit('eins'); f.controller.submit('zwei');
  f.controller.hide();
  await tick();
  assert.equal(f.controller.snapshot().queued, 1);
  assert.equal(f.calls.filter(c => c.path.endsWith('/turn')).length, 1);
  await f.controller.start(); await tick();
  assert.equal(f.controller.snapshot().queued, 0);
  assert.equal(f.calls.filter(c => c.path.endsWith('/turn')).length, 2);
});

test('failed result discards queued work and requires a new session', async () => {
  const f = fixture({ responses: [ok({ state: 'failed', error: 'assistant_unavailable' })] });
  await f.controller.checkEnabled(); await f.controller.start();
  f.controller.submit('eins'); f.controller.submit('zwei');
  await tick();
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/turn').length, 1);
  assert.equal(f.controller.snapshot().queued, 0);
  assert.equal(f.controller.snapshot().fatalPaused, true);
  await f.controller.start();
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/session').length, 1);
  assert.ok(f.hints.some(([_, h]) => h && h.includes('Wartende Nachrichten wurden verworfen')));
});

test('recognizer restart budget pauses only microphone and supports explicit resume', async () => {
  let recognizer;
  class Recognition {
    static available = async () => 'available';
    constructor() { recognizer = this; this.processLocally = false; this.started = 0; }
    start() { this.started++; }
    abort() {}
  }
  const f = fixture({ recognitionFactory: () => new Recognition() });
  await f.controller.checkEnabled(); await f.controller.start();
  for (let i = 0; i < 4; i++) recognizer.onend();
  assert.equal(f.controller.snapshot().micPaused, true);
  assert.equal(f.controller.snapshot().paused, false);
  assert.equal(f.controller.submit('Text bleibt möglich'), true);
  await f.controller.start();
  assert.equal(f.controller.snapshot().micPaused, false);
  assert.equal(recognizer.started, 2);
});

test('queue accepts only three additional messages while one is active', async () => {
  const f = fixture();
  await f.controller.checkEnabled(); await f.controller.start();
  for (let i = 0; i < 4; i++) assert.equal(f.controller.submit(String(i)), true);
  assert.equal(f.controller.submit('overflow'), false);
  assert.equal(f.controller.snapshot().queued, 3);
  assert.equal(f.controller.snapshot().transcript.length, 4);
});

test('unsupported local recognition does not start or install and text stays available', async () => {
  let started = 0;
  class Recognition { start() { started++; } abort() {} }
  const f = fixture({ recognitionFactory: () => new Recognition() });
  await f.controller.checkEnabled(); await f.controller.start();
  assert.equal(started, 0);
  assert.equal(f.controller.snapshot().active, true);
  assert.equal(f.controller.submit('Text funktioniert'), true);
  assert.ok(f.hints.some(([_, h]) => h && h.includes('Lokale Spracherkennung')));
});

test('only local German voices are selected', () => {
  const voices = [{ lang: 'de-DE', localService: false }, { lang: 'en-US', localService: true }, { lang: 'de-DE', localService: true }];
  assert.deepEqual(localGermanVoices({ getVoices: () => voices }), [voices[2]]);
});

test('TTS aborts listening; interruption resumes and does not cancel server job', async () => {
  let recognizer;
  class Recognition {
    static available = async () => 'available';
    constructor() { recognizer = this; this.processLocally = false; this.started = 0; this.aborted = 0; }
    start() { this.started++; }
    abort() { this.aborted++; }
  }
  const synth = { spoken: [], cancelled: 0, getVoices: () => [{ lang: 'de-DE', localService: true }], speak(u) { this.spoken.push(u); }, cancel() { this.cancelled++; } };
  const f = fixture({ recognitionFactory: () => new Recognition(), synthesis: synth, utteranceFactory: text => ({ text }), responses: [ok({ state: 'done', answer: 'Hallo' })] });
  await f.controller.checkEnabled(); await f.controller.start();
  assert.equal(recognizer.processLocally, true);
  assert.equal(recognizer.lang, 'de-DE');
  f.controller.submit('Frage'); await tick();
  assert.equal(synth.spoken.length, 1);
  assert.equal(recognizer.aborted, 1);
  recognizer.onresult({ resultIndex: 0, results: [{ isFinal: true, 0: { transcript: 'Lumi beantwortet sich selbst' } }] });
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/turn').length, 1);
  f.controller.interrupt();
  assert.equal(synth.cancelled, 1);
  assert.equal(recognizer.started, 2);
  assert.equal(f.calls.filter(c => c.path === '/api/lumi/end').length, 0);
});

test('headphones allow local barge-in and visibility requires explicit resume', async () => {
  let recognizer;
  class Recognition {
    static available = async () => 'available';
    constructor() { recognizer = this; this.processLocally = false; this.started = 0; this.aborted = 0; }
    start() { this.started++; }
    abort() { this.aborted++; }
  }
  const synth = { spoken: [], cancelled: 0, getVoices: () => [{ lang: 'de-DE', localService: true }], speak(u) { this.spoken.push(u); }, cancel() { this.cancelled++; } };
  const f = fixture({ recognitionFactory: () => new Recognition(), synthesis: synth, utteranceFactory: text => ({ text }), responses: [ok({ state: 'done', answer: 'Antwort' })] });
  await f.controller.checkEnabled(); await f.controller.start();
  f.controller.setHeadphones(true);
  f.controller.submit('Frage'); await tick();
  assert.equal(recognizer.aborted, 0);
  recognizer.onresult({ resultIndex: 0, results: [{ isFinal: false, 0: { transcript: 'unter' } }] });
  assert.equal(synth.cancelled, 1);
  f.controller.hide();
  assert.equal(f.controller.snapshot().paused, true);
  assert.equal(f.controller.snapshot().micRunning, false);
  await f.controller.start();
  assert.equal(f.controller.snapshot().paused, false);
  assert.equal(recognizer.started, 2);
});

test('rendering uses textContent for untrusted answer and user text', () => {
  const elements = new Map();
  function element() {
    return { textContent: '', children: [], disabled: false, checked: false, value: '',
      append(...children) { this.children.push(...children); },
      replaceChildren() { this.children = []; }, addEventListener() {} };
  }
  for (const id of ['lumi-transcript','lumi-status','lumi-hint','lumi-queue','lumi-interim','lumi-start','lumi-end','lumi-interrupt','lumi-input','lumi-send','lumi-headphones','lumi-form']) elements.set(id, element());
  let created = [];
  const doc = { hidden: false, getElementById: id => elements.get(id) || (id === 'lumi-app' ? element() : null), createElement: () => { const e = element(); created.push(e); return e; }, addEventListener() {} };
  const env = { fetch: async path => path.endsWith('status') ? ok({ enabled: true }) : path.endsWith('session') ? ok({ csrf, state: 'ready' }) : path.endsWith('turn') ? ok({ turn_id: turn, state: 'pending' }) : ok({ state: 'done', answer: '<img src=x onerror=alert(1)>' }), addEventListener() {}, crypto: globalThis.crypto };
  const c = mount(doc, env);
  return tick().then(async () => {
    await c.start(); c.submit('<script>alert(1)</script>'); await tick();
    assert.ok(created.some(e => e.textContent === '<img src=x onerror=alert(1)>'));
    assert.ok(created.some(e => e.textContent === '<script>alert(1)</script>'));
    assert.ok(created.every(e => !('innerHTML' in e)));
  });
});
