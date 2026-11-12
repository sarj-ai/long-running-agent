from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import struct
import time
from typing import TYPE_CHECKING

from long_running_agent.runtime import cleanup, cleanup_artifacts, cleanup_webpack_cache
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="native cache maintenance requires Linux process identities and mounts"
)

NATIVE_PACKS = (
    (
        "0.pack",
        ("d3BjAQEAAAAZAAAAAhCAAgGSbmF0aXZlIGNoaWxkIGNhY2hlFw=="),
        "05d6742908c93cb7a9be55fd102ae71b98236c3c3498475e28b7f33287299869",
    ),
    (
        "index.pack",
        (
            "d3BjAQMAAABFAAAA9////wEAAAACEJ5scmEtbWFpbnRlbmFuY2UtbmF0aXZlLWZpeHR1cmWEcGFja55uYXRpdmUg"
            "V2VicGFjayBjbGVhbnVwIGZpeHR1cmUlAAAAAAAAADAX"
        ),
        "e3b7b08bbc9183869bceaf049d5eb3f60b74a0295883de35468ff3021699fbd2",
    ),
)


@dataclass(frozen=True)
class Fixture:
    paths: RuntimePaths
    root: Path
    target: Path
    now: float


def cache_files(target: Path) -> None:
    target.mkdir(parents=True)
    for name, encoded, digest in NATIVE_PACKS:
        payload = base64.b64decode(encoded)
        assert hashlib.sha256(payload).hexdigest() == digest
        (target / name).write_bytes(payload)


@pytest.fixture
def webpack_cache(tmp_path: Path) -> Fixture:
    paths = RuntimePaths(tmp_path / "home")
    root = paths.code / "worktrees/project"
    root.mkdir(parents=True)
    cleanup.git(root, "init", "-q", "-b", "unmerged")
    (root / ".gitignore").write_text(".next/\n")
    (root / "source.txt").write_text("committed authored source\n")
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
        "private fixture",
    )
    target = root / cleanup_webpack_cache.PATHS[0]
    cache_files(target)
    return Fixture(paths, root, target, time.time() + 3 * cleanup.DAY)


def census(fixture: Fixture, monkeypatch: pytest.MonkeyPatch, references: cleanup.References) -> None:
    def boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == fixture.paths
        assert candidates == (fixture.root,)
        return references

    def clock() -> float:
        return fixture.now

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only the external host census; native files, Git checks, format validation and scoped removal remain real.
        cleanup, "references", boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Age native ctime with the wall clock; eligibility predicates and observed filesystem metadata remain real.
        time, "time", clock
    )


@pytest.mark.parametrize("relative", cleanup_webpack_cache.PATHS)
def test_native_webpack_cache_is_discovered_and_validated(webpack_cache: Fixture, relative: str) -> None:
    fixture = webpack_cache
    target = fixture.root / relative
    if target != fixture.target:
        fixture.target.rename(target)
    assert (fixture.root, target) in cleanup_artifacts.candidates((fixture.root.parent,))
    observed = cleanup_artifacts.inspect(fixture.root, target, now=fixture.now)
    assert observed.allocated == sum((target / name).stat().st_blocks * 512 for name, _, _ in NATIVE_PACKS)


