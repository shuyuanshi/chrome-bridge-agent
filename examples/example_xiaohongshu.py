"""End-to-end example: read your own xiaohongshu state.

Prerequisites:
    1. bridge_server.py is running (in another terminal).
    2. The Chrome Bridge extension is installed and connected.
    3. You're already logged into xiaohongshu in that Chrome — i.e. visiting
       https://www.xiaohongshu.com/explore in your browser shows your feed
       without a login banner.

Run from the repo root:
    python examples/example_xiaohongshu.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# allow running this script directly from the repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from bridge_client import BridgePage  # noqa: E402


def main() -> None:
    page = BridgePage()
    if not page.is_extension_connected():
        sys.exit(
            "Chrome Bridge extension is not connected.\n"
            "Make sure bridge_server.py is running and the extension is loaded\n"
            "in chrome://extensions (Site access → 'On all sites')."
        )

    tab = page.browse_open(
        "https://www.xiaohongshu.com/explore",
        timeout=45000,
    )

    # 1. Pull all session cookies for xiaohongshu. These are the cookies
    #    your real browser uses — feed them straight into requests/httpx if
    #    you want to talk to xiaohongshu's API without using the bridge for
    #    every call.
    cookies = page.get_cookies(domain="xiaohongshu.com")
    session_names = [c["name"] for c in cookies if c["name"] in ("web_session", "a1", "webId")]
    print(f"cookies for xiaohongshu.com: {len(cookies)}")
    print(f"  session-relevant: {session_names}")

    # 2. Read enough innerText to figure out whether we're logged in.
    text = page.browse_do(
        tab["tab_id"],
        "document.body.innerText.slice(0, 4000)",
    )
    logged_in = "登录" not in text[:500]
    print(f"  logged in: {logged_in}")

    # 3. Each card in the rendered text looks like:
    #        <note title>
    #        <author>
    #        <like count, e.g. "1.2万">
    #    The regex below matches lines whose next line is a number — i.e.
    #    creator handles.
    authors = [
        m.group(1).strip()
        for m in re.finditer(
            r"\n([^\n]{2,40})\n[\d.]+\s*(?:万|千)?\s*\n",
            text,
        )
    ][:5]
    print(f"first feed creators ({len(authors)}):")
    for a in authors:
        print(f"  - {a}")

    page.browse_close(tab["tab_id"])


if __name__ == "__main__":
    main()
