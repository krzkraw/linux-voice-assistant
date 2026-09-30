"""Shared models."""

import json
import logging
import math
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from pathlib import Path
from queue import Queue
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Set, Union

if TYPE_CHECKING:
    from google.protobuf import message
    from pymicro_wakeword import MicroWakeWord
    from pyopen_wakeword import OpenWakeWord

    from .entity import (
        ButtonEventSensorEntity,
        ButtonLockEntity,
        ESPHomeEntity,
        LEDLightEntity,
        MediaPlayerEntity,
        MicSettingEntity,
        MuteSwitchEntity,
        StopWordSensitivityNumberEntity,
        ThinkingSoundEntity,
        WakeWord1SensitivityNumberEntity,
        WakeWord2SensitivityNumberEntity,
    )
    from .mpv_player import MpvMediaPlayer
    from .satellite import VoiceSatelliteProtocol

_LOGGER = logging.getLogger(__name__)


class WakeWordType(str, Enum):
    MICRO_WAKE_WORD = "micro"
    OPEN_WAKE_WORD = "openWakeWord"


@dataclass
class AvailableWakeWord:
    id: str
    type: WakeWordType
    wake_word: str
    trained_languages: List[str]
    wake_word_path: Path
    probability_cutoff: float = 0.7

    def load(self) -> "Union[MicroWakeWord, OpenWakeWord]":
        if self.type == WakeWordType.MICRO_WAKE_WORD:
            from pymicro_wakeword import MicroWakeWord

            return MicroWakeWord.from_config(config_path=self.wake_word_path)

        if self.type == WakeWordType.OPEN_WAKE_WORD:
            from pyopen_wakeword import OpenWakeWord

            oww_model = OpenWakeWord.from_model(model_path=self.wake_word_path)
            setattr(oww_model, "wake_word", self.wake_word)

            return oww_model

        raise ValueError(f"Unexpected wake word type: {self.type}")


@dataclass
class LightRegistration:
    """Capabilities a peripheral declares for one of its Light entities.

    The peripheral sends this with the register_light command after
    connecting. LVA materialises a matching LEDLightEntity so HA can
    control it.
    """

    name: str
    object_id: str
    icon: str = "mdi:led-strip-variant"
    effects: List[str] = field(default_factory=list)
    supports_rgb: bool = True
    supports_brightness: bool = True


@dataclass
class Preferences:
    active_wake_words: List[Optional[str]] = field(default_factory=list)
    volume: Optional[float] = None
    thinking_sound: int = 0  # 0 = disabled, 1 = enabled
    button_controls_locked: int = 0  # 0 = buttons enabled (default), 1 = buttons locked
    wake_word_1_sensitivity: Optional[float] = None
    wake_word_2_sensitivity: Optional[float] = None
    stop_word_sensitivity: Optional[float] = None

    mic_auto_gain: int = 0
    mic_noise_suppression: int = 0
    mic_volume: int = 100  # 1–100, default maximum


