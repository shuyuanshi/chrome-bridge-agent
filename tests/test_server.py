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
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import bridge_server
import pytest
from bridge_failure_journal import FAILURE_LOG_ENV, failure_report
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


def test_no_extension_failure_is_persisted_and_linked_from_wire_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

    async def scenario() -> dict:
        async with running_server() as (_server, port):
            return await cli_call(port, {"method": "evaluate"})

    reply = run(scenario())
    report = failure_report(limit=10)
    assert reply["error"]["failure_id"] == report["events"][0]["failure_id"]
    assert report["matching_events"] == 1
    assert report["events"][0]["component"] == "server"
    assert report["events"][0]["code"] == "EXTENSION_NOT_CONNECTED"


def test_extension_failure_batch_is_acknowledged_replayed_and_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    canary = "PRIVATE_URL_https://example.test/?cookie=secret"
    event = {
        "event_id": "1234567890abcdef1234567890abcdef",
        "occurred_at_ms": int(time.time() * 1000),
        "component": "extension",
        "code": "TAB_CLOSE_FAILED",
        "operation": "browse_close",
        "phase": "command",
        "count": 1,
        "retryable": True,
        "extension_version": "2.2.0",
        "message": canary,
        "detail": {"url": canary},
    }

    second_event = {
        **event,
        "event_id": "abcdef1234567890abcdef1234567890",
        "occurred_at_ms": event["occurred_at_ms"] + 1,
    }
    updated_second = {
        **second_event,
        "count": 3,
        "last_seen_ms": event["occurred_at_ms"] + 2,
    }
    invalid_event = {**event, "event_id": "not-an-extension-id", "message": canary}

    async def scenario() -> list[dict]:
        async with running_server() as (_server, port):
            ws = await connect(f"ws://localhost:{port}")
            await ws.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            payload = json.dumps({"type": "failure_batch", "schema": 1, "events": [event]})
            await ws.send(payload)
            first = json.loads(await ws.recv())
            await ws.send(payload)
            replay = json.loads(await ws.recv())
            await ws.send(
                json.dumps({"type": "failure_batch", "schema": 1, "events": [second_event]})
            )
            second = json.loads(await ws.recv())
            await ws.send(
                json.dumps({"type": "failure_batch", "schema": 1, "events": [updated_second]})
            )
            updated = json.loads(await ws.recv())
            await ws.send(
                json.dumps({"type": "failure_batch", "schema": 1, "events": [invalid_event]})
            )
            invalid = json.loads(await ws.recv())
            await ws.close()
            return [first, replay, second, updated, invalid]

    replies = run(scenario())
    report = failure_report(limit=10)
    assert replies[0]["event_ids"] == ["1234567890abcdef1234567890abcdef"]
    assert replies[1]["event_ids"] == ["1234567890abcdef1234567890abcdef"]
    assert replies[2]["event_ids"] == ["abcdef1234567890abcdef1234567890"]
    assert replies[3]["event_ids"] == ["abcdef1234567890abcdef1234567890"]
    assert replies[4]["event_ids"] == []
    assert report["matching_events"] == 2
    assert report["events"][0]["component"] == "extension"
    assert report["events"][1]["count"] == 3
    assert canary not in path.read_text(encoding="utf-8")


def test_command_timeout_is_structured_and_honours_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

    async def scenario() -> dict:
        async with (
            running_server() as (_server, port),
            fake_extension(port, lambda _m: _NO_REPLY),
        ):
            return await cli_call(port, {"method": "evaluate", "deadline_ms": 400})

    reply = run(scenario())
    assert reply["error"]["code"] == "TIMEOUT"
    assert reply["error"]["failure_id"]
    assert reply["error"]["detail"]["timeout"] == pytest.approx(1.0)  # floored at 1s
    assert failure_report(code="TIMEOUT")["matching_events"] == 1


def test_extension_disconnect_fails_inflight_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

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
    assert reply["error"]["failure_id"]
    assert failure_report(code="EXTENSION_NOT_CONNECTED")["matching_events"] == 1


