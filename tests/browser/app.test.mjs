import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../linux_voice_assistant/web_assets/app.js', import.meta.url), 'utf8').replace(/^import .*\n/, '');
const state = { revision: 1, models: [], mic_volume: 100, mic_auto_gain: 0, mic_noise_suppression: 0, primary_model: '', primary_threshold: .7, muted: false };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function fixture(options = {}) {
  const elements = new Map(), sockets = [], requests = [], timers = new Map(), calls = [];
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) {
      const classes = new Set();
      elements.set(id, { hidden: id === 'controls', options: [], value: '', textContent: '',
        classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value) }, listeners: {},
        addEventListener(type, fn) { this.listeners[type] = fn; }, replaceChildren(...options) { this.options = options; } });
    }
    return elements.get(id);
  };
  const document = { hidden: false, documentElement: { dataset: {} }, getElementById: element, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; }, dispatchEvent(event) { this.listeners[event.type]?.(); } };
  const media = { matches: options.dark ?? false, addEventListener(_type, fn) { this.change = fn; } };
  const storage = new Map(options.saved ? [['lva-appearance', options.saved]] : []);
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor() { sockets.push(this); }
    close() {} // Deliver stale callbacks explicitly, including synchronous logout closes below.
    send() {}
  }
  const context = { document, location: { protocol: 'http:', host: 'localhost' }, WebSocket: Socket, Option: class {},
    matchMedia: () => media, Event: class { constructor(type) { this.type = type; } },
    localStorage: { getItem(key) { if (options.blocked) throw new Error('Storage denied'); return storage.get(key) ?? null; }, setItem(key, value) { if (options.blocked) throw new Error('Storage denied'); storage.set(key, value); } },
    AudioMonitor: class { state() { calls.push('state'); } binary() { calls.push('binary'); } message() { calls.push('message'); } disconnected() { calls.push('disconnected'); } stop() {} },
    fetch(path) { return new Promise(resolve => requests.push({ path, resolve: body => resolve({ status: 200, ok: true, json: async () => body }) })); },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); }, ArrayBuffer };
  vm.runInNewContext(source, context);
  return { document, element, sockets, requests, timers, calls, media, storage };
}
async function ready() {
  const f = fixture();
  f.requests.shift().resolve(state);
  await flush();
  assert.equal(f.sockets.length, 1);
  f.sockets[0].onopen();
  assert.equal(f.element('connection-text').textContent, 'Connected');
  assert.equal(f.element('connection').classList.contains('online'), true);
  return f;
}

const stale = await ready();
const old = stale.sockets[0];
stale.document.hidden = true;
stale.document.listeners.visibilitychange();
assert.equal(stale.element('connection-text').textContent, 'Disconnected');
assert.equal(stale.element('connection').classList.contains('online'), false);
stale.document.hidden = false;
stale.document.listeners.visibilitychange();
const count = stale.calls.length;
old.onopen();
old.onmessage({ data: JSON.stringify({ ...state, revision: 99, mic_volume: 999 }) });
old.onmessage({ data: new ArrayBuffer(0) });
old.onmessage({ data: JSON.stringify({ type: 'reset' }) });
old.onclose();
assert.equal(stale.element('connection-text').textContent, 'Disconnected');
assert.equal(stale.element('mic_volume').value, 100);
assert.equal(stale.calls.length, count);
assert.equal(stale.timers.size, 0);

for (const action of ['hide', 'logout']) {
  const f = await ready();
  f.sockets[0].onclose();
  const retry = [...f.timers.values()][0];
  const pendingRetry = retry();
  const request = f.requests.shift();
  let logout;
  if (action === 'hide') { f.document.hidden = true; f.document.listeners.visibilitychange(); }
  else {
    f.sockets[0].close = () => f.sockets[0].onclose();
    logout = f.element('logout').listeners.click();
    assert.equal(f.timers.size, 0);
    f.requests.shift().resolve({});
    await logout;
  }
  request.resolve({ ...state, revision: 20 });
  await pendingRetry;
  assert.equal(f.sockets.length, 1, `${action} must prevent retry reconnect`);
  if (action === 'logout') assert.equal(f.element('controls').hidden, true);
}

const saving = await ready();
const save = saving.element('mic_volume').listeners.change();
const savedResponse = saving.requests.shift();
const logout = saving.element('logout').listeners.click();
assert.equal(saving.element('connection-text').textContent, 'Disconnected');
assert.equal(saving.element('connection').classList.contains('online'), false);
saving.requests.shift().resolve({});
await logout;
savedResponse.resolve({ ...state, revision: 2, mic_volume: 50 });
await save;
assert.equal(saving.element('controls').hidden, true, 'A completed save must not reopen a logged-out page');

const themed = fixture({ saved: '{"mode":"dark","color":"sage"}' });
assert.equal(themed.document.documentElement.dataset.theme, 'dark');
assert.equal(themed.document.documentElement.dataset.color, 'sage');
themed.element('theme-mode').value = 'light';
themed.element('theme-mode').listeners.change();
themed.media.matches = true;
themed.media.change();
assert.equal(themed.document.documentElement.dataset.theme, 'light', 'Explicit appearance ignores system changes');
themed.element('theme-mode').value = 'system';
themed.element('theme-mode').listeners.change();
assert.equal(themed.document.documentElement.dataset.theme, 'dark');
themed.media.matches = false;
themed.media.change();
assert.equal(themed.document.documentElement.dataset.theme, 'light');
themed.element('theme-color').value = 'amber';
themed.element('theme-color').listeners.change();
assert.deepEqual(JSON.parse(themed.storage.get('lva-appearance')), { mode: 'system', color: 'amber' });
assert.equal(themed.requests.length, 1, 'Appearance makes no extra server requests');
for (const saved of ['{broken', 'null', '{"mode":"unknown","color":"unknown"}']) {
  const invalid = fixture({ saved });
  assert.equal(invalid.element('theme-mode').value, 'system');
  assert.equal(invalid.element('theme-color').value, 'violet');
}
const blocked = fixture({ blocked: true, dark: true });
blocked.element('theme-mode').value = 'light';
blocked.element('theme-mode').listeners.change();
blocked.element('theme-color').value = 'sage';
blocked.element('theme-color').listeners.change();
assert.equal(blocked.document.documentElement.dataset.theme, 'light');
assert.equal(blocked.document.documentElement.dataset.color, 'sage');
