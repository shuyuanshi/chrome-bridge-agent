"""Relay tests driven by a fake extension — no Chrome required.

``bridge_server.py`` used to have zero coverage, which is how two bridge-killing
bugs survived:

* a reply larger than the websockets default frame cap (1 MiB) closed the
  extension socket, and the caller was told "extension disconnected";
* a *stale* extension socket closing unregistered the *live* one, wedging the
  bridge until someone manually reloaded the extension.

Both have regression tests below.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Callable
from typing import Any

import pytest
from bridge_server import MAX_FRAME_BYTES, BridgeServer
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

TOKEN = "test-token-abc"


@contextlib.asynccontextmanager
async def running_server(token: str | None = TOKEN):
    """A real BridgeServer on an ephemeral port."""
    server = BridgeServer(token=token)
    async with serve(server.handle, "localhost", 0, max_size=MAX_FRAME_BYTES) as srv:
        yield server, srv.sockets[0].getsockname()[1]


@contextlib.asynccontextmanager
async def fake_extension(port: int, handler: Callable[[dict], Any] | None = None):
    """A stand-in for the Chrome extension.

    ``handler`` maps a command to its result; the default echoes the method.
    Returns the live connection so a test can close it mid-flight.
    """
    ws = await connect(f"ws://localhost:{port}", max_size=MAX_FRAME_BYTES)
    await ws.send(json.dumps({"role": "extension"}))

    async def pump() -> None:
        async for raw in ws:
            msg = json.loads(raw)
            if handler is None:
                continue
            result = handler(msg)
            if result is _NO_REPLY:
                continue
            await ws.send(json.dumps({"id": msg.get("id"), "result": result}))

    task = asyncio.create_task(pump())
    await asyncio.sleep(0.05)  # let the server register the connection
    try:
        yield ws
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        await ws.close()


_NO_REPLY = object()


async def cli_call(port: int, message: dict, *, token: str | None = TOKEN, **kwargs: Any) -> dict:
    ws = await connect(f"ws://localhost:{port}", max_size=MAX_FRAME_BYTES, **kwargs)
    try:
        body = {"role": "cli", **message}
        if token is not None:
            body["token"] = token
        await ws.send(json.dumps(body))
        return json.loads(await ws.recv())
    finally:
        # A refused connection is closed by the server with 1008; closing our
        # end again must not turn a valid reply into an exception.
        with contextlib.suppress(Exception):
            await ws.close()


def run(coro) -> Any:
    return asyncio.run(asyncio.wait_for(coro, timeout=30))


# ─────────────────────── P0 regressions ───────────────────────


def test_reply_larger_than_1mib_survives() -> None:
    """A 2 MB result must arrive intact, not kill the extension connection."""
    payload = "x" * (2 * 1024 * 1024)

    async def scenario() -> dict:
        async with (
            running_server() as (_server, port),
            fake_extension(port, lambda _msg: payload),
        ):
            reply = await cli_call(port, {"method": "evaluate"})
            # The connection must still be usable afterwards.
            health = await cli_call(port, {"method": "ping_server"})
            return {"reply": reply, "health": health}

    out = run(scenario())
    assert out["reply"].get("result") == payload, out["reply"].get("error")
    assert out["health"]["result"]["extension_connected"] is True


async def _settle(predicate, timeout: float = 5.0) -> None:
    """Poll until predicate() is true — deterministic where a sleep is flaky."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(0.02)


def test_stale_extension_close_keeps_the_live_one() -> None:
    """Closing an older extension socket must not unregister the newer one."""

    async def scenario() -> dict:
        async with (
            running_server() as (server, port),
            fake_extension(port, lambda _m: "first") as ext1,
            fake_extension(port, lambda _m: "second") as ext2,
        ):
            await _settle(lambda: server._extension_ws is not None)
            await ext1.close()
            await _settle(lambda: server._extension_ws is ext2 or ext1.close_code is not None)
            health = await cli_call(port, {"method": "ping_server"})
            reply = await cli_call(port, {"method": "evaluate"})
            return {"health": health, "reply": reply}

    out = run(scenario())
    assert out["health"]["result"]["extension_connected"] is True
    assert out["reply"].get("result") == "second", out["reply"].get("error")


