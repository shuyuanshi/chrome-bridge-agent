# Sites That Require Chrome Bridge

When a cloud-hosted or headless browser (Claude Code's `browser_navigate`,
Playwright/Selenium, Browserbase, Puppeteer MCP, …) gets blocked, fall
back to **Chrome Bridge** — it runs inside the user's real Chrome with
real cookies and a real residential fingerprint.

## Must use Chrome Bridge

| Site | Block type | Why Bridge wins |
|------|-----------|-----------------|
| **Zhihu** (zhihu.com) | Login wall + CAPTCHA | `z_c0` cookie required for the following feed and hot list; cloud browsers have no session |
| **A classroom / parent-portal SPA** | Login wall + React SPA | Login required to see posts; remote headless browsers render the SPA as a blank page |
| **A state DOT traffic portal** | Vue SPA + virtualised lists | No REST API or RSS; interaction (click route picker, filter) must run JS; `browser_navigate` can render but not interact |
| **A Cloudflare-protected retailer** | Cloudflare "Press & Hold" | Browserbase headless gets stopped; the user's real Chrome is already pre-cleared |
| **A Vercel-gated portal** | Cloudflare + Vercel Security Checkpoint | `browser_navigate` returns "Verification Failed"; `curl` returns the Vercel checkpoint HTML; only Bridge works |
| **Shopify storefronts** | Shopify + Cloudflare | Browserbase blocked; needs real Chrome cookies |

## Recommended — Chrome Bridge preferred

| Site | Reason |
|------|--------|
| **A gaming-news site** | RSS endpoints 404/403, content is JS-rendered |
| **A hobby-news site** | RSS returns empty (JS render); needs a real browser |
| **Bilibili** (bilibili.com) | Hot list is JS-rendered; Bridge is stable |
| **Google Maps Saved Places** | Canvas + Shadow DOM; needs takeout dump or Bridge extraction |
| **Price / restock trackers** | Cloudflare-prone |
| **An OAuth-only inbox tool** | Google OAuth login required |

## Don't need Chrome Bridge

| Site | Reason |
|------|--------|
| **Gmail** | Use the Gmail API (REST, doesn't mark read); never touch the browser |
| **Google News** | RSS + `browser_navigate` both work; Bridge is overkill |
| **GitHub** | API + RSS cover everything |
| **RSS feeds** (TechCrunch, VentureBeat, CNBC, …) | `curl` + XML parse |
| **A personal-finance aggregator** | Official API |
| **A loyalty-points aggregator** | Export / API |

## Adding a new site

When you hit a new anti-bot wall:

1. Try `browser_navigate` (or your default scraper) first → expect
   Cloudflare / CAPTCHA / login wall.
2. Switch to Chrome Bridge: `browse_and_eval` for one-shot, or
   `browse_open` / `browse_do` / `browse_close` for multi-step.
3. **Add the site to this file** so future runs (and future agents) don't
   re-discover the same wall.
