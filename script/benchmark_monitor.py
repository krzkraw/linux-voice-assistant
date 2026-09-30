#!/usr/bin/env python3
"""Measure the production audio loop with synthetic input and real local models."""

import argparse
import hashlib
import json
import platform
import resource
import statistics
import sys
import tempfile
import time
import tracemalloc
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pymicro_wakeword import MicroWakeWord

from linux_voice_assistant.__main__ import process_audio
from linux_voice_assistant.monitor import MonitorBus
from linux_voice_assistant.wake_word import find_available_wake_words
from tests.unit.conftest import make_state


class SyntheticMicrophone:
    """Replace device I/O and measure each completed production block."""

    def __init__(self, blocks, bus, drain):
        self.name = "synthetic-benchmark"
        self.remaining = blocks
        self.bus = bus
        self.drain = drain
        self.raw = np.random.default_rng(0).uniform(-0.05, 0.05, (1024, 1)).astype(np.float32)
        self.previous = None
        self.timings = deque(maxlen=2000)
        self.completed = 0
        self.queue_peak = 0

    def recorder(self, **_kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def record(self, _size):
        now = time.perf_counter_ns()
        if self.previous is not None:
            self.timings.append((now - self.previous) / 1000)
            self.completed += 1
        if self.bus is not None:
            self.queue_peak = max(self.queue_peak, len(self.bus._queue))
            if self.drain:
                self.bus._drain()
        if self.remaining == 0:
            raise SystemExit(0)
        self.remaining -= 1
        self.previous = time.perf_counter_ns()
        return self.raw


def measure(args, directory):
    available = find_available_wake_words([Path("wakewords"), Path("wakewords/openWakeWord")], "stop")
    primary = available[args.model].load()
    stop = MicroWakeWord.from_config(config_path=Path("wakewords/stop.json"))
    digest = hashlib.sha256()
    satellite = SimpleNamespace(_is_streaming_audio=False, _pipeline_active=False, wakeups=0, stops=0)
    satellite.handle_audio = lambda audio, _reference: digest.update(audio)

    def wakeup(_model):
        satellite.wakeups += 1
        satellite._pipeline_active = True

    def stopped():
        satellite.stops += 1
        satellite._pipeline_active = False

    satellite.wakeup = wakeup
    satellite.stop = stopped
    state = make_state(directory, audio_input_channels=1, available_wake_words=available, wake_words={args.model: primary}, active_wake_words={args.model}, stop_word=stop, satellite=satellite)
    satellite.state = state
    state.preferences.active_wake_words = [args.model, None]
    state.preferences.mic_noise_suppression = args.noise_suppression
    state.preferences.mic_auto_gain = args.auto_gain
    bus = None if args.mode == "disabled" else MonitorBus(SimpleNamespace(call_soon_threadsafe=lambda _callback: None), lambda _events, _dropped: None)
    state.monitor_bus = bus
    if bus is not None and args.mode in ("active", "slow", "disconnected"):
        bus.start()
        if args.mode == "disconnected":
            bus.stop()
    mic = SyntheticMicrophone(args.blocks, bus, args.mode == "active")
    if args.trace_memory:
        tracemalloc.start()
    started, cpu_started = time.perf_counter(), time.process_time()
    try:
        process_audio(state, mic, 1024)
    except SystemExit as exc:
        assert exc.code == 0 and mic.remaining == 0, "Audio loop failed before benchmark completion"
    elapsed, cpu = time.perf_counter() - started, time.process_time() - cpu_started
    assert mic.completed == args.blocks
    result = {
        "wall_seconds": elapsed,
        "cpu_seconds": cpu,
        "block_us_p50": statistics.median(mic.timings),
        "block_us_p95": float(np.percentile(mic.timings, 95)),
        "block_us_p99": float(np.percentile(mic.timings, 99)),
        "timed_blocks": len(mic.timings),
        "queue_peak": mic.queue_peak,
        "pcm_sha256": digest.hexdigest(),
        "pipeline_active": satellite._pipeline_active,
        "wakeups": satellite.wakeups,
        "stops": satellite.stops,
    }
    if args.trace_memory:
        result["python_memory_current_peak_bytes"] = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("disabled", "idle", "active", "slow", "disconnected"), required=True)
    parser.add_argument("--blocks", type=int, default=2000)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--model", default="okay_nabu", help="Available local model ID")
    parser.add_argument("--noise-suppression", type=int, choices=range(5), default=0)
    parser.add_argument("--auto-gain", type=int, choices=range(32), default=0)
    parser.add_argument("--trace-memory", action="store_true", help="Trace Python allocations; timings include tracing overhead")
    args = parser.parse_args()
    if args.blocks < 1 or args.runs < 1:
        parser.error("blocks and runs must be positive")
    with tempfile.TemporaryDirectory(prefix="lva-monitor-benchmark-") as directory:
        runs = [measure(args, Path(directory)) for _ in range(args.runs)]
    peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    print(
        json.dumps(
            {
                "mode": args.mode,
                "model": args.model,
                "platform": platform.platform(),
                "python": platform.python_version(),
                "blocks": args.blocks,
                "noise_suppression": args.noise_suppression,
                "auto_gain": args.auto_gain,
                "trace_memory": args.trace_memory,
                "peak_rss_bytes": peak_rss if sys.platform == "darwin" else peak_rss * 1024,
                "runs": runs,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
