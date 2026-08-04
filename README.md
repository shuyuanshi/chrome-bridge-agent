# Chrome Bridge Agent

[![CI](https://github.com/shuyuanshi/chrome-bridge-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/shuyuanshi/chrome-bridge-agent/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

> Drive **your real Chrome** — with all your logins, cookies, fingerprints,
> and extensions — from Python or any AI agent.

```
your script  ──►  BridgePage  ──ws──►  bridge_server.py  ──ws──►  Chrome extension
                                                                         │
                                                            your everyday browser
```

No headless browser. No re-login. No CAPTCHA loops. No fresh fingerprint
flagged by Cloudflare. The agent works inside the Chrome you're already
signed into.

---

## Why this exists

When you ask an agent to "log in to my Notion, find page X, screenshot it"
or "pull my last 10 xiaohongshu posts", the agent has three off-the-shelf
options. **All three are bad for anything behind a login.**

### Option 1 — Playwright / Puppeteer / Selenium (headless or headed)

The agent launches a *fresh* Chromium process with an empty profile. That means:

- **No cookies.** You have to script the login. Username/password in plain
  text, 2FA flows that don't work, "device not recognised" emails every run.
- **Bot fingerprint.** A headless Chromium has a fingerprint that
  Cloudflare, hCaptcha, Akamai, and every major social/commerce site
  actively block. You spend the next two days bolting on `playwright-stealth`,
  fingerprint spoofers, residential proxies — and it still breaks next week
  when they update their detection.
- **No extensions.** No uBlock, no 1Password autofill, no Bitwarden, no your
  enterprise SSO extension. The browser the agent uses bears no resemblance
  to the browser you use.
- **Per-project setup.** Every agent project needs its own browser binary
  install, its own driver, its own login flow.

### Option 2 — Agent-built-in `browse_*` tools (Claude, Codex, Goose, …)

Modern coding agents ship a built-in browser tool that runs in the cloud
(Claude Code's `browser_navigate` / `browser_console` / `browser_snapshot`,
Codex's web tool, etc.). They're great for *public* pages, but:

- **Runs on a remote, ephemeral Chromium**, not on your machine. There is
  no way to give it your session — your cookies live in your local browser.
- **DOM snapshots truncate** at a few hundred elements on large SPAs
  (Next.js, virtualised lists). Re-snapshotting doesn't help.
- **State doesn't persist** between calls reliably. Multi-step
  click → wait → extract flows on Google Maps, Gmail, Notion fall over.
- **Some sites refuse the tool's User-Agent / IP range** outright, especially
  Chinese sites (xiaohongshu, bilibili, zhihu) and anything behind
  Cloudflare's strict mode.

### Option 3 — Browser MCP servers (puppeteer-mcp, browserbase, etc.)

Same problem as option 1, dressed up as MCP. Headless + clean profile +
ephemeral.

### What Chrome Bridge Agent does instead

It **does not launch a browser**. It connects to the Chrome you already
have open — same window, same tab system, same logged-in sessions, same
fingerprint your daily browsing has built up over years.

| Problem | This solves it because |
|---|---|
| Logging in to scrape | Your Chrome is already logged in |
| Bot detection | Your Chrome's fingerprint is *your fingerprint* |
| CAPTCHA loops | You almost never see them on sites you visit daily |
| Strict CSP (GitHub, Reddit) | Built-in CSP-header strip + CDP fallback |
| Multi-step SPA flows | `browse_open` + `browse_do` keeps tabs alive |
| Cookie export for API clients | One call: `page.get_cookies(domain="x.com")` |
| Extension setup | Install once, every project shares it |
| Chinese sites blocking foreign IPs | Runs on your machine, your IP |

**Tradeoff:** the bridge can only do what your Chrome can do at this very
moment. It can't run on a server with no display. It can't run unattended
on a schedule (your laptop has to be on, Chrome has to be running). For
CI / cron jobs that don't need login state, use Playwright. For
anything else, use this.

---

## Architecture

```
Python script  ─►  bridge_client.BridgePage / Tab
                       ↕  ws://localhost:9333   (token-authenticated)
                   bridge_server.py  (async WebSocket relay)
                       ↕  WebSocket
                   Chrome Extension  (MV3 background.js)
                       ↕  chrome.scripting.executeScript / CDP
                   Browser tab (MAIN world)
```

The server is one Python process. The Chrome side is one unpacked
extension (no content scripts — nothing is injected into pages you browse
normally). The client is one Python class.

---

## End-to-end setup

### Step 1 — Clone and install

```bash
git clone https://github.com/shuyuanshi/chrome-bridge-agent
cd chrome-bridge-agent

# pick one:
uv sync                       # recommended — uses the committed uv.lock
pip install -e .              # standard pip, picks up pyproject.toml
```

**Requirements:** Python 3.10+ and Google Chrome (or any Chromium with
extension support) on the same machine. The only runtime dependency is
`websockets>=12.0`.

### Step 2 — Load the Chrome extension (one-time)

1. Open `chrome://extensions` in the same Chrome you use daily.
2. Toggle **Developer mode** (top right).
3. Click **Load unpacked**, select the `extension/` directory of this
   repo.
4. **Critical:** on the new extension card, click **Details** →
   **Site access** → switch to **"On all sites"**.

   <details>
   <summary>Why this matters</summary>

   Since Chrome 112, <code>chrome.cookies.getAll</code> and cross-site
   <code>executeScript</code> calls are silently gated on
   <em>runtime-granted</em> host permissions, <strong>not</strong> what
   <code>manifest.json</code> declares. Without flipping this to "On all
   sites":

   - `page.get_cookies()` will return only cookies for domains the user
     has individually granted (often empty).
   - `page.navigate` / `page.evaluate` will still work on the *active*
     tab via `activeTab`, but fail mysteriously on background tabs.

   This is the #1 source of "it worked in the example but not for me"
   reports. Flip the switch.
   </details>

### Step 3 — Start the relay

```bash
python scripts/bridge_server.py
# or, if you installed with uv:
uv run python scripts/bridge_server.py
```

Output will look roughly like:

```
INFO:chrome-bridge:Chrome Bridge server 1.1.0 listening on ws://localhost:9333
INFO:chrome-bridge:auth enabled; token at /Users/you/.chrome-bridge-token
INFO:chrome-bridge:waiting for the Chrome extension to connect...
INFO:chrome-bridge:extension connected
```

(Exact prefixes depend on your logging config — what matters is the
"extension connected" line.) Leave this running. The extension
auto-reconnects whenever Chrome restarts.

The server writes a shared secret to `~/.chrome-bridge-token` (mode 0600) on
first start; the Python client picks it up automatically. See
[Security model](#security-model) for why that matters.

### Step 4 — Smoke-test

```bash
python3 scripts/bridge_client.py status
# {"extension_connected": true, "pending": 0, "server_version": "1.1.0"}

python3 scripts/bridge_client.py eval 'document.title' --url https://example.com
# "Example Domain"
```

If `extension_connected` is `false`, see Step 2 (the extension is installed
but Chrome restarted and hasn't reconnected yet — open `chrome://extensions`
and click the ↻ on the extension card).

### Common pitfalls

- **Two `Chrome Bridge` extensions installed.** If you previously had this
  as a private skill in another directory, *both* extensions will connect
  to port 9333 and the relay routes commands to whichever connected last.
  Behaviour becomes non-deterministic. The server now logs a loud warning
  when it sees a second extension — uninstall duplicates in
  `chrome://extensions`.
- **Chrome profile mismatch.** If you have multiple Chrome profiles
  (`Person 1`, `Person 2`, `Work`), the extension is installed *per
  profile*. Make sure the active Chrome window when you start the bridge
  is the profile you want to drive.
- **CDP debugger banner.** Some commands (on strict-CSP sites) trigger
  Chrome's "is debugging this browser" banner. That's a Chrome security
  feature, not a bug — it can't be hidden.
- **Server port already in use.** Default is 9333. Pass `--port 9444` (or
  any free port) and update `BRIDGE_URL` in `bridge_client.py`
  accordingly.
- **Parallel scripts race on the shared managed tab.** Verbs called straight
  on `BridgePage` all target one persistent background tab, so two scripts
  that both call `navigate()` clobber each other. Use a session instead —
  `with page.tab(url) as tab:` gives each script its own tab, and every verb
  on that `Tab` targets it.
- **Bridge doesn't steal focus** (since 2026-05-16). Tabs are opened with
  `active: false` and reused across calls, so the user's visible window is
  not hijacked. Details:
  [`references/background-tab-fix.md`](references/background-tab-fix.md).
  **The single exception is `cdp_mouse`**: Chrome does not reliably deliver
  CDP press/release to a tab that isn't visible, so that call raises the tab
  for a sub-second and then restores whatever the user was on. Pass
  `activate=False` to forbid it, at the risk of the click going nowhere.

---

## Worked example — read your own xiaohongshu state

This is the kind of thing every other tool fails at:

- Playwright: gets blocked by xiaohongshu's bot detection within seconds.
- Headless Chrome: same.
- Claude's built-in `browser_*`: xiaohongshu's edge blocks it
  geographically and by IP range.
- Chrome Bridge Agent: works trivially, because **you're already logged
  in to xiaohongshu in your real Chrome**.

```python
# example_xiaohongshu.py
import re
from bridge_client import BridgePage

page = BridgePage()

# The session closes itself, even if something below raises.
with page.tab("https://www.xiaohongshu.com/explore", timeout=45) as tab:

    # 1. Pull all session cookies for xiaohongshu. These are the same cookies
    #    your real browser uses — feed them straight into requests/httpx if
    #    you want to talk to xiaohongshu's API without using the bridge for
    #    every call.
    cookies = page.get_cookies(domain="xiaohongshu.com")
    session_names = [c["name"] for c in cookies if c["name"] in ("web_session", "a1", "webId")]
    print(f"cookies for xiaohongshu.com: {len(cookies)}")
    print(f"  session-relevant: {session_names}")

    # 2. Confirm we're logged in by reading something only logged-in users see.
    #    xiaohongshu rewrites its DOM constantly — using innerText is more robust
    #    than CSS selectors.
    text = tab.read_text()[:4000]
    logged_in = "登录" not in text[:500]  # the login banner is in the first chunk
    print(f"  logged in: {logged_in}")

    # 3. Pull the first few rows from the explore feed. Each card in the
    #    rendered text looks like:
    #        <note title>
    #        <author>
    #        <like count, e.g. "1.2万">
    #    So a quick regex on lines whose next line starts with a number
    #    captures the creator names.
    authors = [m.group(1).strip()
               for m in re.finditer(r"\n([^\n]{2,40})\n[\d.]+\s*(?:万|千)?\s*\n", text)][:5]
    print(f"first feed creators ({len(authors)}):")
    for a in authors:
        print(f"  - {a}")
```

Output shape (handles replaced with placeholders — a real run prints whoever
is in *your* feed):

```bash
$ python example_xiaohongshu.py
cookies for xiaohongshu.com: 15
  session-relevant: ['a1', 'webId', 'web_session']
  logged in: True
first feed creators (5):
  - 创作者A
  - 创作者B
  - 创作者C
  - 创作者D
  - 创作者E
```

**Try the same thing with Playwright headless** — you'll spend hours on
the login flow alone, and xiaohongshu will block your IP within ten
requests.

---

## Python API surface

Full list in `scripts/bridge_client.py`; the agent-facing contract is
[`SKILL.md`](SKILL.md).

### Sessions — the default for multi-step work

```python
from bridge_client import BridgePage

page = BridgePage()

with page.tab("https://app.example/dashboard") as tab:   # closed on exit
    tab.click_element("#filter")
    tab.wait_for_element(".row", timeout=20)
    rows = tab.evaluate("document.querySelectorAll('.row').length")
```

A `Tab` *is* a `BridgePage` bound to one tab, so every verb targets it —
no more "did that click land on the shared tab?". `name=` reuses the same tab
across runs (and, being named, survives the `with` block — close it yourself);
`page.list_tabs("host.com")` attaches to a tab **you** already have open.

### Agent-native snapshot — stop guessing selectors

```python
print(tab.snapshot_text())
# [0] button 'Save'
# [1] checkbox 'I agree' unchecked
tab.act(1, "check")
tab.act(0, "click")
```

Visible interactive elements only, open shadow roots pierced, stale refs
raise `StaleRefError` instead of mis-clicking.

### Call an internal API with the browser's session

```python
data = tab.fetch_json("https://internal.example/api/items?limit=50")
tab.fetch(url, method="POST", body=payload, csrf_from='meta[name="csrf-token"]')
```

Runs inside the page's origin, so cookies and same-origin CSRF pass.

### One-shot / legacy

```python
page.browse_and_eval(url, expression, timeout=30000)
page.browse_open(url) / page.browse_do(tab_id, js) / page.browse_close(tab_id)
page.navigate(url); page.evaluate(js)          # shared managed tab
```

### Everything else

`has_element` `wait_for_element` `get_element_text` `get_element_attribute`
`get_elements_count` `get_html` `read_text` · `input_text`
`input_content_editable` `select_option` `hover_element` `remove_element` ·
`scroll_*` `press_key` `type_text` `cdp_mouse` (trusted native input) ·
`set_file_input` · `screenshot` `screenshot_element` `get_cookies` ·
`status` `is_server_running` `is_extension_connected`.

Errors are typed (`ElementNotFoundError`, `TabGoneError`, `JSEvalError`,
`StaleRefError`, `BridgeTimeoutError`, …), all subclassing `BridgeError`
with a machine-readable `.code`, so retry logic doesn't have to grep
message strings.

### CLI

```bash
chrome-bridge eval 'document.title' --url https://example.com
chrome-bridge snapshot --session dash
chrome-bridge fetch https://internal/api/x --json
chrome-bridge screenshot --selector "#chart" --out chart.png
chrome-bridge status | list-tabs | list-sessions | reload
```

(Without installing: `python3 scripts/bridge_client.py ...`.)

---

## Use with AI agents

This repo is also a valid [Agent Skills](https://agentskills.io) package
(`SKILL.md` follows the open spec). Drop the entire
`chrome-bridge-agent/` directory into your agent's skills directory, or
symlink it:

- **Claude Code**: `ln -s $(pwd)/chrome-bridge-agent ~/.claude/skills/`
- **OpenAI Codex**: see [Codex skills docs](https://developers.openai.com/codex/skills/)
- **opencode**: drop into `.opencode/skills/`
- **Goose**: see [Goose skills docs](https://block.github.io/goose/docs/guides/context-engineering/using-skills/)
- **Hermes / other agentskills.io-compatible runtimes**: same — place under their skills root.

The agent will pick up `SKILL.md` automatically and call into the bridge
when the user asks for browser automation.

---

## Deep-dive references

- [`references/anti-bot-sites.md`](references/anti-bot-sites.md) —
  the kinds of wall that stop headless/cloud browsers, and which ones a
  real, signed-in Chrome gets through.
- [`references/background-tab-fix.md`](references/background-tab-fix.md)
  — design notes for the 2026-05-16 "don't steal focus" change and the
  resulting shared-tab race condition.
- [`references/cookie-injection-auth.md`](references/cookie-injection-auth.md)
  — live cookie extraction vs. file injection, `httpOnly` limits, and
  graceful degradation when auth has expired.
- [`references/csp-cdp-fallback.md`](references/csp-cdp-fallback.md) — how
  GitHub / Reddit / GitLab work despite their strict CSPs (header
  stripping + CDP fallback design notes).
- [`references/google-maps-saved-places.md`](references/google-maps-saved-places.md)
  — extracting data when the page mixes Canvas and Shadow DOM.
- [`references/integration-test-pattern.md`](references/integration-test-pattern.md)
  — `pytest` auto-skip pattern for projects that depend on a live
  Bridge.
- [`references/multi-step-spa-pattern.md`](references/multi-step-spa-pattern.md)
  — `navigate` + `evaluate` + `wait_*` chains for SPAs that need clicks
  between extractions.
- [`references/spa-text-extraction.md`](references/spa-text-extraction.md) —
  chunked `innerText` recovery when DOM snapshots truncate.

---

## Development

Install dev tooling (ruff, pytest, basedpyright) and run the same checks
CI runs:

```bash
uv sync --group dev

uv run ruff check .              # lint
uv run ruff format --check .     # formatting
uv run basedpyright              # types
uv run pytest -m "not integration"   # unit + smoke tests (no browser needed)
```

The browser-free suite covers more than imports:

| File | What it pins |
|---|---|
| `tests/test_server.py` | relay routing, auth, deadlines, and both bridge-killing regressions — driven by a fake extension, no Chrome |
| `tests/test_client_wire.py` | the exact frames `BridgePage` emits, against a fake relay |
| `tests/test_extension_js.py` | `node --check` plus static invariants (no content script, CSP scoped to tabs, no `captureVisibleTab`) |
| `tests/test_smoke.py` | exact public API surface, and that every method is documented in `SKILL.md` |

To run the **integration tests** (requires the bridge server + extension
+ a live Chrome), drop the marker filter:

```bash
uv run pytest
```

Integration tests auto-skip when the bridge isn't reachable — see
[`references/integration-test-pattern.md`](references/integration-test-pattern.md).

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) runs lint +
unit tests on Python 3.10, 3.11, 3.12, and 3.13. It does **not** run the
integration tests (no headed Chrome in GitHub runners).

---

## Security model

Chrome Bridge Agent runs the agent's commands in your real browser profile.
Anything that can talk to the relay can do anything you can do while logged
in — including `get_cookies()`, which returns session cookies for every
domain. **Treat the relay like an open shell on your browser.**

Loopback is *not* a boundary on its own: `ws://localhost` counts as a
potentially-trustworthy origin, so an ordinary `https://` page is allowed to
open a WebSocket to it. Two gates close that:

1. **Token.** The server generates a secret at `~/.chrome-bridge-token`
   (mode 0600) and refuses CLI connections without it. A web page can't read
   files. Override the location with `CHROME_BRIDGE_TOKEN_FILE`, or the value
   with `CHROME_BRIDGE_TOKEN`.
2. **Origin.** Browsers always send an `Origin` header; the Python client
   never does. Any connection presenting a non-`chrome-extension://` origin
   is refused before it can send a command.

Residual risk, stated plainly:

- Another process running as **the same user** can read the token file. (The
  server refuses to write through a symlink and re-applies `0600` on every
  start; if the file is already group/world-readable it warns loudly.)
- Nothing authenticates the **server** to the extension. A process that binds
  port 9333 before the real relay can pose as it and issue commands —
  including `get_cookies` — without ever seeing the token.

So: this defends against web pages, not against local software you already
trust. `--no-auth` drops the token gate; the Origin gate stays on regardless.

Other properties worth knowing:

- The relay binds to `localhost` only — not exposed to the network.
- **CSP stripping is scoped to bridge-owned tabs** via a session
  declarativeNetRequest rule. Up to 1.0.3 the extension stripped CSP from
  every site in the profile, permanently, even when the bridge wasn't
  running; upgrading purges that old rule automatically.
- **No content scripts.** Nothing is injected into pages you browse
  normally; page-side code only runs in tabs a command targets.
- The CDP fallback shows a "is debugging this browser" banner — Chrome
  security feature, can't be hidden, doesn't indicate a problem.

---

## License

MIT — see [`LICENSE`](LICENSE).
