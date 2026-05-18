---
name: chrome-bridge-agent
description: Browser automation that drives the user's real Chrome (with their logins, cookies, and SPA state) from Python. Use when an agent needs to scrape a logged-in page, click through a SPA, fill a form, dump cookies, take an element screenshot, or run multi-step interactions where a headless browser would fail CAPTCHAs, get blocked by CSP, or lose the user's session. Trigger keywords - "log in to", "scrape behind login", "real browser", "click and extract", "SPA", "Google Maps", "GitHub", "Reddit", "cookies", "session", "screenshot element", "fill form", "multi-step browse", "xiaohongshu", "instagram", "twitter".
license: MIT
compatibility: Requires Python 3.10+, the `websockets` package, and Google Chrome (or any Chromium with extension support) on the same machine as the agent.
metadata:
  version: "1.0.3"
  homepage: https://github.com/shuyuanshi/chrome-bridge-agent
---

# Chrome Bridge

Browser-automation infrastructure: a WebSocket relay + a Chrome extension +
a Python client. Every request runs inside the user's existing Chrome
profile, so it inherits real logins, cookies, fingerprints, and extensions.
Compared with headless Playwright / Selenium / Puppeteer, this skips login
flows, CAPTCHA loops, and SPA-renders-blank pages — at the cost of needing
Chrome installed and one manual extension install.

## Architecture

```
Python script  ─►  bridge_client.BridgePage
                       ↕  ws://localhost:9333
                   bridge_server.py  (async WebSocket relay)
                       ↕  WebSocket
                   Chrome extension  (MV3 background.js)
                       ↕  chrome.scripting.executeScript / CDP
                   Browser tab (MAIN world)
```

| File | Role |
|------|------|
| `extension/` | Chrome MV3 extension (`manifest.json` + `background.js` + `content.js`) |
| `scripts/bridge_server.py` | WebSocket relay (port 9333 by default) |
| `scripts/bridge_client.py` | Python client (`BridgePage` class) |
| `scripts/watch_reload.py` | Standalone extension reloader (server has the same logic built in) |

## Python API

### One-shot (open → eval → close)

```python
from bridge_client import BridgePage

page = BridgePage()
title = page.browse_and_eval(
    url="https://example.com",
    expression="document.title",
    timeout=30000,
)
```

Wraps `browse_open → browse_do → browse_close`. Use for a single
fire-and-forget extraction.

### Multi-step (persistent tab)

```python
tab = page.browse_open("https://example.com")     # persistent tab
page.browse_do(tab["tab_id"], "click_something")  # step 1
page.browse_do(tab["tab_id"], "extract_data")     # step 2
page.browse_close(tab["tab_id"])                  # cleanup
```

Use when you need click → wait → extract on a SPA (Google Maps, Gmail,
internal dashboards).

### Operating on the active (managed) tab

```python
page.navigate(url)        # navigate the managed tab
page.evaluate(js)         # run JS, returns the value
page.click_element(sel)   # click
page.has_element(sel)     # bool
page.get_cookies(domain)  # cookies, optionally filtered by domain
page.reload_self()        # reload the extension (after editing manifest / background.js)
```

The managed tab is a persistent background tab the extension keeps for you;
it does *not* hijack whichever tab is in front of the user.

## Gotchas

- **`navigate()` + `evaluate()` is unsafe on SPAs.** Calling `evaluate()`
  immediately after `navigate()` regularly fails on a login-walled classroom SPA, React/Next.js,
  Vue, etc. — the JS runs before hydration finishes. **For a one-shot SPA
  scrape, always use `page.browse_and_eval()`** (it waits for the page to
  settle first). **For multi-step SPA interactions** (click filter → wait
  → extract), chain `navigate()` + `wait_for_load()` + multiple
  `evaluate()` + `wait_dom_stable()` calls — see
  [`references/multi-step-spa-pattern.md`](references/multi-step-spa-pattern.md).
- **`chrome://` and `chrome-extension://` tabs are invisible.**
  `getOrOpenManagedTab()` skips them and opens a fresh background tab if
  needed.
- **Auto-reload.** Editing `manifest.json` or `background.js` triggers
  `chrome.runtime.reload()` automatically — but only after one initial
  manual reload to grant the `reload_self` hook. See "Auto-reload" below.
- **Avoid `\\u` escapes in JS expressions.** Python's string literal and
  Chrome's serialiser interact badly. Use `String.fromCharCode()` or a
  Python raw triple-quoted string.
- **SPAs need explicit waits.** Pass `wait_selector` to `browse_open` /
  `browse_do`, or sleep after navigation. Tab status `complete` only means
  the initial document parsed, not that React / Vue have rendered.
