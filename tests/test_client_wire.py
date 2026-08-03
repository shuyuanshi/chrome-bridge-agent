"""What BridgePage actually puts on the wire, checked against a fake relay.

The old smoke test only asserted that method *names* existed, which is why
three parameters that callers passed (``evaluate(timeout=)``,
``evaluate_function(*args)``, ``screenshot_element(selector, padding)``) were
accepted and then silently dropped. These tests pin the payloads.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from typing import Any

import pytest
from bridge_client import (
    BridgeError,
    BridgePage,
    BridgeTimeoutError,
    ElementNotFoundError,
    JSEvalError,
    StaleRefError,
    TabGoneError,
)
from websockets.sync.server import serve


class FakeRelay:
    """A stand-in bridge server: records every frame, replies from a callback."""

    def __init__(self, responder: Callable[[dict], dict] | None = None) -> None:
        self.frames: list[dict] = []
        self._responder = responder or (lambda _msg: {"result": None})
        self._server = None
        self._thread = None

    def __enter__(self) -> FakeRelay:
        def handler(ws) -> None:
            for raw in ws:
                msg = json.loads(raw)
                self.frames.append(msg)
                ws.send(json.dumps(self._responder(msg)))

        self._server = serve(handler, "localhost", 0)
        self.port = self._server.socket.getsockname()[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        assert self._server is not None
        # shutdown() alone leaves the listening socket open and the thread
        # alive; dozens of relays per suite then churn ephemeral ports and a
        # later test occasionally connects to a corpse (HTTP 400 / close 1005).
        self._server.shutdown()
        with contextlib.suppress(OSError):
            self._server.socket.close()
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"ws://localhost:{self.port}"

    def last(self, key: str) -> Any:
        return self.frames[-1].get(key)

    def last_params(self) -> dict:
        return self.frames[-1].get("params", {})


def page_for(relay: FakeRelay, **kwargs: Any) -> BridgePage:
    return BridgePage(relay.url, token="tok", **kwargs)


# ─────────────────── params that used to be dropped ───────────────────


def test_evaluate_timeout_reaches_the_wire() -> None:
    with FakeRelay() as relay:
        page_for(relay).evaluate("1+1", timeout=5)
        assert relay.last("deadline_ms") == 5000


def test_evaluate_function_sends_its_arguments() -> None:
    with FakeRelay(lambda m: {"result": m["params"]["args"]}) as relay:
        out = page_for(relay).evaluate_function("return arguments[0] + arguments[1];", 1, 2)
        assert relay.last_params()["args"] == [1, 2]
        assert out == [1, 2]


def test_screenshot_element_sends_selector_and_padding() -> None:
    with FakeRelay(lambda _m: {"result": {"data": ""}}) as relay:
        page_for(relay).screenshot_element("#chart", padding=7)
        assert relay.last("method") == "screenshot_element"
        assert relay.last_params() == {"selector": "#chart", "padding": 7}


def test_long_waits_get_a_deadline_above_the_wait() -> None:
    """A 120 s wait must not be capped by a 90 s default."""
    with FakeRelay() as relay:
        page_for(relay).wait_for_element("#slow", timeout=120)
        assert relay.last_params()["timeout"] == 120_000
        assert relay.last("deadline_ms") > 120_000


# ─────────────────── tab binding ───────────────────


def test_tab_id_is_injected_into_every_call() -> None:
    with FakeRelay(lambda _m: {"result": None}) as relay:
        bound = BridgePage(relay.url, token="tok", tab_id="42")
        bound.click_element("#go")
        assert relay.last_params()["tab_id"] == "42"
        bound.snapshot()
        assert relay.last_params()["tab_id"] == "42"


def test_unbound_page_sends_no_tab_id() -> None:
    with FakeRelay() as relay:
        page_for(relay).click_element("#go")
        assert "tab_id" not in relay.last_params()


def test_tab_context_manager_opens_and_closes() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "77", "url": "https://x/", "status": "ready"}}
        return {"result": None}

    with FakeRelay(responder) as relay:
        page = page_for(relay)
        with page.tab("https://x/") as tab:
            assert tab.tab_id == "77"
            tab.evaluate("1")
        methods = [f["method"] for f in relay.frames]
        assert methods == ["browse_open", "evaluate", "browse_close"]
        assert relay.frames[1]["params"]["tab_id"] == "77"


def test_named_session_is_left_open_on_exit() -> None:
    """A named session exists to outlive the block — closing it defeats the point."""

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "9", "url": "u", "status": "ready", "name": "dash"}}
        return {"result": None}

    with FakeRelay(responder) as relay:
        page = page_for(relay)
        with page.tab("https://x/", name="dash") as tab:
            tab.evaluate("1")
        assert "browse_close" not in [f["method"] for f in relay.frames]


def test_non_positive_timeout_cannot_invert_the_deadline() -> None:
    with FakeRelay() as relay:
        page_for(relay).evaluate("1", timeout=0)
        assert relay.last("deadline_ms") >= 1000


def test_tab_is_closed_even_when_the_body_raises() -> None:
    """The leak that 17 real scripts hit: browse_open with no matching close."""

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "5", "url": "u", "status": "ready"}}
        return {"result": None}

    with FakeRelay(responder) as relay:
        page = page_for(relay)
        with pytest.raises(ZeroDivisionError), page.tab("https://x/"):
            raise ZeroDivisionError
        assert [f["method"] for f in relay.frames][-1] == "browse_close"


# ─────────────────── error taxonomy ───────────────────


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("ELEMENT_NOT_FOUND", ElementNotFoundError),
        ("TIMEOUT", BridgeTimeoutError),
        ("TAB_GONE", TabGoneError),
        ("JS_ERROR", JSEvalError),
        ("STALE_REF", StaleRefError),
        ("SOMETHING_NEW", BridgeError),
    ],
)
def test_error_codes_map_to_exception_types(code: str, expected: type[BridgeError]) -> None:
    with FakeRelay(lambda _m: {"error": {"code": code, "message": "boom"}}) as relay:
        with pytest.raises(expected) as excinfo:
            page_for(relay).click_element("#nope")
        assert excinfo.value.code == code


def test_legacy_string_errors_still_raise_bridge_error() -> None:
    with FakeRelay(lambda _m: {"error": "元素不存在: #x"}) as relay:
        with pytest.raises(BridgeError) as excinfo:
            page_for(relay).click_element("#x")
        assert "元素不存在" in str(excinfo.value)


def test_js_error_carries_the_stack() -> None:
    payload = {
        "error": {
            "code": "JS_ERROR",
            "message": "x is not defined",
            "detail": {"stack": "at <anonymous>"},
        }
    }
    with FakeRelay(lambda _m: payload) as relay:
        with pytest.raises(JSEvalError) as excinfo:
            page_for(relay).evaluate("x")
        assert excinfo.value.detail["stack"] == "at <anonymous>"


def test_connection_failure_is_not_reported_as_a_timeout() -> None:
    from bridge_client import BridgeConnectionError

    page = BridgePage("ws://localhost:1", token="tok")
    with pytest.raises(BridgeConnectionError):
        page.evaluate("1")


# ─────────────────── auth ───────────────────


def test_token_is_sent_and_can_be_refreshed() -> None:
    attempts: list[str | None] = []

    def responder(msg: dict) -> dict:
        attempts.append(msg.get("token"))
        return {"result": "ok"}

    with FakeRelay(responder) as relay:
        page_for(relay).evaluate("1")
        assert attempts == ["tok"]


def test_unauthorized_is_typed() -> None:
    from bridge_client import BridgeAuthError

    payload = {"error": {"code": "UNAUTHORIZED", "message": "bad token"}}
    with FakeRelay(lambda _m: payload) as relay, pytest.raises(BridgeAuthError):
        page_for(relay).evaluate("1")


def test_unauthorized_refreshes_the_token_from_disk_once(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server may have restarted (new token) after this client was built."""
    token_file = tmp_path / "token"
    token_file.write_text("fresh-token\n", encoding="utf-8")
    monkeypatch.setenv("CHROME_BRIDGE_TOKEN_FILE", str(token_file))
    monkeypatch.delenv("CHROME_BRIDGE_TOKEN", raising=False)

    seen: list[str | None] = []

    def responder(msg: dict) -> dict:
        seen.append(msg.get("token"))
        if msg.get("token") != "fresh-token":
            return {"error": {"code": "UNAUTHORIZED", "message": "stale"}}
        return {"result": "ok"}

    with FakeRelay(responder) as relay:
        page = BridgePage(relay.url, token="stale-token")
        assert page.evaluate("1") == "ok"
        assert seen == ["stale-token", "fresh-token"]


