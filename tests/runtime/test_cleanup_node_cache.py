from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import TYPE_CHECKING
import zlib

from long_running_agent.runtime import cleanup, cleanup_artifacts, cleanup_daemon, cleanup_node_cache
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(),
    reason="native cache maintenance requires Linux process identities and mount metadata",
)


# Produced by Node v24.22.1 enableCompileCache()/flushCompileCache(), rather than
# a second implementation of the format. SHA256 d611234505c902f9a23428422f5f7e52bd5437572381540c32c1bc84a7ae5fa5.
NATIVE_CACHE = base64.b64decode(
    "stvfikQAAACAAwAAKSkq2Qrh9qWJBt7A+owz3EQAACDx9j2Ch3P0p2ADAAAAAAAAAAAAAAEkVAMwB7RgAAAAAB8AAAABCAe9"
    "DgQEIQMQB4UBYgAAAAANAAAAAQQCRAkYHwANHwrQAQAAACEHQREhAxwHfQFgAAAAAAUAAAAHIQkHjQoBGAewYgAAAAACAAAA"
    "AAAAAAEAAAAAAAAACAAAAAe9D2AAAAAACgAAAAEkVAMMBzEPSWAqAAAAQQAAAGAAAAAA/////0lcAUgHAQ8IAAE0Umu+17ow"
    "VQAAAC92YXIvdG1wL2xyYS1kaXNrLW1haW50ZW5hbmNlLTIwMjYxMTEyL2VtcHR5LXByb3ZpZGVyLWZpeHR1cmVzL3RtcHp3"
    "Ym9ucmNpL3NvdXJjZS5janMAAABhAAAAAAAAAAAAAAAAAAAAAERiAAAAAAIAAAAAAAAAAAAAAAAAAABeAAAAARxMYAAAAAAF"
    "AAAABQAiKQQBDFJh2tI3TgcAAAByZXF1aXJlAAEMUmFC4mcnBgAAAG1vZHVsZQAAARBSYpbWhFgKAAAAX19maWxlbmFtZQAA"
    "AAAAAAEQUmL60/QdCQAAAF9fZGlybmFtZQAAAAAAAABgAAAAAAAAAAABFAdpAWAAAAAAAwAAABgBJFQDJAe0YAAAAAAFAAAA"
    "AQgHvQ4ERCEDDAeFAWEAAAAABwAAAAEEAwAIkAIAIQdBESEDDAd9AWAAAAAAAQAAAAQAYgAAAAABAAAAAAAAAAAAAACJAAAA"
    "swAAAGAAAAAA/////wEgU2RDMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABEAAAASWAAAAAA/////wEMB6Rh"
    "AAAAAAEAAAAGAAAAAAAAAAQgYgAAAQAAAAIAABAACAAAAAAXDgAAAAAAABiTGAQYRGAAAAAACgAAAIAEXURiAgADAAAAAAAL"
    "0AEAAgAAABkOAAAAAAAABQAiKQRlKAAAAAYAAgAAAAAAAAAAACMAAMwz9wECzYUCBCnLiQMAAspm+Pf29QU5BQQHDrMAYAAA"
    "AAD/////ASRTZMRxAUAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEQAAABJYAAAAAD/////RAEQlGIJAAAAAQAA"
    "AAYUMAlAAAAAAwAAAAAAAACVYgUABgAAAAIAABIAAAEAAAAYDgAAAAAAAAsKCgoKCgoKCg=="
)


NODE_26_CACHE = base64.b64decode(
    "stvfikQAAACIAwAAKSkq2VghSnepBt7AUr3/l0QAACCz9UTPMiY/5GgDAAAAAAAAAAAAAAEkUQMwB8RgAAAAAB8AAAABCAch"
    "DwQEIQMQB7UBYgAAAAANAAAAAgQHRBIYPgAaHxfQAQAAACEHhREhAxwHrQFgAAAAAAUAAAAHSQkHyQoBGAcZAWIAAAAAAgAA"
    "AAAAAAABAAAAAAAAAAgAAABIYAAAAAAKAAAAASRRAwwHzQFFYCoAAABBAAAAYAAAAAD/////RVkBTAdlDwgAATRQa9pwYUxR"
    "AAAAL3Zhci90bXAvbHJhLWRpc2stbWFpbnRlbmFuY2UtMjAyNjExMTIvbm9kZTI2LW5hdGl2ZS1maXh0dXJlLXkyb2ZlZmU1"
    "L2ZpeHR1cmUuY2pzAAAAAAAAAGEAAAAAAAAAAAAAAAAAAAAAQGIAAAAAAgAAAAAAAAAAAAAAAAAAAF8AAAABHEpgAAAAAAUA"
    "AAAFGKLQAQEMUGGOY+nGBwAAAHJlcXVpcmUAAQxQYVqI6bEGAAAAbW9kdWxlAAABEFBintIWXAoAAABfX2ZpbGVuYW1lAAAA"
    "AAAAARBQYt5X4z0JAAAAX19kaXJuYW1lAAAAAAAAAGAAAAAAAAAAAAEUV2AAAAAAAwAAABgBJFEDJAfEYAAAAAAFAAAAAQgH"
    "IQ8ERCEDDAe1AWEAAAAABwAAAAIEBgATkAIAIQeFESEDDAetAWAAAAAAAQAAAAQAYgAAAAABAAAAAAAAAAAAAACLAAAAtwAA"
    "AGAAAAAA/////wEgBz0BZEMwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEQAAABFYAAAAAD/////AQwH4GEA"
    "AAAAAQAAAAYAAAAAAAAABCBiAAABAAAAAgAAEAAIAAAAAPkaAAAAAAAAGJEYBBhAYAAAAAAKAAAAgQBbQGICAAMAAAAAAAvQ"
    "AQACAAAA+xoAAAAAAAAFGKLQAWUoAAAABgACAAAAAAAAAAAAIwAA0DP3AQLRhwIEKc+LAwACzmj49/b1BTkFBAcOtwBgAAAA"
    "AP////8BKJJkxHEBwAAAAAAAAAAABQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAARAAAAEVgAAAAAP////9AYAAAAAAbAAAAARCT"
    "YgkAAAABAAAABhQwCUAAAAADAAAAAAAAAJRiBQAGAAAAAgAAEgAAAQAAAPoaAAAAAAAACwoKCgoKCgoK"
)


