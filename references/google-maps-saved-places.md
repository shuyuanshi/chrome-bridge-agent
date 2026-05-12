# Pattern: Extracting Data from Canvas + Shadow-DOM Pages (Google Maps)

## Problem

Google Maps renders the place list with a mix of Canvas and Shadow DOM, so
the obvious approach — `document.querySelectorAll('a[href*="/place/"]')` —
returns nothing. And `browse_and_eval` opens and closes the tab in one shot,
which doesn't fit a click → wait → extract flow.

## Solution: keep-alive `browse_open` / `browse_do`

```python
from bridge_client import BridgePage

page = BridgePage()

# Step 1: open Google Maps in a persistent tab
tab = page.browse_open("https://www.google.com/maps")
tab_id = tab["tab_id"]

# Step 2: click the "Saved" button.
# Its textContent starts with a Material-Symbols icon glyph (U+E867),
# so match by `includes`, not strict equality.
page.browse_do(tab_id, """
  Array.from(document.querySelectorAll('button'))
    .find(b => b.textContent.includes('Saved'))?.click()
""")

# Step 3: pull the list text — DOM selectors aren't reliable here, so read
# innerText and parse on the Python side.
text = page.browse_do(tab_id, "document.body.innerText")

page.browse_close(tab_id)
```

## Why this works

- The "Saved" button's `textContent` is prefixed with a Material-Symbols icon
  character (`U+E867`); `=== 'Saved'` will never match.
- `innerText` returns the structured list (list name + place count) even when
  the underlying cards live inside Shadow DOM or canvas overlays.
- Once a saved list is expanded, place names, ratings, and price tiers all
  show up in `innerText` as plain text.
- The Canvas tile layer (the actual map) is never reachable through DOM
  selectors. Don't try.

## Extracted shape

A typical block in `text` (after splitting by newline) looks like:

```
<List Name>
<Owner> · Shared · 42 places

Some Restaurant
Italian
Note

Other Place
4.4(83)
$$
· Cafe
Note
```

Parse line-by-line: `place name → cuisine/type → rating → price → optional note`.