def test_newest_extension_close_does_not_wedge_the_bridge() -> None:
    """The mirror image: the *newer* socket closing must not orphan an older,
    still-open one. The fix is to drop the previous socket at registration, so
    at most one extension is ever live."""

    async def scenario() -> dict:
        async with (
            running_server() as (server, port),
            fake_extension(port, lambda _m: "first"),
            fake_extension(port, lambda _m: "second") as ext2,
        ):
            await _settle(lambda: server._extension_ws is not None)
            await ext2.close()
            await _settle(lambda: server._extension_ws is None)
            return await cli_call(port, {"method": "ping_server"})

    reply = run(scenario())
    # Nothing is live, and the server says so — the failure mode to avoid is
    # reporting "connected" while every command errors, or vice versa.
    assert reply["result"]["extension_connected"] is False


def test_extension_version_is_reported() -> None:
    """Lets a client detect a stale service worker that predates tab routing."""

    async def scenario() -> dict:
        async with running_server() as (_server, port):
            ws = await connect(f"ws://localhost:{port}")
            await ws.send(json.dumps({"role": "extension", "version": "2.0.0"}))
            await asyncio.sleep(0.05)
            try:
                return await cli_call(port, {"method": "ping_server"})
            finally:
                await ws.close()

    reply = run(scenario())
    assert reply["result"]["extension_version"] == "2.0.0"


# ─────────────────────── routing & errors ───────────────────────


def test_no_extension_returns_typed_error() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            return await cli_call(port, {"method": "evaluate"})

    reply = run(scenario())
    assert reply["error"]["code"] == "EXTENSION_NOT_CONNECTED"


def test_command_timeout_is_structured_and_honours_the_deadline() -> None:
    async def scenario() -> dict:
        async with (
            running_server() as (_server, port),
            fake_extension(port, lambda _m: _NO_REPLY),
        ):
            return await cli_call(port, {"method": "evaluate", "deadline_ms": 400})

    reply = run(scenario())
    assert reply["error"]["code"] == "TIMEOUT"
    assert reply["error"]["detail"]["timeout"] == pytest.approx(1.0)  # floored at 1s


def test_extension_disconnect_fails_inflight_commands() -> None:
    async def scenario() -> dict:
        async with (
            running_server() as (_server, port),
            fake_extension(port, lambda _m: _NO_REPLY) as ext,
        ):

            async def kill() -> None:
                await asyncio.sleep(0.2)
                await ext.close()

            killer = asyncio.create_task(kill())
            try:
                return await cli_call(port, {"method": "evaluate", "deadline_ms": 10000})
            finally:
                await killer

    reply = run(scenario())
    assert reply["error"]["code"] == "EXTENSION_NOT_CONNECTED"


def test_extension_error_is_relayed_verbatim() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            ws = await connect(f"ws://localhost:{port}")
            await ws.send(json.dumps({"role": "extension"}))

            async def pump() -> None:
                async for raw in ws:
                    msg = json.loads(raw)
                    await ws.send(
                        json.dumps(
                            {
                                "id": msg["id"],
                                "error": {"code": "ELEMENT_NOT_FOUND", "message": "no #foo"},
                            }
                        )
                    )

            task = asyncio.create_task(pump())
            await asyncio.sleep(0.05)
            try:
                return await cli_call(port, {"method": "click_element"})
            finally:
                task.cancel()
                await ws.close()

    reply = run(scenario())
    assert reply["error"]["code"] == "ELEMENT_NOT_FOUND"


def test_ping_server_reports_state() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            before = await cli_call(port, {"method": "ping_server"})
            async with fake_extension(port):
                after = await cli_call(port, {"method": "ping_server"})
            return {"before": before, "after": after}

    out = run(scenario())
    assert out["before"]["result"]["extension_connected"] is False
    assert out["after"]["result"]["extension_connected"] is True


def test_bad_json_and_unknown_role_get_an_answer() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            async with connect(f"ws://localhost:{port}") as ws:
                await ws.send("not json at all")
                bad = json.loads(await ws.recv())
            async with connect(f"ws://localhost:{port}") as ws:
                await ws.send(json.dumps({"role": "wat"}))
                role = json.loads(await ws.recv())
            return {"bad": bad, "role": role}

    out = run(scenario())
    assert out["bad"]["error"]["code"] == "BAD_REQUEST"
    assert out["role"]["error"]["code"] == "BAD_REQUEST"