NODE_26_13_CACHE = base64.b64decode(
    "stvfikQAAACAAwAAKSkq2dSn7tepBt7AUr3/l0QAACCz9UTPMiY/5GADAAAAAAAAAAAAAAEkUQMwB8RgAAAAAB8AAAABCAch"
    "DwQEIQMQB7UBYgAAAAANAAAAAgQHRBIYPgAaHxfQAQAAACEHhREhAxwHrQFgAAAAAAUAAAAHSQkHyQoBGAcZAWIAAAAAAgAA"
    "AAAAAAABAAAAAAAAAAgAAABIYAAAAAAKAAAAASRRAwwHzQFFYCoAAABBAAAAYAAAAAD/////RVkBTAdlDwgAATBQah70gr1L"
    "AAAAL3Zhci90bXAvbHJhLWRpc2stbWFpbnRlbmFuY2UtMjAyNjExMTIvbm9kZTI2MTMtbmF0aXZlLXZubXk1anViL2ZpeHR1"
    "cmUuY2pzAAAAAABhAAAAAAAAAAAAAAAAAAAAAEBiAAAAAAIAAAAAAAAAAAAAAAAAAABfAAAAARxKYAAAAAAFAAAABRii0AEB"
    "DFBh8q+aXwcAAAByZXF1aXJlAAEMUGFW7NlKBgAAAG1vZHVsZQAAARBQYvK++tYKAAAAX19maWxlbmFtZQAAAAAAAAEQUGK6"
    "mmMZCQAAAF9fZGlybmFtZQAAAAAAAABgAAAAAAAAAAABFFdgAAAAAAMAAAAYASRRAyQHxGAAAAAABQAAAAEIByEPBEQhAwwH"
    "tQFhAAAAAAcAAAACBAYAE5ACACEHhREhAwwHrQFgAAAAAAEAAAAEAGIAAAAAAQAAAAAAAAAAAAAAiwAAALcAAABgAAAAAP//"
    "//8BIAc9AWRDMAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABEAAAARWAAAAAA/////wEMB+BhAAAAAAEAAAAG"
    "AAAAAAAAAAQgYgAAAQAAAAIAABAACAAAAAD5GgAAAAAAABiRGAQYQGAAAAAACgAAAIEAW0BiAgADAAAAAAAL0AEAAgAAAPsa"
    "AAAAAAAABRii0AFlKAAAAAYAAgAAAAAAAAAAACMAANAz9wEC0YcCBCnPiwMAAs5o+Pf29QU5BQQHDrcAYAAAAAD/////ASiS"
    "ZMRxAcAAAAAAAAAAAAUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAEQAAABFYAAAAAD/////QGAAAAAAGwAAAAEQk2IJAAAAAQAA"
    "AAYUMAlAAAAAAwAAAAAAAACUYgUABgAAAAIAABIAAAEAAAD6GgAAAAAAAAsKCgoKCgoKCg=="
)


# Produced by Node v22.23.4 from the same 68-byte source as NATIVE_CACHE.
NODE_22_CACHE = base64.b64decode(
    "stvfikQAAABgAwAAKSkq2dTBhC8oBt7AdP7aeUQAAACDXHdLh1HjmUADAAAAAAAAAAAAAAEgVAMwB7RgAAAAAB8AAAABDAed"
    "CgQEYAAAAAAAAAAAHgMQB3UBYgAAAAANAAAAAQQCRAkYHwANHwrQAQAAAB4H9QweAxwHcQFgAAAAAAUAAAAHBQUHdQYBGAew"
    "YgAAAAACAAAAAAAAAAEAAAAAAAAACAAAAAeRAmAAAAAACgAAAAEgVAEMBwkLSWAqAAAAQQAAAElcAUgH4QoIAAEwUmpK/7EA"
    "SQAAAC92YXIvdG1wL2xyYS1kaXNrLW1haW50ZW5hbmNlLTIwMjYxMTEyL25vZGUyMi1uYXRpdmUtMGt1bzdnaXUvZml4dHVy"
    "ZS5janMAAAAAAAAAYQAAAAAAAAAAAAAAAAAAAABEYgAAAAACAAAAAAAAAAAAAAAAAAAATgAAAAEcTGAAAAAABQAAAAUAQgIE"
    "AQxSYbagdu8HAAAAcmVxdWlyZQABDFJhdmkNggYAAABtb2R1bGUAAAEQUmKumYfbCgAAAF9fZmlsZW5hbWUAAAAAAAABEFJi"
    "5jWrSgkAAABfX2Rpcm5hbWUAAAAAAAAAYAAAAAAAAAAAARQHWQFgAAAAAAMAAAAXASBUAyQHtGAAAAAABQAAAAEMB50KBERg"
    "AAAAAAAAAAAeAwwHdQFhAAAAAAcAAAABBAMACJACAB4H9QweAwwHcQFgAAAAAAEAAAAEAGIAAAAACAAAAAAAAAAAAAAAhQAA"
    "AK4AAAABIFNiAAAAAEEwAAAAAAAAAAAAAAAAAAAAAAAASWIAAAAA/////wAAAAAAAAAAAAAAAEQAAAABCAekYAAAAAABAAAA"
    "BCBiAAABAAAAAgAAEAAIAAAAAJYMAAAAAAAAF5MXBBhEYAAAAAAKAAAAgERdRGICAAMAAAAAAAvQAQACAAAAmAwAAAAAAAAF"
    "AEICBGUoAAAAMAAAAAAAAAAAAAAAIQAAxy/3AQLIgQIEKcaFAwACxWL49/b1BTUFBAcOrgABJFNiAAAAAMJxAQAAAAAABQAA"
    "AAAAAAAAAAAASWAAAAAA/////0RhAAAAAAAAAAAAAAAARAAAAAEMlGEJAAAAAQAAAAYUMAlAAAAAlWIFAAYAAAACAAASAAAB"
    "AAAAlwwAAAAAAAALCgoKCgoKCgo="
)


