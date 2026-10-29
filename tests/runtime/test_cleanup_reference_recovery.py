from __future__ import annotations

from contextlib import closing
import errno
import os
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING

from long_running_agent.process import CommandError
from long_running_agent.runtime import cleanup, cleanup_activity, cleanup_daemon, cleanup_inspection
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (FileNotFoundError(errno.ENOENT, "private-path"), "not-found"),
        (ProcessLookupError(errno.ESRCH, "private-process"), "process-exited"),
        (cleanup_activity.MetadataChangedError("private-metadata"), "metadata-changed"),
        (CommandError("private-command", 124), "timeout"),
        (cleanup_activity.DatabaseBusyError("private-database"), "database-busy"),
    ],
)
def test_transient_census_restarts_every_reference_source_without_merging_partial_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str
) -> None:
    paths = RuntimePaths(tmp_path)
    old = tmp_path / "stale-reference"
    current = tmp_path / "current-reference"
    attempts = 0
    deadline = time.monotonic() + 5

    def external_references(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        nonlocal attempts
        attempts += 1
        assert _paths == paths
        assert candidates == (current,)
        assert cleanup.INSPECTION_DEADLINE.get() == deadline
        if attempts == 1:
            # A partially collected native inventory must not survive a failed source.
            partial = cleanup.References(frozenset({old}))
            assert partial.contains(old)
            with cleanup.reference_stage("workspaces"):
                raise error
        return cleanup.References(frozenset({current}))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Inject the external census boundary failure; production classification, retry, deadline and complete-result handling run unchanged.
        cleanup, "references", external_references
    )
    with cleanup.inspection_window(deadline):
        result = cleanup.live_references(paths, (current,))
    assert attempts == 2
    assert result.contains(current)
    assert not result.contains(old)
    assert kind in cleanup.TRANSIENT_REFERENCE_KINDS


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (PermissionError(errno.EACCES, "private-path"), "permission"),
        (OSError(errno.ENOMEM, "private-resource"), "memory"),
        (OSError(errno.EMFILE, "private-descriptors"), "file-limit"),
        (OSError(errno.EIO, "private-device"), "io"),
        (ValueError("private-invalid-data"), "invalid-data"),
        (RuntimeError("private-unknown-schema"), "unavailable"),
        (CommandError("private-command", 1), "command-failed"),
    ],
)
def test_permanent_census_failure_stops_once_and_emits_only_safe_source_codes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error: Exception, kind: str
) -> None:
    attempts = 0

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        nonlocal attempts
        attempts += 1
        with cleanup.reference_stage("containers"):
            raise error

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model only the external reference reader's failure; the real fail-closed and privacy classification must reject every permanent case.
        cleanup, "references", external_references
    )
    with pytest.raises(cleanup.LiveInspectionFailedError) as caught:
        cleanup.live_references(RuntimePaths(tmp_path))
    assert str(caught.value) == f"live reference inspection failed: containers ({kind}); retained"
    assert "private-" not in str(caught.value)
    assert attempts == 1


def test_persistent_transient_failure_is_bounded_and_keeps_inspection_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        nonlocal attempts
        attempts += 1
        with cleanup.reference_stage("terminals"):
            (tmp_path / "missing-terminal-directory").resolve(strict=True)
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Use the native missing-path exception at the external terminal-reader boundary; all retry and retention logic stays production code.
        cleanup, "references", external_references
    )
    with pytest.raises(cleanup.LiveInspectionFailedError) as caught:
        cleanup.live_references(RuntimePaths(tmp_path))
    reason = str(caught.value)
    assert reason == "live reference inspection failed: terminals (not-found); retained"
    assert attempts == 2
    report = cleanup_daemon.Summary(worktrees=1, attempted=0, removed=0, reasons={reason: 1}, cache="skipped")
    assert cleanup_daemon.incomplete_inspection(report)


def test_retry_never_extends_original_deadline_or_accepts_a_late_complete_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0
    elapsed = 0.0
    deadline = 1.0

    def current_time() -> float:
        return elapsed

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        nonlocal attempts, elapsed
        attempts += 1
        assert cleanup.INSPECTION_DEADLINE.get() == deadline
        if attempts == 1:
            with cleanup.reference_stage("workspaces"):
                message = "native replacement"
                raise cleanup_activity.MetadataChangedError(message)
        elapsed = 2.0
        return cleanup.References(frozenset({tmp_path}))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Control only the OS monotonic reading so scheduling delays cannot make the deadline regression flaky; production deadline enforcement runs unchanged.
        time, "monotonic", current_time
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Delay only the external reader response; the production monotonic deadline must reject an otherwise complete but late result.
        cleanup, "references", external_references
    )
    with cleanup.inspection_window(deadline), pytest.raises(cleanup.InspectionDeferredError):
        cleanup.live_references(RuntimePaths(tmp_path))
    assert attempts == 2


