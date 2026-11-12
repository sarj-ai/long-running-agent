from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys
import time
from typing import TYPE_CHECKING

from long_running_agent.runtime import cleanup, cleanup_artifacts, cleanup_daemon, cleanup_providers
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="provider maintenance requires Linux process and ELF identities"
)


@dataclass(frozen=True)
class Fixture:
    paths: RuntimePaths
    root: Path
    target: Path
    binary: Path
    now: float


@pytest.fixture
def provider(tmp_path: Path) -> Fixture:
    paths = RuntimePaths(tmp_path / "home")
    root = paths.code / "worktrees/project"
    root.mkdir(parents=True)
    cleanup.git(root, "init", "-q", "-b", "unmerged")
    (root / ".gitignore").write_text(".cache/\n.terraform/\nterraform-data/\n")
    (root / "source.txt").write_text("committed source\n")
    cleanup.git(root, "add", ".")
    cleanup.git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "private fixture commit",
    )
    target = root / ".cache/terraform/providers"
    package = target / "registry.terraform.io/hashicorp/google/7.0.0/linux_amd64"
    package.mkdir(parents=True)
    binary = package / "terraform-provider-google_v7.0.0_x5"
    shutil.copyfile(Path(sys.executable).resolve(), binary)
    binary.chmod(0o755)
    (package / "LICENSE.txt").write_text("fixture package license\n")
    return Fixture(paths, root, target, binary, time.time() + 3 * cleanup.DAY)


def census(provider: Fixture, monkeypatch: pytest.MonkeyPatch, references: cleanup.References) -> None:
    def native_boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == provider.paths
        assert candidates == (provider.root,)
        return references

    def clock() -> float:
        return provider.now

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only the host census; private Git, provider validation and inode-scoped removal execute normally.
        cleanup, "references", native_boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance the OS wall clock to age native ctime without changing source or eligibility decisions.
        time, "time", clock
    )


