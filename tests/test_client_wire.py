"""What BridgePage actually puts on the wire, checked against a fake relay.

The old smoke test only asserted that method *names* existed, which is why
three parameters that callers passed (``evaluate(timeout=)``,
``evaluate_function(*args)``, ``screenshot_element(selector, padding)``) were
accepted and then silently dropped. These tests pin the payloads.
"""

from __future__ import annotations

import contextlib
import json
import logging
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
                try:
                    msg = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    # Ephemeral ports get recycled under load; a stray frame
                    # from someone else's connection must not kill this thread.
                    continue
                if not (isinstance(msg, dict) and msg.get("role") == "cli"):
                    # Not one of ours either — don't let it pollute `frames`
                    # and break an assertion in a way that looks like a bug.
                    continue
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


@pytest.mark.parametrize("domain", ["", "   "])
def test_cookie_export_requires_an_explicit_scope(domain: str) -> None:
    with FakeRelay() as relay:
        with pytest.raises(ValueError, match="cookie scope required"):
            page_for(relay).get_cookies(domain)
        assert relay.frames == []


def test_cookie_export_sends_only_the_requested_domain() -> None:
    with FakeRelay(lambda _m: {"result": []}) as relay:
        page_for(relay).get_cookies(" example.com ")
        assert relay.last("method") == "get_cookies"
        assert relay.last_params() == {"domain": "example.com"}


def test_all_domain_cookie_export_requires_an_explicit_opt_in() -> None:
    with FakeRelay(lambda _m: {"result": []}) as relay:
        page_for(relay).get_cookies(all_domains=True)
        assert relay.last("method") == "get_cookies"
        assert relay.last_params() == {"all_domains": True}


def test_cookie_export_rejects_conflicting_or_invalid_scope() -> None:
    with FakeRelay() as relay:
        with pytest.raises(ValueError, match="mutually exclusive"):
            page_for(relay).get_cookies("example.com", all_domains=True)
        with pytest.raises(TypeError, match="domain must be a string"):
            page_for(relay).get_cookies(None)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="all_domains must be a bool"):
            page_for(relay).get_cookies(all_domains="yes")  # type: ignore[arg-type]
        assert relay.frames == []


def test_debug_logging_does_not_emit_cookie_values_or_bridge_tokens(
    capsys: pytest.CaptureFixture[str],
) -> None:
    import bridge_client

    root_logger = logging.getLogger()
    websocket_logger = logging.getLogger("websockets")
    websocket_client_logger = logging.getLogger("websockets.client")
    bridge_logger = bridge_client._LOG
    saved = {
        "root_level": root_logger.level,
        "websocket_level": websocket_logger.level,
        "websocket_client_level": websocket_client_logger.level,
        "bridge_level": bridge_logger.level,
        "bridge_handlers": list(bridge_logger.handlers),
        "bridge_propagate": bridge_logger.propagate,
    }

    try:
        root_logger.setLevel(logging.DEBUG)
        websocket_logger.setLevel(logging.NOTSET)
        websocket_client_logger.setLevel(logging.DEBUG)
        bridge_logger.handlers.clear()
        bridge_logger.propagate = True
        bridge_client._configure_debug_logging()

        with FakeRelay(
            lambda _msg: {"result": [{"name": "sid", "value": "TOPSECRET_COOKIE"}]}
        ) as relay:
            BridgePage(relay.url, token="TOPSECRET_TOKEN").get_cookies("example.com")

        stderr = capsys.readouterr().err
        assert "get_cookies" in stderr
        assert "<redacted>" in stderr
        assert "TOPSECRET_COOKIE" not in stderr
        assert "TOPSECRET_TOKEN" not in stderr
        assert websocket_logger.getEffectiveLevel() >= logging.WARNING
        assert websocket_client_logger.getEffectiveLevel() >= logging.WARNING
    finally:
        for handler in bridge_logger.handlers:
            if handler not in saved["bridge_handlers"]:
                handler.close()
        bridge_logger.handlers[:] = saved["bridge_handlers"]
        bridge_logger.setLevel(saved["bridge_level"])
        bridge_logger.propagate = saved["bridge_propagate"]
        websocket_client_logger.setLevel(saved["websocket_client_level"])
        websocket_logger.setLevel(saved["websocket_level"])
        root_logger.setLevel(saved["root_level"])


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


