"""WebUI authentication, settings, and distribution checks."""

import asyncio
import json
import socket
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
from aiohttp import ClientSession, CookieJar, web

from linux_voice_assistant.web_ui import WebUI
from tests.unit.conftest import make_satellite, make_state


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def password_file(tmp_path):
    path = tmp_path / "password"
    path.write_text("secret\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def model(model_id):
    available = MagicMock(id=model_id, wake_word=model_id, trained_languages=[], probability_cutoff=0.7)
    available.load.return_value = SimpleNamespace(id=model_id)
    return available


@pytest.mark.asyncio
async def test_auth_origin_session_logout_and_socket(tmp_path):
    state = make_state(tmp_path)
    port = free_port()
    server = WebUI(state, "127.0.0.1", port, password_file(tmp_path))
    await server.start()
    origin = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            assert (await client.get(f"{origin}/")).status == 200
            assert (await client.get(f"{origin}/api/state")).status == 401
            assert (await client.post(f"{origin}/api/login", json={"password": "secret"})).status == 403
            assert (await client.post(f"{origin}/api/login", json={"password": "secret"}, headers={"Origin": "http://evil.test"})).status == 403
            assert (await client.post(f"{origin}/api/login", json={"password": "wrong"}, headers={"Origin": origin})).status == 401
            response = await client.post(f"{origin}/api/login", json={"password": "secret"}, headers={"Origin": origin})
            assert response.status == 200
            cookie = response.cookies["lva_session"]
            assert cookie["httponly"] and cookie["samesite"] == "Strict" and not cookie["secure"]
            assert (await client.get(f"{origin}/api/state")).status == 200
            assert (await client.post(f"{origin}/api/settings", json={"name": "mic_volume", "value": 52}, headers={"Origin": "http://evil.test"})).status == 403
            ws = await client.ws_connect(f"{origin}/api/ws", headers={"Origin": origin})
            assert (await ws.receive_json())["mic_volume"] == 100
            assert (await client.post(f"{origin}/api/logout", headers={"Origin": origin})).status == 200
            assert (await client.get(f"{origin}/api/state")).status == 401
            assert ws.closed or (await ws.receive()).type.name in ("CLOSE", "CLOSED")
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_expiry_rate_limit_forwarded_and_bypass(tmp_path):
    state = make_state(tmp_path)
    port = free_port()
    server = WebUI(state, "127.0.0.1", port, password_file(tmp_path), "192.0.2.1/32")
    await server.start()
    origin = f"http://127.0.0.1:{port}"
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            assert (await client.get(f"{origin}/api/state", headers={"X-Forwarded-For": "192.0.2.1", "Forwarded": "for=192.0.2.1"})).status == 401
            assert (await client.get(f"{origin}/api/state", headers={"Host": "evil.test"})).status == 403
            assert (await client.get(f"{origin}/api/state", headers={"Host": "127.0.0.1:43123"})).status == 401
            assert (await client.post(f"{origin}/api/login", json={"password": "secret"}, headers={"Origin": origin})).status == 200
            token = next(iter(server.sessions))
            server.sessions[token] = time.monotonic() - 1
            assert (await client.get(f"{origin}/api/state")).status == 401
            for _ in range(4):
                await client.post(f"{origin}/api/login", json={"password": "wrong"}, headers={"Origin": origin})
            assert (await client.post(f"{origin}/api/login", json={"password": "secret"}, headers={"Origin": origin})).status == 429
    finally:
        await server.stop()
    bypass = WebUI(state, "127.0.0.1", free_port(), None, "127.0.0.1/32")
    await bypass.start()
    try:
        async with ClientSession() as client:
            assert (await client.get(f"http://127.0.0.1:{bypass.port}/api/state")).status == 200
    finally:
        await bypass.stop()


@pytest.mark.asyncio
async def test_settings_validation_persistence_and_broadcast(tmp_path):
    state = make_state(tmp_path)
    state.available_wake_words = {name: model(name) for name in ("first", "second", "third")}
    state.available_wake_words["third"].probability_cutoff = 0.43
    state.wake_words = {name: SimpleNamespace(id=name) for name in ("first", "second")}
    state.preferences.active_wake_words = ["first", "second"]
    state.active_wake_words = {"first", "second", "stop"}
    server = WebUI(state, "127.0.0.1", free_port(), None, "127.0.0.1/32")
    await server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        async with ClientSession() as client:
            headers = {"Origin": origin}
            for value in (float("nan"), 101, "50", True, 1.5):
                assert (await client.post(f"{origin}/api/settings", json={"name": "mic_volume", "value": value}, headers=headers)).status == 400
            assert (await client.post(f"{origin}/api/settings", data="{bad", headers=headers)).status == 400
            assert (await client.post(f"{origin}/api/settings", json={"name": "mic_volume", "value": 52}, headers=headers)).status == 200
            assert state.mic_volume == state.preferences.mic_volume == 52
            assert json.loads(state.preferences_path.read_text())["mic_volume"] == 52
            assert (await client.post(f"{origin}/api/settings", json={"name": "primary_model", "value": "second"}, headers=headers)).status == 400
            assert (await client.post(f"{origin}/api/settings", json={"name": "primary_model", "value": "third"}, headers=headers)).status == 200
            assert state.preferences.active_wake_words == ["third", "second"]
            assert state.active_wake_words == {"third", "second", "stop"}
            assert state.wake_words_changed
            assert state.wake_word_1_threshold == 0.43
            assert (await client.post(f"{origin}/api/settings", json={"name": "wake_word_1_threshold", "value": 0.42}, headers=headers)).status == 200
            assert state.wake_word_1_threshold == state.preferences.wake_word_1_sensitivity == 0.42
            old = state.preferences_path.read_bytes()
            with patch.object(state, "save_preferences", side_effect=OSError("disk full")):
                assert (await client.post(f"{origin}/api/settings", json={"name": "mic_volume", "value": 60}, headers=headers)).status == 500
            assert state.mic_volume == state.preferences.mic_volume == 52
            assert state.preferences_path.read_bytes() == old
            old_model = state.preferences.active_wake_words[:]
            state.wake_words_changed = False
            with patch.object(state, "save_preferences", side_effect=OSError("disk full")):
                assert (await client.post(f"{origin}/api/settings", json={"name": "primary_model", "value": "first"}, headers=headers)).status == 500
            assert state.preferences.active_wake_words == old_model
            assert state.active_wake_words == {"third", "second", "stop"}
            assert state.wake_words_changed is False
            state.preferences.active_wake_words = [None, "second"]
            assert server._snapshot()["primary_model"] is None
    finally:
        await server.stop()


def test_disabled_defaults_and_assets(tmp_path):
    from importlib.resources import files

    from linux_voice_assistant.__main__ import main

    assert callable(main)
    assert Path("docker-entrypoint.sh").read_text().find("WEB_UI_ENABLED:-0") >= 0
    assert files("linux_voice_assistant").joinpath("web_assets", "index.html").read_text().startswith("<!doctype html>")
    with pytest.raises(ValueError, match="WEB_UI_PASSWORD_FILE"):
        WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None)
    with pytest.raises(ValueError, match="invalid CIDR"):
        WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "not-an-ip")