@dataclass
class ServerState:
    name: str
    friendly_name: str
    mac_address: str
    ip_address: str
    network_interface: str
    version: str
    esphome_version: str
    audio_queue: "Queue[Optional[bytes]]"
    entities: "List[ESPHomeEntity]"
    available_wake_words: "Dict[str, AvailableWakeWord]"
    wake_words: "Dict[str, Union[MicroWakeWord, OpenWakeWord]]"
    active_wake_words: Set[str]
    stop_word: "MicroWakeWord"
    music_player: "MpvMediaPlayer"
    tts_player: "MpvMediaPlayer"
    wakeup_sound: str
    start_listening_sound: str
    processing_sound: str
    timer_finished_sound: str
    mute_sound: str
    unmute_sound: str
    button_double_press_sound: str
    button_triple_press_sound: str
    button_long_press_sound: str
    preferences: Preferences
    preferences_path: Path
    download_dir: Path
    continue_conversation_delay: float = 0.5  # seconds to wait after TTS before opening mic

    media_player_entity: "Optional[MediaPlayerEntity]" = None
    satellite: "Optional[VoiceSatelliteProtocol]" = None
    connections: "List[VoiceSatelliteProtocol]" = field(default_factory=list)
    mute_switch_entity: "Optional[MuteSwitchEntity]" = None
    thinking_sound_entity: "Optional[ThinkingSoundEntity]" = None
    button_event_sensor_entity: "Optional[ButtonEventSensorEntity]" = None

    # Lights declared by peripherals via register_light. Survives HA
    # reconnects so the satellite can rebuild its entities whenever it
    # is constructed again.
    pending_lights: "List[LightRegistration]" = field(default_factory=list)
    # Materialised LightEntities keyed by object_id, so light_command
    # events can be routed back to the right peripheral hardware.
    led_light_entities: "Dict[str, LEDLightEntity]" = field(default_factory=dict)

    # True once a peripheral sends register_button. Gates creation of
    # ButtonEventSensorEntity so the HA device page only shows the button
    # entity when hardware that actually supports button presses is present.
    # Survives HA reconnects so the entity is re-registered automatically.
    pending_button: bool = False

    # True once a peripheral sends register_button_lock. Gates creation of
    # ButtonLockEntity so the HA device page only shows the "Disable button
    # controls" switch when hardware that actually supports it is present.
    # Survives HA reconnects so the entity is re-registered automatically.
    pending_button_lock: bool = False
    button_lock_entity: "Optional[ButtonLockEntity]" = None

    # Optional peripheral WebSocket API (LEDs, buttons, HAT boards).
    # Assigned in __main__ before the event loop starts.
    peripheral_api: "Optional[Any]" = None  # PeripheralAPIServer at runtime

    sensitivity_1_number_entity: "Optional[WakeWord1SensitivityNumberEntity]" = None
    sensitivity_2_number_entity: "Optional[WakeWord2SensitivityNumberEntity]" = None
    stop_sensitivity_number_entity: "Optional[StopWordSensitivityNumberEntity]" = None
    mic_gain_entity: "Optional[MicSettingEntity]" = None
    mic_noise_suppression_entity: "Optional[MicSettingEntity]" = None
    mic_volume_entity: "Optional[MicSettingEntity]" = None
    wake_words_changed: bool = False
    refractory_seconds: float = 2.0
    thinking_sound_enabled: bool = False
    button_controls_locked: bool = False
    output_only: bool = False
    muted: bool = False
    connected: bool = False
    volume: float = 1.0
    oww_probability_cutoff: float = 0.7  # Dynamic threshold for OpenWakeWord
    oww_second_probability_cutoff: float = 0.7  # Dynamic threshold for second OpenWakeWord
    oww_stop_probability_cutoff: float = 0.5  # Dynamic threshold for Stop word
    wake_word_1_threshold: float = 0.7
    wake_word_2_threshold: float = 0.7
    stop_word_threshold: float = 0.5
    mic_auto_gain: int = 0
    mic_noise_suppression: int = 0
    mic_volume: int = 100  # 1–100, default maximum
    audio_input_channels: int = 2  # number of mic channels to stream
    timer_max_ring_seconds: float = 900.0
    listen_during_wake_sound: bool = False
    settings_revision: int = 0
    settings_changed: Optional[Callable[[], None]] = None

    def broadcast(self, msgs: "Iterable[message.Message]") -> None:
        """Send messages to every connected API client.

        Entity state changes that happen asynchronously (not in response to a
        request) must reach *all* subscribed clients, not just whichever
        connection happens to be referenced by an entity's ``server``. Without
        this fan-out a second API client leaves Home Assistant stuck on a stale
        state (e.g. ``playing`` after playback has finished).
        """
        messages = list(msgs)
        if not messages:
            return
        for connection in list(self.connections):
            connection.send_messages(messages)

    def save_preferences(self, preferences: Optional[Preferences] = None) -> None:
        """Save preferences as JSON."""
        _LOGGER.debug("Saving preferences: %s", self.preferences_path)
        self.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(dir=self.preferences_path.parent, prefix=f".{self.preferences_path.name}.")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as preferences_file:
                json.dump(asdict(preferences or self.preferences), preferences_file, ensure_ascii=False, indent=4)
                preferences_file.flush()
                os.fsync(preferences_file.fileno())
            os.replace(temporary_path, self.preferences_path)
        finally:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)

    def persist_volume(self, volume: float) -> None:
        """Persist the normalized media volume (0.0 - 1.0)."""
        clamped_volume = max(0.0, min(1.0, volume))
        _LOGGER.debug(
            "persist_volume called: new=%s, current=%s, prefs=%s",
            clamped_volume,
            self.volume,
            self.preferences.volume,
        )

        if abs(self.volume - clamped_volume) < 0.0001 and self.preferences.volume is not None and abs(self.preferences.volume - clamped_volume) < 0.0001:
            _LOGGER.debug("Skipping save - volume unchanged")
            return

        previous_muted = self.volume == 0.0
        self.volume = clamped_volume
        self.preferences.volume = clamped_volume
        _LOGGER.info("Saving volume %s to %s", clamped_volume, self.preferences_path)
        self.save_preferences()
        _LOGGER.info("Volume saved successfully")

        # Notify peripheral container (thread-safe; may be called from mpv callbacks)
        api = self.peripheral_api
        if api is not None:
            from .peripheral_api import LVAEvent  # local import avoids circular dep

            api.emit_event_sync(LVAEvent.VOLUME_CHANGED, {"volume": round(clamped_volume, 3)})

            new_muted = clamped_volume == 0.0
            if previous_muted != new_muted:
                api.emit_event_sync(LVAEvent.VOLUME_MUTED, {"muted": new_muted})

    def persist_mic_gain(self, gain: float) -> None:
        """Persist the microphone auto gain value."""
        self.update_setting("mic_auto_gain", int(gain))

    def persist_mic_noise(self, noise: float) -> None:
        """Persist the microphone noise suppression value."""
        self.update_setting("mic_noise_suppression", int(noise))

    def persist_mic_volume(self, volume: float) -> None:
        """Persist the microphone input volume (0–100)."""
        self.update_setting("mic_volume", max(1, min(100, int(round(volume)))))

    def update_setting(self, name: str, value: object) -> None:
        """Validate and persist a shared microphone or primary threshold setting."""
        limits = {"mic_volume": (1, 100), "mic_auto_gain": (0, 31), "mic_noise_suppression": (0, 4), "wake_word_1_threshold": (0.0, 1.0)}
        if name not in limits or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError("Invalid setting or value")
        minimum, maximum = limits[name]
        if not minimum <= value <= maximum or (name != "wake_word_1_threshold" and not float(value).is_integer()):
            raise ValueError(f"{name} must be between {minimum} and {maximum}")
        effective = float(value) if name == "wake_word_1_threshold" else int(value)
        preference_name = "wake_word_1_sensitivity" if name == "wake_word_1_threshold" else name
        if getattr(self, name) == effective and getattr(self.preferences, preference_name) == effective:
            return
        candidate = replace(self.preferences)
        setattr(candidate, preference_name, effective)
        self.save_preferences(candidate)
        setattr(self, name, effective)
        setattr(self.preferences, preference_name, effective)
        entity_name = {
            "mic_volume": "mic_volume_entity",
            "mic_auto_gain": "mic_gain_entity",
            "mic_noise_suppression": "mic_noise_suppression_entity",
            "wake_word_1_threshold": "sensitivity_1_number_entity",
        }[name]
        entity = getattr(self, entity_name)
        if entity is not None:
            self.broadcast_entity_state(entity)
        self.notify_settings_changed()

    def select_primary_wake_word(self, model_id: str) -> None:
        """Change the primary model without changing the secondary or stop model."""
        if model_id not in self.available_wake_words:
            raise ValueError("Unknown wake word model")
        slots = (self.preferences.active_wake_words + [None, None])[:2]
        if slots[0] == model_id:
            return
        if slots[1] == model_id:
            raise ValueError("The selected model is already the secondary wake word")
        model = self.wake_words.get(model_id) or self.available_wake_words[model_id].load()
        slots[0] = model_id
        self.apply_wake_configuration(slots, {**self.wake_words, model_id: model})

    def apply_wake_configuration(self, slots: List[Optional[str]], models: "Dict[str, Union[MicroWakeWord, OpenWakeWord]]") -> None:
        """Persist active wake word slots before publishing them to the detector."""
        if len(slots) != 2 or any(word_id is not None and word_id not in models for word_id in slots):
            raise ValueError("Invalid active wake word configuration")
        candidate = replace(self.preferences, active_wake_words=slots)
        self.save_preferences(candidate)
        stop_active = {self.stop_word.id} if self.stop_word.id in self.active_wake_words else set()
        self.wake_words = models
        self.active_wake_words = {word_id for word_id in slots if word_id is not None} | stop_active
        self.preferences.active_wake_words = slots
        self.refresh_primary_threshold()
        self.wake_words_changed = True
        if self.sensitivity_1_number_entity is not None:
            self.broadcast_entity_state(self.sensitivity_1_number_entity)
        self.notify_settings_changed()

    def refresh_primary_threshold(self) -> None:
        """Apply the primary model's default when no sensitivity was saved."""
        slots = self.preferences.active_wake_words
        if slots and slots[0] in self.available_wake_words:
            self.wake_word_1_threshold = self.preferences.wake_word_1_sensitivity if self.preferences.wake_word_1_sensitivity is not None else self.available_wake_words[slots[0]].probability_cutoff

    def notify_settings_changed(self) -> None:
        """Publish a new effective settings revision."""
        self.settings_revision += 1
        if self.settings_changed is not None:
            self.settings_changed()

    def broadcast_entity_state(self, entity: "ESPHomeEntity") -> None:
        """Synchronize and publish one entity's effective state."""
        from aioesphomeapi.api_pb2 import SubscribeHomeAssistantStatesRequest  # type: ignore[attr-defined] # pylint: disable=no-name-in-module

        self.broadcast(entity.handle_message(SubscribeHomeAssistantStatesRequest()))


def initial_stop_word_threshold(saved_sensitivity: Optional[float]) -> float:
    """
    Resolve the stop word probability cutoff to start from, clamped to 0.0-1.0.
    :param saved_sensitivity: Value persisted in preferences, or None if it has never been set.
    """
    if saved_sensitivity is None:
        return ServerState.stop_word_threshold

    return max(0.0, min(1.0, float(saved_sensitivity)))
