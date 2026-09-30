import assert from 'node:assert/strict';
import { AudioMonitor, decodeFrame } from '../../linux_voice_assistant/web_assets/monitor.js';

const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, { value: id === 'listen-feed' ? 'input' : '0', textContent: '', disabled: false, addEventListener() {}, clientWidth: 600, clientHeight: 220, getContext: () => id === 'level-graph' ? levelCanvas : canvas });
  return elements.get(id);
}
const canvas = { setTransform() {}, clearRect() {}, fillRect() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, setLineDash() {}, arc() {}, fill() {}, fillText() {} };
let recordLevels = false;
const levelPaths = [];
const levelCanvas = {
  ...canvas,
  setLineDash(dash) { this.dash = [...dash]; },
  beginPath() { this.points = []; },
  moveTo(x, y) { this.points.push({ move: true, x, y }); },
  lineTo(x, y) { this.points.push({ move: false, x, y }); },
  stroke() {
    if (recordLevels && ['#75cbb5', '#d7a259'].includes(this.strokeStyle)) levelPaths.push({ color: this.strokeStyle, dash: this.dash, points: this.points });
  },
};
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
for (const feed of ['input', 'processed']) {
  monitor.meter({ feed, data: new Float32Array([.0001, -.0001]) });
  assert.ok(Math.abs(element(`${feed}-meter`).value + 80) < .001);
  assert.equal(element(`${feed}-level`).textContent, '-80.0 dBFS');
  monitor.resetPlayback(false);
  assert.equal(element(`${feed}-level`).textContent, '-80.0 dBFS');
  monitor.meter({ feed, data: new Float32Array([0]) });
  assert.equal(element(`${feed}-meter`).value, -90);
  assert.equal(element(`${feed}-level`).textContent, '−∞ dBFS');
}
monitor.meter({ feed: 'input', data: new Float32Array([2, -2]) });
assert.equal(element('input-meter').value, 0);
assert.equal(element('input-level').textContent, '6.0 dBFS');
assert.equal(element('input-clips').textContent, '2 clipped');
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
for (const boundary of ['stop', 'disconnected', 'reset', 'gap', 'mute']) {
  monitor.muted = false;
  for (const feed of ['input', 'processed']) monitor.meter({ feed, data: new Float32Array([.25]) });
  if (boundary === 'reset' || boundary === 'gap') monitor.message({ type: boundary, epoch: monitor.epoch, to_sample: 100 });
  else if (boundary === 'mute') monitor.state({ primary_model: 'second', primary_threshold: .6, revision: 2, muted: true });
  else monitor[boundary]();
  for (const feed of ['input', 'processed']) {
    assert.equal(element(`${feed}-meter`).value, -90, `${boundary} clears ${feed}`);
    assert.equal(element(`${feed}-level`).textContent, '—');
    assert.equal(element(`${feed}-clips`).textContent, '—');
  }
}
assert.deepEqual(commands, ['monitor_start', 'monitor_stop']);
assert.equal(monitor.sources.length, 0);
monitor.muted = false;

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
for (const feed of ['input', 'processed']) {
  assert.ok(monitor.levelHistory[feed].length <= 469);
  assert.ok(monitor.levelHistory[feed][0].end > monitor.levelNewest - 30 * 16000);
}
monitor.state({ primary_model: 'first', primary_threshold: .7, revision: 1, muted: false });
monitor.state({ primary_model: 'second', primary_threshold: .6, revision: 2, muted: false });
assert.equal(monitor.scores.length, 0);
monitor.stop();

const historyMonitor = new AudioMonitor(() => true);
historyMonitor.message({ type: 'monitor', active: true, epoch: 1, sample_rate: 16000 });
historyMonitor.binary(frame(1, 0));
historyMonitor.binary(frame(1, 1024));
for (const start of [0, 480, 960]) historyMonitor.binary(frame(2, start, 480));
assert.deepEqual(historyMonitor.levelHistory.input.map(point => [point.start, point.end]), [[0, 1024], [1024, 2048]]);
assert.deepEqual(historyMonitor.levelHistory.processed.map(point => [point.start, point.end]), [[0, 480], [480, 960], [960, 1440]]);
assert.ok(Math.abs(historyMonitor.levelHistory.input[0].level - 20 * Math.log10(.25)) < .001);
historyMonitor.binary(frame(1, 2048, 0));
const stale = frame(1, 2048);
new DataView(stale).setUint32(6, 2);
historyMonitor.binary(stale);
assert.equal(historyMonitor.levelHistory.input.length, 2);
recordLevels = true;
historyMonitor.drawLevels(2048);
let [processedPath, inputPath] = levelPaths.splice(0);
assert.equal(processedPath.color, '#d7a259');
assert.equal(inputPath.color, '#75cbb5');
assert.deepEqual(inputPath.dash, [2, 4]);
assert.deepEqual(processedPath.dash, []);
assert.equal(inputPath.points[0].x, processedPath.points[0].x);
assert.ok(Math.abs(processedPath.points[1].x - processedPath.points[0].x - 480 / (30 * 16000) * 564) < 1e-9);
assert.equal(inputPath.points.at(-1).x, 600);
assert.equal(inputPath.points.filter(point => point.move).length, 1);
historyMonitor.message({ type: 'gap', epoch: 1, to_sample: 2048 });
historyMonitor.binary(frame(1, 2048));
historyMonitor.binary(frame(2, 1440, 480, 1));
historyMonitor.drawLevels(3072);
[processedPath, inputPath] = levelPaths.splice(0);
assert.equal(inputPath.points.filter(point => point.move).length, 2, 'An explicit gap breaks a contiguous input path');
assert.equal(processedPath.points.filter(point => point.move).length, 2, 'A frame gap breaks a contiguous processed path');
const drawLevels = historyMonitor.drawLevels.bind(historyMonitor);
let drawnRight;
historyMonitor.drawLevels = right => { drawnRight = right; drawLevels(right); };
historyMonitor.playCursor = () => 1600;
historyMonitor.draw();
assert.equal(drawnRight, 1600, 'Both charts use the playback sample cursor');
recordLevels = false;

for (const boundary of ['stop', 'disconnected', 'reset', 'epoch', 'mute', 'model']) {
  historyMonitor.stop(false);
  historyMonitor.state({ primary_model: 'first', primary_threshold: .7, revision: 1, muted: false });
  historyMonitor.message({ type: 'monitor', active: true, epoch: 1, sample_rate: 16000 });
  historyMonitor.binary(frame(1, 0));
  historyMonitor.binary(frame(2, 0));
  if (boundary === 'reset') historyMonitor.message({ type: 'reset', epoch: 2 });
  else if (boundary === 'epoch') historyMonitor.message({ type: 'monitor', active: true, epoch: 2, sample_rate: 16000 });
  else if (boundary === 'mute' || boundary === 'model') historyMonitor.state({ primary_model: boundary === 'model' ? 'second' : 'first', primary_threshold: .7, revision: 2, muted: boundary === 'mute' });
  else historyMonitor[boundary]();
  assert.deepEqual(historyMonitor.levelHistory, { input: [], processed: [] }, `${boundary} clears both level histories`);
}
historyMonitor.stop(false);
