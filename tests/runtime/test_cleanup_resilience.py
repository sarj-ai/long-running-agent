from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import random
import shutil
import socket
import sqlite3
import sys
from threading import Event
import time

from long_running_agent.process import environment_snapshot
from long_running_agent.runtime import cleanup, cleanup_activity, cleanup_daemon, cleanup_state
from long_running_agent.runtime.settings import RuntimePaths, private_write
from pydantic import JsonValue, RootModel
import pytest


@pytest.fixture
def seed() -> int:
    return int(environment_snapshot().get("LRA_CLEANUP_TEST_SEED", "0"))


@pytest.fixture
def paths(tmp_path: Path) -> RuntimePaths:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    return RuntimePaths(home)


def repository(path: Path) -> Path:
    path.mkdir(parents=True)
    cleanup.git(path, "init", "-q")
    cleanup.git(path, "config", "user.name", "Cleanup fixture")
    cleanup.git(path, "config", "user.email", "cleanup@example.invalid")
    (path / "source.txt").write_text("Preserve this source.\n")
    cleanup.git(path, "add", "source.txt")
    cleanup.git(path, "commit", "-qm", "Fixture source")
    return path


def native_database(home: Path, workspace: Path) -> Path:
    store = home / ".codex"
    store.mkdir(mode=0o700)
    database = store / "state_5.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(  # sarj-noqa: SARJ429 -- Build only the external native path metadata schema in private disposable storage.
            "CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,archived INTEGER NOT NULL,source TEXT NOT NULL);"
            "CREATE TABLE project_roots (project_id TEXT NOT NULL,position INTEGER NOT NULL,path TEXT NOT NULL);"
            "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY,status TEXT NOT NULL);"
            "INSERT INTO backfill_state VALUES (1,'complete');"
            "CREATE TABLE rollout_migration_skipped_rollouts (migration_id TEXT NOT NULL,rollout_path TEXT NOT NULL);"
        )
        connection.execute(  # sarj-noqa: SARJ429 -- Seed external workspace metadata without conversation or authentication data.
            "INSERT INTO threads VALUES ('fixture',?,1,1,1,'cli')", (str(workspace),)
        )
        connection.commit()
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    return database


def worker_payload(count: int = 1) -> str:
    return json.dumps(
        {
            "apply": True,
            "max_removals": 2,
            "worktrees": [
                {"path": f"/fixture/{index}", "reason": "retained", "removed": False, "attempted": False}
                for index in range(count)
            ],
            "cache": "skipped: fixture",
            "inventory_paged": True,
            "inventory_pending": True,
            "inventory_errors": 0,
        }
    )


def test_native_inventory_pages_survive_restart_without_losing_candidates(
    paths: RuntimePaths, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(seed)
    page_size = rng.randint(1, 4)
    count = page_size * rng.randint(3, 5) + 1
    main = repository(paths.code / "org/repo")
    expected: set[Path] = set()
    for index in rng.sample(range(1000), count):
        prefix = ("tree-", "space tree-", "utf8-🧪-")[(seed + index) % 3]
        checkout = paths.code / "worktrees" / f"{prefix}{index:04}"
        cleanup.git(main, "worktree", "add", "-q", "--detach", str(checkout), "HEAD")
        expected.add(checkout)
    # Native directory discovery must not descend through arbitrary audit trees.
    for index in range(20):
        (paths.code / f"outputs/audit-{index}/deep/nested/fixture").mkdir(parents=True)
    broken = paths.code / "org/broken"
    (broken / ".git").mkdir(parents=True)
    (broken / ".git/HEAD").write_text("invalid native Git state\n")
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Shrink only the documented page budget; Git inventory, persistence, and directory discovery remain native.
        cleanup, "INVENTORY_PAGE_SIZE", page_size
    )
    seen: set[Path] = set()
    failures = 0
    last_errors: int | None = None
    state = cleanup.state_directory(paths)
    for _index in range(count + 5):
        page = cleanup.candidate_page(paths)
        assert len(page.entries) <= page_size
        assert seen.isdisjoint(entry.path for entry in page.entries)
        assert all(entry.repository == main and entry.path != main for entry in page.entries)
        seen.update(entry.path for entry in page.entries)
        failures = max(failures, page.errors)
        last_errors = page.errors
        cleanup_state.save_inventory_cursor(state, paths.code, page.cursor)
        if page.cursor is not None:
            # Every iteration reloads the exact persisted checkpoint, as after restart.
            assert cleanup_state.inventory_cursor(state, paths.code) == page.cursor
        if not page.pending:
            break
    assert seen == expected
    assert failures == 1
    assert last_errors == 1
    assert not (state / "cleanup-inventory-cursor.json").exists()
    assert (main / "source.txt").read_text() == "Preserve this source.\n"
    assert all((checkout / "source.txt").is_file() for checkout in expected)