@dataclass(frozen=True)
class NativeCache:
    target: Path
    payload: Path


def native_cache(root: Path) -> NativeCache:
    target = root / "node-compile-cache"
    namespace = target / f"v24.22.1-x64-964aae3f-{os.getuid()}"
    namespace.mkdir(parents=True)
    payload = namespace / "6c008d06"
    payload.write_bytes(NATIVE_CACHE)
    return NativeCache(target, payload)


def test_native_node_compile_cache_is_discovered_independently_of_its_parent(tmp_path: Path) -> None:
    temporary = tmp_path / "temporary"
    root = temporary / "pydantic-verify.private"
    native = native_cache(root)
    (root / "private.env").write_text("authored data must stay")
    assert (root, native.target) in cleanup_artifacts.candidates((temporary,))
    observed = cleanup_artifacts.inspect(root, native.target, now=time.time() + 3 * cleanup.DAY)
    assert observed.allocated == native.payload.stat().st_blocks * 512
    assert (root / "private.env").read_text() == "authored data must stay"


@dataclass(frozen=True)
class Fixture:
    paths: RuntimePaths
    root: Path
    target: Path
    payload: Path
    now: float


@pytest.fixture
def node_cache(tmp_path: Path) -> Fixture:
    paths = RuntimePaths(tmp_path / "home")
    root = paths.code / "worktrees/project"
    root.mkdir(parents=True)
    cleanup.git(root, "init", "-q", "-b", "unmerged")
    (root / ".gitignore").write_text("node-compile-cache/\n")
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
    native = native_cache(root)
    return Fixture(paths, root, native.target, native.payload, time.time() + 3 * cleanup.DAY)


def census(fixture: Fixture, monkeypatch: pytest.MonkeyPatch, references: cleanup.References) -> None:
    def boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == fixture.paths
        assert candidates == (fixture.root,)
        return references

    def clock() -> float:
        return fixture.now

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only the external host census; native files, Git checks, content validation and scoped removal remain real.
        cleanup, "references", boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Age native ctime using the wall clock, without changing source or eligibility predicates.
        time, "time", clock
    )


