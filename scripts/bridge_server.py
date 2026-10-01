"""Chrome Bridge Server.

A WebSocket relay between a Python client and a Chrome extension.
The extension keeps a long-lived connection (role=extension); CLI clients
open short-lived connections (role=cli) to send commands, which the server
forwards to the extension and routes results back.

Run:
    python scripts/bridge_server.py [--port 9333] [--no-watch] [--no-auth]

Features:
    - Token + Origin gate so web pages and other users can't drive your browser
    - Per-command deadlines supplied by the caller (no more fixed 90 s wall)
    - Watches extension/manifest.json and extension/background.js and triggers
      chrome.runtime.reload() on change (after one initial manual reload to
      grant the reload_self hook). Disable with --no-watch.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import sys
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import websockets
from bridge_auth import ensure_token, token_path
from bridge_failure_journal import (
    is_valid_failure_id,
    record_extension_failures,
    record_failure,
)
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger("chrome-bridge")

SERVER_VERSION = "2.2.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCH_FILES = [
    PROJECT_ROOT / "extension" / "manifest.json",
    PROJECT_ROOT / "extension" / "background.js",
]

# Must be >= the client's limit. A DOM dump or a screenshot easily clears 1 MiB,
# and the websockets default (1 MiB) closes the socket with 1009 instead of
# truncating — which used to surface as a bogus "extension disconnected".
MAX_FRAME_BYTES = 64 * 1024 * 1024

DEFAULT_COMMAND_TIMEOUT = 90.0
# The client waits DEADLINE + CLIENT_GRACE, the server waits DEADLINE, so the
# server always wins the race and the caller gets a structured TIMEOUT error
# instead of a socket read timeout mislabelled as a connection failure.
HANDSHAKE_TIMEOUT = 10.0
EXTENSION_FAILURES_PER_MINUTE = 100
ACCEPTED_FAILURE_ID_CACHE = 2048
MANUAL_RELOAD_RECONNECT_TIMEOUT = 15.0
AUTO_RELOAD_RECONNECT_ATTEMPTS = 30
AUTO_RELOAD_RECONNECT_INTERVAL = 0.5

EXTENSION_ORIGIN_PREFIX = "chrome-extension://"


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def _err(code: str, message: str, **detail: Any) -> dict[str, Any]:
    """Build the wire form of an error: {"error": {code, message, ...}}."""
    payload: dict[str, Any] = {"code": code, "message": message}
    if detail:
        payload["detail"] = detail
    return {"error": payload}


class BridgeServer:
    def __init__(self, token: str | None = None) -> None:
        self._extension_ws: ServerConnection | None = None
        self._extension_version: str | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reload_triggered = False
        self._reload_pending = False
        self._token = token
        self._failure_ingest_window = time.monotonic()
        self._failure_ingest_count = 0
        # A freshly booted host can have a monotonic clock below 60 seconds.
        # Start at negative infinity so the first rejected batch is always
        # journaled; later notices remain limited to one per minute.
        self._failure_rate_notice_at = float("-inf")
        self._accepted_failure_ids: set[str] = set()
        self._accepted_failure_order: deque[str] = deque()
        self._reload_generation = 0
        self._origin_rejection_logged_at = float("-inf")
        self._origin_rejection_suppressed = 0

    def _remember_failure_ids(self, failure_ids: list[str]) -> None:
        for failure_id in failure_ids:
            if failure_id in self._accepted_failure_ids:
                continue
            if len(self._accepted_failure_order) >= ACCEPTED_FAILURE_ID_CACHE:
                expired = self._accepted_failure_order.popleft()
                self._accepted_failure_ids.discard(expired)
            self._accepted_failure_order.append(failure_id)
            self._accepted_failure_ids.add(failure_id)

    async def _ensure_durable_extension_failure(
        self,
        wire_failure_id: Any,
        *,
        code: str,
        operation: str,
        duration_ms: float,
    ) -> str | None:
        if is_valid_failure_id(wire_failure_id) and wire_failure_id in self._accepted_failure_ids:
            return wire_failure_id
        failure_id = await asyncio.to_thread(
            record_failure,
            component="extension",
            code=code,
            operation=operation,
            phase="command",
            duration_ms=duration_ms,
            server_version=SERVER_VERSION,
            extension_version=self._extension_version,
            failure_id=wire_failure_id if is_valid_failure_id(wire_failure_id) else None,
            dedupe=False,
        )
        if not failure_id:
            return None
        self._remember_failure_ids([failure_id])
        if failure_id == wire_failure_id and self._extension_ws is not None:
            with contextlib.suppress(Exception):
                await self._extension_ws.send(
                    json.dumps(
                        {
                            "type": "failure_ack",
                            "event_ids": [failure_id],
                        }
                    )
                )
        return failure_id

    async def _origin_rejection_error(self, reason: str) -> dict[str, Any]:
        """Persist at most one web-origin denial per minute without blocking I/O."""
        payload = _err("UNAUTHORIZED", reason)
        now = time.monotonic()
        if now - self._origin_rejection_logged_at < 60:
            self._origin_rejection_suppressed += 1
            return payload
        count = self._origin_rejection_suppressed + 1
        self._origin_rejection_suppressed = 0
        self._origin_rejection_logged_at = now
        logger.warning("refusing WebSocket from a web origin (%d attempt(s))", count)
        failure_id = await asyncio.to_thread(
            record_failure,
            component="server",
            code="UNAUTHORIZED",
            operation="handshake",
            phase="origin",
            server_version=SERVER_VERSION,
            retryable=False,
            count=count,
            dedupe=False,
        )
        if failure_id:
            payload["error"]["failure_id"] = failure_id
        return payload

    def _extension_failure_budget(self, requested: int) -> int:
        """Bound unauthenticated extension-role diagnostics per relay process."""
        now = time.monotonic()
        if now - self._failure_ingest_window >= 60:
            self._failure_ingest_window = now
            self._failure_ingest_count = 0
        accepted = min(
            max(0, requested),
            max(0, EXTENSION_FAILURES_PER_MINUTE - self._failure_ingest_count),
        )
        self._failure_ingest_count += accepted
        if accepted < requested and now - self._failure_rate_notice_at >= 60:
            self._failure_rate_notice_at = now
            record_failure(
                component="server",
                code="FAILURE_BATCH_RATE_LIMITED",
                operation="extension_connection",
                phase="receive",
                server_version=SERVER_VERSION,
                extension_version=self._extension_version,
                retryable=True,
            )
        return accepted

    def _failure_error(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        phase: str,
        component: str = "server",
        exception: BaseException | None = None,
        retryable: bool | None = None,
        **detail: Any,
    ) -> dict[str, Any]:
        """Build a wire error and journal only allowlisted diagnostic metadata."""
        payload = _err(code, message, **detail)
        failure_id = record_failure(
            component=component,
            code=code,
            operation=operation,
            phase=phase,
            exception=exception,
            server_version=SERVER_VERSION,
            extension_version=self._extension_version,
            retryable=retryable,
        )
        if failure_id:
            payload["error"]["failure_id"] = failure_id
        return payload

    # ─── auth ────────────────────────────────────────────────────────

    def _reject_origin(self, ws: ServerConnection) -> str | None:
        """Return a rejection reason if the handshake Origin isn't acceptable.

        Browsers always send Origin; the Python client never does. So an Origin
        that isn't a Chrome extension means a web page is calling us.
        """
        request = ws.request
        origin = request.headers.get("Origin") if request else None
        if origin is None:
            return None
        if origin.startswith(EXTENSION_ORIGIN_PREFIX):
            return None
        return f"refusing WebSocket from web origin {origin!r}"

    def _check_token(self, msg: dict) -> str | None:
        """Return a rejection reason if the CLI token is missing or wrong."""
        if self._token is None:
            return None
        supplied = msg.get("token")
        ok = False
        if isinstance(supplied, str):
            # Compare bytes: compare_digest raises TypeError on str inputs
            # containing non-ASCII, which would surface as an opaque 1011.
            ok = hmac.compare_digest(supplied.encode("utf-8"), self._token.encode("utf-8"))
        if not ok:
            return (
                "missing or invalid token — the bridge server generates one at "
                f"{token_path()}; the Python client reads it automatically. "
                "Start the server with --no-auth to disable this check."
            )
        return None

    # ─── connection dispatch ─────────────────────────────────────────

    async def handle(self, ws: ServerConnection) -> None:
        reason = self._reject_origin(ws)
        if reason:
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(await self._origin_rejection_error(reason)))
            await ws.close(1008, "unauthorized")
            return

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT)
        # asyncio.TimeoutError only became an alias of the builtin in 3.11; on
        # 3.10 it is concurrent.futures.TimeoutError and `except TimeoutError`
        # misses it entirely, killing the connection instead of answering.
        except asyncio.TimeoutError:
            logger.warning("handshake timed out after %.0fs", HANDSHAKE_TIMEOUT)
            record_failure(
                component="server",
                code="HANDSHAKE_TIMEOUT",
                operation="handshake",
                phase="receive",
                server_version=SERVER_VERSION,
                retryable=True,
            )
            return
        except websockets.exceptions.WebSocketException as e:
            logger.warning("handshake failed: %s", e)
            record_failure(
                component="server",
                code="HANDSHAKE_FAILED",
                operation="handshake",
                phase="receive",
                exception=e,
                server_version=SERVER_VERSION,
                retryable=True,
            )
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("handshake was not JSON: %s", e)
            with contextlib.suppress(Exception):
                await ws.send(
                    json.dumps(
                        self._failure_error(
                            "BAD_REQUEST",
                            f"handshake was not JSON: {e}",
                            operation="handshake",
                            phase="decode",
                            exception=e,
                            retryable=False,
                        )
                    )
                )
            return
        if not isinstance(msg, dict):
            with contextlib.suppress(Exception):
                await ws.send(
                    json.dumps(
                        self._failure_error(
                            "BAD_REQUEST",
                            "handshake must be a JSON object",
                            operation="handshake",
                            phase="validate",
                            retryable=False,
                        )
                    )
                )
            return

        role = msg.get("role")
        if role == "extension":
            version = msg.get("version")
            await self._handle_extension(ws, version if isinstance(version, str) else None)
        elif role == "cli":
            denial = self._check_token(msg)
            if denial:
                logger.warning("rejected CLI connection: %s", denial.splitlines()[0])
                with contextlib.suppress(Exception):
                    await ws.send(
                        json.dumps(
                            self._failure_error(
                                "UNAUTHORIZED",
                                denial,
                                operation="handshake",
                                phase="token",
                                retryable=False,
                            )
                        )
                    )
                await ws.close(1008, "unauthorized")
                return
            await self._handle_cli(ws, msg)
        else:
            logger.warning("unknown role: %s", role)
            with contextlib.suppress(Exception):
                await ws.send(
                    json.dumps(
                        self._failure_error(
                            "BAD_REQUEST",
                            f"unknown role: {role!r}",
                            operation="handshake",
                            phase="role",
                            retryable=False,
                        )
                    )
                )

    # ─── Extension 端（长连接） ───────────────────────────────────────

    async def _handle_extension(self, ws: ServerConnection, version: str | None = None) -> None:
        previous = self._extension_ws
        if previous is not None:
            logger.warning(
                "a second extension connected — dropping the previous one. Two Chrome "
                "Bridge extensions (or two profiles) loaded at once makes routing "
                "non-deterministic; uninstall the duplicate in chrome://extensions."
            )
            record_failure(
                component="server",
                code="DUPLICATE_EXTENSION",
                operation="extension_connection",
                phase="register",
                server_version=SERVER_VERSION,
                extension_version=version,
                retryable=False,
            )
            # Close the old socket instead of merely shadowing it. Otherwise the
            # *newer* socket closing later would deregister while the older one
            # is still open and unreachable — the same permanent wedge the
            # identity guard below exists to prevent, just mirrored.
            with contextlib.suppress(Exception):
                await previous.close(1000, "superseded by a newer extension")
        logger.info("extension connected (version %s)", version or "unknown")
        self._extension_ws = ws
        self._extension_version = version
        self._reload_triggered = False
        self._reload_generation += 1
        if self._reload_pending:
            self._reload_pending = False
            asyncio.create_task(self._reload_extension())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError as e:
                    logger.warning("extension sent non-JSON frame (%d bytes)", len(raw))
                    record_failure(
                        component="server",
                        code="PROTOCOL_INVALID_FRAME",
                        operation="extension_connection",
                        phase="decode",
                        exception=e,
                        server_version=SERVER_VERSION,
                        extension_version=self._extension_version,
                        retryable=False,
                    )
                    continue
                if not isinstance(msg, dict):
                    record_failure(
                        component="server",
                        code="PROTOCOL_INVALID_FRAME",
                        operation="extension_connection",
                        phase="validate",
                        server_version=SERVER_VERSION,
                        extension_version=self._extension_version,
                        retryable=False,
                    )
                    continue
                if msg.get("type") == "failure_batch":
                    acknowledged: list[str] = []
                    retry_after_ms: int | None = None
                    events = msg.get("events")
                    if msg.get("schema") == 1 and isinstance(events, list):
                        requested = min(len(events), 25)
                        batch_size = self._extension_failure_budget(requested)
                        acknowledged = await asyncio.to_thread(
                            record_extension_failures,
                            events[:batch_size],
                            server_version=SERVER_VERSION,
                        )
                        self._remember_failure_ids(acknowledged)
                        if len(acknowledged) < requested:
                            retry_after_ms = 60_000
                    with contextlib.suppress(Exception):
                        ack: dict[str, Any] = {
                            "type": "failure_ack",
                            "event_ids": acknowledged,
                        }
                        if retry_after_ms is not None:
                            ack["retry_after_ms"] = retry_after_ms
                        await ws.send(json.dumps(ack))
                    continue
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        future.set_result(msg)
        except websockets.exceptions.ConnectionClosedError as e:
            # 1009 == the frame blew past max_size. Name the real cause; this
            # used to reach the caller as "extension disconnected".
            logger.warning("extension connection dropped: %s", e)
            record_failure(
                component="server",
                code="EXTENSION_DISCONNECTED",
                operation="extension_connection",
                phase="receive",
                exception=e,
                server_version=SERVER_VERSION,
                extension_version=self._extension_version,
                websocket_close_code=e.code,
                retryable=True,
            )
        finally:
            # Identity guard: a *stale* socket closing must not unregister the
            # live one. Without this, a sleep/wake reconnect (or a duplicate
            # extension) leaves the bridge permanently wedged — the extension
            # believes it is connected, so it never re-registers.
            if self._extension_ws is ws:
                had_pending = bool(self._pending)
                self._extension_ws = None
                self._extension_version = None
                if not self._reload_triggered:
                    logger.info("extension disconnected")
                    if had_pending:
                        record_failure(
                            component="server",
                            code="EXTENSION_DISCONNECTED",
                            operation="extension_connection",
                            phase="inflight",
                            server_version=SERVER_VERSION,
                            extension_version=version,
                            retryable=True,
                        )
                for future in self._pending.values():
                    if not future.done():
                        future.set_exception(ConnectionError("extension disconnected"))
                self._pending.clear()
            else:
                logger.info("stale extension connection closed (a newer one is live)")

    # ─── CLI 端（短连接，发一条命令，收一条回复） ─────────────────────

    async def _handle_cli(self, ws: ServerConnection, msg: dict) -> None:
        method = msg.get("method")

        # 特殊命令：查询 server/extension 状态，无需转发
        if method == "ping_server":
            await ws.send(
                json.dumps(
                    {
                        "result": {
                            "extension_connected": self._extension_ws is not None,
                            "extension_version": self._extension_version,
                            "pending": len(self._pending),
                            "server_version": SERVER_VERSION,
                        }
                    }
                )
            )
            return

        if not self._extension_ws:
            await ws.send(
                json.dumps(
                    self._failure_error(
                        "EXTENSION_NOT_CONNECTED",
                        "Extension not connected. Make sure the Chrome Bridge extension "
                        "is installed and enabled, then reload it in chrome://extensions.",
                        operation=str(method or "unknown"),
                        phase="route",
                        retryable=True,
                    )
                )
            )
            return

        timeout = self._deadline_for(msg)
        msg_id = str(uuid.uuid4())
        msg["id"] = msg_id
        msg.pop("token", None)  # never forward the secret into the browser

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = future

        started = time.monotonic()
        logger.debug("→ %s (id=%s, timeout=%.0fs)", method, msg_id[:8], timeout)
        reload_generation: int | None = None
        reload_extension_version: str | None = None
        if method == "reload_self":
            # chrome.runtime.reload() tears down the extension before it can
            # reliably reply. Mark that disconnect as intentional before the
            # frame is sent, then acknowledge successful delivery to the CLI.
            self._reload_triggered = True
            self._reload_generation += 1
            reload_generation = self._reload_generation
            reload_extension_version = self._extension_version
        try:
            await self._extension_ws.send(json.dumps(msg))
        except Exception as e:
            # The extension vanished between the liveness check and the send.
            self._pending.pop(msg_id, None)
            if method == "reload_self" and reload_generation == self._reload_generation:
                self._reload_triggered = False
            logger.warning("%s: send failed: %s", method, e)
            await ws.send(
                json.dumps(
                    self._failure_error(
                        "EXTENSION_NOT_CONNECTED",
                        f"send failed: {e}",
                        operation=str(method or "unknown"),
                        phase="extension_send",
                        exception=e,
                        retryable=True,
                    )
                )
            )
            return

        if method == "reload_self":
            assert reload_generation is not None
            self._pending.pop(msg_id, None)
            future.cancel()
            asyncio.create_task(
                self._manual_reload_watchdog(reload_generation, reload_extension_version)
            )
            await ws.send(
                json.dumps(
                    {
                        "result": {
                            "ok": True,
                            "message": "extension reload command delivered",
                        }
                    }
                )
            )
            return

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("← %s in %.2fs", method, time.monotonic() - started)
            if isinstance(result, dict) and result.get("error"):
                wire_error = result["error"]
                wire_failure_id = (
                    wire_error.get("failure_id") if isinstance(wire_error, dict) else None
                )
                already_recorded = (
                    is_valid_failure_id(wire_failure_id)
                    and wire_failure_id in self._accepted_failure_ids
                )
                if isinstance(wire_error, dict) and not already_recorded:
                    failure_id = await self._ensure_durable_extension_failure(
                        wire_failure_id,
                        code=str(wire_error.get("code") or "INTERNAL"),
                        operation=str(method or "unknown"),
                        duration_ms=(time.monotonic() - started) * 1000,
                    )
                    if failure_id:
                        wire_error = {**wire_error, "failure_id": failure_id}
                        result = {**result, "error": wire_error}
                    else:
                        wire_error = {**wire_error}
                        wire_error.pop("failure_id", None)
                        result = {**result, "error": wire_error}
            elif (
                method == "close_owned_tabs"
                and isinstance(result, dict)
                and isinstance(result.get("result"), dict)
                and "failure_id" in result["result"]
            ):
                cleanup_result = result["result"]
                failure_id = await self._ensure_durable_extension_failure(
                    cleanup_result.get("failure_id"),
                    code="CLEANUP_INCOMPLETE",
                    operation="close_owned_tabs",
                    duration_ms=(time.monotonic() - started) * 1000,
                )
                cleanup_result = {**cleanup_result}
                if failure_id:
                    cleanup_result["failure_id"] = failure_id
                else:
                    cleanup_result.pop("failure_id", None)
                result = {**result, "result": cleanup_result}
            await ws.send(json.dumps(result))
        except asyncio.TimeoutError:  # see the note in handle() — 3.10 differs
            self._pending.pop(msg_id, None)
            logger.warning("%s timed out after %.0fs", method, timeout)
            await ws.send(
                json.dumps(
                    self._failure_error(
                        "TIMEOUT",
                        f"command {method!r} timed out after {timeout:.0f}s",
                        operation=str(method or "unknown"),
                        phase="deadline",
                        retryable=True,
                        method=method,
                        timeout=timeout,
                    )
                )
            )
        except ConnectionError as e:
            logger.warning("%s aborted: %s", method, e)
            await ws.send(
                json.dumps(
                    self._failure_error(
                        "EXTENSION_NOT_CONNECTED",
                        str(e),
                        operation=str(method or "unknown"),
                        phase="inflight",
                        exception=e,
                        retryable=True,
                    )
                )
            )
        finally:
            self._pending.pop(msg_id, None)

    @staticmethod
    def _deadline_for(msg: dict) -> float:
        """Honour the caller's deadline instead of a fixed 90 s wall."""
        raw = msg.get("deadline_ms")
        if isinstance(raw, (int, float)) and raw > 0:
            return max(1.0, float(raw) / 1000.0)
        return DEFAULT_COMMAND_TIMEOUT

    async def _manual_reload_watchdog(self, generation: int, extension_version: str | None) -> None:
        await asyncio.sleep(MANUAL_RELOAD_RECONNECT_TIMEOUT)
        if generation != self._reload_generation or not self._reload_triggered:
            return
        self._reload_triggered = False
        logger.warning("extension did not reconnect after a delivered manual reload command")
        record_failure(
            component="server",
            code="RELOAD_RECONNECT_TIMEOUT",
            operation="reload_self",
            phase="reconnect",
            server_version=SERVER_VERSION,
            extension_version=extension_version,
            retryable=True,
        )

    # ─── 文件监控 + 自动重载 ─────────────────────────────────────────

    async def _reload_extension(self) -> None:
        """Send reload_self to extension, then wait for reconnect."""
        if not self._extension_ws:
            if not self._reload_pending:
                record_failure(
                    component="server",
                    code="EXTENSION_NOT_CONNECTED",
                    operation="reload_self",
                    phase="route",
                    server_version=SERVER_VERSION,
                    retryable=True,
                )
            self._reload_pending = True
            logger.warning("extension not connected; skipping reload")
            return

        self._reload_triggered = True
        self._reload_generation += 1
        generation = self._reload_generation
        extension_version = self._extension_version
        logger.info("triggering extension reload...")
        msg = {"method": "reload_self", "id": str(uuid.uuid4())}
        try:
            await self._extension_ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning(
                "failed to send reload command: %s (extension may have already reloaded)", e
            )
            record_failure(
                component="server",
                code="RELOAD_SEND_FAILED",
                operation="reload_self",
                phase="extension_send",
                exception=e,
                server_version=SERVER_VERSION,
                extension_version=extension_version,
                retryable=True,
            )

        for _ in range(AUTO_RELOAD_RECONNECT_ATTEMPTS):
            await asyncio.sleep(AUTO_RELOAD_RECONNECT_INTERVAL)
            if generation != self._reload_generation:
                return
            if self._extension_ws is None:
                continue
            if not self._reload_triggered:
                logger.info("extension reloaded and reconnected")
                return
        if generation != self._reload_generation or not self._reload_triggered:
            return
        self._reload_triggered = False
        logger.warning("extension did not reconnect within 15s after reload")
        record_failure(
            component="server",
            code="RELOAD_RECONNECT_TIMEOUT",
            operation="reload_self",
            phase="reconnect",
            server_version=SERVER_VERSION,
            extension_version=extension_version,
            retryable=True,
        )

    async def watch_files(self) -> None:
        """Background task: watch manifest.json and background.js for changes."""
        hashes = {p: _file_hash(p) for p in WATCH_FILES}
        logger.info("watching extension files: %s", [p.name for p in WATCH_FILES])

        while True:
            try:
                await asyncio.sleep(2)
                for p in WATCH_FILES:
                    new_hash = _file_hash(p)
                    if new_hash != hashes[p]:
                        logger.info("detected change: %s", p.name)
                        hashes[p] = new_hash
                        await asyncio.sleep(0.3)  # debounce
                        await self._reload_extension()
                        break  # one reload per batch
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # A crashed watcher used to disappear silently, taking
                # auto-reload with it and leaving no trace anywhere.
                logger.exception("file watcher iteration failed; continuing")
                record_failure(
                    component="server",
                    code="FILE_WATCHER_FAILED",
                    operation="watch_files",
                    phase="iteration",
                    exception=e,
                    server_version=SERVER_VERSION,
                    retryable=True,
                )


