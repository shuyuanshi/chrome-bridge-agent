"""Smoke tests that exercise import + API surface without a live Bridge.

These run in CI without Chrome.
"""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The full public surface. This is an *exact* match, not a subset: adding a
# method here is the moment to also document it in SKILL.md (there is a test
# below that enforces that), otherwise agents never learn it exists.
PUBLIC_API = {
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
    "get_url",
    "get_html",
    "read_text",
    # agent-native snapshot
    "snapshot",
    "snapshot_text",
    "act",
    # authenticated fetch
    "fetch",
    "fetch_json",
    # element interactions
    "click_element",
    "close_owned_tabs",
    "input_text",
    "input_content_editable",
    "select_option",
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
    "cdp_mouse",
    # file upload
    "set_file_input",
    # cookies / screenshot
    "get_cookies",
    "screenshot",
    "screenshot_element",
    # sessions
    "tab",
    "list_tabs",
    "list_sessions",
    "activate_tab",
    "browse_open",
    "browse_do",
    "browse_close",
    "browse_and_eval",
    # status / misc
    "inject_stealth",
    "status",
    "is_server_running",
    "is_extension_connected",
}

# Kept only so old scripts keep importing; deliberately not advertised.
UNDOCUMENTED_OK = {"query_selector", "query_selector_all", "inject_stealth"}


def test_import_bridge_client() -> None:
    import bridge_client  # noqa: PLC0415

    assert hasattr(bridge_client, "BridgePage")
    assert hasattr(bridge_client, "BridgeError")
    assert hasattr(bridge_client, "ElementNotFoundError")
    assert bridge_client.BRIDGE_URL == "ws://localhost:9333"


def test_release_version_is_coordinated() -> None:
    from bridge_client import CLIENT_VERSION  # noqa: PLC0415
    from bridge_server import SERVER_VERSION  # noqa: PLC0415

    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    skill = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    manifest = json.loads((REPO_ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))

    project_match = re.search(r'(?m)^version = "([^"]+)"$', pyproject)
    lock_match = re.search(r'(?m)^name = "chrome-bridge-agent"\nversion = "([^"]+)"$', lock)
    skill_match = re.search(r'(?m)^  version: "([^"]+)"$', skill)
    assert project_match and lock_match and skill_match

    versions = {
        "project": project_match.group(1),
        "lock": lock_match.group(1),
        "manifest": manifest["version"],
        "client": CLIENT_VERSION,
        "server": SERVER_VERSION,
        "skill": skill_match.group(1),
    }
    assert len(set(versions.values())) == 1, versions


def test_error_hierarchy() -> None:
    """Every typed error is catchable as BridgeError, and codes are unique."""
    import bridge_client as bc  # noqa: PLC0415

    types = [
        bc.BridgeConnectionError,
        bc.BridgeAuthError,
        bc.ExtensionNotConnectedError,
        bc.BridgeTimeoutError,
        bc.ElementNotFoundError,
        bc.TabGoneError,
        bc.JSEvalError,
        bc.StaleRefError,
        bc.NavigationTimeoutError,
    ]
    for cls in types:
        assert issubclass(cls, bc.BridgeError)
    codes = [cls.code for cls in types]
    assert len(codes) == len(set(codes))


def test_bridgepage_lazy_connect() -> None:
    """Instantiating BridgePage must not open a WebSocket connection."""
    from bridge_client import BridgePage  # noqa: PLC0415

    assert BridgePage() is not None
    assert BridgePage(bridge_url="ws://localhost:9444") is not None


def test_public_api_surface_is_exact() -> None:
    """Lock the public method set: removals *and* silent additions both fail."""
    from bridge_client import BridgePage  # noqa: PLC0415

    actual = {
        name
        for name, _member in inspect.getmembers(BridgePage, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    assert actual == PUBLIC_API, {
        "missing": sorted(PUBLIC_API - actual),
        "undeclared": sorted(actual - PUBLIC_API),
    }
    assert isinstance(inspect.getattr_static(BridgePage, "target_id"), property)


def test_every_public_method_is_documented() -> None:
    """An undocumented method may as well not exist — no agent will call it.

    (``cdp_mouse`` shipped and stayed invisible for exactly this reason.)
    """
    docs = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
    undocumented = sorted(m for m in PUBLIC_API - UNDOCUMENTED_OK if m not in docs)
    assert not undocumented, f"not mentioned in SKILL.md: {undocumented}"


def test_tab_is_a_bridgepage_bound_to_one_tab() -> None:
    from bridge_client import BridgePage, Tab  # noqa: PLC0415

    assert issubclass(Tab, BridgePage)
    tab = Tab("ws://localhost:9444", tab_id="9", token="t")
    assert tab.tab_id == "9"
    assert tab.target_id == "9"
    assert hasattr(tab, "__enter__") and hasattr(tab, "__exit__")


def test_is_server_running_returns_bool_when_offline() -> None:
    """The server probe should never raise — it returns False on failure."""
    from bridge_client import BridgePage  # noqa: PLC0415

    page = BridgePage(bridge_url="ws://localhost:1")
    assert page.is_server_running() is False
    assert page.is_extension_connected() is False
    assert page.status()["error"] == "CONNECTION_FAILED"


def test_connection_error_names_installed_and_source_server_commands() -> None:
    from bridge_client import BridgeConnectionError, BridgePage  # noqa: PLC0415

    with pytest.raises(BridgeConnectionError) as excinfo:
        BridgePage(bridge_url="ws://localhost:1").evaluate("1")
    message = str(excinfo.value)
    assert "chrome-bridge-server" in message
    assert "python3 scripts/bridge_server.py" in message


def test_cli_entrypoint_exists_and_reports_failure() -> None:
    from bridge_client import main  # noqa: PLC0415

    assert main(["--bridge-url", "ws://localhost:1", "status"]) == 0  # status never fails hard
    assert main(["--bridge-url", "ws://localhost:1", "eval", "1"]) == 1


def test_token_helpers_never_widen_permissions(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    import stat  # noqa: PLC0415

    import bridge_auth  # noqa: PLC0415

    target = tmp_path / "token"
    monkeypatch.setenv("CHROME_BRIDGE_TOKEN_FILE", str(target))
    monkeypatch.delenv("CHROME_BRIDGE_TOKEN", raising=False)

    token = bridge_auth.ensure_token()
    assert len(token) == 64
    assert bridge_auth.read_token() == token
    assert bridge_auth.ensure_token() == token  # idempotent
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    # A file restored from a backup under a permissive umask must be tightened
    # on the next start, not silently trusted — the README promises this.
    target.chmod(0o644)
    assert bridge_auth.ensure_token() == token
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
