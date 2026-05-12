---
name: chrome-bridge
description: Browser automation that drives the user's real Chrome (with their logins, cookies, and SPA state) from Python. Use when an agent needs to scrape a logged-in page, click through a SPA, fill a form, dump cookies, take an element screenshot, or run multi-step interactions where a headless browser would fail CAPTCHAs, get blocked by CSP, or lose the user's session. Trigger keywords - "log in to", "scrape behind login", "real browser", "click and extract", "SPA", "Google Maps", "GitHub", "Reddit", "cookies", "session", "screenshot element", "fill form", "multi-step browse".
license: MIT
compatibility: Requires Python 3.10+, the `websockets` package, and Google Chrome (or any Chromium with extension support) on the same machine as the agent.
metadata:
  version: "1.0.3"
  homepage: https://github.com/your-org/chrome-bridge
---

# Chrome Bridge

A WebSocket relay + Chrome extension + Python client. The agent drives the
user's existing Chrome profile, so every request inherits real logins,
cookies, fingerprints, and extensions. Compared to headless Playwright /
Selenium / Puppeteer, this avoids login flows, CAPTCHA loops, and
SPA-renders-blank issues, at the cost of needing Chrome installed and one
manual extension install.

## Architecture

```
Python script  ─►  bridge_client.BridgePage
                       ↕  ws://localhost:9333
                   bridge_server.py
                       ↕  WebSocket
                   Chrome Extension (background.js)
                       ↕  chrome.scripting.executeScript / CDP
                   Browser tab (MAIN world)
```

| File | Role |
|------|------|
| `extension/` | Chrome MV3 extension (`manifest.json` + `background.js` + `content.js`) |
| `scripts/bridge_server.py` | WebSocket relay (port 9333 by default) |
| `scripts/bridge_client.py` | Python client (`BridgePage` class) |
| `scripts/watch_reload.py` | Standalone extension reloader (server has the same logic built in) |

## Preflight (agent: do this before any browser command)

1. **Is the server running?**

   ```bash
   curl -s http://localhost:9333 2>&1 | grep -q "WebSocket" && echo running || echo stopped
   ```

   If stopped, start it in the background. The exact command depends on your
   runtime — examples:

   ```bash
   # generic
   python /path/to/chrome-bridge/scripts/bridge_server.py &

   # uv
   uv run python /path/to/chrome-bridge/scripts/bridge_server.py &
   ```

   Wait ~2 seconds, re-run the curl check.

2. **Is the extension connected?**

   ```python
   from bridge_client import BridgePage
   BridgePage().is_extension_connected()  # True / False
   ```

   If `False`, stop and tell the user (the extension install is a one-time
   manual step — do not try to script it):

   > Chrome Bridge extension isn't connected. One-time setup:
   > 1. Open `chrome://extensions`
   > 2. Toggle "Developer mode" (top right)
   > 3. Click "Load unpacked" and select `<this-skill>/extension/`
   > 4. Confirm it's enabled, then tell me "ready"
   >
   > After that the extension auto-connects to ws://localhost:9333 on every
   > Chrome start; you won't see this again.

3. Only when both checks pass, issue `browse_*` / `navigate` / `evaluate`
   commands.

## Python API

### One-shot: open, evaluate, close

```python
from bridge_client import BridgePage

page = BridgePage()
title = page.browse_and_eval(
    url="https://example.com",
    expression="document.title",
    timeout=30000,
)
```

### Multi-step (persistent tab)

```python
tab = page.browse_open("https://example.com")
page.browse_do(tab["tab_id"], "document.querySelector('button.next').click()")
data = page.browse_do(tab["tab_id"], "document.body.innerText")
page.browse_close(tab["tab_id"])
```

Use this when you need click → wait → extract on a SPA (Google Maps, Gmail,
internal dashboards).

### Operating on the active (managed) tab

```python
page.navigate("https://example.com")  # navigates the active tab
page.evaluate("document.title")
page.click_element("button.submit")
page.has_element("#error-banner")
page.get_cookies(domain="example.com")  # empty domain = all cookies
page.screenshot_element("img.logo")     # returns PNG bytes
```

The "managed tab" is whichever tab is currently active (skipping
`chrome://` and `chrome-extension://` pages). If none are scriptable, a
blank tab is opened.

## Gotchas (read before debugging)

- **`chrome://` tabs are invisible** to the extension. If every tab is a
  `chrome://` page, a blank tab is opened instead.
- **Auto-reload caveat:** when you edit `manifest.json` or `background.js`,
  `bridge_server` triggers `chrome.runtime.reload()` automatically — but
  only after one initial manual reload (the first install needs to grant
  the `reload_self` hook).
- **Avoid `\\u` escapes in JS expressions** — Python's string literal +
  Chrome's serialiser interact badly. Use `String.fromCharCode()` or a
  Python raw triple-quoted string.
- **SPAs need explicit waits.** Pass `wait_selector` to `browse_open` /
  `browse_do`, or sleep after navigation. Status `complete` only means the
  initial document parsed, not that React/Vue have rendered.
- **Canvas / closed Shadow DOM** isn't reachable via DOM selectors. See
  `references/google-maps-saved-places.md` for the `innerText` workaround.
- **Strict-CSP sites** (GitHub, Reddit, …) are handled transparently by
  CSP-header stripping plus a CDP fallback. The CDP fallback shows a
  "[Chrome Bridge] is debugging this browser" banner — that's a Chrome
  security feature, not a bug. Details: `references/csp-cdp-fallback.md`.
- **`:contains()` is jQuery, not CSS.** `document.querySelector('span:contains(foo)')`
  throws. Use `document.body.innerText` and filter in Python instead.
- **Don't expect `page.eval_js`** — the method is `evaluate`.
- **Snapshot truncation:** if a tool that snapshots the DOM cuts off long
  pages, fall back to chunked `innerText`. See
  `references/spa-text-extraction.md`.

## Manual extension reload (when server is offline)

```bash
python scripts/watch_reload.py --once
```

## Permissions

`<all_urls>` is requested in `manifest.json`, so the extension can operate
on every page. After install or any extension upgrade, reload the extension
once in `chrome://extensions` so Chrome re-grants the wildcard.

## References

- `references/csp-cdp-fallback.md` — why GitHub/Reddit work; design of the
  CSP-strip + CDP fallback
- `references/google-maps-saved-places.md` — Canvas + Shadow DOM extraction
  pattern
- `references/spa-text-extraction.md` — chunked `innerText` recovery when
  another tool's DOM snapshot truncates