def test_unauthorized_does_not_retry_forever(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHROME_BRIDGE_TOKEN_FILE", str(tmp_path / "missing"))
    monkeypatch.delenv("CHROME_BRIDGE_TOKEN", raising=False)
    payload = {"error": {"code": "UNAUTHORIZED", "message": "nope"}}

    with FakeRelay(lambda _m: payload) as relay:
        page = BridgePage(relay.url, token="whatever")
        with pytest.raises(BridgeError):
            page.evaluate("1")
        assert len(relay.frames) == 1


# ─────────────────── chunked reads ───────────────────


def test_fetch_drains_a_buffered_body() -> None:
    body = "y" * 2500

    def responder(msg: dict) -> dict:
        if msg["method"] == "page_fetch":
            return {
                "result": {
                    "status": 200,
                    "ok": True,
                    "url": "u",
                    "length": len(body),
                    "buffered": True,
                }
            }
        start = msg["params"]["start"]
        # Serve at most 1000 chars per read so the paging loop is exercised.
        # No "next" key here on purpose: that is what a pre-1.1 extension
        # returns, and the client must still page correctly.
        length = min(msg["params"]["length"], 1000)
        return {"result": {"chunk": body[start : start + length], "total": len(body)}}

    with FakeRelay(responder) as relay:
        res = page_for(relay).fetch("https://internal/api")
        assert res["body"] == body
        assert res.get("buffered") is None
        assert sum(1 for f in relay.frames if f["method"] == "read_buffer") == 3


def test_fetch_raises_on_http_error() -> None:
    payload = {"result": {"status": 403, "ok": False, "url": "u", "length": 2, "body": "no"}}
    with FakeRelay(lambda _m: payload) as relay:
        with pytest.raises(BridgeError) as excinfo:
            page_for(relay).fetch("https://internal/api")
        assert excinfo.value.code == "HTTP_ERROR"


def _utf16_slice(text: str, start: int, length: int) -> tuple[str, int, int]:
    """Slice the way the page does: UTF-16 code units, and never through a
    surrogate pair (a half-emoji cannot be reassembled on the Python side)."""
    units = text.encode("utf-16-le")
    total = len(units) // 2
    end = min(total, start + length)
    if end < total:
        last = int.from_bytes(units[(end - 1) * 2 : end * 2], "little")
        if 0xD800 <= last <= 0xDBFF:
            end -= 1
    if end <= start:
        end = min(total, start + 2)
    piece = units[start * 2 : end * 2].decode("utf-16-le")
    return piece, end, total


def test_read_text_pages_through_chunks() -> None:
    text = "abcdefghij"

    def responder(msg: dict) -> dict:
        piece, nxt, total = _utf16_slice(text, msg["params"]["start"], msg["params"]["length"])
        return {"result": {"text": piece, "next": nxt, "total": total}}

    with FakeRelay(responder) as relay:
        assert page_for(relay).read_text(chunk=3) == text
        assert len(relay.frames) == 4


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 8, 25])
def test_read_text_survives_astral_characters(chunk: int) -> None:
    """JS counts UTF-16 units, Python counts code points — emoji desync both.

    Every chunk size matters: the interesting ones land mid-surrogate-pair.
    """
    text = "hello 😀😀😀😀😀 world 漢字"

    def responder(msg: dict) -> dict:
        piece, nxt, total = _utf16_slice(text, msg["params"]["start"], msg["params"]["length"])
        return {"result": {"text": piece, "next": nxt, "total": total}}

    with FakeRelay(responder) as relay:
        assert page_for(relay).read_text(chunk=chunk) == text


