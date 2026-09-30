"""Bounded diagnostic transport and sample-position mapping."""

# pylint: disable=consider-using-with

from unittest.mock import MagicMock

from linux_voice_assistant.monitor import MonitorBus, MonitorEvent, ProcessedTimeline


def event(epoch, start, end):
    return MonitorEvent(epoch, start, end, 0, b"\x00" * ((end - start) * 4), [], [], True)


def test_thread_queue_is_bounded_and_schedules_one_drain():
    loop = MagicMock()
    deliveries = []
    bus = MonitorBus(loop, lambda events, dropped: deliveries.append((events, dropped)))
    epoch, _ = bus.start()
    for index in range(8):
        start = index * 100
        assert bus.capture(start, start + 100) == (epoch, 0)
        assert bus.offer(event(epoch, start, start + 100))
    assert loop.call_soon_threadsafe.call_count == 1
    assert bus.capture(800, 900) is None
    assert len(bus._queue) == 8
    bus._drain()
    assert len(deliveries[0][0]) == 8
    assert deliveries[0][1] == (epoch, 800, 900)
    assert bus.capture(900, 1000) == (epoch, 900)
    assert bus.offer(event(epoch, 900, 1000))
    assert loop.call_soon_threadsafe.call_count == 2
    bus.stop()
    assert not bus.active
    assert not bus._queue


def test_audio_thread_never_waits_on_monitor_lock():
    bus = MonitorBus(MagicMock(), lambda _events, _dropped: None)
    epoch, _ = bus.start()
    bus._lock.acquire()
    try:
        assert bus.capture(0, 100) is None
        assert bus.capture(100, 200) is None
        assert not bus.offer(event(epoch, 0, 100))
    finally:
        bus._lock.release()
    assert bus.capture(200, 300) == (epoch, 200)
    assert bus._dropped == (epoch, 0, 200)


def test_webrtc_frames_map_to_original_samples_and_toggle_gap():
    timeline = ProcessedTimeline()
    first = b"\x11\x22" * 960
    second = b"\x71\x72" * 960
    assert timeline.segments(1, 0, 0, 1024, first, True, 0) == [(0, first, False)]
    assert timeline.segments(1, 0, 1024, 1024, second, True, 64) == [(960, second[:128], False), (1024, second[128:], False)]
    assert sum(length for _, length in timeline.pending) == 128
    direct = b"\x21\x22" * 1024
    assert timeline.segments(1, 0, 2048, 1024, direct, False) == [(2048, direct, True)]
    resumed = b"\x31\x32" * 960
    assert timeline.segments(1, 0, 3072, 1024, resumed, True, 128) == [(3072, resumed[256:], False)]
    assert timeline.last_consumed_end == 3904


def test_drop_waits_for_clean_processor_boundary():
    timeline = ProcessedTimeline()
    timeline.segments(1, 0, 0, 1024, b"\x00\x00" * 960, True, 0)
    timeline.invalidate()
    assert timeline.segments(1, 1024, 1024, 1024, b"\x11\x22" * 960, True, 64) == []
    assert timeline.segments(1, 2048, 2048, 1024, b"\x33\x44" * 960, True, 0) == [(2048, b"\x33\x44" * 960, False)]


def test_reset_invalidates_old_epoch():
    bus = MonitorBus(MagicMock(), lambda _events, _dropped: None)
    epoch, _ = bus.start()
    assert bus.offer(event(epoch, 0, 100))
    new_epoch = bus.reset()
    assert new_epoch != epoch
    assert not bus._queue
    assert not bus.offer(event(epoch, 100, 200))
    assert bus.capture(100, 200) == (new_epoch, 0)


def test_coalesced_gap_discards_intervening_queued_audio():
    bus = MonitorBus(MagicMock(), lambda _events, _dropped: None)
    epoch, _ = bus.start()
    bus._mark_drop(epoch, 0, 100)
    assert bus.offer(event(epoch, 100, 200))
    bus._mark_drop(epoch, 200, 300)
    assert not bus._queue
    assert bus._dropped == (epoch, 0, 300)
