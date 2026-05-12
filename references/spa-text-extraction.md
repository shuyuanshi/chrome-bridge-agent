# SPA Full-Text Extraction (Chunked innerText)

## Problem

Some agent tools that "snapshot" the DOM truncate JS-rendered single-page apps
(Next.js, React SPA, etc.) at a few hundred elements. Lazy-loaded sections,
virtualised lists, and large category grids get cut off. Re-snapshotting or
scrolling does not break past the cap, and triggering the page's own
client-side search often clears the DOM instead of filtering it.

## Workaround: read `innerText` in chunks via `page.evaluate`

`BridgePage.evaluate` returns whatever the JS expression evaluates to, with
no DOM-element cap. `String.prototype.substring` lets you slice
`document.body.innerText` into manageable pieces.

```python
from bridge_client import BridgePage

page = BridgePage()
tab = page.browse_open("https://example.com/")

chunks: list[str] = []
start = 0
CHUNK = 3000
while True:
    text = page.browse_do(
        tab["tab_id"],
        f"document.body.innerText.substring({start}, {start + CHUNK})",
    )
    if not text:
        break
    chunks.append(text)
    start += CHUNK

page.browse_close(tab["tab_id"])
full_text = "".join(chunks)
```

Parse `full_text` in Python (regex, line splits, etc.). Stop the loop when
the returned chunk is empty or shorter than `CHUNK` — that means you've hit
the end of the document.

## When it works

- JS-rendered sites whose primary content is text (links, titles, prices,
  descriptions).
- Any time another tool's DOM snapshot truncates.

## When it doesn't

- **Canvas-rendered pages** (e.g. Google Maps). Text is drawn into a canvas
  and isn't part of `innerText`. See `google-maps-saved-places.md`.
- **Closed Shadow DOM components.** `innerText` skips closed shadow trees;
  use CDP (`Runtime.evaluate` against the specific frame) instead.

## Tuning

- 3000–4000 chars/chunk is a good default. Larger chunks reduce round trips
  but raise the chance of a 90-second WebSocket timeout on slow tabs.
- Very large pages (> 50k chars) usually mean the site is paginating
  virtually; consider scrolling between extractions or driving the page's
  own pagination via `click_element`.
