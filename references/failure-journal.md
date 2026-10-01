# Failure journal

Use this reference when diagnosing a reported Chrome Bridge failure or changing
the journal itself.

## Guarantees

- Client transport failures are written even when the relay is down.
- Relay/protocol failures and extension command failures share the same host
  journal. The extension first persists its own safe event in
  `chrome.storage.local` (at most 100 events or 64 KiB, expiring after seven
  days), then sends acknowledged batches after reconnect.
- Replayed extension events are deduplicated by immutable `failure_id`. The
  extension records at most one event per relay-outage episode, so expected
  between-task reconnect attempts cannot flood the journal.
- Every journal/lock file and the default or newly created parent directory are
  owner-only; an existing custom parent directory must be trusted. The host uses
  a cross-process lock and up to three 1 MiB rotated backups. A journal I/O
  failure never replaces the original Bridge exception.
- Events contain only schema version, time, failure ID, component, typed code,
  fixed operation/phase, exception class name, duration, an opaque scope
  fingerprint, component versions, retryability, count, and numeric WebSocket
  close code when applicable.
- Events never contain command parameters, raw exception messages, URLs,
  origins, tab titles or IDs, selectors, expressions, typed text, file paths,
  headers, bodies, cookies, tokens, DOM/page text, screenshots, or stacks.

Default path: `~/.local/state/chrome-bridge/failures.jsonl`. Set
`CHROME_BRIDGE_FAILURE_LOG=/absolute/path` to relocate it or `off` to disable
persistence. The `chrome-bridge failures` command reports malformed/truncated
line counts instead of echoing unsafe file content.

This is diagnostic evidence, not a tamper-evident security audit log. Extension
batches are allowlisted and rate-limited, but a local process able to impersonate
the extension role could still inject sanitized events or acknowledge queued
ones.

## Review workflow

1. Run `chrome-bridge failures --limit 50`; narrow with `--id`, `--code`, or
   `--component` when useful.
2. Match the failure by time, ID, typed code, operation, phase, and component
   versions. State which facts are logged and which cause is still inferred.
3. Reproduce with the smallest safe/read-only command. Because Bridge commands
   are at-most-once, inspect state before retrying any action with side effects.
4. Fix the smallest owning layer: client, relay, extension, or documentation.
   Add a regression test for the exact signature rather than a string-matching
   test for prose.
5. Run static checks and the focused test, then live integration when the
   affected behavior needs Chrome. Confirm cleanup leaves zero owned tabs and
   the reproduction creates no new matching event.
6. Do not delete or clear the journal automatically. Rotation handles retention;
   preserving prior IDs makes later recurrence analysis possible.

The journal is diagnostic evidence, not permission to mutate code or retry a
browser action. Apply improvements only when the user asks.
