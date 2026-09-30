const RATE = 16000;
const SECOND = RATE;
const HISTORY = RATE * 30;

export function decodeFrame(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 26 || view.getUint32(0) !== 0x4c564131) throw new Error('Invalid audio frame');
  const feed = view.getUint8(4) === 1 ? 'input' : view.getUint8(4) === 2 ? 'processed' : null;
  if (!feed) throw new Error('Invalid audio feed');
  const epoch = view.getUint32(6);
  const start = Number(view.getBigUint64(10));
  const count = view.getUint32(18);
  const revision = view.getUint32(22);
  const size = feed === 'input' ? 4 : 2;
  if (buffer.byteLength !== 26 + count * size || count > 4096) throw new Error('Invalid audio length');
  const data = new Float32Array(count);
  for (let i = 0; i < count; i++) data[i] = feed === 'input' ? view.getFloat32(26 + i * 4, true) : view.getInt16(26 + i * 2, true) / 32768;
  return { feed, epoch, start, end: start + count, revision, data, gap: Boolean(view.getUint8(5) & 1) };
}

export class AudioMonitor {
  constructor(send) {
    this.send = send;
    this.startButton = document.getElementById('monitor-start');
    this.stopButton = document.getElementById('monitor-stop');
    this.feedSelect = document.getElementById('listen-feed');
    this.volume = document.getElementById('listen-volume');
    this.canvas = document.getElementById('score-graph');
    this.status = document.getElementById('monitor-status');
    this.gaps = document.getElementById('gap-count');
    this.sourceRate = document.getElementById('source-rate');
    this.outputRate = document.getElementById('output-rate');
    this.levels = { input: document.getElementById('input-level'), processed: document.getElementById('processed-level') };
    this.clips = { input: document.getElementById('input-clips'), processed: document.getElementById('processed-clips') };
    this.streams = { input: [], processed: [] };
    this.scores = [];
    this.gapMarks = [];
    this.gapCount = 0;
    this.epoch = -1;
    this.model = null;
    this.threshold = 0.7;
    this.muted = false;
    this.monitoring = false;
    this.requested = false;
    this.startGeneration = 0;
    this.playing = false;
    this.sources = [];
    this.nextSample = null;
    this.nextTime = 0;
    this.cursor = null;
    this.frame = null;
    this.startButton.addEventListener('click', () => this.start());
    this.stopButton.addEventListener('click', () => this.stop());
    this.feedSelect.addEventListener('change', () => this.switchFeed());
    this.volume.addEventListener('input', () => { if (this.gain) this.gain.gain.value = Number(this.volume.value) / 100; document.getElementById('listen-volume-value').value = this.volume.value; });
    this.updateButtons();
    this.draw();
  }

  async start() {
    if (this.requested || this.monitoring) return;
    const generation = ++this.startGeneration;
    this.requested = true;
    this.updateButtons();
    try {
      if (!this.context) {
        this.context = new AudioContext();
        this.gain = this.context.createGain();
        this.gain.gain.value = Number(this.volume.value) / 100;
        this.gain.connect(this.context.destination);
      }
      await this.context.resume();
      if (generation !== this.startGeneration) return;
      if (document.hidden) { this.stop(false); return; }
      this.outputRate.textContent = String(this.context.sampleRate);
      if (!this.send({ command: 'monitor_start' })) throw new Error('WebUI connection unavailable');
      this.requested = true;
      this.status.textContent = 'Starting…';
      this.updateButtons();
    } catch (error) {
      if (generation !== this.startGeneration) return;
      this.requested = false;
      this.status.textContent = error.message;
      this.updateButtons();
    }
  }

  stop(send = true) {
    this.startGeneration++;
    if (send && (this.monitoring || this.requested)) this.send({ command: 'monitor_stop' });
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.resetPlayback();
    this.clearHistory();
    this.monitoring = false;
    this.requested = false;
    this.status.textContent = 'Stopped';
    this.updateButtons();
  }

  disconnected() {
    this.startGeneration++;
    if (this.frame !== null) cancelAnimationFrame(this.frame);
    this.frame = null;
    this.resetPlayback();
    this.clearHistory();
    this.monitoring = false;
    this.requested = false;
    this.epoch = -1;
    this.status.textContent = 'Disconnected';
    this.updateButtons();
  }

