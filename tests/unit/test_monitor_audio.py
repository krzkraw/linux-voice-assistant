"""Observe the real audio loop without changing wake detection or PCM bytes."""

# pylint: disable=too-many-arguments,too-many-positional-arguments,too-many-locals

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from linux_voice_assistant.monitor import MonitorBus
from tests.unit.conftest import make_state


class FakeMicro:
    def __init__(self, model_id, probabilities):
        self.id = model_id
        self.wake_word = model_id
        self.probabilities = list(probabilities)
        self.probability_cutoff = 0.7
        self.debug_probabilities = False
        self.calls = 0

    def process_streaming_prob(self, _features):
        self.calls += 1
        return self.probabilities.pop(0)

    def process_streaming(self, features):
        probability = self.process_streaming_prob(features)
        return probability is not None and probability > self.probability_cutoff


class FakeOpen:
    def __init__(self, model_id, probabilities):
        self.id = model_id
        self.wake_word = model_id
        self.probabilities = list(probabilities)
        self.calls = 0

    def process_streaming(self, _features):
        self.calls += 1
        yield self.probabilities.pop(0)


class FakeSatellite:
    def __init__(self, state, active=False):
        self.state = state
        self._is_streaming_audio = False
        self._pipeline_active = active
        self.audio = []
        self.wakeups = 0

    def handle_audio(self, chunk, reference):
        self.audio.append((chunk, reference))

    def wakeup(self, _model):
        if not self._pipeline_active:
            self._pipeline_active = True
            self.wakeups += 1


def run_once(tmp_path, family, monitoring, satellite=True, active=False, channels=2):
    from linux_voice_assistant.__main__ import process_audio

    raw = np.array([[0.25, -0.5], [-0.75, 0.125]] * 512, dtype=np.float32)[:, :channels]
    primary = FakeMicro("primary", [0.1, 0.8]) if family == "micro" else FakeOpen("primary", [0.1, 0.8])
    secondary = FakeMicro("secondary", [0.2, 0.2])
    state = make_state(tmp_path, audio_input_channels=channels, mic_volume=50, wake_words={"primary": primary, "secondary": secondary}, active_wake_words={"primary", "secondary"})
    state.preferences.active_wake_words = ["primary", "secondary"]
    fake_satellite = FakeSatellite(state, active) if satellite else None
    state.satellite = fake_satellite
    deliveries = []
    if monitoring:
        bus = MonitorBus(MagicMock(), lambda events, dropped: deliveries.extend(events))
        bus.start()
        state.monitor_bus = bus
    mic = MagicMock()
    mic.recorder.return_value.__enter__.return_value.record.side_effect = [raw, RuntimeError("finished")]
    features = MagicMock()
    features.process_streaming.return_value = [object(), object()]
    with (
        patch("linux_voice_assistant.__main__.MicroWakeWord", FakeMicro),
        patch("linux_voice_assistant.__main__.OpenWakeWord", FakeOpen),
        patch("linux_voice_assistant.__main__.MicroWakeWordFeatures", return_value=features),
        patch("linux_voice_assistant.__main__.OpenWakeWordFeatures.from_builtin", return_value=features),
    ):
        with pytest.raises(SystemExit):
            process_audio(state, mic, 1024)
    if monitoring:
        bus._drain()
    return raw, primary, secondary, fake_satellite, deliveries


