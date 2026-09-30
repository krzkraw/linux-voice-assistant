"""Bounded observation of the existing microphone and wake-word pipeline."""

import asyncio
import secrets
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

SAMPLE_RATE = 16000
MAX_BLOCK_SAMPLES = 4096


@dataclass
class MonitorEvent:
    epoch: int
    start: int
    end: int
    revision: int
    input_bytes: bytes
    processed: list[tuple[int, bytes, bool]]
    scores: list[dict]
    detector_active: bool


class MonitorBus:
    """Move diagnostic data to the event loop without blocking the audio thread."""

    def __init__(self, loop: asyncio.AbstractEventLoop, deliver: Callable[[list[MonitorEvent], Optional[tuple[int, int, int]]], None]) -> None:
        self.loop = loop
        self.deliver = deliver
        self.stream_id = secrets.token_hex(8)
        self.epoch = 0
        self.minimum_sample = 0
        self.last_sample_end = 0
        self.subscribers = 0
        self._lock = threading.Lock()
        self._queue: deque[MonitorEvent] = deque()
        self._scheduled = False
        self._dropped: Optional[tuple[int, int, int]] = None
        self._contended: Optional[tuple[int, int, int]] = None

    @property
    def active(self) -> bool:
        return self.subscribers > 0

    def observe(self, end: int) -> None:
        """Track the latest capture position even when monitoring is stopped."""
        self.last_sample_end = end

    def start(self) -> tuple[int, int]:
        """Subscribe a client and return the current epoch and starting sample."""
        with self._lock:
            if self.subscribers == 0:
                self.epoch += 1
                self.minimum_sample = self.last_sample_end
                self._queue.clear()
                self._dropped = None
                self._contended = None
            self.subscribers += 1
            return self.epoch, self.last_sample_end

    def stop(self) -> None:
        """Remove one subscriber and discard pending data when none remain."""
        with self._lock:
            self.subscribers = max(0, self.subscribers - 1)
            if self.subscribers == 0:
                self._queue.clear()
                self._dropped = None
                self._contended = None

    def reset(self) -> int:
        """Invalidate pending audio after mute, model, or processor changes."""
        with self._lock:
            self.epoch += 1
            self.minimum_sample = self.last_sample_end
            self._queue.clear()
            self._dropped = None
            self._contended = None
            return self.epoch

    def capture(self, start: int, end: int) -> Optional[tuple[int, int]]:
        """Reserve bounded diagnostic capacity before copying microphone data."""
        if not self._lock.acquire(blocking=False):
            self._record_contention(self.epoch, start, end)
            return None
        try:
            if self._contended is not None:
                self._mark_drop(*self._contended)
                self._contended = None
            if self.subscribers == 0 or end <= self.minimum_sample:
                return None
            if end - start > MAX_BLOCK_SAMPLES:
                self._mark_drop(self.epoch, start, end)
                return None
            if len(self._queue) >= 8:
                self._mark_drop(self.epoch, start, end)
                return None
            return self.epoch, self.minimum_sample
        finally:
            self._lock.release()

    def offer(self, event: MonitorEvent) -> bool:
        """Publish one block without waiting for the event loop."""
        if not self._lock.acquire(blocking=False):
            self._record_contention(event.epoch, event.start, event.end)
            return False
        try:
            if self.subscribers == 0 or event.epoch != self.epoch:
                return False
            if len(self._queue) >= 8:
                self._mark_drop(self.epoch, event.start, event.end)
                return False
            self._queue.append(event)
            if self._scheduled:
                return True
            self._scheduled = True
        finally:
            self._lock.release()
        try:
            self.loop.call_soon_threadsafe(self._drain)
        except RuntimeError:
            if self._lock.acquire(blocking=False):
                self._queue.clear()
                self._scheduled = False
                self._lock.release()
            return False
        return True

    def _drain(self) -> None:
        with self._lock:
            events = list(self._queue)
            self._queue.clear()
            dropped = self._dropped
            self._dropped = None
            self._scheduled = False
        if events or dropped:
            self.deliver(events, dropped)

    def _mark_drop(self, epoch: int, start: int, end: int) -> None:
        if epoch != self.epoch:
            return
        if self._dropped is None:
            self._dropped = (epoch, start, end)
        else:
            start = min(start, self._dropped[1])
            end = max(end, self._dropped[2])
            # A merged gap must include queued blocks between separate losses.
            retained: deque[MonitorEvent] = deque()
            for event in self._queue:
                if event.start >= start:
                    end = max(end, event.end)
                else:
                    retained.append(event)
            self._queue = retained
            self._dropped = (epoch, start, end)
        self.minimum_sample = max(self.minimum_sample, end)

    def _record_contention(self, epoch: int, start: int, end: int) -> None:
        previous = self._contended
        if previous is not None and previous[0] == epoch:
            self._contended = (epoch, min(start, previous[1]), max(end, previous[2]))
        else:
            self._contended = (epoch, start, end)


class ProcessedTimeline:
    """Map WebRTC's buffered output to capture sample positions."""

    def __init__(self) -> None:
        self.epoch = -1
        self.pending: deque[tuple[int, int]] = deque()
        self.last_end: Optional[int] = None
        self.last_consumed_end: Optional[int] = None
        self.await_empty = False

    def invalidate(self) -> None:
        """Wait for a clean processor frame boundary after an observation gap."""
        self.epoch = -1
        self.pending.clear()
        self.last_end = None
        self.last_consumed_end = None
        self.await_empty = True

    def segments(self, epoch: int, floor: int, start: int, samples: int, output: bytes, use_webrtc: bool, buffered_before: int = 0) -> list[tuple[int, bytes, bool]]:
        """Return exact output bytes split at sample-position discontinuities."""
        if self.epoch != epoch:
            self.epoch = epoch
            self.pending.clear()
            self.last_end = None
            self.last_consumed_end = None
            if use_webrtc and buffered_before and not self.await_empty:
                self.pending.append((start - buffered_before, buffered_before))
        if not use_webrtc:
            self.await_empty = False
            self.last_consumed_end = start + len(output) // 2
            return self._visible(floor, start, output)
        if self.await_empty and buffered_before:
            return []
        self.await_empty = False
        self.pending.append((start, samples))
        remaining = len(output) // 2
        offset = 0
        segments: list[tuple[int, bytes, bool]] = []
        while remaining and self.pending:
            span_start, span_samples = self.pending.popleft()
            taken = min(span_samples, remaining)
            self.last_consumed_end = span_start + taken
            segments.extend(self._visible(floor, span_start, output[offset : offset + taken * 2]))
            offset += taken * 2
            remaining -= taken
            if taken < span_samples:
                self.pending.appendleft((span_start + taken, span_samples - taken))
        if remaining or len(output) % 2:
            self.pending.clear()
            self.last_end = None
            self.last_consumed_end = None
            return []
        return segments

    def _visible(self, floor: int, start: int, data: bytes) -> list[tuple[int, bytes, bool]]:
        end = start + len(data) // 2
        visible_start = max(start, floor, self.last_end if self.last_end is not None else floor)
        if visible_start >= end:
            return []
        visible = data[(visible_start - start) * 2 :]
        gap = self.last_end is not None and visible_start != self.last_end
        self.last_end = end
        return [(visible_start, visible, gap)]