@pytest.mark.parametrize("protection", ["dirty", "unmerged", "locked"])
def test_native_node_cache_removal_preserves_checkout_protections(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    fixture = node_cache
    if protection == "dirty":
        (fixture.root / "source.txt").write_text("uncommitted authored edit\n")
        (fixture.root / "private.env").write_text("private data stays\n")
    elif protection == "locked":
        linked = fixture.root.parent / "locked-project"
        cleanup.git(fixture.root, "worktree", "add", "--lock", "-b", "locked", str(linked))
        shutil.move(str(fixture.target), str(linked / "node-compile-cache"))
        target = linked / "node-compile-cache"
        fixture = Fixture(
            fixture.paths, linked, target, target / fixture.payload.relative_to(fixture.target), fixture.now
        )
    head = cleanup.git(fixture.root, "rev-parse", "HEAD").strip()
    source = (fixture.root / "source.txt").read_bytes()
    observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
    census(fixture, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(fixture.paths, observed) == "removed"
    assert not fixture.target.exists()
    assert fixture.root.is_dir()
    assert (fixture.root / "source.txt").read_bytes() == source
    assert cleanup.git(fixture.root, "rev-parse", "HEAD").strip() == head
    if protection == "locked":
        assert "locked" in cleanup.git(fixture.root.parent / "project", "worktree", "list", "--porcelain")
    if protection == "dirty":
        assert (fixture.root / "private.env").read_text() == "private data stays\n"


@pytest.mark.parametrize("kind", ["process", "workspace", "terminal", "container", "queued"])
def test_live_reference_sources_retain_native_node_cache(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    root = node_cache.root
    references = {
        "process": cleanup.References(frozenset({node_cache.payload})),
        "workspace": cleanup.References(frozenset(), workspaces=frozenset({root})),
        "terminal": cleanup.References(frozenset(), arguments=(str(root),)),
        "container": cleanup.References(frozenset(), mounted=frozenset({root})),
        "queued": cleanup.References(frozenset(), arguments=(f"check --cwd={root}",)),
    }
    observed = cleanup_artifacts.inspect(root, node_cache.target, now=node_cache.now)
    census(node_cache, monkeypatch, references[kind])
    assert "referenced" in cleanup_artifacts.remove(node_cache.paths, observed)
    assert node_cache.payload.read_bytes() == NATIVE_CACHE


@pytest.mark.parametrize(
    "kind",
    [
        "private",
        "source",
        "symlink",
        "namespace-link",
        "version",
        "uid",
        "name",
        "truncated",
        "checksum",
        "writable",
        "empty",
    ],
)
def test_unrecognized_node_cache_contents_are_retained(node_cache: Fixture, kind: str) -> None:
    payload = node_cache.payload
    match kind:
        case "private":
            (payload.parent / ".env").write_text("private data")
        case "source":
            (payload.parent / "source.js").write_text("authored source")
        case "symlink":
            payload.unlink()
            outside = node_cache.root / "outside-native-cache"
            outside.write_bytes(NATIVE_CACHE)
            payload.symlink_to(outside)
        case "namespace-link":
            outside = node_cache.root / "outside"
            payload.parent.rename(outside)
            payload.parent.symlink_to(outside, target_is_directory=True)
        case "version":
            payload.parent.rename(payload.parent.with_name(f"v25.0.0-x64-964aae3f-{os.getuid()}"))
        case "uid":
            payload.parent.rename(payload.parent.with_name(f"v24.22.1-x64-964aae3f-{os.getuid() + 1}"))
        case "name":
            payload.rename(payload.with_name("6c008d06.pending"))
        case "truncated":
            payload.write_bytes(payload.read_bytes()[:-1])
        case "checksum":
            content = payload.read_bytes()
            payload.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
        case "writable":
            payload.chmod(0o666)
        case "empty":
            payload.unlink()
        case _:
            message = "Unknown invalid-cache test case"
            raise AssertionError(message)
    with pytest.raises(ValueError, match=r"unsafe|unrecognized"):
        cleanup_artifacts.inspect(node_cache.root, node_cache.target, now=node_cache.now)
    assert node_cache.target.exists()


@pytest.mark.parametrize("version", ["v24.21.0", "v24.22.0", "v24.22.1"])
def test_qualified_node_native_layouts(tmp_path: Path, version: str) -> None:
    native = native_cache(tmp_path)
    native.payload.parent.rename(native.target / f"{version}-x64-964aae3f-{os.getuid()}")
    assert (
        hashlib.sha256(NATIVE_CACHE).hexdigest() == "d611234505c902f9a23428422f5f7e52bd5437572381540c32c1bc84a7ae5fa5"
    )
    assert cleanup_node_cache.installed(native.target)


def test_recent_node_cache_is_retained(node_cache: Fixture) -> None:
    with pytest.raises(ValueError, match="too recent"):
        cleanup_artifacts.inspect(node_cache.root, node_cache.target, now=time.time())


@pytest.mark.parametrize("corrupt", [False, True], ids=["native", "corrupted"])
def test_expired_native_validation_reports_deferred_cleanup(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, corrupt: bool
) -> None:
    fixture = node_cache
    payload = NATIVE_CACHE[:-1] + bytes([NATIVE_CACHE[-1] ^ 1]) if corrupt else NATIVE_CACHE
    fixture.payload.write_bytes(payload)
    state = fixture.paths.home / "state"
    state.mkdir()
    elapsed = 0.0
    native_read = os.pread

    def clock() -> float:
        return elapsed

    def slow_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal elapsed
        block = native_read(descriptor, size, offset)
        elapsed = cleanup_node_cache.SECONDS + 1.0
        return block

    census(fixture, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance only elapsed time during a genuine native file read; do not wait for the production timeout.
        time, "monotonic", clock
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Read actual native cache bytes and simulate the observed slow filesystem boundary.
        os, "pread", slow_read
    )
    report = cleanup_artifacts.recover(fixture.paths, state, deadline=60, temporary=fixture.paths.home / "temporary")
    assert report.inspected == 1
    assert report.removed == report.reclaimed_bytes == 0
    assert "inspection deferred: Node cache time budget" in report.reasons
    assert "unrecognized Node compile cache contents" not in report.reasons
    summary = cleanup_daemon.Summary(worktrees=0, attempted=0, removed=0, reasons={}, cache="", artifacts=report)
    assert cleanup_daemon.incomplete_inspection(summary)
    assert fixture.payload.read_bytes() == payload
    assert (fixture.root / "source.txt").read_text() == "committed authored source\n"
    assert (state / cleanup_artifacts.CURSOR).read_text().strip() == str(fixture.target)


@pytest.mark.parametrize(
    ("deadline", "continues"),
    [pytest.param(60, True, id="local-cache-budget"), pytest.param(5, False, id="whole-sweep-budget")],
)
def test_local_cache_timeout_does_not_end_the_remaining_sweep(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, deadline: int, *, continues: bool
) -> None:
    fixture = node_cache
    later_root = fixture.root.with_name("z-unrelated")
    later = native_cache(later_root)
    private = later_root / "private.env"
    private.write_text("authored sibling stays\n")
    state = fixture.paths.home / "state"
    state.mkdir()
    elapsed = 0.0
    native_read = os.pread

    def clock() -> float:
        return elapsed

    def slow_first_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal elapsed
        result = native_read(descriptor, size, offset)
        elapsed = cleanup_node_cache.SECONDS + 1.0
        return result

    def boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == fixture.paths
        assert candidates
        assert len(candidates) == 1
        assert candidates[0] in {fixture.root, later_root}
        return cleanup.References(frozenset())

    census(fixture, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Only the external host census is replaced; real native files, validation and inode-scoped removal execute unchanged.
        cleanup, "references", boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance elapsed time once during a real native payload read to distinguish candidate and whole-sweep deadlines deterministically.
        time, "monotonic", clock
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Read genuine payload bytes while simulating the observed first-cache filesystem delay.
        os, "pread", slow_first_read
    )
    report = cleanup_artifacts.recover(fixture.paths, state, deadline=deadline, temporary=fixture.paths.home / "unused")
    assert report.inspected == (2 if continues else 1)
    assert report.removed == int(continues)
    assert later.target.exists() is not continues
    assert fixture.payload.read_bytes() == NATIVE_CACHE
    assert private.read_text() == "authored sibling stays\n"
    assert any("inspection" in reason for reason in report.reasons)


def test_slow_native_validation_resumes_without_restarting_completed_files(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = node_cache
    for index in range(4):
        (fixture.payload.parent / f"{index:08x}").write_bytes(NATIVE_CACHE)
    private = fixture.root / "private.env"
    private.write_text("authored sibling stays\n")
    state = fixture.paths.home / "state"
    state.mkdir(mode=0o700)
    elapsed = 0.0
    reads: list[int] = []
    native_read = os.pread

    def clock() -> float:
        return elapsed

    def slow_read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal elapsed
        block = native_read(descriptor, size, offset)
        reads.append(os.fstat(descriptor).st_ino)
        elapsed += cleanup_node_cache.SECONDS / 2 + 1
        return block

    census(fixture, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance elapsed time only after reading genuine native cache bytes, reproducing repeated bounded validation under throttling.
        time, "monotonic", clock
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Preserve actual file reads, checksums, metadata and removal; simulate only the observed read latency.
        os, "pread", slow_read
    )
    reports = [
        cleanup_artifacts.recover(fixture.paths, state, deadline=elapsed + 60, temporary=fixture.paths.home / "unused")
        for _ in range(3)
    ]
    assert [report.removed for report in reports] == [0, 0, 1]
    assert len(reads) == len(set(reads)) == 5
    assert not fixture.target.exists()
    assert private.read_text() == "authored sibling stays\n"
    assert (fixture.root / "source.txt").read_text() == "committed authored source\n"


@dataclass(frozen=True)
class ValidatedCache:
    fixture: Fixture
    state: Path


def test_native_validation_continues_across_python_process_restarts(node_cache: Fixture) -> None:
    fixture = node_cache
    for index in range(4):
        (fixture.payload.parent / f"{index:08x}").write_bytes(NATIVE_CACHE)
    state = fixture.paths.home / "state"
    state.mkdir(mode=0o700)
    script = """
import os,sys,time
from pathlib import Path
from long_running_agent.runtime import cleanup,cleanup_artifacts,cleanup_node_cache
from long_running_agent.runtime.settings import RuntimePaths
paths=RuntimePaths(Path(sys.argv[1]))
root=paths.code/'worktrees/project'
elapsed=0.0
native_read=os.pread
def read(descriptor,size,offset):
    global elapsed
    block=native_read(descriptor,size,offset)
    elapsed+=cleanup_node_cache.SECONDS/2+1
    return block
def references(current,candidates=None):
    assert current==paths and candidates==(root,)
    return cleanup.References(frozenset())
cleanup.references=references
time.time=lambda:float(sys.argv[2])
time.monotonic=lambda:elapsed
os.pread=read
report=cleanup_artifacts.recover(paths,paths.home/'state',deadline=60,temporary=paths.home/'unused')
print(report.model_dump_json())
"""
    reports = [
        cleanup_artifacts.Report.model_validate_json(
            subprocess.run(
                [sys.executable, "-I", "-c", script, str(fixture.paths.home), str(fixture.now)],
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            ).stdout
        )
        for _ in range(3)
    ]
    assert [report.removed for report in reports] == [0, 0, 1]
    assert not fixture.target.exists()
    assert (fixture.root / "source.txt").read_text() == "committed authored source\n"


def test_changed_tree_during_partial_validation_does_not_save_a_continuation(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = node_cache
    for index in range(4):
        (fixture.payload.parent / f"{index:08x}").write_bytes(NATIVE_CACHE)
    state = fixture.paths.home / "state"
    state.mkdir(mode=0o700)
    elapsed = 0.0
    reads = 0
    native_read = os.pread

    def read(descriptor: int, size: int, offset: int) -> bytes:
        nonlocal elapsed, reads
        block = native_read(descriptor, size, offset)
        reads += 1
        elapsed += cleanup_node_cache.SECONDS / 2 + 1
        if reads == 2:
            first = fixture.payload.parent / "00000000"
            metadata = first.stat()
            time.sleep(0.02)
            first.write_bytes(NATIVE_CACHE[:-1] + bytes([NATIVE_CACHE[-1] ^ 1]))
            os.utime(first, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
            assert first.stat().st_ctime_ns != metadata.st_ctime_ns
        return block

    census(fixture, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance elapsed time after genuine reads to exercise partial validation without waiting for the production deadline.
        time, "monotonic", lambda: elapsed
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Change an already-read file during the real native inspection; post-validation metadata must invalidate the whole continuation.
        os, "pread", read
    )
    report = cleanup_artifacts.recover(fixture.paths, state, deadline=60, temporary=fixture.paths.home / "unused")
    assert report.removed == 0
    assert fixture.target.exists()
    assert not (state / "node-cache-validation").exists()


@pytest.fixture
def validated_node_cache(node_cache: Fixture, monkeypatch: pytest.MonkeyPatch) -> ValidatedCache:
    fixture = node_cache
    state = fixture.paths.home / "state"
    state.mkdir(mode=0o700)
    census(fixture, monkeypatch, cleanup.References(frozenset({fixture.root})))
    report = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert report.removed == 0
    assert report.reasons == {"referenced by process, workspace, terminal, or container": 1}
    records = list((state / "node-cache-validation").glob("*.json"))
    assert len(records) == 1
    assert cleanup_node_cache.ValidationProgress.model_validate_json(records[0].read_bytes()).validated == 1
    census(fixture, monkeypatch, cleanup.References(frozenset()))
    return ValidatedCache(fixture, state)


@pytest.mark.parametrize("change", ["checksum", "restored-mtime", "authored", "symlink", "recent", "writable", "major"])
def test_completed_validation_does_not_bypass_changed_native_cache_guards(
    validated_node_cache: ValidatedCache, change: str
) -> None:
    fixture = validated_node_cache.fixture
    state = validated_node_cache.state
    private = fixture.root / "private.env"
    private.write_text("authored sibling stays\n")
    match change:
        case "checksum" | "restored-mtime":
            metadata = fixture.payload.stat()
            fixture.payload.write_bytes(NATIVE_CACHE[:-1] + bytes([NATIVE_CACHE[-1] ^ 1]))
            if change == "restored-mtime":
                os.utime(fixture.payload, ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
        case "authored":
            (fixture.payload.parent / "notes.txt").write_text("authored file stays\n")
        case "symlink":
            fixture.payload.unlink()
            fixture.payload.symlink_to(private)
        case "recent":
            os.utime(fixture.payload, (fixture.now, fixture.now))
        case "writable":
            fixture.payload.chmod(0o620)
        case "major":
            fixture.payload.parent.rename(fixture.payload.parent.with_name(f"v28.0.0-x64-964aae3f-{os.getuid()}"))
        case _:
            message = "Unknown cache mutation"
            raise AssertionError(message)
    report = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert report.removed == report.reclaimed_bytes == 0
    assert fixture.target.exists()
    assert private.read_text() == "authored sibling stays\n"
    assert (fixture.root / "source.txt").read_text() == "committed authored source\n"


@pytest.mark.parametrize("kind", ["schema", "permission", "symlink", "hardlink"])
def test_unknown_or_nonprivate_validation_progress_is_retained(validated_node_cache: ValidatedCache, kind: str) -> None:
    fixture = validated_node_cache.fixture
    state = validated_node_cache.state
    record = next((state / "node-cache-validation").glob("*.json"))
    private = fixture.root / "private.env"
    private.write_text("authored sibling stays\n")
    match kind:
        case "schema":
            record.write_text('{"validated": true, "fingerprint": "unknown"}\n')
        case "permission":
            record.chmod(0o644)
        case "symlink":
            record.unlink()
            record.symlink_to(private)
        case "hardlink":
            record.unlink()
            os.link(private, record)
        case _:
            message = "Unknown progress mutation"
            raise AssertionError(message)
    report = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert report.removed == report.reclaimed_bytes == 0
    assert fixture.payload.read_bytes() == NATIVE_CACHE
    assert private.read_text() == "authored sibling stays\n"


def test_changed_validator_rechecks_native_payload_before_removal(
    validated_node_cache: ValidatedCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = validated_node_cache.fixture
    state = validated_node_cache.state
    native_read = os.pread
    reads: list[int] = []

    def read(descriptor: int, size: int, offset: int) -> bytes:
        reads.append(os.fstat(descriptor).st_ino)
        return native_read(descriptor, size, offset)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Change only the validator identity; prior native CRC proofs must be invalidated after implementation changes.
        cleanup_artifacts, "NODE_VALIDATOR_ID", b"different qualified validator"
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe genuine native reads to prove an old implementation's proof cannot bypass current CRC validation.
        os, "pread", read
    )
    report = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert report.removed == 1
    assert reads
    assert not fixture.target.exists()


def test_completed_validation_still_requires_fresh_active_work_protection(
    validated_node_cache: ValidatedCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = validated_node_cache.fixture
    state = validated_node_cache.state
    census(fixture, monkeypatch, cleanup.References(frozenset({fixture.root})))
    protected = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert protected.removed == 0
    assert fixture.payload.read_bytes() == NATIVE_CACHE
    census(fixture, monkeypatch, cleanup.References(frozenset()))
    completed = cleanup_artifacts.recover(
        fixture.paths, state, deadline=time.monotonic() + 60, temporary=fixture.paths.home / "unused"
    )
    assert completed.removed == 1
    assert not fixture.target.exists()


def test_genuine_native_cache_reads_with_one_available_descriptor(node_cache: Fixture) -> None:
    script = (
        "from pathlib import Path\n"
        "import resource,sys,time\n"
        "from long_running_agent.runtime import cleanup_node_cache\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE,(4,4))\n"
        "print(cleanup_node_cache.regular_file(Path(sys.argv[1]),time.monotonic()+10))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", script, str(node_cache.payload)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == node_cache.payload.stat().st_size


@pytest.mark.parametrize("chunk", [cleanup_node_cache.HEADER.size, 64, len(NATIVE_CACHE), len(NATIVE_CACHE) + 1])
def test_genuine_native_cache_keeps_crc_and_eof_checks_across_chunk_boundaries(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, chunk: int
) -> None:
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Lower only the read chunk; genuine native bytes, checksum, EOF and filesystem identity checks remain real.
        cleanup_node_cache, "CHUNK", chunk
    )
    assert cleanup_node_cache.installed(node_cache.target)
    node_cache.payload.write_bytes(NATIVE_CACHE + b"unexpected trailing data")
    assert not cleanup_node_cache.installed(node_cache.target)


@pytest.mark.parametrize("tag", ["310b2ce3", "e5074b9b"])
def test_genuine_node26_cache_uses_the_verified_native_format(tmp_path: Path, tag: str) -> None:
    root = tmp_path / "node26-private"
    target = root / "node-compile-cache"
    namespace = target / f"v26.12.1-x64-{tag}-{os.getuid()}"
    namespace.mkdir(parents=True)
    payload = namespace / "4f68eddf"
    payload.write_bytes(NODE_26_CACHE)
    assert (
        hashlib.sha256(NODE_26_CACHE).hexdigest() == "16e77c4aa7ec02008e15bd47590c0a775237372e3ddb1eecd2c8e7b0cbe600f5"
    )
    observed = cleanup_artifacts.inspect(root, target, now=time.time() + 3 * cleanup.DAY)
    assert observed.allocated == payload.stat().st_blocks * 512


@pytest.mark.parametrize("kind", ["unknown-major", "uidless"])
def test_unknown_node_major_and_uidless_namespaces_stay_protected(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "node26-private"
    target = root / "node-compile-cache"
    name = f"v27.0.0-x64-310b2ce3-{os.getuid()}" if kind == "unknown-major" else "v26.12.1-x64-310b2ce3"
    namespace = target / name
    namespace.mkdir(parents=True)
    payload = namespace / "4f68eddf"
    payload.write_bytes(NODE_26_CACHE)
    with pytest.raises(ValueError, match="unrecognized"):
        cleanup_artifacts.inspect(root, target, now=time.time() + 3 * cleanup.DAY)
    assert payload.read_bytes() == NODE_26_CACHE


@pytest.mark.parametrize(
    ("version", "payload"),
    [
        ("v22.0.0", NODE_22_CACHE),
        ("v22.24.0", NODE_22_CACHE),
        ("v24.0.0", NATIVE_CACHE),
        ("v24.23.0", NATIVE_CACHE),
        ("v26.13.0", NODE_26_13_CACHE),
        ("v26.14.0", NODE_26_13_CACHE),
    ],
)
def test_verified_node_format_survives_minor_version_changes(tmp_path: Path, version: str, payload: bytes) -> None:
    root = tmp_path / "native-family-private"
    target = root / "node-compile-cache"
    namespace = target / f"{version}-x64-310b2ce3-{os.getuid()}"
    namespace.mkdir(parents=True)
    file = namespace / "c74c039d"
    file.write_bytes(payload)
    assert (
        hashlib.sha256(NODE_26_13_CACHE).hexdigest()
        == "92a06a3630edfbbc9e290694564536541b6366a80cd06e534f65678fa63a9834"
    )
    observed = cleanup_artifacts.inspect(root, target, now=time.time() + 3 * cleanup.DAY)
    assert observed.allocated == file.stat().st_blocks * 512


@pytest.mark.parametrize(
    "version", ["v22.023.4", "v22.23.04", "v22.23.4-rc.1", "v26.013.0", "v26.13.00", "v26.13.0-rc.1"]
)
def test_noncanonical_node_version_names_remain_protected(tmp_path: Path, version: str) -> None:
    root = tmp_path / "native-family-private"
    target = root / "node-compile-cache"
    namespace = target / f"{version}-x64-310b2ce3-{os.getuid()}"
    namespace.mkdir(parents=True)
    file = namespace / "c74c039d"
    file.write_bytes(NODE_26_13_CACHE)
    with pytest.raises(ValueError, match="unrecognized"):
        cleanup_artifacts.inspect(root, target, now=time.time() + 3 * cleanup.DAY)
    assert file.read_bytes() == NODE_26_13_CACHE


@pytest.mark.parametrize("kind", ["disposable", "crc", "private", "referenced"])
def test_genuine_node22_cache_keeps_native_disposal_guards(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    fixture = node_cache
    namespace = fixture.payload.parent.with_name(f"v22.23.4-x64-23a69f65-{os.getuid()}")
    fixture.payload.parent.rename(namespace)
    payload = (namespace / fixture.payload.name).rename(namespace / "5d71ad14")
    payload.write_bytes(NODE_22_CACHE)
    fixture = Fixture(fixture.paths, fixture.root, fixture.target, payload, fixture.now)
    assert (
        hashlib.sha256(NODE_22_CACHE).hexdigest() == "74b76cf13db7039c9c9c373154ab0688afcbe172893a3694b8c3cbc4cbbac3b0"
    )
    match kind:
        case "crc":
            payload.write_bytes(NODE_22_CACHE[:-1] + bytes([NODE_22_CACHE[-1] ^ 1]))
            assert not cleanup_node_cache.installed(fixture.target)
            assert payload.is_file()
            return
        case "private":
            private = namespace / ".env"
            private.write_text("private data stays")
            assert not cleanup_node_cache.installed(fixture.target)
            assert private.read_text() == "private data stays"
            return
        case "referenced" | "disposable":
            pass
        case _:
            pytest.fail(f"Unrecognized native fixture case: {kind}")
    source = (fixture.root / "source.txt").read_bytes()
    head = cleanup.git(fixture.root, "rev-parse", "HEAD")
    observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
    references = cleanup.References(frozenset({payload}) if kind == "referenced" else frozenset())
    census(fixture, monkeypatch, references)
    reason = cleanup_artifacts.remove(fixture.paths, observed)
    if kind == "referenced":
        assert "referenced" in reason
        assert payload.read_bytes() == NODE_22_CACHE
    else:
        assert reason == "removed"
        assert not fixture.target.exists()
    assert (fixture.root / "source.txt").read_bytes() == source
    assert cleanup.git(fixture.root, "rev-parse", "HEAD") == head


def test_native_cache_larger_than_the_original_entry_limit_is_completely_inspected(tmp_path: Path) -> None:
    root = tmp_path / "large-native-cache"
    native = native_cache(root)
    for index in range(40000):
        (native.payload.parent / f"{index:08x}").write_bytes(NATIVE_CACHE)
    observed = cleanup_artifacts.inspect(root, native.target, now=time.time() + 3 * cleanup.DAY)
    assert observed.allocated > 0
    assert native.payload.read_bytes() == NATIVE_CACHE


@pytest.mark.parametrize("kind", ["native", "checksum", "truncated", "total-budget"])
def test_genuine_large_native_cache_keeps_content_and_total_size_guards(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    # Node 24.22.1 enableCompileCache()/flushCompileCache() on a module exporting
    # a 20 MiB ASCII literal. Keep the native serialized bytes in a small zlib file.
    payload = zlib.decompress((Path(__file__).parent / "fixtures/node24-large-constant.bin.zlib").read_bytes())
    assert hashlib.sha256(payload).hexdigest() == "7bbe4d2840eb62c226b6675999a23b53698b0f937d20a51a6b588a340a433d56"
    assert len(payload) > 16 * 1024 * 1024
    fixture = node_cache
    match kind:
        case "checksum":
            offset = cleanup_node_cache.CHUNK + 1
            payload = payload[:offset] + bytes([payload[offset] ^ 1]) + payload[offset + 1 :]
        case "truncated":
            payload = payload[:-1]
        case "total-budget":
            monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Lower only the total-byte budget; genuine native bytes, ownership, age and checksum inspection remain real.
                cleanup_node_cache, "MAX_BYTES", len(payload) - 1
            )
        case "native":
            pass
        case _:
            message = "Unknown large-cache test case"
            raise AssertionError(message)
    fixture.payload.write_bytes(payload)
    private = fixture.root / "private.env"
    private.write_text("authored sibling stays")
    if kind != "native":
        with pytest.raises(ValueError, match="unrecognized"):
            cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
        assert fixture.payload.exists()
    else:
        observed = cleanup_artifacts.inspect(fixture.root, fixture.target, now=fixture.now)
        census(fixture, monkeypatch, cleanup.References(frozenset()))
        assert cleanup_artifacts.remove(fixture.paths, observed) == "removed"
        assert not fixture.target.exists()
    assert private.read_text() == "authored sibling stays"
    assert (fixture.root / "source.txt").read_text() == "committed authored source\n"


def test_oversized_node_cache_file_is_retained_without_reading_it(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_read(_descriptor: int, _size: int, _offset: int) -> bytes:
        message = "Oversized cache payload must not be read"
        raise AssertionError(message)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Prove that the file-size guard rejects an oversized native path before payload reads; the real sparse file and metadata checks remain intact.
        os, "pread", unexpected_read
    )
    with node_cache.payload.open("r+b") as stream:
        stream.truncate(cleanup_node_cache.MAX_FILE_BYTES + 1)
    assert not cleanup_node_cache.installed(node_cache.target)
    assert node_cache.payload.exists()


def test_shared_node_cache_file_preserves_outside_link_and_does_not_claim_its_bytes(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = node_cache.root / "outside-native-cache"
    os.link(node_cache.payload, outside)
    observed = cleanup_artifacts.inspect(node_cache.root, node_cache.target, now=node_cache.now)
    assert observed.allocated == 0
    census(node_cache, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(node_cache.paths, observed) == "removed"
    assert outside.read_bytes() == NATIVE_CACHE


@pytest.mark.parametrize("tracked_in", ["index", "head"])
def test_tracked_node_cache_is_retained(node_cache: Fixture, tracked_in: str) -> None:
    cleanup.git(node_cache.root, "add", "-f", str(node_cache.payload))
    if tracked_in == "head":
        cleanup.git(
            node_cache.root,
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
        cleanup.git(node_cache.root, "rm", "--cached", str(node_cache.payload))
    with pytest.raises(ValueError, match=r"tracked|not ignored"):
        cleanup_artifacts.inspect(node_cache.root, node_cache.target, now=node_cache.now)
    assert node_cache.payload.read_bytes() == NATIVE_CACHE


def test_changed_node_cache_is_retained_after_host_inspection(
    node_cache: Fixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = cleanup_artifacts.inspect(node_cache.root, node_cache.target, now=node_cache.now)

    def changing_boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == node_cache.paths
        assert candidates == (node_cache.root,)
        node_cache.payload.write_bytes(b"concurrent write stays intact")
        return cleanup.References(frozenset())

    census(node_cache, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- The host boundary writes a concurrent change; the native final fingerprint must retain it.
        cleanup, "references", changing_boundary
    )
    assert "changed" in cleanup_artifacts.remove(node_cache.paths, observed)
    assert node_cache.payload.read_bytes() == b"concurrent write stays intact"


def test_node_cache_sweep_keeps_cursor_and_removal_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = RuntimePaths(tmp_path / "home")
    temporary = tmp_path / "temporary"
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    roots = [temporary / f"pydantic-{index}" for index in range(3)]
    caches = [native_cache(root) for root in roots]
    for root in roots:
        (root / "private.env").write_text("source is independent of generated cache")
    fixture = Fixture(paths, roots[0], caches[0].target, caches[0].payload, time.time() + 3 * cleanup.DAY)

    def boundary(_paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert _paths == paths
        assert candidates
        assert candidates[0] in roots
        return cleanup.References(frozenset())

    census(fixture, monkeypatch, cleanup.References(frozenset()))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Only the external census is replaced; the native rotating sweep and filesystem removal execute unchanged.
        cleanup, "references", boundary
    )
    report = cleanup_artifacts.recover(paths, state, deadline=time.monotonic() + 30, temporary=temporary)
    assert report.removed == cleanup_artifacts.MAX_REMOVALS == 2
    assert report.reclaimed_bytes > 0
    assert sum((root / "node-compile-cache").exists() for root in roots) == 1
    assert all((root / "private.env").exists() for root in roots)
    second = cleanup_artifacts.recover(paths, state, deadline=time.monotonic() + 30, temporary=temporary)
    assert second.removed == 1
    assert all(root.is_dir() for root in roots)
