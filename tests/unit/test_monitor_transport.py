"""Check authenticated monitor transport and stream controls."""

# pylint: disable=consider-using-with,duplicate-code,too-many-locals

import asyncio
import socket
import struct
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientSession, CookieJar, WSMsgType

from linux_voice_assistant.monitor import MonitorBus, MonitorEvent
from linux_voice_assistant.web_ui import WebUI, _Client
from tests.unit.conftest import make_state
from tests.unit.test_web_ui import password_file


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


@pytest.mark.asyncio
async def test_monitor_binary_scores_reset_and_stop(tmp_path):
    state = make_state(tmp_path)
    server = WebUI(state, "127.0.0.1", free_port(), None, "127.0.0.1/32")
    await server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        async with ClientSession() as client:
            ws = await client.ws_connect(f"{origin}/api/ws", headers={"Origin": origin})
            await ws.receive_json()
            assert server.monitor is not None
            assert not server.monitor.active
            await ws.send_json({"command": "monitor_start"})
            start = await ws.receive_json()
            assert start["type"] == "monitor" and start["active"] is True
            assert start["sample_rate"] == 16000
            epoch = start["epoch"]
            raw = struct.pack("<ff", 0.25, -0.5)
            pcm = struct.pack("<hh", 8191, -16383)
            score = {"model": "primary", "at_sample": 2, "probability": 0.8, "threshold": 0.7, "crossing": True, "accepted": False}
            server.monitor.offer(MonitorEvent(epoch, 0, 2, 4, raw, [(0, pcm, False)], [score], False))
            assert (await ws.receive_json()) == {"type": "detector", "active": False}
            for feed, payload in ((1, raw), (2, pcm)):
                message = await asyncio.wait_for(ws.receive(), 1)
                assert message.type == WSMsgType.BINARY
                assert struct.unpack("!4sBBIQII", message.data[:26]) == (b"LVA1", feed, 0, epoch, 0, 2, 4)
                assert message.data[26:] == payload
            scores = await ws.receive_json()
            assert scores["items"] == [score]
            assert scores["position_kind"] == "available_after_processed_block"

            state.muted = True
            state.notify_settings_changed()
            reset = await ws.receive_json()
            assert reset["type"] == "reset" and reset["epoch"] > epoch and reset["reason"] == "mute"
            reset_start = await ws.receive_json()
            assert reset_start["type"] == "monitor" and reset_start["epoch"] == reset["epoch"]
            assert (await ws.receive_json())["muted"] is True
            await ws.send_json({"command": "monitor_stop"})
            while (message := await ws.receive_json()).get("type") != "monitor":
                assert message["muted"] is True
            assert message == {"type": "monitor", "active": False}
            assert not server.monitor.active
            assert not server.monitor.offer(MonitorEvent(reset_start["epoch"], 2, 4, 5, raw, [], [], False))
            await ws.close()
    finally:
        await server.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["logout", "expiry"])
async def test_authenticated_monitor_loses_subscription_when_session_ends(tmp_path, reason):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), password_file(tmp_path))
    await server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        async with ClientSession(cookie_jar=CookieJar(unsafe=True)) as client:
            response = await client.post(f"{origin}/api/login", json={"password": "secret"}, headers={"Origin": origin})
            assert response.status == 200
            ws = await client.ws_connect(f"{origin}/api/ws", headers={"Origin": origin})
            await ws.receive_json()
            await ws.send_json({"command": "monitor_start"})
            assert (await ws.receive_json())["active"]
            assert server.monitor is not None and server.monitor.active
            if reason == "logout":
                assert (await client.post(f"{origin}/api/logout", headers={"Origin": origin})).status == 200
            else:
                server.sessions[next(iter(server.sessions))] = time.monotonic() - 1
                await ws.send_json({"command": "monitor_start"})
            assert (await ws.receive(timeout=1)).type in (WSMsgType.CLOSE, WSMsgType.CLOSED)
            await ws.close()
            assert not server.monitor.active
            assert not server.clients
            assert server.monitor.capture(0, 1024) is None
    finally:
        await server.stop()


def test_late_subscriber_excludes_queued_and_overlapping_audio(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "127.0.0.1/32")
    server.monitor = MonitorBus(MagicMock(), server._deliver_monitor)
    first_socket = MagicMock(closed=False)
    second_socket = MagicMock(closed=False)
    first = _Client(first_socket)
    second = _Client(second_socket)
    server.clients = {first_socket: first, second_socket: second}
    server._start_monitor(first)
    assert server.monitor is not None
    epoch = server.monitor.epoch
    server.monitor.observe(100)
    assert server.monitor.offer(MonitorEvent(epoch, 0, 100, 0, b"\x00" * 400, [(0, b"\x00" * 200, False)], [{"at_sample": 100}], True))
    server._start_monitor(second)
    assert second.start_sample == 100
    server.monitor._drain()
    assert second.queue.qsize() == 1
    assert first.queue.qsize() == 3

    server._deliver_monitor([MonitorEvent(epoch, 50, 150, 0, b"\x01" * 400, [(80, b"\x02" * 140, False)], [{"at_sample": 90}, {"at_sample": 150}], True)], None)
    _, detector = second.queue.get_nowait()
    assert detector == [
        (
            "json",
            {
                "type": "monitor",
                "active": True,
                "stream_id": server.monitor.stream_id,
                "epoch": epoch,
                "start_sample": 100,
                "sample_rate": 16000,
                "input_format": "float32le",
                "processed_format": "s16le",
                "score_position": "available_after_processed_block",
            },
        )
    ]
    _, detector = second.queue.get_nowait()
    assert detector == [("json", {"type": "detector", "active": True})]
    _, bundle = second.queue.get_nowait()
    assert [kind for kind, _ in bundle] == ["bytes", "bytes", "json"]
    assert struct.unpack("!4sBBIQII", bundle[0][1][:26]) == (b"LVA1", 1, 1, epoch, 100, 50, 0)
    assert bundle[0][1][26:] == b"\x01" * 200
    assert struct.unpack("!4sBBIQII", bundle[1][1][:26]) == (b"LVA1", 2, 1, epoch, 100, 50, 0)
    assert bundle[1][1][26:] == b"\x02" * 100
    assert bundle[2][1]["items"] == [{"at_sample": 150}]


