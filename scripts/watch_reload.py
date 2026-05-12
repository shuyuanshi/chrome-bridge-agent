#!/usr/bin/env python3
"""Watch manifest.json for changes and auto-reload the Chrome Bridge extension.

Usage:
    python watch_reload.py [--once]

    --once   Check once and exit (for manual trigger)
    (no flag) Watch continuously in background

Requires: bridge_server.py running, extension loaded with reload_self support.
"""

import hashlib
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST_PATH = PROJECT_ROOT / "extension" / "manifest.json"
BACKGROUND_PATH = PROJECT_ROOT / "extension" / "background.js"


def get_hash(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest() if path.exists() else ""


def reload_extension():
    """Send reload_self command to bridge server."""
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
    from bridge_client import BridgePage

    page = BridgePage()
    result = page.reload_self()
    print(f"🔄 Reload triggered: {result}")
    # Give extension time to reload
    time.sleep(2)


def main():
    once = "--once" in sys.argv
    watch_paths = [MANIFEST_PATH, BACKGROUND_PATH]

    if not MANIFEST_PATH.exists():
        print(f"❌ manifest.json not found at {MANIFEST_PATH}")
        sys.exit(1)

    print(f"👀 Watching: {MANIFEST_PATH}")
    print(f"   ({'one-shot' if once else 'continuous mode, Ctrl+C to stop'})")

    hashes = {p: get_hash(p) for p in watch_paths}

    # Check once
    reload_extension()
    if once:
        print("✅ Done.")
        return

    # Continuous watch
    while True:
        time.sleep(2)
        for p in watch_paths:
            new_hash = get_hash(p)
            if new_hash != hashes[p]:
                print(f"📝 Detected change: {p.name}")
                hashes[p] = new_hash
                # Wait a moment for file write to complete
                time.sleep(0.5)
                reload_extension()
                print("✅ Reload complete. Watching...")


if __name__ == "__main__":
    main()
