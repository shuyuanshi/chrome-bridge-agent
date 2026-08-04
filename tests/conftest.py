"""Shared pytest config for chrome-bridge-agent.

Implements the auto-skip pattern documented in
``references/integration-test-pattern.md``: any test marked
``@pytest.mark.integration`` is skipped when the Bridge isn't reachable.
"""

from __future__ import annotations

from functools import lru_cache

import pytest


@lru_cache(maxsize=1)
def _bridge_available() -> bool:
    """Probe the Chrome Bridge once per test session.

    Returns True only if the server is up, the extension is connected, *and*
    Chrome actually has a window to drive. The service worker outlives the last
    window when "continue running background apps" is on, so a connected
    extension alone is not enough — without this the whole suite errors out
    with "No current window" instead of skipping.
    """
    try:
        from bridge_client import BridgePage  # noqa: PLC0415 — lazy import on purpose

        page = BridgePage()
        return bool(page.is_extension_connected() and page.list_tabs())
    except Exception:
        return False


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Auto-skip integration tests when no live Bridge is reachable."""
    if item.get_closest_marker("integration") is not None and not _bridge_available():
        pytest.skip("Chrome Bridge unavailable (no local server / extension)")