def test_scope_is_opaque_stable_and_sent_on_every_call() -> None:
    with FakeRelay() as relay:
        first = page_for(relay, scope_id="agent-a")
        second = page_for(relay, scope_id="agent-a")
        other = page_for(relay, scope_id="agent-b")
        first.evaluate("1")
        second.evaluate("2")
        other.evaluate("3")

        assert first.scope_id == second.scope_id
        assert first.scope_id != other.scope_id
        assert first.scope_id.startswith("cb-")
        assert [frame["scope_id"] for frame in relay.frames] == [
            first.scope_id,
            first.scope_id,
            other.scope_id,
        ]


def test_legacy_scope_is_reserved_for_upgrade_cleanup() -> None:
    assert BridgePage(scope_id="legacy").scope_id == "legacy"


def test_tab_inherits_its_parent_scope() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "77", "url": "https://x/", "status": "ready"}}
        return {"result": None}

    with FakeRelay(responder) as relay:
        page = page_for(relay, scope_id="agent-a")
        with page.tab("https://x/") as tab:
            tab.evaluate("1")
        assert {frame["scope_id"] for frame in relay.frames} == {page.scope_id}


def test_page_context_cleans_its_scope_even_when_body_raises() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 0, "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {"result": []}
        return {"result": None}

    with (
        FakeRelay(responder) as relay,
        pytest.raises(ZeroDivisionError),
        page_for(relay, scope_id="agent-a"),
    ):
        raise ZeroDivisionError
    assert [frame["method"] for frame in relay.frames] == ["close_owned_tabs", "list_tabs"]


def test_page_context_reports_tabs_left_open_after_cleanup() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 1, "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {
                "result": [{"tab_id": "2", "bridge_owned": True, "bridge_owned_by_scope": True}]
            }
        return {"result": None}

    with (
        FakeRelay(responder) as relay,
        pytest.raises(BridgeError) as excinfo,
        page_for(relay, scope_id="agent-a"),
    ):
        pass
    assert excinfo.value.code == "TAB_CLEANUP_FAILED"


def test_close_owned_tabs_ignores_other_scopes() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 1, "confirmed": ["1"], "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {
                "result": [
                    {"tab_id": "2", "bridge_owned": True, "bridge_owned_by_scope": False},
                    {"tab_id": "3", "bridge_owned": False, "bridge_owned_by_scope": False},
                ]
            }
        return {"result": None}

    with FakeRelay(responder) as relay:
        page = page_for(relay, scope_id="agent-a")
        result = page.close_owned_tabs()
        assert result["confirmed"] == ["1"]
        assert result["remaining"] == []
        assert {frame["scope_id"] for frame in relay.frames} == {page.scope_id}


def test_close_owned_tabs_wait_false_returns_accepted_pending_tabs() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "close_owned_tabs":
            return {
                "result": {
                    "requested": 1,
                    "confirmed": [],
                    "pending": ["1"],
                    "refused": [],
                }
            }
        if msg["method"] == "list_tabs":
            return {
                "result": [{"tab_id": "1", "bridge_owned": True, "bridge_owned_by_scope": True}]
            }
        return {"result": None}

    with FakeRelay(responder) as relay:
        result = page_for(relay, scope_id="agent-a").close_owned_tabs(wait=False)
    assert result["pending"] == ["1"]
    assert result["remaining"] == ["1"]


def test_foreground_actions_are_opt_in_on_the_wire() -> None:
    with FakeRelay() as relay:
        page = page_for(relay, tab_id="4")
        page.cdp_mouse("move", x=1, y=2)
        assert relay.last_params()["activate"] is False
        page.activate_tab()
        assert relay.last_params()["allow_foreground"] is False


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


def test_tab_context_reports_a_close_failure_when_the_body_succeeds() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "5", "url": "u", "status": "ready"}}
        if msg["method"] == "browse_close":
            return {"error": {"code": "TAB_CLOSE_FAILED", "message": "refused"}}
        return {"result": None}

    with (
        FakeRelay(responder) as relay,
        pytest.raises(BridgeError) as excinfo,
        page_for(relay).tab("https://x/"),
    ):
        pass
    assert excinfo.value.code == "TAB_CLOSE_FAILED"


def test_browse_and_eval_reports_cleanup_failure_after_success() -> None:
    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "5", "url": "u", "status": "ready"}}
        if msg["method"] == "browse_do":
            return {"result": "answer"}
        if msg["method"] == "browse_close":
            return {"error": {"code": "TAB_CLOSE_FAILED", "message": "refused"}}
        return {"result": None}

    with FakeRelay(responder) as relay, pytest.raises(BridgeError) as excinfo:
        page_for(relay).browse_and_eval("https://x/", "1")
    assert excinfo.value.code == "TAB_CLOSE_FAILED"


