from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import shutil
import ssl
import time
from typing import TYPE_CHECKING, Protocol
import venv

from long_running_agent.process import run
from long_running_agent.runtime import cleanup, cleanup_artifacts
from long_running_agent.runtime.settings import RuntimePaths
import pytest


pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="dependency maintenance requires Linux process and filesystem identities"
)


if TYPE_CHECKING:
    from collections.abc import Sequence


class DependencyRequest(Protocol):
    @property
    def param(self) -> str: ...


@dataclass(frozen=True)
class Dependency:
    paths: RuntimePaths
    root: Path
    target: Path
    now: float


@pytest.fixture(params=["venv", "npm"])
def dependency(tmp_path: Path, request: DependencyRequest) -> Dependency:
    paths = RuntimePaths(tmp_path / "home")
    root = paths.code / "worktrees/project"
    root.mkdir(parents=True)
    (root / ".gitignore").write_text(".venv/\nnode_modules/\npython/.venv/\n")
    (root / "source.txt").write_text("authored source\n")
    if request.param == "npm":
        if shutil.which("npm") is None:
            pytest.skip("native npm is unavailable")
        package = root / "package"
        package.mkdir()
        (package / "package.json").write_text(
            json.dumps({"name": "cache-fixture", "version": "1.0.0", "bin": {"cache-fixture": "index.js"}})
        )
        (package / "index.js").write_text("#!/usr/bin/env node\nconsole.log('fixture');\n")
        archives = tmp_path / "archives"
        archives.mkdir()
        run(
            [
                "npm",
                "pack",
                "--offline",
                "--ignore-scripts",
                f"--pack-destination={archives}",
                f"--cache={tmp_path / 'npm-cache'}",
            ],
            cwd=package,
            timeout=30,
            capture_output=True,
        )
        (root / "package.json").write_text(
            json.dumps(
                {
                    "name": "fixture-project",
                    "private": True,
                    "dependencies": {"cache-fixture": f"file:{archives / 'cache-fixture-1.0.0.tgz'}"},
                }
            )
        )
    cleanup.git(root, "init", "-q", "-b", "unmerged")
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
        "private fixture source",
    )
    target = root / (".venv" if request.param == "venv" else "node_modules")
    if request.param == "venv":
        venv.EnvBuilder(with_pip=False, symlinks=True).create(target)
    else:
        run(
            [
                "npm",
                "install",
                "--offline",
                "--ignore-scripts",
                "--no-audit",
                "--no-fund",
                "--install-links",
                "--install-strategy=hoisted",
                f"--cache={tmp_path / 'npm-cache'}",
            ],
            cwd=root,
            timeout=30,
            capture_output=True,
        )
    return Dependency(paths, root, target, time.time() + 3 * cleanup.DAY)


def census(dependency: Dependency, monkeypatch: pytest.MonkeyPatch, references: cleanup.References) -> None:
    def native_boundary(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> cleanup.References:
        assert paths == dependency.paths
        assert candidates == (dependency.root,)
        return references

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only the authenticated host boundary; native environments, npm output, Git and inode-scoped removal remain real.
        cleanup, "references", native_boundary
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Age real native ctime by advancing the fixture's wall clock, preserving every freshness decision.
        time, "time", lambda: dependency.now
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Keep temporary-root alias discovery within this private fixture; real cache validation and shared-alias decisions still execute.
        cleanup_artifacts, "TEMPORARY_ROOT", dependency.paths.home / "temporary"
    )


