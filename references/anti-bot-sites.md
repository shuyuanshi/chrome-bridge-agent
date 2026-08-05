# Sites That Require Chrome Bridge

When a cloud-hosted or headless browser (a host browser tool,
Playwright/Selenium, Browserbase, Puppeteer MCP, …) gets blocked, fall
back to **Chrome Bridge** — it runs inside the user's real Chrome with
real cookies and a real residential fingerprint.

This is organised by **block type** rather than by site, because that is the
part that transfers: the wall you hit tells you which tool can get through it.

## Must use Chrome Bridge

| Block type | Symptom | Why Bridge wins |
|------------|---------|-----------------|
| **Login wall + `httpOnly` session cookie** | Redirect to a sign-in page; the auth cookie can't be set from JS | The user's Chrome is already signed in. Canonical example: Zhihu's `z_c0` — see [`cookie-injection-auth.md`](cookie-injection-auth.md) |
| **Login wall + client-rendered SPA** | Remote headless renders a blank page even with cookies (school/parent portals, LMS and admin dashboards) | Real Chrome hydrates the app; `wait_for_element` then sees real nodes |
| **Cloudflare interactive challenge** ("Press & Hold", Turnstile) | Browserbase/headless is stopped at the challenge | A daily-driver profile is usually pre-cleared, and the fingerprint is genuine |
| **Vercel / platform security checkpoint** | `browser_navigate` returns "Verification Failed"; `curl` returns the checkpoint HTML | Only a real browser session gets past the interstitial |
| **Shopify storefronts behind Cloudflare** | Headless blocked at the edge | Needs real cookies + real TLS fingerprint |
| **Virtualised list + no API** | Content exists only after JS runs; scrolling loads more | Interaction (click, filter, scroll) must actually execute — rendering isn't enough |

## Recommended — Chrome Bridge preferred

| Situation | Reason |
|-----------|--------|
| **RSS/JSON endpoints return 404, 403 or empty** | The feed is a lie; content is JS-rendered (common on media and aggregator sites, e.g. Bilibili's hot list) |
| **OAuth-only web apps** | No API key path; the session lives in the browser |
| **Canvas or closed Shadow DOM** | Not reachable by selectors — e.g. Google Maps saved places. See [`google-maps-saved-places.md`](google-maps-saved-places.md) |
| **Geo- or IP-restricted sites** | Runs on the user's machine, from their own IP |

## Don't need Chrome Bridge

| Situation | Reason |
|-----------|--------|
| **The service has an official API** | Faster, stabler, and it won't mark things read or trip anti-bot heuristics |
| **Gmail** | Gmail API (REST) — never touch the browser for this |
| **GitHub** | API + RSS cover almost everything |
| **RSS feeds** (TechCrunch, VentureBeat, CNBC, …) | `curl` + XML parse |
| **Static HTML** | `curl` + a parser; a browser is pure overhead |

## Adding a new wall

When you hit a new anti-bot wall:

1. Try `browser_navigate` (or your default scraper) first → expect
   Cloudflare / CAPTCHA / login wall.
2. Switch to Chrome Bridge: `browse_and_eval` for one-shot, or
   `page.tab(url)` for multi-step.
3. **Add the *block type* to this file** — not the account you happened to be
   logged into. Future runs need to recognise the wall, not your bookmarks.
