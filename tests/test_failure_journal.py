"""Persistent failure journal: privacy, durability, and offline inspection."""

from __future__ import annotations

import json
import multiprocessing
import os
import re
import stat
from pathlib import Path

import pytest
from bridge_client import BridgePage, main
from bridge_failure_journal import (
    FAILURE_LOG_ENV,
    FailureJournalError,
    append_failure_event,
    failure_report,
    is_valid_failure_id,
    record_extension_failure,
    record_extension_failures,
    record_failure,
)


def _concurrent_writer(path: str, prefix: str, count: int) -> None:
    os.environ[FAILURE_LOG_ENV] = path
    for index in range(count):
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation=f"{prefix}_{index}",
            phase="transport",
            client_version="test",
            dedupe=False,
        )


def test_journal_is_owner_only_and_never_serializes_exception_text_or_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "private" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    canary = "SECRET_CANARY_URL_https://private.example/path?token=abc"

    failure_id = record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation=canary,
        phase="transport",
        exception=RuntimeError(canary),
        scope_id=canary,
        client_version="2.2.0",
    )

    assert failure_id
    raw = path.read_text(encoding="utf-8")
    assert canary not in raw
    event = json.loads(raw)
    assert event["operation"] == "unknown"
    assert event["exception_type"] == "RuntimeError"
    assert event["scope_fingerprint"] != canary
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_report_filters_and_cli_work_without_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    wanted = record_failure(
        component="server",
        code="TIMEOUT",
        operation="browse_open",
        phase="deadline",
        server_version="2.2.0",
    )
    record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation="evaluate",
        phase="transport",
        client_version="2.2.0",
    )

    assert main(["failures", "--id", str(wanted)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["returned"] == 1
    assert payload["events"][0]["failure_id"] == wanted
    assert payload["events"][0]["code"] == "TIMEOUT"


def test_explicit_off_disables_all_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(FAILURE_LOG_ENV, "off")

    failure_id = record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation="evaluate",
        phase="transport",
    )
    report = failure_report()

    assert failure_id
    assert report["disabled"] is True
    assert report["events"] == []
    assert not (tmp_path / ".local" / "state" / "chrome-bridge").exists()


def test_extension_diagnostic_enums_are_a_subset_of_the_host_allowlists() -> None:
    import bridge_failure_journal as journal

    source = (Path(__file__).parents[1] / "extension" / "background.js").read_text(encoding="utf-8")

    def js_set(name: str) -> set[str]:
        match = re.search(rf"const {name} = new Set\(\[(.*?)\]\);", source, re.DOTALL)
        assert match, name
        return set(re.findall(r'"([A-Z_a-z0-9]+)"', match.group(1)))

    assert js_set("SAFE_FAILURE_CODES") <= journal._SAFE_CODES
    assert js_set("SAFE_DIAGNOSTIC_OPERATIONS") <= journal._SAFE_OPERATIONS
    assert js_set("SAFE_FAILURE_PHASES") <= journal._SAFE_PHASES


def test_status_failure_is_journaled_even_though_status_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    result = BridgePage("ws://127.0.0.1:1", token="test").status()

    assert result["error"] == "CONNECTION_FAILED"
    assert result["failure_id"]
    report = failure_report(code="CONNECTION_FAILED")
    assert report["matching_events"] == 1
    assert report["events"][0]["operation"] == "ping_server"


def test_corrupt_line_is_counted_without_being_echoed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    record_failure(component="client", code="TIMEOUT", operation="evaluate", phase="receive")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"raw_secret":"DO_NOT_ECHO"\n')

    report = failure_report()

    assert report["corrupt_lines"] == 1
    assert report["returned"] == 1
    assert "DO_NOT_ECHO" not in json.dumps(report)


def test_append_after_truncated_tail_keeps_the_new_event_reportable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    path.parent.mkdir()
    path.write_bytes(b'{"partial"')
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    failure_id = record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation="evaluate",
        phase="transport",
    )

    report = failure_report(failure_id=failure_id)
    assert report["matching_events"] == 1
    assert report["events"][0]["failure_id"] == failure_id
    assert report["corrupt_lines"] == 1


