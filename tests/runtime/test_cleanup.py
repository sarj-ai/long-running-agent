from __future__ import annotations

import ast
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import gzip
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING, TypedDict, Unpack
from uuid import UUID

from long_running_agent.process import CommandError, run
from long_running_agent.runtime import cleanup, cleanup_activity, cleanup_daemon, cleanup_inspection, cleanup_state
from long_running_agent.runtime.health import Pressure
from long_running_agent.runtime.settings import RuntimePaths, private_write
import pytest
from typer.testing import CliRunner


if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence
    from contextlib import AbstractContextManager


class NativeOptions(TypedDict, total=False):
    capture_output: bool
    check: bool
    timeout: float | None
    input_text: str | None
    cwd: Path | None
    env: Mapping[str, str] | None


@dataclass(frozen=True)
class GitInvocation:
    directory: Path
    arguments: tuple[str, ...]


@dataclass(frozen=True)
class CacheEvidence:
    command: Path
    processes: Path


@dataclass(frozen=True)
class Repository:
    paths: RuntimePaths
    main: Path
    now: float

    def linked(self, name: str = "old") -> cleanup.Worktree:
        path = self.paths.code / "worktrees" / name
        cleanup.git(self.main, "worktree", "add", "-b", name, str(path))
        return next(entry for entry in cleanup.inventory(self.paths.code) if entry.path == path)


@dataclass
class SweepClock:
    elapsed: float = 0.0
    expired: bool = False

    def current(self) -> float:
        return self.elapsed


def expire_before_ancestry(
    entry: cleanup.Worktree,
    monkeypatch: pytest.MonkeyPatch,
    *,
    seconds: float = cleanup.REVIEW_SECONDS + 1,
    after: int = 0,
) -> SweepClock:
    clock = SweepClock()
    native = cleanup.git
    matched = 0

    def bounded_native(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal matched
        if arguments[:3] == ("merge-base", "--is-ancestor", entry.head):
            if not clock.expired and matched == after:
                clock.elapsed += seconds
                clock.expired = True
            matched += 1
        return native(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The monotonic OS clock controls a real-Git sweep without sleeping or replacing an eligibility decision.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance only the OS clock at a native ancestry dispatch boundary; every admitted Git command still executes normally.
        cleanup, "git", bounded_native
    )
    return clock


def paged_candidates(repository: Repository) -> list[cleanup.Worktree]:
    first = repository.linked("page-a")
    second = repository.linked("page-b")
    late = repository.linked("page-c")
    commit_fixture_file(first.path, "unique-a.txt")
    commit_fixture_file(second.path, "unique-b.txt")
    for number, entry in enumerate((first, second, late)):
        os.utime(entry.path, (1000000 + number, 1000000 + number))
    return cleanup.inventory(repository.paths.code)


def commit_fixture_file(path: Path, name: str) -> str:
    (path / name).write_text("native fixture commit\n")
    cleanup.git(path, "add", name)
    cleanup.git(
        path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "native fixture commit",
    )
    return cleanup.git(path, "rev-parse", "HEAD").strip()


def detached_checkout(repository: Repository, name: str = "detached") -> cleanup.Worktree:
    initial = repository.linked(name)
    cleanup.git(initial.path, "checkout", "--quiet", "--detach")
    return next(entry for entry in cleanup.inventory(repository.paths.code) if entry.path == initial.path)


def publish_nested_cache_rules(repository: Repository) -> None:
    ignore = repository.main / ".gitignore"
    ignore.write_text(ignore.read_text() + "**/__pycache__/\n**/.pytest_cache/\n")
    for relative in ("python/pkg/source.py", "tools/clouddeploy/src/main.py"):
        source = repository.main / relative
        source.parent.mkdir(parents=True)
        source.write_text("# tracked native fixture source\n")
    cleanup.git(repository.main, "add", ".")
    cleanup.git(
        repository.main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "nested cache fixture",
    )
    cleanup.git(repository.main, "push", "-q", "origin", "main")
    cleanup.git(repository.main, "fetch", "-q", "origin")
    os.utime(repository.main / ".git/FETCH_HEAD", (repository.now, repository.now))


def no_references(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
    return cleanup.References(frozenset())


def observe_directory_scans(directory: Path, monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    native = os.scandir
    observed: list[Path] = []

    def native_scan(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes] = ".",
    ) -> AbstractContextManager[Iterator[os.DirEntry[str] | os.DirEntry[bytes]]]:
        if path == directory:
            observed.append(directory)
        return native(path)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count delegated native directory opens without replacing traversal, entries or any cleanup decision.
        os, "scandir", native_scan
    )
    return observed


def isolate_native_git(repository: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    def current_time() -> float:
        return repository.now

    def idle_kernel() -> bool:
        return True

    def deterministic_clock(cls: type[cleanup_state.AttemptStamp]) -> cleanup_state.AttemptStamp:
        return cls(wall=repository.now, uptime=1000.0, boot_id=UUID(int=1))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native Git fixtures must not inspect the host's unrelated terminal/process inventories.
        cleanup, "references", no_references
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advancing the OS clock models aging real Git ctime, which fixtures cannot backdate.
        time, "time", current_time
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Kernel PSI is a native boundary; fixture deletion must not depend on unrelated host pressure.
        cleanup, "maintenance_idle", idle_kernel
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Linux boot identity/boottime is an OS boundary; portable real-Git fixtures use one stable boot and time.
        cleanup_state.AttemptStamp, "current", classmethod(deterministic_clock)
    )


def native_cache(repository: Repository, monkeypatch: pytest.MonkeyPatch) -> CacheEvidence:
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setenv("HOME", str(repository.paths.home))
    installations = repository.paths.home / ".local/share/mise/installs/node"
    current = installations / "fixture"
    node = current / "bin/node"
    node.parent.mkdir(parents=True)
    node.write_text('#!/bin/sh\nprintf \'%s\\n\' "$@" > "$HOME/cache-native-arguments"\n')
    node.chmod(0o700)
    package = current / "lib/node_modules/npm"
    package.mkdir(parents=True)
    (package / "package.json").write_text('{"name":"npm"}\n')
    (package / "bin").mkdir()
    (package / "bin/npm-cli.js").write_text("// native fixture entrypoint\n")
    (installations / "global").symlink_to(current, target_is_directory=True)
    evidence = CacheEvidence(
        repository.paths.home / "cache-native-arguments", repository.paths.home / "cache-process-inspected"
    )

    def native_process_boundary() -> cleanup.References:
        evidence.processes.write_text("fixture process inventory inspected\n")
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Isolate the native process census while the actual cache code launches only the private fixture executable, never the host package store.
        cleanup, "process_references", native_process_boundary
    )
    return evidence


@pytest.fixture
def repository(tmp_path: Path) -> Repository:
    paths = RuntimePaths(tmp_path / "home")
    main = paths.code / "project"
    main.mkdir(parents=True)
    origin = tmp_path / "origin.git"
    run(["git", "init", "-q", "--bare", str(origin)], capture_output=True)
    cleanup.git(main, "init", "-q", "-b", "main")
    (main / "source.txt").write_text("committed\n")
    (main / ".gitignore").write_text(".env\nnode_modules/\n.venv/\n")
    cleanup.git(main, "add", ".")
    cleanup.git(
        main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "fixture",
    )
    cleanup.git(main, "remote", "add", "origin", str(origin))
    cleanup.git(main, "push", "-qu", "origin", "main")
    cleanup.git(main, "fetch", "-q", "origin")
    cleanup.git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    now = time.time() + 8 * cleanup.DAY
    fetch = main / ".git/FETCH_HEAD"
    os.utime(fetch, (now, now))
    return Repository(paths, main, now)


def eligible(repository: Repository, entry: cleanup.Worktree, protected: cleanup.References | None = None) -> str:
    return cleanup.eligibility(
        entry, repository.paths.code, protected or cleanup.References(frozenset()), min_age_days=7, now=repository.now
    )


def full_filesystem(_path: str | Path) -> os.statvfs_result:
    return os.statvfs_result((4096, 4096, 1000, 150, 150, 1000, 900, 900, 0, 255))


def test_primary_group_writable_checkout_and_object_store_are_safe_beneath_a_private_ancestor(
    repository: Repository,
) -> None:
    entry = repository.linked()
    repository.paths.code.chmod(0o700)
    entry.path.chmod(0o770)
    common = repository.main / ".git"
    common.chmod(0o775)
    (common / "objects").chmod(0o775)
    assert cleanup.owned_path(entry.path, repository.paths.code)
    assert cleanup.object_storage(common) == "eligible"
    assert eligible(repository, entry) == "eligible"


def test_private_ancestor_never_exempts_world_writable_worktrees(repository: Repository) -> None:
    entry = repository.linked()
    repository.paths.code.chmod(0o700)
    entry.path.chmod(0o777)
    assert not cleanup.owned_path(entry.path, repository.paths.code)
    assert "writable" in eligible(repository, entry)


def test_group_write_requires_the_current_primary_group(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    repository.paths.code.chmod(0o700)
    entry.path.chmod(0o770)
    actual = os.getgid()

    def different_group() -> int:
        return actual + 1

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model a different native primary-group identity without changing fixture ownership or real credentials.
        os, "getgid", different_group
    )
    assert not cleanup.owned_path(entry.path, repository.paths.code)


def test_capacity_mode_decouples_two_per_sweep_from_the_persisted_daily_budget(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [
        repository.linked(name) for name in ("recovery-a", "recovery-b", "recovery-c", "recovery-d", "recovery-e")
    ]
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply deterministic kernel filesystem counters; never fill or delete the host filesystem.
        os, "statvfs", full_filesystem
    )
    assert sum(row.removed for row in cleanup.reap(repository.paths, apply=True, capacity_recovery=True)) == 2
    state = cleanup.state_directory(repository.paths)
    before = (state / "cleanup-attempts.json").read_bytes(), (state / "cleanup-attempts-initialized").read_bytes()
    normal = cleanup.reap(repository.paths, apply=True)
    assert all("24-hour" in row.reason and not row.attempted for row in normal)
    assert (
        (state / "cleanup-attempts.json").read_bytes(),
        (state / "cleanup-attempts-initialized").read_bytes(),
    ) == before
    assert sum(row.removed for row in cleanup.reap(repository.paths, apply=True, capacity_recovery=True)) == 2
    receipts = cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts
    assert len(receipts) == 4
    assert len(cleanup_state.AttemptHistory.model_validate_json(before[0]).root) == 2
    assert receipts[:2] == tuple(cleanup_state.AttemptHistory.model_validate_json(before[0]).root)
    assert entries[-1].path.is_dir()
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


def test_capacity_mode_uses_six_hour_retention_and_stops_when_headroom_recovers(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(name) for name in ("recover-first", "recover-second")]
    recent = Repository(repository.paths, repository.main, time.time() + cleanup.DAY / 2)
    os.utime(repository.main / ".git/FETCH_HEAD", (recent.now, recent.now))
    isolate_native_git(recent, monkeypatch)

    def capacity_after_removal(_path: str | Path) -> os.statvfs_result:
        available = 150 if entries[0].path.exists() else 250
        return os.statvfs_result((4096, 4096, 1000, available, available, 1000, 900, 900, 0, 255))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Real native worktree removal changes the modeled kernel capacity; only this fixture's first deletion restores headroom.
        os, "statvfs", capacity_after_removal
    )
    normal = cleanup.reap(recent.paths, apply=True)
    assert all("too recent" in row.reason and not row.attempted for row in normal)
    report = cleanup.reap(recent.paths, apply=True, capacity_recovery=True)
    assert sum(row.removed for row in report) == 1
    assert entries[1].path.is_dir()
    assert "free-space target" in report[1].reason
    cursor = cleanup_state.review_cursor(cleanup.state_directory(recent.paths), recent.paths.code)
    assert cursor is not None
    assert cursor.path == entries[0].path


