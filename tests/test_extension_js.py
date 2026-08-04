"""Static guards on the extension. Most of the real logic lives in JS, which
had no lint, no test, and no CI step at all.

These are cheap invariants, not a JS test suite: a syntax check plus the
handful of properties whose violation caused real bugs.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parent.parent / "extension"
BACKGROUND = EXT / "background.js"
MANIFEST = EXT / "manifest.json"
RUNTIME_TEST = Path(__file__).resolve().parent / "js" / "test_background_runtime.mjs"


def _strip_comments(js: str) -> str:
    """Drop comments so these guards test the code, not the prose about it.

    (Several comments legitimately name the old, banned APIs while explaining
    why they were replaced.) ``//`` preceded by ``:`` is left alone so URLs
    like ``ws://localhost`` survive.
    """
    js = re.sub(r"/\*.*?\*/", "", js, flags=re.DOTALL)
    return re.sub(r"(?<!:)//.*", "", js)


@pytest.fixture(scope="module")
def source() -> str:
    return _strip_comments(BACKGROUND.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_background_js_parses() -> None:
    subprocess.run(["node", "--check", str(BACKGROUND)], check=True, capture_output=True)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_background_runtime_behaviour() -> None:
    subprocess.run(["node", str(RUNTIME_TEST)], check=True, capture_output=True)


def test_manifest_is_valid_and_declares_what_the_code_uses(manifest: dict) -> None:
    assert manifest["manifest_version"] == 3
    assert manifest["background"]["service_worker"] == "background.js"
    for permission in (
        "tabs",
        "cookies",
        "scripting",
        "storage",
        "debugger",
        "declarativeNetRequest",
    ):
        assert permission in manifest["permissions"], permission


def test_no_content_script_is_injected(manifest: dict) -> None:
    """content.js was 212 lines of unreachable code injected into every page
    the user ever loaded — including their bank. It must stay gone."""
    assert "content_scripts" not in manifest
    assert not (EXT / "content.js").exists()


def test_nothing_talks_to_a_content_script(source: str) -> None:
    assert "chrome.tabs.sendMessage" not in source


def test_csp_stripping_is_scoped_to_bridge_tabs(source: str) -> None:
    """The CSP strip must be a *session* rule limited to tabIds.

    The old dynamic <all_urls> rule disabled CSP for the entire browsing
    profile, permanently, even when the bridge wasn't running.
    """
    assert "updateSessionRules" in source
    assert "tabIds" in source
    # The only permitted dynamic-rule call is the one that purges the legacy rule.
    dynamic_calls = source.count("updateDynamicRules")
    assert dynamic_calls == 1, "dynamic (persistent) DNR rules must not be added"
    assert "removeRuleIds: [CSP_RULE_ID]" in source


def test_cdp_fallback_also_covers_the_silent_csp_failure(source: str) -> None:
    """A strict CSP can drop the injection without throwing. Tabs the user
    opened (attached via list_tabs) are outside the CSP-strip scope, so that
    path has to fall back too, or the caller silently gets None."""
    assert "value === undefined && !_bridgeTabs.has(tabId)" in source


def test_screenshots_do_not_capture_the_users_foreground_tab(source: str) -> None:
    """captureVisibleTab grabs whatever the user is looking at, not the driven
    tab — it leaked unrelated pages into the agent's context."""
    assert "captureVisibleTab" not in source
    assert "Page.captureScreenshot" in source


def test_input_goes_through_the_native_value_setter(source: str) -> None:
    """Assigning el.value directly is invisible to React's value tracker."""
    assert "getOwnPropertyDescriptor" in source
    assert "setNativeValue" in source


def test_snapshot_masks_secret_field_values(source: str) -> None:
    """A snapshot goes straight into an LLM's context and the transcript, so an
    autofilled password field must never travel with it."""
    snapshot = source.split('case "snapshot"')[1].split('case "act_ref"')[0]
    assert '"password"' in snapshot
    assert '"***"' in snapshot
    assert "one-time-code" in snapshot


def test_snapshot_only_reports_checked_for_checkable_roles(source: str) -> None:
    """el.checked is a boolean on every input, so an ungated copy labels text
    boxes 'unchecked' and invites the model to click them."""
    assert "CHECKABLE" in source
    assert "CHECKABLE.test(item.role)" in source


def test_full_page_screenshot_computes_a_clip(source: str) -> None:
    """captureBeyondViewport alone no longer expands to the document."""
    assert "Page.getLayoutMetrics" in source
    assert "cssContentSize" in source


def test_chunked_reads_return_a_utf16_cursor(source: str) -> None:
    """Python len() counts code points; the page slices in UTF-16 units. The
    page must hand back the cursor, and must never cut a surrogate pair — a
    half-emoji cannot be reassembled by concatenation on the Python side."""
    assert "function safeSlice" in source
    assert "0xdbff" in source
    for case in ('case "get_text"', 'case "read_buffer"'):
        block = source.split(case)[1][:600]
        assert "next:" in block, case
        assert "safeSlice(" in block, case


def test_page_errors_carry_machine_readable_codes(source: str) -> None:
    for code in (
        "ELEMENT_NOT_FOUND",
        "TAB_GONE",
        "JS_ERROR",
        "STALE_REF",
        "NAV_TIMEOUT",
        "DEBUGGER_BUSY",
        "RESTRICTED_URL",
    ):
        assert code in source, code


def test_opening_a_tab_survives_having_no_browser_window(source: str) -> None:
    """Chrome keeps the service worker alive after the last window closes (the
    "continue running background apps" setting), and `tabs.create` without a
    windowId then throws "No current window" — which took the whole bridge
    down rather than just opening a window."""
    assert "function newBackgroundTab" in source
    assert "no current window" in source.lower()
    assert "chrome.windows.create" in source
    assert "NO_BROWSER_WINDOW" in source


def test_closing_a_tab_does_not_block_on_window_teardown(source: str) -> None:
    """chrome.tabs.remove() settles only after the tab — and its window, if it
    was the last one — has torn down, which can outlast the caller's deadline
    and turn a successful close into a spurious TIMEOUT."""
    discard = source.split("async function discardBridgeTab")[1].split(
        "chrome.tabs.onRemoved.addListener"
    )[0]
    close = source.split("async function cmdBrowseClose")[1][:600]
    assert "Promise.race" in discard
    assert "chrome.tabs.remove" in discard
    assert "discardBridgeTab" in close


def test_waits_are_driven_from_the_service_worker(source: str) -> None:
    """Chrome throttles setTimeout in background tabs to minute-scale, so an
    in-page polling loop makes every wait crawl. The SW isn't throttled."""
    router = source.split("async function handleCommand")[1].split("async function resolveTab")[0]
    assert 'case "wait_for_selector"' in router
    assert 'case "wait_dom_stable"' in router
    assert "async function cmdWaitDomStable" in source


def test_state_survives_service_worker_eviction(source: str) -> None:
    assert "chrome.storage.session" in source
    assert "chrome.tabs.onRemoved" in source


def test_reconnect_uses_backoff(source: str) -> None:
    assert "_reconnectDelay" in source
    assert "setTimeout(connect, 3000)" not in source