def test_drain_buffer_survives_astral_characters() -> None:
    body = "x😀" * 300

    def responder(msg: dict) -> dict:
        if msg["method"] == "page_fetch":
            total = len(body.encode("utf-16-le")) // 2
            return {
                "result": {"status": 200, "ok": True, "url": "u", "length": total, "buffered": True}
            }
        piece, nxt, total = _utf16_slice(
            body, msg["params"]["start"], min(msg["params"]["length"], 97)
        )
        return {"result": {"chunk": piece, "next": nxt, "total": total}}

    with FakeRelay(responder) as relay:
        assert page_for(relay).fetch("https://internal/api")["body"] == body


def test_snapshot_text_renders_one_line_per_element() -> None:
    snap = {
        "snapshot_id": "s1",
        "url": "https://x/",
        "title": "T",
        "elements": [
            {"ref": 0, "tag": "button", "role": "button", "name": "Save"},
            {"ref": 1, "tag": "input", "role": "checkbox", "name": "Agree", "checked": False},
            # A text input carries no `checked` key, so it must not be labelled.
            {"ref": 2, "tag": "input", "role": "email", "name": "Email"},
        ],
    }
    with FakeRelay(lambda _m: {"result": snap}) as relay:
        out = page_for(relay).snapshot_text()
        assert "[0] button 'Save'" in out
        assert "[1] checkbox 'Agree' unchecked" in out
        assert "[2] email 'Email'" in out
        assert "checked" not in out.splitlines()[-1]