def test_disappearing_cursor_and_untrusted_progress_cannot_authorize_removal(paths: RuntimePaths, seed: int) -> None:
    main = repository(paths.code / "org/repo")
    checkout = paths.code / "worktrees/retained"
    cleanup.git(main, "worktree", "add", "-q", "--detach", str(checkout), "HEAD")
    state = cleanup.state_directory(paths)
    cursor_path = state / "cleanup-inventory-cursor.json"
    payload = f"broken-{seed}" if seed % 2 else '{"repository":"/another-user/repo"}'
    private_write(cursor_path, payload)
    page = cleanup.candidate_page(paths)
    assert [entry.path for entry in page.entries] == [checkout]
    assert cursor_path.with_name(cursor_path.name + ".invalid").read_text() == payload
    cleanup_state.save_inventory_cursor(
        state, paths.code, cleanup_state.InventoryCursor(repository=paths.code / "org/previously-deleted")
    )
    assert [entry.path for entry in cleanup.candidate_page(paths).entries] == [checkout]
    assert (checkout / "source.txt").is_file()


def test_native_inventory_over_previous_hard_cap_is_paged(paths: RuntimePaths, seed: int) -> None:
    main = repository(paths.code / "org/scale")
    prototype = paths.code / "worktrees/prototype"
    cleanup.git(main, "worktree", "add", "-q", "--detach", str(prototype), "HEAD")
    metadata = Path(cleanup.git(prototype, "rev-parse", "--absolute-git-dir").strip())
    expected = {prototype}
    for index in range(cleanup.MAX_DIRECTORIES + 1):
        (paths.code / f"worktrees/retained-placeholder-{index:04}").mkdir()
    for index in range(1025 + seed % 23):
        checkout = paths.code / f"worktrees/bulk-{index:04}"
        checkout.mkdir()
        registration = metadata.parent / checkout.name
        # Copy the actual external Git fixture layout, then ask native Git to
        # enumerate it. No cleanup parser or eligibility result is fabricated.
        shutil.copytree(metadata, registration)
        (registration / "gitdir").write_text(str(checkout / ".git") + "\n")
        (checkout / ".git").write_text(f"gitdir: {registration}\n")
        shutil.copyfile(prototype / "source.txt", checkout / "source.txt")
        expected.add(checkout)
    assert len(cleanup.parse_worktrees(cleanup.git(main, "worktree", "list", "--porcelain", "-z"), main)) > 1024
    state = cleanup.state_directory(paths)
    seen: set[Path] = set()
    pages = (len(expected) + cleanup.INVENTORY_PAGE_SIZE - 1) // cleanup.INVENTORY_PAGE_SIZE
    for _index in range(pages + 1):
        page = cleanup.candidate_page(paths)
        assert len(page.entries) <= cleanup.INVENTORY_PAGE_SIZE
        assert not page.errors
        assert seen.isdisjoint(entry.path for entry in page.entries)
        seen.update(entry.path for entry in page.entries)
        cleanup_state.save_inventory_cursor(state, paths.code, page.cursor)
        if not page.pending:
            break
    assert seen == expected
    assert all((checkout / "source.txt").is_file() for checkout in expected)
    assert len(tuple((paths.code / "worktrees").iterdir())) > cleanup.MAX_DIRECTORIES


