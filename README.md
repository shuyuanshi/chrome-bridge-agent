# Chrome Bridge

> Drive your **real** Chrome from Python or any agent — with your logins,
> cookies, SPA state, and extensions intact.

Chrome Bridge is a tiny WebSocket relay between a Chrome extension and a
Python client. Instead of spinning up a headless browser that immediately
gets blocked or needs to re-log in everywhere, the agent reuses the
browser you're already signed into.

| Use case | Why this instead of Playwright/Puppeteer |
|---|---|
| Scrape a page that requires login | Your Chrome already has the session |
| Click through a SPA (Google Maps, Gmail, internal dashboards) | No headless fingerprint, no CAPTCHA loop |
| Pull cookies for an API client | One call — no separate login script |
| Take element screenshots | `page.screenshot_element(selector)` |
| Drive a strict-CSP site (GitHub, Reddit, …) | Built-in CSP-header strip + CDP fallback |

## How it works

```
Python script  ─►  bridge_client.BridgePage
                       ↕  ws://localhost:9333
                   bridge_server.py
                       ↕  WebSocket
                   Chrome Extension (background.js)
                       ↕  chrome.scripting.executeScript / CDP
                   Browser tab (MAIN world)
```

The server is one Python process. The Chrome side is one unpacked
extension. The client is one Python class.

## Install

```bash
git clone https://github.com/your-org/chrome-bridge
cd chrome-bridge

# Python dependency
pip install websockets        # or: uv pip install websockets

# Load the extension once
#   1. open chrome://extensions
#   2. enable "Developer mode" (top right)
#   3. "Load unpacked" → select the `extension/` directory
#   4. on the extension card, click "Details" → set
#      "Site access" to "On all sites"
#      (Chrome 112+ requires this; without it `cookies.getAll`
#      and cross-site scripting silently return empty/partial results)
```

Requirements:

- Python 3.10+
- Google Chrome (or any Chromium with extension support)

## Run

Start the relay (long-running):

```bash
python scripts/bridge_server.py
# logs: Chrome Bridge server listening on ws://localhost:9333
# logs: waiting for the Chrome extension to connect...
# logs: extension connected
```

Then, from Python:

```python
from bridge_client import BridgePage

page = BridgePage()

# One-shot: open a URL, evaluate JS, close the tab.
title = page.browse_and_eval(
    url="https://example.com",
    expression="document.title",
)
print(title)

# Multi-step: keep the tab open for click → wait → extract flows.
tab = page.browse_open("https://example.com")
page.browse_do(tab["tab_id"], "document.querySelector('button.next').click()")
text = page.browse_do(tab["tab_id"], "document.body.innerText")
page.browse_close(tab["tab_id"])
```

The active managed tab can also be driven directly:

```python
page.navigate("https://example.com")
page.click_element("button.submit")
page.has_element("#error-banner")          # True / False
cookies = page.get_cookies(domain="example.com")
png_bytes = page.screenshot_element("img.logo")
```

See `bridge_client.py` for the full surface — keyboard events, file
uploads, scrolling, content-editable input, mouse events, etc.

## Use with AI agents

This repo is also a valid [Agent Skills](https://agentskills.io) package —
`SKILL.md` follows the open Agent Skills spec, so any compatible agent
runtime (Claude Code, OpenAI Codex, opencode, OpenHands, Cursor, Gemini
CLI, Goose, Hermes, …) can load it directly.

To install as a skill, drop the entire `chrome-bridge/` directory into your
agent's skills directory (or symlink it). The agent will pick up the
`SKILL.md` automatically and use it when the user asks for browser
automation.

## Architecture at a glance

- **`extension/manifest.json` + `background.js` + `content.js`** — MV3
  service worker that connects to the relay, executes commands in the
  active tab, and auto-reloads itself when its own files change.
- **`scripts/bridge_server.py`** — async WebSocket relay; extension keeps a
  long-lived connection, CLI clients open short-lived ones per command.
- **`scripts/bridge_client.py`** — `BridgePage` class; one method per
  command.
- **`scripts/watch_reload.py`** — standalone reloader (the server has the
  same logic built in; this is for offline use).

Deeper notes:

- [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md) — how
  GitHub / Reddit work despite CSP.
- [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md)
  — extracting data when the page is Canvas + Shadow DOM.
- [`references/spa-text-extraction.md`](references/spa-text-extraction.md) —
  chunked `innerText` recovery when DOM snapshots truncate.

## Security model

Chrome Bridge runs the agent's commands in your real browser profile. That
means:

- An agent that can talk to `ws://localhost:9333` can do anything you can
  do while logged in. Treat the relay like an open shell on your browser.
- The relay binds to `localhost` only.
- There is no auth between the client and the relay. If you don't want
  that, change `BRIDGE_URL` in both `bridge_server.py` and `bridge_client.py`
  to a UNIX domain socket, or run the agent in a sandbox.
- The CDP fallback will pop a "[Chrome Bridge] is debugging this browser"
  banner. That's Chrome telling you something has full debugger access —
  not a bug, it can't be hidden.

## License

MIT — see [`LICENSE`](LICENSE).