def test_extension_error_is_relayed_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

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
    assert reply["error"]["failure_id"]
    assert failure_report(code="ELEMENT_NOT_FOUND")["matching_events"] == 1


def test_forged_wire_failure_id_is_replaced_with_a_journal_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    token_shaped_id = "a" * 64

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
                                "error": {
                                    "code": "ELEMENT_NOT_FOUND",
                                    "message": "missing",
                                    "failure_id": token_shaped_id,
                                },
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
    report = failure_report()
    assert reply["error"]["failure_id"] == report["events"][0]["failure_id"]
    assert reply["error"]["failure_id"] != token_shaped_id
    assert token_shaped_id not in path.read_text(encoding="utf-8")


def test_manual_reload_reconnect_is_acknowledged_without_a_failure_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    monkeypatch.setattr(bridge_server, "MANUAL_RELOAD_RECONNECT_TIMEOUT", 1.0)

    async def scenario() -> dict:
        async with running_server() as (server, port):
            first = await connect(f"ws://localhost:{port}")
            await first.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            call = asyncio.create_task(cli_call(port, {"method": "reload_self"}))
            await first.recv()
            reply = await call
            await first.close()
            await _settle(lambda: server._extension_ws is None)
            second = await connect(f"ws://localhost:{port}")
            await second.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            assert server._reload_triggered is False
            await asyncio.sleep(0.01)
            await second.close()
            return reply

    reply = run(scenario())
    assert reply["result"]["ok"] is True
    assert failure_report()["matching_events"] == 0


def test_manual_reload_without_reconnect_is_journaled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    monkeypatch.setattr(bridge_server, "MANUAL_RELOAD_RECONNECT_TIMEOUT", 0.05)

    async def scenario() -> dict:
        async with running_server() as (server, port):
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            call = asyncio.create_task(cli_call(port, {"method": "reload_self"}))
            await ext.recv()
            reply = await call
            # Simulate an extension that received reload_self but ignored it.
            await asyncio.sleep(0.1)
            await ext.close()
            return reply

    reply = run(scenario())
    assert reply["result"]["ok"] is True
    event = failure_report(code="RELOAD_RECONNECT_TIMEOUT")["events"][0]
    assert event["extension_version"] == "2.2.0"


def test_file_reload_while_disconnected_is_journaled_and_delivered_on_reconnect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

    async def scenario() -> dict:
        async with running_server() as (server, port):
            await server._reload_extension()
            assert server._reload_pending is True
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            command = json.loads(await asyncio.wait_for(ext.recv(), timeout=2))
            await ext.close()
            return command

    command = run(scenario())
    event = failure_report(code="EXTENSION_NOT_CONNECTED")["events"][0]
    assert command["method"] == "reload_self"
    assert event["operation"] == "reload_self"
    assert event["phase"] == "route"


def test_auto_reload_timeout_resets_expected_disconnect_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_INTERVAL", 0.01)

    async def scenario() -> bool:
        async with running_server() as (server, port):
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            await server._reload_extension()
            triggered = server._reload_triggered
            await ext.close()
            return triggered

    assert run(scenario()) is False
    event = failure_report(code="RELOAD_RECONNECT_TIMEOUT")["events"][0]
    assert event["extension_version"] == "2.2.0"