@pytest.mark.parametrize("protection", ["dirty", "unmerged", "locked"])
def test_provider_downloads_are_independent_of_source_retention(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    root = provider.root
    if protection == "dirty":
        (root / "source.txt").write_text("uncommitted authored change\n")
        (root / "private.env").write_text("private fixture data\n")
    elif protection == "locked":
        linked = root.parent / "locked-project"
        cleanup.git(root, "worktree", "add", "--lock", "-b", "locked", str(linked))
        shutil.move(str(provider.target.parent.parent), str(linked / ".cache"))
        target = linked / ".cache/terraform/providers"
        provider = Fixture(
            provider.paths, linked, target, target / provider.binary.relative_to(provider.target), provider.now
        )
        root = linked
    head = cleanup.git(root, "rev-parse", "HEAD").strip()
    source = (root / "source.txt").read_bytes()
    observed = cleanup_artifacts.inspect(root, provider.target, now=provider.now)
    census(provider, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(provider.paths, observed) == "removed"
    assert not provider.target.exists()
    assert (root / "source.txt").read_bytes() == source
    assert cleanup.git(root, "rev-parse", "HEAD").strip() == head
    if protection == "locked":
        assert "locked" in cleanup.git(provider.root.parent / "project", "worktree", "list", "--porcelain")
    if protection == "dirty":
        assert (root / "private.env").read_text() == "private fixture data\n"


@pytest.mark.parametrize("kind", ["process", "workspace", "terminal", "container", "queued"])
def test_fresh_reference_sources_protect_provider_downloads(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    candidate = provider.root
    references = {
        "process": cleanup.References(frozenset({candidate})),
        "workspace": cleanup.References(frozenset(), workspaces=frozenset({candidate})),
        "terminal": cleanup.References(frozenset(), arguments=(str(candidate),)),
        "container": cleanup.References(frozenset(), mounted=frozenset({candidate})),
        "queued": cleanup.References(frozenset(), arguments=(f"check --cwd={candidate}",)),
    }
    observed = cleanup_artifacts.inspect(candidate, provider.target, now=provider.now)
    census(provider, monkeypatch, references[kind])
    assert "referenced" in cleanup_artifacts.remove(provider.paths, observed)
    assert provider.binary.is_file()


@pytest.mark.parametrize("kind", ["private", "source", "symlink", "wrong-version", "script", "writable"])
def test_provider_validator_retains_unrecognized_or_private_payloads(provider: Fixture, kind: str) -> None:
    match kind:
        case "private":
            (provider.binary.parent / ".env").write_text("preserve private data\n")
        case "source":
            (provider.binary.parent / "src").mkdir()
        case "symlink":
            provider.binary.unlink()
            provider.binary.symlink_to(Path(sys.executable).resolve())
        case "wrong-version":
            provider.binary.rename(provider.binary.with_name("terraform-provider-google_v9.0.0"))
        case "script":
            provider.binary.write_text("preserve custom code\n")
        case "writable":
            provider.binary.parent.chmod(0o777)
        case _:
            raise AssertionError(kind)
    assert not cleanup_providers.installed(provider.target)
    assert provider.target.is_dir()


def test_recent_nested_provider_file_is_retained_even_when_root_is_old(provider: Fixture) -> None:
    os.utime(provider.binary, (provider.now, provider.now))
    with pytest.raises(ValueError, match="too recent"):
        cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert provider.binary.is_file()


def test_changed_provider_cannot_reuse_old_inspection(provider: Fixture, monkeypatch: pytest.MonkeyPatch) -> None:
    observed = cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    provider.binary.write_bytes(provider.binary.read_bytes() + b"changed")
    census(provider, monkeypatch, cleanup.References(frozenset()))
    assert "changed" in cleanup_artifacts.remove(provider.paths, observed)
    assert provider.binary.is_file()


def test_tracked_provider_file_is_never_a_disposable_download(provider: Fixture) -> None:
    cleanup.git(provider.root, "add", "-f", str(provider.binary))
    with pytest.raises(ValueError, match=r"tracked|not ignored"):
        cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert provider.binary.is_file()


@pytest.mark.parametrize("referenced", [False, True])
def test_verified_bare_primary_keeps_source_and_configuration_while_inspecting_downloads(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, referenced: bool
) -> None:
    head = cleanup.git(provider.root, "rev-parse", "HEAD").strip()
    cleanup.git(provider.root, "config", "core.bare", "true")
    observed = cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    census(provider, monkeypatch, cleanup.References(frozenset({provider.root}) if referenced else frozenset()))
    reason = cleanup_artifacts.remove(provider.paths, observed)
    if referenced:
        assert "referenced" in reason
    else:
        assert reason == "removed"
    assert provider.binary.exists() is referenced
    assert cleanup.git(provider.root, "config", "--get", "core.bare").strip() == "true"
    assert cleanup.git(provider.root, "rev-parse", "HEAD").strip() == head
    assert (provider.root / ".git/objects").is_dir()
    assert (provider.root / "source.txt").read_text() == "committed source\n"


@pytest.mark.parametrize("bare", [False, True])
def test_provider_tracked_in_head_is_retained_even_after_index_removal(provider: Fixture, bare: bool) -> None:
    cleanup.git(provider.root, "add", "-f", str(provider.binary))
    cleanup.git(
        provider.root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "private tracked provider fixture",
    )
    cleanup.git(provider.root, "rm", "--cached", str(provider.binary))
    setting = str(bare).lower()
    cleanup.git(provider.root, "config", "core.bare", setting)
    with pytest.raises(ValueError, match="tracked"):
        cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert provider.binary.is_file()
    assert cleanup.git(provider.root, "config", "--get", "core.bare").strip() == setting


def test_removed_provider_does_not_pin_checkout_through_empty_ignored_parents(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    census(provider, monkeypatch, cleanup.References(frozenset()))
    observed = cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert cleanup_artifacts.remove(provider.paths, observed) == "removed"
    assert (provider.root / ".cache/terraform").is_dir()
    assert cleanup.local_files(provider.root) == "eligible"
    assert (provider.root / "source.txt").read_text() == "committed source\n"


@pytest.mark.parametrize("extra", ["private-file", "unknown-directory", "symlink", "terraform-root"])
def test_empty_provider_parent_retention_keeps_other_ignored_payloads(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    census(provider, monkeypatch, cleanup.References(frozenset()))
    observed = cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert cleanup_artifacts.remove(provider.paths, observed) == "removed"
    match extra:
        case "private-file":
            (provider.root / ".cache/private.env").write_text("private fixture data\n")
        case "unknown-directory":
            (provider.root / ".cache/authored-empty-directory").mkdir()
        case "symlink":
            parent = provider.root / ".cache/terraform"
            parent.rmdir()
            parent.symlink_to(provider.root / "source.txt")
        case _:
            (provider.root / "terraform").mkdir()
            (provider.root / ".git/info/exclude").write_text("/terraform/\n")
    assert cleanup.local_files(provider.root) == "tracked, untracked, or non-disposable ignored local files"
    assert (provider.root / "source.txt").read_text() == "committed source\n"


@pytest.mark.parametrize("relative", cleanup_artifacts.PROVIDER_PATHS)
def test_bounded_inventory_finds_supported_provider_layouts(provider: Fixture, relative: str) -> None:
    target = provider.root / relative
    if target != provider.target:
        target.parent.mkdir(parents=True, exist_ok=True)
        provider.target.rename(target)
    assert (provider.root, target) in cleanup_artifacts.candidates((provider.root.parent,))


def test_foreign_private_temporary_root_cannot_starve_owned_provider_discovery(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    temporary = tmp_path / "temporary"
    foreign = temporary / "foreign-private-task"
    (foreign / ".cache/terraform/providers").mkdir(parents=True)
    private = foreign / "authored.txt"
    private.write_text("preserve private fixture data\n")
    owner = os.getuid()

    def other_identity() -> int:
        return owner + 1

    foreign.chmod(0)
    try:
        with monkeypatch.context() as boundary:
            boundary.setattr(  # sarj-noqa: SARJ445 -- Simulate only the OS user identity so this real unreadable private directory represents a foreign task; no fixture permission or discovery decision is mocked.
                os, "getuid", other_identity
            )
            assert cleanup_artifacts.candidates((temporary,)) == []
    finally:
        foreign.chmod(0o700)
    assert private.read_text() == "preserve private fixture data\n"
    assert (provider.root, provider.target) in cleanup_artifacts.candidates((provider.root.parent,))


def test_completed_artifact_cleanup_reports_bytes_and_source_retention(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    census(provider, monkeypatch, cleanup.References(frozenset()))
    state = cleanup.state_directory(provider.paths)
    result = cleanup_artifacts.recover(
        provider.paths, state, deadline=time.monotonic() + 20, temporary=tmp_path / "empty"
    )
    assert result.removed == 1
    assert result.reclaimed_bytes > 0
    assert result.reasons == {"removed": 1}
    assert (provider.root / "source.txt").is_file()
    summary = cleanup_daemon.Summary(worktrees=0, attempted=0, removed=0, reasons={}, cache="skipped", artifacts=result)
    schedule = cleanup_daemon.Schedule.new()
    assert schedule.completed(summary, cleanup_daemon.Space(total=100, available=90), 1, provider.now) == "cleaned"


def test_failed_artifact_inspection_is_unhealthy_even_when_daemon_is_live() -> None:
    report = cleanup_artifacts.Report(reasons={"inspection failed; retained": 1})
    summary = cleanup_daemon.Summary(worktrees=0, attempted=0, removed=0, reasons={}, cache="skipped", artifacts=report)
    assert cleanup_daemon.incomplete_inspection(summary)


def test_failed_host_probe_advances_cursor_without_repeating_or_deleting_the_candidate(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    later = provider.root.with_name("z-project")
    shutil.copytree(provider.root, later)
    calls: list[Path] = []

    def native_boundary(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert candidates is not None
        calls.extend(candidates)
        if provider.root in candidates:
            raise PermissionError
        return cleanup.References(frozenset())

    census(provider, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Fail only the authenticated OS census for the first private checkout; cursor persistence and the next native removal are exercised unchanged.
        cleanup, "references", native_boundary
    )
    state = cleanup.state_directory(provider.paths)
    first = cleanup_artifacts.recover(
        provider.paths, state, deadline=time.monotonic() + 20, temporary=tmp_path / "empty"
    )
    assert first.removed == 0
    assert first.inspected == 1
    assert provider.binary.exists()
    second = cleanup_artifacts.recover(
        provider.paths, state, deadline=time.monotonic() + 20, temporary=tmp_path / "empty"
    )
    assert second.removed == 1
    assert not (later / ".cache/terraform/providers").exists()
    assert provider.binary.exists()
    assert calls[:2] == [provider.root, later]


def test_legacy_cleanup_lock_prevents_concurrent_provider_removal(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    census(provider, monkeypatch, cleanup.References(frozenset()))
    state = cleanup.state_directory(provider.paths)
    with cleanup_artifacts.legacy_lock(provider.paths):
        report = cleanup_artifacts.recover(
            provider.paths, state, deadline=time.monotonic() + 20, temporary=tmp_path / "empty"
        )
    assert report.removed == 0
    assert report.inspected == 0
    assert provider.binary.exists()
    assert any("inspection failed" in reason for reason in report.reasons)


@pytest.mark.parametrize("location", ["root", "ancestor", "target", "descendant", "elsewhere"])
def test_provider_mount_guard_covers_project_bind_mount_ancestors(tmp_path: Path, location: str) -> None:
    root = tmp_path / "project with spaces"
    target = root / ".cache/terraform/providers"
    mounts = {
        "root": root,
        "ancestor": root / ".cache",
        "target": target,
        "descendant": target / "registry.terraform.io",
        "elsewhere": tmp_path / "other",
    }
    mount = str(mounts[location]).replace(" ", "\\040")
    table = tmp_path / "mountinfo"
    table.write_text(f"42 29 8:1 / {mount} rw - ext4 /dev/root rw\n")
    assert cleanup_artifacts.mounted(target, root=root, table=table) is (location != "elsewhere")


def test_provider_retained_when_removal_receipt_directory_is_linked(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    census(provider, monkeypatch, cleanup.References(frozenset()))
    state = cleanup.state_directory(provider.paths)
    receipts = state / "cleanup-artifact-removals"
    receipts.symlink_to(tmp_path, target_is_directory=True)
    observed = cleanup_artifacts.inspect(provider.root, provider.target, now=provider.now)
    assert cleanup_artifacts.remove(provider.paths, observed) == "cleanup receipt directory is unsafe"
    assert provider.binary.exists()


def test_failed_native_git_inspection_cannot_be_reported_as_healthy_retention(
    provider: Fixture, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (provider.root / ".git/HEAD").unlink()
    census(provider, monkeypatch, cleanup.References(frozenset()))
    state = cleanup.state_directory(provider.paths)
    report = cleanup_artifacts.recover(
        provider.paths, state, deadline=time.monotonic() + 20, temporary=tmp_path / "empty"
    )
    assert report.removed == 0
    assert provider.binary.exists()
    summary = cleanup_daemon.Summary(worktrees=0, attempted=0, removed=0, reasons={}, cache="skipped", artifacts=report)
    assert cleanup_daemon.incomplete_inspection(summary)
