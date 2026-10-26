from __future__ import annotations

import hashlib
from io import BytesIO
import tarfile
from typing import TYPE_CHECKING

from long_running_agent.runtime import zellij
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from pathlib import Path


def release_fixture(directory: Path, *, version: str = zellij.VERSION) -> Path:
    archive = directory / "release.tar.gz"
    content = f"#!/bin/sh\nprintf 'zellij {version}\\n'\n".encode()
    with tarfile.open(archive, "w:gz") as output:
        member = tarfile.TarInfo("zellij")
        member.size = len(content)
        member.mode = 0o700
        output.addfile(member, BytesIO(content))
    return archive


def test_existing_user_zellij_is_preserved_without_executing_or_downloading(tmp_path: Path) -> None:
    executable = tmp_path / ".local/bin/zellij"
    executable.parent.mkdir(parents=True)
    original = b"existing executable with an active server"
    executable.write_bytes(original)
    executable.chmod(0o700)
    zellij.install_zellij(RuntimePaths(tmp_path))
    assert executable.read_bytes() == original
    assert list(executable.parent.iterdir()) == [executable]


def test_bad_download_is_rejected_before_extracting_or_executing(tmp_path: Path) -> None:
    archive = tmp_path / "untrusted.tar.gz"
    archive.write_bytes(b"untrusted download")
    destination = tmp_path / "zellij"
    with pytest.raises(RuntimeError, match="SHA256"):
        zellij.install_archive(archive, destination)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == [archive]


def test_verified_native_archive_installs_a_private_executable_and_cleans_staging(tmp_path: Path) -> None:
    archive = release_fixture(tmp_path)
    destination = tmp_path / "zellij"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    zellij.install_archive(archive, destination, expected_sha256=digest)
    assert destination.read_bytes().startswith(b"#!/bin/sh\n")
    assert destination.stat().st_mode & 0o777 == 0o700
    assert set(tmp_path.iterdir()) == {archive, destination}


def test_mismatched_native_version_is_not_published(tmp_path: Path) -> None:
    archive = release_fixture(tmp_path, version="0.1.0")
    destination = tmp_path / "zellij"
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="does not match"):
        zellij.install_archive(archive, destination, expected_sha256=digest)
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == [archive]


def test_concurrent_install_cannot_overwrite_an_existing_binary(tmp_path: Path) -> None:
    archive = release_fixture(tmp_path)
    destination = tmp_path / "zellij"
    destination.write_bytes(b"another setup already installed this binary")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    with pytest.raises(FileExistsError):
        zellij.install_archive(archive, destination, expected_sha256=digest)
    assert destination.read_bytes() == b"another setup already installed this binary"
    assert set(tmp_path.iterdir()) == {archive, destination}
