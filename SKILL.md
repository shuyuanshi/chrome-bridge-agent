---
name: chrome-bridge-agent
description: Browser automation that drives the user's real Chrome (with their logins, cookies, and SPA state) from Python. Use when an agent needs to scrape a logged-in page, click through a SPA, fill a form, call an internal API with the browser's session, dump cookies, take an element screenshot, or run multi-step interactions where a headless browser would fail CAPTCHAs, get blocked by CSP, or lose the user's session. Trigger keywords - "log in to", "scrape behind login", "real browser", "click and extract", "SPA", "internal dashboard", "Google Maps", "GitHub", "Reddit", "cookies", "session", "screenshot element", "fill form", "multi-step browse", "xiaohongshu", "instagram", "twitter".
license: MIT
compatibility: Requires Python 3.10+, the `websockets` package, and Google Chrome (or any Chromium with extension support) on the same machine as the agent.
metadata:
  version: "1.1.0"
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
Python script  ─►  bridge_client.BridgePage / Tab
                       ↕  ws://localhost:9333   (token-authenticated)
                   bridge_server.py  (async WebSocket relay)
                       ↕  WebSocket
                   Chrome extension  (MV3 background.js)
                       ↕  chrome.scripting.executeScript / CDP
                   Browser tab (MAIN world)
```

| File | Role |
|------|------|
| `extension/` | Chrome MV3 extension (`manifest.json` + `background.js`) |
| `scripts/bridge_server.py` | WebSocket relay (port 9333 by default) |
| `scripts/bridge_client.py` | Python client (`BridgePage`, `Tab`) + `chrome-bridge` CLI |
| `scripts/bridge_auth.py` | Shared-token helpers |

## Preflight (do this before any browser command)

```bash
python3 scripts/bridge_client.py status
# {"extension_connected": true, "extension_version": "1.1.0", "pending": 0,
#  "server_version": "1.1.0"}
```

If `extension_version` is missing or below the server version, Chrome is still
running an old service worker: run `python3 scripts/bridge_client.py reload`
(or click ↻ in `chrome://extensions`). A pre-1.1 extension ignores `tab_id`, so
session-scoped verbs would quietly act on the shared managed tab.

- `{"error": "CONNECTION_FAILED"}` → start the relay:
  `python3 scripts/bridge_server.py &` (wait ~2 s, re-check).
- `{"extension_connected": false}` → **stop and ask the user.** The extension
  install is a one-time manual action; don't try to script it:

  > Chrome Bridge extension isn't connected. One-time setup:
  > 1. Open `chrome://extensions`
  > 2. Toggle "Developer mode" (top right)
  > 3. "Load unpacked" → select this repo's `extension/` directory
  > 4. On the card: **Details** → **Site access** → **"On all sites"**.
  >    Chrome 112+ requires this for `get_cookies` and cross-site DOM work
  >    even though the manifest declares `<all_urls>`.
  > 5. Confirm it's enabled, then say "ready".

- `{"error": "UNAUTHORIZED"}` → the client couldn't read the token file. It
  lives at `~/.chrome-bridge-token` (mode 0600) and is created by the server
  on first start; `CHROME_BRIDGE_TOKEN` overrides it.

## Shell one-liners (no .py file needed)

```bash
python3 scripts/bridge_client.py eval 'document.title' --url https://example.com
python3 scripts/bridge_client.py snapshot --session dash    # numbered elements
python3 scripts/bridge_client.py text --url https://x.com/y # innerText, chunked
python3 scripts/bridge_client.py fetch https://internal/api/x --json
python3 scripts/bridge_client.py screenshot --selector "#chart" --out chart.png
python3 scripts/bridge_client.py list-tabs
python3 scripts/bridge_client.py reload                     # after editing extension/
```

`--url` opens a temp tab and closes it; `--session NAME` keeps a named tab
alive across calls; `--tab-id N` targets a tab the user already has open.
Installed as `chrome-bridge` if you `pip install -e .`.

## Python API

### Sessions — the default way to do multi-step work

```python
from bridge_client import BridgePage

page = BridgePage()

with page.tab("https://internal.dashboard/x") as tab:      # closed on exit
    tab.click_element("#filter")
    tab.wait_for_element(".results-row", timeout=20)
    rows = tab.evaluate("document.querySelectorAll('.results-row').length")
```

`tab()` returns a `Tab`, which **is** a `BridgePage` bound to that tab — every
verb below targets it. An anonymous tab is closed when the block exits.

Pass `name="dash"` to reuse the same tab across runs instead of opening a new one
each time. A **named** tab is deliberately left open when the block exits;
`list_sessions()` lists them and `tab.close()` / `browse_close(tab_id)` disposes
of one.

To drive a tab the *user* already has open:

```python
hit = page.list_tabs("github.com")[0]
tab = BridgePage(tab_id=hit["tab_id"])
```

### Agent-native page snapshot

Stop guessing CSS selectors on React apps with hashed class names:

```python
print(tab.snapshot_text())
# [0] button 'Save'
# [1] checkbox 'I agree' unchecked
# [2] combobox 'Region' options=['us-east', 'us-west', 'eu']

snap = tab.snapshot(filter="agree", limit=50)   # also: root="form#main"
tab.act(1, "check")
tab.act(0, "click", snapshot_id=snap["snapshot_id"])
```

`snapshot()` lists visible interactive elements (piercing open shadow roots)
with `{ref, tag, role, name, value, checked, disabled, options, x, y}`.
`act(ref, action)` supports `click / fill / check / select / hover / focus /
scroll_into_view / text`, and raises `StaleRefError` rather than mis-clicking
when the page re-rendered — re-snapshot and retry.

### Call an internal API with the browser's session

```python
data = tab.fetch_json("https://internal/api/tasks?limit=50")
res  = tab.fetch(url, method="POST", body=json.dumps(payload),
                 csrf_from='meta[name="csrf-token"]')   # -> {status, ok, body}
```

The request runs *inside the page's origin*, so cookies and same-origin CSRF
checks just work — the cheap way to reach an API that has no standalone client,
or whose auth only exists in the browser. Bodies over 4 MB are streamed back in
chunks automatically.

### One-shot

```python
title = page.browse_and_eval("https://example.com", "document.title", timeout=30000)
```

### Everything else

| Group | Methods |
|---|---|
| Navigation | `navigate` `wait_for_load` `wait_dom_stable` `get_url` |
| Evaluate | `evaluate` (an *expression*) · `evaluate_function(body, *args)` |
| Query | `has_element` `wait_for_element` `get_element_text` `get_element_attribute` `get_elements_count` `get_html` `read_text` |
| Snapshot | `snapshot` `snapshot_text` `act` |
| Fetch | `fetch` `fetch_json` |
| Interact | `click_element` `input_text` `input_content_editable` `select_option` `hover_element` `select_all_text` `remove_element` |
| Scroll | `scroll_by` `scroll_to` `scroll_to_bottom` `scroll_element_into_view` `scroll_nth_element_into_view` `get_scroll_top` `get_viewport_height` |
| Input | `press_key` `type_text` `mouse_move` `mouse_click` `dispatch_wheel_event` `cdp_mouse` |
| Files | `set_file_input` |
| Capture | `screenshot` `screenshot_element` `get_cookies` |
| Sessions | `tab` `list_tabs` `list_sessions` `activate_tab` `browse_open` `browse_do` `browse_close` `browse_and_eval` |
| Health | `status` `is_server_running` `is_extension_connected` `reload_self` |

`cdp_mouse(action, x, y, x2, y2)` sends **trusted** native input via CDP —
reach for it when synthetic events are ignored (native context menus, HTML5
drag-and-drop, widgets that check `event.isTrusted`).

> **This is the one verb that touches the foreground.** Chrome delivers CDP
> pointer *moves* to a background tab but silently drops press/release, so a
> click on a background tab lands nowhere. The tab is therefore raised for the
> duration and the user's tab is restored afterwards — a sub-second flash.
> `activate=False` opts out, at the cost of the click doing nothing.
> `activate_tab()` exposes the same move on its own, and returns the tab it
> displaced so you can put it back.

### Errors are typed — branch on them, don't grep the message

```python
from bridge_client import ElementNotFoundError, StaleRefError, TabGoneError

try:
    tab.click_element("#submit")
except ElementNotFoundError:
    tab.wait_dom_stable(); ...        # re-render race → retry
except TabGoneError:
    ...                                # tab closed → abort, don't retry
```

`BridgeError` is the base; subclasses are `BridgeConnectionError`,
`BridgeAuthError`, `ExtensionNotConnectedError`, `BridgeTimeoutError`,
`ElementNotFoundError`, `TabGoneError`, `JSEvalError` (`.detail["stack"]`),
`StaleRefError`, `NavigationTimeoutError`. Every one carries `.code`.

## Gotchas

- **`evaluate()` takes an expression, not statements.** It is wrapped as
  `Function("return (<expr>)")`, so bare `var x = ...` / `if (...) {}` raise
  "Unexpected token". Wrap statements in an IIFE — `(function(){ ... return v; })()`
  — or use `evaluate_function("return ...;", arg1, arg2)`.
- **SPAs need explicit waits.** Tab status `complete` only means the initial
  document parsed. Pass `wait_selector=` to `tab()` / `browse_do`, or call
  `wait_for_element` / `wait_dom_stable`. Fixed `time.sleep()` is what makes
  long flows both slow and flaky.
- **Long-running JS exceeds the call deadline.** Drive loops from Python (one
  call per iteration), not `for (...) setTimeout(...)` inside one `evaluate`.
  Deadlines are per-call and derived from the timeout you pass.
- **The managed tab is shared.** Verbs called on `BridgePage` (not on a `Tab`)
  all target one background tab, so two concurrent scripts clobber each other.
  Use `page.tab(...)` — a session per script — and this disappears.
