"""BridgePage - Python client for the Chrome Bridge extension.

Talks to bridge_server.py over WebSocket; the server forwards each command to
the Chrome extension, which executes it in the target tab and returns the
result.

Three ways to use it:

    page = BridgePage()

    # 1. one-shot
    title = page.browse_and_eval("https://example.com", "document.title")

    # 2. a session (recommended for multi-step SPA work) — the tab is closed
    #    for you, and every verb below targets *that* tab
    with page.tab("https://example.com") as tab:
        tab.click_element("#filter")
        rows = tab.evaluate("document.querySelectorAll('tr').length")

    # 3. a scope-local managed tab
    page.navigate("https://example.com")
    page.evaluate("document.title")

Wrap a complete task in ``with BridgePage() as page:`` to close every tab
opened by that page's ownership scope on success or failure. Separate page
instances get separate scopes, so parallel agents cannot reuse or clean up one
another's tabs.

Set CHROME_BRIDGE_DEBUG=1 to log every RPC (method, params, elapsed, reply) from
the bridge logger to stderr. Cookie replies are redacted, but *parameters* are
logged verbatim — so don't leave it on for a run that types a credential.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import time
from typing import Any

import websockets.sync.client as ws_client
from bridge_auth import read_token
from websockets.exceptions import WebSocketException

BRIDGE_URL = "ws://localhost:9333"

DEFAULT_TIMEOUT = 90.0
# The server must time out first so the caller gets a structured TIMEOUT error
# rather than a socket read timeout mislabelled as a connection failure.
CLIENT_GRACE = 15.0

_LOG = logging.getLogger("chrome_bridge")

# Replies whose body is a credential. Debug logging still records the call —
# just not what came back. (Params are logged; don't debug-log a run that types
# a password unless you are happy to have it on stderr.)
_SECRET_RESULTS = {"get_cookies"}


class BridgeError(Exception):
    """Raised on any Bridge communication failure.

    ``code`` carries the machine-readable reason (``ELEMENT_NOT_FOUND``,
    ``TIMEOUT``, ``TAB_GONE``, ...) so callers can branch instead of
    substring-matching an error message.
    """

    code = "BRIDGE_ERROR"

    def __init__(self, message: str, *, code: str | None = None, detail: Any = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.detail = detail


class BridgeConnectionError(BridgeError):
    """The bridge server itself is unreachable."""

    code = "CONNECTION_FAILED"


class BridgeAuthError(BridgeError):
    """The server refused the token (or the Origin)."""

    code = "UNAUTHORIZED"


class ExtensionNotConnectedError(BridgeError):
    """The relay is up but no Chrome extension is registered."""

    code = "EXTENSION_NOT_CONNECTED"


class BridgeTimeoutError(BridgeError):
    """A command exceeded its deadline."""

    code = "TIMEOUT"


class ElementNotFoundError(BridgeError):
    """A CSS selector did not match any element."""

    code = "ELEMENT_NOT_FOUND"


class TabGoneError(BridgeError):
    """The target tab was closed, or the browser restarted."""

    code = "TAB_GONE"


class JSEvalError(BridgeError):
    """The evaluated JavaScript threw. ``detail['stack']`` has the trace."""

    code = "JS_ERROR"


class StaleRefError(BridgeError):
    """A snapshot ref no longer points at a live element — re-snapshot."""

    code = "STALE_REF"


class NavigationTimeoutError(BridgeError):
    """A page did not finish loading in time."""

    code = "NAV_TIMEOUT"


class ForegroundRequiredError(BridgeError):
    """The requested action would bring Chrome to the foreground."""

    code = "FOREGROUND_REQUIRED"


class TabNotOwnedError(BridgeError):
    """A close targeted a user tab rather than a tab created by this scope."""

    code = "TAB_NOT_OWNED"


class TabScopeMismatchError(BridgeError):
    """An operation targeted a tab owned by another bridge scope."""

    code = "TAB_SCOPE_MISMATCH"


_ERROR_TYPES: dict[str, type[BridgeError]] = {
    cls.code: cls
    for cls in (
        BridgeConnectionError,
        BridgeAuthError,
        ExtensionNotConnectedError,
        BridgeTimeoutError,
        ElementNotFoundError,
        TabGoneError,
        JSEvalError,
        StaleRefError,
        NavigationTimeoutError,
        ForegroundRequiredError,
        TabNotOwnedError,
        TabScopeMismatchError,
    )
}


def _make_scope_id(value: str | None = None) -> str:
    """Return a short opaque wire ID; explicit names are stable but not exposed."""
    raw = value or os.environ.get("CHROME_BRIDGE_SCOPE")
    # Reserved upgrade escape hatch for tabs persisted by pre-2.1 extensions.
    if raw == "legacy":
        return raw
    if raw and raw.startswith("cb-") and len(raw) == 27:
        return raw
    if not raw:
        raw = secrets.token_urlsafe(24)
    return "cb-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _validate_session_name(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("session name must be a string")
    if not value.strip() or value.isdecimal() or "\x1f" in value:
        raise ValueError(
            "session name must be a non-empty, non-numeric string without control separators"
        )
    return value


def _raise_for(error: Any) -> None:
    """Turn the wire error into the most specific exception we have."""
    if isinstance(error, dict):
        code = str(error.get("code") or "BRIDGE_ERROR")
        message = str(error.get("message") or error)
        detail = error.get("detail")
    else:  # pre-1.1 servers/extensions sent a bare string
        code, message, detail = "BRIDGE_ERROR", str(error), None
    raise _ERROR_TYPES.get(code, BridgeError)(message, code=code, detail=detail)


class BridgePage:
    """Browser automation via the Chrome Bridge extension.

    Each method opens a short-lived WebSocket connection to the bridge server,
    sends one command, and waits for the reply. No long-lived state is kept
    on the client side.

    When ``tab_id`` is set (see :meth:`tab`), every command targets that tab
    instead of this scope's managed tab.
    """

    def __init__(
        self,
        bridge_url: str = BRIDGE_URL,
        *,
        tab_id: str | None = None,
        scope_id: str | None = None,
        token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._bridge_url = bridge_url
        self._tab_id = tab_id
        self._scope_id = _make_scope_id(scope_id)
        self._token = token if token is not None else read_token()
        self._timeout = timeout

    @property
    def scope_id(self) -> str:
        """Opaque ownership scope used to isolate tabs from parallel agents."""
        return self._scope_id

    def __enter__(self) -> BridgePage:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        try:
            result = self.close_owned_tabs()
        except BridgeError:
            if exc_type is None:
                raise
            _LOG.warning("could not clean up Chrome Bridge scope %s", self._scope_id)
            return
        refused = result.get("refused") or []
        remaining = result.get("remaining") or []
        if refused or remaining:
            count = len(remaining) or len(refused)
            message = f"Chrome left {count} bridge-owned tab(s) open after task cleanup"
            if exc_type is None:
                raise BridgeError(message, code="TAB_CLEANUP_FAILED", detail=result)
            _LOG.warning("%s", message)

    # ─── internal RPC ───────────────────────────────────────────

    def _call(
        self, method: str, params: dict | None = None, *, timeout: float | None = None
    ) -> Any:
        """Send one command to the bridge server and wait for the reply."""
        deadline = timeout if timeout is not None else self._timeout
        # A non-positive deadline would make the client give up before the
        # server does, orphaning a command that is still running in the page.
        deadline = max(1.0, float(deadline))
        payload: dict[str, Any] = {
            "role": "cli",
            "method": method,
            "scope_id": self._scope_id,
            "deadline_ms": int(deadline * 1000),
        }
        merged = dict(params or {})
        if self._tab_id is not None:
            merged.setdefault("tab_id", self._tab_id)
        if merged:
            payload["params"] = merged

        started = time.monotonic()
        resp = self._roundtrip(payload, deadline)
        if _LOG.isEnabledFor(logging.DEBUG):
            _LOG.debug(
                "%s(%s) -> %s in %.2fs",
                method,
                _truncate(merged),
                "<redacted>" if method in _SECRET_RESULTS else _truncate(resp),
                time.monotonic() - started,
            )

        if resp.get("error"):
            _raise_for(resp["error"])
        return resp.get("result")

    def _roundtrip(self, payload: dict[str, Any], deadline: float) -> dict:
        """One connect → send → recv cycle, retrying once if the token is stale."""
        for attempt in (0, 1):
            body = dict(payload)
            if self._token:
                body["token"] = self._token
            try:
                with ws_client.connect(self._bridge_url, max_size=None, open_timeout=10) as ws:
                    ws.send(json.dumps(body, ensure_ascii=False))
                    raw = ws.recv(timeout=deadline + CLIENT_GRACE)
            except TimeoutError as e:
                raise BridgeTimeoutError(
                    f"no reply from the bridge server within {deadline + CLIENT_GRACE:.0f}s "
                    f"(the server should have answered first — is it wedged?)"
                ) from e
            except (OSError, WebSocketException) as e:
                raise BridgeConnectionError(
                    f"could not talk to the bridge server at {self._bridge_url}: {e}. "
                    "Start it with `chrome-bridge-server`, or from a source checkout with "
                    "`python3 scripts/bridge_server.py`."
                ) from e

            try:
                resp = json.loads(raw)
                if not isinstance(resp, dict):
                    raise ValueError("reply was not a JSON object")
            except (ValueError, UnicodeDecodeError) as e:
                # Whatever answered on this port is not a bridge server. Say so,
                # instead of leaking a UnicodeDecodeError from json.loads.
                raise BridgeConnectionError(
                    f"{self._bridge_url} answered with something that isn't a bridge reply "
                    f"({e}). Is another process listening on that port?"
                ) from e

            error = resp.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            if code == "UNAUTHORIZED" and attempt == 0:
                # The server may have been started (and written its token file)
                # after this client was constructed.
                fresh = read_token()
                if fresh and fresh != self._token:
                    self._token = fresh
                    continue
            return resp
        raise AssertionError("unreachable")  # pragma: no cover

    # ─── sessions ───────────────────────────────────────────────

    def tab(
        self,
        url: str,
        *,
        name: str | None = None,
        wait_selector: str | None = None,
        timeout: float = 60.0,
        settle_ms: int = 2000,
    ) -> Tab:
        """Open (or reuse) a tab and return a :class:`Tab` bound to it.

        Usable as a context manager, which is the point — the old
        ``browse_open`` / ``browse_close`` pair leaked a background tab every
        time an exception fired in between::

            with page.tab("https://example.com") as t:
                t.click_element("#go")

        Pass ``name=`` to reuse the same tab across calls in this scope instead
        of opening a new one each time. A *named* tab is deliberately left open
        when the block exits — call ``.close()`` to dispose of it.
        """
        info = self.browse_open(
            url,
            timeout=int(timeout * 1000),
            name=name,
            wait_selector=wait_selector,
            settle_ms=settle_ms,
        )
        return Tab(
            self._bridge_url,
            tab_id=info["tab_id"],
            scope_id=self._scope_id,
            token=self._token,
            timeout=self._timeout,
            url=info.get("url"),
            name=name,
        )

    def list_sessions(self) -> list[dict]:
        """Named sessions owned by this page's scope."""
        result = self._call("list_sessions")
        return result if isinstance(result, list) else []

    def list_tabs(self, url_contains: str = "") -> list[dict]:
        """Every tab in the browser, optionally filtered by URL substring.

        Lets a script attach to a tab the *user* already has open:
        ``BridgePage(tab_id=page.list_tabs("github.com")[0]["tab_id"])``.
        """
        result = self._call("list_tabs", {"url_contains": url_contains})
        return result if isinstance(result, list) else []

    def close_owned_tabs(self, *, wait: bool = True, timeout: float = 10.0) -> dict:
        """Close every bridge-created tab owned by this scope, and no others.

        This is idempotent. ``wait`` polls tabs whose close was accepted but
        not yet confirmed. Refused closes always raise; ``wait=False`` returns
        accepted pending/remaining tabs without treating them as a failure.
        """
        result = self._call("close_owned_tabs", {}, timeout=max(timeout, 1.0) + 5)
        if not isinstance(result, dict):
            result = {}
        pending = {str(tab_id) for tab_id in result.get("pending", [])}
        if wait and pending:
            deadline = time.monotonic() + max(0.0, timeout)
            while pending and time.monotonic() < deadline:
                owned = {
                    str(tab["tab_id"])
                    for tab in self.list_tabs()
                    if tab.get("bridge_owned_by_scope")
                }
                pending &= owned
                if pending:
                    time.sleep(0.1)
        result["remaining"] = [
            str(tab["tab_id"]) for tab in self.list_tabs() if tab.get("bridge_owned_by_scope")
        ]
        if result.get("refused") or (wait and result["remaining"]):
            count = len(result["remaining"]) or len(result.get("refused") or [])
            raise BridgeError(
                f"Chrome left {count} bridge-owned tab(s) open after cleanup",
                code="TAB_CLEANUP_FAILED",
                detail=result,
            )
        return result

    # ─── navigation ─────────────────────────────────────────────

    def navigate(self, url: str, *, timeout: float = 60.0, settle_ms: int = 0) -> None:
        self._call(
            "navigate",
            {"url": url, "timeout": int(timeout * 1000), "settle_ms": settle_ms},
            timeout=timeout + 30,
        )

    def reload_self(self) -> dict:
        """Reload the Chrome Bridge extension itself.

        The extension tears down its socket as it reloads, so a missing reply
        is the expected case — but a genuinely unreachable server still raises.
        """
        try:
            return self._call("reload_self", {}, timeout=10)
        except (BridgeTimeoutError, ExtensionNotConnectedError) as e:
            return {"ok": True, "message": f"extension reload triggered ({e.code})"}

    def wait_for_load(self, timeout: float = 60.0) -> None:
        self._call("wait_for_load", {"timeout": int(timeout * 1000)}, timeout=timeout + 30)

    def wait_dom_stable(self, timeout: float = 10.0, interval: float = 0.5) -> dict:
        """Block until the DOM stops changing. Returns {stable, waited_ms}."""
        return self._call(
            "wait_dom_stable",
            {"timeout": int(timeout * 1000), "interval": int(interval * 1000)},
            timeout=timeout + 30,
        )

    # ─── JavaScript execution ───────────────────────────────────

    def evaluate(self, expression: str, timeout: float = DEFAULT_TIMEOUT) -> Any:
        """Evaluate a JS *expression* (not statements) and return its value.

        The default matches the pre-1.1 server-side wall: before this release
        the parameter was accepted and dropped, so shortening it here would
        newly abort slow calls that used to complete.
        """
        return self._call("evaluate", {"expression": expression}, timeout=timeout)

    def evaluate_function(
        self, function_body: str, *args: Any, timeout: float = DEFAULT_TIMEOUT
    ) -> Any:
        """Run a function body with arguments, e.g.::

            page.evaluate_function("return a + b;", 1, 2)   # -> 3

        The body receives the arguments positionally as ``arguments[0]``, ...
        """
        return self._call(
            "evaluate_function",
            {"body": function_body, "args": list(args)},
            timeout=timeout,
        )

    # ─── element queries ────────────────────────────────────────

    def query_selector(self, selector: str) -> str | None:
        """Deprecated Playwright-shaped shim — prefer :meth:`has_element`."""
        return "found" if self.has_element(selector) else None

    def query_selector_all(self, selector: str) -> list[str]:
        """Deprecated Playwright-shaped shim — prefer :meth:`get_elements_count`."""
        return ["found"] * self.get_elements_count(selector)

    def has_element(self, selector: str) -> bool:
        return bool(self._call("has_element", {"selector": selector}))

    def wait_for_element(self, selector: str, timeout: float = 30.0) -> str:
        """Block until ``selector`` matches. Raises ElementNotFoundError on timeout."""
        self._call(
            "wait_for_selector",
            {"selector": selector, "timeout": int(timeout * 1000)},
            timeout=timeout + 30,
        )
        return "found"

    def get_element_text(self, selector: str) -> str | None:
        return self._call("get_element_text", {"selector": selector})

    def get_element_attribute(self, selector: str, attr: str) -> str | None:
        return self._call("get_element_attribute", {"selector": selector, "attr": attr})

    def get_elements_count(self, selector: str) -> int:
        result = self._call("get_elements_count", {"selector": selector})
        return int(result) if result is not None else 0

    def get_url(self) -> str:
        return self._call("get_url")

    def get_html(self, selector: str | None = None) -> str:
        """outerHTML of ``selector`` (default: the whole document)."""
        return self._call("get_html", {"selector": selector} if selector else {})

    def read_text(self, selector: str | None = None, *, chunk: int = 500_000) -> str:
        """Read ``innerText`` in chunks, so huge SPA pages don't blow the frame.

        Replaces the hand-rolled ``substring(start, end)`` loop documented in
        references/spa-text-extraction.md.
        """
        out: list[str] = []
        start = 0
        while True:
            params: dict[str, Any] = {"start": start, "length": chunk}
            if selector:
                params["selector"] = selector
            page = self._call("get_text", params)
            out.append(page["text"])
            # Advance by the cursor the *page* computed. JS slices in UTF-16
            # units while Python's len() counts code points, so deriving the
            # next offset here would drift by one per emoji and duplicate text.
            start = page.get("next", start + len(page["text"]))
            if start >= page["total"] or not page["text"]:
                return "".join(out)

    # ─── agent-native snapshot ──────────────────────────────────

    def snapshot(self, *, limit: int = 200, filter: str = "", root: str | None = None) -> dict:
        """List the visible, interactive elements with stable refs.

        Returns ``{snapshot_id, url, title, elements: [{ref, tag, role, name,
        value, checked, disabled, x, y}, ...]}``. Feed a ``ref`` to :meth:`act`
        instead of guessing CSS selectors on a React app with hashed class
        names. Pierces open shadow roots.
        """
        params: dict[str, Any] = {"limit": limit}
        if filter:
            params["filter"] = filter
        if root:
            params["root"] = root
        return self._call("snapshot", params)

    def snapshot_text(self, **kwargs: Any) -> str:
        """:meth:`snapshot` rendered as one compact line per element."""
        snap = self.snapshot(**kwargs)
        lines = [f"# {snap['title']} — {snap['url']}"]
        for el in snap["elements"]:
            bits = [f"[{el['ref']}]", el.get("role") or el["tag"], repr(el.get("name", ""))]
            if el.get("value"):
                bits.append(f"value={el['value']!r}")
            if el.get("checked") is not None:
                bits.append("checked" if el["checked"] else "unchecked")
            if el.get("disabled"):
                bits.append("disabled")
            if el.get("options"):
                bits.append(f"options={el['options']}")
            lines.append(" ".join(bits))
        return "\n".join(lines)

    def act(
        self,
        ref: int,
        action: str = "click",
        *,
        text: str | None = None,
        checked: bool = True,
        snapshot_id: str | None = None,
        blur: bool = False,
    ) -> Any:
        """Act on a :meth:`snapshot` ref.

        Actions: click, fill, check, select, hover, focus, scroll_into_view, text.
        Raises :class:`StaleRefError` if the element is gone — re-snapshot.
        """
        params: dict[str, Any] = {"ref": ref, "action": action, "checked": checked, "blur": blur}
        if text is not None:
            params["text"] = text
        if snapshot_id:
            params["snapshot_id"] = snapshot_id
        return self._call("act_ref", params)

    # ─── authenticated fetch (reuses the browser session) ───────

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: str | None = None,
        headers: dict[str, str] | None = None,
        csrf_from: str | None = None,
        csrf_header: str = "x-csrf-token",
        raise_for_status: bool = True,
        timeout: float = 60.0,
    ) -> dict:
        """``fetch()`` the URL *from the page's own origin*, with its cookies.

        This is the cheap way to hit an internal REST/GraphQL endpoint that a
        plain HTTP client can't reach: the request carries the browser's
        session, and same-origin CSRF checks pass.

        ``csrf_from`` is a CSS selector whose ``content``/``value`` is sent as
        ``csrf_header`` (e.g. ``'meta[name="csrf-token"]'``).

        Returns ``{status, ok, url, length, body}``.
        """
        params: dict[str, Any] = {"url": url, "method": method}
        if body is not None:
            params["body"] = body
        if headers:
            params["headers"] = headers
        if csrf_from:
            params["csrf_from"] = csrf_from
            params["csrf_header"] = csrf_header

        res = self._call("page_fetch", params, timeout=timeout)
        if res.get("buffered"):
            res["body"] = self._drain_buffer(res["length"])
            res.pop("buffered", None)
        if raise_for_status and not res.get("ok"):
            raise BridgeError(
                f"HTTP {res.get('status')} for {url}",
                code="HTTP_ERROR",
                detail={"status": res.get("status"), "body": (res.get("body") or "")[:2000]},
            )
        return res

    def fetch_json(self, url: str, **kwargs: Any) -> Any:
        """:meth:`fetch` + ``json.loads`` on the body."""
        res = self.fetch(url, **kwargs)
        return json.loads(res["body"])

    def _drain_buffer(self, total: int, chunk: int = 1_000_000) -> str:
        parts: list[str] = []
        start = 0
        while start < total:
            page = self._call("read_buffer", {"start": start, "length": chunk})
            if not page["chunk"]:
                break
            parts.append(page["chunk"])
            # UTF-16 cursor from the page — see the note in read_text.
            start = page.get("next", start + len(page["chunk"]))
        return "".join(parts)

    # ─── element interactions ──────────────────────────────────

    def click_element(self, selector: str) -> None:
        self._call("click_element", {"selector": selector})

    def input_text(self, selector: str, text: str, *, blur: bool = False) -> None:
        """Fill an ``<input>``/``<textarea>``.

        Goes through the prototype's native value setter, so React's value
        tracker actually registers the change (assigning ``el.value`` directly
        leaves React thinking nothing happened and the form submits empty).
        """
        self._call("input_text", {"selector": selector, "text": text, "blur": blur})

    def input_content_editable(self, selector: str, text: str) -> None:
        self._call("input_content_editable", {"selector": selector, "text": text})

    def select_option(self, selector: str, value: str) -> None:
        """Set a native ``<select>`` — more reliable than clicking a custom popup."""
        self._call("select_option", {"selector": selector, "value": value})

    def remove_element(self, selector: str) -> None:
        self._call("remove_element", {"selector": selector})

    def hover_element(self, selector: str) -> None:
        self._call("hover_element", {"selector": selector})

    def select_all_text(self, selector: str) -> None:
        self._call("select_all_text", {"selector": selector})

    # ─── scrolling ──────────────────────────────────────────────

    def scroll_by(self, x: int, y: int) -> None:
        self._call("scroll_by", {"x": x, "y": y})

    def scroll_to(self, x: int, y: int) -> None:
        self._call("scroll_to", {"x": x, "y": y})

    def scroll_to_bottom(self) -> None:
        self._call("scroll_to_bottom")

    def scroll_element_into_view(self, selector: str) -> None:
        self._call("scroll_element_into_view", {"selector": selector})

    def scroll_nth_element_into_view(self, selector: str, index: int) -> None:
        self._call("scroll_nth_element_into_view", {"selector": selector, "index": index})

    def get_scroll_top(self) -> int:
        result = self._call("get_scroll_top")
        return int(result) if result is not None else 0

    def get_viewport_height(self) -> int:
        result = self._call("get_viewport_height")
        return int(result) if result is not None else 768

    # ─── keyboard / mouse events ────────────────────────────────

    def press_key(self, key: str) -> None:
        self._call("press_key", {"key": key})

    def type_text(self, text: str, delay_ms: int = 50) -> None:
        self._call(
            "type_text",
            {"text": text, "delayMs": delay_ms},
            timeout=max(DEFAULT_TIMEOUT, len(text) * delay_ms / 1000 + 30),
        )

    def mouse_move(self, x: float, y: float) -> None:
        self._call("mouse_move", {"x": x, "y": y})

    def mouse_click(self, x: float, y: float, button: str = "left") -> None:
        self._call("mouse_click", {"x": x, "y": y, "button": button})

    def dispatch_wheel_event(self, delta_y: float) -> None:
        self._call("dispatch_wheel_event", {"deltaY": delta_y})

    def cdp_mouse(
        self,
        action: str = "click",
        x: float = 0,
        y: float = 0,
        x2: float | None = None,
        y2: float | None = None,
        button: str = "left",
        *,
        activate: bool = False,
    ) -> Any:
        """Trusted native mouse input (CDP Input). action: click / rightclick / move / drag.

        Use when synthetic events are ignored — native context menus, HTML5
        drag-and-drop, and widgets that check ``event.isTrusted``.

        Background pointer moves work normally. Click/drag on the minimized
        automation window raises :class:`ForegroundRequiredError` by default;
        ``activate=True`` is an explicit foreground opt-in and should be used
        only with the user's consent.
        """
        return self._call(
            "cdp_mouse",
            {
                "action": action,
                "x": x,
                "y": y,
                "x2": x2,
                "y2": y2,
                "button": button,
                "activate": activate,
            },
        )

    def activate_tab(self, *, allow_foreground: bool = False) -> dict:
        """Bring this tab to the front only with explicit foreground opt-in.

        ``BridgePage(tab_id=previous).activate_tab()`` puts the old one back.
        """
        return self._call("activate_tab", {"allow_foreground": allow_foreground})

    # ─── file upload ────────────────────────────────────────────

    def set_file_input(self, selector: str, files: list[str]) -> None:
        abs_paths = [os.path.abspath(path) for path in files]
        self._call("set_file_input", {"selector": selector, "files": abs_paths})

    # ─── Cookies ────────────────────────────────────────────────

    def get_cookies(self, domain: str = "", *, all_domains: bool = False) -> list[dict]:
        """Return cookies for one domain, or every domain by explicit opt-in.

        Each entry: {name, value, domain, path, secure, httpOnly, ...}.
        """
        if not isinstance(domain, str):
            raise TypeError("domain must be a string")
        if not isinstance(all_domains, bool):
            raise TypeError("all_domains must be a bool")

        domain = domain.strip()
        if domain and all_domains:
            raise ValueError("domain and all_domains are mutually exclusive")
        if domain:
            params: dict[str, Any] = {"domain": domain}
        elif all_domains:
            params = {"all_domains": True}
        else:
            raise ValueError("cookie scope required: pass domain='example.com' or all_domains=True")
        result = self._call("get_cookies", params)
        return result if isinstance(result, list) else []

    # ─── screenshot ─────────────────────────────────────────────

    def screenshot_element(self, selector: str, padding: int = 0) -> bytes:
        """PNG of just that element, from the *driven* tab.

        (Before 1.1 this silently screenshotted whichever tab the user happened
        to be looking at and ignored the selector entirely.)
        """
        result = self._call("screenshot_element", {"selector": selector, "padding": padding})
        return base64.b64decode(result["data"]) if result and result.get("data") else b""

    def screenshot(self, *, full_page: bool = False) -> bytes:
        """PNG of the driven tab's viewport, or the whole scrollable page."""
        result = self._call("screenshot", {"full_page": full_page})
        return base64.b64decode(result["data"]) if result and result.get("data") else b""

    # ─── Keep-alive browse ───────────────────────────────────────

    def browse_open(
        self,
        url: str,
        timeout: int = 60000,
        *,
        name: str | None = None,
        wait_selector: str | None = None,
        settle_ms: int = 2000,
    ) -> dict:
        """Open a persistent tab. Returns {tab_id, url, status, name}.

        Prefer :meth:`tab` — it closes the tab for you.
        """
        if name is not None:
            _validate_session_name(name)
        params: dict[str, Any] = {"url": url, "timeout": timeout, "settle_ms": settle_ms}
        if name:
            params["name"] = name
        if wait_selector:
            params["wait_selector"] = wait_selector
        return self._call("browse_open", params, timeout=timeout / 1000 + 30)

    def browse_do(
        self,
        tab_id: str,
        expression: str,
        wait_selector: str | None = None,
        wait_timeout: int = 15000,
    ) -> Any:
        """Evaluate a JS expression inside an open persistent tab."""
        params: dict[str, Any] = {"tab_id": tab_id, "expression": expression}
        if wait_selector:
            params["wait_selector"] = wait_selector
            params["wait_timeout"] = wait_timeout
        return self._call("browse_do", params, timeout=wait_timeout / 1000 + DEFAULT_TIMEOUT)

    def browse_close(self, tab_id: str) -> dict:
        """Close a persistent tab opened via browse_open.

        Returns ``{closed, confirmed}``. ``confirmed`` is ``None`` when Chrome
        hadn't finished tearing the tab down yet — closing the last tab of a
        window can take a while — but the close has been issued either way.
        """
        return self._call("browse_close", {"tab_id": tab_id}, timeout=60)

    def browse_and_eval(
        self,
        url: str,
        expression: str,
        wait_selector: str | None = None,
        timeout: int = 30000,
        wait_timeout: int = 15000,
    ) -> Any:
        """Open a URL, evaluate a JS expression, then close the tab."""
        tab_info = self.browse_open(url, timeout=timeout)
        tab_id = tab_info["tab_id"]
        try:
            result = self.browse_do(
                tab_id, expression, wait_selector=wait_selector, wait_timeout=wait_timeout
            )
        except BaseException:
            try:
                self.browse_close(tab_id)
            except BridgeError:
                _LOG.warning("could not close browse tab %s", tab_id)
            raise
        self.browse_close(tab_id)
        return result

    # ─── no-ops (kept for API compatibility) ────────────────────

    def inject_stealth(self) -> None:
        """No-op: Chrome Bridge drives the user's real browser, no stealth needed."""

    # ─── status probes ──────────────────────────────────────────

    def status(self) -> dict:
        """{extension_connected, pending, server_version} — never raises.

        On failure returns {error, message} instead, so a health check can say
        *why* (server down vs. bad token vs. no extension).
        """
        try:
            return self._call("ping_server", timeout=5) or {}
        except BridgeError as e:
            return {"error": e.code, "message": str(e)}

    def is_server_running(self) -> bool:
        try:
            self._call("ping_server", timeout=5)
            return True
        except BridgeConnectionError:
            return False
        except BridgeError:
            # An auth refusal still proves a server answered.
            return True

    def is_extension_connected(self) -> bool:
        return bool(self.status().get("extension_connected"))

    @property
    def target_id(self) -> str:
        return self._tab_id or "extension-bridge"


