"""Optional, same-origin WebUI for local voice settings."""

import asyncio
import hmac
import ipaddress
import json
import logging
import secrets
import time
from importlib.resources import files
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .models import ServerState

_LOGGER = logging.getLogger(__name__)
_COOKIE = "lva_session"
_SESSION_SECONDS = 3600
_MAX_SESSIONS = 64
_MAX_LOGIN_PEERS = 1024


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
        self.socket_reservations = 0
        self.runner: Optional[web.AppRunner] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.broadcast_pending = False
        self.broadcast_dirty = False
        self.app = web.Application(client_max_size=4096, middlewares=[self._guard])
        self.app.add_routes(
            [
                web.get("/", self._asset),
                web.get("/style.css", self._asset),
                web.get("/app.js", self._asset),
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
        self.state.settings_changed = self._changed
        _LOGGER.info("WebUI listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        """Close sessions and the listener."""
        self.state.settings_changed = None
        for socket in list(self.sockets):
            await socket.close()
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
        content_type = {"index.html": "text/html", "style.css": "text/css", "app.js": "text/javascript"}[name]
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
        finally:
            self.socket_reservations -= 1
        try:
            token = request.cookies.get(_COOKIE)
            self.sockets[socket] = (token, self._bypass(request.remote))
            await asyncio.wait_for(socket.send_json(self._snapshot()), timeout=1)
            while not socket.closed:
                try:
                    message = await socket.receive(timeout=5)
                except asyncio.TimeoutError:
                    if not self._authorized(request):
                        break
                    continue
                if not self._authorized(request) or message.type not in (WSMsgType.PING, WSMsgType.PONG):
                    break
        finally:
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
            "models": [{"id": word.id, "name": word.wake_word} for word in self.state.available_wake_words.values()],
        }

    def _changed(self) -> None:
        if self.loop is not None:
            try:
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
                try:
                    await asyncio.wait_for(socket.send_json(snapshot), timeout=1)
                except (ConnectionError, asyncio.TimeoutError):
                    await socket.close()
        finally:
            self.broadcast_pending = False
            if self.broadcast_dirty:
                self._schedule_broadcast()

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