@pytest.mark.parametrize("protection", ["dirty", "unmerged", "locked"])
def test_native_webpack_cache_removal_preserves_parent_work(
    webpack_cache: Fixture, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    fixture = webpack_cache
    if protection == "dirty":
        (fixture.root / "source.txt").write_text("uncommitted authored edit\n")
        (fixture.root / "private.env").write_text("private data stays\n")
    elif protection == "locked":
        linked = fixture.root.parent / "locked-project"
        cleanup.git(fixture.root, "worktree", "add", "--lock", "-b", "locked", str(linked))
        relative = fixture.target.relative_to(fixture.root)
        (linked / relative.parent).mkdir(parents=True)
        shutil.move(str(fixture.target), str(linked / relative))
        fixture = Fixture(fixture.paths, linked, linked / relative, fixture.now)
    head = cleanup.git(fixture.root, "rev-parse", "HEAD").strip()
    source = (fixture.root / "source.txt").read_bytes()
    observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
    census(fixture, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(fixture.paths, observed) == "removed"
    assert not fixture.target.exists()
    assert fixture.root.is_dir()
    assert (fixture.root / "source.txt").read_bytes() == source
    assert cleanup.git(fixture.root, "rev-parse", "HEAD").strip() == head
    if protection == "dirty":
        assert (fixture.root / "private.env").read_text() == "private data stays\n"
    if protection == "locked":
        assert "locked" in cleanup.git(fixture.root.parent / "project", "worktree", "list", "--porcelain")


@pytest.mark.parametrize("kind", ["process", "workspace", "terminal", "container", "queued"])
def test_live_references_retain_native_webpack_cache(
    webpack_cache: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    fixture = webpack_cache
    references = {
        "process": cleanup.References(frozenset({fixture.target / "0.pack"})),
        "workspace": cleanup.References(frozenset(), workspaces=frozenset({fixture.root})),
        "terminal": cleanup.References(frozenset(), arguments=(str(fixture.root),)),
        "container": cleanup.References(frozenset(), mounted=frozenset({fixture.root})),
        "queued": cleanup.References(frozenset(), arguments=(f"check --cwd={fixture.root}",)),
    }
    observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
    census(fixture, monkeypatch, references[kind])
    assert "referenced" in cleanup_artifacts.remove(fixture.paths, observed)
    assert (fixture.target / "index.pack").is_file()


@pytest.mark.parametrize("kind", ["disposable", "process", "workspace", "terminal", "container", "queued"])
def test_native_webpack_cache_without_git_preserves_private_and_active_parent_work(
    webpack_cache: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = webpack_cache.root.parent / "completed-build"
    target = root / cleanup_webpack_cache.PATHS[0]
    cache_files(target)
    private = root / "private.env"
    private.write_text("private authored data stays")
    source = root / "source.txt"
    source.write_text("authored build source stays")
    fixture = Fixture(webpack_cache.paths, root, target, webpack_cache.now)
    references = {
        "disposable": cleanup.References(frozenset()),
        "process": cleanup.References(frozenset({target / "0.pack"})),
        "workspace": cleanup.References(frozenset(), workspaces=frozenset({root})),
        "terminal": cleanup.References(frozenset(), arguments=(str(root),)),
        "container": cleanup.References(frozenset(), mounted=frozenset({root})),
        "queued": cleanup.References(frozenset(), arguments=(f"check --cwd={root}",)),
    }
    observed = cleanup_artifacts.inspect(root, target, now=fixture.now)
    census(fixture, monkeypatch, references[kind])
    reason = cleanup_artifacts.remove(fixture.paths, observed)
    if kind == "disposable":
        assert reason == "removed"
        assert not target.exists()
    else:
        assert "referenced" in reason
        assert (target / "index.pack").is_file()
    assert private.read_text() == "private authored data stays"
    assert source.read_text() == "authored build source stays"


@pytest.mark.parametrize("kind", ["index", "head", "broken"])
def test_native_webpack_cache_with_ancestor_or_unresolved_git_stays_protected(
    webpack_cache: Fixture, kind: str
) -> None:
    parent = webpack_cache.root / "nested-parent"
    root = parent / "completed-build"
    target = root / cleanup_webpack_cache.PATHS[0]
    cache_files(target)
    relative = str((target / "index.pack").relative_to(webpack_cache.root))
    if kind == "broken":
        (parent / ".git").write_text(f"gitdir: {parent / 'missing-metadata'}\n")
    else:
        cleanup.git(webpack_cache.root, "add", "-f", "--", relative)
        if kind == "head":
            cleanup.git(
                webpack_cache.root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "tracked native fixture",
            )
            cleanup.git(webpack_cache.root, "rm", "--cached", "--", relative)
    with pytest.raises(ValueError, match="Git status is unverified"):
        cleanup_artifacts.inspect(root, target, now=webpack_cache.now)
    assert (target / "index.pack").is_file()
    assert (webpack_cache.root / "source.txt").read_text() == "committed authored source\n"


@pytest.mark.parametrize(
    "kind",
    [
        "private",
        "source",
        "symlink",
        "directory",
        "magic",
        "version",
        "truncated",
        "sections",
        "dangling",
        "pointer",
        "pointer-size",
        "orphan",
        "compressed",
        "writable",
    ],
)
def test_unrecognized_webpack_pack_sets_are_retained(webpack_cache: Fixture, kind: str) -> None:
    target = webpack_cache.target
    index = target / "index.pack"
    child = target / "0.pack"
    match kind:
        case "private":
            (target / ".env").write_text("private data")
        case "source":
            (target / "source.js").write_text("authored source")
        case "symlink":
            outside = webpack_cache.root / "outside.pack"
            index.rename(outside)
            index.symlink_to(outside)
        case "directory":
            child.unlink()
            child.mkdir()
        case "magic":
            index.write_bytes(b"data" + index.read_bytes()[4:])
        case "version":
            index.write_bytes(b"wpc\x02" + index.read_bytes()[4:])
        case "truncated":
            index.write_bytes(index.read_bytes()[:-1])
        case "sections":
            index.write_bytes(index.read_bytes()[:4] + struct.pack("<I", 2**32 - 1) + index.read_bytes()[8:])
        case "dangling":
            child.unlink()
        case "pointer" | "pointer-size":
            payload = index.read_bytes()
            pointer = struct.pack("<Q", child.stat().st_size) + b"0"
            assert payload.count(pointer) == 1
            replacement = (
                pointer[:-1] + b"/" if kind == "pointer" else struct.pack("<Q", child.stat().st_size + 1) + b"0"
            )
            index.write_bytes(payload.replace(pointer, replacement))
        case "orphan":
            (target / "1.pack").write_bytes(child.read_bytes())
        case "compressed":
            index.rename(target / "index.pack.gz")
        case "writable":
            index.chmod(0o666)
        case _:
            pytest.fail(f"Unrecognized fixture mutation: {kind}")
    with pytest.raises(ValueError, match=r"unsafe|unrecognized"):
        cleanup_artifacts.inspect(webpack_cache.root, target, now=webpack_cache.now)
    assert target.is_dir()


def test_recent_native_webpack_cache_is_retained(webpack_cache: Fixture) -> None:
    with pytest.raises(ValueError, match="recent"):
        cleanup_artifacts.inspect(webpack_cache.root, webpack_cache.target, now=time.time())


@pytest.mark.parametrize("staged_removal", [False, True])
def test_tracked_native_webpack_files_are_retained(webpack_cache: Fixture, staged_removal: bool) -> None:
    fixture = webpack_cache
    relative = str((fixture.target / "index.pack").relative_to(fixture.root))
    cleanup.git(fixture.root, "add", "-f", "--", relative)
    if staged_removal:
        cleanup.git(
            fixture.root,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "tracked native fixture",
        )
        cleanup.git(fixture.root, "rm", "--cached", "--", relative)
    with pytest.raises(ValueError, match=r"not ignored|tracked"):
        cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)


def test_shared_native_webpack_pack_is_preserved_outside_the_cache(
    webpack_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = webpack_cache
    outside = fixture.root / "shared.pack"
    os.link(fixture.target / "0.pack", outside)
    expected = outside.read_bytes()
    observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
    assert observed.allocated == (fixture.target / "index.pack").stat().st_blocks * 512
    census(fixture, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(fixture.paths, observed) == "removed"
    assert outside.read_bytes() == expected


@pytest.mark.parametrize("budget", ["files", "bytes", "time", "large-file"])
def test_native_webpack_validation_stays_bounded(
    webpack_cache: Fixture, monkeypatch: pytest.MonkeyPatch, budget: str
) -> None:
    fixture = webpack_cache
    match budget:
        case "files":
            monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Lower only the resource budget; real native files and the validator still decide retention.
                cleanup_webpack_cache, "MAX_FILES", 1
            )
        case "bytes":
            monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Lower only the byte budget; real native file sizes and parsing remain unchanged.
                cleanup_webpack_cache, "MAX_BYTES", 1
            )
        case "time":
            assert not cleanup_webpack_cache.valid_files(fixture.target, time.monotonic() - 1)
            return
        case _:
            with (fixture.target / "0.pack").open("r+b") as stream:
                stream.truncate(cleanup_webpack_cache.MAX_FILE_BYTES + 1)
    assert not cleanup_webpack_cache.installed(fixture.target)


def test_native_webpack_cache_requires_its_exact_inventory_path(webpack_cache: Fixture) -> None:
    target = webpack_cache.root / "authored-pack-files"
    webpack_cache.target.rename(target)
    assert (webpack_cache.root, target) not in cleanup_artifacts.candidates((webpack_cache.root.parent,))
    with pytest.raises(ValueError, match="unrecognized provider"):
        cleanup_artifacts.inspect(webpack_cache.root, target, now=webpack_cache.now)