def test_numeric_session_names_are_rejected() -> None:
    with FakeRelay() as relay, pytest.raises(ValueError, match="non-numeric"):
        page_for(relay).browse_open("https://x/", name="123")


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
        if method == "get_cookies":
            return {"result": [{"name": "sid", "value": "secret", "domain": "example.com"}]}
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

        assert main([*base, "cookies", "--domain", "example.com"]) == 0
        domain_output = capsys.readouterr().out
        assert "<redacted>" in domain_output
        assert "secret" not in domain_output

        assert main([*base, "cookies", "--all-domains"]) == 0
        all_output = capsys.readouterr().out
        assert "<redacted>" in all_output
        assert "secret" not in all_output

        methods = [f["method"] for f in relay.frames]
        # --url must open and dispose of a temp tab around the command
        assert methods.count("browse_open") == 1
        assert methods.count("browse_close") == 1
        cookie_frames = [frame for frame in relay.frames if frame["method"] == "get_cookies"]
        assert [frame["params"] for frame in cookie_frames] == [
            {"domain": "example.com"},
            {"all_domains": True},
        ]


def test_cli_cookie_export_requires_exactly_one_scope() -> None:
    from bridge_client import main

    with pytest.raises(SystemExit) as missing:
        main(["cookies"])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as conflicting:
        main(["cookies", "--domain", "example.com", "--all-domains"])
    assert conflicting.value.code == 2

    with pytest.raises(SystemExit) as empty:
        main(["cookies", "--domain", "   "])
    assert empty.value.code == 2


def test_cli_persistent_sessions_and_cleanup_require_an_explicit_scope() -> None:
    from bridge_client import main

    with pytest.raises(SystemExit) as session_error:
        main(["--session", "dash", "snapshot"])
    assert session_error.value.code == 2

    with pytest.raises(SystemExit) as cleanup_error:
        main(["cleanup"])
    assert cleanup_error.value.code == 2

    with pytest.raises(SystemExit) as listing_error:
        main(["list-sessions"])
    assert listing_error.value.code == 2


def test_cli_existing_named_session_keeps_the_explicit_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bridge_client import BridgePage, main

    expected_scope = BridgePage(scope_id="agent-a").scope_id

    def responder(msg: dict) -> dict:
        assert msg["scope_id"] == expected_scope
        assert msg["params"]["tab_id"] == "dash"
        return {"result": "Dashboard"}

    with FakeRelay(responder) as relay:
        assert (
            main(
                [
                    "--bridge-url",
                    relay.url,
                    "--scope",
                    "agent-a",
                    "--session",
                    "dash",
                    "eval",
                    "document.title",
                ]
            )
            == 0
        )
        assert capsys.readouterr().out.strip() == "Dashboard"


def test_cli_anonymous_url_does_not_clean_a_persistent_scope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bridge_client import BridgePage, main

    persistent_scope = BridgePage(scope_id="task-a").scope_id

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "3", "url": "u", "status": "ready"}}
        if msg["method"] == "browse_close":
            return {"result": {"closed": True, "confirmed": True}}
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 0, "confirmed": [], "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {"result": []}
        return {"result": "Title"}

    with FakeRelay(responder) as relay:
        assert (
            main(
                [
                    "--bridge-url",
                    relay.url,
                    "--scope",
                    "task-a",
                    "--url",
                    "https://x/",
                    "eval",
                    "document.title",
                ]
            )
            == 0
        )
        capsys.readouterr()
        cleanup_frames = [frame for frame in relay.frames if frame["method"] == "close_owned_tabs"]
        assert cleanup_frames
        assert all(frame["scope_id"] != persistent_scope for frame in cleanup_frames)


def test_cli_anonymous_url_ignores_persistent_env_scope_for_cleanup(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    from bridge_client import BridgePage, main

    monkeypatch.setenv("CHROME_BRIDGE_SCOPE", "task-from-env")
    persistent_scope = BridgePage().scope_id

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "3", "url": "u", "status": "ready"}}
        if msg["method"] == "browse_close":
            return {"result": {"closed": True, "confirmed": True}}
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 0, "confirmed": [], "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {"result": []}
        return {"result": "Title"}

    with FakeRelay(responder) as relay:
        assert main(["--bridge-url", relay.url, "--url", "https://x/", "eval", "1"]) == 0
        capsys.readouterr()
        cleanup_frames = [frame for frame in relay.frames if frame["method"] == "close_owned_tabs"]
        assert cleanup_frames
        assert all(frame["scope_id"] != persistent_scope for frame in cleanup_frames)


