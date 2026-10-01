# Background Windows, Scoped Runs, and Focus Safety

Chrome Bridge v2.1 separates automation from the user's live browsing at three
layers.

## Dedicated automation window

Every bridge-created tab is placed in one same-profile Chrome window created
with `focused: false` and `state: "minimized"`. Subsequent tabs always specify
that window's ID; the extension never lets `chrome.tabs.create()` default to
the user's current window.

The profile is selected by where the extension is installed. Chrome extension
APIs cannot switch profiles per command. Install the extension in a dedicated
profile when OS-level separation is more important than sharing the user's
existing cookies and login state.

## Ownership scopes

Each `BridgePage` sends an opaque `scope_id`. The extension namespaces managed
tabs and named sessions by that scope and records `tab_id -> scope_id` for every
tab it creates. Tabs opened with `window.open` or `target=_blank` inherit the
scope of their bridge-owned opener when Chrome exposes `openerTabId`.

`close_owned_tabs()` removes only the caller's tabs. `browse_close()` refuses
both user-owned tabs and tabs owned by another scope. This makes task cleanup
safe while multiple agents share the extension.

Cleanup never calls `chrome.windows.remove()`. It refuses to remove any owned
tab after that tab leaves the automation window, avoiding even a last-tab race
that could make Chrome close a user window implicitly. Only the unfocused
automation window may disappear when its final owned tab closes.

Wrap a task in an outer context so cleanup happens on success and failure:

```python
from bridge_client import BridgePage

with BridgePage() as page:
    with page.tab("https://example.com") as tab:
        print(tab.snapshot_text())
```

## Foreground-required input

DOM actions, page evaluation, snapshots, fetches, and CDP pointer moves remain
background-only. Chrome may drop trusted CDP press/release events when the
target renderer has no live foreground surface. `cdp_mouse()` click/drag
therefore raises `FOREGROUND_REQUIRED` by default instead of focusing Chrome.

`cdp_mouse(..., activate=True)` and
`activate_tab(allow_foreground=True)` are deliberate opt-ins. They may bring
Chrome in front of another macOS application, which an extension cannot
reliably restore; obtain the user's permission first.

## Activating extension changes

Run `chrome-bridge reload`. If the connected service worker is too old to
receive that command, open `chrome://extensions` and click the reload button on
the Chrome Bridge card. Reinstalling is unnecessary when the unpacked extension
already points at this repository's `extension/` directory.
