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
from pathlib import Path
from typing import Any

import websockets
from bridge_auth import ensure_token, token_path
from websockets.asyncio.server import ServerConnection, serve

logger = logging.getLogger("chrome-bridge")

SERVER_VERSION = "1.1.0"

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
        self._token = token

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
            logger.warning("%s", reason)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(_err("UNAUTHORIZED", reason)))
            await ws.close(1008, "unauthorized")
            return

        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HANDSHAKE_TIMEOUT)
        except TimeoutError:
            logger.warning("handshake timed out after %.0fs", HANDSHAKE_TIMEOUT)
            return
        except websockets.exceptions.WebSocketException as e:
            logger.warning("handshake failed: %s", e)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.warning("handshake was not JSON: %s", e)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(_err("BAD_REQUEST", f"handshake was not JSON: {e}")))
            return
        if not isinstance(msg, dict):
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(_err("BAD_REQUEST", "handshake must be a JSON object")))
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
                    await ws.send(json.dumps(_err("UNAUTHORIZED", denial)))
                await ws.close(1008, "unauthorized")
                return
            await self._handle_cli(ws, msg)
        else:
            logger.warning("unknown role: %s", role)
            with contextlib.suppress(Exception):
                await ws.send(json.dumps(_err("BAD_REQUEST", f"unknown role: {role!r}")))

    # ─── Extension 端（长连接） ───────────────────────────────────────

    async def _handle_extension(self, ws: ServerConnection, version: str | None = None) -> None:
        previous = self._extension_ws
        if previous is not None:
            logger.warning(
                "a second extension connected — dropping the previous one. Two Chrome "
                "Bridge extensions (or two profiles) loaded at once makes routing "
                "non-deterministic; uninstall the duplicate in chrome://extensions."
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
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    logger.warning("extension sent non-JSON frame (%d bytes)", len(raw))
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
        finally:
            # Identity guard: a *stale* socket closing must not unregister the
            # live one. Without this, a sleep/wake reconnect (or a duplicate
            # extension) leaves the bridge permanently wedged — the extension
            # believes it is connected, so it never re-registers.
            if self._extension_ws is ws:
                self._extension_ws = None
                self._extension_version = None
                if not self._reload_triggered:
                    logger.info("extension disconnected")
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
                    _err(
                        "EXTENSION_NOT_CONNECTED",
                        "Extension not connected. Make sure the Chrome Bridge extension "
                        "is installed and enabled, then reload it in chrome://extensions.",
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
        try:
            await self._extension_ws.send(json.dumps(msg))
        except Exception as e:
            # The extension vanished between the liveness check and the send.
            self._pending.pop(msg_id, None)
            logger.warning("%s: send failed: %s", method, e)
            await ws.send(json.dumps(_err("EXTENSION_NOT_CONNECTED", f"send failed: {e}")))
            return

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.debug("← %s in %.2fs", method, time.monotonic() - started)
            await ws.send(json.dumps(result))
        except TimeoutError:
            self._pending.pop(msg_id, None)
            logger.warning("%s timed out after %.0fs", method, timeout)
            await ws.send(
                json.dumps(
                    _err(
                        "TIMEOUT",
                        f"command {method!r} timed out after {timeout:.0f}s",
                        method=method,
                        timeout=timeout,
                    )
                )
            )
        except ConnectionError as e:
            logger.warning("%s aborted: %s", method, e)
            await ws.send(json.dumps(_err("EXTENSION_NOT_CONNECTED", str(e))))
        finally:
            self._pending.pop(msg_id, None)

    @staticmethod
    def _deadline_for(msg: dict) -> float:
        """Honour the caller's deadline instead of a fixed 90 s wall."""
        raw = msg.get("deadline_ms")
        if isinstance(raw, (int, float)) and raw > 0:
            return max(1.0, float(raw) / 1000.0)
        return DEFAULT_COMMAND_TIMEOUT

    # ─── 文件监控 + 自动重载 ─────────────────────────────────────────

    async def _reload_extension(self) -> None:
        """Send reload_self to extension, then wait for reconnect."""
        if not self._extension_ws:
            logger.warning("extension not connected; skipping reload")
            return

        self._reload_triggered = True
        logger.info("triggering extension reload...")
        msg = {"method": "reload_self", "id": str(uuid.uuid4())}
        try:
            await self._extension_ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning(
                "failed to send reload command: %s (extension may have already reloaded)", e
            )

        for _ in range(30):  # up to 15 seconds
            await asyncio.sleep(0.5)
            if self._extension_ws is None:
                continue
            if not self._reload_triggered:
                logger.info("extension reloaded and reconnected")
                return
        logger.warning("extension did not reconnect within 15s after reload")

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
            except Exception:
                # A crashed watcher used to disappear silently, taking
                # auto-reload with it and leaving no trace anywhere.
                logger.exception("file watcher iteration failed; continuing")


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
