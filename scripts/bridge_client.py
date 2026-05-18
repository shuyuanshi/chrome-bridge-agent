"""BridgePage - Python client for the Chrome Bridge extension.

Talks to bridge_server.py over WebSocket; the server forwards each command to
the Chrome extension, which executes it in the active tab and returns the
result.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any

import websockets.sync.client as ws_client

BRIDGE_URL = "ws://localhost:9333"


class BridgeError(Exception):
    """Raised on any Bridge communication failure."""


class ElementNotFoundError(BridgeError):
    """Raised when a CSS selector does not match any element."""


class BridgePage:
    """Browser automation via the Chrome Bridge extension.

    Each method opens a short-lived WebSocket connection to the bridge server,
    sends one command, and waits for the reply. No long-lived state is kept
    on the client side.
    """

    def __init__(self, bridge_url: str = BRIDGE_URL) -> None:
        self._bridge_url = bridge_url

    # ─── internal RPC ───────────────────────────────────────────

    def _call(self, method: str, params: dict | None = None) -> Any:
        """Send one command to the bridge server and wait for the reply."""
        msg: dict[str, Any] = {"role": "cli", "method": method}
        if params:
            msg["params"] = params
        try:
            with ws_client.connect(self._bridge_url, max_size=50 * 1024 * 1024) as ws:
                ws.send(json.dumps(msg, ensure_ascii=False))
                raw = ws.recv(timeout=90)
        except OSError as e:
            raise BridgeError(
                f"could not connect to bridge server at {self._bridge_url}: {e}"
            ) from e

        resp = json.loads(raw)
        if "error" in resp and resp["error"]:
            raise BridgeError(f"bridge error: {resp['error']}")
        return resp.get("result")

    # ─── navigation ─────────────────────────────────────────────

    def navigate(self, url: str) -> None:
        self._call("navigate", {"url": url})

    def reload_self(self) -> dict:
        """Reload the Chrome Bridge extension itself. Returns immediately."""
        try:
            return self._call("reload_self", {})
        except Exception:
            return {"ok": True, "message": "extension reload triggered"}

    def wait_for_load(self, timeout: float = 60.0) -> None:
        self._call("wait_for_load", {"timeout": int(timeout * 1000)})

    def wait_dom_stable(self, timeout: float = 10.0, interval: float = 0.5) -> None:
        self._call(
            "wait_dom_stable",
            {
                "timeout": int(timeout * 1000),
                "interval": int(interval * 1000),
            },
        )

    # ─── JavaScript execution ───────────────────────────────────

    def evaluate(self, expression: str, timeout: float = 30.0) -> Any:
        return self._call("evaluate", {"expression": expression})

    def evaluate_function(self, function_body: str, *args: Any) -> Any:
        return self._call("evaluate", {"expression": f"({function_body})()"})

    # ─── element queries ────────────────────────────────────────

    def query_selector(self, selector: str) -> str | None:
        found = self._call("has_element", {"selector": selector})
        return "found" if found else None

    def query_selector_all(self, selector: str) -> list[str]:
        count = self.get_elements_count(selector)
        return ["found"] * count

    def has_element(self, selector: str) -> bool:
        return bool(self._call("has_element", {"selector": selector}))

    def wait_for_element(self, selector: str, timeout: float = 30.0) -> str:
        found = self._call(
            "wait_for_selector",
            {
                "selector": selector,
                "timeout": int(timeout * 1000),
            },
        )
        if not found:
            raise ElementNotFoundError(selector)
        return "found"

    # ─── element interactions ──────────────────────────────────

    def click_element(self, selector: str) -> None:
        self._call("click_element", {"selector": selector})

    def input_text(self, selector: str, text: str) -> None:
        self._call("input_text", {"selector": selector, "text": text})

    def input_content_editable(self, selector: str, text: str) -> None:
        self._call("input_content_editable", {"selector": selector, "text": text})

    def get_element_text(self, selector: str) -> str | None:
        return self._call("get_element_text", {"selector": selector})

    def get_element_attribute(self, selector: str, attr: str) -> str | None:
        return self._call("get_element_attribute", {"selector": selector, "attr": attr})

    def get_elements_count(self, selector: str) -> int:
        result = self._call("get_elements_count", {"selector": selector})
        return int(result) if result is not None else 0

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
        self._call("type_text", {"text": text, "delayMs": delay_ms})

    def mouse_move(self, x: float, y: float) -> None:
        self._call("mouse_move", {"x": x, "y": y})

    def mouse_click(self, x: float, y: float, button: str = "left") -> None:
        self._call("mouse_click", {"x": x, "y": y, "button": button})

    def dispatch_wheel_event(self, delta_y: float) -> None:
        self._call("dispatch_wheel_event", {"deltaY": delta_y})

    # ─── file upload ────────────────────────────────────────────

    def set_file_input(self, selector: str, files: list[str]) -> None:
        abs_paths = [os.path.abspath(path) for path in files]
        self._call("set_file_input", {"selector": selector, "files": abs_paths})

    # ─── Cookies ────────────────────────────────────────────────

    def get_cookies(self, domain: str = "") -> list[dict]:
        """Return cookies for the given domain (or all domains if empty).

        Each entry: {name, value, domain, path, secure, httpOnly, ...}.
        """
        params: dict[str, Any] = {}
        if domain:
            params["domain"] = domain
        result = self._call("get_cookies", params)
        return result if isinstance(result, list) else []

    # ─── screenshot ─────────────────────────────────────────────

    def screenshot_element(self, selector: str, padding: int = 0) -> bytes:
        result = self._call("screenshot_element", {"selector": selector, "padding": padding})
        if result and result.get("data"):
            return base64.b64decode(result["data"])
        return b""

    # ─── Keep-alive browse ───────────────────────────────────────

    def browse_open(self, url: str, timeout: int = 60000) -> dict:
        """Open a persistent tab. Returns {tab_id, url, status}."""
        return self._call("browse_open", {"url": url, "timeout": timeout})

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
        return self._call("browse_do", params)

    def browse_close(self, tab_id: str) -> dict:
        """Close a persistent tab opened via browse_open."""
        return self._call("browse_close", {"tab_id": tab_id})

    def browse_and_eval(
        self,
        url: str,
        expression: str,
        wait_selector: str | None = None,
        timeout: int = 30000,
        wait_timeout: int = 15000,
    ) -> Any:
        """Open a URL, evaluate a JS expression, then close the tab.

        Convenience wrapper around browse_open -> browse_do -> browse_close.
        """
        tab_info = self.browse_open(url, timeout=timeout)
        tab_id = tab_info["tab_id"]
        try:
            result = self.browse_do(
                tab_id,
                expression,
                wait_selector=wait_selector,
                wait_timeout=wait_timeout,
            )
        finally:
            self.browse_close(tab_id)
        return result

    # ─── no-ops (kept for API compatibility) ────────────────────

    def inject_stealth(self) -> None:
        """No-op: Chrome Bridge drives the user's real browser, no stealth needed."""

    # ─── status probes ──────────────────────────────────────────

    def is_server_running(self) -> bool:
        try:
            with ws_client.connect(self._bridge_url, open_timeout=3) as ws:
                ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                raw = ws.recv(timeout=5)
            resp = json.loads(raw)
            return "result" in resp
        except Exception:
            return False

    def is_extension_connected(self) -> bool:
        try:
            with ws_client.connect(self._bridge_url, open_timeout=3) as ws:
                ws.send(json.dumps({"role": "cli", "method": "ping_server"}))
                raw = ws.recv(timeout=5)
            resp = json.loads(raw)
            return bool(resp.get("result", {}).get("extension_connected"))
        except Exception:
            return False

    @property
    def target_id(self) -> str:
        return "extension-bridge"