# ─────────────────── CLI ───────────────────


def test_cli_routes_each_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    from bridge_client import main

    def responder(msg: dict) -> dict:
        method = msg["method"]
        if method == "browse_open":
            return {"result": {"tab_id": "3", "url": "u", "status": "ready"}}
        if method == "get_text":
            return {"result": {"text": "hi", "next": 2, "total": 2}}
        if method == "snapshot":
            return {"result": {"snapshot_id": "s", "url": "u", "title": "T", "elements": []}}
        if method == "page_fetch":
            return {"result": {"status": 200, "ok": True, "url": "u", "length": 2, "body": "{}"}}
        if method == "ping_server":
            return {"result": {"extension_connected": True}}
        if method == "list_tabs":
            return {"result": [{"tab_id": "3", "url": "u"}]}
        return {"result": "42"}

    with FakeRelay(responder) as relay:
        base = ["--bridge-url", relay.url]
        assert main([*base, "eval", "document.title"]) == 0
        assert "42" in capsys.readouterr().out

        assert main([*base, "--url", "https://x/", "text"]) == 0
        assert "hi" in capsys.readouterr().out

        assert main([*base, "list-tabs"]) == 0
        assert "tab_id" in capsys.readouterr().out

        assert main([*base, "fetch", "https://x/api", "--json"]) == 0
        capsys.readouterr()

        methods = [f["method"] for f in relay.frames]
        # --url must open and dispose of a temp tab around the command
        assert methods.count("browse_open") == 1
        assert methods.count("browse_close") == 1


def test_cli_writes_a_screenshot_file(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    import base64

    from bridge_client import main

    png = base64.b64encode(b"\x89PNG-not-really").decode()
    out_file = tmp_path / "shot.png"
    with FakeRelay(lambda _m: {"result": {"data": png}}) as relay:
        code = main(
            ["--bridge-url", relay.url, "screenshot", "--selector", "#x", "--out", str(out_file)]
        )
        assert code == 0
        assert out_file.read_bytes().startswith(b"\x89PNG")
        capsys.readouterr()
