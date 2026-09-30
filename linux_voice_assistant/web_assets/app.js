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
}

async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', cache: 'no-store', ...options });
  if (response.status === 401) { login.hidden = false; panel.hidden = true; socket?.close(); }
  if (!response.ok) throw new Error(await response.text());
  return response.json();
}

function connect() {
  clearTimeout(retryTimer);
  socket?.close();
  socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/api/ws`);
  const current = socket;
  socket.onopen = () => { document.getElementById('connection').classList.add('online'); document.getElementById('connection-text').textContent = 'Connected'; };
  socket.onmessage = event => showState(JSON.parse(event.data));
  socket.onclose = () => {
    if (socket !== current) return;
    revision = -1;
    deferredState = undefined;
    document.getElementById('connection').classList.remove('online');
    document.getElementById('connection-text').textContent = 'Disconnected';
    if (!document.hidden && !panel.hidden) retryTimer = setTimeout(async () => {
      try { showState(await request('/api/state')); connect(); } catch { /* Sign-in view is shown by request. */ }
    }, 2000);
  };
}

async function set(name, value) {
  if (pending) return;
  pending = true;
  for (const control of Object.values(controls)) control.disabled = true;
  message.textContent = 'Saving…';
  try {
    const state = await request('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name, value }) });
    pending = false;
    for (const control of Object.values(controls)) control.disabled = false;
    showState(state);
    if (deferredState) { showState(deferredState); deferredState = undefined; }
    message.textContent = 'Saved';
  } catch (error) {
    pending = false;
    for (const control of Object.values(controls)) control.disabled = false;
    message.textContent = `Could not save: ${error.message}`;
    try { showState(await request('/api/state')); } catch { /* Authentication may have expired. */ }
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
  clearTimeout(retryTimer);
  await request('/api/logout', { method: 'POST' }).catch(() => {});
  socket?.close();
  panel.hidden = true;
  login.hidden = false;
});
document.addEventListener('visibilitychange', () => { if (document.hidden) { clearTimeout(retryTimer); socket?.close(); } else if (!panel.hidden) connect(); });

request('/api/state').then(state => { showState(state); connect(); }).catch(() => { login.hidden = false; });
