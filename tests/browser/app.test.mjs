import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../linux_voice_assistant/web_assets/app.js', import.meta.url), 'utf8').replace(/^import .*\n/, '');
const state = { revision: 1, models: [], mic_volume: 100, mic_auto_gain: 0, mic_noise_suppression: 0, primary_model: '', primary_threshold: .7, muted: false };
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function fixture() {
  const elements = new Map(), sockets = [], requests = [], timers = new Map(), calls = [];
  let timerId = 0;
  const element = id => {
    if (!elements.has(id)) elements.set(id, { hidden: id === 'controls', options: [], value: '', textContent: '', classList: { add() {}, remove() {} }, listeners: {},
      addEventListener(type, fn) { this.listeners[type] = fn; }, replaceChildren(...options) { this.options = options; } });
    return elements.get(id);
  };
  const document = { hidden: false, getElementById: element, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } };
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor() { sockets.push(this); }
    close() {} // Deliver stale callbacks explicitly, including synchronous logout closes below.
    send() {}
  }
  const context = { document, location: { protocol: 'http:', host: 'localhost' }, WebSocket: Socket, Option: class {},
    AudioMonitor: class { state() { calls.push('state'); } binary() { calls.push('binary'); } message() { calls.push('message'); } disconnected() { calls.push('disconnected'); } stop() {} },
    fetch(path) { return new Promise(resolve => requests.push({ path, resolve: body => resolve({ status: 200, ok: true, json: async () => body }) })); },
    setTimeout(fn) { timers.set(++timerId, fn); return timerId; }, clearTimeout(id) { timers.delete(id); }, ArrayBuffer };
  vm.runInNewContext(source, context);
  return { document, element, sockets, requests, timers, calls };
}
async function ready() {
  const f = fixture();
  f.requests.shift().resolve(state);
  await flush();
  assert.equal(f.sockets.length, 1);
  return f;
}

const stale = await ready();
const old = stale.sockets[0];
stale.document.hidden = true;
stale.document.listeners.visibilitychange();
stale.document.hidden = false;
stale.document.listeners.visibilitychange();
const count = stale.calls.length;
old.onopen();
old.onmessage({ data: JSON.stringify({ ...state, revision: 99, mic_volume: 999 }) });
old.onmessage({ data: new ArrayBuffer(0) });
old.onmessage({ data: JSON.stringify({ type: 'reset' }) });
old.onclose();
assert.equal(stale.element('connection-text').textContent, '');
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