@pytest.mark.parametrize("family", ["micro", "open"])
def test_monitoring_preserves_detector_and_exact_audio(tmp_path, family):
    raw, primary, secondary, satellite, events = run_once(tmp_path, family, True)
    _, quiet_primary, quiet_secondary, quiet_satellite, _ = run_once(tmp_path, family, False)
    assert primary.calls == quiet_primary.calls
    assert secondary.calls == quiet_secondary.calls == 2
    assert satellite.wakeups == quiet_satellite.wakeups == 1
    assert satellite.audio == quiet_satellite.audio
    assert len(events) == 1
    event = events[0]
    assert event.input_bytes == raw[:, 0].astype("<f4").tobytes()
    assert b"".join(data for _, data, _ in event.processed) == satellite.audio[0][0]
    assert event.processed[0][0] == 0
    assert event.scores == [
        {"model": "primary", "at_sample": 1024, "probability": 0.1, "threshold": 0.7, "crossing": False, "accepted": False},
        {"model": "primary", "at_sample": 1024, "probability": 0.8, "threshold": 0.7, "crossing": True, "accepted": True},
    ]
    expected_reference = (np.clip(raw[:, 1] * 0.5, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    assert satellite.audio[0][1] == expected_reference


def test_monitor_exports_audio_without_ha_but_does_not_infer(tmp_path):
    raw, primary, secondary, satellite, events = run_once(tmp_path, "micro", True, satellite=False, channels=1)
    assert satellite is None
    assert primary.calls == secondary.calls == 0
    assert len(events) == 1
    assert events[0].input_bytes == raw[:, 0].astype("<f4").tobytes()
    assert events[0].processed
    assert events[0].scores == []
    assert events[0].detector_active is False


def test_active_pipeline_suppresses_accepted_marker(tmp_path):
    from linux_voice_assistant.satellite import VoiceSatelliteProtocol

    with patch.object(FakeSatellite, "wakeup", VoiceSatelliteProtocol.wakeup):
        _, _, _, satellite, events = run_once(tmp_path, "micro", True, active=True)
    assert satellite.wakeups == 0
    assert events[0].scores[1]["crossing"] is True
    assert events[0].scores[1]["accepted"] is False


@pytest.mark.parametrize("mute_during_second", [False, True])
def test_webrtc_multiblock_exports_feature_bytes_and_flushes_inflight_mute(tmp_path, mute_during_second):
    from linux_voice_assistant.__main__ import process_audio

    blocks = [np.full((1024, 1), value, dtype=np.float32) for value in (0.125, -0.25, 0.5)]
    primary = FakeMicro("primary", [0.1, 0.1, 0.1])
    state = make_state(tmp_path, audio_input_channels=1, wake_words={"primary": primary}, active_wake_words={"primary"})
    state.preferences.active_wake_words = ["primary"]
    state.preferences.mic_noise_suppression = 1
    state.stop_word.process_streaming.return_value = False
    satellite = FakeSatellite(state)
    state.satellite = satellite
    events = []
    bus = MonitorBus(MagicMock(), lambda published, _dropped: events.extend(published))
    bus.start()
    state.monitor_bus = bus
    mic = MagicMock()
    calls = 0

    def record(_size):
        nonlocal calls
        if calls == 1 and mute_during_second:
            bus._drain()
        if calls == len(blocks):
            raise RuntimeError("finished")
        block = blocks[calls]
        calls += 1
        return block

    mic.recorder.return_value.__enter__.return_value.record.side_effect = record
    frames = 0

    def process_frame(frame):
        nonlocal frames
        frames += 1
        if mute_during_second and frames == 7:
            state.muted = True
            bus.reset()
        return SimpleNamespace(audio=frame)

    features = MagicMock()
    features.process_streaming.return_value = [object()]
    with (
        patch("linux_voice_assistant.__main__.MicroWakeWord", FakeMicro),
        patch("linux_voice_assistant.__main__.MicroWakeWordFeatures", return_value=features),
        patch("webrtc_noise_gain.AudioProcessor") as processor,
    ):
        processor.return_value.Process10ms.side_effect = process_frame
        with pytest.raises(SystemExit):
            process_audio(state, mic, 1024)
    bus._drain()
    feature_bytes = [call.args[0] for call in features.process_streaming.call_args_list]
    assert feature_bytes == [audio for audio, _ in satellite.audio]
    assert [len(audio) // 2 for audio in feature_bytes] == [960, 960, 1120]
    assert [event.start for event in events] == ([0] if mute_during_second else [0, 1024, 2048])
    assert b"".join(data for event in events for _, data, _ in event.processed) == b"".join(feature_bytes[: len(events)])
    assert b"".join(event.input_bytes for event in events) == b"".join(block.astype("<f4").tobytes() for block in blocks[: len(events)])
    if not mute_during_second:
        assert [[(start, len(data) // 2) for start, data, _ in event.processed] for event in events] == [[(0, 960)], [(960, 64), (1024, 896)], [(1920, 128), (2048, 992)]]