def test_ha_and_webui_share_model_commit_and_entity_state(tmp_path):
    from aioesphomeapi.api_pb2 import NumberStateResponse, VoiceAssistantSetConfiguration

    available = {name: model(name) for name in ("first", "second", "third")}
    state_overrides = {
        "available_wake_words": available,
        "wake_words": {name: SimpleNamespace(id=name) for name in ("first", "second")},
        "active_wake_words": {"first", "second", "stop"},
    }
    sat = make_satellite(tmp_path, state_overrides=state_overrides)
    state = sat.state
    state.preferences.active_wake_words = ["first", "second"]
    peer = MagicMock()
    state.connections.append(peer)
    state.update_setting("mic_volume", 48)
    assert isinstance(peer.send_messages.call_args.args[0][0], NumberStateResponse)
    assert peer.send_messages.call_args.args[0][0].state == 48
    list(sat.handle_message(VoiceAssistantSetConfiguration(active_wake_words=["third", "second"])))
    assert state.preferences.active_wake_words == ["third", "second"]
    assert state.active_wake_words == {"third", "second", "stop"}
    assert json.loads(state.preferences_path.read_text())["active_wake_words"] == ["third", "second"]
    server = WebUI(state, "127.0.0.1", free_port(), None, "127.0.0.1/32")
    assert server._snapshot()["primary_model"] == "third"
    with patch.object(state, "save_preferences", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            list(sat.handle_message(VoiceAssistantSetConfiguration(active_wake_words=["first", "second"])))
    assert state.preferences.active_wake_words == ["third", "second"]
    assert state.active_wake_words == {"third", "second", "stop"}


@pytest.mark.asyncio
async def test_slow_login_body_times_out(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), password_file(tmp_path))
    await server.start()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(f"POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:{server.port}\r\nOrigin: http://127.0.0.1:{server.port}\r\nContent-Type: application/json\r\nContent-Length: 10\r\n\r\n".encode())
        await writer.drain()
        assert b"408 Request Timeout" in await asyncio.wait_for(reader.readline(), timeout=7)
    finally:
        writer.close()
        await writer.wait_closed()
        await server.stop()


