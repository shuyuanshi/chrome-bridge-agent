# CSP Bypass + CDP Fallback — Design Notes

## Problem

`chrome.scripting.executeScript` internally uses `eval()` to serialise and
deserialise the function and its arguments. Pages with a strict CSP
(`script-src` without `'unsafe-eval'`) block it.

It surfaces in two ways:

- `executeScript` returns `[{result: null}]` (silent failure).
- It throws `Cannot access contents of url` or a CSP-violation error.

**Affected sites:** GitHub, Reddit, GitLab, Stack Overflow — anywhere with a
strict CSP.

## Solution (two layers)

### Layer 1 — Strip the CSP response header, **for bridge-owned tabs only**

`chrome.declarativeNetRequest` can drop the CSP header before the page parses
it. The scope of that rule is the whole design decision.

#### ⚠️ What not to do (this shipped in ≤1.0.3 and was a real vulnerability)

```javascript
// ❌ Strips CSP from EVERY site, for as long as the extension is enabled
chrome.declarativeNetRequest.updateDynamicRules({
  removeRuleIds: [9999],
  addRules: [{ id: 9999, priority: 1, action: STRIP_CSP,
    condition: { urlFilter: "*", resourceTypes: ["main_frame", "sub_frame"] } }],
});
```

Two multipliers make this worse than it looks. *Dynamic* rules persist across
browser restarts and extension updates, so the user's banking and webmail tabs
lose CSP 24/7 whether or not the bridge is even running. And removing the CSP
header also removes `frame-ancestors`, so sites that rely on it instead of
`X-Frame-Options` become framable. An XSS anywhere in the profile — normally
contained — becomes fully exploitable.

#### ✅ What 1.1.0 does

`RuleCondition.tabIds` narrows the strip to specific tabs, but it is supported
**only on session-scoped rules**, so the fix is a change of rule store, not just
an added field:

```javascript
async function syncCspRule() {
  const tabIds = [..._bridgeTabs];               // tabs the bridge itself opened
  await chrome.declarativeNetRequest.updateSessionRules({
    removeRuleIds: [CSP_RULE_ID],
    addRules: tabIds.length ? [{
      id: CSP_RULE_ID, priority: 1, action: STRIP_CSP,
      condition: { urlFilter: "*", resourceTypes: ["main_frame", "sub_frame"], tabIds },
    }] : [],
  });
}
```

Three consequences worth knowing:

- **Order matters.** A tab is created at `about:blank`, registered, the rule
  synced, and only *then* navigated — otherwise the real response arrives
  before the rule exists.
- **Upgrades must purge the old rule.** Session rules don't replace the
  persisted dynamic one, so the extension issues a one-time
  `updateDynamicRules({removeRuleIds: [CSP_RULE_ID]})` at startup.
- **Tabs the bridge did not open are out of scope** (e.g. one you attached to
  via `list_tabs`). Those fall through to Layer 2 instead — including the
  *silent* CSP failure, where `executeScript` returns nothing rather than
  throwing.

### ⚠️ Gotcha: `onInstalled` vs top-level

**Don't** register the rule inside `chrome.runtime.onInstalled`:

```javascript
// ❌ Doesn't fire on hot-reload!
chrome.runtime.onInstalled.addListener(() => { /* ... */ });
```

`bridge_server` reloads the extension via `chrome.runtime.reload()`, which
does **not** fire `onInstalled`. That event only fires on install, browser
update, or a manual "Load unpacked".

**Do** re-apply at the service worker's top level — every SW activation
(including hot-reload) restores the rule. Session rules are dropped when the
browser closes, which is exactly the lifetime you want here.

### Layer 2 — CDP `Runtime.evaluate` fallback

Header stripping covers most sites, but edge cases remain (CSP declared in a
`<meta http-equiv>`, other extension conflicts, etc.). Fall back to
chrome.debugger:

```javascript
async function executeWithCdpFallback(tabId, func, args) {
  try {
    // Prefer executeScript — faster and doesn't show a debugger banner.
    return await chrome.scripting.executeScript({
      target: { tabId }, world: "MAIN", func, args,
    });
  } catch (e) {
    // CSP-shaped error → switch to CDP
    if (!/Cannot access|extension|blocked|prohibited|Forbidden/i.test(e.message)) throw e;

    await chrome.debugger.attach({ tabId }, "1.3");
    const funcStr = func.toString();
    const argsJson = JSON.stringify(args);
    const expression = `(${funcStr})(...${argsJson})`;
    const cdpRes = await chrome.debugger.sendCommand({ tabId }, "Runtime.evaluate", {
      expression, awaitPromise: true, returnByValue: true,
    });
    await chrome.debugger.detach({ tabId });

    if (cdpRes.exceptionDetails) {
      throw new Error(cdpRes.exceptionDetails.exception?.description);
    }
    return [{ result: cdpRes.result.value }];
  }
}
```

**Notes:**

- The CDP path makes Chrome show a "[extension] is debugging this browser"
  banner at the top of the window. That's a Chrome security feature and
  can't be hidden.
- `Runtime.evaluate` runs in the page's MAIN world; all page globals are
  available.
- `awaitPromise: true` lets async expressions resolve before returning.
- `returnByValue: true` returns the result as JSON instead of a CDP
  RemoteObject handle.
- The wrapper returns `[{ result: value }]` so callers can't tell whether
  the fast path or the fallback handled the call.

## Wired in everywhere

All four execution paths in `background.js` route through
`executeWithCdpFallback`:

- `cmdEvaluateInMainWorld` — `evaluate`, `has_element`, `get_elements_count`, …
- `cmdDomInMainWorld` — click, input, scroll, …
- `cmdBrowseDo` — keep-alive `browse_do` expressions
- `cmdBrowseAndEval` — one-shot browse + eval

## Required permissions

```json
{
  "permissions": ["debugger", "declarativeNetRequest"],
  "host_permissions": ["<all_urls>"]
}
```

## References

- Chrome Extension MV3 `chrome.debugger` API:
  https://developer.chrome.com/docs/extensions/reference/api/debugger
- CDP `Runtime.evaluate`:
  https://chromedevtools.github.io/devtools-protocol/tot/Runtime/#method-evaluate
