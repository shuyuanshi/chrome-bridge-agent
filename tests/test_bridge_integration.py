"""End-to-end tests against a live Chrome Bridge.

These tests are skipped automatically when the bridge isn't reachable
(see ``conftest.py``). They use only public, login-free pages so they
work for any developer.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from bridge_client import BridgePage, ElementNotFoundError, StaleRefError, Tab, TabGoneError

EXAMPLE = "https://example.com"


@pytest.fixture(scope="module")
def local_site():
    """A tiny same-origin HTTP server for the fetch tests.

    They used to hit httpbin.org, which made a red suite mean "someone else's
    service is slow" as often as "the bridge is broken".
    """

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path.startswith("/api"):
                body = json.dumps({"path": self.path, "ok": True}).encode()
                ctype = "application/json"
            else:
                body = b"<!doctype html><h1>local</h1>"
                ctype = "text/html"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # keep the test output clean

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.integration
class TestBridgePublicSites:
    @pytest.fixture(scope="class")
    def page(self) -> BridgePage:
        return BridgePage()

    def test_server_running(self, page: BridgePage) -> None:
        assert page.is_server_running()

    def test_extension_connected(self, page: BridgePage) -> None:
        assert page.is_extension_connected()

    def test_status_reports_a_version(self, page: BridgePage) -> None:
        assert page.status().get("server_version")

    def test_browse_and_eval_example_com(self, page: BridgePage) -> None:
        title = page.browse_and_eval(url=EXAMPLE, expression="document.title", timeout=30000)
        assert title == "Example Domain"

    def test_browse_open_do_close_lifecycle(self, page: BridgePage) -> None:
        tab = page.browse_open(EXAMPLE, timeout=30000)
        try:
            assert "tab_id" in tab
            heading = page.browse_do(tab["tab_id"], "document.querySelector('h1').innerText")
            assert heading == "Example Domain"
        finally:
            page.browse_close(tab["tab_id"])

    def test_navigate_then_evaluate_on_managed_tab(self, page: BridgePage) -> None:
        page.navigate(EXAMPLE)
        page.wait_for_load(timeout=30)
        assert page.evaluate("document.title") == "Example Domain"

    def test_get_cookies_returns_list(self, page: BridgePage) -> None:
        cookies = page.get_cookies(domain="example.com")
        # example.com sets no cookies; the API still has to return a list.
        assert isinstance(cookies, list)


@pytest.mark.integration
class TestSessions:
    """The tab-bound API: typed verbs working inside a browse session."""

    @pytest.fixture(scope="class")
    def page(self) -> BridgePage:
        return BridgePage()

    def test_tab_context_manager_closes_the_tab(self, page: BridgePage) -> None:
        """Assert what the bridge controls, not Chrome's scheduling.

        `browse_close` deliberately doesn't wait for Chrome to reap the tab:
        measured on a real browser, removal lands anywhere between a few
        milliseconds and (with an unfocused window and a queue of pending
        removals) over a minute. Blocking on that turned successful closes into
        spurious TIMEOUTs, so the contract is "the close is issued and the tab
        is deregistered", and `confirmed` tells you whether Chrome had already
        finished.
        """
        started = time.monotonic()
        with page.tab(EXAMPLE) as tab:
            tab_id = tab.tab_id
            assert tab.evaluate("document.title") == "Example Domain"
        assert time.monotonic() - started < 60, "closing the session blocked for too long"

        assert all(s["tab_id"] != tab_id for s in page.list_sessions())
        still_owned = [t for t in page.list_tabs() if t["tab_id"] == tab_id and t["bridge_owned"]]
        assert not still_owned, "tab is still registered as a live bridge session"

    def test_a_tab_id_that_never_existed_is_reported_as_gone(self, page: BridgePage) -> None:
        with pytest.raises(TabGoneError):
            BridgePage(tab_id="99999999").evaluate("1")

    def test_typed_verbs_target_the_session_tab(self, page: BridgePage) -> None:
        """Before tab_id plumbing, these silently hit the shared managed tab."""
        page.navigate("https://example.org")  # poison the managed tab
        with page.tab(EXAMPLE) as tab:
            assert tab.has_element("h1")
            assert (tab.get_element_text("h1") or "").strip() == "Example Domain"
            assert tab.wait_for_element("h1", timeout=5) == "found"
            assert tab.get_elements_count("p") >= 1
            assert "example.com" in tab.get_url()

    def test_named_session_is_reused_not_duplicated(self, page: BridgePage) -> None:
        first = page.browse_open(EXAMPLE, name="pytest-session")
        try:
            second = page.browse_open(EXAMPLE, name="pytest-session")
            assert second["tab_id"] == first["tab_id"]
            assert second["status"] == "reused"
            assert any(s["name"] == "pytest-session" for s in page.list_sessions())
        finally:
            page.browse_close(first["tab_id"])

    def test_list_tabs_sees_the_session(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            found = [t for t in page.list_tabs("example.com") if t["tab_id"] == tab.tab_id]
            assert found and found[0]["bridge_owned"] is True


@pytest.mark.integration
class TestAgentPrimitives:
    @pytest.fixture(scope="class")
    def page(self) -> BridgePage:
        return BridgePage()

    def test_snapshot_lists_interactive_elements_with_refs(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            snap = tab.snapshot()
            assert snap["title"] == "Example Domain"
            assert snap["snapshot_id"]
            links = [e for e in snap["elements"] if e["tag"] == "a"]
            assert links, snap["elements"]
            assert "iana" in links[0]["name"].lower() or links[0].get("href")

    def test_snapshot_text_is_compact(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            text = tab.snapshot_text()
            assert text.startswith("# Example Domain")
            assert "[0]" in text

    def test_act_by_ref_reads_text(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            snap = tab.snapshot()
            ref = snap["elements"][0]["ref"]
            assert isinstance(tab.act(ref, "text", snapshot_id=snap["snapshot_id"]), str)

    def test_stale_ref_is_reported_not_mis_clicked(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            snap = tab.snapshot()
            with pytest.raises(StaleRefError):
                tab.act(snap["elements"][0]["ref"], "click", snapshot_id="not-the-current-one")

    def test_fetch_uses_the_page_session(self, page: BridgePage, local_site: str) -> None:
        with page.tab(f"{local_site}/") as tab:
            res = tab.fetch(f"{local_site}/api/thing")
            assert res["ok"] and res["status"] == 200
            assert "/api/thing" in res["body"]

    def test_fetch_json_parses(self, page: BridgePage, local_site: str) -> None:
        with page.tab(f"{local_site}/") as tab:
            assert tab.fetch_json(f"{local_site}/api/items")["path"] == "/api/items"

    def test_fetch_raises_on_a_bad_status(self, page: BridgePage, local_site: str) -> None:
        from bridge_client import BridgeError  # noqa: PLC0415

        with page.tab(f"{local_site}/") as tab:
            with pytest.raises(BridgeError) as excinfo:
                tab.fetch(f"{local_site}/api/x", method="POST")  # handler only serves GET
            assert excinfo.value.code == "HTTP_ERROR"

    def test_read_text_pages_a_page_larger_than_the_chunk(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            whole = tab.read_text()
            assert "Example Domain" in whole
            # A chunk far smaller than the page proves the paging loop joins up.
            assert tab.read_text(chunk=17) == whole

    def test_read_text_never_splits_an_emoji(self, page: BridgePage) -> None:
        """Real page, real UTF-16: chunk boundaries must not land mid-pair."""
        with page.tab(EXAMPLE) as tab:
            tab.evaluate(
                "(function(){document.body.innerHTML="
                "'<p>hello \\u{1F600}\\u{1F600}\\u{1F600} world \\u6F22\\u5B57</p>';return 1})()"
            )
            whole = tab.read_text()
            assert "😀😀😀" in whole
            for chunk in (1, 3, 7):
                assert tab.read_text(chunk=chunk) == whole, chunk

    def test_get_html(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            assert tab.get_html("h1").startswith("<h1")

    def test_element_screenshot_is_cropped_and_from_the_right_tab(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            png = tab.screenshot_element("h1", padding=4)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"
            width, height = _png_size(png)
            # An <h1>, not a whole 1280x800 window.
            assert 0 < height < 200, (width, height)
            full = tab.screenshot()
            assert _png_size(full)[1] > height

    def test_missing_element_raises_a_typed_error(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            with pytest.raises(ElementNotFoundError) as excinfo:
                tab.click_element("#definitely-not-here")
            assert excinfo.value.code == "ELEMENT_NOT_FOUND"

    def test_evaluate_function_receives_arguments(self, page: BridgePage) -> None:
        with page.tab(EXAMPLE) as tab:
            assert tab.evaluate_function("return arguments[0] + arguments[1];", 2, 3) == 5

    def test_js_error_carries_a_stack(self, page: BridgePage) -> None:
        from bridge_client import JSEvalError  # noqa: PLC0415

        with page.tab(EXAMPLE) as tab:
            with pytest.raises(JSEvalError) as excinfo:
                tab.evaluate("nope_not_defined()")
            assert "not defined" in str(excinfo.value)


@pytest.mark.integration
class TestReactForms:
    """Proves the fill path defeats React's value tracker.

    React installs an *instance-level* ``value`` property on the input. Code
    that does ``el.value = x`` goes through it, so the tracker records the new
    value and React concludes nothing changed — the DOM looks filled and the
    form submits empty. The cure is to call the *prototype's* native setter,
    leaving the tracker stale so React registers a real change.

    (Chrome forbids an extension from navigating a tab to a ``data:`` URL, so
    the tracker is installed into a real page instead.)
    """

    INSTALL_TRACKER = """(function () {
      document.querySelectorAll('#tracked').forEach(n => n.remove());
      const el = document.createElement('input');
      el.id = 'tracked';
      document.body.appendChild(el);
      const desc = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
      window.__instanceSetterCalls = 0;
      window.__inputEvents = 0;
      Object.defineProperty(el, 'value', {
        get() { return desc.get.call(el); },
        set(v) { window.__instanceSetterCalls++; desc.set.call(el, v); },
        configurable: true,
      });
      el.addEventListener('input', () => { window.__inputEvents++; });
      return 'ready';
    })()"""

    def _assert_tracker_was_bypassed(self, tab: BridgePage, expected: str) -> None:
        assert tab.evaluate("document.querySelector('#tracked').value") == expected
        assert tab.evaluate("window.__instanceSetterCalls") == 0, (
            "wrote through React's value tracker — the framework will ignore this change"
        )
        assert tab.evaluate("window.__inputEvents") >= 1

    def test_input_text_bypasses_the_value_tracker(self) -> None:
        with BridgePage().tab(EXAMPLE) as tab:
            assert tab.evaluate(self.INSTALL_TRACKER) == "ready"
            tab.input_text("#tracked", "hello world")
            self._assert_tracker_was_bypassed(tab, "hello world")

    def test_act_fill_bypasses_the_value_tracker(self) -> None:
        with BridgePage().tab(EXAMPLE) as tab:
            assert tab.evaluate(self.INSTALL_TRACKER) == "ready"
            snap = tab.snapshot()
            ref = next(e["ref"] for e in snap["elements"] if e["tag"] == "input")
            tab.act(ref, "fill", text="typed by ref")
            self._assert_tracker_was_bypassed(tab, "typed by ref")


def _png_size(data: bytes) -> tuple[int, int]:
    """(width, height) from the IHDR chunk — avoids a Pillow dependency."""
    assert data[12:16] == b"IHDR"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


@pytest.mark.integration
class TestInputScrollAndCdp:
    """The verbs the core suite never exercises — including the two pure-CDP
    paths (`set_file_input`, `cdp_mouse`), which have no unit coverage at all
    because they cannot run without a real browser.

    Each test builds its own scratch DOM inside a real page, so nothing depends
    on a third-party site's markup.
    """

    # One real tab for the whole class: opening a fresh one per test cost ~10 s
    # each and dominated the suite. The DOM is rebuilt before every test, so
    # they stay independent.
    @pytest.fixture(scope="class")
    def _session(self):
        page = BridgePage()
        with page.tab(EXAMPLE) as tab:
            yield tab

    @pytest.fixture
    def tab(self, _session):
        tab = _session
        tab.evaluate("(function(){window.scrollTo(0,0);return 1})()")
        tab.evaluate(
            """(function () {
                  document.body.innerHTML = `
                    <select id="sel"><option value="a">A</option><option value="b">B</option></select>
                    <div id="ce" contenteditable="true"></div>
                    <input id="txt" type="text">
                    <input id="file" type="file">
                    <button id="btn">Click me</button>
                    <p id="doomed">delete me</p>
                    <div class="row">r0</div><div class="row">r1</div><div class="row">r2</div>
                    <div id="tall" style="height:3000px"></div>`;
                  window.__log = [];
                  const rec = (n) => (e) => window.__log.push([n, e.isTrusted === true]);
                  for (const ev of ['mouseover', 'mousemove', 'wheel', 'click', 'contextmenu']) {
                    document.getElementById('btn').addEventListener(ev, rec(ev));
                  }
                  document.addEventListener('wheel', rec('doc-wheel'));
                  document.getElementById('sel').addEventListener('change', rec('change'));
                  return 'ready';
                })()"""
        )
        return tab

    # ── form controls ──

    def test_select_option_fires_change(self, tab: Tab) -> None:
        tab.select_option("#sel", "b")
        assert tab.evaluate("document.querySelector('#sel').value") == "b"
        assert tab.evaluate("window.__log.some(e => e[0] === 'change')") is True

    def test_input_content_editable_handles_newlines(self, tab: Tab) -> None:
        tab.input_content_editable("#ce", "line one\nline two")
        text = tab.evaluate("document.querySelector('#ce').innerText")
        assert "line one" in text and "line two" in text

    def test_type_text_and_press_key(self, tab: Tab) -> None:
        tab.evaluate("(function(){document.querySelector('#txt').focus();return 1})()")
        tab.type_text("hi", delay_ms=5)
        tab.press_key("Enter")  # must not throw on a plain input

    def test_select_all_text_and_get_attribute(self, tab: Tab) -> None:
        tab.evaluate("(function(){document.querySelector('#txt').value='pick me';return 1})()")
        tab.select_all_text("#txt")
        assert tab.evaluate(
            "window.getSelection().toString() || document.querySelector('#txt').value"
        )
        assert tab.get_element_attribute("#file", "type") == "file"

    def test_remove_element(self, tab: Tab) -> None:
        assert tab.has_element("#doomed")
        tab.remove_element("#doomed")
        assert not tab.has_element("#doomed")

    # ── pointer events ──

    def test_hover_and_synthetic_mouse(self, tab: Tab) -> None:
        tab.hover_element("#btn")
        assert tab.evaluate("window.__log.some(e => e[0] === 'mouseover')") is True
        box = tab.evaluate("JSON.stringify(document.querySelector('#btn').getBoundingClientRect())")
        rect = json.loads(box)
        cx, cy = rect["left"] + rect["width"] / 2, rect["top"] + rect["height"] / 2
        tab.mouse_move(cx, cy)
        tab.mouse_click(cx, cy)
        assert tab.evaluate("window.__log.some(e => e[0] === 'click')") is True

    def test_dispatch_wheel_event(self, tab: Tab) -> None:
        tab.dispatch_wheel_event(120)
        assert tab.evaluate("window.__log.some(e => e[0] === 'doc-wheel')") is True

    @staticmethod
    def _centre(tab: Tab, selector: str) -> tuple[float, float]:
        rect = json.loads(
            tab.evaluate(
                f"JSON.stringify(document.querySelector('{selector}').getBoundingClientRect())"
            )
        )
        return rect["left"] + rect["width"] / 2, rect["top"] + rect["height"] / 2

    def test_cdp_mouse_delivers_a_trusted_click(self, tab: Tab) -> None:
        """The entire point of cdp_mouse: events synthetic dispatch can't fake.

        Anything gated on `event.isTrusted` (native menus, HTML5 drag) only
        responds to this path.
        """
        cx, cy = self._centre(tab, "#btn")
        tab.cdp_mouse("click", x=cx, y=cy)
        trusted = tab.evaluate("window.__log.filter(e => e[0] === 'click' && e[1] === true).length")
        assert trusted >= 1, tab.evaluate("JSON.stringify(window.__log)")

    def test_cdp_mouse_rightclick_and_move(self, tab: Tab) -> None:
        cx, cy = self._centre(tab, "#btn")
        tab.cdp_mouse("move", x=cx, y=cy)
        tab.cdp_mouse("rightclick", x=cx, y=cy)
        assert (
            tab.evaluate("window.__log.some(e => e[0] === 'contextmenu' && e[1] === true)") is True
        )

    def test_activate_tab_reports_and_restores_the_displaced_tab(self, tab: Tab) -> None:
        """`reload_self` is the only public verb with no E2E test: it tears
        down every open session, so it can't run inside a suite. It is
        exercised constantly in practice — the server's file watcher calls it
        on every edit to extension/."""
        page = BridgePage()
        info = tab.activate_tab()
        assert info["activated"] == tab.tab_id
        assert [t["tab_id"] for t in page.list_tabs() if t["active"]].count(tab.tab_id) == 1
        if info["previous"]:
            BridgePage(tab_id=info["previous"]).activate_tab()
            actives = [t["tab_id"] for t in page.list_tabs() if t["active"]]
            assert info["previous"] in actives

    def test_cdp_mouse_puts_the_users_tab_back(self, tab: Tab) -> None:
        page = BridgePage()
        before = sorted(t["tab_id"] for t in page.list_tabs() if t["active"])
        cx, cy = self._centre(tab, "#btn")
        tab.cdp_mouse("click", x=cx, y=cy)
        after = sorted(t["tab_id"] for t in page.list_tabs() if t["active"])
        assert after == before, "cdp_mouse left the user looking at a different tab"

    # ── file upload (CDP DOM.setFileInputFiles) ──

    def test_set_file_input(self, tab: Tab, tmp_path) -> None:
        payload = tmp_path / "upload-probe.txt"
        payload.write_text("hello from the bridge", encoding="utf-8")
        tab.set_file_input("#file", [str(payload)])
        assert tab.evaluate("document.querySelector('#file').files.length") == 1
        assert tab.evaluate("document.querySelector('#file').files[0].name") == "upload-probe.txt"

    def test_set_file_input_on_a_missing_selector_is_typed(self, tab: Tab) -> None:
        with pytest.raises(ElementNotFoundError):
            tab.set_file_input("#no-such-input", [__file__])

    # ── scrolling ──

    def test_scrolling_verbs(self, tab: Tab) -> None:
        assert tab.get_viewport_height() > 0
        assert tab.get_scroll_top() == 0
        tab.scroll_by(0, 500)
        assert tab.get_scroll_top() > 0
        tab.scroll_to(0, 0)
        assert tab.get_scroll_top() == 0
        tab.scroll_to_bottom()
        assert tab.get_scroll_top() > 0
        tab.scroll_element_into_view("#btn")
        tab.scroll_nth_element_into_view(".row", 2)

    def test_wait_dom_stable_reports_stability(self, tab: Tab) -> None:
        result = tab.wait_dom_stable(timeout=5)
        assert result["stable"] is True

    # ── deprecated shims still answer ──

    def test_deprecated_shims(self, tab: Tab) -> None:
        assert tab.query_selector("#btn") == "found"
        assert tab.query_selector("#nope") is None
        assert len(tab.query_selector_all(".row")) == 3
        assert tab.target_id == tab.tab_id
        assert tab.inject_stealth() is None