@pytest.mark.parametrize("protection", ["dirty", "unmerged", "locked"])
def test_native_dependencies_do_not_inherit_source_retention(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    if protection == "dirty":
        (dependency.root / "source.txt").write_text("uncommitted authored change\n")
        (dependency.root / ".env").write_text("PRIVATE_FIXTURE=true\n")
    elif protection == "locked":
        linked = dependency.root.parent / "locked-project"
        cleanup.git(dependency.root, "worktree", "add", "--lock", "-b", "locked", str(linked))
        target = linked / dependency.target.name
        shutil.move(str(dependency.target), target)
        dependency = Dependency(dependency.paths, linked, target, dependency.now)
    head = cleanup.git(dependency.root, "rev-parse", "HEAD")
    source = (dependency.root / "source.txt").read_bytes()
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert not dependency.target.exists()
    assert (dependency.root / "source.txt").read_bytes() == source
    assert cleanup.git(dependency.root, "rev-parse", "HEAD") == head
    if protection == "dirty":
        assert (dependency.root / ".env").read_text() == "PRIVATE_FIXTURE=true\n"
    if protection == "locked":
        assert "locked" in cleanup.git(dependency.root, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("kind", ["process", "workspace", "terminal", "container", "queued"])
def test_fresh_usage_protects_native_dependencies(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    references = cleanup.References(frozenset({dependency.target}))
    if kind == "workspace":
        references = cleanup.References(frozenset(), workspaces=frozenset({dependency.root}))
    elif kind == "container":
        references = cleanup.References(frozenset(), mounted=frozenset({dependency.target}))
    elif kind in {"terminal", "queued"}:
        references = cleanup.References(frozenset(), arguments=(str(dependency.root),))
    census(dependency, monkeypatch, references)
    assert cleanup_artifacts.remove(dependency.paths, observed) != "removed"
    assert dependency.target.is_dir()


@pytest.mark.parametrize("name", [".env", ".npmrc", ".pypirc", ".netrc", ".git", "authored-notes.txt"])
def test_private_or_authored_environment_files_are_retained(dependency: Dependency, name: str) -> None:
    (dependency.target / name).write_text("preserve fixture data\n")
    with pytest.raises(ValueError, match=r"private|unrecognized"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert (dependency.target / name).read_text() == "preserve fixture data\n"


def test_native_dependencies_are_discovered_and_fresh_contents_retained(dependency: Dependency) -> None:
    assert (dependency.root, dependency.target) in cleanup_artifacts.candidates((dependency.root.parent,))
    with pytest.raises(ValueError, match="recent"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=time.time())


def test_late_dependency_alias_keeps_shared_environment(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    other = dependency.root.parent / "other-project"
    other.mkdir()
    (other / dependency.target.name).symlink_to(dependency.target, target_is_directory=True)
    assert (dependency.root, dependency.target) in cleanup_artifacts.candidates((dependency.root.parent,))
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert "shared" in cleanup_artifacts.remove(dependency.paths, observed)
    assert dependency.target.is_dir()


def test_tracked_dependency_files_are_retained(dependency: Dependency) -> None:
    authored = dependency.target / ("pyvenv.cfg" if dependency.target.name == ".venv" else ".package-lock.json")
    cleanup.git(dependency.root, "add", "--force", str(authored))
    with pytest.raises(ValueError, match=r"ignored|tracked"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert authored.exists()


def test_symlink_removal_preserves_authored_target(dependency: Dependency, monkeypatch: pytest.MonkeyPatch) -> None:
    authored = dependency.root / "source.txt"
    if dependency.target.name == ".venv":
        directory = dependency.target / "share"
    else:
        directory = dependency.target / "cache-fixture"
    directory.mkdir(exist_ok=True)
    (directory / "authored-link").symlink_to(authored)
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert authored.read_text() == "authored source\n"


def test_parent_alias_also_preserves_shared_environment(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    other = dependency.root.parent / "other-project"
    other.mkdir()
    if dependency.target.name == ".venv":
        (other / "python").symlink_to(dependency.root, target_is_directory=True)
    else:
        (other / "typescript").symlink_to(dependency.root, target_is_directory=True)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert "shared" in cleanup_artifacts.remove(dependency.paths, observed)
    assert dependency.target.is_dir()


def test_unresolved_alias_defers_dependencies_without_hiding_provider_inventory(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    other = dependency.root.parent / "other-project"
    other.mkdir()
    (other / "node_modules").symlink_to("node_modules", target_is_directory=True)
    provider = dependency.root / ".cache/terraform/providers"
    provider.mkdir(parents=True)
    assert (dependency.root, provider) in cleanup_artifacts.candidates((dependency.root.parent,))
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    with pytest.raises(ValueError, match="inspection deferred"):
        cleanup_artifacts.remove(dependency.paths, observed)
    assert dependency.target.is_dir()


def test_provider_inventory_ignores_unresolved_parent_aliases(tmp_path: Path) -> None:
    root = tmp_path / "worktrees/project"
    provider = root / ".cache/terraform/providers"
    provider.mkdir(parents=True)
    other = root.parent / "other-project"
    other.mkdir()
    (other / "node_modules").symlink_to("node_modules", target_is_directory=True)
    assert (root, provider) in cleanup_artifacts.candidates((root.parent,))


def test_dangling_unrelated_alias_does_not_pin_native_dependencies(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    other = dependency.root.parent / "other-project"
    other.mkdir()
    alias = other / "node_modules"
    alias.symlink_to("missing-native-cache", target_is_directory=True)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert alias.is_symlink()


@pytest.mark.parametrize(
    "name", ["Cookies", "Cookies-journal", "credentials.db-wal", "auth.json", "private.key", "private.env"]
)
def test_nested_credentials_and_browser_profiles_are_preserved(dependency: Dependency, name: str) -> None:
    directory = dependency.target / ("share" if dependency.target.name == ".venv" else "cache-fixture") / "profile"
    directory.mkdir(parents=True)
    (directory / name).write_text("private fixture data\n")
    with pytest.raises(ValueError, match="private"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert (directory / name).read_text() == "private fixture data\n"


@pytest.mark.parametrize("kind", ["private-key", "public-certificate", "unknown"])
def test_private_pem_is_preserved_and_native_public_ca_bundle_is_disposable(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    directory = dependency.target / ("share" if dependency.target.name == ".venv" else "cache-fixture")
    directory.mkdir(exist_ok=True)
    certificate = directory / "fixture.pem"
    if kind == "public-certificate":
        public_bundle = ssl.get_default_verify_paths().cafile
        if public_bundle is None and Path("/etc/ssl/certs/ca-certificates.crt").is_file():
            public_bundle = "/etc/ssl/certs/ca-certificates.crt"
        if public_bundle is None:
            pytest.skip("native public CA bundle is unavailable")
        shutil.copyfile(public_bundle, certificate)
    else:
        payload = (
            "-----BEGIN PRIVATE KEY-----\nprivate fixture\n" if kind == "private-key" else "authored fixture data\n"
        )
        certificate.write_text(payload)
        with pytest.raises(ValueError, match="private"):
            cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
        return
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"


def test_native_shared_metadata_survives_and_is_not_claimed_as_reclaimed(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata = dependency.target / ("pyvenv.cfg" if dependency.target.name == ".venv" else "cache-fixture/package.json")
    before = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    preserved = dependency.root / "preserved-native-metadata"
    preserved.hardlink_to(metadata)
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert before.allocated - observed.allocated == metadata.stat().st_blocks * 512
    expected = preserved.read_bytes()
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert preserved.read_bytes() == expected


def test_nested_environment_exceeds_provider_budget_without_deleting_source(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    if dependency.target.name != ".venv":
        pytest.skip("the Python environment supplies native data_files")
    target = dependency.root / "python/.venv"
    target.parent.mkdir()
    shutil.move(str(dependency.target), target)
    data = target / "share/native-fixture-data"
    data.mkdir(parents=True)
    for number in range(cleanup_artifacts.MAX_ENTRIES + 1):
        (data / str(number)).touch()
    dependency = Dependency(dependency.paths, dependency.root, target, dependency.now)
    assert (dependency.root, target) in cleanup_artifacts.candidates((dependency.root.parent,))
    observed = cleanup_artifacts.inspect(dependency.root, target, now=dependency.now)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert (dependency.root / "source.txt").read_text() == "authored source\n"


VITE_8_3_5_NATIVE_FILES = (
    (
        "_metadata.json",
        (
            "ewogICJoYXNoIjogIjgyZWEyYTBhIiwKICAiY29uZmlnSGFzaCI6ICJiNmE0NDJiZCIsCiAgImxvY2tmaWxlSGFzaCI6ICJl"
            "M2IwYzQ0MiIsCiAgImJyb3dzZXJIYXNoIjogIjc5ZjAxMGU0IiwKICAib3B0aW1pemVkIjogewogICAgImNhY2hlLWZpeHR1"
            "cmUiOiB7CiAgICAgICJzcmMiOiAiLi4vLi4vLi4vY2FjaGUtZml4dHVyZS9pbmRleC5qcyIsCiAgICAgICJmaWxlIjogImNh"
            "Y2hlLWZpeHR1cmUuanMiLAogICAgICAiZmlsZUhhc2giOiAiM2RiYzk4MzMiLAogICAgICAibmVlZHNJbnRlcm9wIjogZmFs"
            "c2UKICAgIH0KICB9LAogICJjaHVua3MiOiB7CiAgICAiZHluYW1pYy1EazhCU21DcSI6IHsKICAgICAgImZpbGUiOiAiZHlu"
            "YW1pYy1EazhCU21DcS5qcyIsCiAgICAgICJpc0R5bmFtaWNFbnRyeSI6IHRydWUKICAgIH0sCiAgICAic2hhcmVkLUNESHRi"
            "aGNhIjogewogICAgICAiZmlsZSI6ICJzaGFyZWQtQ0RIdGJoY2EuanMiLAogICAgICAiaXNEeW5hbWljRW50cnkiOiBmYWxz"
            "ZQogICAgfQogIH0KfQ=="
        ),
        "1bdc24ca248034bd59809376228d4d498ac87bc2d737f81c112e5fbe8deca855",
    ),
    (
        "cache-fixture.js",
        (
            "aW1wb3J0IHsgdCBhcyB2YWx1ZSB9IGZyb20gIi4vc2hhcmVkLUNESHRiaGNhLmpzIjsKLy8gLi4vLi4vLi4vLi4vLi4vLi4v"
            "Li4vLi4vLi4vdmFyL3RtcC9scmEtYXN0cm8tZGVwZW5kZW5jeS1jYWNoZS0yMDI2MTExNC9wcm9kdWNlci9ub2RlX21vZHVs"
            "ZXMvY2FjaGUtZml4dHVyZS9pbmRleC5qcwphc3luYyBmdW5jdGlvbiBsYXp5KCkgewoJcmV0dXJuIGltcG9ydCgiLi9keW5h"
            "bWljLURrOEJTbUNxLmpzIik7Cn0KZXhwb3J0IHsgbGF6eSwgdmFsdWUgfTsK"
        ),
        "6291e22b69772c96f47d4342f429bc7d450ef634383fa4bd58593849a26cca00",
    ),
    (
        "cache-fixture.js.map",
        (
            "eyJ2ZXJzaW9uIjozLCJmaWxlIjoiY2FjaGUtZml4dHVyZS5qcyIsIm5hbWVzIjpbXSwic291cmNlcyI6WyIuLi8uLi8uLi9j"
            "YWNoZS1maXh0dXJlL2luZGV4LmpzIl0sInNvdXJjZXNDb250ZW50IjpbImV4cG9ydCB7IHZhbHVlIH0gZnJvbSAnLi9zaGFy"
            "ZWQuanMnOyBleHBvcnQgYXN5bmMgZnVuY3Rpb24gbGF6eSgpIHsgcmV0dXJuIGltcG9ydCgnLi9keW5hbWljLmpzJyk7IH1c"
            "biJdLCJtYXBwaW5ncyI6Ijs7QUFBcUMsZUFBc0IsT0FBTztDQUFFLE9BQU8sT0FBTztBQUFpQiJ9"
        ),
        "a612913819168c96f181f9a651ebd0783a22a88c1a4c65881f5ea4b57f3a1835",
    ),
    (
        "dynamic-Dk8BSmCq.js",
        ("ZXhwb3J0IHsgdCBhcyB2YWx1ZSB9IGZyb20gIi4vc2hhcmVkLUNESHRiaGNhLmpzIjsK"),
        "698c0e73d80742143d9067e729c95890b24e76e92d1edf0a15a6e4b0bacc61a2",
    ),
    (
        "package.json",
        ("ewogICJ0eXBlIjogIm1vZHVsZSIKfQo="),
        "3ca9d4afd21425087cf31893b8f9f63c81b0b8408db5e343ca76e5f8aa26ab9a",
    ),
    (
        "shared-CDHtbhca.js",
        (
            "Ly8gLi4vLi4vLi4vLi4vLi4vLi4vLi4vLi4vLi4vdmFyL3RtcC9scmEtYXN0cm8tZGVwZW5kZW5jeS1jYWNoZS0yMDI2MTEx"
            "NC9wcm9kdWNlci9ub2RlX21vZHVsZXMvY2FjaGUtZml4dHVyZS9zaGFyZWQuanMKdmFyIHZhbHVlID0gNDI7CmV4cG9ydCB7"
            "IHZhbHVlIGFzIHQgfTsK"
        ),
        "14c10cb5cc034bccc780f79f587a18a7d144a9cd6398246e6e55bbc0e5b0aac8",
    ),
    (
        "shared-CDHtbhca.js.map",
        (
            "eyJ2ZXJzaW9uIjozLCJmaWxlIjoic2hhcmVkLUNESHRiaGNhLmpzIiwibmFtZXMiOltdLCJzb3VyY2VzIjpbIi4uLy4uLy4u"
            "L2NhY2hlLWZpeHR1cmUvc2hhcmVkLmpzIl0sInNvdXJjZXNDb250ZW50IjpbImV4cG9ydCBjb25zdCB2YWx1ZSA9IDQyO1xu"
            "Il0sIm1hcHBpbmdzIjoiO0FBQUEsSUFBYSxRQUFRIn0="
        ),
        "ef0c3f4af71b75480732968f5e5b58075509f73456b3ba1f6c1cdc8aba7b9d31",
    ),
)


def native_astro_cache(target: Path) -> Path:
    cache = target / ".astro/vite-sync/deps"
    cache.mkdir(parents=True)
    for name, encoded, expected in VITE_8_3_5_NATIVE_FILES:
        payload = base64.b64decode(encoded)
        assert hashlib.sha256(payload).hexdigest() == expected
        (cache / name).write_bytes(payload)
    return cache


@pytest.mark.parametrize("dependency", ["npm"], indirect=True)
@pytest.mark.parametrize("kind", ["idle", "process", "workspace", "terminal", "container", "queued"])
def test_native_astro_cache_preserves_parent_work_and_fresh_usage(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    native_astro_cache(dependency.target)
    source = dependency.root / "source.txt"
    source.write_text("uncommitted authored work stays\n")
    private = dependency.root / ".env"
    private.write_text("PRIVATE_FIXTURE=true\n")
    head = cleanup.git(dependency.root, "rev-parse", "HEAD")
    references = {
        "idle": cleanup.References(frozenset()),
        "process": cleanup.References(frozenset({dependency.target / ".astro/vite-sync/deps/cache-fixture.js"})),
        "workspace": cleanup.References(frozenset(), workspaces=frozenset({dependency.root})),
        "terminal": cleanup.References(frozenset(), arguments=(str(dependency.root),)),
        "container": cleanup.References(frozenset(), mounted=frozenset({dependency.root})),
        "queued": cleanup.References(frozenset(), arguments=(f"check --cwd={dependency.root}",)),
    }
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    census(dependency, monkeypatch, references[kind])
    reason = cleanup_artifacts.remove(dependency.paths, observed)
    if kind == "idle":
        assert reason == "removed"
        assert not dependency.target.exists()
    else:
        assert "referenced" in reason
        assert (dependency.target / ".astro/vite-sync/deps/_metadata.json").is_file()
    assert source.read_text() == "uncommitted authored work stays\n"
    assert private.read_text() == "PRIVATE_FIXTURE=true\n"
    assert cleanup.git(dependency.root, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("dependency", ["npm"], indirect=True)
@pytest.mark.parametrize(
    "kind", ["receipt", "path-escape", "missing-output", "invalid-metadata", "missing-native-hash", "unknown-wrapper"]
)
def test_astro_cache_retains_unrecognized_or_authored_content(dependency: Dependency, kind: str) -> None:
    cache = native_astro_cache(dependency.target)
    metadata = cache / "_metadata.json"
    if kind == "receipt":
        (cache / "qualification-receipt.txt").write_text("authored qualification evidence\n")
    elif kind == "path-escape":
        payload = metadata.read_text()
        metadata.write_text(payload.replace('"file": "cache-fixture.js"', '"file": "../../source.txt"', 1))
    elif kind == "missing-output":
        (cache / "cache-fixture.js").unlink()
    elif kind == "invalid-metadata":
        metadata.write_text('{"optimized": false}')
    elif kind == "missing-native-hash":
        metadata.write_text(metadata.read_text().replace('"fileHash"', '"unknownFileHash"'))
    elif kind == "unknown-wrapper":
        (dependency.target / ".astro/private-notes.txt").write_text("authored notes stay\n")
    with pytest.raises(ValueError, match="unrecognized managed dependency cache contents"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert metadata.is_file()
    assert (dependency.root / "source.txt").read_text() == "authored source\n"


def test_historical_rebase_ref_does_not_pin_idle_native_dependencies(
    dependency: Dependency, monkeypatch: pytest.MonkeyPatch
) -> None:
    head = cleanup.git(dependency.root, "rev-parse", "HEAD").strip()
    cleanup.git(dependency.root, "update-ref", "REBASE_HEAD", head)
    native = cleanup.git_process(dependency.root, "rebase", "--show-current-patch", check=False)
    assert native.returncode == 128
    assert native.stderr.strip() == "fatal: no rebase in progress"
    observed = cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    census(dependency, monkeypatch, cleanup.References(frozenset()))
    assert cleanup_artifacts.remove(dependency.paths, observed) == "removed"
    assert cleanup.git(dependency.root, "rev-parse", "REBASE_HEAD").strip() == head
    assert cleanup.git(dependency.root, "rev-parse", "HEAD").strip() == head
    assert (dependency.root / "source.txt").is_file()


def test_native_conflicted_rebase_still_protects_dependencies(dependency: Dependency) -> None:
    source = dependency.root / "source.txt"
    cleanup.git(dependency.root, "switch", "-q", "-c", "native-rebase-upstream")
    source.write_text("upstream authored change\n")
    cleanup.git(dependency.root, "add", "source.txt")
    cleanup.git(
        dependency.root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "upstream",
    )
    cleanup.git(dependency.root, "switch", "-q", "unmerged")
    source.write_text("topic authored change\n")
    cleanup.git(dependency.root, "add", "source.txt")
    cleanup.git(
        dependency.root, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "topic"
    )
    native = cleanup.git_process(dependency.root, "rebase", "native-rebase-upstream", check=False)
    assert native.returncode == 1
    assert cleanup.git(dependency.root, "diff", "--name-only", "--diff-filter=U").strip() == "source.txt"
    with pytest.raises(ValueError, match="Git operation is in progress"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert dependency.target.is_dir()
    assert cleanup.git(dependency.root, "rev-parse", "--verify", "REBASE_HEAD").strip()


def test_malformed_rebase_ref_stays_protected(dependency: Dependency) -> None:
    directory = Path(cleanup.git(dependency.root, "rev-parse", "--absolute-git-dir").strip())
    marker = directory / "REBASE_HEAD"
    marker.write_text("unrecognized operation state\n")
    with pytest.raises(ValueError, match="Git operation is in progress"):
        cleanup_artifacts.inspect(dependency.root, dependency.target, now=dependency.now)
    assert marker.read_text() == "unrecognized operation state\n"