# ─────────────────────── auth ───────────────────────


def test_cli_without_token_is_refused() -> None:
    async def scenario() -> dict:
        async with (
            running_server() as (_server, port),
            fake_extension(port, lambda _m: "ok"),
        ):
            return await cli_call(port, {"method": "evaluate"}, token=None)

    reply = run(scenario())
    assert reply["error"]["code"] == "UNAUTHORIZED"


def test_cli_with_wrong_token_is_refused() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            return await cli_call(port, {"method": "ping_server"}, token="nope")

    reply = run(scenario())
    assert reply["error"]["code"] == "UNAUTHORIZED"


def test_no_auth_mode_accepts_everything() -> None:
    async def scenario() -> dict:
        async with running_server(token=None) as (_server, port):
            return await cli_call(port, {"method": "ping_server"}, token=None)

    reply = run(scenario())
    assert reply["result"]["extension_connected"] is False


def test_web_page_origin_is_refused() -> None:
    """A page on any https:// site can open ws://localhost — it must be refused.

    ``ws://localhost`` counts as a potentially-trustworthy origin, so mixed
    content does *not* stop a web page from trying. The refusal has to happen
    here, and no command may reach the browser.
    """
    delivered: list[dict] = []

    async def scenario() -> dict:
        async with running_server() as (_server, port):

            def handler(msg: dict) -> str:
                delivered.append(msg)
                return "leaked!"

            async with fake_extension(port, handler):
                ws = await connect(
                    f"ws://localhost:{port}",
                    additional_headers={"Origin": "https://evil.example"},
                )
                reply = None
                try:
                    with contextlib.suppress(Exception):
                        reply = json.loads(await ws.recv())
                    with contextlib.suppress(Exception):
                        await ws.send(
                            json.dumps({"role": "cli", "method": "get_cookies", "token": TOKEN})
                        )
                        reply = reply or json.loads(await ws.recv())
                finally:
                    with contextlib.suppress(Exception):
                        await ws.close()
                await asyncio.sleep(0.1)
                return {"reply": reply, "close_code": ws.close_code}

    out = run(scenario())
    assert delivered == [], "a web-origin socket got a command through to the browser"
    assert out["close_code"] == 1008 or out["reply"]["error"]["code"] == "UNAUTHORIZED"


def test_extension_origin_is_allowed() -> None:
    async def scenario() -> dict:
        async with running_server() as (_server, port):
            ws = await connect(
                f"ws://localhost:{port}",
                additional_headers={"Origin": "chrome-extension://abcdef"},
            )
            await ws.send(json.dumps({"role": "extension"}))
            await asyncio.sleep(0.05)
            try:
                return await cli_call(port, {"method": "ping_server"})
            finally:
                await ws.close()

    reply = run(scenario())
    assert reply["result"]["extension_connected"] is True


def test_non_ascii_token_denies_instead_of_crashing() -> None:
    """hmac.compare_digest raises TypeError on non-ASCII str, which used to
    surface as a 1011 and a 'could not talk to the server' red herring."""

    async def scenario() -> dict:
        async with running_server(token="tök-en") as (_server, port):
            good = await cli_call(port, {"method": "ping_server"}, token="tök-en")
            bad = await cli_call(port, {"method": "ping_server"}, token="wrong")
            return {"good": good, "bad": bad}

    out = run(scenario())
    assert out["good"]["result"]["extension_connected"] is False
    assert out["bad"]["error"]["code"] == "UNAUTHORIZED"


def test_token_is_not_forwarded_to_the_browser() -> None:
    """The shared secret must never reach page-side code."""
    seen: list[dict] = []

    async def scenario() -> None:
        async with running_server() as (_server, port):

            def handler(msg: dict) -> str:
                seen.append(msg)
                return "ok"

            async with fake_extension(port, handler):
                await cli_call(port, {"method": "evaluate"})

    run(scenario())
    assert seen and "token" not in seen[0]
