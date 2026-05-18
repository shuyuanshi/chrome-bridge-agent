"""End-to-end tests against a live Chrome Bridge.

These tests are skipped automatically when the bridge isn't reachable
(see ``conftest.py``). They use only public, login-free pages so they
work for any developer.
"""

from __future__ import annotations

import pytest
from bridge_client import BridgePage


@pytest.mark.integration
class TestBridgePublicSites:
    @pytest.fixture(scope="class")
    def page(self) -> BridgePage:
        return BridgePage()

    def test_server_running(self, page: BridgePage) -> None:
        assert page.is_server_running()

    def test_extension_connected(self, page: BridgePage) -> None:
        assert page.is_extension_connected()

    def test_browse_and_eval_example_com(self, page: BridgePage) -> None:
        title = page.browse_and_eval(
            url="https://example.com",
            expression="document.title",
            timeout=30000,
        )
        assert title == "Example Domain"

    def test_browse_open_do_close_lifecycle(self, page: BridgePage) -> None:
        tab = page.browse_open("https://example.com", timeout=30000)
        try:
            assert "tab_id" in tab
            heading = page.browse_do(
                tab["tab_id"],
                "document.querySelector('h1').innerText",
            )
            assert heading == "Example Domain"
        finally:
            page.browse_close(tab["tab_id"])

    def test_navigate_then_evaluate_on_managed_tab(self, page: BridgePage) -> None:
        page.navigate("https://example.com")
        page.wait_for_load(timeout=30)
        title = page.evaluate("document.title")
        assert title == "Example Domain"

    def test_get_cookies_returns_list(self, page: BridgePage) -> None:
        cookies = page.get_cookies(domain="example.com")
        # example.com sets no cookies; the API still has to return a list.
        assert isinstance(cookies, list)