def test_older_auto_reload_cannot_clear_newer_manual_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_INTERVAL", 101.0)
    monkeypatch.setattr(bridge_server, "MANUAL_RELOAD_RECONNECT_TIMEOUT", 102.0)

    async def scenario() -> tuple[int, int, bool]:
        auto_gate = asyncio.Event()
        manual_gate = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay: float) -> None:
            if delay == 101.0:
                await auto_gate.wait()
            elif delay == 102.0:
                await manual_gate.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(bridge_server.asyncio, "sleep", controlled_sleep)
        async with running_server() as (server, port):
            manual_watchdog_done = asyncio.Event()
            original_watchdog = server._manual_reload_watchdog

            async def observed_watchdog(generation: int, extension_version: str | None) -> None:
                try:
                    await original_watchdog(generation, extension_version)
                finally:
                    manual_watchdog_done.set()

            monkeypatch.setattr(server, "_manual_reload_watchdog", observed_watchdog)
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)

            auto_reload = asyncio.create_task(server._reload_extension())
            assert json.loads(await ext.recv())["method"] == "reload_self"
            auto_generation = server._reload_generation

            manual_reload = asyncio.create_task(cli_call(port, {"method": "reload_self"}))
            assert json.loads(await ext.recv())["method"] == "reload_self"
            assert (await manual_reload)["result"]["ok"] is True
            manual_generation = server._reload_generation

            auto_gate.set()
            await auto_reload
            newer_reload_still_expected = server._reload_triggered
            manual_gate.set()
            await asyncio.wait_for(manual_watchdog_done.wait(), timeout=1)
            await ext.close()
            return auto_generation, manual_generation, newer_reload_still_expected

    auto_generation, manual_generation, newer_reload_still_expected = run(scenario())
    assert manual_generation > auto_generation
    assert newer_reload_still_expected is True
    assert failure_report(code="RELOAD_RECONNECT_TIMEOUT")["matching_events"] == 1


def test_older_manual_watchdog_cannot_clear_newer_auto_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    monkeypatch.setattr(bridge_server, "MANUAL_RELOAD_RECONNECT_TIMEOUT", 101.0)
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_ATTEMPTS", 1)
    monkeypatch.setattr(bridge_server, "AUTO_RELOAD_RECONNECT_INTERVAL", 102.0)

    async def scenario() -> tuple[int, int, bool, int]:
        manual_gate = asyncio.Event()
        auto_gate = asyncio.Event()
        real_sleep = asyncio.sleep

        async def controlled_sleep(delay: float) -> None:
            if delay == 101.0:
                await manual_gate.wait()
            elif delay == 102.0:
                await auto_gate.wait()
            else:
                await real_sleep(delay)

        monkeypatch.setattr(bridge_server.asyncio, "sleep", controlled_sleep)
        async with running_server() as (server, port):
            manual_watchdog_done = asyncio.Event()
            original_watchdog = server._manual_reload_watchdog

            async def observed_watchdog(generation: int, extension_version: str | None) -> None:
                try:
                    await original_watchdog(generation, extension_version)
                finally:
                    manual_watchdog_done.set()

            monkeypatch.setattr(server, "_manual_reload_watchdog", observed_watchdog)
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)

            manual_reload = asyncio.create_task(cli_call(port, {"method": "reload_self"}))
            assert json.loads(await ext.recv())["method"] == "reload_self"
            assert (await manual_reload)["result"]["ok"] is True
            manual_generation = server._reload_generation

            auto_reload = asyncio.create_task(server._reload_extension())
            assert json.loads(await ext.recv())["method"] == "reload_self"
            auto_generation = server._reload_generation

            manual_gate.set()
            await asyncio.wait_for(manual_watchdog_done.wait(), timeout=1)
            newer_reload_still_expected = server._reload_triggered
            early_timeouts = failure_report(code="RELOAD_RECONNECT_TIMEOUT")["matching_events"]
            auto_gate.set()
            await auto_reload
            await ext.close()
            return (
                manual_generation,
                auto_generation,
                newer_reload_still_expected,
                early_timeouts,
            )

    manual_generation, auto_generation, newer_reload_still_expected, early_timeouts = run(
        scenario()
    )
    assert auto_generation > manual_generation
    assert newer_reload_still_expected is True
    assert early_timeouts == 0
    assert failure_report(code="RELOAD_RECONNECT_TIMEOUT")["matching_events"] == 1


def test_extension_failure_ingestion_is_rate_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))
    # GitHub runners may execute this within their first minute of uptime.
    # The first rejection must be recorded even when monotonic() is below 60.
    monkeypatch.setattr(bridge_server.time, "monotonic", lambda: 10.0)
    server = BridgeServer(token=TOKEN)

    assert server._extension_failure_budget(100) == 100
    assert server._extension_failure_budget(1) == 0
    report = failure_report(code="FAILURE_BATCH_RATE_LIMITED")
    assert report["matching_events"] == 1