def test_thread_gap_precedes_newer_audio(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "127.0.0.1/32")
    server.monitor = MonitorBus(MagicMock(), server._deliver_monitor)
    socket_mock = MagicMock(closed=False)
    client = _Client(socket_mock)
    server.clients = {socket_mock: client}
    server._start_monitor(client)
    bus = server.monitor
    assert bus is not None
    epoch = bus.epoch
    bus._lock.acquire()
    try:
        assert bus.capture(0, 100) is None
    finally:
        bus._lock.release()
    assert bus.capture(100, 200) == (epoch, 100)
    assert bus.offer(MonitorEvent(epoch, 100, 200, 0, b"\x00" * 400, [], [], False))
    bus._drain()
    types = [bundle[0][1].get("type") if bundle[0][0] == "json" else "audio" for _, bundle in list(client.queue._queue)]
    assert types == ["monitor", "gap", "detector", "audio"]


@pytest.mark.asyncio
async def test_slow_client_queue_keeps_control_and_closes_after_repeated_overflow(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "127.0.0.1/32")
    socket_mock = MagicMock(closed=False)
    socket_mock.close = AsyncMock()
    client = _Client(socket_mock)
    for _ in range(8):
        server._queue_client(client, [("bytes", b"audio")], 1)
    server._queue_client(client, [("json", {"type": "reset", "epoch": 2})])
    assert client.queue.qsize() == 1
    _, bundle = client.queue.get_nowait()
    assert [item[1].get("type") for item in bundle if item[0] == "json"] == ["gap", None, "reset"]
    for _ in range(2):
        for _ in range(8 - client.queue.qsize()):
            server._queue_client(client, [("bytes", b"audio")], 1)
        server._queue_client(client, [("bytes", b"audio")], 1)
    await asyncio.sleep(0)
    socket_mock.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("name,value", [("mic_volume", 80), ("mic_auto_gain", 5), ("mic_noise_suppression", 1), ("wake_word_1_threshold", 0.8)])
async def test_tuning_reset_precedes_acknowledgement_and_keeps_sample_clock(tmp_path, name, value):
    state = make_state(tmp_path)
    server = WebUI(state, "127.0.0.1", free_port(), None, "127.0.0.1/32")
    await server.start()
    origin = f"http://127.0.0.1:{server.port}"
    try:
        async with ClientSession() as client:
            ws = await client.ws_connect(f"{origin}/api/ws", headers={"Origin": origin})
            await ws.receive_json()
            await ws.send_json({"command": "monitor_start"})
            initial = await ws.receive_json()
            assert server.monitor is not None
            server.monitor.observe(1024)
            state.update_setting(name, value)
            if name == "wake_word_1_threshold":
                assert (await ws.receive_json())["primary_threshold"] == value
                assert server.monitor.epoch == initial["epoch"]
            else:
                reset = await ws.receive_json()
                assert reset == {"type": "reset", "epoch": initial["epoch"] + 1, "reason": "processor"}
                acknowledgement = await ws.receive_json()
                assert acknowledgement["type"] == "monitor" and acknowledgement["epoch"] == reset["epoch"]
                assert acknowledgement["start_sample"] == 1024
                assert (await ws.receive_json())[name] == value
            epoch = server.monitor.epoch
            stale_epoch = initial["epoch"] if name != "wake_word_1_threshold" else epoch - 1
            assert not server.monitor.offer(MonitorEvent(stale_epoch, 0, 1024, 0, b"\0" * 4096, [], [], False))
            assert server.monitor.offer(MonitorEvent(epoch, 1024, 2048, 1, b"\0" * 4096, [(1024, b"\0" * 2048, False)], [{"at_sample": 2048}], False))
            while (message := await ws.receive(timeout=1)).type != WSMsgType.BINARY:
                assert message.type == WSMsgType.TEXT
            assert struct.unpack("!4sBBIQII", message.data[:26]) == (b"LVA1", 1, 0, epoch, 1024, 1024, 1)
            await ws.close()
    finally:
        await server.stop()


def test_rapid_resets_use_current_epoch_and_preserve_pending_privacy_clear(tmp_path):
    server = WebUI(make_state(tmp_path), "127.0.0.1", free_port(), None, "127.0.0.1/32")
    server.monitor = MonitorBus(MagicMock(), server._deliver_monitor)
    socket_mock = MagicMock(closed=False)
    client = _Client(socket_mock)
    server.clients = {socket_mock: client}
    server._start_monitor(client)
    server.monitor.observe(1024)
    old_epoch = server.monitor.reset()
    epoch = server.monitor.reset()
    for reason in ("processor", "mute", "model"):
        server._reset_monitor_clients(old_epoch, reason)
        server._reset_monitor_clients(epoch, "processor")
        messages = [payload for _, bundle in list(client.queue._queue) for kind, payload in bundle if kind == "json"]
        assert [message.get("type") for message in messages] == ["reset", "monitor", None]
        assert messages[0] == {"type": "reset", "epoch": epoch, "reason": reason}
        assert messages[1]["epoch"] == epoch and messages[1]["start_sample"] == 1024
        while not client.queue.empty():
            client.queue.get_nowait()
