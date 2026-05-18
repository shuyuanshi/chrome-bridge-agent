"""Smoke tests that exercise import + API surface without a live Bridge.

These run in CI without Chrome.
"""

from __future__ import annotations

import inspect


def test_import_bridge_client() -> None:
    import bridge_client  # noqa: PLC0415

    assert hasattr(bridge_client, "BridgePage")
    assert hasattr(bridge_client, "BridgeError")
    assert hasattr(bridge_client, "ElementNotFoundError")
    assert bridge_client.BRIDGE_URL == "ws://localhost:9333"


def test_error_hierarchy() -> None:
    from bridge_client import BridgeError, ElementNotFoundError  # noqa: PLC0415

    assert issubclass(BridgeError, Exception)
    assert issubclass(ElementNotFoundError, BridgeError)


def test_bridgepage_lazy_connect() -> None:
    """Instantiating BridgePage must not open a WebSocket connection."""
    from bridge_client import BridgePage  # noqa: PLC0415

    # If this opened a socket, it would fail when no server is running.
    page = BridgePage()
    assert page is not None
    # Custom URL should round-trip into the instance.
    page2 = BridgePage(bridge_url="ws://localhost:9444")
    assert page2 is not None


def test_public_api_surface() -> None:
    """Lock in the public method set so accidental removals fail loudly."""
    from bridge_client import BridgePage  # noqa: PLC0415

    expected = {
        # navigation
        "navigate",
        "reload_self",
        "wait_for_load",
        "wait_dom_stable",
        # JS execution
        "evaluate",
        "evaluate_function",
        # element queries
        "query_selector",
        "query_selector_all",
        "has_element",
        "wait_for_element",
        "get_element_text",
        "get_element_attribute",
        "get_elements_count",
        # element interactions
        "click_element",
        "input_text",
        "input_content_editable",
        "remove_element",
        "hover_element",
        "select_all_text",
        # scrolling
        "scroll_by",
        "scroll_to",
        "scroll_to_bottom",
        "scroll_element_into_view",
        "scroll_nth_element_into_view",
        "get_scroll_top",
        "get_viewport_height",
        # input events
        "press_key",
        "type_text",
        "mouse_move",
        "mouse_click",
        "dispatch_wheel_event",
        # file upload
        "set_file_input",
        # cookies / screenshot
        "get_cookies",
        "screenshot_element",
        # keep-alive browse
        "browse_open",
        "browse_do",
        "browse_close",
        "browse_and_eval",
        # status / misc
        "inject_stealth",
        "is_server_running",
        "is_extension_connected",
    }
    actual = {
        name
        for name, member in inspect.getmembers(BridgePage, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    missing = expected - actual
    assert not missing, f"missing public methods: {sorted(missing)}"
    # `target_id` is exposed as a property, not a regular method.
    assert isinstance(inspect.getattr_static(BridgePage, "target_id"), property)


def test_is_server_running_returns_bool_when_offline() -> None:
    """The server probe should never raise — it returns False on failure."""
    from bridge_client import BridgePage  # noqa: PLC0415

    # Use a port nothing should be listening on.
    page = BridgePage(bridge_url="ws://localhost:1")
    assert page.is_server_running() is False
    assert page.is_extension_connected() is False