def test_public_append_api_rebuilds_allowlisted_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    canary = "PRIVATE_https://example.test/?token=secret"

    failure_id = append_failure_event(
        {
            "schema": 1,
            "component": "client",
            "code": "CONNECTION_FAILED",
            "operation": "evaluate",
            "phase": "transport",
            "message": canary,
            "params": {"url": canary},
            "stack": canary,
        }
    )

    assert failure_id
    assert canary not in path.read_text(encoding="utf-8")


def test_failure_id_shape_cannot_masquerade_as_a_64_hex_token() -> None:
    assert is_valid_failure_id("a" * 32)
    assert is_valid_failure_id("12345678-1234-1234-1234-123456789abc")
    assert not is_valid_failure_id("a" * 64)


def test_repeated_host_signature_is_coalesced_with_an_occurrence_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    first = record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation="evaluate",
        phase="transport",
    )
    second = record_failure(
        component="client",
        code="CONNECTION_FAILED",
        operation="evaluate",
        phase="transport",
    )

    report = failure_report()
    assert second == first
    assert report["matching_events"] == 1
    assert report["events"][0]["count"] == 2


def test_distinct_websocket_close_causes_are_not_coalesced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    first = record_failure(
        component="server",
        code="EXTENSION_DISCONNECTED",
        operation="extension_connection",
        phase="receive",
        websocket_close_code=1006,
        retryable=True,
    )
    second = record_failure(
        component="server",
        code="EXTENSION_DISCONNECTED",
        operation="extension_connection",
        phase="receive",
        websocket_close_code=1009,
        retryable=True,
    )

    assert first != second
    assert [event["websocket_close_code"] for event in failure_report()["events"]] == [1006, 1009]


def test_same_extension_id_cannot_rewrite_its_failure_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    event_id = "a" * 32
    base = {
        "event_id": event_id,
        "occurred_at_ms": 1_700_000_000_000,
        "code": "JS_ERROR",
        "operation": "evaluate",
        "phase": "command",
        "count": 1,
        "extension_version": "2.2.0",
    }

    record_extension_failure(base, server_version="2.2.0")
    record_extension_failure(
        {
            **base,
            "occurred_at_ms": 1_700_000_000_100,
            "code": "TAB_CLOSE_FAILED",
            "operation": "browse_close",
            "count": 3,
        },
        server_version="2.2.0",
    )

    event = failure_report()["events"][0]
    assert event["code"] == "JS_ERROR"
    assert event["operation"] == "evaluate"
    assert event["count"] == 1


def test_untrusted_extension_version_cannot_smuggle_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    canary = "1.2.3-" + "a" * 64

    record_extension_failure(
        {
            "event_id": "b" * 32,
            "code": "JS_ERROR",
            "operation": "evaluate",
            "phase": "command",
            "extension_version": canary,
        },
        server_version="2.2.0",
    )

    event = failure_report()["events"][0]
    assert "extension_version" not in event
    assert canary not in path.read_text(encoding="utf-8")


def test_symlink_target_is_refused_without_masking_original_bridge_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "target.txt"
    target.write_text("unchanged", encoding="utf-8")
    link = tmp_path / "failures.jsonl"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    monkeypatch.setenv(FAILURE_LOG_ENV, str(link))

    result = BridgePage("ws://127.0.0.1:1", token="test").status()

    assert result["error"] == "CONNECTION_FAILED"
    assert target.read_text(encoding="utf-8") == "unchanged"
    with pytest.raises(FailureJournalError):
        failure_report()


def test_hard_link_target_and_unsecurable_mode_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    hard_link = tmp_path / "failures.jsonl"
    try:
        os.link(victim, hard_link)
    except OSError:
        pytest.skip("hard links unavailable")
    monkeypatch.setenv(FAILURE_LOG_ENV, str(hard_link))

    assert (
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation="evaluate",
            phase="transport",
        )
        is None
    )
    assert victim.read_text(encoding="utf-8") == "unchanged"

    hard_link.unlink()
    journal = tmp_path / "mode.jsonl"
    journal.write_text("", encoding="utf-8")
    journal.chmod(0o644)
    monkeypatch.setenv(FAILURE_LOG_ENV, str(journal))

    def deny_fchmod(_fd: int, _mode: int) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(os, "fchmod", deny_fchmod)
    assert (
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation="evaluate",
            phase="transport",
        )
        is None
    )
    assert stat.S_IMODE(journal.stat().st_mode) == 0o644