@pytest.mark.parametrize("offset", [-1, 1], ids=["just-under-six-hours", "just-over-six-hours"])
def test_capacity_recovery_respects_the_native_six_hour_activity_boundary(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    entry = repository.linked()
    recent = Repository(repository.paths, repository.main, time.time() + cleanup.DAY / 4 + offset)
    os.utime(repository.main / ".git/FETCH_HEAD", (recent.now, recent.now))
    isolate_native_git(recent, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model capacity pressure with native filesystem counters, without consuming or deleting host disk space.
        os, "statvfs", full_filesystem
    )
    report = cleanup.reap(recent.paths, apply=True, capacity_recovery=True)
    assert len(report) == 1
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    if offset < 0:
        assert "too recent" in report[0].reason
        assert not report[0].attempted
        assert entry.path.is_dir()
    else:
        assert report[0].removed
        assert report[0].attempted
        assert not entry.path.exists()


def test_capacity_flag_below_recovery_floor_preserves_normal_seven_day_retention(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    recent = Repository(repository.paths, repository.main, time.time() + cleanup.DAY / 2)
    os.utime(repository.main / ".git/FETCH_HEAD", (recent.now, recent.now))
    isolate_native_git(recent, monkeypatch)

    def recovered_filesystem(_path: str | Path) -> os.statvfs_result:
        return os.statvfs_result((4096, 4096, 1000, 250, 250, 1000, 900, 900, 0, 255))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native filesystem counters prove recovery is no longer admitted; the flag cannot reduce ordinary retention.
        os, "statvfs", recovered_filesystem
    )
    report = cleanup.reap(recent.paths, apply=True, capacity_recovery=True)
    assert report == [cleanup.Decision(str(entry.path), "worktree checkout is too recent")]
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize("activity", ["index", "future-root"], ids=["fresh-index", "future-checkout"])
def test_short_capacity_retention_never_ignores_new_or_future_native_activity(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    entry = repository.linked()
    recent = Repository(repository.paths, repository.main, time.time() + cleanup.DAY / 2)
    os.utime(repository.main / ".git/FETCH_HEAD", (recent.now, recent.now))
    if activity == "index":
        index = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip()) / "index"
        os.utime(index, (recent.now, recent.now))
    else:
        os.utime(entry.path, (recent.now + cleanup.DAY, recent.now + cleanup.DAY))
    isolate_native_git(recent, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply current capacity pressure while native checkout/index timestamps remain the activity evidence under test.
        os, "statvfs", full_filesystem
    )
    report = cleanup.reap(recent.paths, apply=True, capacity_recovery=True)
    assert len(report) == 1
    assert "too recent" in report[0].reason
    assert not report[0].attempted
    assert not report[0].removed
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("coverage", ["exact", "descendant"], ids=["branch-at-head", "branch-contains-head"])
def test_clean_merged_detached_checkout_is_removed_only_with_a_preserved_local_branch(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, coverage: str
) -> None:
    entry = detached_checkout(repository)
    assert entry.detached
    assert not entry.branch
    assert not entry.blocked
    if coverage == "descendant":
        descendant = commit_fixture_file(repository.main, "later.txt")
        cleanup.git(repository.main, "update-ref", "refs/heads/detached", descendant)
        assert descendant != entry.head
    branches = cleanup.git(repository.main, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/")
    assert cleanup.preserved_detached(entry)
    original = cleanup.git
    removals: list[tuple[str, ...]] = []

    def native_removal(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[:2] == ("worktree", "remove"):
            assert "--force" not in arguments
            removals.append(arguments)
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe the real native nonforce removal command while retaining all actual detached, branch and merge inspections.
        cleanup, "git", native_removal
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [
        cleanup.Decision(
            str(entry.path), "removed clean merged worktree; branch retained", removed=True, attempted=True
        )
    ]
    assert len(removals) == 1
    assert not entry.path.exists()
    assert original(repository.main, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads/") == branches
    receipts = cleanup_state.active_receipts(
        cleanup.state_directory(repository.paths), cleanup_state.AttemptStamp.current()
    ).receipts
    assert len(receipts) == 1
    assert receipts[0].path == entry.path
    assert receipts[0].head == entry.head


def test_detached_checkout_with_only_remote_and_tag_coverage_is_retained(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = detached_checkout(repository)
    unrelated = cleanup.git(
        repository.main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        entry.head + "^{tree}",
        "-m",
        "independent fixture root",
    ).strip()
    for branch in ("refs/heads/main", "refs/heads/detached"):
        cleanup.git(repository.main, "update-ref", branch, unrelated)
    cleanup.git(repository.main, "tag", "remote-only", entry.head)
    assert cleanup.git(repository.main, "rev-parse", "refs/remotes/origin/main").strip() == entry.head
    assert cleanup.git(repository.main, "rev-parse", "refs/tags/remote-only").strip() == entry.head
    assert not cleanup.preserved_detached(entry)
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [cleanup.Decision(str(entry.path), "detached HEAD has no verified preserving local branch")]
    assert entry.path.is_dir()
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize("blocker", ["locked", "tracked", "env", "live", "unique"])
def test_explicit_detached_eligibility_preserves_all_local_and_live_guards(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    entry = detached_checkout(repository)
    match blocker:
        case "locked":
            cleanup.git(repository.main, "worktree", "lock", str(entry.path))
        case "tracked":
            (entry.path / "source.txt").write_text("unsaved native fixture\n")
        case "env":
            (entry.path / ".env").write_text("PRIVATE_FIXTURE=retain\n")
        case "unique":
            unique = commit_fixture_file(entry.path, "unpublished.txt")
            cleanup.git(repository.main, "update-ref", "refs/heads/detached", unique)
        case "live":
            pass
        case _:
            pytest.fail("Unknown detached checkout guard")
    isolate_native_git(repository, monkeypatch)
    if blocker == "live":

        def native_live_reference(
            _paths: RuntimePaths, _candidates: Sequence[Path] | None = None
        ) -> cleanup.References:
            return cleanup.References(frozenset({entry.path / "source.txt"}))

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A live native process reference must veto an otherwise merged, branch-preserved detached checkout.
            cleanup, "references", native_live_reference
        )
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert report[0].reason != "eligible"
    assert not report[0].attempted
    assert not report[0].removed
    assert entry.path.is_dir()
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize("mutation", ["branches", "head"], ids=["branch-coverage-lost", "detached-head-changed"])
def test_detached_native_removal_rechecks_head_and_local_branch_coverage(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    entry = detached_checkout(repository)
    original = cleanup.git
    changed = False

    def change_after_review(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal changed
        output = original(path, *arguments, timeout=timeout)
        if not changed and path == entry.path and "--ignored=matching" in arguments:
            changed = True
            match mutation:
                case "branches":
                    for branch in ("refs/heads/main", "refs/heads/detached"):
                        original(repository.main, "update-ref", "-d", branch)
                case "head":
                    commit_fixture_file(entry.path, "late-unpublished.txt")
                case _:
                    pytest.fail("Unknown detached apply-boundary mutation")
        return output

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Mutate real native refs or HEAD after initial status; final uncached admission must reject the changed checkout.
        cleanup, "git", change_after_review
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert changed
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert entry.path.is_dir()
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize("mutation", ["head", "assume-unchanged", "skip-worktree", "sequencer", "public-root"])
def test_post_status_checkout_mutations_retain_without_charging(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    entry = repository.linked("post-status-closure")
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    assert cleanup_state.reserve_attempt(state, cleanup_state.AttemptStamp.current(), state / "previous", entry.head)
    budget_files = [state / name for name in ("cleanup-attempts.json", "cleanup-attempts-initialized")]
    budget_before = [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in budget_files]
    identity = entry.path.stat().st_dev, entry.path.stat().st_ino
    original = cleanup.git
    changed = False
    payload = entry.path / "source.txt"
    head_after = entry.head
    blob_after = ""
    refs_after = ""

    def change_after_status(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal changed, payload, head_after, blob_after, refs_after
        output = original(path, *arguments, timeout=timeout)
        if not changed and path == entry.path and "--ignored=matching" in arguments:
            changed = True
            match mutation:
                case "head":
                    commit_fixture_file(entry.path, "late-unpublished.txt")
                    payload = entry.path / "late-unpublished.txt"
                case "assume-unchanged" | "skip-worktree":
                    original(entry.path, "update-index", "--" + mutation, "--", "source.txt")
                    payload.write_text("unsaved tracked content created after native status\n")
                case "sequencer":
                    git_directory = Path(original(entry.path, "rev-parse", "--absolute-git-dir").strip())
                    sequencer = git_directory / "sequencer"
                    sequencer.mkdir()
                    payload = sequencer / "todo"
                    payload.write_text(f"pick {entry.head} retain native operation\n")
                case "public-root":
                    entry.path.chmod(0o702)
                case _:
                    pytest.fail("Unknown post-status checkout mutation")
            head_after = original(entry.path, "rev-parse", "HEAD").strip()
            blob_after = original(entry.path, "hash-object", "--", str(payload)).strip()
            refs_after = original(repository.main, "for-each-ref", "--format=%(refname) %(objectname)")
        return output

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Mutate real fixture HEAD/index/operation/permissions only after native status returns; all guards and removal remain native.
        cleanup, "git", change_after_status
    )
    page = cleanup.reap(repository.paths, apply=True)

    assert changed
    reasons = {
        "head": "HEAD or branch changed since inventory",
        "assume-unchanged": "index flags could hide local changes",
        "skip-worktree": "index flags could hide local changes",
        "sequencer": "Git operation or index update is in progress",
        "public-root": "path is missing, linked, foreign-owned, or writable by another user",
    }
    assert page == [cleanup.Decision(str(entry.path), reasons[mutation])]
    assert (entry.path.stat().st_dev, entry.path.stat().st_ino) == identity
    fresh = next(item for item in cleanup.inventory(repository.paths.code) if item.path == entry.path)
    assert (fresh.path, fresh.head, fresh.branch) == (entry.path, head_after, entry.branch)
    assert original(repository.main, "rev-parse", entry.branch).strip() == head_after
    assert original(repository.main, "for-each-ref", "--format=%(refname) %(objectname)") == refs_after
    assert blob_after
    assert original(entry.path, "hash-object", "--", str(payload)).strip() == blob_after
    assert [(path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns) for path in budget_files] == budget_before


@pytest.mark.parametrize(
    "diagnostic",
    [
        pytest.param("stderr", id="success-with-object-diagnostic"),
        pytest.param("invalid-ref", id="invalid-local-reference"),
        pytest.param("extra-row", id="ambiguous-coverage-rows"),
    ],
)
def test_detached_branch_coverage_rejects_ambiguous_native_results(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, diagnostic: str
) -> None:
    entry = detached_checkout(repository)
    original = run

    def native_result(arguments: Sequence[str], **options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        result = original(arguments, **options)
        if "for-each-ref" not in arguments:
            return result
        assert result.returncode == 0
        assert result.stdout
        match diagnostic:
            case "stderr":
                return subprocess.CompletedProcess(arguments, 0, result.stdout, "error: incomplete fixture objects\n")
            case "invalid-ref":
                return subprocess.CompletedProcess(arguments, 0, "refs/heads/../invalid\0" + entry.head + "\n", "")
            case "extra-row":
                return subprocess.CompletedProcess(arguments, 0, result.stdout + result.stdout, "")
            case _:
                pytest.fail("Unknown native branch coverage diagnostic")

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Alter only the native command result after real Git runs, exercising stderr and schema rejection without replacing branch-coverage logic.
        cleanup, "run", native_result
    )
    assert not cleanup.preserved_detached(entry)
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert entry.path.is_dir()


def test_detached_branch_coverage_never_accepts_missing_native_objects(repository: Repository) -> None:
    entry = detached_checkout(repository)
    (repository.main / ".git/objects" / entry.head[:2] / entry.head[2:]).unlink()
    with pytest.raises(CommandError, match="git exited with status"):
        cleanup.preserved_detached(entry)
    assert entry.path.is_dir()


def test_successful_native_ancestry_with_object_diagnostics_cannot_authorize_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    original = run

    def native_result(arguments: Sequence[str], **options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        result = original(arguments, **options)
        if "merge-base" in arguments:
            assert result.returncode == 0
            return subprocess.CompletedProcess(arguments, 0, result.stdout, "error: partial fixture object graph\n")
        return result

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve real native ancestry execution, then expose an object diagnostic that a zero return code alone must not authorize.
        cleanup, "run", native_result
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [cleanup.Decision(str(entry.path), "inspection failed; retained")]
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize(
    "field",
    [
        pytest.param("HEAD", id="duplicate-head"),
        pytest.param("unknown", id="unknown-field"),
        pytest.param("branch", id="detached-with-branch"),
        pytest.param("prunable", id="prunable-state"),
    ],
)
def test_detached_native_inventory_keeps_ambiguous_and_prunable_states_blocked(
    repository: Repository, field: str
) -> None:
    entry = detached_checkout(repository)
    native = cleanup.git(repository.main, "worktree", "list", "--porcelain", "-z")
    record = next(record for record in native.split("\0\0") if record.startswith(f"worktree {entry.path!s}\0"))
    value = "refs/heads/main" if field == "branch" else entry.head
    records = cleanup.parse_worktrees(f"{record}\0{field} {value}\0\0", repository.main)
    assert len(records) == 1
    assert records[0].blocked
    assert records[0].path == entry.path
    assert entry.path.is_dir()


def test_default_branch_refresh_is_native_and_does_not_fetch_other_branches(repository: Repository) -> None:
    entry = repository.linked()
    origin = Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip())
    cleanup.git(origin, "update-ref", "refs/heads/unrequested", entry.head)
    cleanup.git(repository.main, "update-ref", "refs/remotes/origin/preserved-stale", entry.head)
    cleanup.git(repository.main, "tag", "preserved-tag")
    for name in ("fetch.prune", "remote.origin.prune", "fetch.pruneTags", "fetch.writeCommitGraph"):
        cleanup.git(repository.main, "config", name, "true")
    fetch = repository.main / ".git/FETCH_HEAD"
    stale = time.time() - 2 * cleanup.DAY
    os.utime(fetch, (stale, stale))
    assert cleanup.merged_status(entry, now=time.time()) == cleanup.STALE_REMOTE
    assert cleanup.refresh_default(entry)
    assert "branch 'main' of " in fetch.read_text()
    assert "unrequested" not in cleanup.git(repository.main, "for-each-ref", "refs/remotes/origin/")
    assert cleanup.git(repository.main, "rev-parse", "refs/remotes/origin/preserved-stale").strip() == entry.head
    assert cleanup.git(repository.main, "rev-parse", "refs/tags/preserved-tag").strip() == entry.head
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_fresh_feature_fetch_does_not_prevent_guarded_default_branch_refresh(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    origin = Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip())
    cleanup.git(origin, "update-ref", "refs/heads/feature-only", entry.head)
    cleanup.git(repository.main, "fetch", "origin", "refs/heads/feature-only")
    fetch = repository.main / ".git/FETCH_HEAD"
    os.utime(fetch, (repository.now, repository.now))
    assert cleanup.merged_status(entry, now=repository.now) == cleanup.MISSING_DEFAULT_FETCH
    original = cleanup.git
    fetched: list[tuple[str, ...]] = []

    def native_fetch_clock(path: Path, *arguments: str, timeout: float = 15) -> str:
        output = original(path, *arguments, timeout=timeout)
        if arguments[0] == "fetch":
            fetched.append(arguments)
            os.utime(fetch, (repository.now, repository.now))
        return output

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve real native Git fetch/removal; align the fetched file's OS timestamp with the aged fixture's clock.
        cleanup, "git", native_fetch_clock
    )
    result = cleanup.reap(repository.paths, apply=True)
    assert len(fetched) == 1
    assert fetched[0][-1] == "refs/heads/main:refs/remotes/origin/main"
    assert "branch 'main' of " in fetch.read_text()
    assert result == [
        cleanup.Decision(
            str(entry.path), "removed clean merged worktree; branch retained", removed=True, attempted=True
        )
    ]
    assert original(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("proof", ["stale", "missing-default"], ids=["stale-fetch", "feature-only-fetch"])
@pytest.mark.parametrize("guard", ["object-alias", "operation"], ids=["aliased-objects", "active-sequencer"])
def test_cached_negative_proof_cannot_authorize_fetch_before_checkout_guards(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, proof: str, guard: str
) -> None:
    entry = repository.linked("guarded-fetch")
    fetch = repository.main / ".git/FETCH_HEAD"
    if proof == "missing-default":
        cleanup.git(
            Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip()),
            "update-ref",
            "refs/heads/feature-only",
            entry.head,
        )
        cleanup.git(repository.main, "fetch", "origin", "refs/heads/feature-only")
        os.utime(fetch, (repository.now, repository.now))
        expected_proof = cleanup.MISSING_DEFAULT_FETCH
    else:
        stale = repository.now - 2 * cleanup.DAY
        os.utime(fetch, (stale, stale))
        expected_proof = cleanup.STALE_REMOTE
    objects = repository.main / ".git/objects"
    identity = objects.stat().st_dev, objects.stat().st_ino
    match guard:
        case "object-alias":
            destination = entry.path / "node_modules/private-objects"
            destination.parent.mkdir()
            objects.rename(destination)
            objects.symlink_to(destination, target_is_directory=True)
            expected_reason = "Git object storage is aliased, foreign-owned, or writable by another user"
        case "operation":
            sequencer = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip()) / "sequencer"
            sequencer.mkdir()
            (sequencer / "head").write_text(entry.head + "\n")
            (sequencer / "todo").write_text(f"pick {entry.head} fixture\n")
            expected_reason = "Git operation or index update is in progress"
        case _:
            pytest.fail("Unknown native checkout guard")
    assert cleanup.verified_default(entry, now=repository.now) == expected_proof
    before_fetch = fetch.read_bytes(), fetch.stat().st_mtime_ns
    before_refs = cleanup.git(repository.main, "for-each-ref", "--format=%(refname) %(objectname)")
    original = cleanup.git
    fetched: list[tuple[str, ...]] = []

    def observe_native_fetch(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[0] == "fetch":
            fetched.append(arguments)
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe delegated native Git fetch commands against real aliased storage or sequencer metadata; no guard implementation is replaced.
        cleanup, "git", observe_native_fetch
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [cleanup.Decision(str(entry.path), expected_reason)]
    assert not fetched
    assert (fetch.read_bytes(), fetch.stat().st_mtime_ns) == before_fetch
    assert original(repository.main, "for-each-ref", "--format=%(refname) %(objectname)") == before_refs
    assert (objects.stat().st_dev, objects.stat().st_ino) == identity
    assert original(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert entry.path.is_dir()
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()


def test_initial_merge_proof_is_reused_per_repository_and_unmerged_trees_skip_checkout_inspection(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    merged = [repository.linked(name) for name in ("cache-a", "cache-b")]
    unique = repository.linked("cache-unique")
    unique_head = commit_fixture_file(unique.path, "unique.txt")
    origin = Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip())
    cleanup.git(origin, "symbolic-ref", "HEAD", "refs/heads/main")
    second_main = repository.paths.code / "second-project"
    cleanup.git(repository.paths.code, "clone", "-q", "--no-hardlinks", str(origin), str(second_main))
    second_head = commit_fixture_file(second_main, "second-repository.txt")
    cleanup.git(second_main, "push", "-q", "origin", "main")
    cleanup.git(second_main, "fetch", "-q", "origin")
    os.utime(second_main / ".git/FETCH_HEAD", (repository.now, repository.now))
    second = Repository(repository.paths, second_main, repository.now)
    other = second.linked("cache-other")
    original = cleanup.git
    calls: list[GitInvocation] = []

    def native_command_counter(path: Path, *arguments: str, timeout: float = 15) -> str:
        calls.append(GitInvocation(path, arguments))
        if path == unique.path:
            pytest.fail("Known negative ancestry must retain before inspecting the checkout or index")
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count delegated native Git operations across two real repositories; never replace merge, checkout or proof logic.
        cleanup, "git", native_command_counter
    )
    report = cleanup.reap(repository.paths, apply=False)
    reasons = {row.path: row.reason for row in report}
    assert reasons[str(unique.path)] == "commits are not proven merged into origin's default branch"
    assert all(reasons[str(entry.path)] == "eligible" for entry in (*merged, other))
    proof_directories = [
        call.directory for call in calls if call.arguments == ("rev-parse", "--git-path", "FETCH_HEAD")
    ]
    assert proof_directories.count(repository.main) == proof_directories.count(second_main) == 1
    targets = [call.arguments[-1] for call in calls if call.arguments[:2] == ("merge-base", "--is-ancestor")]
    assert targets.count(merged[0].head) == 3
    assert targets.count(second_head) == 1
    assert original(repository.main, "rev-parse", unique.branch).strip() == unique_head
    assert all(not row.attempted and not row.removed for row in report)
    assert unique.path.is_dir()


def test_bounded_apply_resumes_after_the_last_completed_review_and_preserves_budget_prefix(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, late = paged_candidates(repository)
    isolate_native_git(repository, monkeypatch)
    expire_before_ancestry(second, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    assert cleanup_state.reserve_attempt(state, cleanup_state.AttemptStamp.current(), state / "previous", first.head)
    ledger = state / "cleanup-attempts.json"
    marker = state / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    prior_receipts = cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts

    page = cleanup.reap(repository.paths, apply=True)

    assert page[0].path == str(first.path)
    assert "not proven merged" in page[0].reason
    assert all(row.reason == cleanup.DEFERRED for row in page[1:])
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == first.path
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    resumed = cleanup.reap(repository.paths, apply=True)
    assert resumed[0].path == str(second.path)
    assert next(row for row in resumed if row.path == str(late.path)).removed
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == late.path
    assert ledger.read_bytes().startswith(  # sarj-noqa: SARJ402 -- Appending a durable charge must preserve the exact serialized receipt prefix, beyond parsed equivalence.
        before[0].removesuffix(b"]\n") + b","
    )
    assert marker.read_bytes() == before[1]
    receipts = cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts
    assert len(receipts) == 2
    assert receipts[: len(prior_receipts)] == prior_receipts
    assert (receipts[-1].path, receipts[-1].head) == (late.path, late.head)
    assert not late.path.exists()
    for entry in (first, second, late):
        assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("change", ["dirty", "live"])
def test_later_apply_page_rechecks_new_local_changes_and_live_holders(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    first, second, late = paged_candidates(repository)
    isolate_native_git(repository, monkeypatch)
    expire_before_ancestry(second, monkeypatch)
    assert cleanup.reap(repository.paths, apply=True)[1].reason == cleanup.DEFERRED
    if change == "dirty":
        (late.path / "source.txt").write_text("new private work between sweeps\n")
    else:

        def held(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
            return cleanup.References(frozenset({late.path}))

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A native live holder appears between two real-Git pages; no saved positive eligibility can suppress it.
            cleanup, "references", held
        )
    page = cleanup.reap(repository.paths, apply=True)
    result = next(row for row in page if row.path == str(late.path))
    assert ("local files" if change == "dirty" else "referenced") in result.reason
    assert not any(row.attempted or row.removed for row in page)
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()
    assert all(entry.path.is_dir() for entry in (first, second, late))


def test_deleted_cursor_anchor_and_changed_inventory_do_not_restart_an_old_prefix(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, late = paged_candidates(repository)
    isolate_native_git(repository, monkeypatch)
    expire_before_ancestry(second, monkeypatch)
    cleanup.reap(repository.paths, apply=True)
    cleanup.git(repository.main, "worktree", "remove", "--", str(first.path))
    added = repository.linked("page-new-old")
    os.utime(added.path, (999999, 999999))
    os.utime(second.path, (1000003, 1000003))

    resumed = cleanup.reap(repository.paths, apply=True)

    assert resumed[0].path == str(late.path)
    assert resumed[0].removed
    assert next(row for row in resumed if row.path == str(added.path)).removed
    assert next(row for row in resumed if row.path == str(second.path)).reason.startswith("commits are not proven")
    for entry in (first, second, late, added):
        assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("prior_attempts", [0, 255], ids=["two-per-run", "last-of-256-daily"])
def test_slow_final_proof_does_not_spend_selection_time_or_expand_native_attempt_limits(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, prior_attempts: int
) -> None:
    entries = [repository.linked(name) for name in ("slow-proof-a", "slow-proof-b", "slow-proof-c")]
    for number, entry in enumerate(entries):
        os.utime(entry.path, (1000000 + number, 1000000 + number))
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    stamp = cleanup_state.AttemptStamp.current()
    for number in range(prior_attempts):
        assert cleanup_state.reserve_attempt(
            state, stamp, state / f"previous-{number}", entries[0].head, limit=cleanup_state.CAPACITY_ATTEMPTS
        )
    previous = cleanup_state.active_receipts(state, stamp).receipts
    expire_before_ancestry(entries[0], monkeypatch, after=1)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Deterministic kernel free-space counters select actual capacity policy without consuming host disk.
        os, "statvfs", full_filesystem
    )

    page = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)

    allowed = min(2, 256 - prior_attempts)
    assert sum(row.removed for row in page) == sum(row.attempted for row in page) == allowed
    assert all(not entry.path.exists() for entry in entries[:allowed])
    assert all(entry.path.is_dir() for entry in entries[allowed:])
    assert all("retained without further inspection" in row.reason for row in page[allowed:])
    receipts = cleanup_state.active_receipts(state, stamp).receipts
    assert len(receipts) == prior_attempts + allowed
    assert receipts[:prior_attempts] == previous
    assert [receipt.path for receipt in receipts[prior_attempts:]] == [entry.path for entry in entries[:allowed]]
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == entries[allowed - 1].path
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)
    if prior_attempts:
        before = (state / "cleanup-attempts.json").read_bytes()
        refused = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
        assert all("24-hour" in row.reason and not row.attempted for row in refused)
        assert (  # sarj-noqa: SARJ402 -- Refusing a 49th charge must preserve exact durable ledger bytes, not merely an equivalent parsed history.
            (state / "cleanup-attempts.json").read_bytes() == before
        )


@pytest.mark.parametrize("cutoff", ["selection", "overall"])
def test_slow_final_proof_preserves_selection_and_absolute_cutoffs_and_pending_rotation(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, cutoff: str
) -> None:
    first, second, third = [repository.linked(name) for name in ("proof-clock-a", "proof-clock-b", "proof-clock-c")]
    for number, entry in enumerate((first, second, third)):
        os.utime(entry.path, (1000000 + number, 1000000 + number))
    isolate_native_git(repository, monkeypatch)
    clock = SweepClock()
    native = cleanup.git
    advanced: set[Path] = set()

    def slow_native_status(path: Path, *arguments: str, timeout: float = 15) -> str:
        output = native(path, *arguments, timeout=timeout)
        if arguments and arguments[0] == "status" and path not in advanced:
            if path == first.path and arguments[-1] == "--ignored=matching":
                clock.elapsed += cleanup.REVIEW_SECONDS + 1
                advanced.add(path)
            elif path == second.path:
                target = "--ignored=no" if cutoff == "selection" else "--ignored=matching"
                if arguments[-1] == target:
                    clock.elapsed += cleanup.REVIEW_SECONDS if cutoff == "selection" else 89
                    advanced.add(path)
        return output

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model actual elapsed monotonic time at real native Git status boundaries, without replacing proof or removal outcomes.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Every admitted Git status/removal still executes; only the OS clock advances after the chosen native response.
        cleanup, "git", slow_native_status
    )

    page = cleanup.reap(repository.paths, apply=True)

    assert page[0].removed
    assert page[0].attempted
    assert all(row.reason == cleanup.DEFERRED and not row.attempted for row in page[1:])
    assert clock.elapsed == (121 if cutoff == "selection" else 150)
    assert second.path.is_dir()
    assert third.path.is_dir()
    state = cleanup.state_directory(repository.paths)
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == first.path
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 1

    resumed = cleanup.reap(repository.paths, apply=True)

    assert resumed[0].path == str(second.path)
    assert resumed[0].removed
    assert not second.path.exists()
    assert third.path.is_dir()
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == second.path
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 2
    assert all(
        cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
        for entry in (first, second, third)
    )
    assert cleanup.INSPECTION_DEADLINE.get() is None


def test_final_proof_timeout_keeps_the_pending_candidate_before_any_charge(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second, late = paged_candidates(repository)
    isolate_native_git(repository, monkeypatch)
    native = cleanup.git
    clock = SweepClock()
    inventories = 0

    def interrupted_final_inventory(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal inventories
        if arguments[:2] == ("worktree", "list"):
            inventories += 1
            if inventories == 2:
                clock.elapsed = cleanup.FINAL_PROOF_SECONDS
        return native(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model elapsed monotonic time without delaying unrelated work on the shared VM.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The final real-Git inventory reaches the hard inspection cutoff; no review or deletion result is mocked.
        cleanup, "git", interrupted_final_inventory
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert next(row for row in page if row.path == str(late.path)).reason == cleanup.DEFERRED
    state = cleanup.state_directory(repository.paths)
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == second.path
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()
    assert all(entry.path.is_dir() for entry in (first, second, late))


def test_global_live_failure_preserves_actual_removal_and_stops_at_the_pending_candidate(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = [repository.linked(name) for name in ("live-a", "live-b")]
    isolate_native_git(repository, monkeypatch)
    calls = 0

    def changing_native_references(
        _paths: RuntimePaths, _candidates: Sequence[Path] | None = None
    ) -> cleanup.References:
        nonlocal calls
        calls += 1
        if calls == 1:
            return cleanup.References(frozenset())
        message = "Native live inventory became unreadable"
        raise RuntimeError(message)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The live-reference OS boundary fails only after a real native removal has completed.
        cleanup, "references", changing_native_references
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page[0].removed
    assert page[0].attempted
    assert page[1] == cleanup.Decision(str(second.path), "live reference inspection failed; retained")
    assert not first.path.exists()
    assert second.path.is_dir()
    state = cleanup.state_directory(repository.paths)
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == first.path
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 1


def test_cursor_write_failure_does_not_hide_a_completed_native_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = [repository.linked(name) for name in ("persist-a", "persist-b")]
    isolate_native_git(repository, monkeypatch)

    def cursor_full(path: Path, text: str) -> None:
        if path.name == "cleanup-cursor.json":
            message = "Fixture progress storage is full"
            raise OSError(message)
        private_write(path, text)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Fail only the atomic cursor filesystem write after real deletion and durable budget charging.
        cleanup_state, "private_write", cursor_full
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page[0].removed
    assert page[0].attempted
    assert "progress persistence failed" in page[0].reason
    assert "progress persistence failed" in page[1].reason
    assert all("inspection failed" in row.reason for row in page)
    assert not page[1].attempted
    assert not first.path.exists()
    assert second.path.is_dir()
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-cursor.json").exists()
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 1
    preceding = [
        cleanup_daemon.WorktreeResult(
            path=str(repository.paths.code / f"retained-{number}"),
            reason=f"independent retained reason {number}",
            removed=False,
            attempted=False,
        )
        for number in range(cleanup_daemon.MAX_REASONS)
    ]
    actual = [
        cleanup_daemon.WorktreeResult(path=row.path, reason=row.reason, removed=row.removed, attempted=row.attempted)
        for row in page
    ]
    summary = cleanup_daemon.summarize(
        cleanup_daemon.WorkerReport(apply=True, max_removals=2, worktrees=[*preceding, *actual], cache="skipped")
    )
    assert summary.attempted == summary.removed == 1
    assert len(summary.reasons) == cleanup_daemon.MAX_REASONS
    assert all(row.reason in summary.reasons for row in page)
    space = cleanup_daemon.Space(total=1000, available=500)
    assert cleanup_daemon.Schedule.new().completed(summary, space, 1.0, repository.now) == "cleaned"
    retained = cleanup_daemon.summarize(
        cleanup_daemon.WorkerReport(apply=True, max_removals=2, worktrees=[*preceding, actual[-1]], cache="skipped")
    )
    assert retained.attempted == retained.removed == 0
    assert actual[-1].reason in retained.reasons
    assert cleanup_daemon.Schedule.new().completed(retained, space, 1.0, repository.now) == "blocked"


def test_report_time_budget_defers_unchecked_rows_without_reading_or_changing_private_state(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = [repository.linked(name) for name in ("report-a", "report-b")]
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    assert cleanup_state.reserve_attempt(state, cleanup_state.AttemptStamp.current(), state / "previous", first.head)
    private_write(state / "cleanup-cursor.json", "unknown cursor; report must ignore it\n")
    files = [state / name for name in ("cleanup-attempts.json", "cleanup-attempts-initialized", "cleanup-cursor.json")]
    before = [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files]
    expire_before_ancestry(second, monkeypatch, seconds=cleanup.REPORT_SECONDS + 1, after=1)

    page = cleanup.reap(repository.paths, apply=False)

    assert page == [cleanup.Decision(str(first.path), "eligible"), cleanup.Decision(str(second.path), cleanup.DEFERRED)]
    assert [(path.read_bytes(), path.stat().st_mtime_ns, path.stat().st_ino) for path in files] == before
    assert first.path.is_dir()
    assert second.path.is_dir()


@pytest.mark.parametrize("unknown", ["malformed", "outside", "public", "symlink"])
def test_unknown_private_cursor_blocks_apply_without_inspection_or_state_repair(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, unknown: str
) -> None:
    entry = repository.linked("cursor-unknown")
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    cursor = state / "cleanup-cursor.json"
    payload = cleanup_state.ReviewCursor(version=1, modified=1000000.0, path=repository.main).model_dump_json()
    match unknown:
        case "symlink":
            target = state / "original-cursor"
            private_write(target, payload)
            cursor.symlink_to(target)
        case "outside":
            private_write(
                cursor,
                cleanup_state.ReviewCursor(version=1, modified=1000000.0, path=repository.paths.home).model_dump_json(),
            )
        case "malformed":
            private_write(cursor, "invalid cursor\n")
        case "public":
            private_write(cursor, payload)
            cursor.chmod(0o644)
        case _:
            private_write(cursor, payload)
    before = cursor.read_bytes(), cursor.lstat().st_ino, cursor.lstat().st_mode

    def no_live_inspection(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        pytest.fail("Unknown cursor state must retain before any live-holder inspection")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- An unknown private scheduling hint must stop before the native process/reference boundary.
        cleanup, "references", no_live_inspection
    )
    assert cleanup.reap(repository.paths, apply=True) == [
        cleanup.Decision(str(entry.path), "inspection cursor inspection failed; retained")
    ]
    assert (cursor.read_bytes(), cursor.lstat().st_ino, cursor.lstat().st_mode) == before
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_missing_native_checkout_has_a_finite_cursor_key_and_keeps_its_branch(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("missing-cursor-key")
    shutil.rmtree(entry.path)
    isolate_native_git(repository, monkeypatch)
    page = cleanup.reap(repository.paths, apply=True)
    assert page == [cleanup.Decision(str(entry.path), "locked, prunable, or unknown worktree")]
    state = cleanup.state_directory(repository.paths)
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cleanup_state.valid_time(cursor.modified)
    assert cursor.path == entry.path
    assert not (state / "cleanup-attempts.json").exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_native_inspection_uses_monotonic_deadlines_and_clips_an_inflight_command(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = SweepClock()
    wall = [repository.now]

    def wall_clock() -> float:
        return wall[0]

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Deadline behavior must be independent of wall-clock jumps used for retention age.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exercise real wall-clock jumps independently from the monotonic inspection deadline.
        time, "time", wall_clock
    )
    with cleanup.inspection_window(10):
        wall[0] -= 100 * cleanup.DAY
        assert cleanup.git(repository.main, "rev-parse", "HEAD").strip()
        wall[0] += 200 * cleanup.DAY
        assert cleanup.git(repository.main, "rev-parse", "HEAD").strip()
        clock.elapsed = 10
        with pytest.raises(cleanup.InspectionDeferredError):
            cleanup.git(repository.main, "rev-parse", "HEAD")
    monkeypatch.undo()
    with cleanup.inspection_window(time.monotonic() + 0.02), pytest.raises(cleanup.InspectionDeferredError):
        cleanup.inspection_run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=15)
    assert cleanup.git(repository.main, "rev-parse", "HEAD").strip()


def test_native_removal_keeps_its_120_second_timeout_and_resets_inspection_context(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("native-timeout")
    isolate_native_git(repository, monkeypatch)
    clock = SweepClock()
    native = run
    observed: list[float | None] = []

    def late_references(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        clock.elapsed = 100
        return cleanup.References(frozenset())

    def native_dispatch(arguments: Sequence[str], **options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        if "remove" in arguments:
            observed.append(options.get("timeout"))
        return native(arguments, **options)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A slow native reference census leaves only 50 inspection seconds before a real removal.
        cleanup, "references", late_references
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Inspect the subprocess timeout while delegating the actual native Git removal and preserving its result.
        cleanup, "run", native_dispatch
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model elapsed monotonic time without adding a long test sleep.
        time, "monotonic", clock.current
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page[0].removed
    assert observed == [120]
    clock.elapsed = cleanup.FINAL_PROOF_SECONDS + 1
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_late_durable_charge_is_reported_without_launching_native_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("charge-cutoff")
    isolate_native_git(repository, monkeypatch)
    clock = SweepClock()

    def slow_durable_ledger(path: Path, text: str) -> None:
        private_write(path, text)
        if path.name == "cleanup-attempts.json":
            clock.elapsed = cleanup.FINAL_PROOF_SECONDS

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The monotonic OS clock models a slow durable write without waiting 150 seconds.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The actual private ledger write completes at the dispatch cutoff; its real charge must remain visible.
        cleanup_state, "private_write", slow_durable_ledger
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page == [
        cleanup.Decision(
            str(entry.path), "native removal deferred after durable charge; branch retained", attempted=True
        )
    ]
    state = cleanup.state_directory(repository.paths)
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 1
    assert (state / "cleanup-attempts-initialized").read_bytes() == b"1\n"
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("cached", [True, False], ids=["initial-cached-review", "final-uncached-removal"])
def test_verified_fetch_oid_cannot_be_replaced_by_a_moving_remote_ref(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, cached: bool
) -> None:
    initial = repository.linked("moving-proof")
    commit_fixture_file(initial.path, "unique.txt")
    entry = next(item for item in cleanup.inventory(repository.paths.code) if item.path == initial.path)
    fetch = repository.main / ".git/FETCH_HEAD"
    fetch_before = fetch.read_bytes()
    original = cleanup.git
    targets: list[str] = []

    def move_native_ref(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[:2] == ("merge-base", "--is-ancestor"):
            if not targets:
                original(repository.main, "update-ref", "refs/remotes/origin/main", entry.head)
            targets.append(arguments[-1])
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Move only a real fixture remote ref at the native ancestry command boundary, after FETCH_HEAD evidence was read.
        cleanup, "git", move_native_ref
    )
    report = (
        cleanup.reap(repository.paths, apply=True)
        if cached
        else [cleanup.remove_reviewed(entry, repository.paths, min_age_days=7)]
    )
    assert targets == [initial.head]
    assert report == [cleanup.Decision(str(entry.path), "commits are not proven merged into origin's default branch")]
    assert original(repository.main, "rev-parse", "refs/remotes/origin/main").strip() == entry.head
    assert original(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert fetch.read_bytes() == fetch_before
    assert entry.path.is_dir()
    assert not (repository.paths.home / ".local/state/long-running-agent/cleanup-attempts.json").exists()


def test_successful_fetch_invalidates_cached_staleness_even_when_first_checkout_is_unmerged(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = repository.linked("invalidate-a-unique")
    unique_head = commit_fixture_file(first.path, "unique.txt")
    merged = repository.linked("invalidate-b-merged")
    older = time.time() - 60
    os.utime(first.path, (older, older))
    fetch = repository.main / ".git/FETCH_HEAD"
    stale = repository.now - 2 * cleanup.DAY
    os.utime(fetch, (stale, stale))
    original = cleanup.git
    fetched: list[tuple[str, ...]] = []

    def native_fetch_clock(path: Path, *arguments: str, timeout: float = 15) -> str:
        output = original(path, *arguments, timeout=timeout)
        if arguments[0] == "fetch":
            fetched.append(arguments)
            os.utime(fetch, (repository.now, repository.now))
        return output

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve actual native fetch/removal and align only FETCH_HEAD's OS timestamp with the fixture's aged clock.
        cleanup, "git", native_fetch_clock
    )
    report = cleanup.reap(repository.paths, apply=True)
    rows = {row.path: row for row in report}
    assert len(fetched) == 1
    assert fetched[0][-1] == "refs/heads/main:refs/remotes/origin/main"
    assert rows[str(first.path)].reason == "commits are not proven merged into origin's default branch"
    assert not rows[str(first.path)].attempted
    assert rows[str(merged.path)].removed
    assert rows[str(merged.path)].attempted
    assert first.path.is_dir()
    assert not merged.path.exists()
    assert original(repository.main, "rev-parse", first.branch).strip() == unique_head
    assert original(repository.main, "rev-parse", merged.branch).strip() == merged.head
    receipts = cleanup_state.active_receipts(
        cleanup.state_directory(repository.paths), cleanup_state.AttemptStamp.current()
    ).receipts
    assert len(receipts) == 1
    assert receipts[0].path == merged.path
    assert receipts[0].head == merged.head


@pytest.mark.parametrize("invalidate", [False, True], ids=["fresh-native-removal", "changed-fetch-retained"])
def test_final_native_removal_revalidates_fetch_evidence_without_the_initial_cache(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, invalidate: bool
) -> None:
    entry = repository.linked("final-proof")
    origin = Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip())
    cleanup.git(origin, "update-ref", "refs/heads/feature-only", entry.head)
    original = cleanup.git
    calls: list[GitInvocation] = []
    changed = False
    inventories = 0

    def native_fetch_at_final_inventory(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal changed, inventories
        calls.append(GitInvocation(path, arguments))
        output = original(path, *arguments, timeout=timeout)
        if arguments[:2] == ("worktree", "list"):
            inventories += 1
        if invalidate and not changed and inventories == 2:
            original(repository.main, "fetch", "origin", "refs/heads/feature-only")
            os.utime(repository.main / ".git/FETCH_HEAD", (repository.now, repository.now))
            changed = True
        return output

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A real feature fetch replaces FETCH_HEAD at final inventory, after the retaining-only merge prefilter; deletion must rebuild its own proof.
        cleanup, "git", native_fetch_at_final_inventory
    )
    report = cleanup.reap(repository.paths, apply=True)
    proof_reads = [call for call in calls if call.arguments == ("rev-parse", "--git-path", "FETCH_HEAD")]
    assert len(proof_reads) == 2
    assert len(report) == 1
    assert original(repository.main, "rev-parse", entry.branch).strip() == entry.head
    if invalidate:
        assert changed
        assert report[0] == cleanup.Decision(str(entry.path), cleanup.MISSING_DEFAULT_FETCH)
        assert entry.path.is_dir()
        assert not (repository.paths.home / ".local/state/long-running-agent/cleanup-attempts.json").exists()
    else:
        assert report[0].removed
        assert report[0].attempted
        assert not entry.path.exists()
        receipts = cleanup_state.active_receipts(
            cleanup.state_directory(repository.paths), cleanup_state.AttemptStamp.current()
        ).receipts
        assert len(receipts) == 1
        assert receipts[0].path == entry.path
        assert receipts[0].head == entry.head


def test_merge_proof_cache_does_not_survive_between_native_reap_invocations(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = repository.linked("invocation-proof")
    unique_head = commit_fixture_file(initial.path, "unique.txt")
    original = cleanup.git
    calls: list[GitInvocation] = []

    def native_command_counter(path: Path, *arguments: str, timeout: float = 15) -> str:
        calls.append(GitInvocation(path, arguments))
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count real proof reads across separate public reap calls while the fixture's actual remote history advances.
        cleanup, "git", native_command_counter
    )
    first = cleanup.reap(repository.paths, apply=False)
    assert first == [cleanup.Decision(str(initial.path), "commits are not proven merged into origin's default branch")]
    original(initial.path, "push", "-q", "origin", initial.branch + ":refs/heads/main")
    original(repository.main, "fetch", "-q", "origin", "refs/heads/main:refs/remotes/origin/main")
    os.utime(repository.main / ".git/FETCH_HEAD", (repository.now, repository.now))
    second = cleanup.reap(repository.paths, apply=False)
    assert second == [cleanup.Decision(str(initial.path), "eligible")]
    proof_reads = [call for call in calls if call.arguments == ("rev-parse", "--git-path", "FETCH_HEAD")]
    assert len(proof_reads) == 2
    assert initial.path.is_dir()
    assert original(repository.main, "rev-parse", initial.branch).strip() == unique_head
    assert not (repository.paths.home / ".local/state/long-running-agent/cleanup-attempts.json").exists()


@pytest.mark.parametrize("prior_attempts", [0, 1], ids=["per-run-cap", "remaining-daily-cap"])
def test_spent_attempt_allowance_retains_later_rows_without_native_checkout_inspection(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, prior_attempts: int
) -> None:
    entries = [repository.linked(name) for name in ("bounded-a", "bounded-b", "bounded-c", "bounded-d")]
    isolate_native_git(repository, monkeypatch)
    if prior_attempts:
        assert cleanup_state.reserve_attempt(
            cleanup.state_directory(repository.paths),
            cleanup_state.AttemptStamp.current(),
            repository.paths.code / "previous-attempt",
            entries[0].head,
        )
    allowance = cleanup.MAX_REMOVALS - prior_attempts
    original = cleanup.git
    attempted: list[str] = []

    def native_inspection_boundary(path: Path, *arguments: str, timeout: float = 15) -> str:
        if len(attempted) >= allowance and path in {entry.path for entry in entries[allowance:]}:
            pytest.fail("The spent native attempt allowance must retain later rows before checkout inspection")
        output = original(path, *arguments, timeout=timeout)
        if arguments[:2] == ("worktree", "remove"):
            attempted.append(arguments[-1])
        return output

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe native Git commands after real fixture removals; no eligibility or budget implementation is replaced.
        cleanup, "git", native_inspection_boundary
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert sum(row.attempted for row in report) == sum(row.removed for row in report) == allowance
    for row in report[allowance:]:
        assert "retained without further inspection" in row.reason
        assert "eligible" not in row.reason
        assert not row.attempted
    assert all(entry.path.is_dir() for entry in entries[allowance:])
    assert all(original(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


@pytest.mark.parametrize("blocker", ["dirty", "unique", "live"])
def test_capacity_recovery_keeps_local_commits_files_and_live_references(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, blocker: str
) -> None:
    entry = repository.linked()
    if blocker == "dirty":
        (entry.path / ".env").write_text("private local fixture\n")
    elif blocker == "unique":
        (entry.path / "unique.txt").write_text("unmerged fixture\n")
        cleanup.git(entry.path, "add", "unique.txt")
        cleanup.git(
            entry.path,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "unique",
        )
    isolate_native_git(repository, monkeypatch)
    if blocker == "live":

        def live_reference_boundary(
            _paths: RuntimePaths, _candidates: Sequence[Path] | None = None
        ) -> cleanup.References:
            return cleanup.References(frozenset({entry.path / "source.txt"}))

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model a native live process holder while keeping real checkout, merge and persisted-budget checks.
            cleanup, "references", live_reference_boundary
        )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exercise capacity recovery using deterministic kernel counters without consuming host disk space.
        os, "statvfs", full_filesystem
    )
    report = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert report[0].reason != "eligible"
    assert entry.path.is_dir()
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


def test_all_young_apply_retains_without_collecting_unrelated_live_processes(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    observed: list[RuntimePaths] = []

    def host_reference_boundary(paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        observed.append(paths)
        return no_references(paths)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe the host process-inspection boundary; this real Git checkout is too young to be removed regardless of live holders.
        cleanup, "references", host_reference_boundary
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert report[0].path == str(entry.path)
    assert "too recent" in report[0].reason
    assert not report[0].attempted
    assert not observed


def test_capacity_retry_does_not_repeatedly_charge_or_remove_the_same_failed_checkout(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    original = cleanup.git
    attempted: list[str] = []

    def native_refusal(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[:2] == ("worktree", "remove"):
            attempted.append(arguments[-1])
            message = "Owned fixture native refusal"
            raise RuntimeError(message)
        return original(path, *arguments, timeout=timeout)

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Return a real native-command refusal only at Git removal; retain actual registration, branch and receipt files.
        cleanup, "git", native_refusal
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply deterministic kernel capacity without using host free-space or modifying unrelated files.
        os, "statvfs", full_filesystem
    )
    first = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
    assert first[0].attempted
    assert not first[0].removed
    state = cleanup.state_directory(repository.paths)
    before = (state / "cleanup-attempts.json").read_bytes()
    second = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
    assert "already attempted" in second[0].reason
    assert not second[0].attempted
    assert attempted == [str(entry.path)]
    assert (
        (state / "cleanup-attempts.json").read_bytes() == before
    )  # sarj-noqa: SARJ402 -- Exact persisted bytes are the no-rewrite contract for a refused retry, beyond equivalent parsed receipts.
    assert entry.path.is_dir()


def test_native_removal_retains_branch_and_respects_per_run_limit(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(name) for name in ("old-a", "old-b", "old-c")]
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=False)
    assert all(item.reason == "eligible" and not item.removed for item in report)
    assert all(entry.path.exists() for entry in entries)
    report = cleanup.reap(repository.paths, apply=True)
    assert sum(item.removed for item in report) == 2
    assert not entries[0].path.exists()
    assert not entries[1].path.exists()
    assert entries[2].path.exists()
    assert "per-run" in report[2].reason
    assert cleanup.git(repository.main, "rev-parse", "refs/heads/old-a").strip() == entries[0].head
    assert repository.main.exists()


def test_repeated_apply_cannot_exceed_the_persisted_daily_attempt_budget(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(name) for name in ("budget-a", "budget-b", "budget-c")]
    isolate_native_git(repository, monkeypatch)
    assert sum(item.removed for item in cleanup.reap(repository.paths, apply=True)) == 2
    second = cleanup.reap(repository.paths, apply=True)
    assert len(second) == 1
    assert "24-hour" in second[0].reason
    assert not second[0].attempted
    assert not second[0].removed
    assert entries[2].path.is_dir()
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


@pytest.mark.parametrize("apply", [False, True], ids=["report-inspects", "exhausted-apply-retains"])
def test_exhausted_budget_skips_live_inspection_only_for_apply(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, *, apply: bool
) -> None:
    entries = [repository.linked(name) for name in ("budget-held-a", "budget-held-b")]
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    stamp = cleanup_state.AttemptStamp.current()
    for entry in entries:
        assert cleanup_state.reserve_attempt(state, stamp, entry.path, entry.head)
    ledger, marker = state / "cleanup-attempts.json", state / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    inspections: list[RuntimePaths] = []

    def inspect_references(paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        inspections.append(paths)
        return no_references(paths)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count native live-holder boundary calls while real Git inventory and the persisted budget remain intact.
        cleanup, "references", inspect_references
    )
    report = cleanup.reap(repository.paths, apply=apply)
    assert len(report) == len(entries)
    assert len(inspections) == (0 if apply else 1)
    assert all("24-hour" in item.reason if apply else item.reason == "eligible" for item in report)
    assert not any(item.attempted or item.removed for item in report)
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    assert all(entry.path.is_dir() for entry in entries)
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


@pytest.mark.parametrize(
    "failed_name",
    [
        pytest.param("cleanup-attempts.json", id="ledger-enospc-before-charge"),
        pytest.param("cleanup-attempts-initialized", id="marker-enospc-after-charge"),
    ],
)
def test_native_apply_budget_enospc_retains_every_checkout_and_preserves_any_precharges(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, failed_name: str
) -> None:
    entries = [repository.linked(name) for name in ("enospc-a", "enospc-b")]
    assert all(eligible(repository, entry) == "eligible" for entry in entries)
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    failures: list[Path] = []

    def native_budget_disk_full(path: Path, text: str) -> None:
        if path.name == failed_name:
            failures.append(path)
            raise OSError(errno.ENOSPC, "Native fixture budget storage is full")
        private_write(path, text)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Atomic budget-file writes are the native filesystem boundary; ENOSPC must precede actual Git removal without filling any filesystem.
        cleanup_state, "private_write", native_budget_disk_full
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert {item.path for item in report} == {str(entry.path) for entry in entries}
    assert all(item.reason == "inspection failed; retained" for item in report)
    assert not any(item.attempted or item.removed for item in report)
    assert failures == [state / failed_name, state / failed_name]
    assert set(cleanup.inventory(repository.paths.code)) == set(entries)
    assert all(entry.path.is_dir() for entry in entries)
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)
    assert not (state / "cleanup-attempts-initialized").exists()
    ledger = state / "cleanup-attempts.json"
    if failed_name == "cleanup-attempts.json":
        assert not ledger.exists()
    else:
        before = cleanup_state.private_payload(ledger)
        history = cleanup_state.AttemptHistory.model_validate_json(before)
        assert {(receipt.path, receipt.head) for receipt in history.root} == {
            (entry.path, entry.head) for entry in entries
        }
        assert len(history.root) == len(entries)
        repeated = cleanup.reap(repository.paths, apply=True)
        assert all("24-hour" in item.reason for item in repeated)
        assert not any(item.attempted or item.removed for item in repeated)
        assert cleanup_state.private_payload(ledger) == before
        assert len(failures) == len(entries)
        assert not (state / "cleanup-attempts-initialized").exists()


def test_unknown_budget_retains_native_inventory_without_inspection_or_state_repair(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    isolate_native_git(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    ledger, marker = state / "cleanup-attempts.json", state / "cleanup-attempts-initialized"
    private_write(ledger, "invalid native budget\n")
    private_write(marker, "1\n")
    before = ledger.read_bytes(), marker.read_bytes()

    def inspect_references(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        pytest.fail("An unknown budget must retain before native reference inspection")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Fail if the live-holder OS boundary is reached after a corrupt persisted native budget.
        cleanup, "references", inspect_references
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [cleanup.Decision(str(entry.path), "native-removal budget inspection failed; retained")]
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    assert (entry.path / "source.txt").read_text() == "committed\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("apply", [False, True], ids=["report-only", "apply"])
def test_unreadable_live_references_retain_the_entire_native_inventory(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, *, apply: bool
) -> None:
    entries = [repository.linked(name) for name in ("old-a", "old-b", "old-c")]
    assert all(eligible(repository, entry) == "eligible" for entry in entries)
    isolate_native_git(repository, monkeypatch)

    def unreadable_references(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        message = "Native live-holder inspection is unavailable"
        raise RuntimeError(message)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Simulate inaccessible native live-holder references without inspecting unrelated host processes.
        cleanup, "references", unreadable_references
    )
    report = cleanup.reap(repository.paths, apply=apply)
    assert [item.path for item in report] == [str(entry.path) for entry in entries]
    assert all(item.reason == "live reference inspection failed; retained" for item in report)
    assert not any(item.attempted or item.removed for item in report)
    assert cleanup.inventory(repository.paths.code) == entries
    for entry in entries:
        assert (entry.path / "source.txt").read_text() == "committed\n"
        assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_authenticated_probe_fallback_preserves_native_candidate_references(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, second = repository.linked("held"), repository.linked("unused")

    def unavailable_proc() -> cleanup.References:
        raise PermissionError

    def probe(candidates: Sequence[Path]) -> cleanup_inspection.ProbeReply:
        assert set(candidates) == {first.path, second.path}
        return cleanup_inspection.ProbeReply.model_validate(
            {
                "schema": 1,
                "nonce": "a" * 32,
                "uid": os.getuid(),
                "protected": [str(first.path)],
                "package_writer": True,
                "zellij_running": True,
                "process_count": 2,
            }
        )

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Simulate the kernel's nondumpable process restriction before the authenticated IPC fallback.
        cleanup, "process_references", unavailable_proc
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The privileged socket is an external boundary; native worker/client protocol tests cover its authentication separately.
        cleanup_inspection, "inspect", probe
    )
    result = cleanup.inspected_processes(repository.paths)
    assert result.contains(first.path)
    assert not result.contains(second.path)
    assert result.package_writer
    assert "zellij" in result.arguments
    assert "referenced" in eligible(repository, first, result)
    assert eligible(repository, second, result) == "eligible"


def test_missing_privileged_probe_never_authorizes_native_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked("preserve-a"), repository.linked("preserve-b")]

    def aged_clock() -> float:
        return repository.now

    def unavailable_proc() -> cleanup.References:
        raise PermissionError

    def unavailable_probe(_candidates: Sequence[Path]) -> cleanup_inspection.ProbeReply:
        raise cleanup_inspection.ProbeUnavailableError

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model inaccessible kernel process metadata, without reading unrelated host sessions.
        cleanup, "process_references", unavailable_proc
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model an absent or failed administrator-owned inspection socket.
        cleanup_inspection, "inspect", unavailable_probe
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Age the real Git fixture at the OS clock boundary so the young-checkout fast path cannot mask the unavailable-probe guard.
        time, "time", aged_clock
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert all("probe unavailable" in item.reason for item in report)
    assert not any(item.attempted or item.removed for item in report)
    assert all(entry.path.is_dir() for entry in entries)
    assert not (repository.paths.home / ".local/state/long-running-agent/cleanup-attempts.json").exists()


def test_generic_home_history_does_not_pin_unrelated_native_worktrees(repository: Repository) -> None:
    held, unused = repository.linked("saved"), repository.linked("unused")
    protected = cleanup.References(
        frozenset(),
        workspaces=frozenset({repository.paths.home, held.path}),
        workspace_containers=cleanup.workspace_containers(repository.paths),
    )
    assert "referenced" in eligible(repository, held, protected)
    assert eligible(repository, unused, protected) == "eligible"
    assert repository.paths.home in protected.workspaces


def test_actual_git_state_in_a_container_keeps_saved_workspace_protection(repository: Repository) -> None:
    entry = repository.linked()
    marker = repository.paths.code / ".git"
    marker.mkdir()
    (marker / "HEAD").write_text("unknown Git state\n")
    protected = cleanup.References(
        frozenset(),
        workspaces=frozenset({repository.paths.code}),
        workspace_containers=cleanup.workspace_containers(repository.paths),
    )
    assert "referenced" in eligible(repository, entry, protected)


@pytest.mark.parametrize("state", ["bare", "enclosing", "symlink"])
def test_generic_container_classification_preserves_bare_enclosing_and_linked_git(
    repository: Repository, state: str
) -> None:
    container = repository.paths.code
    match state:
        case "bare":
            (container / "HEAD").write_text("ref: refs/heads/main\n")
        case "enclosing":
            marker = repository.paths.home / ".git"
            marker.mkdir()
            (marker / "HEAD").write_text("unknown Git state\n")
        case _:
            (container / ".git").symlink_to(repository.main / ".git", target_is_directory=True)
    assert not cleanup.nongit_container(container)


def test_inaccessible_git_marker_is_unknown_instead_of_a_generic_container(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    unknown = repository.paths.code / ".git"
    original = Path.lstat

    def unavailable_marker(path: Path) -> os.stat_result:
        if path == unknown:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native permission denial must not become 'no Git marker' through Path.exists suppression.
        Path, "lstat", unavailable_marker
    )
    with pytest.raises(PermissionError):
        cleanup.nongit_container(repository.paths.code)


def test_empty_parent_git_marker_does_not_hide_native_worktrees(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    marker = repository.paths.code / ".git"
    marker.mkdir(mode=0o700)
    assert cleanup.inventory(repository.paths.code) == [entry]
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert report[0].removed
    assert not entry.path.exists()
    assert marker.is_dir()
    assert next(marker.iterdir(), None) is None
    assert repository.main.exists()


def test_nonempty_invalid_git_marker_stops_native_inventory(repository: Repository) -> None:
    entry = repository.linked()
    marker = repository.paths.code / ".git"
    marker.mkdir(mode=0o700)
    payload = marker / "unknown-state"
    payload.write_text("retain unknown Git state\n")
    with pytest.raises(CommandError, match="git exited with status 128"):
        cleanup.inventory(repository.paths.code)
    assert payload.read_text() == "retain unknown Git state\n"
    assert entry.path.exists()


def test_symlink_to_empty_git_marker_does_not_expose_nested_repositories(
    repository: Repository, tmp_path: Path
) -> None:
    entry = repository.linked()
    target = tmp_path / "empty-git"
    target.mkdir(mode=0o700)
    marker = repository.paths.code / ".git"
    marker.symlink_to(target, target_is_directory=True)
    assert cleanup.inventory(repository.paths.code) == []
    assert marker.is_symlink()
    assert next(target.iterdir(), None) is None
    assert entry.path.exists()


def test_repository_discovery_stops_at_its_directory_budget(tmp_path: Path) -> None:
    root = tmp_path / "code"
    root.mkdir()
    for index in range(cleanup.MAX_DIRECTORIES - 1):
        (root / f"placeholder-{index}").mkdir()
    assert list(cleanup.repositories(root)) == []
    overflow = root / "overflow"
    overflow.mkdir()
    with pytest.raises(RuntimeError, match="bounded inventory"):
        list(cleanup.repositories(root))
    assert overflow.is_dir()
    assert len(tuple(root.iterdir())) == cleanup.MAX_DIRECTORIES


def test_repository_entry_budget_counts_files_and_child_directories(tmp_path: Path) -> None:
    child = tmp_path / "source"
    child.mkdir()
    payload = tmp_path / "notes.txt"
    payload.write_text("retain local notes\n")
    scan = cleanup.child_directories(tmp_path, remaining=2)
    assert scan.children == [child]
    assert scan.inspected == 2
    with pytest.raises(RuntimeError, match="bounded entry limit"):
        cleanup.child_directories(tmp_path, remaining=1)
    assert child.is_dir()
    assert payload.read_text() == "retain local notes\n"


@pytest.mark.parametrize(
    "change",
    [
        "tracked",
        "staged",
        "untracked",
        "ignored",
        "unique",
        "locked",
        "detached",
        "operation",
        "dangling-operation",
        "assume-unchanged",
        "skip-worktree",
    ],
)
def test_local_work_and_git_states_are_preserved(
    repository: Repository, change: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    match change:
        case "tracked":
            (entry.path / "source.txt").write_text("unsaved\n")
        case "staged":
            (entry.path / "source.txt").write_text("staged\n")
            cleanup.git(entry.path, "add", "source.txt")
        case "untracked":
            (entry.path / "notes.txt").write_text("unsaved\n")
        case "ignored":
            (entry.path / ".env").write_text("PRIVATE_FIXTURE=keep\n")
        case "unique":
            cleanup.git(
                entry.path,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--allow-empty",
                "-qm",
                "unique work",
            )
            entry = cleanup.inventory(repository.paths.code)[0]
        case "locked":
            cleanup.git(repository.main, "worktree", "lock", str(entry.path))
            entry = cleanup.inventory(repository.paths.code)[0]
        case "detached":
            cleanup.git(entry.path, "checkout", "--quiet", "--detach")
            cleanup.git(repository.main, "worktree", "lock", str(entry.path))
            entry = cleanup.inventory(repository.paths.code)[0]
        case "operation" | "dangling-operation":
            directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
            if change == "operation":
                (directory / "MERGE_HEAD").write_text(entry.head)
            else:
                (directory / "MERGE_HEAD").symlink_to(directory / "missing-merge-state")
        case _:
            cleanup.git(entry.path, "update-index", "--" + change, "source.txt")
            (entry.path / "source.txt").write_text("hidden unsaved work\n")
    assert eligible(repository, entry) != "eligible"
    before = {path.name: path.read_bytes() for path in entry.path.iterdir() if path.is_file()}
    isolate_native_git(repository, monkeypatch)
    assert not any(item.removed for item in cleanup.reap(repository.paths, apply=True))
    assert entry.path.exists()
    assert {path.name: path.read_bytes() for path in entry.path.iterdir() if path.is_file()} == before


def test_nondisposable_native_status_retains_before_opening_an_earlier_ignored_cache(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("retained-before-cache-scan")
    cache = entry.path / ".venv"
    cache.mkdir()
    (cache / "cache.bin").write_bytes(b"disposable fixture")
    retained = entry.path / "zz-retained"
    retained.mkdir()
    (retained / "source.txt").write_text("retain native local source\n")
    exclude = repository.main / ".git/info/exclude"
    exclude.write_text(exclude.read_text() + "\nzz-retained/\n")
    status = cleanup.git(entry.path, "status", "--porcelain=v1", "-z", "--ignored=matching")
    assert status.index("!! .venv/") < status.index("!! zz-retained/")
    scans = observe_directory_scans(cache, monkeypatch)
    isolate_native_git(repository, monkeypatch)
    assert cleanup.reap(repository.paths, apply=True) == [
        cleanup.Decision(str(entry.path), "tracked, untracked, or non-disposable ignored local files")
    ]
    assert scans == []
    assert (cache / "cache.bin").read_bytes() == b"disposable fixture"
    assert (retained / "source.txt").read_text() == "retain native local source\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


def test_repeated_native_ignored_records_scan_one_root_and_final_validation_still_finds_new_private_files(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = repository.linked("one-generated-root")
    ignore = initial.path / ".gitignore"
    ignore.write_text(ignore.read_text().replace("node_modules/\n", "node_modules/*\n"))
    cleanup.git(initial.path, "add", ".gitignore")
    head = commit_fixture_file(initial.path, "published-ignore-rules.txt")
    cleanup.git(initial.path, "push", "-q", "origin", initial.branch + ":refs/heads/main")
    cleanup.git(repository.main, "fetch", "-q", "origin", "refs/heads/main:refs/remotes/origin/main")
    os.utime(repository.main / ".git/FETCH_HEAD", (repository.now, repository.now))
    modules = initial.path / "node_modules"
    modules.mkdir()
    for name in ("first.cache", "second.cache"):
        (modules / name).write_bytes(b"native disposable fixture")
    status = cleanup.git(initial.path, "status", "--porcelain=v1", "-z", "--ignored=matching")
    assert status.count("!! node_modules/") == 2
    scans = observe_directory_scans(modules, monkeypatch)
    isolate_native_git(repository, monkeypatch)
    assert cleanup.reap(repository.paths, apply=False) == [cleanup.Decision(str(initial.path), "eligible")]
    assert scans == [modules]
    native = cleanup.git
    inventories = 0
    private = modules / ".env"

    def native_inventory(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal inventories
        result = native(path, *arguments, timeout=timeout)
        if path == repository.main and arguments == ("worktree", "list", "--porcelain", "-z"):
            inventories += 1
            if inventories == 2:
                private.write_text("PRIVATE_FIXTURE=retain\n")
        return result

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Introduce a real private file at the final native inventory boundary after initial review; delegate every Git call.
        cleanup, "git", native_inventory
    )
    assert cleanup.reap(repository.paths, apply=True) == [
        cleanup.Decision(str(initial.path), "ignored files may contain nested source repositories")
    ]
    assert inventories == 2
    assert scans == [modules, modules]
    assert private.read_text() == "PRIVATE_FIXTURE=retain\n"
    assert native(repository.main, "rev-parse", initial.branch).strip() == head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


def test_only_known_ignored_dependency_caches_can_accompany_a_clean_tree(repository: Repository) -> None:
    entry = repository.linked()
    cache = entry.path / "node_modules/fixture"
    cache.mkdir(parents=True)
    (cache / "generated.js").write_text("generated dependency\n")
    assert eligible(repository, entry) == "eligible"
    (entry.path / ".env").write_text("PRIVATE_FIXTURE=keep\n")
    assert eligible(repository, entry) != "eligible"


@pytest.mark.parametrize(
    "relative",
    [
        pytest.param("python/pkg/__pycache__/source.pyc", id="nested-bytecode"),
        pytest.param("tools/clouddeploy/src/.pytest_cache/cache.json", id="nested-pytest-cache"),
    ],
)
def test_ignored_generated_caches_below_tracked_source_can_accompany_native_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, relative: str
) -> None:
    publish_nested_cache_rules(repository)
    entry = repository.linked()
    source_blob = cleanup.git(repository.main, "rev-parse", "HEAD:python/pkg/source.py")
    generated = entry.path / relative
    generated.parent.mkdir(parents=True)
    generated.write_bytes(b"reproducible native fixture cache\n")
    assert not cleanup.git(entry.path, "status", "--porcelain", "--untracked-files=normal")
    assert eligible(repository, entry) == "eligible"
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert report == [
        cleanup.Decision(
            str(entry.path), "removed clean merged worktree; branch retained", removed=True, attempted=True
        )
    ]
    assert not entry.path.exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert cleanup.git(repository.main, "rev-parse", "HEAD:python/pkg/source.py") == source_blob


@pytest.mark.parametrize("source", ["git", "bare", "editable"], ids=["nested-git", "nested-bare", "editable-venv"])
def test_nested_ignored_generated_roots_never_make_source_disposable(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    publish_nested_cache_rules(repository)
    entry = repository.linked()
    match source:
        case "git":
            preserved = entry.path / "python/.pytest_cache/private-source"
            preserved.mkdir(parents=True)
            cleanup.git(preserved, "init", "-q")
            (preserved / "source.py").write_text("unique nested fixture source\n")
        case "bare":
            preserved = entry.path / "tools/clouddeploy/src/__pycache__/private.git"
            preserved.mkdir(parents=True)
            cleanup.git(preserved, "init", "--bare", "-q")
        case "editable":
            preserved = entry.path / "python/.venv/src/private/source.py"
            preserved.parent.mkdir(parents=True)
            preserved.write_text("unique editable fixture source\n")
        case _:
            pytest.fail("Unknown nested generated-source fixture")
    assert "nested source" in eligible(repository, entry)
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert entry.path.is_dir()
    assert preserved.exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize(
    ("name", "retained"),
    [
        pytest.param(".env", True, id="private-env"),
        pytest.param(".env.local", True, id="private-local-env"),
        pytest.param(".env.production", True, id="private-production-env"),
        pytest.param(".env.example", False, id="public-example-env"),
        pytest.param(".env.sample", False, id="public-sample-env"),
        pytest.param(".env.template", False, id="public-template-env"),
    ],
)
def test_nested_generated_environment_files_preserve_private_configuration_and_allow_templates(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, name: str, *, retained: bool
) -> None:
    entry = repository.linked()
    configuration = entry.path / "node_modules/fixture/config" / name
    configuration.parent.mkdir(parents=True)
    configuration.write_bytes(b"synthetic native configuration; no payload inspection\n")
    configuration.chmod(0)
    assert (eligible(repository, entry) != "eligible") is retained
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert report[0].attempted is not retained
    assert report[0].removed is not retained
    assert configuration.exists() is retained
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_registered_storage_cannot_be_exempted_as_a_generic_saved_workspace_container(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    store = entry.path / "node_modules/private-claude"
    store.mkdir(parents=True, mode=0o700)
    credential = store / ".credentials.json"
    credential.write_bytes(b"synthetic fixture, never a real credential\n")
    credential.chmod(0)
    registry = repository.paths.home / ".local/share/long-running-agent/activity"
    registry.mkdir(parents=True, mode=0o700)
    identifier = str(UUID(int=2))
    record = registry / f"{identifier}.json"
    record.write_text(json.dumps({"id": identifier, "agent": "claude", "uid": os.getuid(), "store": str(store)}))
    record.chmod(0o600)
    activity = cleanup_activity.collect(repository.paths.home)
    protected = cleanup.References(
        frozenset(),
        workspaces=frozenset({repository.paths.home}),
        workspace_containers=frozenset({repository.paths.home}),
        storage=activity.storage,
    )
    assert eligible(repository, entry) == "eligible"
    assert eligible(repository, entry, protected) != "eligible"
    assert protected.contains(store / "nested-checkout")
    isolate_native_git(repository, monkeypatch)

    def native_storage_boundary(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        return protected

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native process/session collectors are isolated; the actual metadata collector and native Git removal guards remain under test.
        cleanup, "references", native_storage_boundary
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert credential.exists()
    assert credential.stat().st_mode & 0o777 == 0
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("local", ["env", "untracked-cache"], ids=["ignored-env-below-source", "untracked-cache"])
def test_generated_path_components_do_not_authorize_unknown_or_untracked_files(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, local: str
) -> None:
    entry = repository.linked()
    relative = "source/.env" if local == "env" else "python/__pycache__/untracked.pyc"
    preserved = entry.path / relative
    preserved.parent.mkdir(parents=True)
    preserved.write_bytes(b"retain local native fixture data\n")
    assert eligible(repository, entry) != "eligible"
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert preserved.read_bytes() == b"retain local native fixture data\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_nested_generated_root_cannot_follow_a_parent_alias_outside_the_checkout(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_nested_cache_rules(repository)
    entry = repository.linked()
    external = repository.paths.home / "outside-generated"
    preserved = external / "__pycache__/retained.pyc"
    preserved.parent.mkdir(parents=True)
    preserved.write_bytes(b"external native fixture data\n")
    alias = entry.path / "python/alias"
    alias.symlink_to(external, target_is_directory=True)
    assert not cleanup.owned_path(alias / "__pycache__", entry.path)
    assert eligible(repository, entry) != "eligible"
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 1
    assert not report[0].attempted
    assert not report[0].removed
    assert preserved.read_bytes() == b"external native fixture data\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_nested_ignored_source_repository_is_never_removed(repository: Repository) -> None:
    entry = repository.linked()
    source = entry.path / "node_modules/private-source"
    source.mkdir(parents=True)
    cleanup.git(source, "init", "-q")
    (source / "unsaved.txt").write_text("unique nested source\n")
    assert "nested source" in eligible(repository, entry)


def test_nested_bare_repository_unique_commit_and_branch_are_retained(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    source = entry.path / "node_modules/private.git"
    source.mkdir(parents=True)
    cleanup.git(source, "init", "--bare", "-q")
    cleanup.git(source, "fetch", "-q", str(repository.main), "main")
    unique = cleanup.git(
        source,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        "FETCH_HEAD^{tree}",
        "-m",
        "private nested root",
    ).strip()
    cleanup.git(source, "update-ref", "refs/heads/private", unique)
    with pytest.raises(CommandError, match="git exited with status"):
        cleanup.git(repository.main, "cat-file", "-e", unique)
    assert "nested source" in eligible(repository, entry)
    isolate_native_git(repository, monkeypatch)
    assert not any(item.attempted or item.removed for item in cleanup.reap(repository.paths, apply=True))
    assert cleanup.git(source, "rev-parse", "refs/heads/private").strip() == unique


@pytest.mark.parametrize(
    "layout",
    [
        pytest.param("partial-bare", id="objects-and-refs-without-head"),
        pytest.param("object-store", id="objects-info-and-pack"),
        pytest.param("loose-object", id="loose-object-fanout"),
        pytest.param("packed-object", id="distinctive-pack-file"),
        pytest.param("dangling-head", id="dangling-head-marker"),
    ],
)
def test_ignored_partial_git_storage_is_not_disposable(repository: Repository, layout: str) -> None:
    entry = repository.linked()
    source = entry.path / "node_modules/private-storage"
    source.mkdir(parents=True)
    match layout:
        case "partial-bare":
            (source / "objects").mkdir()
            (source / "refs").mkdir()
        case "object-store":
            (source / "info").mkdir()
            (source / "pack").mkdir()
        case "loose-object":
            original = repository.main / ".git/objects" / entry.head[:2] / entry.head[2:]
            target = source / entry.head[:2] / entry.head[2:]
            target.parent.mkdir()
            shutil.copyfile(original, target)
        case "packed-object":
            (source / f"pack-{entry.head}.pack").write_bytes(b"retained unknown Git pack\n")
        case "dangling-head":
            (source / "HEAD").symlink_to(source / "missing-head")
        case _:
            pytest.fail("Unknown native storage fixture")
    assert "nested source" in eligible(repository, entry)
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("parent", ["", "node_modules/dependency"], ids=["root-venv", "nested-venv"])
def test_editable_sources_inside_any_generated_virtualenv_are_retained(repository: Repository, parent: str) -> None:
    entry = repository.linked()
    source = entry.path / parent / ".venv/src/private/source.py"
    source.parent.mkdir(parents=True)
    source.write_text('"unique editable source"\n')
    assert "nested source" in eligible(repository, entry)
    assert ast.literal_eval(source.read_text()) == "unique editable source"


@pytest.mark.parametrize(
    "operation", [pytest.param("sequencer", id="cherry-pick-sequencer"), pytest.param("REBASE_HEAD", id="rebase-head")]
)
def test_native_operation_metadata_vetoes_a_clean_linked_checkout(repository: Repository, operation: str) -> None:
    entry = repository.linked()
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    if operation == "sequencer":
        sequencer = directory / operation
        sequencer.mkdir()
        (sequencer / "head").write_text(entry.head + "\n")
        (sequencer / "abort-safety").write_text(entry.head + "\n")
        (sequencer / "todo").write_text(f"pick {entry.head} fixture\n")
    else:
        (directory / operation).write_text(entry.head + "\n")
    assert not cleanup.git(entry.path, "status", "--porcelain")
    assert "operation" in eligible(repository, entry)
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize(
    "guard",
    [
        pytest.param("operation", id="active-sequencer-retains-first"),
        pytest.param("recent-metadata", id="recent-index-retains-first"),
        pytest.param("none", id="otherwise-eligible-checkout-keeps-deep-alias-veto"),
    ],
)
def test_cheap_checkout_vetoes_precede_real_object_alias_inspection(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, guard: str
) -> None:
    entry = repository.linked("guarded-shared-history")
    assert eligible(repository, entry) == "eligible"
    selected = repository.main / ".git/objects" / entry.head[:2] / entry.head[2:]
    payload = entry.path / "node_modules/payload.bin"
    payload.parent.mkdir()
    selected.rename(payload)
    selected.symlink_to(payload)
    assert cleanup.git(repository.main, "cat-file", "-t", entry.head).strip() == "commit"
    assert cleanup.local_files(entry.path) == "eligible"
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    match guard:
        case "operation":
            (directory / "sequencer").mkdir()
            (directory / "sequencer/head").write_text(entry.head + "\n")
            expected = "Git operation or index update is in progress"
        case "recent-metadata":
            os.utime(directory / "index", (repository.now, repository.now))
            expected = "worktree checkout or Git metadata is too recent"
        case "none":
            expected = "Git object storage contains an unverified alias"
        case _:
            pytest.fail("Unknown native checkout retention fixture")
    isolate_native_git(repository, monkeypatch)
    assert cleanup.reap(repository.paths, apply=True) == [cleanup.Decision(str(entry.path), expected)]
    assert payload.is_file()
    assert cleanup.git(repository.main, "cat-file", "-t", entry.head).strip() == "commit"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert any(row.path == entry.path for row in cleanup.inventory(repository.paths.code))
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()


@pytest.mark.parametrize(
    "storage",
    [
        pytest.param("symlink", id="relocated-object-root"),
        pytest.param("alternate", id="alternate-in-checkout"),
        pytest.param("fanout", id="aliased-object-fanout"),
    ],
)
def test_common_git_objects_cannot_depend_on_disposable_checkout_files(repository: Repository, storage: str) -> None:
    entry = repository.linked()
    objects = repository.main / ".git/objects"
    destination = entry.path / "node_modules/private-objects"
    destination.parent.mkdir()
    match storage:
        case "symlink":
            objects.rename(destination)
            objects.symlink_to(destination, target_is_directory=True)
        case "alternate":
            objects.rename(destination)
            (objects / "info").mkdir(parents=True)
            (objects / "pack").mkdir()
            (objects / "info/alternates").write_text(str(destination) + "\n")
        case "fanout":
            destination.mkdir()
            fanout = objects / entry.head[:2]
            fanout.rename(destination / fanout.name)
            fanout.symlink_to(destination / fanout.name, target_is_directory=True)
        case _:
            pytest.fail("Unknown object-storage fixture")
    assert cleanup.git(entry.path, "cat-file", "-t", entry.head).strip() == "commit"
    assert "object storage" in eligible(repository, entry)
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_common_object_storage_under_an_aliased_git_ancestor_is_retained(repository: Repository) -> None:
    entry = repository.linked()
    common = repository.main / ".git"
    destination = entry.path / "node_modules/private-metadata"
    destination.parent.mkdir()
    common.rename(destination)
    common.symlink_to(destination, target_is_directory=True)
    assert cleanup.git(entry.path, "cat-file", "-t", entry.head).strip() == "commit"
    assert "aliased" in cleanup.object_storage(common)
    assert eligible(repository, entry) != "eligible"
    assert cleanup.git(entry.path, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("storage", ["pack", "index", "loose"])
def test_individual_git_object_links_cannot_depend_on_an_ignored_checkout_payload(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, storage: str
) -> None:
    entry = repository.linked()
    objects = repository.main / ".git/objects"
    if storage == "loose":
        selected = objects / entry.head[:2] / entry.head[2:]
    else:
        cleanup.git(repository.main, "repack", "-a", "-d")
        extension = "pack" if storage == "pack" else "idx"
        selected = next((objects / "pack").glob(f"*.{extension}"))
    payload = entry.path / "node_modules/payload.bin"
    payload.parent.mkdir()
    selected.rename(payload)
    selected.symlink_to(payload)
    assert cleanup.git(repository.main, "cat-file", "-t", entry.head).strip() == "commit"
    assert cleanup.local_files(entry.path) == "eligible"
    assert "object storage" in eligible(repository, entry)
    isolate_native_git(repository, monkeypatch)
    decision = next(row for row in cleanup.reap(repository.paths, apply=True) if row.path == str(entry.path))
    assert not decision.removed
    assert not decision.attempted
    assert "object storage" in decision.reason
    assert payload.is_file()
    assert cleanup.git(repository.main, "cat-file", "-t", entry.head).strip() == "commit"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_shared_regular_git_pack_files_keep_history_after_native_checkout_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    cleanup.git(repository.main, "repack", "-a", "-d")
    selected = next((repository.main / ".git/objects/pack").glob("*.pack"))
    payload = entry.path / "node_modules/payload.bin"
    payload.parent.mkdir()
    payload.hardlink_to(selected)
    assert selected.stat().st_nlink == 2
    assert eligible(repository, entry) == "eligible"
    isolate_native_git(repository, monkeypatch)
    decision = next(row for row in cleanup.reap(repository.paths, apply=True) if row.path == str(entry.path))
    assert decision.removed
    assert decision.attempted
    assert not entry.path.exists()
    assert selected.is_file()
    assert selected.stat().st_nlink == 1
    assert cleanup.git(repository.main, "cat-file", "-t", entry.head).strip() == "commit"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("boundary", ["entries", "deadline", "depth"])
def test_descendant_object_inspection_keeps_one_finite_budget(tmp_path: Path, boundary: str) -> None:
    objects = tmp_path / "objects"
    for name in ("aa", "bb"):
        folder = objects / name
        folder.mkdir(parents=True)
        (folder / "object").write_bytes(b"private bounded object fixture")
    descriptor = os.open(objects, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    budget = cleanup.ObjectStorageBudget(
        deadline=0 if boundary == "deadline" else time.monotonic() + 10,
        inspected=cleanup.MAX_FDS - 3 if boundary == "entries" else 0,
    )
    try:
        assert "limits" in cleanup.object_entries(descriptor, budget, depth=17 if boundary == "depth" else 0)
    finally:
        os.close(descriptor)
    assert (objects / "aa/object").is_file()
    assert (objects / "bb/object").is_file()


def test_object_directory_replaced_by_an_alias_during_open_never_follows_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    common = tmp_path / "common"
    selected = common / "objects/aa"
    selected.mkdir(parents=True)
    (selected / "object").write_bytes(b"private object fixture")
    outside = tmp_path / "unrelated"
    outside.mkdir()
    retained = outside / "retained"
    retained.write_bytes(b"unrelated data must remain untouched")
    native_open = os.open
    swapped: list[Path] = []

    def replaced_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "aa" and dir_fd is not None and not swapped:
            destination = common / "moved"
            selected.rename(destination)
            selected.symlink_to(outside, target_is_directory=True)
            swapped.append(destination)
        return native_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace an actual private directory at the OS open boundary to reproduce the otherwise nondeterministic alias race.
        os, "open", replaced_open
    )
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        cleanup.object_storage(common)
    assert swapped == [common / "moved"]
    assert (swapped[0] / "object").read_bytes() == b"private object fixture"
    assert retained.read_bytes() == b"unrelated data must remain untouched"


def test_nested_untracked_files_remain_a_veto_with_collapsed_status(repository: Repository) -> None:
    entry = repository.linked()
    notes = entry.path / "untracked/deep/notes.txt"
    notes.parent.mkdir(parents=True)
    notes.write_text("unsaved nested notes\n")
    assert "?? untracked/" in cleanup.git(entry.path, "status", "--porcelain", "--untracked-files=normal")
    assert "untracked" in eligible(repository, entry)
    assert notes.read_text() == "unsaved nested notes\n"


def test_symlinked_worktree_path_is_not_a_candidate_for_removal(repository: Repository) -> None:
    entry = repository.linked()
    destination = repository.paths.code / "moved-source"
    entry.path.rename(destination)
    entry.path.symlink_to(destination, target_is_directory=True)
    assert eligible(repository, entry) != "eligible"
    assert (destination / "source.txt").read_text() == "committed\n"


def test_checkout_age_and_verified_default_branch_fetch_are_required(repository: Repository) -> None:
    entry = repository.linked()
    assert (
        cleanup.eligibility(
            entry, repository.paths.code, cleanup.References(frozenset()), min_age_days=7, now=time.time()
        )
        != "eligible"
    )
    fetch = repository.main / ".git/FETCH_HEAD"
    fetch.write_text(f"{entry.head}\t\tbranch 'different' of {repository.main}\n")
    os.utime(fetch, (repository.now, repository.now))
    assert "verified fetch" in eligible(repository, entry)
    os.utime(fetch, (repository.now - 2 * cleanup.DAY, repository.now - 2 * cleanup.DAY))
    assert "stale" in eligible(repository, entry)


def test_future_fetch_timestamp_cannot_prove_recent_default_branch_state(repository: Repository) -> None:
    entry = repository.linked()
    fetch = repository.main / ".git/FETCH_HEAD"
    os.utime(fetch, (repository.now + cleanup.DAY, repository.now + cleanup.DAY))
    assert "stale" in eligible(repository, entry)
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_inherited_git_directory_override_is_rejected(repository: Repository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_DIR", str(repository.main / ".git"))
    with pytest.raises(RuntimeError, match="overrides"):
        cleanup.git(repository.main, "status", "--porcelain")


def test_replacement_objects_cannot_make_unique_commits_eligible(repository: Repository) -> None:
    entry = repository.linked()
    unique = cleanup.git(
        repository.main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit-tree",
        "HEAD^{tree}",
        "-m",
        "unique root",
    ).strip()
    cleanup.git(entry.path, "update-ref", entry.branch, unique)
    cleanup.git(
        repository.main,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "replace",
        "--graft",
        "main",
        unique,
    )
    entry = cleanup.inventory(repository.paths.code)[0]
    assert "not proven merged" in eligible(repository, entry)


def test_legacy_grafts_block_ancestry_proofs(repository: Repository) -> None:
    entry = repository.linked()
    (repository.main / ".git/info/grafts").write_text(entry.head + "\n")
    assert "grafts" in eligible(repository, entry)


def test_process_references_block_cwd_open_files_and_arguments(repository: Repository) -> None:
    entry = repository.linked()
    for protected in (
        cleanup.References(frozenset({entry.path / "subdirectory"})),
        cleanup.References(frozenset(), (f"--cd={entry.path}",)),
    ):
        assert "referenced" in eligible(repository, entry, protected)
    assert not cleanup.References(frozenset({entry.path.with_name("old-other")})).contains(entry.path)


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires Linux process reference files")
def test_actual_child_open_file_is_retained_when_its_cwd_is_elsewhere(repository: Repository, tmp_path: Path) -> None:
    entry = repository.linked()
    script = "import os,sys,time;f=open('source.txt');os.chdir(sys.argv[1]);print('ready',flush=True);time.sleep(30)"
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)], cwd=entry.path, stdout=subprocess.PIPE, text=True
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline() == "ready\n"
        assert cleanup.process_reference(Path("/proc") / str(process.pid)).contains(entry.path)
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()


def test_live_process_with_missing_cwd_does_not_silently_disappear(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    process = proc / "1234"
    process.mkdir(parents=True)
    (process / "stat").write_bytes(b"1234 (fixture) S " + b"0 " * 18 + b"100\n")
    (process / "cwd").symlink_to(tmp_path / "deleted")
    with pytest.raises(RuntimeError, match="live process"):
        cleanup.process_references(proc)


@pytest.mark.parametrize("title", ["npm install", "npm exec fixture", "npm cache verify"])
def test_npm_process_titles_are_recognized_as_active_package_managers(tmp_path: Path, title: str) -> None:
    process = tmp_path / "1234"
    process.mkdir()
    (process / "stat").write_bytes(b"1234 (npm) S " + b"0 " * 18 + b"100\n")
    (process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    (process / "cmdline").write_bytes(os.fsencode(title) + b"\0")
    (process / "fd").mkdir()
    (process / "maps").write_bytes(b"")
    assert cleanup.process_references(tmp_path).package_writer


def test_ambiguous_mapped_file_paths_fail_closed(tmp_path: Path) -> None:
    process = tmp_path / "1234"
    process.mkdir()
    (process / "stat").write_bytes(b"1234 (fixture) S " + b"0 " * 18 + b"100\n")
    (process / "cwd").symlink_to(tmp_path, target_is_directory=True)
    (process / "cmdline").write_bytes(b"fixture\0")
    (process / "fd").mkdir()
    (process / "maps").write_text("1000-2000 r--p 00000000 00:00 1 /worktree\\012name/source.txt\n")
    with pytest.raises(RuntimeError, match="Mapped-file path is ambiguous"):
        cleanup.process_references(tmp_path)


def test_zellij_held_pane_is_protected_dead_sessions_are_not_resurrected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands: list[list[str]] = []
    (tmp_path / "not-focused").mkdir()

    def executable(_name: str, *, path: str) -> str:
        assert path
        return "/native/zellij"

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- This test intercepts native executable lookup, not application policy.
        shutil, "which", executable
    )

    def native(arguments: Sequence[str], **_options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        commands.append(list(arguments))
        output = (
            "alive [Created 2days ago] \nold [Created 4days ago] (EXITED - attach to resurrect)\n"
            if "list-sessions" in arguments
            else json.dumps(
                [
                    {"pane_cwd": str(tmp_path), "pane_command": "codex resume fixture", "is_held": True, "tab_id": 5},
                    {"pane_cwd": str(tmp_path / "not-focused"), "tab_id": 25},
                    {"is_plugin": True, "is_selectable": False, "plugin_url": "zellij:status-bar"},
                    {"is_plugin": True, "is_suppressed": True, "plugin_url": "zellij:link"},
                ]
            )
        )
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native command interception verifies no exited session is attached.
        cleanup, "run", native
    )
    assert cleanup.zellij_references(RuntimePaths(tmp_path), running=True).contains(tmp_path)
    assert cleanup.zellij_references(RuntimePaths(tmp_path), running=True).contains(tmp_path / "not-focused")
    assert len(commands) == 4
    assert commands[1][2] == "alive"


@pytest.mark.parametrize("cwd", ["", ".", "relative/worktree"])
def test_unknown_zellij_pane_working_directory_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cwd: str
) -> None:
    def executable(_name: str, *, path: str) -> str:
        assert path
        return "/native/zellij"

    def native(arguments: Sequence[str], **_options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        output = "alive [Created 2days ago]\n" if "list-sessions" in arguments else json.dumps([{"pane_cwd": cwd}])
        return subprocess.CompletedProcess(arguments, 0, output, "")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exercise malformed native pane data without reading unrelated live sessions.
        shutil, "which", executable
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The native inventory boundary supplies unknown working directories.
        cleanup, "run", native
    )
    with pytest.raises(RuntimeError, match="no known working directory"):
        cleanup.zellij_references(RuntimePaths(tmp_path), running=True)


def test_changed_references_at_apply_boundary_block_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    native = cleanup.git
    listed = 0
    protected: set[Path] = set()

    def native_inventory(path: Path, *arguments: str, timeout: float = 15) -> str:
        nonlocal listed
        if arguments[:2] == ("worktree", "list"):
            listed += 1
            if listed == 2:
                protected.add(entry.path)
        return native(path, *arguments, timeout=timeout)

    def protect(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        return cleanup.References(frozenset(protected))

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Changing this lookup between review and apply is the race-safety behavior under test.
        cleanup, "references", protect
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A live holder appears at the delegated final native inventory boundary, after the retaining-only merge prefilter.
        cleanup, "git", native_inventory
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert not report[0].removed
    assert entry.path.exists()


def test_native_remove_failure_is_reported_without_force_or_fallback(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(name) for name in ("old-a", "old-b", "old-c")]
    native = cleanup.git
    attempts: list[str] = []

    def fail_remove(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[:2] == ("worktree", "remove"):
            assert "--force" not in arguments
            attempts.append(arguments[-1])
            message = "native refusal"
            raise RuntimeError(message)
        return native(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Intercept the native removal refusal to verify no force or fallback deletion follows.
        cleanup, "git", fail_remove
    )
    isolate_native_git(repository, monkeypatch)
    report = cleanup.reap(repository.paths, apply=True)
    assert "did not complete" in report[0].reason
    assert len(attempts) == 2
    assert sum(item.attempted for item in report) == 2
    assert all(entry.path.exists() for entry in entries)
    assert "per-run" in report[2].reason


def test_docker_bind_intersections_protect_parent_and_descendant_worktrees(tmp_path: Path) -> None:
    mounted = tmp_path / "worktree/data"
    mounted.mkdir(parents=True)
    output = json.dumps([{"Type": "bind", "Source": str(mounted)}])
    protected = cleanup.References(frozenset(), mounted=cleanup.parse_mounts(output, expected_rows=1))
    assert protected.contains(tmp_path / "worktree")
    assert protected.contains(tmp_path / "worktree/data/child")
    assert not protected.contains(tmp_path / "worktree-other")


@pytest.mark.parametrize("reference", ["parent", "checkout", "child"])
def test_saved_codex_workspace_intersections_protect_checkouts(tmp_path: Path, reference: str) -> None:
    checkout = tmp_path / "worktrees/active"
    saved = {"parent": tmp_path, "checkout": checkout, "child": checkout / "src"}[reference]
    protected = cleanup.References(frozenset(), workspaces=frozenset({saved}))
    assert protected.contains(checkout)
    if reference != "parent":
        assert not protected.contains(tmp_path / "unrelated")


@pytest.mark.parametrize("output", ["", "[]\n[]\n"], ids=["missing-container-row", "extra-container-row"])
def test_incomplete_docker_mount_inventory_fails_closed(output: str) -> None:
    with pytest.raises(RuntimeError, match="Docker mount inventory is incomplete"):
        cleanup.parse_mounts(output, expected_rows=1)


def test_user_cleanup_lock_is_exclusive_and_cannot_follow_a_symlink(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    with (
        cleanup.cleanup_lock(paths),
        pytest.raises(BlockingIOError, match=r"temporarily unavailable|would block"),
        cleanup.cleanup_lock(paths),
    ):
        pass
    state = tmp_path / ".local/state/long-running-agent"
    (state / "cleanup.lock").unlink()
    payload = tmp_path / "keep.txt"
    payload.write_text("keep\n")
    (state / "cleanup.lock").symlink_to(payload)
    with pytest.raises(OSError, match="Too many levels of symbolic links"), cleanup.cleanup_lock(paths):
        pass
    assert payload.read_text() == "keep\n"


def test_cleanup_lock_cannot_chmod_an_unrelated_hardlinked_file(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    state = cleanup.state_directory(paths)
    payload = tmp_path / "unrelated-file"
    payload.write_text("preserve mode and content\n")
    payload.chmod(0o644)
    os.link(payload, state / "cleanup.lock")
    with pytest.raises(RuntimeError, match="own regular file"), cleanup.cleanup_lock(paths):
        pass
    assert payload.stat().st_mode & 0o777 == 0o644
    assert payload.read_text() == "preserve mode and content\n"


@pytest.mark.skipif(
    os.getuid() == 0 or not Path("/proc/self").exists(), reason="cleanup CLI requires unprivileged Linux"
)
@pytest.mark.parametrize("refused", [False, True], ids=["successful-native-removals", "refused-native-attempts"])
def test_native_cleanup_cli_defers_cache_after_every_attempted_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, refused: bool
) -> None:
    entries = [repository.linked(name) for name in ("cache-first", "cache-second")]
    evidence = native_cache(repository, monkeypatch)
    monkeypatch.setenv("HOME", str(repository.paths.home))
    state = cleanup.state_directory(repository.paths)
    stamp = state / "cache-pruned"
    private_write(stamp, "previous successful native cache verification\n")
    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
    original = cleanup.git

    def native_attempt(path: Path, *arguments: str, timeout: float = 15) -> str:
        if refused and arguments[:2] == ("worktree", "remove"):
            assert "--force" not in arguments
            message = "Owned native fixture refusal"
            raise RuntimeError(message)
        return original(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Refuse only the native removal boundary in one case; real Git admission and persisted precharges remain intact.
        cleanup, "git", native_attempt
    )
    result = CliRunner().invoke(cleanup.app, ["run", "--apply"])
    assert result.exit_code == 0, result.output
    report = cleanup_daemon.WorkerReport.model_validate_json(result.stdout)
    assert sum(row.attempted for row in report.worktrees) == 2
    assert all(row.removed is not refused for row in report.worktrees)
    assert report.cache == "skipped: native worktree removal used this sweep; npm verification deferred"
    assert not evidence.command.exists()
    assert not evidence.processes.exists()
    assert (stamp.read_bytes(), stamp.stat().st_mtime_ns) == before
    for entry in entries:
        assert entry.path.exists() is refused
        assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 2


@pytest.mark.skipif(
    os.getuid() == 0 or not Path("/proc/self").exists(), reason="cleanup CLI requires unprivileged Linux"
)
def test_fast_zero_attempt_cli_runs_due_native_cache_verification_and_records_success(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    unique = commit_fixture_file(entry.path, "unmerged.txt")
    evidence = native_cache(repository, monkeypatch)
    monkeypatch.setenv("HOME", str(repository.paths.home))
    result = CliRunner().invoke(cleanup.app, ["run", "--apply"])
    assert result.exit_code == 0, result.output
    report = cleanup_daemon.WorkerReport.model_validate_json(result.stdout)
    assert not any(row.attempted or row.removed for row in report.worktrees)
    assert report.cache.startswith("npm download cache verified")
    assert evidence.processes.is_file()
    assert evidence.command.read_text().splitlines()[1:3] == ["cache", "verify"]
    stamp = cleanup.state_directory(repository.paths) / "cache-pruned"
    assert stamp.is_file()
    assert stamp.stat().st_mode & 0o777 == 0o600
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == unique


@pytest.mark.skipif(
    os.getuid() == 0 or not Path("/proc/self").exists(), reason="cleanup CLI requires unprivileged Linux"
)
def test_report_only_cli_never_enters_due_cache_maintenance(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked()
    evidence = native_cache(repository, monkeypatch)
    monkeypatch.setenv("HOME", str(repository.paths.home))
    result = CliRunner().invoke(cleanup.app, ["run"])
    assert result.exit_code == 0, result.output
    assert '"cache": "report only"' in result.stdout
    assert not evidence.command.exists()
    assert not evidence.processes.exists()
    assert not (cleanup.state_directory(repository.paths) / "cache-pruned").exists()
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize(
    ("elapsed", "runs"),
    [pytest.param(149.999, True, id="headroom-available"), pytest.param(150.0, False, id="headroom-exhausted")],
)
def test_cache_start_deadline_preserves_due_success_stamp_when_deferred(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, elapsed: float, runs: bool
) -> None:
    evidence = native_cache(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    stamp = state / "cache-pruned"
    private_write(stamp, "prior successful fixture verification\n")
    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
    outcome = cleanup.cache_maintenance(repository.paths, state, [], elapsed=elapsed)
    if runs:
        assert outcome.startswith("npm download cache verified")
        assert evidence.command.is_file()
        assert evidence.processes.is_file()
        assert stamp.read_bytes() != before[0]
    else:
        assert outcome == "skipped: worktree inspection used cache time budget; npm verification deferred"
        assert not evidence.command.exists()
        assert not evidence.processes.exists()
        assert (stamp.read_bytes(), stamp.stat().st_mtime_ns) == before


def test_recent_successful_cache_stamp_keeps_weekly_admission_before_sweep_deferrals(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = native_cache(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    stamp = state / "cache-pruned"
    private_write(stamp, "current successful fixture verification\n")
    os.utime(stamp, (repository.now, repository.now))
    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
    decisions = [cleanup.Decision(str(repository.main), "native fixture attempt", attempted=True)]
    assert cleanup.cache_maintenance(repository.paths, state, decisions, elapsed=150) == (
        "skipped: weekly npm verification is not due"
    )
    assert not evidence.command.exists()
    assert not evidence.processes.exists()
    assert (stamp.read_bytes(), stamp.stat().st_mtime_ns) == before


def test_native_cache_failure_never_advances_the_weekly_success_stamp(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = native_cache(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    stamp = state / "cache-pruned"
    private_write(stamp, "prior successful fixture verification\n")
    before = stamp.read_bytes(), stamp.stat().st_mtime_ns
    node = repository.paths.home / ".local/share/mise/installs/node/fixture/bin/node"
    node.write_text("#!/bin/sh\nexit 3\n")
    assert cleanup.cache_maintenance(repository.paths, state, [], elapsed=0) == (
        "skipped: native package-store cleanup was unavailable or busy"
    )
    assert evidence.processes.is_file()
    assert not evidence.command.exists()
    assert (stamp.read_bytes(), stamp.stat().st_mtime_ns) == before


def test_cache_only_runs_npm_download_cache_verification_when_idle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []

    def idle_pressure(_path: Path) -> Pressure:
        return Pressure(0, 0)

    def idle_processes() -> cleanup.References:
        return cleanup.References(frozenset())

    def executable(_name: str, *, path: str) -> str:
        assert path
        return "/native/npm"

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Host PSI availability must not determine a native command-boundary test.
        cleanup, "pressure", idle_pressure
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Real host package-manager activity is outside this isolated native-command fixture.
        cleanup, "process_references", idle_processes
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The native executable boundary must not launch a package-manager shim.
        shutil, "which", executable
    )

    def native(arguments: Sequence[str], **options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        calls.append(list(arguments))
        assert options.get("timeout") == 30
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Capture the native cache command without touching the developer's cache.
        cleanup, "run", native
    )
    assert cleanup.prune_cache(RuntimePaths(tmp_path)).startswith("npm")
    assert calls[0][:3] == ["/native/npm", "cache", "verify"]
    assert "--offline" in calls[0]

    def package_writer() -> cleanup.References:
        return cleanup.References(frozenset(), package_writer=True)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Transition the global process lookup to prove the native command is skipped while busy.
        cleanup, "process_references", package_writer
    )
    assert cleanup.prune_cache(RuntimePaths(tmp_path)).startswith("skipped")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "stalls",
    [
        pytest.param(Pressure(50, 0), id="high-partial-stalls"),
        pytest.param(Pressure(None, 0), id="unknown-partial-stalls"),
        pytest.param(Pressure(0, None), id="unknown-full-stalls"),
    ],
)
def test_cache_skips_high_or_unknown_partial_pressure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stalls: Pressure
) -> None:
    def busy_pressure(_path: Path) -> Pressure:
        return stalls

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Simulate the kernel PSI boundary without depending on real host pressure.
        cleanup, "pressure", busy_pressure
    )
    assert cleanup.prune_cache(RuntimePaths(tmp_path)) == "skipped: memory or disk pressure is high or unknown"


def test_timer_uses_private_installed_python_and_bounds_resource_use() -> None:
    text = cleanup.service_text(Path("/home/a%user/tool path/bin/python"))
    assert (
        'ExecStart="/home/a%%user/tool path/bin/python" -m long_running_agent.cli runtime cleanup run --apply' in text
    )
    assert "Nice=19" in text
    assert "IOSchedulingClass=idle" in text
    assert "TimeoutStartSec=5min" in text
    assert "sudo" not in text


def test_existing_mise_node_npm_is_resolved_without_executing_a_shim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    node = tmp_path / ".local/share/mise/installs/node/24.22.0"
    (node / "bin").mkdir(parents=True)
    executable = node / "bin/node"
    executable.write_text("native fixture\n")
    executable.chmod(0o700)
    package = node / "lib/node_modules/npm"
    (package / "bin").mkdir(parents=True)
    (package / "package.json").write_text('{"name":"npm"}')
    (package / "bin/npm-cli.js").write_text("native CLI fixture\n")
    (node.parent / "global").symlink_to(node, target_is_directory=True)
    shim = tmp_path / "mise"
    shim.write_text("must never execute\n")

    def shim_lookup(_name: str, *, path: str) -> str:
        assert path
        return str(shim)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Intercept global tool lookup to prove an installed CLI bypasses its shim.
        shutil, "which", shim_lookup
    )
    assert cleanup.npm_arguments(RuntimePaths(tmp_path)) == [str(executable), str(package / "bin/npm-cli.js")]


def test_mac_or_root_cannot_install_a_user_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    def root_uid() -> int:
        return 0

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Root identity is an OS boundary; the test must never become root.
        os, "getuid", root_uid
    )
    with pytest.raises(RuntimeError, match="own Linux user"):
        cleanup.enable()


@pytest.mark.parametrize("kind", ["tracked", "untracked"], ids=["edited-source", "unsaved-file"])
def test_dirty_native_candidate_is_retained_before_expensive_global_inspection(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    entry = repository.linked("cheap-dirty-veto")
    target = entry.path / ("source.txt" if kind == "tracked" else "unsaved.txt")
    target.write_text("keep every unsaved byte\n")
    isolate_native_git(repository, monkeypatch)

    def forbidden_live_inventory(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        pytest.fail("A visible dirty checkout must not start expensive global reference inspection")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The external process census is forbidden only after real Git has proved local dirt; no eligibility result is replaced.
        cleanup, "references", forbidden_live_inventory
    )
    assert cleanup.reap(repository.paths, apply=True) == [
        cleanup.Decision(str(entry.path), "tracked, untracked, or non-disposable ignored local files")
    ]
    assert target.read_text() == "keep every unsaved byte\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


def test_final_native_registration_reopens_only_the_known_repository(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("scoped-registration")
    isolate_native_git(repository, monkeypatch)
    original = cleanup.inventory
    observed: list[Path] = []

    def observed_inventory(root: Path) -> list[cleanup.Worktree]:
        observed.append(root)
        return original(root)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count delegated real inventory calls without replacing discovery or native registration contents.
        cleanup, "inventory", observed_inventory
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page[0].removed
    assert observed == [repository.paths.code]
    assert not entry.path.exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_scoped_registration_rejects_a_linked_main_git_marker(repository: Repository) -> None:
    entry = repository.linked("linked-main-marker")
    marker = repository.main / ".git"
    saved = repository.main / "saved-native-git"
    marker.rename(saved)
    marker.symlink_to(saved, target_is_directory=True)
    try:
        assert cleanup.registered_candidate(entry, repository.paths.code) is None
        assert entry.path.is_dir()
    finally:
        marker.unlink()
        saved.rename(marker)
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("target", ["main", "metadata"], ids=["replaced-main-root", "replaced-main-gitdir"])
def test_scoped_registration_rejects_native_identity_replacement_during_list(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, target: str
) -> None:
    entry = repository.linked("replaced-main-identity")
    original = cleanup.git
    path = repository.main if target == "main" else repository.main / ".git"
    saved = path.with_name(path.name + "-retained")

    def replace_after_native_list(directory: Path, *arguments: str, timeout: float = 15) -> str:
        output = original(directory, *arguments, timeout=timeout)
        if arguments[:2] == ("worktree", "list"):
            path.rename(saved)
            path.mkdir()
        return output

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Change only the real fixture inode after the native list returns, exercising the subsequent filesystem identity guard.
        cleanup, "git", replace_after_native_list
    )
    try:
        assert cleanup.registered_candidate(entry, repository.paths.code) is None
        assert entry.path.is_dir()
    finally:
        path.rmdir()
        saved.rename(path)
    assert original(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("empty", [False, True], ids=["one-candidate", "package-writer-only"])
def test_scoped_authenticated_process_probe_preserves_global_flags_without_rediscovery(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    entry = repository.linked("scoped-process-holder")
    requested = () if empty else (entry.path,)

    def unavailable_proc() -> cleanup.References:
        raise PermissionError

    def probe(candidates: Sequence[Path]) -> cleanup_inspection.ProbeReply:
        assert tuple(candidates) == requested
        return cleanup_inspection.ProbeReply.model_validate(
            {
                "schema": 1,
                "nonce": "a" * 32,
                "uid": os.getuid(),
                "protected": [] if empty else [str(entry.path)],
                "package_writer": True,
                "zellij_running": True,
                "process_count": 2,
            }
        )

    def forbidden_inventory(_root: Path) -> list[cleanup.Worktree]:
        pytest.fail("An explicit process-probe scope must not rediscover every repository")

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model the actual kernel permission boundary; the authenticated protocol has independent native socket tests.
        cleanup, "process_references", unavailable_proc
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exercise subset routing and global flags at the external root-owned IPC boundary, never synthesize eligibility.
        cleanup_inspection, "inspect", probe
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A forbidden external rediscovery proves explicit empty scope is distinct from omitted scope.
        cleanup, "inventory", forbidden_inventory
    )
    result = cleanup.inspected_processes(repository.paths, requested)
    assert result.package_writer
    assert "zellij" in result.arguments
    assert result.contains(entry.path) is not empty


def test_due_cache_verification_can_run_after_a_bounded_partial_worktree_page(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence = native_cache(repository, monkeypatch)
    state = cleanup.state_directory(repository.paths)
    outcome = cleanup.cache_maintenance(
        repository.paths, state, [cleanup.Decision("uninspected", cleanup.DEFERRED)], elapsed=60
    )
    assert outcome.startswith("npm download cache verified")
    assert evidence.command.is_file()
    assert evidence.processes.is_file()
    assert (state / "cache-pruned").is_file()


def publish_generated_artifact_rules(repository: Repository) -> None:
    ignore = repository.main / ".gitignore"
    ignore.write_text(ignore.read_text() + ".import_linter_cache/\n.yarn/*\n")
    publish_nested_cache_rules(repository)


def test_native_removal_accepts_documented_import_graph_and_yarn_install_state_caches(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_generated_artifact_rules(repository)
    entry = repository.linked("documented-generated-artifacts")
    state = entry.path / ".yarn/install-state.gz"
    state.parent.mkdir()
    state.write_bytes(gzip.compress(b"private fixture install graph"))
    cache = entry.path / "python/.import_linter_cache"
    cache.mkdir()
    (cache / "sarj.meta.json").write_text('{"generated":"import graph"}\n')
    assert eligible(repository, entry) == "eligible"
    isolate_native_git(repository, monkeypatch)
    page = cleanup.reap(repository.paths, apply=True)
    assert len(page) == 1
    assert page[0].removed
    assert page[0].attempted
    assert not entry.path.exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("kind", ["plaintext", "symlink", "hardlink", "directory", "oversized", "writable", "neighbor"])
def test_nonstandard_or_aliased_yarn_state_is_retained_with_all_contents(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    publish_generated_artifact_rules(repository)
    entry = repository.linked("protected-yarn-state")
    state = entry.path / ".yarn/install-state.gz"
    state.parent.mkdir()
    preserved = repository.paths.home / "preserved-native-graph.gz"
    payload = gzip.compress(b"preserve every native fixture byte")
    preserved.write_bytes(payload)
    match kind:
        case "plaintext":
            state.write_bytes(b"unique unpublished notes\n")
        case "symlink":
            state.symlink_to(preserved)
        case "hardlink":
            os.link(preserved, state)
        case "directory":
            state.mkdir()
            (state / "source.py").write_text("# unique fixture source\n")
        case "oversized":
            state.write_bytes(payload)
            with state.open("r+b") as stream:
                stream.truncate(cleanup.YARN_STATE_BYTES + 1)
        case "writable":
            state.write_bytes(payload)
            state.chmod(0o666)
        case "neighbor":
            state.write_bytes(payload)
            (state.parent / "private-notes.txt").write_text("retain unrelated ignored source\n")
        case _:
            pytest.fail("Unknown native artifact guard fixture")
    isolate_native_git(repository, monkeypatch)
    page = cleanup.reap(repository.paths, apply=True)
    assert len(page) == 1
    assert not page[0].attempted
    assert not page[0].removed
    assert entry.path.is_dir()
    assert preserved.read_bytes() == payload
    assert state.exists()
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_import_graph_cache_with_a_native_nested_repository_stays_protected(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_generated_artifact_rules(repository)
    entry = repository.linked("source-in-import-cache")
    cache = entry.path / "python/.import_linter_cache"
    cache.mkdir()
    cleanup.git(cache, "init", "-q")
    (cache / "unpublished.py").write_text("# retain nested fixture source\n")
    isolate_native_git(repository, monkeypatch)
    page = cleanup.reap(repository.paths, apply=True)
    assert len(page) == 1
    assert not page[0].attempted
    assert not page[0].removed
    assert (cache / ".git/HEAD").is_file()
    assert (
        (cache / "unpublished.py").read_text() == "# retain nested fixture source\n"
    )  # sarj-noqa: SARJ402 -- Exact source bytes are the preservation contract after a refused native removal; parsing would miss changed comments.
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_replaced_yarn_cache_inode_after_native_open_never_authorizes_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    publish_generated_artifact_rules(repository)
    entry = repository.linked("replaced-yarn-inode")
    state = entry.path / ".yarn/install-state.gz"
    state.parent.mkdir()
    old = gzip.compress(b"preserved old native graph")
    state.write_bytes(old)
    saved = state.parent / "preserved-graph.gz"
    native = os.open
    replaced = False

    def replace_after_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal replaced
        descriptor = native(path, flags, mode, dir_fd=dir_fd)
        if path == state and not replaced:
            replaced = True
            state.rename(saved)
            state.write_bytes(gzip.compress(b"new native graph"))
        return descriptor

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Swap the real fixture inode after native open returns; the production descriptor and all identity checks still execute unchanged.
        os, "open", replace_after_open
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert replaced
    assert len(page) == 1
    assert not page[0].attempted
    assert not page[0].removed
    assert saved.read_bytes() == old
    assert state.is_file()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("primary", [True, False], ids=["private-primary-group", "different-primary-group"])
def test_group_writable_yarn_state_uses_the_existing_private_ancestor_policy(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, primary: bool
) -> None:
    publish_generated_artifact_rules(repository)
    entry = repository.linked("group-yarn-state")
    state = entry.path / ".yarn/install-state.gz"
    state.parent.mkdir()
    state.write_bytes(gzip.compress(b"generated native graph"))
    state.chmod(0o664)
    actual_group = os.getgid()
    isolate_native_git(repository, monkeypatch)
    if not primary:

        def different_group() -> int:
            return actual_group + 1

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model only the kernel primary-group identity, keeping actual fixture ownership and all production path checks intact.
            os, "getgid", different_group
        )
    page = cleanup.reap(repository.paths, apply=True)
    assert len(page) == 1
    assert page[0].removed is primary
    assert page[0].attempted is primary
    assert entry.path.exists() is not primary
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("nested", [False, True], ids=["complete-large-cache", "nested-source-after-large-cache"])
def test_large_native_dependency_cache_is_fully_inspected_beyond_the_old_entry_limit(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, nested: bool
) -> None:
    entry = repository.linked("large-native-cache")
    cache = entry.path / "node_modules"
    cache.mkdir()
    for number in range(40_000):
        (cache / f"cache-{number:05}.json").touch()
    if nested:
        project = cache / "unpacked-project"
        project.mkdir()
        cleanup.git(project, "init", "-q")
        (project / "source.py").write_text("# preserve native nested source\n")
    isolate_native_git(repository, monkeypatch)
    page = cleanup.reap(repository.paths, apply=True)
    assert len(page) == 1
    assert page[0].removed is not nested
    assert page[0].attempted is not nested
    assert entry.path.exists() is nested
    if nested:
        assert (
            (cache / "unpacked-project/source.py").read_text() == "# preserve native nested source\n"
        )  # sarj-noqa: SARJ402 -- Verify every original nested source byte survives the refused removal, including its comment.
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_generated_scan_deadline_never_turns_an_unfinished_cache_into_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("slow-native-cache")
    cache = entry.path / "node_modules"
    cache.mkdir()
    (cache / "cache.json").write_text("{}\n")
    clock = SweepClock()
    native = os.scandir

    def slow_native_scan(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes] = ".",
    ) -> AbstractContextManager[Iterator[os.DirEntry[str] | os.DirEntry[bytes]]]:
        opened = native(path)
        if path == cache:
            clock.elapsed = cleanup.GENERATED_SECONDS + 1
        return opened

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance only the OS monotonic clock at a real directory-open boundary; do not replace the scan result or eligibility logic.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Delegate actual directory traversal and move only the modeled clock after the private cache directory opens.
        os, "scandir", slow_native_scan
    )
    page = cleanup.reap(repository.paths, apply=True)
    assert page == [cleanup.Decision(str(entry.path), "inspection failed; retained")]
    assert json.loads((cache / "cache.json").read_text()) == {}
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


@pytest.mark.parametrize("directory", [False, True], ids=["file-entry-flood", "pending-directory-flood"])
def test_native_directory_entry_flood_stops_at_the_generated_scan_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: bool
) -> None:
    cache = tmp_path / "node_modules"
    cache.mkdir()
    if directory:
        (cache / "nested").mkdir()
    else:
        (cache / "cache.json").write_text("{}\n")
    native = os.scandir
    observed = 0

    @contextmanager
    def native_entry_flood(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes] = ".",
    ) -> Generator[Iterator[os.DirEntry[str] | os.DirEntry[bytes]]]:
        nonlocal observed
        with native(path) as entries:
            if path != cache:
                yield entries
                return
            actual = next(entries)

            def records() -> Iterator[os.DirEntry[str] | os.DirEntry[bytes]]:
                nonlocal observed
                while True:
                    observed += 1
                    yield actual

            yield records()

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Simulate an unending OS directory response using a real private DirEntry; the unchanged application budget must reject incomplete traversal.
        os, "scandir", native_entry_flood
    )
    with pytest.raises(RuntimeError, match="Generated files could not be proven free"):
        cleanup.generated_source(cache)
    limit = cleanup.GENERATED_DIRECTORIES if directory else cleanup.GENERATED_ENTRIES
    assert observed == limit + 1
    if directory:
        assert (cache / "nested").is_dir()
    else:
        assert json.loads((cache / "cache.json").read_text()) == {}


def test_overdue_empty_generated_directory_never_becomes_an_absence_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = tmp_path / "node_modules"
    cache.mkdir()
    native = os.scandir
    clock = SweepClock()

    def delayed_empty_scan(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes] = ".",
    ) -> AbstractContextManager[Iterator[os.DirEntry[str] | os.DirEntry[bytes]]]:
        opened = native(path)
        if path == cache:
            clock.elapsed = cleanup.GENERATED_SECONDS + 1
        return opened

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Control the OS monotonic clock while an actual empty directory response consumes the scan's time budget.
        time, "monotonic", clock.current
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve the native empty iterator; only its modeled I/O duration changes.
        os, "scandir", delayed_empty_scan
    )
    with pytest.raises(RuntimeError, match="Generated files could not be proven free"):
        cleanup.generated_source(cache)
    assert cache.is_dir()


def test_coded_probe_failure_preserves_native_budget_cursor_and_branch_until_fresh_recovery(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("classified-probe-retained")
    isolate_native_git(repository, monkeypatch)
    failed = True
    corrupt = False

    def external_references(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        if failed:
            error = cleanup_inspection.ProbeUnavailableError("worker-maps-too-large")
            if corrupt:
                vars(error)["reason"] = "private-diagnostic-must-never-be-printed"
            raise error
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The external live-reference inventory yields a typed socket failure then a fresh complete inventory; native Git, eligibility, removal and state paths execute unchanged.
        cleanup, "references", external_references
    )
    state = cleanup.state_directory(repository.paths)
    first = cleanup.reap(repository.paths, apply=True)
    assert first == [
        cleanup.Decision(
            str(entry.path),
            "live reference inspection failed: read-only process probe unavailable (worker-maps-too-large); retained",
        )
    ]
    assert entry.path.is_dir()
    assert cleanup_state.review_cursor(state, repository.paths.code) is None
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    corrupt = True
    corrupt_report = cleanup.reap(repository.paths, apply=True)
    assert corrupt_report == [
        cleanup.Decision(
            str(entry.path), "live reference inspection failed: read-only process probe unavailable; retained"
        )
    ]
    assert "private-diagnostic" not in repr(corrupt_report)
    assert entry.path.is_dir()
    assert cleanup_state.review_cursor(state, repository.paths.code) is None
    assert not (state / "cleanup-attempts.json").exists()
    failed = False
    second = cleanup.reap(repository.paths, apply=True)
    assert len(second) == 1
    assert second[0].removed
    assert second[0].attempted
    assert not entry.path.exists()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert len(cleanup_state.active_receipts(state, cleanup_state.AttemptStamp.current()).receipts) == 1
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == entry.path


def test_capacity_recovery_removes_native_checkouts_beyond_the_full_previous_daily_limit(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(name) for name in ("after-old-limit-a", "after-old-limit-b", "after-old-limit-c")]
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Kernel capacity counters select recovery without allocating or filling the shared filesystem.
        os, "statvfs", full_filesystem
    )
    state = cleanup.state_directory(repository.paths)
    stamp = cleanup_state.AttemptStamp.current()
    for number in range(48):
        assert cleanup_state.reserve_attempt(
            state, stamp, repository.paths.code / str(number), entries[0].head, limit=48
        )
    ledger = state / "cleanup-attempts.json"
    marker = state / "cleanup-attempts-initialized"
    before = cleanup_state.AttemptHistory.model_validate_json(ledger.read_bytes()).root
    marker_before = marker.read_bytes()
    first = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
    assert sum(row.attempted for row in first) == sum(row.removed for row in first) == 2
    assert sum(entry.path.exists() for entry in entries) == 1
    second = cleanup.reap(repository.paths, apply=True, capacity_recovery=True)
    assert sum(row.attempted for row in second) == sum(row.removed for row in second) == 1
    after = cleanup_state.AttemptHistory.model_validate_json(ledger.read_bytes()).root
    assert len(after) == 51
    assert after[:48] == before
    assert marker.read_bytes() == marker_before
    assert all(not entry.path.exists() for entry in entries)
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


def test_native_inventory_survives_large_audit_output(repository: Repository) -> None:
    entry = repository.linked("after-large-audit")
    output = repository.paths.code / "audits" / "completed-output"
    output.mkdir(parents=True)
    for number in range(32769):
        (output / f"receipt-{number}.json").touch()
    assert cleanup.inventory(repository.paths.code) == [entry]
    assert len(tuple(output.iterdir())) == 32769
    assert entry.path.is_dir()


@pytest.mark.parametrize(
    ("bound", "kind"),
    [
        ("MAX_DISCOVERY_ENTRIES", "entries"),
        ("MAX_DIRECTORIES", "directories"),
        ("DISCOVERY_SECONDS", "deadline"),
        ("MAX_WORKTREES", "worktrees"),
    ],
)
def test_inventory_limit_cli_reports_fixed_failure_without_touching_worktrees(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, bound: str, kind: str
) -> None:
    entry = repository.linked("retained-at-discovery-limit")
    monkeypatch.setenv("HOME", str(repository.paths.home))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exhaust one configuration budget while native repository discovery and CLI error handling remain real.
        cleanup, bound, 0
    )
    result = CliRunner().invoke(cleanup.app, ["run", "--apply"])
    assert result.exit_code == 1
    failure = cleanup.InventoryFailure.model_validate_json(result.stdout)
    assert failure.kind == kind
    assert failure.stage == "repository-discovery"
    assert failure.version == 1
    assert str(repository.paths.home) not in result.stdout
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-cursor.json").exists()


def unexpected_activity_census(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
    pytest.fail("Invalid checkout state must retain before an expensive native activity census")


@pytest.mark.parametrize("marker", cleanup.OPERATIONS)
def test_git_operation_prefilter_retains_before_expensive_activity(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    entry = repository.linked("early-operation")
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    operation = directory / marker
    operation.write_text("preserve native operation evidence\n")
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Deny the external process/terminal census; every Git metadata and retention decision remains native.
        cleanup, "references", unexpected_activity_census
    )
    results = cleanup.reap(repository.paths, apply=True)
    assert len(results) == 1
    assert results[0].reason == "Git operation or index update is in progress"
    assert not results[0].attempted
    assert not results[0].removed
    assert operation.read_text() == "preserve native operation evidence\n"
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


def test_recent_index_prefilter_retains_before_expensive_activity(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("recent-admin-metadata")
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    os.utime(directory / "index", (repository.now, repository.now))
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Recent native administrative metadata must avoid the external process/terminal census.
        cleanup, "references", unexpected_activity_census
    )
    result = cleanup.reap(repository.paths, apply=True)
    assert len(result) == 1
    assert result[0].reason == "worktree checkout or Git metadata is too recent"
    assert not result[0].attempted
    assert not result[0].removed
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head


def test_default_refresh_rejects_git_operation_before_live_census(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("operation-before-refresh")
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    (directory / "index.lock").write_text("preserve operation\n")
    fetch = repository.main / ".git/FETCH_HEAD"
    before = fetch.read_bytes(), fetch.stat().st_mtime_ns
    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A native operation discovered at refresh must reject before the external process/terminal census.
        cleanup, "references", unexpected_activity_census
    )
    refreshed: set[Path] = set()
    reason = cleanup.refresh_review(
        entry, repository.paths, min_age_days=7, refreshed=refreshed, merge_views={}, final_deadline=None
    )
    assert reason == "Git operation or index update is in progress"
    assert not refreshed
    assert (fetch.read_bytes(), fetch.stat().st_mtime_ns) == before
    assert entry.path.is_dir()


def test_operation_started_after_prefilter_is_rechecked_before_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = repository.linked("late-operation")
    directory = Path(cleanup.git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    calls: list[Path] = []

    def operation_during_census(_paths: RuntimePaths, _candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(directory)
        (directory / "index.lock").write_text("operation started after negative-only shortcut\n")
        return cleanup.References(frozenset())

    isolate_native_git(repository, monkeypatch)
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The external census boundary starts a real private Git operation after the shortcut; final eligibility remains untouched.
        cleanup, "references", operation_during_census
    )
    result = cleanup.reap(repository.paths, apply=True)
    assert calls
    assert len(result) == 1
    assert result[0].reason == "Git operation or index update is in progress"
    assert not result[0].attempted
    assert not result[0].removed
    assert entry.path.is_dir()
    assert cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


REFERENCE_KINDS = (
    "process-path",
    "process-argument",
    "docker-parent",
    "docker-child",
    "saved-parent",
    "saved-child",
    "storage-parent",
    "storage-child",
)


def held_reference_fixture(entries: Sequence[cleanup.Worktree], kind: str) -> cleanup.References:
    children = frozenset(entry.path / "source.txt" for entry in entries)
    parent = frozenset({entries[0].path.parent})
    match kind:
        case "process-path":
            return cleanup.References(children)
        case "process-argument":
            return cleanup.References(frozenset(), tuple(f"--cwd={entry.path}" for entry in entries))
        case "docker-parent":
            return cleanup.References(frozenset(), mounted=parent)
        case "docker-child":
            return cleanup.References(frozenset(), mounted=children)
        case "saved-parent":
            return cleanup.References(frozenset(), workspaces=parent)
        case "saved-child":
            return cleanup.References(frozenset(), workspaces=children)
        case "storage-parent":
            return cleanup.References(frozenset(), storage=parent)
        case "storage-child":
            return cleanup.References(frozenset(), storage=children)
        case _:
            pytest.fail("Unknown reference fixture")


@pytest.mark.parametrize("kind", REFERENCE_KINDS)
@pytest.mark.parametrize("capacity", [False, True], ids=["routine", "capacity"])
def test_saved_reference_hints_retain_native_page_with_one_census(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, kind: str, capacity: bool
) -> None:
    entries = [repository.linked(f"held-{index}") for index in range(6)]
    isolate_native_git(repository, monkeypatch)
    if capacity:
        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Use deterministic full-disk counters for private Git fixtures without filling the shared VM.
            os, "statvfs", full_filesystem
        )
    calls: list[Sequence[Path] | None] = []

    def native_reference_snapshot(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        return held_reference_fixture(entries, kind)

    original = cleanup.git
    later_paths = {entry.path for entry in entries[1:]}

    def retained_checkout_boundary(path: Path, *arguments: str, timeout: float = 15) -> str:
        if calls and path in later_paths:
            pytest.fail("A complete retained-reference hint must avoid further native checkout inspection")
        return original(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count delegated native checkout work after a complete retaining snapshot; no eligibility or native Git proof is replaced.
        cleanup, "git", retained_checkout_boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model complete process/workspace boundary evidence while exercising real native inventory, retention and Git state.
        cleanup, "references", native_reference_snapshot
    )
    report = cleanup.reap(repository.paths, apply=True, capacity_recovery=capacity)
    assert len(report) == len(entries)
    assert all("referenced" in row.reason and not row.attempted and not row.removed for row in report)
    assert len(calls) == 1
    assert all(
        entry.path.is_dir() and cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head
        for entry in entries
    )
    state = cleanup.state_directory(repository.paths)
    assert not (state / "cleanup-attempts.json").exists()
    assert not (state / "cleanup-attempts-initialized").exists()
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == entries[-1].path


@pytest.mark.parametrize("mutation", ["tracked", "env", "assume-unchanged", "skip-worktree", "operation", "unique"])
def test_clear_reference_hint_never_skips_late_native_checkout_guards(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    first, later = [repository.linked(name) for name in ("hint-first", "hint-later")]
    isolate_native_git(repository, monkeypatch)
    calls: list[Sequence[Path] | None] = []

    def late_native_change(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        if len(calls) == 1:
            return cleanup.References(frozenset({first.path}))
        assert candidates == (later.path,)
        match mutation:
            case "tracked":
                (later.path / "source.txt").write_text("uncommitted native fixture work\n")
            case "env":
                (later.path / ".env").write_text("PRIVATE_FIXTURE_VALUE=preserve\n")
            case "assume-unchanged" | "skip-worktree":
                cleanup.git(later.path, "update-index", "--" + mutation, "source.txt")
                (later.path / "source.txt").write_text("hidden local fixture work\n")
            case "operation":
                directory = Path(cleanup.git(later.path, "rev-parse", "--absolute-git-dir").strip())
                (directory / "sequencer").mkdir()
            case "unique":
                commit_fixture_file(later.path, "unique-fixture.txt")
            case _:
                pytest.fail("Unknown late checkout mutation")
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Mutate actual checkout state during the second native reference boundary; no eligibility, Git operation, ancestry or status proof is replaced.
        cleanup, "references", late_native_change
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert len(calls) == len(report) == 2
    assert all(not row.attempted and not row.removed for row in report)
    assert first.path.is_dir()
    assert later.path.is_dir()
    assert (
        cleanup.git(repository.main, "rev-parse", later.branch).strip()
        == cleanup.git(later.path, "rev-parse", "HEAD").strip()
    )
    assert not (cleanup.state_directory(repository.paths) / "cleanup-attempts.json").exists()


@pytest.mark.parametrize("report_only", [False, True], ids=["next-apply", "next-report"])
def test_retention_hints_do_not_survive_another_native_reap(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, report_only: bool
) -> None:
    entries = [repository.linked(name) for name in ("released-first", "released-second")]
    isolate_native_git(repository, monkeypatch)
    held = True
    calls: list[Sequence[Path] | None] = []

    def changing_native_snapshot(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        return held_reference_fixture(entries, "saved-child") if held else cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Release synthetic saved-workspace evidence between public native reap calls while retaining all actual Git proofs.
        cleanup, "references", changing_native_snapshot
    )
    first = cleanup.reap(repository.paths, apply=True)
    assert all("referenced" in row.reason for row in first)
    assert len(calls) == 1
    held = False
    second = cleanup.reap(repository.paths, apply=not report_only)
    assert len(second) == 2
    assert all(row.removed is not report_only for row in second)
    assert len(calls) == (2 if report_only else 3)
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


def test_clear_hints_preserve_one_fresh_singleton_census_per_native_removal(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entries = [repository.linked(f"clear-{index}") for index in range(3)]
    isolate_native_git(repository, monkeypatch)
    calls: list[Sequence[Path] | None] = []

    def clear_native_snapshot(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe every real fixture removal's process-query scope; an earlier empty reply must never eliminate a later fresh census.
        cleanup, "references", clear_native_snapshot
    )
    report = cleanup.reap(repository.paths, apply=True)
    assert sum(row.removed for row in report) == 2
    assert calls == [(entry.path,) for entry in entries[:2]]
    assert entries[-1].path.is_dir()
    assert all(cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in entries)


def test_fresh_probe_failure_after_a_clear_hint_preserves_progress_and_recovers(
    repository: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, later = [repository.linked(name) for name in ("failure-first", "failure-later")]
    isolate_native_git(repository, monkeypatch)
    calls: list[Sequence[Path] | None] = []

    def interrupted_native_snapshot(
        _paths: RuntimePaths, candidates: Sequence[Path] | None = None
    ) -> cleanup.References:
        calls.append(candidates)
        if len(calls) == 1:
            return cleanup.References(frozenset({first.path}))
        if len(calls) == 2:
            reason: cleanup_inspection.ProbeFailureReason = "worker-incomplete"
            raise cleanup_inspection.ProbeUnavailableError(reason)
        return cleanup.References(frozenset())

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Fail the later native reference boundary after an earlier complete snapshot, then permit a fresh public retry.
        cleanup, "references", interrupted_native_snapshot
    )
    initial = cleanup.reap(repository.paths, apply=True)
    assert len(initial) == 2
    assert "worker-incomplete" in initial[-1].reason
    assert all(not row.attempted and not row.removed for row in initial)
    state = cleanup.state_directory(repository.paths)
    cursor = cleanup_state.review_cursor(state, repository.paths.code)
    assert cursor is not None
    assert cursor.path == first.path
    assert first.path.is_dir()
    assert later.path.is_dir()
    assert not (state / "cleanup-attempts.json").exists()
    recovered = cleanup.reap(repository.paths, apply=True)
    assert sum(row.removed for row in recovered) == 2
    assert len(calls) == 4
    assert all(
        cleanup.git(repository.main, "rev-parse", entry.branch).strip() == entry.head for entry in (first, later)
    )


@pytest.mark.parametrize("proof", ["stale", "missing-default"])
def test_retention_hint_from_refresh_preflight_prevents_repeated_censuses_and_fetches(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, proof: str
) -> None:
    entries = [repository.linked(f"refresh-held-{index}") for index in range(4)]
    if proof == "stale":
        stale = repository.now - 2 * cleanup.DAY
        os.utime(repository.main / ".git/FETCH_HEAD", (stale, stale))
    else:
        origin = Path(cleanup.git(repository.main, "remote", "get-url", "origin").strip())
        cleanup.git(origin, "update-ref", "refs/heads/feature-only", entries[0].head)
        cleanup.git(repository.main, "fetch", "origin", "refs/heads/feature-only")
        os.utime(repository.main / ".git/FETCH_HEAD", (repository.now, repository.now))
    isolate_native_git(repository, monkeypatch)
    calls: list[Sequence[Path] | None] = []
    original = cleanup.git

    def reference_boundary(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        return held_reference_fixture(entries, "saved-child")

    def native_fetch_boundary(path: Path, *arguments: str, timeout: float = 15) -> str:
        if arguments[0] == "fetch":
            pytest.fail("A retained saved workspace cannot authorize a default-branch fetch")
        return original(path, *arguments, timeout=timeout)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply complete saved-workspace evidence during a real stale-default Git preflight.
        cleanup, "references", reference_boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe delegated native Git calls and reject any fetch after a real saved-workspace veto.
        cleanup, "git", native_fetch_boundary
    )
    before = (repository.main / ".git/FETCH_HEAD").read_bytes()
    report = cleanup.reap(repository.paths, apply=True)
    assert len(report) == 4
    assert len(calls) == 1
    assert all("referenced" in row.reason and not row.attempted for row in report)
    assert (repository.main / ".git/FETCH_HEAD").read_bytes() == before
    assert all(
        entry.path.is_dir() and original(repository.main, "rev-parse", entry.branch).strip() == entry.head
        for entry in entries
    )


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires Linux process reference files")
def test_a_clear_singleton_snapshot_cannot_hide_another_childs_real_open_file(
    repository: Repository, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    first, later = [repository.linked(name) for name in ("fd-first", "fd-later")]
    isolate_native_git(repository, monkeypatch)
    script = "import os,sys;held=open('source.txt');os.chdir(sys.argv[1]);print('ready',flush=True);sys.stdin.read()"
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path)],
        cwd=later.path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    calls: list[Sequence[Path] | None] = []

    def native_child_reference(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        calls.append(candidates)
        if len(calls) == 1:
            return cleanup.References(frozenset({first.path}))
        assert candidates == (later.path,)
        return cleanup.process_reference(Path("/proc") / str(child.pid))

    try:
        assert child.stdout is not None
        assert child.stdout.readline() == "ready\n"
        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve actual child process FD inspection while modeling the earlier singleton probe scope, which did not include the later checkout.
            cleanup, "references", native_child_reference
        )
        report = cleanup.reap(repository.paths, apply=True)
        assert len(report) == len(calls) == 2
        assert all("referenced" in row.reason and not row.attempted and not row.removed for row in report)
        assert first.path.is_dir()
        assert later.path.is_dir()
    finally:
        if child.stdin is not None:
            child.stdin.close()
        child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
