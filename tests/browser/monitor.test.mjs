import assert from 'node:assert/strict';
import { AudioMonitor, decodeFrame } from '../../linux_voice_assistant/web_assets/monitor.js';

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { value: id === 'listen-feed' ? 'input' : '0', textContent: '', disabled: false, addEventListener() {}, clientWidth: 600, clientHeight: 220, getContext: () => canvas });
  return elements.get(id);
}
const canvas = { setTransform() {}, clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, setLineDash() {}, arc() {}, fill() {}, fillText() {} };
globalThis.document = { getElementById: element, hidden: false };
globalThis.devicePixelRatio = 1;
globalThis.requestAnimationFrame = () => 1;
globalThis.cancelAnimationFrame = () => {};

const started = [];
globalThis.AudioContext = class {
  sampleRate = 48000;
  currentTime = 0;
  destination = {};
  async resume() {}
  createGain() { return { gain: { value: 0 }, connect() {} }; }
  createBuffer(_channels, count) { return { getChannelData: () => new Float32Array(count) }; }
  createBufferSource() {
    const source = { connect() {}, start(time) { started.push(time); }, stop() { this.stopped = true; } };
    return source;
  }
};

function frame(feed, start, count = 1024, flags = 0) {
  const size = feed === 1 ? 4 : 2;
  const bytes = new ArrayBuffer(26 + count * size);
  const view = new DataView(bytes);
  view.setUint32(0, 0x4c564131);
  view.setUint8(4, feed);
  view.setUint8(5, flags);
  view.setUint32(6, 1);
  view.setBigUint64(10, BigInt(start));
  view.setUint32(18, count);
  view.setUint32(22, 3);
  for (let i = 0; i < count; i++) {
    if (feed === 1) view.setFloat32(26 + i * 4, 0.25, true);
    else view.setInt16(26 + i * 2, 8192, true);
  }
  return bytes;
}

assert.equal(decodeFrame(frame(1, 8)).data[0], 0.25);
assert.equal(decodeFrame(frame(2, 8)).data[0], 0.25);
assert.throws(() => decodeFrame(new ArrayBuffer(3)), /Invalid audio frame/);
const commands = [];
const monitor = new AudioMonitor(command => { commands.push(command.command); return true; });
monitor.meter({ feed: 'input', data: new Float32Array([1, -1, .999, -.999]) });
assert.equal(element('input-clips').textContent, '2 clipped');
monitor.meter({ feed: 'processed', data: new Float32Array([32767 / 32768, -1, 32766 / 32768, -32767 / 32768]) });
assert.equal(element('processed-clips').textContent, '2 clipped');
await monitor.start();
assert.deepEqual(commands, ['monitor_start']);
assert.equal(monitor.gain.gain.value, 0);
monitor.message({ type: 'monitor', active: true, epoch: 1, stream_id: 'test', sample_rate: 16000 });
for (let i = 0; i < 8; i++) {
  monitor.binary(frame(1, i * 1024));
  monitor.binary(frame(2, i * 1024));
}
assert.equal(started.length, 8);
assert.ok(monitor.sources.at(-1).endTime - monitor.context.currentTime < 1);
monitor.context.currentTime = 0.13;
const cursor = monitor.playCursor();
element('listen-feed').value = 'processed';
monitor.switchFeed();
assert.ok(monitor.sources[0].startSample >= Math.floor(cursor));
monitor.binary(frame(2, 5120, 1024, 1));
assert.ok(monitor.gapCount > 0);
monitor.message({ type: 'reset', epoch: 2, reason: 'mute' });
assert.equal(monitor.streams.input.length, 0);
assert.equal(monitor.streams.processed.length, 0);
monitor.stop();
assert.deepEqual(commands, ['monitor_start', 'monitor_stop']);
assert.equal(monitor.sources.length, 0);

for (const end of ['stop', 'disconnected']) {
  let resume;
  const before = commands.length;
  monitor.context.resume = () => new Promise(resolve => { resume = resolve; });
  const starting = monitor.start();
  monitor[end]();
  resume();
  await starting;
  assert.equal(commands.slice(before).includes('monitor_start'), false, `${end} must cancel a pending start`);
  assert.equal(monitor.requested, false);
}

monitor.message({ type: 'monitor', active: true, epoch: 1, stream_id: 'test', sample_rate: 16000 });
for (let i = 0; i < 1000; i++) {
  monitor.binary(frame(1, i * 1024));
  monitor.binary(frame(2, i * 1024));
  monitor.message({ type: 'scores', epoch: 1, items: [{ at_sample: (i + 1) * 1024, probability: .2 }] });
}
assert.ok(monitor.streams.input.length <= 17 && monitor.streams.processed.length <= 17);
assert.ok(monitor.scores.at(-1).at_sample - monitor.scores[0].at_sample <= 30 * 16000);
monitor.state({ primary_model: 'first', primary_threshold: .7, revision: 1, muted: false });
monitor.state({ primary_model: 'second', primary_threshold: .6, revision: 2, muted: false });
assert.equal(monitor.scores.length, 0);
monitor.stop();