def test_closed_wal_and_concurrent_writer_preserve_committed_workspace_references(
    paths: RuntimePaths, seed: int
) -> None:
    workspace = paths.code / f"archived-{seed}"
    database = native_database(paths.home, workspace)
    assert not database.with_name(database.name + "-wal").exists()
    before = database.read_bytes()
    assert workspace in cleanup_activity.collect(paths.home).paths
    assert database.read_bytes() == before
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(  # sarj-noqa: SARJ429 -- Hold a real uncommitted external SQLite writer while the cleanup collector reads its native snapshot.
            "UPDATE threads SET cwd=?", (str(paths.code / f"uncommitted-{seed}"),)
        )
        references = cleanup_activity.collect(paths.home)
        assert workspace in references.paths
        assert paths.code / f"uncommitted-{seed}" not in references.paths
        writer.rollback()
        committed = paths.code / f"committed-in-wal-{seed}"
        writer.execute(  # sarj-noqa: SARJ429 -- Commit external workspace metadata into a live WAL and require native read-only inspection to protect it before checkpoint.
            "INSERT INTO threads VALUES ('committed',?,1,1,0,'cli')", (str(committed),)
        )
        writer.commit()
        assert committed in cleanup_activity.collect(paths.home).paths
    assert workspace in cleanup_activity.collect(paths.home).paths
    assert committed in cleanup_activity.collect(paths.home).paths


@pytest.mark.parametrize("damage", ["partial-sidecars", "hardlink", "unknown-schema"])
def test_ambiguous_native_sqlite_is_retained(paths: RuntimePaths, damage: str) -> None:
    database = native_database(paths.home, paths.code / "retained")
    match damage:
        case "partial-sidecars":
            database.with_name(database.name + "-wal").touch()
        case "hardlink":
            database.with_name("ambiguous-link").hardlink_to(database)
        case _:
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(  # sarj-noqa: SARJ429 -- Corrupt only the external native schema in private disposable storage to verify fail-closed inspection.
                    "ALTER TABLE threads RENAME TO unknown_threads"
                )
                connection.commit()
    with pytest.raises(RuntimeError, match=r"WAL|hard links|schema|metadata"):
        cleanup_activity.collect(paths.home)
    assert database.exists()


def test_corrupt_heartbeat_recovers_without_resetting_native_removal_budget(paths: RuntimePaths, seed: int) -> None:
    state = cleanup.state_directory(paths)
    stamp = cleanup_state.AttemptStamp.current()
    for index in range(2):
        assert cleanup_state.reserve_attempt(state, stamp, paths.code / f"retired-{index}", "a" * 40)
    ledger = state / "cleanup-attempts.json"
    before = ledger.read_bytes()
    payload = f"corrupt-heartbeat-{seed}"
    private_write(state / cleanup_daemon.STATUS_FILE, payload)
    now = time.monotonic()
    restored = cleanup_daemon.Schedule.restore(state, now, time.time())
    assert restored.phase == "error"
    assert restored.due == now + cleanup_daemon.PRESSURE_SECONDS
    assert restored.failures == 1
    assert restored.last_failure is not None
    assert restored.last_failure.kind == "invalid-data"
    assert (
        (state / (cleanup_daemon.STATUS_FILE + ".invalid")).read_text() == payload
    )  # sarj-noqa: SARJ402 -- Exact byte preservation of corrupt private status is the quarantine contract, not an implementation-text oracle.
    assert cleanup_state.remaining_attempts(state, stamp) == 0
    assert ledger.read_bytes() == before


@pytest.mark.parametrize("damage", ["symlink", "hardlink", "public", "fifo"])
def test_unsafe_heartbeat_is_never_replaced_as_recoverable_progress(paths: RuntimePaths, damage: str) -> None:
    state = cleanup.state_directory(paths)
    target = state / "protected-target"
    private_write(target, "Preserve this target")
    heartbeat = state / cleanup_daemon.STATUS_FILE
    match damage:
        case "symlink":
            heartbeat.symlink_to(target)
        case "hardlink":
            heartbeat.hardlink_to(target)
        case "public":
            private_write(heartbeat, "invalid public state")
            heartbeat.chmod(0o644)
        case _:
            os.mkfifo(heartbeat, 0o600)
    with pytest.raises((OSError, RuntimeError)):
        cleanup_daemon.Schedule.restore(state, time.monotonic(), time.time())
    assert target.read_text() == "Preserve this target"
    assert not (state / (cleanup_daemon.STATUS_FILE + ".invalid")).exists()