class Tab(BridgePage):
    """A :class:`BridgePage` bound to one tab, closable and context-managed.

    Every inherited verb (click_element, wait_for_element, screenshot_element,
    snapshot, fetch, ...) targets *this* tab, which is what makes them usable
    inside a browse session at all.
    """

    def __init__(
        self, *args: Any, url: str | None = None, name: str | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.url = url
        self.name = name

    @property
    def tab_id(self) -> str:
        assert self._tab_id is not None
        return self._tab_id

    def close(self) -> None:
        self.browse_close(self.tab_id)

    def __enter__(self) -> Tab:
        return self

    def __exit__(self, exc_type: object, _exc: object, _tb: object) -> None:
        if self.name:
            # A named session is meant to outlive the block — that is the whole
            # reason to name it. Call close() explicitly to dispose of one.
            return
        try:
            self.close()
        except BridgeError:
            if exc_type is None:
                raise
            _LOG.warning("could not close tab %s", self._tab_id)

    def __repr__(self) -> str:
        return f"<Tab {self._tab_id} {self.url or ''}>"


def _truncate(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + f"...({len(text)} chars)"


def _configure_debug_logging() -> None:
    """Enable bridge RPC tracing without exposing authenticated WebSocket frames."""
    if not _LOG.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        _LOG.addHandler(handler)
    _LOG.setLevel(logging.DEBUG)
    _LOG.propagate = False
    # websockets DEBUG logs include complete frame bodies, including the bridge
    # token and cookie results. Never enable them as a side effect of our flag.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("websockets.client").setLevel(logging.WARNING)


if os.environ.get("CHROME_BRIDGE_DEBUG"):  # pragma: no cover - opt-in tracing
    _configure_debug_logging()


# ─────────────────────────── CLI ───────────────────────────


def main(argv: list[str] | None = None) -> int:
    """One-shot bridge calls from a shell, so an agent needn't write a .py file.

    chrome-bridge eval 'document.title' --url https://example.com
    chrome-bridge --scope task-a snapshot --session dash
    chrome-bridge fetch https://internal/api/x --json
    """
    import argparse

    # Target options are accepted on both sides of the subcommand, because
    # `eval 'x' --url U` is what everyone types first. SUPPRESS keeps the
    # subparser copy from clobbering a value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--bridge-url", default=argparse.SUPPRESS)
    common.add_argument(
        "--url", default=argparse.SUPPRESS, help="open this URL first (temp tab, closed afterwards)"
    )
    common.add_argument(
        "--session",
        type=_validate_session_name,
        default=argparse.SUPPRESS,
        help="named session to use (kept open). Combine with --url to create one.",
    )
    common.add_argument(
        "--tab-id", default=argparse.SUPPRESS, help="target an existing tab id (see `list-tabs`)"
    )
    common.add_argument(
        "--scope",
        default=argparse.SUPPRESS,
        help="stable task/agent ownership scope (required for cross-process sessions and cleanup)",
    )

    parser = argparse.ArgumentParser(
        prog="chrome-bridge", description=main.__doc__, parents=[common]
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_text: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help_text, parents=[common])

    def cookie_domain(value: str) -> str:
        value = value.strip()
        if not value:
            raise argparse.ArgumentTypeError("cookie domain must not be empty")
        return value

    p_eval = add("eval", "evaluate a JS expression")
    p_eval.add_argument("expression")

    p_text = add("text", "read innerText")
    p_text.add_argument("--selector")

    p_snap = add("snapshot", "list interactive elements")
    p_snap.add_argument("--limit", type=int, default=200)
    p_snap.add_argument("--filter", default="")

    p_fetch = add("fetch", "fetch a URL using the browser session")
    p_fetch.add_argument("target")
    p_fetch.add_argument("--json", action="store_true")

    p_shot = add("screenshot", "PNG of the page or one element")
    p_shot.add_argument("--selector")
    p_shot.add_argument("--out", default="screenshot.png")

    p_cookies = add(
        "cookies",
        "list cookie metadata; values are redacted unless explicitly revealed",
    )
    cookie_scope = p_cookies.add_mutually_exclusive_group(required=True)
    cookie_scope.add_argument("--domain", type=cookie_domain)
    cookie_scope.add_argument(
        "--all-domains",
        action="store_true",
        help="list every cookie in the Chrome profile (sensitive metadata)",
    )
    p_cookies.add_argument(
        "--show-values",
        action="store_true",
        help="include cookie values in stdout (sensitive; values are redacted by default)",
    )

    add("status", "server + extension health")
    add("list-tabs", "every open tab")
    add("list-sessions", "named bridge sessions")
    add("reload", "reload the extension (after editing extension/)")
    add("cleanup", "close every bridge-owned tab in this scope")

    p_close = add("close", "close a named session or tab id")
    p_close.add_argument("target")

    args = parser.parse_args(argv)
    bridge_url = getattr(args, "bridge_url", BRIDGE_URL)
    open_url = getattr(args, "url", None)
    session = getattr(args, "session", None)
    scope = getattr(args, "scope", None)
    explicit_scope = bool(scope or os.environ.get("CHROME_BRIDGE_SCOPE"))
    if (session or args.cmd in {"close", "cleanup", "list-sessions"}) and not (
        scope or os.environ.get("CHROME_BRIDGE_SCOPE")
    ):
        parser.error(
            "--session, close, cleanup, and list-sessions require --scope or CHROME_BRIDGE_SCOPE"
        )
    page = BridgePage(
        bridge_url,
        tab_id=getattr(args, "tab_id", None),
        scope_id=scope,
    )

    if args.cmd == "status":
        print(json.dumps(page.status(), indent=2))
        return 0
    if args.cmd == "reload":
        print(json.dumps(page.reload_self(), indent=2))
        return 0

    target: BridgePage = page
    temp: Tab | None = None
    transient_owner: BridgePage | None = None
    out: Any = None
    command_error: BaseException | None = None
    try:
        if open_url:
            # An anonymous one-shot gets its own ownership scope even when the
            # caller also supplied a persistent --scope. Its popup descendants
            # can then be cleaned without deleting that scope's named sessions.
            owner = page
            if not session:
                transient_owner = BridgePage(
                    bridge_url,
                    scope_id=f"oneshot-{secrets.token_urlsafe(24)}",
                )
                owner = transient_owner
            temp = owner.tab(open_url, name=session)
            target = temp
        elif session:
            target = BridgePage(bridge_url, tab_id=session, scope_id=page.scope_id)

        if args.cmd == "eval":
            out = target.evaluate(args.expression)
        elif args.cmd == "text":
            out = target.read_text(args.selector)
        elif args.cmd == "snapshot":
            out = target.snapshot_text(limit=args.limit, filter=args.filter)
        elif args.cmd == "fetch":
            out = target.fetch_json(args.target) if args.json else target.fetch(args.target)["body"]
        elif args.cmd == "screenshot":
            data = (
                target.screenshot_element(args.selector) if args.selector else target.screenshot()
            )
            with open(args.out, "wb") as fh:
                fh.write(data)
            out = {"written": args.out, "bytes": len(data)}
        elif args.cmd == "cookies":
            cookies = target.get_cookies(args.domain or "", all_domains=args.all_domains)
            out = (
                cookies
                if args.show_values
                else [{**cookie, "value": "<redacted>"} for cookie in cookies]
            )
        elif args.cmd == "list-tabs":
            out = page.list_tabs()
        elif args.cmd == "list-sessions":
            out = page.list_sessions()
        elif args.cmd == "cleanup":
            out = page.close_owned_tabs()
        elif args.cmd == "close":
            out = page.browse_close(args.target)
        else:  # pragma: no cover - argparse enforces this
            parser.error(f"unknown command {args.cmd}")
            return 2
    except BaseException as e:
        command_error = e

    cleanup_error: BridgeError | None = None
    if transient_owner is not None:
        if temp is not None:
            try:
                temp.close()
            except BridgeError as e:
                cleanup_error = e
        try:
            transient_owner.close_owned_tabs()
        except BridgeError as e:
            cleanup_error = e
        else:
            # Scope cleanup is authoritative and catches popup descendants too.
            cleanup_error = None
    elif (
        not explicit_scope
        and not session
        and args.cmd in {"eval", "text", "snapshot", "fetch", "screenshot"}
    ):
        try:
            page.close_owned_tabs()
        except BridgeError as e:
            cleanup_error = e

    if command_error is not None:
        if not isinstance(command_error, BridgeError):
            if cleanup_error is not None:
                _LOG.warning(
                    "cleanup also failed after %s: %s",
                    type(command_error).__name__,
                    cleanup_error,
                )
            raise command_error
        payload = {
            "error": command_error.code,
            "message": str(command_error),
            "detail": command_error.detail,
        }
        if cleanup_error is not None:
            payload["cleanup_error"] = {
                "code": cleanup_error.code,
                "message": str(cleanup_error),
                "detail": cleanup_error.detail,
            }
        print(json.dumps(payload), file=sys.stderr)
        return 1
    if cleanup_error is not None:
        print(
            json.dumps(
                {
                    "error": cleanup_error.code,
                    "message": str(cleanup_error),
                    "detail": cleanup_error.detail,
                }
            ),
            file=sys.stderr,
        )
        return 1

    print(out if isinstance(out, str) else json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
