"""Shared token helpers for the Chrome Bridge relay.

The relay speaks WebSocket on loopback. Loopback is *not* a security boundary:
``ws://localhost`` counts as a potentially-trustworthy origin, so an ordinary
``https://`` page is allowed to open a socket to it. Without a check, any page
the user visits could send ``{"role": "cli", "method": "get_cookies"}`` and walk
away with every session cookie in the profile.

Two cheap gates close that:

1. **Token** — CLI clients must present a secret that only processes able to
   read ``~/.chrome-bridge-token`` (mode 0600) can know. A web page cannot read
   files, so this alone stops the browser vector.
2. **Origin** — browsers always attach an ``Origin`` header; the Python client
   never does. Anything presenting a non-``chrome-extension://`` origin is a web
   page and is refused outright.

Residual risk, stated plainly: another process running as the same macOS user
can read the token file, and nothing authenticates the *server* to the
extension — a process that binds port 9333 first can pose as the relay. This is
a defence against web pages and other users, not against local software you
already invited in.
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
from pathlib import Path

TOKEN_ENV = "CHROME_BRIDGE_TOKEN"
TOKEN_FILE_ENV = "CHROME_BRIDGE_TOKEN_FILE"
DEFAULT_TOKEN_PATH = Path.home() / ".chrome-bridge-token"

_LOG = logging.getLogger("chrome_bridge")


def token_path() -> Path:
    """Where the shared secret lives (override with CHROME_BRIDGE_TOKEN_FILE)."""
    override = os.environ.get(TOKEN_FILE_ENV)
    return Path(override).expanduser() if override else DEFAULT_TOKEN_PATH


def read_token() -> str | None:
    """Read the token from the environment, else from disk. None if absent."""
    from_env = os.environ.get(TOKEN_ENV)
    if from_env:
        return from_env.strip()
    path = token_path()
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value:
        _warn_if_readable_by_others(path)
    return value or None


def ensure_token() -> str:
    """Return the existing token, generating and persisting one if needed.

    An existing token file has its mode re-tightened to 0600 on every start —
    a file restored from a dotfile backup under a permissive umask would
    otherwise stay world-readable, and the secret with it.
    """
    existing = read_token()
    if existing:
        if not os.environ.get(TOKEN_ENV):
            _tighten(token_path())
        return existing
    token = secrets.token_hex(32)
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_NOFOLLOW: refuse to write through a symlink someone else planted.
    # fchmod: the mode argument only applies when *creating*, so an existing
    # file (restored from a dotfile backup, say) would keep its old, possibly
    # world-readable permissions.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(token + "\n")
    return token


def _tighten(path: Path) -> None:
    """Re-apply 0600 to an existing token file, warning if it had been wider."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return
    if mode & 0o077:
        _LOG.warning("%s was readable by other accounts (mode %o) — tightening to 600", path, mode)
        with contextlib.suppress(OSError):
            path.chmod(0o600)


def _warn_if_readable_by_others(path: Path) -> None:
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if stat.S_IMODE(mode) & 0o077:
        _LOG.warning(
            "%s is readable by other accounts (mode %o) — any local user can drive "
            "your browser. Fix with: chmod 600 %s",
            path,
            stat.S_IMODE(mode),
            path,
        )