def test_status_verifies_process_birth_and_boot_instead_of_pid_alone(paths: RuntimePaths, seed: int) -> None:
    state = cleanup.state_directory(paths)
    cleanup_daemon.publish(
        state, cleanup_daemon.Schedule.new(), cleanup_daemon.Space(total=100, available=50), "waiting"
    )
    heartbeat = cleanup_daemon.Heartbeat.model_validate_json((state / cleanup_daemon.STATUS_FILE).read_bytes())
    assert cleanup_daemon.daemon_alive(heartbeat)
    assert heartbeat.process_start is not None
    replaced = heartbeat.model_copy(update={"process_start": heartbeat.process_start + seed + 1})
    assert not cleanup_daemon.daemon_alive(replaced)
    assert not cleanup_daemon.daemon_alive(heartbeat.model_copy(update={"boot_id": "0" * 36}))
    assert not cleanup_daemon.daemon_alive(heartbeat.model_copy(update={"phase": "stopped"}))


@pytest.mark.parametrize("abstract", [False, True])
def test_watchdog_notifications_require_supervisor_progress_and_identity(
    tmp_path: Path, seed: int, abstract: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoint = f"@lra-cleanup-fixture-{os.getpid()}-{seed}" if abstract else str(tmp_path / "notify.sock")
    address = "\0" + endpoint[1:] if abstract else endpoint
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver:
        receiver.bind(address)
        receiver.settimeout(0.05)
        monkeypatch.setenv("NOTIFY_SOCKET", endpoint)
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid()))
        cleanup_daemon.systemd_notify("READY=1\nWATCHDOG=1")
        assert receiver.recv(128) == b"READY=1\nWATCHDOG=1"
        monkeypatch.setenv("WATCHDOG_PID", str(os.getpid() + 1))
        cleanup_daemon.systemd_notify("WATCHDOG=1")
        with pytest.raises(TimeoutError):
            receiver.recv(128)


def test_pending_pages_retry_and_never_certify_a_global_clean_baseline(seed: int) -> None:
    report = cleanup_daemon.WorkerReport.model_validate_json(worker_payload(1 + seed % 128))
    summary = cleanup_daemon.summarize(report)
    assert summary.inventory_paged
    assert summary.inventory_pending
    assert not cleanup_daemon.baseline_learning_eligible(summary)
    schedule = cleanup_daemon.Schedule.new()
    schedule.completed(summary, cleanup_daemon.Space(total=100, available=99), 100, 1000)
    assert schedule.due == 100 + cleanup_daemon.LOW_SPACE_SECONDS
    summary = summary.model_copy(update={"inventory_errors": 1, "inventory_pending": False})
    assert schedule.completed(summary, cleanup_daemon.Space(total=100, available=99), 200, 1100) == "blocked"
    assert schedule.due == 200 + cleanup_daemon.PRESSURE_SECONDS


def test_large_report_and_worker_faults_cannot_publish_a_success(seed: int) -> None:
    payload = worker_payload(1024)
    arguments = [sys.executable, "-c", "import sys;sys.stdout.write(sys.argv[1])", payload]
    assert cleanup_daemon.execute_worker(Event(), arguments).worktrees == 1024
    damaged = "prefix-" + payload if seed % 2 else payload + "-suffix"
    arguments[-1] = damaged
    with pytest.raises(ValueError, match="Invalid JSON"):
        cleanup_daemon.execute_worker(Event(), arguments)
    nonzero = [sys.executable, "-c", "import sys;sys.exit(int(sys.argv[1]))", str(1 + seed % 125)]
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="failed"):
        cleanup_daemon.execute_worker(Event(), nonzero)
    flood = [sys.executable, "-c", "import os;os.write(1,b'x'*(4*1024*1024+65536))"]
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="bounded size"):
        cleanup_daemon.execute_worker(Event(), flood)
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="deadline exceeded"):
        cleanup_daemon.execute_worker(Event(), [sys.executable, "-c", "import time;time.sleep(20)"], timeout=0.05)