- **Canvas / closed Shadow DOM** isn't reachable via DOM selectors. Google
  Maps places, for example, are rendered to Canvas. Use the `innerText`
  pattern in [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md).
- **`page.eval_js` doesn't exist.** The method is `evaluate`.
- **Strict-CSP sites** (GitHub, Reddit, GitLab) are handled transparently
  via two layers: a CSP-header strip and a CDP fallback when
  `executeScript` is rejected. The CDP fallback shows a "is debugging this
  browser" banner — that's a Chrome security feature, not a bug. Details:
  [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md).
- **`httpOnly` cookies can't be set via `document.cookie`.** Sites whose
  auth token is `httpOnly` (e.g. Zhihu's `z_c0`) need
  `page.get_cookies(domain)` to read what the user's browser already has,
  not file-based injection. See
  [`references/cookie-injection-auth.md`](references/cookie-injection-auth.md).
- **Snapshot truncation → chunked `innerText`.** If a separate tool that
  snapshots the DOM truncates large SPAs (Next.js, virtualised lists),
  fall back to chunked `document.body.innerText.substring(start, end)`
  reads via the bridge. See
  [`references/spa-text-extraction.md`](references/spa-text-extraction.md).
- **JSON returned from JS must be parsed with `json.loads`.** JS's `true` /
  `false` / `null` are not Python keywords; `eval()` raises `NameError`.
- **Vue/React combobox often hides a real `<select>`.** Custom
  `[role="combobox"]` widgets in libraries like Element Plus frequently
  have a hidden native `<select>` underneath. Setting
  `select.value = ...; select.dispatchEvent(new Event('change'))` is far
  more reliable than clicking through the popup's `[role="option"]`
  entries. `document.querySelectorAll('select')` first.
- **`:contains()` is jQuery, not CSS.**
  `document.querySelector('span:contains(foo)')` throws "not a valid
  selector". Read `document.body.innerText` and filter in Python.
- **Long-running JS exceeds the 90s call timeout.** If you need to poll
  for minutes (export N items, wait for a long render), drive the loop
  from Python and issue one `browse_do` per iteration. Don't write
  `for i in range(N): setTimeout(...)` inside a single `evaluate`.
- **Single shared managed tab — race condition under parallelism.** The
  extension keeps exactly one managed tab. Running two scripts that both
  call `navigate()` against the same `BridgePage` will let the later
  caller overwrite the earlier one, and the earlier script silently scrapes
  the wrong page. **Defences**: (a) serialise dependent scripts at the
  scheduler layer; (b) before every `evaluate()` / `browse_and_eval()`,
  verify `window.location.hostname` matches the expected domain and
  re-navigate (or `raise RuntimeError`) on mismatch.
- **`evaluate()` accepts expressions, not statements.** The extension wraps
  the input as `new Function("use strict"; return (<expr>))()`, so bare
  `var x = ...` / `const x = ...` / `if (...) {...}` raise
  "Unexpected token 'var'". Wrap statements in an IIFE:
  `(function() { var x = document.querySelectorAll("button"); x[0].click(); return "clicked"; })()`.
  An IIFE *is* a valid expression. `evaluate_function` has the same rule:
  pass a function body that returns (`return document.title;`), not a
  sequence of bare statements.
- **Bridge tab no longer steals focus** (2026-05-16 fix). All bridge
  operations now run in a persistent background tab; the user's active
  window is never hijacked. After upgrading, manually reload the extension
  once at `chrome://extensions` so `background.js` picks up the new
  behaviour. See [`references/background-tab-fix.md`](references/background-tab-fix.md).

### Explore the DOM before writing a Bridge script

Writing CSS selectors and debugging blindly is slow. The right flow:

1. Navigate to the target page with whatever browser/console tool you have.
2. Run exploratory JS:

   ```js
   // find interactive elements
   document.querySelectorAll('input[type="checkbox"]')
     .forEach(cb => console.log(cb.id, cb.parentElement.tagName));
   // find data containers
   document.querySelectorAll('.card, .item, [class*="result"]').length;
   // many SPAs use role="heading" instead of <h1>..<h6>
   document.querySelectorAll('[role="heading"]').length;
   ```

3. Only commit to a selector once you've confirmed it matches in the live
   page.

Common traps: assuming `<h4>` exists (SPAs often use
`<div role="heading" aria-level="4">`); assuming the checkbox is *inside*
its `<label>`; assuming the page renders all rows at once instead of
virtualising them.

## Auto-reload

`bridge_server.py` hashes `extension/manifest.json` and
`extension/background.js` on startup and polls every 2 s. When either
changes, it sends `reload_self` over the WebSocket and the extension calls
`chrome.runtime.reload()`.

No extra command, no manual click. Just start the server.

> **First-time setup**: you have to reload the extension manually once in
> `chrome://extensions` so the `reload_self` hook becomes live. After that
> the server handles every subsequent reload.

Standalone fallback (when the server isn't running):

```bash
python scripts/watch_reload.py --once
```

## Quick start

```bash
# 1. Load the extension in Chrome:
#    chrome://extensions → toggle Developer mode → "Load unpacked"
#    → select the extension/ directory.
# 2. Set Site access to "On all sites" (see Permissions below).

# 3. Start the bridge server (long-running):
python scripts/bridge_server.py
# or:
uv run python scripts/bridge_server.py

# 4. Call from Python:
from bridge_client import BridgePage
page = BridgePage()
page.navigate("https://example.com")
print(page.evaluate("document.title"))
```

## Preflight checklist for agents

Any agent that uses this bridge **must** verify both of these before
issuing browser commands:

### 1. Is the server running?

```bash
curl -s http://localhost:9333 2>&1 | grep -q "WebSocket" && echo running || echo stopped
```

If stopped, start it as a background process. Examples:

```bash
# generic
python /path/to/chrome-bridge-agent/scripts/bridge_server.py &

# uv
uv run python /path/to/chrome-bridge-agent/scripts/bridge_server.py &
```

Wait ~2 s and re-run the `curl` check.

### 2. Is the Chrome extension connected?

The server can be up while no extension is connected. Probe explicitly:

```python
from bridge_client import BridgePage
BridgePage().is_extension_connected()  # True / False
```

If `False`, **stop and prompt the user** — extension install is a one-time
manual action; don't try to script it:

> Chrome Bridge extension isn't connected. One-time setup:
> 1. Open `chrome://extensions`
> 2. Toggle "Developer mode" (top right)
> 3. Click "Load unpacked" and select the `extension/` directory of this
>    repo
> 4. On the extension card, click **Details** → **Site access** → set to
>    **"On all sites"**. Chrome 112+ requires this for `cookies.getAll`
>    and cross-site DOM operations even though the manifest declares
>    `<all_urls>`.
> 5. Confirm the extension is enabled, then say "ready".
>
> After that the extension auto-connects to `ws://localhost:9333` every
> time Chrome starts — you won't see this again.

If the extension is installed but reports disconnected, ask the user to
click the ↻ button on the extension's card in `chrome://extensions`.

### 3. Only after both checks pass

Issue `browse_*` / `navigate` / `evaluate` commands.

## Permissions

`manifest.json` requests `<all_urls>`, but **Chrome 112+ does not grant it
implicitly**. The user must open the extension's **Details** in
`chrome://extensions` and switch **Site access → "On all sites"**.
Without this:

- `navigate` / `evaluate` / `click_element` still work on the active tab
  via the `activeTab` permission, but
- `chrome.cookies.getAll({})` silently returns cookies only for the
  domains the user has individually granted, and
- `executeScript` may fail on tabs that weren't directly user-activated.

Only one Chrome Bridge extension should be loaded at a time. If two are
installed (e.g. an old copy in another directory), both connect to
`ws://localhost:9333` and the relay routes to whichever connected last —
behaviour becomes non-deterministic.

## References

- [`references/anti-bot-sites.md`](references/anti-bot-sites.md) —
  catalogue of sites that block headless/cloud browsers and require a
  real, signed-in Chrome.
- [`references/background-tab-fix.md`](references/background-tab-fix.md) —
  design of the 2026-05-16 "don't steal focus" fix.
- [`references/cookie-injection-auth.md`](references/cookie-injection-auth.md)
  — live cookie extraction vs. file injection, `httpOnly` limits,
  graceful degradation when auth has expired.
- [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md) —
  how CSP header stripping + CDP fallback combine to handle GitHub /
  Reddit / GitLab.
- [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md)
  — Canvas + Shadow-DOM extraction pattern.
- [`references/integration-test-pattern.md`](references/integration-test-pattern.md)
  — `pytest` auto-skip pattern for skills that depend on a live Bridge,
  so the same suite runs in CI and locally.
- [`references/multi-step-spa-pattern.md`](references/multi-step-spa-pattern.md)
  — chaining `navigate` + `evaluate` + `wait_*` for SPAs that need clicks
  between extractions.
- [`references/spa-text-extraction.md`](references/spa-text-extraction.md)
  — chunked `innerText` recovery when a DOM snapshot truncates.
- `scripts/bridge_client.py` — `BridgePage` method definitions.
