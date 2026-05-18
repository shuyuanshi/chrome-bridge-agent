# Multi-Step SPA Interaction Pattern

For SPA pages that need interaction (toggle a filter, change the sort,
walk through a wizard), `browse_and_eval()` is not enough — it's a
one-shot open → eval → close. Use `navigate()` + `evaluate()` +
`wait_*()` as a chain instead.

## Pattern

```python
page = BridgePage()

# Step 1: navigate + wait for the initial render
page.navigate(url)
page.wait_for_load(timeout=30)

# Step 2: first interaction (e.g. tick filter checkboxes)
page.evaluate(JS_CLICK_FILTERS)
page.wait_dom_stable(timeout=5)

# Step 3: second interaction (e.g. change sort order)
page.evaluate(JS_SET_SORT)
page.wait_dom_stable(timeout=3)

# Step 4: extract
items = page.evaluate(EXTRACT_JS)
```

## Key points

- **`wait_dom_stable` after every `evaluate`** — Vue/React state updates
  asynchronously, the DOM needs time to settle.
- **Await long interactions inside the JS expression.** Clicking "Apply"
  may trigger a fetch + re-render that takes 1-2 seconds:

  ```js
  return new Promise(r => setTimeout(() => r({done: true}), 2000));
  ```

- **`navigate()` + `evaluate()` is fine on SPAs** *if* you call
  `wait_for_load()` between them. The unsafe pattern called out in
  `SKILL.md` is `navigate()` immediately followed by `evaluate()` with no
  wait.
- **Don't use `browse_and_eval()` for multi-step flows** — it closes the
  tab after the first eval, killing all subsequent state.

## Why not `browse_open` / `browse_do` / `browse_close`?

That trio also supports multi-step, but:

- You have to track the `tab_id` yourself.
- You need a `try / finally` block to guarantee `browse_close()`.
- `navigate()` + `evaluate()` uses the managed tab (auto-lifecycle), so
  the code is shorter.

The two styles are functionally equivalent — pick whichever reads better
for the task.