def test_oversized_report_keeps_the_protocol_bound() -> None:
    with pytest.raises(ValueError, match="at most 1024"):
        cleanup_daemon.WorkerReport.model_validate_json(worker_payload(1025))


def test_primary_code_repository_can_resume_a_page(paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch) -> None:
    main = repository(paths.code)
    expected: set[Path] = set()
    for name in ("first", "second"):
        checkout = paths.code / "worktrees" / name
        cleanup.git(main, "worktree", "add", "-q", "--detach", str(checkout), "HEAD")
        expected.add(checkout)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Shrink only the page budget while Git and checkpoint persistence remain native.
        cleanup, "INVENTORY_PAGE_SIZE", 1
    )
    first = cleanup.candidate_page(paths)
    assert first.pending
    assert first.cursor is not None
    cleanup_state.save_inventory_cursor(cleanup.state_directory(paths), paths.code, first.cursor)
    second = cleanup.candidate_page(paths)
    assert not second.pending
    assert {entry.path for entry in [*first.entries, *second.entries]} == expected
    assert all(entry.path != main for entry in [*first.entries, *second.entries])


def test_saved_summary_rejects_inconsistent_partial_inventory() -> None:
    summary = cleanup_daemon.summarize(cleanup_daemon.WorkerReport.model_validate_json(worker_payload()))
    invalid = summary.model_dump()
    invalid["inventory_paged"] = False
    with pytest.raises(ValueError, match="inconsistent inventory progress"):
        cleanup_daemon.Summary.model_validate(invalid)


def test_transient_reference_failure_retries_before_normal_cadence() -> None:
    report = cleanup_daemon.WorkerReport.model_validate_json(worker_payload())
    report.worktrees[0].reason = "live reference inspection failed; retained"
    report.inventory_pending = False
    result = cleanup_daemon.summarize(report)
    schedule = cleanup_daemon.Schedule.new()
    assert schedule.completed(result, cleanup_daemon.Space(total=100, available=99), 100, 1000) == "blocked"
    assert schedule.due == 100 + cleanup_daemon.PRESSURE_SECONDS
    assert not cleanup_daemon.baseline_learning_eligible(result)


@dataclass
class ReviewClock:
    elapsed: float = 0.0

    def tick(self) -> float:
        self.elapsed += 0.1
        return self.elapsed


