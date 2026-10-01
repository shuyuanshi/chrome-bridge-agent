"""Privacy-safe persistent failure journal for Chrome Bridge.

The journal is intentionally separate from debug logging.  It stores only a
small allowlisted schema and never serializes RPC parameters, exception text,
URLs, page content, cookies, tokens, selectors, JavaScript, or stack traces.
Writes are best-effort: a broken journal must never hide the original Bridge
failure.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import stat
import uuid
from collections import defaultdict
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JOURNAL_SCHEMA = 1
FAILURE_LOG_ENV = "CHROME_BRIDGE_FAILURE_LOG"
MAX_FILE_BYTES = 1024 * 1024
MAX_BACKUPS = 3
MAX_EVENT_BYTES = 4096
DEDUPE_SECONDS = 30.0

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_SAFE_FAILURE_ID = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$")
_SAFE_VERSION = re.compile(r"^(?:\d{1,5}(?:\.\d{1,5}){2,3}|test|unknown)$")
_SAFE_SCOPE_FINGERPRINT = re.compile(r"^[0-9a-f]{12}$")
_DISABLED_VALUES = {"off", "none", "disabled", "0"}
UTC = timezone.utc

_SAFE_COMPONENTS = {"client", "server", "extension", "cli", "unknown"}
_SAFE_PHASES = {
    "bind",
    "cleanup_result",
    "command",
    "connect",
    "decode",
    "deadline",
    "delivery",
    "dispatch",
    "extension_send",
    "http_status",
    "hydrate",
    "inflight",
    "iteration",
    "lifecycle",
    "origin",
    "persist",
    "receive",
    "reconnect",
    "register",
    "response",
    "role",
    "route",
    "sync",
    "token",
    "transport",
    "unknown",
    "validate",
}
_SAFE_OPERATIONS = {
    "act_ref",
    "activate_tab",
    "browse_and_eval",
    "browse_close",
    "browse_do",
    "browse_open",
    "cdp_mouse",
    "click_element",
    "close_owned_tabs",
    "dispatch_wheel_event",
    "evaluate",
    "evaluate_function",
    "extension_connection",
    "get_cookies",
    "get_element_attribute",
    "get_element_text",
    "get_elements_count",
    "get_html",
    "get_scroll_top",
    "get_text",
    "get_url",
    "get_viewport_height",
    "handshake",
    "has_element",
    "hover_element",
    "input_content_editable",
    "input_text",
    "list_sessions",
    "list_tabs",
    "mouse_click",
    "mouse_move",
    "navigate",
    "page_fetch",
    "ping_server",
    "press_key",
    "protocol",
    "read_buffer",
    "reload_self",
    "remove_element",
    "screenshot",
    "screenshot_element",
    "scroll_by",
    "scroll_element_into_view",
    "scroll_nth_element_into_view",
    "scroll_to",
    "scroll_to_bottom",
    "select_all_text",
    "select_option",
    "server_start",
    "set_file_input",
    "snapshot",
    "state",
    "type_text",
    "unknown",
    "wait_dom_stable",
    "wait_for_load",
    "wait_for_selector",
    "watch_files",
    "window_lifecycle",
}
_SAFE_CODES = {
    "BAD_REQUEST",
    "BRIDGE_ERROR",
    "CLEANUP_INCOMPLETE",
    "CONNECTION_FAILED",
    "CSP_SYNC_FAILED",
    "DEBUGGER_BUSY",
    "DUPLICATE_EXTENSION",
    "ELEMENT_NOT_FOUND",
    "EXTENSION_DISCONNECTED",
    "EXTENSION_NOT_CONNECTED",
    "FETCH_FAILED",
    "FAILURE_BATCH_RATE_LIMITED",
    "FILE_WATCHER_FAILED",
    "FOREGROUND_REQUIRED",
    "HANDSHAKE_FAILED",
    "HANDSHAKE_SEND_FAILED",
    "HANDSHAKE_TIMEOUT",
    "HTTP_ERROR",
    "INTERNAL",
    "INTERNAL_CLI",
    "JS_ERROR",
    "NAV_TIMEOUT",
    "NO_AUTOMATION_WINDOW",
    "PAGE_ERROR",
    "PROTOCOL_INVALID_FRAME",
    "RELAY_UNREACHABLE",
    "RELOAD_RECONNECT_TIMEOUT",
    "RELOAD_SEND_FAILED",
    "REPLY_DELIVERY_FAILED",
    "RESTRICTED_URL",
    "ROUTE_RESTORE_FAILED",
    "SCOPE_CLOSING",
    "SCREENSHOT_FAILED",
    "SERVER_BIND_FAILED",
    "STATE_HYDRATE_FAILED",
    "STATE_PERSIST_FAILED",
    "STALE_REF",
    "TAB_CLEANUP_FAILED",
    "TAB_CLOSE_FAILED",
    "TAB_GONE",
    "TAB_NOT_OWNED",
    "TAB_SCOPE_MISMATCH",
    "TAB_TRACKING_FAILED",
    "TIMEOUT",
    "UNAUTHORIZED",
    "UNKNOWN",
    "WINDOW_STATE_FAILED",
}
_SAFE_EXCEPTION_TYPES = {
    "BridgeAuthError",
    "BridgeConnectionError",
    "BridgeError",
    "BridgeTimeoutError",
    "ConnectionClosedError",
    "ConnectionError",
    "ElementNotFoundError",
    "ExtensionNotConnectedError",
    "ForegroundRequiredError",
    "JSONDecodeError",
    "JSEvalError",
    "KeyError",
    "NavigationTimeoutError",
    "OSError",
    "PermissionError",
    "RuntimeError",
    "StaleRefError",
    "TabGoneError",
    "TabNotOwnedError",
    "TabScopeMismatchError",
    "TimeoutError",
    "TypeError",
    "ValueError",
    "WebSocketException",
}


class FailureJournalError(RuntimeError):
    """The journal could not be read safely."""


def failure_log_path() -> Path | None:
    """Return the configured journal path, or ``None`` when explicitly disabled."""
    override = os.environ.get(FAILURE_LOG_ENV)
    if override is not None:
        if override.strip().lower() in _DISABLED_VALUES:
            return None
        return Path(override).expanduser().absolute()
    state_home = os.environ.get("XDG_STATE_HOME")
    root = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return root / "chrome-bridge" / "failures.jsonl"


def _safe_token(value: Any, fallback: str = "unknown") -> str:
    text = str(value or "")
    return text if _SAFE_TOKEN.fullmatch(text) else fallback


def _safe_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = str(value or "")
    return text if text in allowed else fallback


def _safe_version(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if _SAFE_VERSION.fullmatch(text) else None


def _safe_failure_id(value: Any) -> str:
    text = str(value or "")
    return text if _SAFE_FAILURE_ID.fullmatch(text) else uuid.uuid4().hex


def is_valid_failure_id(value: Any) -> bool:
    """Return whether a wire ID can safely identify a durable journal event."""
    return isinstance(value, str) and _SAFE_FAILURE_ID.fullmatch(value) is not None


def _scope_fingerprint(scope_id: str | None) -> str | None:
    if not scope_id:
        return None
    return hashlib.sha256(scope_id.encode("utf-8", errors="replace")).hexdigest()[:12]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _timestamp_from_ms(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    try:
        stamp = datetime.fromtimestamp(float(value) / 1000.0, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    # Do not let a corrupted or hostile extension pin a nonsensical timestamp.
    if abs((datetime.now(UTC) - stamp).total_seconds()) > 31 * 24 * 60 * 60:
        return None
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bounded_int(value: Any, *, minimum: int, maximum: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return max(minimum, min(maximum, int(value)))


def build_failure_event(
    *,
    component: str,
    code: str,
    operation: str = "unknown",
    phase: str = "unknown",
    exception: BaseException | None = None,
    duration_ms: int | float | None = None,
    scope_id: str | None = None,
    client_version: str | None = None,
    server_version: str | None = None,
    extension_version: str | None = None,
    failure_id: str | None = None,
    timestamp: str | None = None,
    first_seen: str | None = None,
    last_seen: str | None = None,
    count: int = 1,
    retryable: bool | None = None,
    websocket_close_code: int | None = None,
) -> dict[str, Any]:
    """Create an event from explicit safe fields only."""
    event: dict[str, Any] = {
        "schema": JOURNAL_SCHEMA,
        "failure_id": _safe_failure_id(failure_id),
        "timestamp": _safe_timestamp(timestamp) or _utc_now(),
        "component": _safe_choice(component, _SAFE_COMPONENTS, "unknown"),
        "code": _safe_choice(str(code or "").upper(), _SAFE_CODES, "UNKNOWN"),
        "operation": _safe_choice(operation, _SAFE_OPERATIONS, "unknown"),
        "phase": _safe_choice(phase, _SAFE_PHASES, "unknown"),
        "count": max(1, min(1_000_000, int(count))),
    }
    if exception is not None:
        event["exception_type"] = _safe_choice(
            type(exception).__name__, _SAFE_EXCEPTION_TYPES, "Exception"
        )
    if duration_ms is not None:
        bounded = _bounded_int(duration_ms, minimum=0, maximum=86_400_000)
        if bounded is not None:
            event["duration_ms"] = bounded
    fingerprint = _scope_fingerprint(scope_id)
    if fingerprint:
        event["scope_fingerprint"] = fingerprint
    for key, value in (
        ("client_version", client_version),
        ("server_version", server_version),
        ("extension_version", extension_version),
    ):
        safe = _safe_version(value)
        if safe:
            event[key] = safe
    safe_first_seen = _safe_timestamp(first_seen)
    safe_last_seen = _safe_timestamp(last_seen)
    if safe_first_seen:
        event["first_seen"] = safe_first_seen
    if safe_last_seen:
        event["last_seen"] = safe_last_seen
    if isinstance(retryable, bool):
        event["retryable"] = retryable
    close_code = _bounded_int(websocket_close_code, minimum=0, maximum=65535)
    if close_code is not None:
        event["websocket_close_code"] = close_code
    return event


def _assert_safe_file(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FailureJournalError(
            f"cannot inspect failure journal target: {type(exc).__name__}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
        raise FailureJournalError("failure journal target must be a regular file, not a symlink")


def _assert_private_directory(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise FailureJournalError(
            f"cannot inspect failure journal directory: {type(exc).__name__}"
        ) from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise FailureJournalError("failure journal parent must be a real directory")


def _prepare_directory(path: Path) -> None:
    parent = path.parent
    created = not parent.exists()
    if os.environ.get(FAILURE_LOG_ENV) is None:
        _assert_private_directory(parent)
    parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    if created or os.environ.get(FAILURE_LOG_ENV) is None:
        _assert_private_directory(parent)
        try:
            parent.chmod(0o700)
        except OSError as exc:
            raise FailureJournalError(
                f"cannot secure failure journal directory: {type(exc).__name__}"
            ) from exc
        if os.name != "nt":
            info = parent.stat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise FailureJournalError("failure journal directory is not owner-only")


def _secure_open(path: Path, flags: int, mode: int = 0o600) -> int:
    _assert_safe_file(path)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise FailureJournalError("failure journal target must be a regular file")
        if os.name != "nt":
            if info.st_uid != os.geteuid():
                raise FailureJournalError("failure journal target is not owned by this user")
            if info.st_nlink != 1:
                raise FailureJournalError("failure journal target must not be hard-linked")
            os.fchmod(fd, 0o600)
            if stat.S_IMODE(os.fstat(fd).st_mode) != 0o600:
                raise FailureJournalError("failure journal target is not owner-only")
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(path.name + ".lock")
    fd = _secure_open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI/users
            import msvcrt

            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _journal_files(path: Path) -> list[Path]:
    return [path.with_name(f"{path.name}.{index}") for index in range(MAX_BACKUPS, 0, -1)] + [path]


def _read_lines(path: Path) -> Iterator[str]:
    try:
        fd = _secure_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    if os.fstat(fd).st_size > MAX_FILE_BYTES + MAX_EVENT_BYTES:
        os.close(fd)
        raise FailureJournalError("failure journal file exceeds the bounded size")
    with os.fdopen(fd, "r", encoding="utf-8", errors="replace") as handle:
        yield from handle


def _load_events_locked(path: Path) -> tuple[list[dict[str, Any]], int]:
    # Replayed extension events may append a newer snapshot with the same ID
    # (for example, a coalesced count changing from 1 to 3). Keep only the last
    # durable snapshot and move it to the corresponding chronological position.
    events_by_id: dict[str, dict[str, Any]] = {}
    corrupt = 0
    for candidate in _journal_files(path):
        for line in _read_lines(candidate):
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError):
                corrupt += 1
                continue
            try:
                normalized = _normalize_event_dict(event, require_valid_identity=True)
            except (ValueError, TypeError, OverflowError, OSError):
                normalized = None
            if normalized is None:
                corrupt += 1
                continue
            failure_id = normalized["failure_id"]
            if failure_id in events_by_id:
                del events_by_id[failure_id]
            events_by_id[failure_id] = normalized
    return list(events_by_id.values()), corrupt


def _parse_timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _safe_timestamp(value: Any) -> str | None:
    parsed = _parse_timestamp(value)
    if parsed is None:
        return None
    try:
        stamp = datetime.fromtimestamp(parsed, UTC)
    except (ValueError, OverflowError, OSError):
        return None
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _normalize_event_dict(event: Any, *, require_valid_identity: bool) -> dict[str, Any] | None:
    """Rebuild an event exclusively from allowlisted fields."""
    if not isinstance(event, dict) or event.get("schema") != JOURNAL_SCHEMA:
        return None
    failure_id = event.get("failure_id")
    timestamp = _safe_timestamp(event.get("timestamp"))
    if require_valid_identity and (
        not isinstance(failure_id, str)
        or not _SAFE_FAILURE_ID.fullmatch(failure_id)
        or timestamp is None
    ):
        return None
    normalized = build_failure_event(
        component=event.get("component", "unknown"),
        code=event.get("code", "UNKNOWN"),
        operation=event.get("operation", "unknown"),
        phase=event.get("phase", "unknown"),
        duration_ms=event.get("duration_ms"),
        client_version=event.get("client_version"),
        server_version=event.get("server_version"),
        extension_version=event.get("extension_version"),
        failure_id=failure_id if isinstance(failure_id, str) else None,
        timestamp=timestamp,
        first_seen=_safe_timestamp(event.get("first_seen")),
        last_seen=_safe_timestamp(event.get("last_seen")),
        count=event.get("count", 1) if isinstance(event.get("count", 1), int) else 1,
        retryable=event.get("retryable"),
        websocket_close_code=event.get("websocket_close_code"),
    )
    exception_type = event.get("exception_type")
    if isinstance(exception_type, str):
        normalized["exception_type"] = _safe_choice(
            exception_type, _SAFE_EXCEPTION_TYPES, "Exception"
        )
    fingerprint = event.get("scope_fingerprint")
    if isinstance(fingerprint, str) and _SAFE_SCOPE_FINGERPRINT.fullmatch(fingerprint):
        normalized["scope_fingerprint"] = fingerprint
    return normalized


def _event_signature(event: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(
        event.get(key)
        for key in (
            "component",
            "code",
            "operation",
            "phase",
            "exception_type",
            "scope_fingerprint",
            "client_version",
            "server_version",
            "extension_version",
            "retryable",
            "websocket_close_code",
        )
    )


def _rotate_locked(path: Path, incoming_bytes: int) -> bool:
    try:
        current_size = path.stat().st_size
    except FileNotFoundError:
        return False
    if current_size + incoming_bytes <= MAX_FILE_BYTES:
        return False
    for index in range(MAX_BACKUPS, 0, -1):
        source = path if index == 1 else path.with_name(f"{path.name}.{index - 1}")
        target = path.with_name(f"{path.name}.{index}")
        if not source.exists():
            continue
        _assert_safe_file(source)
        os.replace(source, target)
    return True


def _active_file_needs_separator(path: Path) -> bool:
    try:
        fd = _secure_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return False
    try:
        size = os.fstat(fd).st_size
        if size == 0:
            return False
        os.lseek(fd, -1, os.SEEK_END)
        return os.read(fd, 1) != b"\n"
    finally:
        os.close(fd)


def _append_payload_locked(path: Path, payload: bytes) -> None:
    """Append completely or restore the prior active-file length."""
    separator = b"\n" if _active_file_needs_separator(path) else b""
    rotated = _rotate_locked(path, len(separator) + len(payload))
    if rotated:
        separator = b""
    payload = separator + payload
    fd = _secure_open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
    original_size = os.fstat(fd).st_size
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("failure journal write made no progress")
            view = view[written:]
        os.fsync(fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.ftruncate(fd, original_size)
            os.fsync(fd)
        raise
    finally:
        os.close(fd)


def append_failure_event(event: dict[str, Any], *, dedupe: bool = True) -> str | None:
    """Append one event and return its durable (or coalesced) failure ID."""
    path = failure_log_path()
    normalized = _normalize_event_dict(event, require_valid_identity=False)
    if normalized is None:
        return None
    event = normalized
    if path is None:
        return str(event.get("failure_id"))
    try:
        _prepare_directory(path)
        with _exclusive_lock(path):
            events, _ = _load_events_locked(path)
            same_id_update = False
            for existing in reversed(events):
                if existing.get("failure_id") == event.get("failure_id"):
                    # A replay carrying a larger coalesced count is a newer
                    # snapshot, not a duplicate. Appending keeps the file crash
                    # safe; the reader selects the last snapshot for each ID.
                    if existing == event:
                        return str(existing["failure_id"])
                    if _event_signature(existing) != _event_signature(event):
                        return str(existing["failure_id"])
                    existing_seen = _parse_timestamp(
                        existing.get("last_seen") or existing.get("timestamp")
                    )
                    event_seen = _parse_timestamp(event.get("last_seen") or event.get("timestamp"))
                    if int(event.get("count", 1)) <= int(existing.get("count", 1)) and (
                        event_seen or 0
                    ) <= (existing_seen or 0):
                        return str(existing["failure_id"])
                    same_id_update = True
                    break
            if (
                dedupe
                and not same_id_update
                and events
                and _event_signature(events[-1]) == _event_signature(event)
            ):
                previous = _parse_timestamp(events[-1].get("timestamp"))
                current = _parse_timestamp(event.get("timestamp"))
                if (
                    previous is not None
                    and current is not None
                    and 0 <= current - previous <= DEDUPE_SECONDS
                ):
                    event = {
                        **events[-1],
                        "timestamp": event["timestamp"],
                        "last_seen": event.get("last_seen", event["timestamp"]),
                        "first_seen": events[-1].get("first_seen", events[-1]["timestamp"]),
                        "count": min(
                            1_000_000,
                            int(events[-1].get("count", 1)) + int(event.get("count", 1)),
                        ),
                    }
            encoded = (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            )
            if len(encoded) > MAX_EVENT_BYTES:
                return None
            _append_payload_locked(path, encoded)
        return str(event["failure_id"])
    except (OSError, FailureJournalError, ValueError, TypeError):
        return None


def record_failure(**kwargs: Any) -> str | None:
    """Build and persist one event; never raise into the Bridge call path."""
    dedupe = kwargs.pop("dedupe", True)
    try:
        event = build_failure_event(**kwargs)
    except (ValueError, TypeError, OverflowError):
        return None
    return append_failure_event(event, dedupe=bool(dedupe))


def _extension_event(raw: Any, *, server_version: str | None) -> dict[str, Any] | None:
    """Build one extension event without ever copying arbitrary fields."""
    if not isinstance(raw, dict):
        return None
    event_id = raw.get("event_id")
    if not isinstance(event_id, str) or not _SAFE_FAILURE_ID.fullmatch(event_id):
        return None
    timestamp = _timestamp_from_ms(raw.get("last_seen_ms") or raw.get("occurred_at_ms"))
    first_seen = _timestamp_from_ms(raw.get("first_seen_ms"))
    return build_failure_event(
        component="extension",
        code=raw.get("code", "INTERNAL"),
        operation=raw.get("operation", "unknown"),
        phase=raw.get("phase", "unknown"),
        extension_version=raw.get("extension_version"),
        server_version=server_version,
        failure_id=event_id,
        timestamp=timestamp,
        first_seen=first_seen,
        last_seen=timestamp,
        count=raw.get("count", 1) if isinstance(raw.get("count", 1), int) else 1,
        retryable=raw.get("retryable"),
        websocket_close_code=raw.get("websocket_close_code"),
    )


def _same_id_update_is_newer(existing: dict[str, Any], event: dict[str, Any]) -> bool:
    if _event_signature(existing) != _event_signature(event):
        return False
    existing_seen = _parse_timestamp(existing.get("last_seen") or existing.get("timestamp"))
    event_seen = _parse_timestamp(event.get("last_seen") or event.get("timestamp"))
    return int(event.get("count", 1)) > int(existing.get("count", 1)) or (
        (event_seen or 0) > (existing_seen or 0)
    )


def record_extension_failures(raw_events: Any, *, server_version: str | None = None) -> list[str]:
    """Validate and durably append one acknowledged extension batch."""
    if not isinstance(raw_events, list):
        return []
    events: list[dict[str, Any]] = []
    for raw in raw_events[:25]:
        try:
            event = _extension_event(raw, server_version=server_version)
        except (ValueError, TypeError, OverflowError):
            event = None
        if event is not None:
            events.append(event)
    if not events:
        return []

    path = failure_log_path()
    if path is None:
        return list(dict.fromkeys(str(event["failure_id"]) for event in events))
    try:
        _prepare_directory(path)
        with _exclusive_lock(path):
            retained, _ = _load_events_locked(path)
            by_id = {str(event["failure_id"]): event for event in retained}
            accepted: list[str] = []
            accepted_set: set[str] = set()
            pending: list[dict[str, Any]] = []
            for event in events:
                failure_id = str(event["failure_id"])
                existing = by_id.get(failure_id)
                if existing is None:
                    pending.append(event)
                    by_id[failure_id] = event
                elif _same_id_update_is_newer(existing, event):
                    # Legacy 2.2 prerelease queues may replay a coalesced count.
                    # The append-only reader selects this last snapshot by ID.
                    pending.append(event)
                    by_id[failure_id] = event
                if failure_id not in accepted_set:
                    accepted.append(failure_id)
                    accepted_set.add(failure_id)

            encoded_parts = [
                (json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
                for event in pending
            ]
            if any(len(item) > MAX_EVENT_BYTES for item in encoded_parts):
                return []
            payload = b"".join(encoded_parts)
            if payload:
                _append_payload_locked(path, payload)
            return accepted
    except (OSError, FailureJournalError, ValueError, TypeError):
        return []


def record_extension_failure(raw: Any, *, server_version: str | None = None) -> str | None:
    """Backward-compatible single-event wrapper around batched ingestion."""
    recorded = record_extension_failures([raw], server_version=server_version)
    return recorded[0] if recorded else None


def failure_report(
    *,
    limit: int = 50,
    code: str | None = None,
    component: str | None = None,
    failure_id: str | None = None,
) -> dict[str, Any]:
    """Return filtered events and an aggregate summary without needing the relay."""
    path = failure_log_path()
    if path is None:
        return {
            "schema": JOURNAL_SCHEMA,
            "disabled": True,
            "log_path": None,
            "returned": 0,
            "corrupt_lines": 0,
            "summary": [],
            "events": [],
        }
    try:
        _prepare_directory(path)
        with _exclusive_lock(path):
            events, corrupt = _load_events_locked(path)
    except (OSError, FailureJournalError) as exc:
        raise FailureJournalError(f"could not read failure journal: {type(exc).__name__}") from exc

    if code:
        wanted_code = _safe_token(code.upper())
        events = [event for event in events if event.get("code") == wanted_code]
    if component:
        wanted_component = _safe_token(component)
        events = [event for event in events if event.get("component") == wanted_component]
    if failure_id:
        events = [event for event in events if event.get("failure_id") == failure_id]

    groups: dict[tuple[str, str, str, str], dict[str, Any]] = defaultdict(
        lambda: {"occurrences": 0, "last_seen": None}
    )
    for event in events:
        key = (
            str(event.get("component", "unknown")),
            str(event.get("code", "UNKNOWN")),
            str(event.get("operation", "unknown")),
            str(event.get("phase", "unknown")),
        )
        group = groups[key]
        group["occurrences"] += int(event.get("count", 1))
        group["last_seen"] = event.get("last_seen") or event.get("timestamp")

    summary = [
        {
            "component": key[0],
            "code": key[1],
            "operation": key[2],
            "phase": key[3],
            **value,
        }
        for key, value in groups.items()
    ]
    summary.sort(
        key=lambda item: (str(item.get("last_seen")), int(item["occurrences"])), reverse=True
    )
    bounded_limit = max(0, min(int(limit), 1000))
    returned = events[-bounded_limit:] if bounded_limit else []
    return {
        "schema": JOURNAL_SCHEMA,
        "disabled": False,
        "log_path": str(path),
        "returned": len(returned),
        "matching_events": len(events),
        "corrupt_lines": corrupt,
        "summary": summary,
        "events": returned,
    }
