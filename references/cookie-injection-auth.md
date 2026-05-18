# Cookie Injection (Authenticated Scraping)

## Problem

A new Bridge tab may not have the same cookies as the user's manual
browser session. Typical symptoms:

- **Zhihu**: `z_c0` (the main auth token) is missing or has expired in
  the Bridge tab.
- **Any login-gated site**: a freshly-opened Bridge tab redirects to
  `/signin` or `/login` and the script silently returns empty data.

## Diagnose: is the cookie present?

```python
cookie_check = page.browse_do(tab_id, """
  (function(){
    var cookies = document.cookie.split(';').map(c => c.trim());
    return JSON.stringify(
      cookies.filter(c => c.startsWith('z_c0=') || c.startsWith('d_c0='))
    );
  })()
""")
```

## Fix: inject from a file, then navigate

### 1. Prepare a cookie file

After the user logs in in Chrome, extract the relevant cookies (manually
or via a helper script) into a JSON file:

```json
{
  "z_c0": "2|1:0|10:...",
  "d_c0": "...",
  "_zap": "..."
}
```

Store it somewhere outside version control (e.g.
`./private/zhihu_cookies.json`).

### 2. Inject

```python
import json
from pathlib import Path

COOKIES_PATH = Path("private/zhihu_cookies.json")


def _inject_cookies(page, tab_id):
    """Read cookies from disk and set them on the current tab."""
    if not COOKIES_PATH.exists():
        return "cookies_file_missing"
    with COOKIES_PATH.open() as f:
        cookies = json.load(f)

    for name in ("z_c0", "d_c0", "_zap"):
        value = cookies.get(name)
        if not value:
            continue
        value_escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        js = (
            f"(function(){{"
            f"var v='{value_escaped}';"
            f"document.cookie='{name}='+v+'; domain=.zhihu.com; path=/; secure';"
            f"}})()"
        )
        page.browse_do(tab_id, js)


tab_info = page.browse_open("https://www.zhihu.com", timeout=30000)
_inject_cookies(page, tab_info["tab_id"])
page.browse_do(tab_info["tab_id"], "window.location.href = 'https://www.zhihu.com/follow'")
```

### 3. Detect auth failure (graceful degrade)

After injecting, check the URL:

```python
url_check = str(page.browse_do(tab_id, "window.location.href"))
if "signin" in url_check:
    # Cookie expired — record "login_required" rather than fake data
    result = {"items": [], "source": "login_required_cookies_stale"}
elif "unhuman" in url_check:
    # Anti-bot challenge — user must clear CAPTCHA manually
    result = {"items": [], "source": "captcha_blocked"}
```

## Better: live extraction (preferred over file injection)

**Key insight**: Bridge runs in the user's real Chrome. If the user has
logged in to the target site at any point, the cookie is already in
Chrome's cookie store. `page.get_cookies()` reads it live — no file to
maintain, nothing to refresh.

```python
def _extract_live_cookies(page) -> dict[str, str]:
    """Pull auth cookies straight from the browser."""
    all_cookies = page.get_cookies(".zhihu.com") + page.get_cookies("www.zhihu.com")
    return {
        c["name"]: c.get("value", "")
        for c in all_cookies
        if c.get("name") in ("z_c0", "d_c0", "_zap")
    }
```

### Layered strategy: live → file fallback → degrade

```python
def _try_ensure_login(page, tab_id):
    # 1. Live extraction (best)
    live = _extract_live_cookies(page)
    if live.get("z_c0"):
        _save_cookies(live)  # cache for fallback
        return "live"

    # 2. File-based injection
    cookies = load_from_file()
    if cookies:
        for name, value in cookies.items():
            page.browse_do(tab_id, inject_js(name, value))
        return "injected"

    # 3. Give up; downstream should write a "login_required" marker
    return "login_required"
```

### Health-check cron

A short script that runs every few days, checks for `z_c0`, and pings
the user if it's missing — they log in once in Chrome and the next run
auto-extracts the new cookie via `_extract_live_cookies` and refreshes
the file backup.

## Limits

| Limit | Detail |
|------|------|
| **`httpOnly` cookies can't be set via `document.cookie`** | If the site's auth token is `httpOnly`, file-based injection doesn't work — use live extraction (`page.get_cookies`) instead. |
| **CAPTCHA risk** | Injected cookies can trip anti-bot challenges (e.g. Zhihu's `unhuman` page). |
| **Cookies expire** | `get_cookies()` reads whatever the browser currently has. If the user hasn't visited the site in Chrome for a long time, the cookie is stale. |
| **`BridgePage` has no `set_cookies`** | Today the API only has `get_cookies()`. Injection goes through `browse_do` + `document.cookie`. |
| **Live still hits `signin` if the server revoked the cookie** | The cookie exists locally but is no longer valid. The post-inject URL check is the final say. |