def test_partial_inventory_pages_visit_every_candidate_across_restarts(
    paths: RuntimePaths, seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    rng = random.Random(seed)
    indices = rng.sample(range(1000), 9 + seed % 4)
    expected: set[Path] = set()
    for name in ("first", "second"):
        main = repository(paths.code / f"org/{name}")
        for index in indices:
            checkout = paths.code / f"worktrees/{name}-{index:04}"
            cleanup.git(main, "worktree", "add", "-q", "--detach", str(checkout), "HEAD")
            modified = 1000000 + (seed + index * 13) % 10000
            os.utime(checkout, (modified, modified))
            expected.add(checkout)
    state = cleanup.state_directory(paths)
    legacy = cleanup_state.ReviewCursor(version=1, modified=time.time() + 1000, path=max(expected))
    cleanup_state.save_review_cursor(state, paths.code, legacy)
    legacy_bytes = (state / "cleanup-cursor.json").read_bytes()
    seen: set[Path] = set()
    for _sweep in range(len(expected) + 2):
        batch = cleanup.candidate_page(paths)
        progress = cleanup.ReviewProgress()
        with monkeypatch.context() as bounded:
            bounded.setattr(  # sarj-noqa: SARJ445 -- A deterministic OS clock expires native Git fixture reviews without replacing retention or removal decisions.
                time, "monotonic", ReviewClock().tick
            )
            bounded.setattr(  # sarj-noqa: SARJ445 -- Shrink only the documented review time budget so every pass exercises partial-page continuation.
                cleanup, "REVIEW_SECONDS", 0.5 + seed % 3 * 0.1
            )
            decisions = cleanup.reap(paths, apply=True, batch=batch, progress=progress)
        assert progress.examined > 0
        visited = {entry.path for entry in batch.entries[: progress.examined]}
        assert seen.isdisjoint(visited)
        seen.update(visited)
        assert not any(row.attempted or row.removed for row in decisions)
        checkpoint = cleanup.checkpoint_review(paths, batch, progress)
        cleanup_state.save_inventory_cursor(state, paths.code, checkpoint.cursor)
        if progress.examined < len(batch.entries):
            assert checkpoint.pending
            assert checkpoint.cursor is not None
            assert checkpoint.cursor.worktree == batch.entries[progress.examined - 1].path
        if not checkpoint.pending:
            break
    assert seen == expected
    assert not (state / "cleanup-inventory-cursor.json").exists()
    assert (
        (state / "cleanup-cursor.json").read_bytes() == legacy_bytes
    )  # sarj-noqa: SARJ402 -- Paged reviews must leave the legacy private checkpoint byte-for-byte unchanged.
    assert all((checkout / "source.txt").is_file() for checkout in expected)


def test_zero_review_budget_preserves_the_existing_inventory_checkpoint(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    main = repository(paths.code / "org/repo")
    for name in ("first", "second", "third"):
        cleanup.git(main, "worktree", "add", "-q", "--detach", str(paths.code / f"worktrees/{name}"), "HEAD")
    state = cleanup.state_directory(paths)
    first = cleanup.candidate_page(paths).entries[0]
    cursor = cleanup_state.InventoryCursor(repository=main, worktree=first.path)
    cleanup_state.save_inventory_cursor(state, paths.code, cursor)
    before = (state / "cleanup-inventory-cursor.json").read_bytes()
    batch = cleanup.candidate_page(paths)
    with monkeypatch.context() as bounded:
        bounded.setattr(  # sarj-noqa: SARJ445 -- Zero is an injected documented review budget; no positive eligibility result is fabricated.
            cleanup, "REVIEW_SECONDS", 0
        )
        progress = cleanup.ReviewProgress()
        decisions = cleanup.reap(paths, apply=True, batch=batch, progress=progress)
    assert progress.examined == 0
    assert all(row.reason == cleanup.DEFERRED for row in decisions)
    checkpoint = cleanup.checkpoint_review(paths, batch, progress)
    assert checkpoint.pending
    assert checkpoint.cursor == cursor
    cleanup_state.save_inventory_cursor(state, paths.code, checkpoint.cursor)
    assert (
        (state / "cleanup-inventory-cursor.json").read_bytes() == before
    )  # sarj-noqa: SARJ402 -- A zero review budget must preserve the exact private checkpoint bytes.


def test_deferred_sweep_is_blocked_and_retries_without_claiming_retention() -> None:
    report = cleanup_daemon.WorkerReport.model_validate_json(worker_payload())
    report.worktrees[0].reason = cleanup.DEFERRED
    report.inventory_pending = False
    summary = cleanup_daemon.summarize(report)
    schedule = cleanup_daemon.Schedule.new()
    assert schedule.completed(summary, cleanup_daemon.Space(total=100, available=99), 100, 1000) == "blocked"
    assert schedule.due == 100 + cleanup_daemon.PRESSURE_SECONDS
    assert not cleanup_daemon.baseline_learning_eligible(summary)


@pytest.mark.parametrize("deferred", [False, True], ids=["complete-page", "deferred-page"])
def test_live_heartbeat_never_masks_an_incomplete_inspection(
    paths: RuntimePaths, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], deferred: bool
) -> None:
    monkeypatch.setenv("HOME", str(paths.home))
    report = cleanup_daemon.WorkerReport.model_validate_json(worker_payload())
    if deferred:
        report.worktrees[0].reason = cleanup.DEFERRED
    outcome = cleanup_daemon.summarize(report)
    schedule = cleanup_daemon.Schedule.new()
    space = cleanup_daemon.Space(total=100, available=99)
    schedule.completed(outcome, space, time.monotonic(), time.time())
    cleanup_daemon.publish(cleanup.state_directory(paths), schedule, space, "running")
    cleanup_daemon.status()
    status = RootModel[dict[str, JsonValue]].model_validate_json(capsys.readouterr().out).root
    assert status["fresh"] is True
    assert status["daemon_alive"] is True
    assert status["inspection_blocked"] is deferred
    assert status["cleanup_healthy"] is not deferred