- **`:contains()` is jQuery, not CSS.** Read `read_text()` and filter in Python.
- **JSON from JS must be `json.loads`-ed**, not `eval`-ed (`true`/`null`).
- **Vue/React comboboxes often hide a real `<select>`.** `select_option` on the
  hidden native element beats clicking through the popup's `[role="option"]`.
- **Canvas / closed Shadow DOM** isn't reachable by selectors (Google Maps
  places, for example). `snapshot()` pierces *open* shadow roots only; for
  canvas use the `innerText` pattern in
  [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md).
- **Avoid `\u` escapes in JS expressions** — Python's literal and Chrome's
  serialiser interact badly. Use `String.fromCharCode()` or a raw string.
- **`httpOnly` cookies can't be set via `document.cookie`.** Read what the
  browser already has with `get_cookies(domain)`. See
  [`references/cookie-injection-auth.md`](references/cookie-injection-auth.md).
- **Strict-CSP sites** (GitHub, Reddit, GitLab) work transparently: the CSP
  header is stripped **for bridge-owned tabs only**, and `executeScript`
  falls back to CDP when rejected. The CDP path shows Chrome's "is debugging
  this browser" banner — a security feature, not a bug.
  [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md).
- **DevTools open on a driven tab** blocks the CDP paths (screenshot, file
  upload, `cdp_mouse`) with `DEBUGGER_BUSY`. Close DevTools for that tab.
- **Commands are at-most-once.** If the extension reconnects mid-command the
  call fails with `EXTENSION_NOT_CONNECTED` — but a side-effecting command
  (a click) may already have run. Re-check state before blindly retrying.

### Explore the DOM before writing a Bridge script

`snapshot_text()` is usually enough. When it isn't:

```js
document.querySelectorAll('[role="heading"]').length   // SPAs skip <h1>..<h6>
document.querySelectorAll('select').length             // hidden native selects
```

Common traps: assuming `<h4>` exists (SPAs use `<div role="heading">`);
assuming the checkbox is *inside* its `<label>`; assuming all rows render at
once instead of virtualising.

## Auto-reload while editing the extension

`bridge_server.py` hashes `extension/manifest.json` + `background.js` and polls
every 2 s; on change it sends `reload_self` and the extension calls
`chrome.runtime.reload()`. First time only, reload manually at
`chrome://extensions` so the hook exists.

**Reloading discards every open session** (tab ids survive, but named sessions
and the CSP scope are re-hydrated from `chrome.storage.session`). Run the
server with `--no-watch` for unattended work.

## Security model

The relay is loopback-only, but loopback is **not** a boundary:
`ws://localhost` is a potentially-trustworthy origin, so an ordinary
`https://` page is allowed to open a socket to it. Two gates close that:

1. **Token** — CLI clients must present the secret from
   `~/.chrome-bridge-token` (0600). A web page can't read files.
2. **Origin** — browsers always send `Origin`; the Python client never does.
   Anything with a non-`chrome-extension://` origin is refused before it can
   send a command.

Residual risk, stated plainly:

- Another process running as **the same user** can read the token file.
- Nothing authenticates the *server* to the extension, so a process that binds
  port 9333 before the real relay can pose as it and issue commands — including
  `get_cookies` — without ever seeing the token.

So this defends against web pages, not against local software you already
trust. `--no-auth` drops the token gate; the Origin gate stays on regardless.

`snapshot()` masks `<input type=password>`, hidden fields, and
`autocomplete="current-password|new-password|one-time-code|cc-*"` as `***`, so
an autofilled login form doesn't travel into the model's context. Anything you
read with `evaluate`/`read_text`/`get_cookies` is unfiltered by design.

CSP stripping is scoped to bridge-owned tabs via a *session* declarativeNetRequest
rule, so the user's ordinary browsing keeps its CSP (≤1.0.3 stripped it
profile-wide, permanently, even with the bridge stopped).

## References

- [`references/anti-bot-sites.md`](references/anti-bot-sites.md) — the kinds of
  wall that stop headless/cloud browsers, and which need a real, signed-in Chrome.
- [`references/background-tab-fix.md`](references/background-tab-fix.md) —
  why the bridge never steals focus.
- [`references/cookie-injection-auth.md`](references/cookie-injection-auth.md)
  — live cookie extraction vs. file injection, `httpOnly` limits.
- [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md) — CSP
  strip + CDP fallback design.
- [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md)
  — Canvas + Shadow-DOM extraction.
- [`references/integration-test-pattern.md`](references/integration-test-pattern.md)
  — the `pytest` auto-skip pattern used by `tests/`.
- [`references/multi-step-spa-pattern.md`](references/multi-step-spa-pattern.md)
  — click → wait → extract chains.
- [`references/spa-text-extraction.md`](references/spa-text-extraction.md) —
  what `read_text()` automates, and when to reach past it.
- `scripts/bridge_client.py` — full method signatures and docstrings.
