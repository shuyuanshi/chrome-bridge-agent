# Chrome Script Injection Blocked on External Tabs

> **Date**: 2026-06-07
> **Symptom**: `chrome.scripting.executeScript` throws "Cannot access contents of url..." or CDP fallback returns "Frame with ID 0 is showing error page" on external URLs, but works on `about:blank`.
> **Root cause**: Chrome security policy blocks extension script injection into certain tabs. Common triggers:
> - Chrome version update that tightens extension permissions
> - Extension's `host_permissions` not matching the target URL
> - Chrome's `ExtensionSettings` policy restricting content injection
> - Tab created via `chrome.tabs.create()` but extension lacks `activeTab` permission for that origin

## Symptoms

1. `browse_open()` succeeds — tab created, URL set, status `ready`
2. `browse_do(tab_id, 'document.title')` or any `evaluate()` call fails with:
   - `Bridge 错误: Frame with ID 0 is showing error page` (CDP fallback path)
   - Or `Cannot access contents of url "https://..."` (direct path)
3. `about:blank` works fine — confirms the issue is URL-origin specific, not a general extension failure

## Diagnosis

```bash
# Step 1: Confirm extension is connected
uv run python -c "from bridge_client import BridgePage; print(BridgePage().is_extension_connected())"
# → True means server+extension comms OK

# Step 2: Test with about:blank (should work)
uv run python -c "
from bridge_client import BridgePage
page = BridgePage()
tab = page.browse_open('about:blank')
title = page.browse_do(tab['tab_id'], 'document.title')
print('Title:', repr(title))
page.browse_close(tab['tab_id'])
"
# → Returns '' (empty) = OK

# Step 3: Test with external URL (may fail)
uv run python -c "
from bridge_client import BridgePage
page = BridgePage()
tab = page.browse_open('https://example.com')
title = page.browse_do(tab['tab_id'], 'document.title')
print('Title:', title)
page.browse_close(tab['tab_id'])
"
# → Error = script injection blocked
```

## Fixes

### 1. Reload the extension
Open `chrome://extensions`, find **Chrome Bridge**, click **Reload** (↻). This re-evaluates host permissions.

### 2. Verify host permissions
In `chrome://extensions` → Chrome Bridge → **Details** → confirm all required permissions are granted. The manifest declares `<all_urls>` — if Chrome shows a warning, the extension needs to be reloaded once more.

### 3. Check Chrome policies
Run in terminal:
```bash
defaults read /Applications/Google\ Chrome.app/Contents/Resources/com.google.Chrome.plist 2>/dev/null
# Or check enterprise policies:
sudo defaults read /Library/Google/GoogleSoftwareUpdate/GoogleSoftwareUpdate.plist 2>/dev/null
```
Look for `ExtensionSettings` or `ExtensionInstallBlocklist` entries that might restrict the bridge extension.

### 4. Reinstall the extension
If reload doesn't help:
1. Remove the extension from `chrome://extensions`
2. Re-add via **Developer mode** → **Load unpacked** → point to `skills/chrome-bridge/extension/`
3. Grant all permissions when prompted

### 5. Chrome version compatibility
Some Chrome updates (especially major version bumps) change how `chrome.scripting` works. If the extension was working before and suddenly breaks:
- Check Chrome version: `defaults read /Applications/Google\ Chrome.app/Contents/Info.plist CFBundleVersion`
- Check for known issues: https://crbug.com (search "scripting executeScript blocked")
- Workaround: manually navigate to the target URL in Chrome first, then let Bridge attach to that tab instead of creating new ones

## Workaround: Use existing tab instead of creating new

If `browse_open` keeps failing, use `getOrOpenManagedTab` via the `navigate()` + `evaluate()` path instead of `browse_open()` + `browse_do()`. The managed tab path sometimes bypasses the restriction because Chrome treats it differently.

```python
page = BridgePage()
page.navigate('https://target-url.com')
# Wait for page to load
import time; time.sleep(3)
result = page.evaluate('document.title')
```

> **Note**: This is a Chrome platform behavior, not a Bridge bug. Future Chrome updates may change this behavior.
> **Related**: See `references/csp-cdp-fallback.md` for CSP-related injection issues.
