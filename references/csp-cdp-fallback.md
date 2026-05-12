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

### Layer 1 — Strip the CSP response header

`chrome.declarativeNetRequest.updateDynamicRules` can drop the CSP header
before the page parses it:

```javascript
chrome.declarativeNetRequest.updateDynamicRules({
  removeRuleIds: [9999],
  addRules: [{
    id: 9999, priority: 1,
    action: {
      type: "modifyHeaders",
      responseHeaders: [
        { header: "content-security-policy", operation: "remove" },
        { header: "content-security-policy-report-only", operation: "remove" },
      ],
    },
    condition: { urlFilter: "*", resourceTypes: ["main_frame", "sub_frame"] },
  }],
});
```

With no CSP header on the response, `eval()` runs freely in the page.

### ⚠️ Gotcha: `onInstalled` vs top-level

**Don't** put the rule inside `chrome.runtime.onInstalled`:

```javascript
// ❌ Doesn't fire on hot-reload!
chrome.runtime.onInstalled.addListener(() => {
  chrome.declarativeNetRequest.updateDynamicRules({...});
});
```

`bridge_server` reloads the extension via `chrome.runtime.reload()`, which
does **not** fire `onInstalled`. That event only fires on install, browser
update, or a manual "Load unpacked".

**Do** put it at the service worker's top level — every SW activation
(including hot-reload) re-applies the rule:

```javascript
// ✅ Runs on every SW activation, including hot-reload
chrome.declarativeNetRequest.updateDynamicRules({...});
```

`updateDynamicRules` is idempotent — repeated calls don't stack duplicate
rules.

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