def test_rate_limited_batch_falls_back_to_durable_command_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    failure_id = "c" * 32

    async def scenario() -> tuple[dict, dict, dict]:
        async with running_server() as (server, port):
            server._failure_ingest_count = 100
            server._failure_ingest_window = time.monotonic()
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            client = asyncio.create_task(cli_call(port, {"method": "evaluate"}))
            command = json.loads(await ext.recv())
            event = {
                "event_id": failure_id,
                "code": "JS_ERROR",
                "operation": "evaluate",
                "phase": "command",
                "extension_version": "2.2.0",
            }
            await ext.send(json.dumps({"type": "failure_batch", "schema": 1, "events": [event]}))
            empty_ack = json.loads(await ext.recv())
            await ext.send(
                json.dumps(
                    {
                        "id": command["id"],
                        "error": {
                            "code": "JS_ERROR",
                            "message": "private message is not journaled",
                            "failure_id": failure_id,
                        },
                    }
                )
            )
            fallback_ack = json.loads(await ext.recv())
            reply = await client
            await ext.close()
            return empty_ack, fallback_ack, reply

    empty_ack, fallback_ack, reply = run(scenario())
    report = failure_report(code="JS_ERROR")
    assert empty_ack["event_ids"] == []
    assert fallback_ack["event_ids"] == [failure_id]
    assert reply["error"]["failure_id"] == failure_id
    assert report["matching_events"] == 1
    assert report["events"][0]["failure_id"] == failure_id


def test_rate_limited_cleanup_result_id_is_durable_before_relay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    failure_id = "d" * 32

    async def scenario() -> tuple[dict, dict, dict]:
        async with running_server() as (server, port):
            server._failure_ingest_count = 100
            server._failure_ingest_window = time.monotonic()
            ext = await connect(f"ws://localhost:{port}")
            await ext.send(json.dumps({"role": "extension", "version": "2.2.0"}))
            await _settle(lambda: server._extension_ws is not None)
            client = asyncio.create_task(cli_call(port, {"method": "close_owned_tabs"}))
            command = json.loads(await ext.recv())
            event = {
                "event_id": failure_id,
                "code": "CLEANUP_INCOMPLETE",
                "operation": "close_owned_tabs",
                "phase": "command",
                "extension_version": "2.2.0",
            }
            await ext.send(json.dumps({"type": "failure_batch", "schema": 1, "events": [event]}))
            empty_ack = json.loads(await ext.recv())
            await ext.send(
                json.dumps(
                    {
                        "id": command["id"],
                        "result": {
                            "requested": 1,
                            "confirmed": [],
                            "pending": [],
                            "refused": [],
                            "remaining": ["1"],
                            "failure_id": failure_id,
                        },
                    }
                )
            )
            fallback_ack = json.loads(await ext.recv())
            reply = await client
            await ext.close()
            return empty_ack, fallback_ack, reply

    empty_ack, fallback_ack, reply = run(scenario())
    report = failure_report(code="CLEANUP_INCOMPLETE")
    assert empty_ack["event_ids"] == []
    assert fallback_ack["event_ids"] == [failure_id]
    assert reply["result"]["failure_id"] == failure_id
    assert report["matching_events"] == 1
    assert report["events"][0]["failure_id"] == failure_id


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


def test_web_origin_rejection_burst_has_bounded_off_loop_journaling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(FAILURE_LOG_ENV, str(tmp_path / "journal" / "failures.jsonl"))

    async def scenario() -> dict:
        async with running_server() as (_server, port):

            async def rejected(index: int) -> None:
                ws = await connect(
                    f"ws://localhost:{port}",
                    additional_headers={"Origin": f"https://evil{index}.example"},
                )
                with contextlib.suppress(Exception):
                    await ws.recv()
                with contextlib.suppress(Exception):
                    await ws.close()

            await asyncio.gather(*(rejected(index) for index in range(20)))
            return await cli_call(port, {"method": "ping_server"})

    health = run(scenario())
    report = failure_report(code="UNAUTHORIZED")
    assert health["result"]["server_version"] == "2.2.0"
    assert report["matching_events"] == 1
    assert report["events"][0]["phase"] == "origin"


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
