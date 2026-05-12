"""Chrome Bridge Server.

A WebSocket relay between a Python client and a Chrome extension.
The extension keeps a long-lived connection (role=extension); CLI clients
open short-lived connections (role=cli) to send commands, which the server
forwards to the extension and routes results back.

Run:
    python scripts/bridge_server.py [--port 9333]

Features:
    - Watches extension/manifest.json and extension/background.js
    - Triggers chrome.runtime.reload() automatically on change
      (after one initial manual reload to grant the reload_self hook)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.server import ServerConnection

logger = logging.getLogger("chrome-bridge")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCH_FILES = [
    PROJECT_ROOT / "extension" / "manifest.json",
    PROJECT_ROOT / "extension" / "background.js",
]


def _file_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


class BridgeServer:
    def __init__(self) -> None:
        self._extension_ws: ServerConnection | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._reload_triggered = False

    async def handle(self, ws: ServerConnection) -> None:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
        except (asyncio.TimeoutError, Exception) as e:
            logger.warning("handshake timeout or failed: %s", e)
            return

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        role = msg.get("role")
        if role == "extension":
            await self._handle_extension(ws)
        elif role == "cli":
            await self._handle_cli(ws, msg)
        else:
            logger.warning("unknown role: %s", role)

    # ─── Extension 端（长连接） ───────────────────────────────────────

    async def _handle_extension(self, ws: ServerConnection) -> None:
        logger.info("extension connected")
        self._extension_ws = ws
        self._reload_triggered = False
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                msg_id = msg.get("id")
                if msg_id and msg_id in self._pending:
                    future = self._pending.pop(msg_id)
                    if not future.done():
                        future.set_result(msg)
        finally:
            self._extension_ws = None
            if not self._reload_triggered:
                logger.info("extension disconnected")
            # 唤醒所有等待中的 CLI 请求并报错
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("extension disconnected"))
            self._pending.clear()

    # ─── CLI 端（短连接，发一条命令，收一条回复） ─────────────────────

    async def _handle_cli(self, ws: ServerConnection, msg: dict) -> None:
        # 特殊命令：查询 server/extension 状态，无需转发
        if msg.get("method") == "ping_server":
            await ws.send(json.dumps({"result": {"extension_connected": self._extension_ws is not None}}))
            return

        if not self._extension_ws:
            await ws.send(json.dumps({"error": "Extension not connected. Make sure the Chrome Bridge extension is installed and enabled."}))
            return

        msg_id = str(uuid.uuid4())
        msg["id"] = msg_id

        loop = asyncio.get_event_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[msg_id] = future

        await self._extension_ws.send(json.dumps(msg))

        try:
            result = await asyncio.wait_for(future, timeout=90.0)
            await ws.send(json.dumps(result))
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            await ws.send(json.dumps({"error": "command timed out after 90s"}))
        except ConnectionError as e:
            await ws.send(json.dumps({"error": str(e)}))

    # ─── 文件监控 + 自动重载 ─────────────────────────────────────────

    async def _reload_extension(self) -> None:
        """Send reload_self to extension, then wait for reconnect."""
        if not self._extension_ws:
            logger.warning("extension not connected; skipping reload")
            return

        self._reload_triggered = True
        logger.info("triggering extension reload...")
        msg_id = str(uuid.uuid4())
        msg = {"method": "reload_self", "id": msg_id}
        try:
            await self._extension_ws.send(json.dumps(msg))
        except Exception as e:
            logger.warning("failed to send reload command: %s (extension may have already reloaded)", e)

        # Wait for extension to reconnect
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
            await asyncio.sleep(2)
            for p in WATCH_FILES:
                new_hash = _file_hash(p)
                if new_hash != hashes[p]:
                    logger.info("detected change: %s", p.name)
                    hashes[p] = new_hash
                    await asyncio.sleep(0.3)  # debounce
                    await self._reload_extension()
                    break  # one reload per batch


async def main(port: int) -> None:
    server = BridgeServer()
    watcher_task = asyncio.create_task(server.watch_files())

    async with websockets.serve(server.handle, "localhost", port):
        logger.info("Chrome Bridge server listening on ws://localhost:%d", port)
        logger.info("waiting for the Chrome extension to connect...")
        await asyncio.Future()  # run forever

    watcher_task.cancel()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # pyright: ignore[reportAttributeAccessIssue]

    parser = argparse.ArgumentParser(description="Chrome Bridge Server")
    parser.add_argument("--port", type=int, default=9333, help="WebSocket port (default 9333)")
    args = parser.parse_args()

    asyncio.run(main(args.port))