  state(state) {
    if (this.model !== null && this.model !== state.primary_model) this.clearHistory();
    this.model = state.primary_model;
    this.threshold = state.primary_threshold;
    if (!this.muted && state.muted) { this.resetPlayback(); this.clearHistory(); }
    this.muted = state.muted;
    document.getElementById('monitor-model').textContent = state.primary_model ?? 'None';
    document.getElementById('monitor-threshold').textContent = Number(state.primary_threshold).toFixed(3);
    document.getElementById('monitor-revision').textContent = String(state.revision);
    if (this.monitoring && state.muted) this.status.textContent = 'Muted';
    this.draw();
  }

  message(message) {
    if (message.type === 'monitor') {
      if (!message.active) { this.stop(false); return; }
      this.epoch = message.epoch;
      this.streamId = message.stream_id;
      this.monitoring = true;
      this.requested = false;
      this.sourceRate.textContent = String(message.sample_rate);
      this.status.textContent = this.muted ? 'Muted' : 'Live';
      this.updateButtons();
      if (this.frame === null) this.animate();
    } else if (message.type === 'reset') {
      this.epoch = message.epoch;
      this.resetPlayback();
      this.clearHistory();
      this.status.textContent = this.muted ? 'Muted' : 'Restarting…';
    } else if (message.type === 'gap') {
      if (message.epoch !== this.epoch) return;
      this.markGap(message.to_sample);
      this.resetPlayback();
    } else if (message.type === 'detector') {
      document.getElementById('detector-status').textContent = message.active ? 'Detection active' : 'Waiting for Home Assistant';
    } else if (message.type === 'scores' && message.epoch === this.epoch && this.monitoring && !this.muted) {
      for (const item of message.items) this.scores.push(item);
      const newest = this.scores.at(-1)?.at_sample ?? 0;
      this.scores = this.scores.filter(item => item.at_sample >= newest - HISTORY);
      this.draw();
    }
  }

  binary(buffer) {
    if (!this.monitoring || this.muted) return;
    let frame;
    try { frame = decodeFrame(buffer); } catch { this.markGap(); return; }
    if (frame.epoch !== this.epoch) return;
    const previous = this.streams[frame.feed].at(-1);
    if (frame.gap || (previous && frame.start !== previous.end)) {
      this.markGap(frame.start);
      if (frame.feed === this.feedSelect.value) this.resetPlayback();
    } else this.status.textContent = 'Live';
    const stream = this.streams[frame.feed];
    stream.push(frame);
    const floor = frame.end - SECOND;
    while (stream.length && stream[0].end <= floor) stream.shift();
    this.meter(frame);
    if (frame.feed === this.feedSelect.value) this.schedule();
  }

  switchFeed() {
    const aligned = this.playCursor();
    this.resetPlayback(false);
    this.cursor = aligned === null ? null : Math.round(aligned);
    this.schedule();
  }

  schedule() {
    if (!this.monitoring || this.muted || !this.context) return;
    const stream = this.streams[this.feedSelect.value];
    if (!stream.length) return;
    if (!this.playing) {
      const first = stream.find(frame => frame.end > (this.cursor ?? -1));
      if (!first) return;
      const start = Math.max(first.start, this.cursor ?? first.start, stream.at(-1).end - SECOND);
      if (stream.at(-1).end - start < RATE / 4) return;
      this.nextSample = start;
      this.nextTime = this.context.currentTime + 0.03;
      this.playing = true;
    }
    for (const frame of stream) {
      if (frame.end <= this.nextSample) continue;
      if (frame.start > this.nextSample) { this.markGap(frame.start); this.resetPlayback(); return; }
      const offset = this.nextSample - frame.start;
      const data = frame.data.subarray(offset);
      const startTime = Math.max(this.context.currentTime + 0.01, this.nextTime);
      if (startTime + data.length / RATE - this.context.currentTime > 1) { this.markGap(this.nextSample); this.resetPlayback(); return; }
      const buffer = this.context.createBuffer(1, data.length, RATE);
      buffer.getChannelData(0).set(data);
      const source = this.context.createBufferSource();
      source.buffer = buffer;
      source.connect(this.gain);
      source.start(startTime);
      this.sources.push({ source, startTime, startSample: this.nextSample, endTime: startTime + data.length / RATE, endSample: frame.end });
      this.nextTime = startTime + data.length / RATE;
      this.nextSample = frame.end;
    }
    this.sources = this.sources.filter(item => item.endTime >= this.context.currentTime);
  }