async def main(port: int, watch: bool, token: str | None) -> None:
    server = BridgeServer(token=token)
    watcher_task = asyncio.create_task(server.watch_files()) if watch else None

    try:
        async with serve(server.handle, "localhost", port, max_size=MAX_FRAME_BYTES):
            logger.info(
                "Chrome Bridge server %s listening on ws://localhost:%d", SERVER_VERSION, port
            )
            if token:
                logger.info("auth enabled; token at %s", token_path())
            else:
                logger.warning(
                    "auth DISABLED (--no-auth): any local process can drive your browser"
                )
            logger.info("waiting for the Chrome extension to connect...")
            await asyncio.Future()  # run forever
    except OSError as e:
        logger.error(
            "cannot listen on port %d: %s. Another bridge server is probably already "
            "running — check with `lsof -nP -iTCP:%d -sTCP:LISTEN`, or pass --port.",
            port,
            e,
            port,
        )
        record_failure(
            component="server",
            code="SERVER_BIND_FAILED",
            operation="server_start",
            phase="bind",
            exception=e,
            server_version=SERVER_VERSION,
            retryable=True,
        )
        raise SystemExit(1) from e
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher_task


def main_cli() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]

    parser = argparse.ArgumentParser(description="Chrome Bridge Server")
    parser.add_argument("--port", type=int, default=9333, help="WebSocket port (default 9333)")
    parser.add_argument(
        "--no-watch",
        action="store_true",
        help="don't auto-reload the extension when extension/ files change "
        "(auto-reload discards every open browse session)",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="accept unauthenticated CLI connections (not recommended)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="log every command")
    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    token = None if args.no_auth else ensure_token()

    try:
        asyncio.run(main(args.port, watch=not args.no_watch, token=token))
    except KeyboardInterrupt:
        logger.info("shutting down")


if __name__ == "__main__":
    main_cli()