def test_cli_cleans_temp_scope_when_command_raises_a_non_bridge_error() -> None:
    from bridge_client import main

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "3", "url": "u", "status": "ready"}}
        if msg["method"] == "page_fetch":
            return {
                "result": {
                    "status": 200,
                    "ok": True,
                    "url": "u",
                    "length": 8,
                    "body": "not-json",
                }
            }
        if msg["method"] == "browse_close":
            return {"result": {"closed": True, "confirmed": True}}
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 0, "confirmed": [], "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {"result": []}
        return {"result": None}

    with FakeRelay(responder) as relay, pytest.raises(json.JSONDecodeError):
        main(
            [
                "--bridge-url",
                relay.url,
                "--url",
                "https://x/",
                "fetch",
                "https://x/api",
                "--json",
            ]
        )
    methods = [frame["method"] for frame in relay.frames]
    assert "browse_close" in methods
    assert "close_owned_tabs" in methods


def test_cli_returns_nonzero_when_one_shot_cleanup_fails(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bridge_client import main

    def responder(msg: dict) -> dict:
        if msg["method"] == "browse_open":
            return {"result": {"tab_id": "3", "url": "u", "status": "ready"}}
        if msg["method"] == "browse_close":
            return {"error": {"code": "TAB_CLOSE_FAILED", "message": "refused"}}
        if msg["method"] == "close_owned_tabs":
            return {
                "result": {
                    "requested": 1,
                    "confirmed": [],
                    "pending": [],
                    "refused": [{"tab_id": "3", "error": "refused"}],
                }
            }
        if msg["method"] == "list_tabs":
            return {
                "result": [{"tab_id": "3", "bridge_owned": True, "bridge_owned_by_scope": True}]
            }
        return {"result": "Title"}

    with FakeRelay(responder) as relay:
        assert main(["--bridge-url", relay.url, "--url", "https://x/", "eval", "1"]) == 1
        assert "TAB_CLEANUP_FAILED" in capsys.readouterr().err


def test_cli_cleanup_targets_only_the_named_scope(capsys: pytest.CaptureFixture[str]) -> None:
    from bridge_client import main

    def responder(msg: dict) -> dict:
        if msg["method"] == "close_owned_tabs":
            return {"result": {"requested": 0, "pending": [], "refused": []}}
        if msg["method"] == "list_tabs":
            return {"result": []}
        return {"result": None}

    with FakeRelay(responder) as relay:
        assert main(["--bridge-url", relay.url, "--scope", "task-a", "cleanup"]) == 0
        capsys.readouterr()
        assert [frame["method"] for frame in relay.frames] == ["close_owned_tabs", "list_tabs"]
        assert len({frame["scope_id"] for frame in relay.frames}) == 1


def test_cli_cookie_values_require_an_explicit_reveal_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from bridge_client import main

    def responder(msg: dict) -> dict:
        assert msg["params"] == {"domain": "example.com"}
        return {"result": [{"name": "sid", "value": "secret"}]}

    with FakeRelay(responder) as relay:
        base = ["--bridge-url", relay.url, "cookies", "--domain", "example.com"]
        assert main(base) == 0
        redacted = capsys.readouterr().out
        assert "<redacted>" in redacted
        assert "secret" not in redacted

        assert main([*base, "--show-values"]) == 0
        revealed = capsys.readouterr().out
        assert "secret" in revealed
        assert "<redacted>" not in revealed


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


def test_a_non_bridge_reply_is_reported_as_a_connection_problem() -> None:
    """If something else is squatting the port, say that — don't leak a
    UnicodeDecodeError out of json.loads."""
    import threading

    from websockets.sync.server import serve as sync_serve

    def handler(ws) -> None:
        for _ in ws:
            ws.send(b"\xff\xfe not json at all")

    server = sync_serve(handler, "localhost", 0)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        from bridge_client import BridgeConnectionError

        page = BridgePage(f"ws://localhost:{port}", token="tok")
        with pytest.raises(BridgeConnectionError) as excinfo:
            page.evaluate("1")
        assert "isn't a bridge reply" in str(excinfo.value)
    finally:
        server.shutdown()
        with contextlib.suppress(OSError):
            server.socket.close()
        thread.join(timeout=5)
