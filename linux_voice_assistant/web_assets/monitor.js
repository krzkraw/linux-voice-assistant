const RATE = 16000;
const SECOND = RATE;
const HISTORY = RATE * 300;

export function levelIntervals(input, processed) {
  const intervals = [];
  let i = 0, j = 0;
  let start = Math.min(input[0]?.start ?? Infinity, processed[0]?.start ?? Infinity);
  while (i < input.length || j < processed.length) {
    while (i < input.length && input[i].end <= start) i++;
    while (j < processed.length && processed[j].end <= start) j++;
    const a = input[i], b = processed[j];
    const activeInput = a?.start <= start ? a : undefined;
    const activeProcessed = b?.start <= start ? b : undefined;
    const end = Math.min(activeInput ? a.end : a?.start ?? Infinity, activeProcessed ? b.end : b?.start ?? Infinity);
    if (activeInput || activeProcessed) {
      const paired = activeInput && activeProcessed && a.segment === b.segment;
      const merged = Boolean(paired && (a.level === b.level || Math.abs(a.level - b.level) <= 2));
      intervals.push({ start, end, input: activeInput, processed: activeProcessed, merged, fill: Boolean(paired && !merged) });
    }
    start = end;
  }
  return intervals;
}

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
    this.levelCanvas = document.getElementById('level-graph');
    this.windowControl = document.getElementById('graph-window');
    this.windowSeconds = 30;
    this.windowControl.value = String(this.windowSeconds);
    this.status = document.getElementById('monitor-status');
    this.gaps = document.getElementById('gap-count');
    this.sourceRate = document.getElementById('source-rate');
    this.outputRate = document.getElementById('output-rate');
    this.levels = { input: document.getElementById('input-level'), processed: document.getElementById('processed-level') };
    this.meters = { input: document.getElementById('input-meter'), processed: document.getElementById('processed-meter') };
    this.clips = { input: document.getElementById('input-clips'), processed: document.getElementById('processed-clips') };
    this.streams = { input: [], processed: [] };
    this.scores = [];
    this.levelHistory = { input: [], processed: [] };
    this.levelNewest = 0;
    this.levelSegment = 0;
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
    this.updateColors();
    document.addEventListener('appearancechange', () => { this.updateColors(); this.draw(); });
    this.startButton.addEventListener('click', () => this.start());
    this.stopButton.addEventListener('click', () => this.stop());
    this.feedSelect.addEventListener('change', () => this.switchFeed());
    this.volume.addEventListener('input', () => { if (this.gain) this.gain.gain.value = Number(this.volume.value) / 100; document.getElementById('listen-volume-value').value = this.volume.value; });
    this.windowControl.addEventListener('input', () => {
      const seconds = Number(this.windowControl.value);
      if (!Number.isInteger(seconds) || seconds < 30 || seconds > 300) return;
      this.windowSeconds = seconds;
      this.draw();
    });
    const restoreWindow = () => { this.windowControl.value = String(this.windowSeconds); };
    for (const event of ['change', 'blur']) this.windowControl.addEventListener(event, restoreWindow);
    this.windowControl.addEventListener('keydown', event => { if (event.key === 'Enter') restoreWindow(); });
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
      if (message.epoch !== this.epoch) { this.resetPlayback(); this.clearHistory(); }
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
      if (message.reason === 'processor' && !this.muted) this.levelSegment++;
      else this.clearHistory();
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
    if (frame.epoch !== this.epoch || !frame.data.length) return;
    const previous = this.streams[frame.feed].at(-1);
    if (frame.gap || (previous && frame.start !== previous.end)) {
      this.markGap(frame.start);
      if (frame.feed === this.feedSelect.value) this.resetPlayback();
    } else this.status.textContent = 'Live';
    const stream = this.streams[frame.feed];
    stream.push(frame);
    const floor = frame.end - SECOND;
    while (stream.length && stream[0].end <= floor) stream.shift();
    if (stream[0].start < floor) {
      stream[0].data = stream[0].data.slice(floor - stream[0].start);
      stream[0].start = floor;
    }
    this.levelHistory[frame.feed].push({ start: frame.start, end: frame.end, level: this.meter(frame), segment: this.levelSegment });
    this.levelNewest = Math.max(this.levelNewest, frame.end);
    for (const feed of ['input', 'processed']) while (this.levelHistory[feed][0]?.end <= this.levelNewest - HISTORY) this.levelHistory[feed].shift();
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
    if (clearBuffers) {
      this.streams = { input: [], processed: [] };
      for (const feed of ['input', 'processed']) {
        this.meters[feed].value = -90;
        this.levels[feed].textContent = '—';
        this.clips[feed].textContent = '—';
      }
    }
    this.playing = false;
    this.nextSample = null;
    this.nextTime = 0;
    this.cursor = null;
  }

  clearHistory() {
    this.scores = [];
    this.levelHistory = { input: [], processed: [] };
    this.levelNewest = 0;
    this.levelSegment = 0;
    this.gapMarks = [];
    this.gapCount = 0;
    this.gaps.textContent = '0';
    this.draw();
  }

  markGap(sample) {
    this.levelSegment++;
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
    const db = peak > 0 ? 20 * Math.log10(peak) : -Infinity;
    this.meters[frame.feed].value = Math.max(-90, Math.min(0, db));
    this.levels[frame.feed].textContent = `${peak > 0 ? db.toFixed(1) : '−∞'} dBFS`;
    this.clips[frame.feed].textContent = `${clips} clipped`;
    return db;
  }

  animate() {
    if (!this.monitoring || document.hidden) { this.frame = null; return; }
    this.draw();
    this.frame = requestAnimationFrame(() => { this.frame = null; this.animate(); });
  }

  draw() {
    const { ctx, width, height } = this.prepareCanvas(this.canvas);
    const right = this.playCursor() ?? Math.max(this.scores.at(-1)?.at_sample ?? 0, this.levelNewest);
    const window = this.windowSeconds * RATE;
    const left = right - window;
    const x = sample => 36 + (sample - left) / window * (width - 36);
    const y = probability => 16 + (1 - probability) * (height - 38);
    ctx.font = '12px system-ui';
    for (const probability of [0, .25, .5, .75, 1]) {
      ctx.strokeStyle = this.colors.grid; ctx.beginPath(); ctx.moveTo(36, y(probability)); ctx.lineTo(width, y(probability)); ctx.stroke();
      ctx.fillStyle = this.colors.muted; ctx.fillText(probability.toFixed(2), 2, y(probability) + 4);
    }
    ctx.strokeStyle = this.colors.accent; ctx.setLineDash([5, 5]); ctx.beginPath(); ctx.moveTo(36, y(this.threshold)); ctx.lineTo(width, y(this.threshold)); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = this.colors.primary;
    for (const score of this.scores) {
      const pointX = x(score.at_sample);
      if (pointX < 36 || pointX > width) continue;
      ctx.beginPath(); ctx.arc(pointX, y(score.probability), score.crossing ? 4 : 2, 0, Math.PI * 2); ctx.fill();
      if (score.accepted) { ctx.strokeStyle = this.colors.accent; ctx.beginPath(); ctx.moveTo(pointX, 8); ctx.lineTo(pointX, height - 22); ctx.stroke(); }
    }
    ctx.strokeStyle = this.colors.error;
    for (const sample of this.gapMarks) { const pointX = x(sample); if (pointX >= 36 && pointX <= width) { ctx.beginPath(); ctx.moveTo(pointX, 0); ctx.lineTo(pointX, height); ctx.stroke(); } }
    ctx.fillStyle = this.colors.muted; ctx.fillText(`${this.windowSeconds} seconds`, 36, height - 6); ctx.fillText('threshold', 44, Math.max(12, y(this.threshold) - 5));
    if (this.playing) { ctx.strokeStyle = this.colors['on-surface']; ctx.beginPath(); ctx.moveTo(width - 1, 0); ctx.lineTo(width - 1, height); ctx.stroke(); }
    this.drawLevels(right);
  }

  drawLevels(right) {
    const { ctx, width, height } = this.prepareCanvas(this.levelCanvas);
    const window = this.windowSeconds * RATE;
    const left = right - window;
    const x = sample => 36 + (sample - left) / window * (width - 36);
    const y = level => 16 - Math.max(-90, Math.min(0, level)) / 90 * (height - 38);
    ctx.font = '12px system-ui';
    for (const level of [0, -15, -30, -45, -60, -75, -90]) {
      ctx.strokeStyle = this.colors.grid; ctx.beginPath(); ctx.moveTo(36, y(level)); ctx.lineTo(width, y(level)); ctx.stroke();
      ctx.fillStyle = this.colors.muted; ctx.fillText(String(level), 4, y(level) + 4);
    }
    const intervals = levelIntervals(this.levelHistory.input.filter(point => point.end > left && point.start < right), this.levelHistory.processed.filter(point => point.end > left && point.start < right));
    ctx.fillStyle = this.colors.band;
    ctx.beginPath();
    for (const interval of intervals) {
      if (!interval.fill || interval.end <= left || interval.start >= right) continue;
      const startX = x(Math.max(left, interval.start)), endX = x(Math.min(right, interval.end));
      const a = y(interval.input.level), b = y(interval.processed.level);
      ctx.rect(startX, Math.min(a, b), endX - startX, Math.abs(a - b));
    }
    ctx.fill();
    ctx.lineWidth = 2.5;
    for (const feed of ['processed', 'input']) {
      ctx.strokeStyle = feed === 'input' ? this.colors.primary : this.colors.accent;
      ctx.beginPath();
      let previous;
      for (const interval of intervals) {
        const point = interval[feed];
        if (!point || (feed === 'processed' && interval.merged)) { previous = undefined; continue; }
        if (interval.end <= left || interval.start >= right) continue;
        const startX = x(Math.max(left, interval.start));
        const endX = x(Math.min(right, interval.end));
        if (previous && previous.end === interval.start && previous.segment === point.segment) ctx.lineTo(startX, y(point.level));
        else ctx.moveTo(startX, y(point.level));
        ctx.lineTo(endX, y(point.level));
        previous = { end: interval.end, segment: point.segment };
      }
      ctx.stroke();
    }
    ctx.setLineDash([]); ctx.lineWidth = 1;
    ctx.strokeStyle = this.colors.error;
    for (const sample of this.gapMarks) { const pointX = x(sample); if (pointX >= 36 && pointX <= width) { ctx.beginPath(); ctx.moveTo(pointX, 0); ctx.lineTo(pointX, height); ctx.stroke(); } }
    ctx.fillStyle = this.colors.muted; ctx.fillText(`${this.windowSeconds} seconds`, 36, height - 6);
    if (this.playing) { ctx.strokeStyle = this.colors['on-surface']; ctx.beginPath(); ctx.moveTo(width - 1, 0); ctx.lineTo(width - 1, height); ctx.stroke(); }
  }

  prepareCanvas(canvas) {
    const width = canvas.clientWidth || 600;
    const height = canvas.clientHeight || 220;
    const ratio = devicePixelRatio || 1;
    if (canvas.width !== Math.round(width * ratio) || canvas.height !== Math.round(height * ratio)) { canvas.width = Math.round(width * ratio); canvas.height = Math.round(height * ratio); }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ctx.fillStyle = this.colors.background; ctx.fillRect(0, 0, width, height);
    return { ctx, width, height };
  }

  updateButtons() {
    this.startButton.disabled = this.monitoring || this.requested;
    this.stopButton.disabled = !(this.monitoring || this.requested);
  }

  updateColors() {
    const style = getComputedStyle(document.documentElement);
    this.colors = Object.fromEntries(['background', 'on-surface', 'muted', 'grid', 'primary', 'accent', 'band', 'error'].map(role => [role, style.getPropertyValue(`--${role}`).trim()]));
  }
}