def test_probe_authentication_failure_keeps_its_existing_retry_and_privacy_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        nonlocal attempts
        attempts += 1
        with cleanup.reference_stage("processes"):
            reason: cleanup_inspection.ProbeFailureReason = "reply-identity"
            raise cleanup_inspection.ProbeUnavailableError(reason)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply only a native authenticated-probe boundary rejection; no outer census retry may weaken its existing failure policy.
        cleanup, "references", external_references
    )
    with pytest.raises(cleanup.LiveInspectionFailedError, match="reply-identity"):
        cleanup.live_references(RuntimePaths(tmp_path))
    assert attempts == 1


def test_real_sqlite_lock_is_classified_as_transient_without_reading_an_incomplete_database(tmp_path: Path) -> None:
    database = tmp_path / "state.sqlite"
    with closing(sqlite3.connect(database)) as writer, closing(sqlite3.connect(database, timeout=0.01)) as reader:
        writer.execute(  # sarj-noqa: SARJ429 -- Build private native SQLite metadata for a deliberate read-lock contention fixture.
            "CREATE TABLE workspaces (path TEXT)"
        )
        writer.commit()
        writer.execute("BEGIN EXCLUSIVE")
        with pytest.raises(cleanup.ReferenceInspectionError) as caught, cleanup.reference_stage("workspaces"):
            reader.execute(  # sarj-noqa: SARJ429 -- Observe the real SQLITE_BUSY failure while the native fixture writer holds an exclusive lock.
                "SELECT path FROM workspaces"
            ).fetchall()
        assert caught.value.kind == "database-busy"
        writer.rollback()
        with cleanup.reference_stage("workspaces"):
            assert (
                reader.execute(  # sarj-noqa: SARJ429 -- Verify native reads succeed after releasing the external SQLite lock.
                    "SELECT path FROM workspaces"
                ).fetchall()
                == []
            )


def test_atomic_native_metadata_replacement_is_reopened_before_accepting_current_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_workspace = tmp_path / "old-workspace"
    current_workspace = tmp_path / "current-workspace"
    record = tmp_path / "activity.json"
    replacement = tmp_path / "replacement.json"
    record.write_text(str(old_workspace))
    replacement.write_text(str(current_workspace))
    record.chmod(0o600)
    replacement.chmod(0o600)
    native_open = os.open
    replaced = False
    attempts = 0

    def replace_before_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        if os.fsdecode(path) == str(record) and not replaced:
            replacement.replace(record)
            replaced = True
        return native_open(path, flags, mode, dir_fd=dir_fd)

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        nonlocal attempts
        attempts += 1
        with cleanup.reference_stage("workspaces"):
            budget = cleanup_activity.InventoryBudget(time.monotonic() + 5)
            value = cleanup_activity.metadata_bytes(record, tmp_path, budget, private=True)
        return cleanup.References(frozenset({Path(os.fsdecode(value))}))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Atomically replace only a real private metadata inode between native stat and open; every production ownership and identity check executes unchanged.
        os, "open", replace_before_open
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Bind the external workspace reader to the native private metadata fixture while keeping the real snapshot retry and reference policy.
        cleanup, "references", external_references
    )
    result = cleanup.live_references(RuntimePaths(tmp_path))
    assert replaced
    assert attempts == 2
    assert result.contains(current_workspace)
    assert not result.contains(old_workspace)
    assert record.read_text() == str(current_workspace)


@pytest.mark.parametrize("field", ["stage", "kind"])
def test_malformed_internal_error_codes_cannot_expose_private_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    error = cleanup.ReferenceInspectionError("workspaces", "unavailable")
    vars(error)[field] = "private-diagnostic-value"

    def external_references(_paths: RuntimePaths) -> cleanup.References:
        raise error

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Corrupt only an external error payload; production allowlists must keep diagnostics private and fail closed.
        cleanup, "references", external_references
    )
    with pytest.raises(cleanup.LiveInspectionFailedError) as caught:
        cleanup.live_references(RuntimePaths(tmp_path))
    assert str(caught.value) == "live reference inspection failed; retained"
