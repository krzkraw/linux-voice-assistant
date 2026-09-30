"""Optional, same-origin WebUI for local voice settings."""

import asyncio
import hmac
import ipaddress
import json
import logging
import secrets
import struct
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable, Optional, cast
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .models import ServerState
from .monitor import SAMPLE_RATE, MonitorBus, MonitorEvent

_LOGGER = logging.getLogger(__name__)
_COOKIE = "lva_session"
_SESSION_SECONDS = 3600
_MAX_SESSIONS = 64
_MAX_LOGIN_PEERS = 1024
_AUDIO_HEADER = struct.Struct("!4sBBIQII")


@dataclass
class _Client:
    socket: web.WebSocketResponse
    queue: "asyncio.Queue[tuple[Optional[int], list[tuple[str, object]]]]" = field(default_factory=lambda: asyncio.Queue(maxsize=8))
    sender: Optional[asyncio.Task] = None
    monitoring: bool = False
    detector_active: Optional[bool] = None
    start_sample: int = 0
    overflows: deque[float] = field(default_factory=deque)


class WebUI:
    """Serve the optional local settings UI."""

    def __init__(self, state: ServerState, host: str, port: int, password_file: Optional[Path], bypass_cidrs: str = "") -> None:
        self.state = state
        self.host = host
        self.port = port
        if not 1 <= port <= 65535:
            raise ValueError("WEB_UI_PORT must be between 1 and 65535")
        try:
            self.networks = [ipaddress.ip_network(item.strip(), strict=True) for item in bypass_cidrs.split(",") if item.strip()]
        except ValueError as exc:
            raise ValueError("WEB_UI_AUTH_BYPASS_CIDRS contains an invalid CIDR") from exc
        if password_file is None and not self.networks:
            raise ValueError("WEB_UI_PASSWORD_FILE or WEB_UI_AUTH_BYPASS_CIDRS is required when WebUI is enabled")
        self.password: Optional[bytes] = None
        if password_file is not None:
            try:
                if password_file.stat().st_mode & 0o077:
                    raise ValueError("WEB_UI_PASSWORD_FILE must be readable only by its owner")
                self.password = password_file.read_bytes().rstrip(b"\r\n")
            except OSError as exc:
                raise ValueError("WEB_UI_PASSWORD_FILE cannot be read") from exc
            if not self.password or len(self.password) > 1024:
                raise ValueError("WEB_UI_PASSWORD_FILE must contain 1 to 1024 bytes")
        self.sessions: dict[str, float] = {}
        self.login_attempts: dict[str, list[float]] = {}
        self.sockets: dict[web.WebSocketResponse, tuple[Optional[str], bool]] = {}
        self.clients: dict[web.WebSocketResponse, _Client] = {}
        self.socket_reservations = 0
        self.runner: Optional[web.AppRunner] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.broadcast_pending = False
        self.broadcast_dirty = False
        self.monitor: Optional[MonitorBus] = None
        self.monitor_settings: Optional[tuple[object, ...]] = None
        self.app = web.Application(client_max_size=4096, middlewares=[self._guard])
        self.app.add_routes(
            [
                web.get("/", self._asset),
                web.get("/style.css", self._asset),
                web.get("/app.js", self._asset),
                web.get("/monitor.js", self._asset),
                web.post("/api/login", self._login),
                web.post("/api/logout", self._logout),
                web.get("/api/state", self._state),
                web.post("/api/settings", self._settings),
                web.get("/api/ws", self._socket),
            ]
        )

    async def start(self) -> None:
        """Bind the server before attaching state notifications."""
        runner = web.AppRunner(self.app, access_log=None)
        try:
            await runner.setup()
            await web.TCPSite(runner, self.host, self.port).start()
        except Exception:
            await runner.cleanup()
            raise
        self.runner = runner
        self.loop = asyncio.get_running_loop()
        self.monitor = MonitorBus(self.loop, self._deliver_monitor)
        self.state.monitor_bus = self.monitor
        self.monitor_settings = self._monitor_settings()
        self.state.settings_changed = self._changed
        _LOGGER.info("WebUI listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Close sessions and the listener."""
        self.state.settings_changed = None
        self.state.monitor_bus = None
        for socket in list(self.sockets):
            await socket.close()
        for client in self.clients.values():
            if client.sender is not None:
                client.sender.cancel()
        self.clients.clear()
        self.sockets.clear()
        self.sessions.clear()
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None

    @web.middleware
    async def _guard(self, request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
        if not self._valid_host(request):
            raise web.HTTPForbidden(text="Invalid Host")
        if request.method != "GET" or request.path == "/api/ws":
            if request.headers.get("Origin") != self._origin(request):
                raise web.HTTPForbidden(text="Invalid Origin")
        if request.path in ("/api/logout", "/api/state", "/api/settings", "/api/ws") and not self._authorized(request):
            raise web.HTTPUnauthorized(text="Login required")
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = "default-src 'self'; connect-src 'self' ws: wss:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    async def _asset(self, request: web.Request) -> web.Response:
        name = "index.html" if request.path == "/" else request.path.lstrip("/")
        content_type = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript", "monitor.js": "text/javascript"}[name]
        return web.Response(body=files("linux_voice_assistant").joinpath("web_assets", name).read_bytes(), content_type=content_type)

    async def _login(self, request: web.Request) -> web.Response:
        peer = request.remote or ""
        now = time.monotonic()
        self.login_attempts = {ip: [stamp for stamp in stamps if now - stamp < 60] for ip, stamps in self.login_attempts.items() if any(now - stamp < 60 for stamp in stamps)}
        if peer not in self.login_attempts and len(self.login_attempts) >= _MAX_LOGIN_PEERS:
            raise web.HTTPTooManyRequests(text="Login temporarily unavailable")
        attempts = self.login_attempts.setdefault(peer, [])
        if len(attempts) >= 5:
            raise web.HTTPTooManyRequests(text="Try again in one minute")
        attempts.append(now)
        if self.password is None:
            raise web.HTTPForbidden(text="Password login is disabled")
        try:
            body = await asyncio.wait_for(request.json(), timeout=5)
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text="Request body timed out") from None
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(body, dict) or not isinstance(body.get("password"), str):
            raise web.HTTPBadRequest(text="Password required")
        if not hmac.compare_digest(body["password"].encode(), self.password):
            raise web.HTTPUnauthorized(text="Invalid password")
        self._expire_sessions()
        if len(self.sessions) >= _MAX_SESSIONS:
            raise web.HTTPTooManyRequests(text="Session limit reached")
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + _SESSION_SECONDS
        response = web.json_response(self._snapshot())
        response.set_cookie(_COOKIE, token, max_age=_SESSION_SECONDS, httponly=True, samesite="Strict", secure=request.secure, path="/")
        return response

    async def _logout(self, request: web.Request) -> web.Response:
        token = request.cookies.get(_COOKIE)
        if token is not None:
            self.sessions.pop(token, None)
            for socket, (socket_token, _) in list(self.sockets.items()):
                if socket_token == token:
                    await socket.close(code=1008, message=b"Logged out")
        response = web.json_response({"ok": True})
        response.del_cookie(_COOKIE, path="/")
        return response

    async def _state(self, request: web.Request) -> web.Response:
        return web.json_response(self._snapshot())

    async def _settings(self, request: web.Request) -> web.Response:
        try:
            body = await asyncio.wait_for(request.json(), timeout=5)
        except asyncio.TimeoutError:
            raise web.HTTPRequestTimeout(text="Request body timed out") from None
        except (json.JSONDecodeError, ValueError):
            raise web.HTTPBadRequest(text="Invalid JSON") from None
        if not isinstance(body, dict) or set(body) != {"name", "value"} or not isinstance(body["name"], str):
            raise web.HTTPBadRequest(text="Expected name and value")
        try:
            if body["name"] == "primary_model":
                if not isinstance(body["value"], str):
                    raise ValueError("Invalid model")
                self.state.select_primary_wake_word(body["value"])
                self._publish_wake_configuration()
            elif body["name"] == "muted":
                if not isinstance(body["value"], bool):
                    raise ValueError("Invalid mute value")
                if self.state.satellite is not None:
                    self.state.satellite._set_muted(body["value"])  # pylint: disable=protected-access
                else:
                    self.state.muted = body["value"]
                    if self.state.mute_switch_entity is not None:
                        self.state.broadcast_entity_state(self.state.mute_switch_entity)
                    self.state.notify_settings_changed()
            else:
                self.state.update_setting(body["name"], body["value"])
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc
        except OSError as exc:
            _LOGGER.error("WebUI settings persistence failed: %s", exc)
            raise web.HTTPInternalServerError(text="Could not save settings") from exc
        return web.json_response(self._snapshot())

    async def _socket(self, request: web.Request) -> web.WebSocketResponse:
        if len(self.sockets) + self.socket_reservations >= 16:
            raise web.HTTPTooManyRequests(text="WebSocket limit reached")
        self.socket_reservations += 1
        socket = web.WebSocketResponse(max_msg_size=2048, heartbeat=30)
        try:
            await socket.prepare(request)
            await asyncio.wait_for(socket.send_json(self._snapshot()), timeout=1)
            token = request.cookies.get(_COOKIE)
            self.sockets[socket] = (token, self._bypass(request.remote))
            client = _Client(socket)
            self.clients[socket] = client
            client.sender = asyncio.create_task(self._send_client(client))
        except Exception:
            with suppress(Exception):
                await socket.close()
            raise
        finally:
            self.socket_reservations -= 1
        try:
            while not socket.closed:
                try:
                    message = await socket.receive(timeout=5)
                except asyncio.TimeoutError:
                    if not self._authorized(request):
                        break
                    continue
                if not self._authorized(request):
                    break
                if message.type in (WSMsgType.PING, WSMsgType.PONG):
                    continue
                if message.type != WSMsgType.TEXT:
                    break
                try:
                    command = json.loads(message.data)
                except (json.JSONDecodeError, ValueError):
                    break
                if command == {"command": "monitor_start"}:
                    self._start_monitor(client)
                elif command == {"command": "monitor_stop"}:
                    self._stop_monitor(client)
                else:
                    break
        finally:
            if client.monitoring:
                self._stop_monitor(client, acknowledge=False)
            if client.sender is not None:
                client.sender.cancel()
            self.clients.pop(socket, None)
            self.sockets.pop(socket, None)
            await socket.close()
        return socket

    def _snapshot(self) -> dict[str, object]:
        slots = (self.state.preferences.active_wake_words + [None, None])[:2]
        primary = slots[0] if self.state.preferences.active_wake_words else next((word for word in self.state.wake_words if word in self.state.active_wake_words), None)
        return {
            "revision": self.state.settings_revision,
            "mic_volume": self.state.mic_volume,
            "mic_auto_gain": self.state.mic_auto_gain,
            "mic_noise_suppression": self.state.mic_noise_suppression,
            "primary_model": primary,
            "primary_threshold": self.state.wake_word_1_threshold,
            "muted": self.state.muted,
            "connected": self.state.connected,
            "models": [{"id": word.id, "name": word.wake_word} for word in self.state.available_wake_words.values()],
        }

    def _changed(self) -> None:
        if self.loop is not None:
            try:
                settings = self._monitor_settings()
                if self.monitor is not None and self.monitor_settings is not None and settings != self.monitor_settings:
                    reason = "mute" if settings[0] != self.monitor_settings[0] else "model" if settings[1] != self.monitor_settings[1] else "processor"
                    epoch = self.monitor.reset()
                    self.loop.call_soon_threadsafe(self._reset_monitor_clients, epoch, reason)
                self.monitor_settings = settings
                self.loop.call_soon_threadsafe(self._schedule_broadcast)
            except RuntimeError:
                pass

    def _schedule_broadcast(self) -> None:
        if self.broadcast_pending:
            self.broadcast_dirty = True
        else:
            self.broadcast_pending = True
            asyncio.create_task(self._broadcast())

    async def _broadcast(self) -> None:
        try:
            self.broadcast_dirty = False
            snapshot = self._snapshot()
            for socket, (token, bypassed) in list(self.sockets.items()):
                if socket.closed or (not bypassed and (token is None or self.sessions.get(token, 0) <= time.monotonic())):
                    await socket.close(code=1008, message=b"Session expired")
                    continue
                self._queue_client(self.clients[socket], [("json", snapshot)])
        finally:
            self.broadcast_pending = False
            if self.broadcast_dirty:
                self._schedule_broadcast()

    async def _send_client(self, client: _Client) -> None:
        try:
            while not client.socket.closed:
                epoch, bundle = await client.queue.get()
                for kind, payload in bundle:
                    if epoch is not None and (not client.monitoring or self.monitor is None or epoch != self.monitor.epoch):
                        break
                    if kind == "bytes":
                        await asyncio.wait_for(client.socket.send_bytes(cast(bytes, payload)), timeout=1)
                    else:
                        await asyncio.wait_for(client.socket.send_json(payload), timeout=1)
        except (ConnectionError, asyncio.TimeoutError, RuntimeError):
            await client.socket.close()

    def _queue_client(self, client: _Client, bundle: list[tuple[str, object]], epoch: Optional[int] = None) -> None:
        if client.socket.closed:
            return
        if client.queue.full():
            while not client.queue.empty():
                client.queue.get_nowait()
            now = time.monotonic()
            client.overflows.append(now)
            while client.overflows and now - client.overflows[0] > 10:
                client.overflows.popleft()
            if len(client.overflows) >= 3:
                asyncio.create_task(client.socket.close(code=1013, message=b"Slow monitor client"))
                return
            gap = {"type": "gap", "epoch": self.monitor.epoch if self.monitor is not None else 0, "reason": "slow_client"}
            mandatory = bundle if epoch is None else []
            client.queue.put_nowait((None, [("json", gap), ("json", self._snapshot()), *mandatory]))
            return
        client.queue.put_nowait((epoch, bundle))

    def _start_monitor(self, client: _Client) -> None:
        assert self.monitor is not None
        if not client.monitoring:
            epoch, start = self.monitor.start()
            client.monitoring = True
            client.detector_active = None
        else:
            epoch, start = self.monitor.epoch, self.monitor.last_sample_end
        client.start_sample = start
        self._queue_client(
            client,
            [
                (
                    "json",
                    {
                        "type": "monitor",
                        "active": True,
                        "stream_id": self.monitor.stream_id,
                        "epoch": epoch,
                        "start_sample": start,
                        "sample_rate": SAMPLE_RATE,
                        "input_format": "float32le",
                        "processed_format": "s16le",
                        "score_position": "available_after_processed_block",
                    },
                )
            ],
        )

    def _stop_monitor(self, client: _Client, acknowledge: bool = True) -> None:
        if client.monitoring and self.monitor is not None:
            self.monitor.stop()
            client.monitoring = False
        while not client.queue.empty():
            client.queue.get_nowait()
        if acknowledge:
            self._queue_client(client, [("json", {"type": "monitor", "active": False})])

    def _reset_monitor_clients(self, epoch: int, reason: str) -> None:
        for client in self.clients.values():
            if not client.monitoring:
                continue
            while not client.queue.empty():
                client.queue.get_nowait()
            self._start_monitor(client)
            self._queue_client(client, [("json", {"type": "reset", "epoch": epoch, "reason": reason}), ("json", self._snapshot())])

    def _deliver_monitor(self, events: list[MonitorEvent], dropped: Optional[tuple[int, int, int]]) -> None:
        if self.monitor is None:
            return
        ordered: list[tuple[int, int, object]] = [(event.start, 1, event) for event in events]
        if dropped is not None and dropped[0] == self.monitor.epoch:
            ordered.append((dropped[1], 0, dropped))
        for _, kind, item in sorted(ordered, key=lambda entry: (entry[0], entry[1])):
            if kind == 0:
                gap = cast(tuple[int, int, int], item)
                for client in self.clients.values():
                    if client.monitoring and gap[2] > client.start_sample:
                        self._queue_client(client, [("json", {"type": "gap", "epoch": gap[0], "from_sample": max(gap[1], client.start_sample), "to_sample": gap[2], "reason": "thread_queue"})])
                continue
            event = cast(MonitorEvent, item)
            if event.epoch != self.monitor.epoch:
                continue
            for client in self.clients.values():
                if client.monitoring and event.end > client.start_sample:
                    if client.detector_active != event.detector_active:
                        self._queue_client(client, [("json", {"type": "detector", "active": event.detector_active})])
                        client.detector_active = event.detector_active
                    floor = client.start_sample
                    bundle: list[tuple[str, object]] = []
                    input_start = max(event.start, floor)
                    input_data = event.input_bytes[(input_start - event.start) * 4 :]
                    if input_data:
                        input_header = _AUDIO_HEADER.pack(b"LVA1", 1, int(input_start > event.start), event.epoch, input_start, len(input_data) // 4, event.revision)
                        bundle.append(("bytes", input_header + input_data))
                    for start, data, discontinuity in event.processed:
                        visible_start = max(start, floor)
                        visible = data[(visible_start - start) * 2 :]
                        if visible:
                            header = _AUDIO_HEADER.pack(b"LVA1", 2, int(discontinuity or visible_start > start), event.epoch, visible_start, len(visible) // 2, event.revision)
                            bundle.append(("bytes", header + visible))
                    scores = [score for score in event.scores if score["at_sample"] > floor]
                    if scores:
                        bundle.append(("json", {"type": "scores", "epoch": event.epoch, "revision": event.revision, "position_kind": "available_after_processed_block", "items": scores}))
                    if bundle:
                        self._queue_client(client, bundle, event.epoch)

    def _monitor_settings(self) -> tuple[object, ...]:
        slots = self.state.preferences.active_wake_words
        return (self.state.muted, slots[0] if slots else None, self.state.mic_auto_gain, self.state.mic_noise_suppression, self.state.mic_volume)

    def _authorized(self, request: web.Request) -> bool:
        self._expire_sessions()
        if self._bypass(request.remote):
            return True
        token = request.cookies.get(_COOKIE)
        return token is not None and token in self.sessions

    def _expire_sessions(self) -> None:
        now = time.monotonic()
        self.sessions = {token: expiry for token, expiry in self.sessions.items() if expiry > now}

    def _bypass(self, peer: Optional[str]) -> bool:
        try:
            address = ipaddress.ip_address(peer or "")
        except ValueError:
            return False
        return any(address in network for network in self.networks)

    def _valid_host(self, request: web.Request) -> bool:
        raw = request.headers.get("Host", "")
        if not raw or "/" in raw or "@" in raw or "\\" in raw:
            return False
        try:
            parsed = urlsplit("//" + raw)
            hostname = parsed.hostname
            if not hostname or parsed.path or parsed.query or parsed.fragment:
                return False
            if hostname == "localhost":
                return parsed.port is not None
            address = ipaddress.ip_address(hostname)
            return parsed.port is not None and (address.is_loopback or (parsed.port == self.port and (self.host in ("0.0.0.0", "::") or address == ipaddress.ip_address(self.host))))
        except ValueError:
            return False

    def _publish_wake_configuration(self) -> None:
        from aioesphomeapi.api_pb2 import VoiceAssistantConfigurationResponse, VoiceAssistantWakeWord  # type: ignore[attr-defined] # pylint: disable=no-name-in-module

        available = [VoiceAssistantWakeWord(id=word.id, wake_word=word.wake_word, trained_languages=word.trained_languages) for word in self.state.available_wake_words.values()]
        active = [word for word in self.state.preferences.active_wake_words if word in self.state.active_wake_words]
        self.state.broadcast([VoiceAssistantConfigurationResponse(available_wake_words=available, active_wake_words=active, max_active_wake_words=2)])

    @staticmethod
    def _origin(request: web.Request) -> str:
        return f"{request.scheme}://{request.headers['Host']}"