def test_non_finite_semantic_field_is_sanitized_without_crashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    path.parent.mkdir()
    path.write_text(
        '{"schema":1,"failure_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
        '"timestamp":"2026-01-01T00:00:00Z","component":"client",'
        '"code":"TIMEOUT","duration_ms":Infinity}\n',
        encoding="utf-8",
    )
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    report = failure_report()
    assert report["corrupt_lines"] == 0
    assert report["events"][0].get("duration_ms") is None


def test_external_oversized_journal_is_refused_without_loading_or_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bridge_failure_journal as journal

    path = tmp_path / "journal" / "failures.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"x" * (journal.MAX_FILE_BYTES + journal.MAX_EVENT_BYTES + 1))
    original_size = path.stat().st_size
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))

    with pytest.raises(FailureJournalError):
        failure_report()
    assert (
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation="evaluate",
            phase="transport",
        )
        is None
    )
    assert path.stat().st_size == original_size


def test_short_write_rolls_back_instead_of_acknowledging_partial_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    real_write = os.write
    calls = 0

    def short_then_fail(fd: int, payload: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, payload[: max(1, len(payload) // 2)])
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(os, "write", short_then_fail)
    assert (
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation="evaluate",
            phase="transport",
        )
        is None
    )
    report = failure_report()
    assert report["matching_events"] == 0
    assert report["corrupt_lines"] == 0


def test_extension_batch_loads_and_fsyncs_under_one_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bridge_failure_journal as journal

    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    real_load = journal._load_events_locked
    load_calls = 0

    def counted_load(target: Path):
        nonlocal load_calls
        load_calls += 1
        return real_load(target)

    monkeypatch.setattr(journal, "_load_events_locked", counted_load)
    raw_events = [
        {
            "event_id": f"{index:032x}",
            "code": "JS_ERROR",
            "operation": "evaluate",
            "phase": "command",
            "extension_version": "2.2.0",
        }
        for index in range(25)
    ]

    acknowledged = record_extension_failures(raw_events, server_version="2.2.0")

    assert acknowledged == [event["event_id"] for event in raw_events]
    assert load_calls == 1
    assert failure_report(limit=100)["matching_events"] == 25


def test_rotation_keeps_a_bounded_number_of_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import bridge_failure_journal as journal

    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    monkeypatch.setattr(journal, "MAX_FILE_BYTES", 700)
    monkeypatch.setattr(journal, "MAX_BACKUPS", 2)
    written_ids = []
    for index in range(30):
        failure_id = f"{index:032x}"
        written_ids.append(failure_id)
        record_failure(
            component="client",
            code="CONNECTION_FAILED",
            operation=f"operation_{index}",
            phase="transport",
            failure_id=failure_id,
            dedupe=False,
        )

    files = sorted(path.parent.glob("failures.jsonl*"))
    data_files = [candidate for candidate in files if candidate.name != "failures.jsonl.lock"]
    assert {candidate.name for candidate in data_files} <= {
        "failures.jsonl",
        "failures.jsonl.1",
        "failures.jsonl.2",
    }
    assert len(data_files) == 3
    assert all(candidate.stat().st_size <= 1000 for candidate in data_files)
    retained = [event["failure_id"] for event in failure_report(limit=1000)["events"]]
    assert retained == written_ids[-len(retained) :]


def test_parallel_processes_append_parseable_distinct_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "journal" / "failures.jsonl"
    monkeypatch.setenv(FAILURE_LOG_ENV, str(path))
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_concurrent_writer, args=(str(path), f"writer{index}", 12))
        for index in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    report = failure_report(limit=1000)
    assert report["corrupt_lines"] == 0
    assert report["matching_events"] == 36
    assert len({event["failure_id"] for event in report["events"]}) == 36
