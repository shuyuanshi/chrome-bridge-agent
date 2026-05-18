# Bridge Runs in the Background (2026-05-16)

## Problem

Chrome Bridge used to steal focus:

1. `browse_open` opened new tabs with `active: true`, so Chrome switched
   the user's view to the new tab.
2. `getOrOpenManagedTab` reused the user's *active* tab, so the page the
   user was looking at was navigated out from under them.

## Fix

### `cmdBrowseOpen` — used by `browse_open` / `browse_do` / `browse_close`

`extension/background.js`: `active: true` → `active: false`.

```diff
-async function cmdBrowseOpen({ url, timeout = 60000 }) {
-  const tab = await chrome.tabs.create({ url, active: true });
+async function cmdBrowseOpen({ url, timeout = 60000 }) {
+  const tab = await chrome.tabs.create({ url, active: false });
```

### `getOrOpenManagedTab` — used by `navigate` / `evaluate`

`extension/background.js`: rewritten to keep one persistent background tab
instead of hijacking the user's active tab.

```diff
-async function getOrOpenManagedTab() {
-  const [activeTab] = await chrome.tabs.query({ active: true, currentWindow: true });
-  if (activeTab && /* …scriptable… */ ) return activeTab;
-  const tabs = await chrome.tabs.query({});
-  …
-  const tab = await chrome.tabs.create({ url: "about:blank" });
+async function getOrOpenManagedTab() {
+  if (_managedTabId) {
+    try {
+      const existing = await chrome.tabs.get(_managedTabId);
+      if (existing) return existing;
+    } catch (e) { /* tab was closed; fall through */ }
+  }
+  const tab = await chrome.tabs.create({ url: "about:blank", active: false });
+  _managedTabId = tab.id;
```

Plus a new module-level variable:

```js
let _managedTabId = null;  // persistent background tab ID
```

## Activating the fix

1. Reload the extension manually once in `chrome://extensions` (click the
   ↻ button on the extension card).
2. After that, `bridge_server`'s auto-reload picks up future changes.

## Side-effect: race conditions

Because the managed tab is shared and persistent, two scripts that both
call `navigate()` will race — the second `navigate()` clobbers the page
the first one was about to read. Either serialise dependent scripts at
the scheduler layer, or have each script verify
`window.location.hostname` before extracting and re-navigate (or
`raise RuntimeError`) on mismatch.