@pytest.mark.asyncio
async def test_socket_reservations_and_initial_send_cleanup(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "127.0.0.1/32")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_prepare(_socket, _request):
        entered.set()
        await release.wait()
        raise ConnectionResetError("handshake failed")

    with patch.object(web.WebSocketResponse, "prepare", slow_prepare):
        tasks = [asyncio.create_task(server._socket(MagicMock())) for _ in range(16)]
        await entered.wait()
        await asyncio.sleep(0)
        assert server.socket_reservations == 16
        with pytest.raises(web.HTTPTooManyRequests):
            await server._socket(MagicMock())
        release.set()
        assert all(isinstance(result, ConnectionResetError) for result in await asyncio.gather(*tasks, return_exceptions=True))
        assert server.socket_reservations == 0

    request = MagicMock(cookies={}, remote="127.0.0.1")
    with (
        patch.object(web.WebSocketResponse, "prepare", new_callable=AsyncMock),
        patch.object(web.WebSocketResponse, "send_json", new_callable=AsyncMock, side_effect=ConnectionResetError("initial send failed")),
        patch.object(web.WebSocketResponse, "close", new_callable=AsyncMock),
    ):
        with pytest.raises(ConnectionResetError, match="initial send failed"):
            await server._socket(request)
    assert server.socket_reservations == 0
    assert not server.sockets


@pytest.mark.parametrize("slots,active,expected", [([None, "second"], {"second"}, (None, 0.21)), (["third", "second"], {"third", "second"}, (0.63, 0.21))])
def test_audio_loop_uses_explicit_wake_slots(tmp_path, slots, active, expected):
    from linux_voice_assistant.__main__ import process_audio

    class FakeMicro:
        def __init__(self, model_id):
            self.id = model_id
            self.probability_cutoff = None

        def process_streaming(self, _features):
            return False

    models = {model_id: FakeMicro(model_id) for model_id in ("first", "second", "third")}
    available = {model_id: model(model_id) for model_id in ("second", "third")}
    available["third"].probability_cutoff = 0.63
    state = make_state(tmp_path, audio_input_channels=1, wake_words=models, available_wake_words=available, active_wake_words=active)
    state.preferences.active_wake_words = slots
    state.preferences.wake_word_2_sensitivity = 0.21
    state.wake_word_1_threshold = 0.63
    state.wake_word_2_threshold = 0.21
    state.satellite = MagicMock(state=state, _is_streaming_audio=False)
    mic = MagicMock()
    mic.recorder.return_value.__enter__.return_value.record.side_effect = [np.zeros((320, 1), dtype=np.float32), RuntimeError("finished")]
    features = MagicMock()
    features.process_streaming.return_value = [object()]
    with patch("linux_voice_assistant.__main__.MicroWakeWord", FakeMicro), patch("linux_voice_assistant.__main__.MicroWakeWordFeatures", return_value=features):
        with pytest.raises(SystemExit):
            process_audio(state, mic, 320)
    assert models["second"].probability_cutoff == expected[1]
    if expected[0] is None:
        assert models["first"].probability_cutoff is None
        assert models["third"].probability_cutoff is None
    else:
        assert models["third"].probability_cutoff == expected[0]
        assert models["first"].probability_cutoff is None