  playCursor() {
    if (!this.context) return this.cursor;
    const now = this.context.currentTime;
    if (this.sources.length && now < this.sources[0].startTime) return this.sources[0].startSample;
    const active = this.sources.find(item => now >= item.startTime && now < item.endTime);
    if (active) return active.startSample + (now - active.startTime) * RATE;
    if (this.sources.length && now < this.sources.at(-1).endTime) return this.sources.findLast(item => item.endTime <= now)?.endSample ?? this.sources[0].startSample;
    return this.sources.at(-1)?.endSample ?? this.cursor;
  }

  resetPlayback(clearBuffers = true) {
    for (const item of this.sources) { try { item.source.stop(); } catch { /* The source already ended. */ } }
    this.sources = [];
    if (clearBuffers) this.streams = { input: [], processed: [] };
    this.playing = false;
    this.nextSample = null;
    this.nextTime = 0;
    this.cursor = null;
  }

  clearHistory() {
    this.scores = [];
    this.gapMarks = [];
    this.gapCount = 0;
    this.gaps.textContent = '0';
    this.draw();
  }

  markGap(sample) {
    this.gapCount += 1;
    this.gaps.textContent = String(this.gapCount);
    if (Number.isFinite(sample)) this.gapMarks.push(sample);
    this.gapMarks = this.gapMarks.slice(-64);
    this.status.textContent = 'Audio gap';
  }

  meter(frame) {
    let peak = 0;
    let clips = 0;
    for (const sample of frame.data) {
      const level = Math.abs(sample);
      peak = Math.max(peak, level);
      if (frame.feed === 'input' ? level >= 1 : sample === -1 || sample === 32767 / 32768) clips++;
    }
    this.levels[frame.feed].textContent = `${Math.round(peak * 100)}% peak`;
    this.clips[frame.feed].textContent = `${clips} clipped`;
  }

  animate() {
    if (!this.monitoring || document.hidden) { this.frame = null; return; }
    this.draw();
    this.frame = requestAnimationFrame(() => { this.frame = null; this.animate(); });
  }

  draw() {
    const canvas = this.canvas;
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 220;
    const ratio = devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = '#101b25'; ctx.fillRect(0, 0, width, height);
    ctx.strokeStyle = '#314a59'; ctx.beginPath(); ctx.moveTo(0, height - 22); ctx.lineTo(width, height - 22); ctx.stroke();
    const right = this.playCursor() ?? this.scores.at(-1)?.at_sample ?? 0;
    const left = right - HISTORY;
    const x = sample => (sample - left) / HISTORY * width;
    const y = probability => 16 + (1 - probability) * (height - 38);
    ctx.strokeStyle = '#d7a259'; ctx.setLineDash([5, 5]); ctx.beginPath(); ctx.moveTo(0, y(this.threshold)); ctx.lineTo(width, y(this.threshold)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = '#7fdcca';
    for (const score of this.scores) {
      const pointX = x(score.at_sample);
      if (pointX < 0 || pointX > width) continue;
      ctx.beginPath(); ctx.arc(pointX, y(score.probability), score.crossing ? 4 : 2, 0, Math.PI * 2); ctx.fill();
      if (score.accepted) { ctx.strokeStyle = '#edca71'; ctx.beginPath(); ctx.moveTo(pointX, 8); ctx.lineTo(pointX, height - 22); ctx.stroke(); }
    }
    ctx.strokeStyle = '#e17878';
    for (const sample of this.gapMarks) { const pointX = x(sample); if (pointX >= 0 && pointX <= width) { ctx.beginPath(); ctx.moveTo(pointX, 0); ctx.lineTo(pointX, height); ctx.stroke(); } }
    ctx.fillStyle = '#a6bac8'; ctx.font = '12px system-ui'; ctx.fillText('30 seconds', 12, height - 6); ctx.fillText('threshold', 12, Math.max(12, y(this.threshold) - 5));
    if (this.playing) { ctx.strokeStyle = '#ffffff'; ctx.beginPath(); ctx.moveTo(width - 1, 0); ctx.lineTo(width - 1, height); ctx.stroke(); }
  }

  updateButtons() {
    this.startButton.disabled = this.monitoring || this.requested;
    this.stopButton.disabled = !(this.monitoring || this.requested);
  }
}
