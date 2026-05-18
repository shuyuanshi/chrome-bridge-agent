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

    Returns True only if the server is up *and* the extension is connected.
    Any exception (network, import, timeout) means "not available".
    """
    try:
        from bridge_client import BridgePage  # noqa: PLC0415 — lazy import on purpose

        return BridgePage().is_extension_connected()
    except Exception:
        return False


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Auto-skip integration tests when no live Bridge is reachable."""
    if item.get_closest_marker("integration") is not None and not _bridge_available():
        pytest.skip("Chrome Bridge unavailable (no local server / extension)")
