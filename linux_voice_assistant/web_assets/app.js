import { AudioMonitor } from './monitor.js';

const names = ['mic_volume', 'mic_auto_gain', 'mic_noise_suppression', 'primary_model', 'primary_threshold', 'muted'];
const controls = Object.fromEntries(names.map(name => [name, document.getElementById(name)]));
const login = document.getElementById('login');
const panel = document.getElementById('controls');
const message = document.getElementById('message');
let revision = -1;
let socket;
let pending = false;
let deferredState;
let retryTimer;
let connectionGeneration = 0;
const monitor = new AudioMonitor(command => {
  if (socket?.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(command));
  return true;
});

function showState(state) {
  if (pending) {
    if (!deferredState || state.revision > deferredState.revision) deferredState = state;
    return;
  }
  if (state.revision < revision) return;
  revision = state.revision;
  const models = controls.primary_model;
  if (models.options.length !== state.models.length || state.models.some((model, index) => models.options[index]?.value !== model.id)) {
    models.replaceChildren(...state.models.map(model => new Option(model.name, model.id)));
  }
  for (const name of names) {
    if (name === 'muted') controls[name].checked = state[name];
    else controls[name].value = state[name] ?? '';
    const output = document.getElementById(`${name}-value`);
    if (output) output.value = name === 'primary_threshold' ? Number(state[name]).toFixed(3) : String(state[name]);
  }
  login.hidden = true;
  panel.hidden = false;
  monitor.state(state);
}

async function request(path, options = {}) {
  const generation = connectionGeneration;
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
  if (response.status === 401 && generation === connectionGeneration) { login.hidden = false; panel.hidden = true; monitor.disconnected(); disconnect(); }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function disconnect() {
  connectionGeneration++;
  clearTimeout(retryTimer);
  revision = -1;
  deferredState = undefined;
  const previous = socket;
  socket = undefined;
  document.getElementById('connection').classList.remove('online');
  document.getElementById('connection-text').textContent = 'Disconnected';
  previous?.close();
}

function connect() {
  disconnect();
  if (document.hidden || panel.hidden) return;
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`);
  socket.binaryType = 'arraybuffer';
  const current = socket;
  socket.onopen = () => { if (socket !== current) return; document.getElementById('connection').classList.add('online'); document.getElementById('connection-text').textContent = 'Connected'; };
  socket.onmessage = event => {
    if (socket !== current) return;
    if (event.data instanceof ArrayBuffer) { monitor.binary(event.data); return; }
    const data = JSON.parse(event.data);
    if (data.type) monitor.message(data);
    else showState(data);
  };
  socket.onclose = () => {
    if (socket !== current) return;
    monitor.disconnected();
    revision = -1;
    deferredState = undefined;
    document.getElementById('connection').classList.remove('online');
    document.getElementById('connection-text').textContent = 'Disconnected';
    const generation = connectionGeneration;
    if (!document.hidden && !panel.hidden) retryTimer = setTimeout(async () => {
      try {
        const state = await request('/api/state');
        if (generation !== connectionGeneration || document.hidden || panel.hidden) return;
        showState(state);
        connect();
      } catch { /* Sign-in view is shown by request. */ }
    }, 2000);
  };
}

async function set(name, value) {
  if (pending) return;
  const generation = connectionGeneration;
  pending = true;
  for (const control of Object.values(controls)) control.disabled = true;
  message.textContent = 'Saving…';
  try {
    const state = await request('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, value }) });
    pending = false;
    for (const control of Object.values(controls)) control.disabled = false;
    if (generation !== connectionGeneration) return;
    showState(state);
    if (deferredState) { showState(deferredState); deferredState = undefined; }
    message.textContent = 'Saved';
  } catch (error) {
    pending = false;
    for (const control of Object.values(controls)) control.disabled = false;
    if (generation !== connectionGeneration) return;
    message.textContent = `Could not save: ${error.message}`;
    try {
      const state = await request('/api/state');
      if (generation === connectionGeneration) showState(state);
    } catch { /* Authentication may have expired. */ }
    deferredState = undefined;
  }
}

for (const name of names) {
  const input = controls[name];
  if (input.type === 'range') input.addEventListener('input', () => { document.getElementById(`${name}-value`).value = name === 'primary_threshold' ? Number(input.value).toFixed(3) : input.value; });
  input.addEventListener('change', () => set(name === 'primary_threshold' ? 'wake_word_1_threshold' : name, input.type === 'checkbox' ? input.checked : input.type === 'range' ? Number(input.value) : input.value));
}

document.getElementById('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const password = document.getElementById('password');
  try {
    showState(await request('/api/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: password.value }) }));
    password.value = '';
    document.getElementById('login-error').textContent = '';
    connect();
  } catch (error) { document.getElementById('login-error').textContent = error.message; }
});
document.getElementById('logout').addEventListener('click', async () => {
  monitor.stop();
  disconnect();
  panel.hidden = true;
  login.hidden = false;
  await request('/api/logout', { method: 'POST' }).catch(() => {});
});
document.addEventListener('visibilitychange', () => { if (document.hidden) { monitor.stop(); disconnect(); } else if (!panel.hidden) connect(); });

request('/api/state').then(state => { showState(state); connect(); }).catch(() => { login.hidden = false; });
