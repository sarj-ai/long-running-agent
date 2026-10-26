from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import TYPE_CHECKING

from long_running_agent.runtime import transfer
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class SelectedFile:
    name: str
    payload: bytes


@pytest.fixture
def home(tmp_path: Path) -> Path:
    directory = tmp_path / "home"
    directory.mkdir(mode=0o700)
    return directory


def file_spec(file: SelectedFile) -> transfer.FileSpec:
    return transfer.FileSpec(name=file.name, size=len(file.payload), sha256=hashlib.sha256(file.payload).hexdigest())


def upload_bytes(files: Sequence[SelectedFile]) -> bytes:
    manifest = transfer.Manifest(version=1, files=[file_spec(file) for file in files])
    return manifest.model_dump_json().encode() + b"\n" + b"".join(file.payload for file in files)


def receive_bytes(payload: bytes, home: Path, tmp_path: Path) -> list[transfer.Receipt]:
    source = tmp_path / "native-upload-stream"
    source.write_bytes(payload)
    with source.open("rb") as stream:
        return transfer.receive(stream, home=home)


def test_native_upload_preserves_binary_bytes_and_private_distinct_destinations(home: Path, tmp_path: Path) -> None:
    files = [SelectedFile("雪 screenshot.png", bytes(range(256)) * 300), SelectedFile("empty.bin", b"")]
    receipts = receive_bytes(upload_bytes(files), home, tmp_path)
    assert len(receipts) == 2
    for receipt, file in zip(receipts, files, strict=True):
        path = Path(receipt.path)
        assert path.read_bytes() == file.payload
        assert receipt.size == len(file.payload)
        assert receipt.sha256 == hashlib.sha256(file.payload).hexdigest()
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    second = receive_bytes(upload_bytes(files), home, tmp_path)
    assert Path(second[0].path).parent != Path(receipts[0].path).parent
    assert Path(receipts[0].path).read_bytes() == files[0].payload


@pytest.mark.parametrize(
    "failure", ["truncated", "checksum", "trailing"], ids=["interrupted-body", "changed-body", "trailing-data"]
)
def test_invalid_native_upload_never_publishes_partial_or_unverified_files(
    home: Path, tmp_path: Path, failure: str
) -> None:
    payload = upload_bytes(
        [SelectedFile("first.png", b"verified first"), SelectedFile("second.png", b"complete second")]
    )
    match failure:
        case "truncated":
            payload = payload[:-1]
        case "checksum":
            payload = payload[:-1] + b"X"
        case _:
            payload += b"unexpected"
    with pytest.raises(RuntimeError, match=r"interrupted|checksum|trailing"):
        receive_bytes(payload, home, tmp_path)
    inbox = home / ".local/share/long-running-agent/inbox"
    assert not list(inbox.rglob("*.png"))
    assert not list(inbox.rglob(".incoming-*"))


def test_disk_full_stream_error_preserves_existing_files_and_removes_only_owned_temp(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = home / "keep"
    sentinel.write_bytes(b"unchanged")
    native_write = os.write

    def full_disk(descriptor: int, payload: bytes | memoryview) -> int:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            message = "fixture disk is full"
            raise OSError(errno.ENOSPC, message)
        return native_write(descriptor, payload)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native descriptor writes are the transfer boundary; inject real ENOSPC before atomic publication.
        os, "write", full_disk
    )
    with pytest.raises(OSError, match="fixture disk is full"):
        receive_bytes(upload_bytes([SelectedFile("image.png", b"binary\0\xff")]), home, tmp_path)
    assert sentinel.read_bytes() == b"unchanged"
    assert not list((home / ".local/share/long-running-agent/inbox").rglob(".incoming-*"))
    assert not list((home / ".local/share/long-running-agent/inbox").rglob("image.png"))


@pytest.mark.parametrize("link", [False, True], ids=["regular-destination", "symlink-destination"])
def test_atomic_publication_refuses_existing_file_or_symlink(tmp_path: Path, *, link: bool) -> None:
    target = tmp_path / "keep"
    target.write_bytes(b"original")
    selected = tmp_path / "selected.png"
    if link:
        selected.symlink_to(target)
    else:
        selected.write_bytes(b"existing")
    folder = transfer.directory(tmp_path)
    try:
        with pytest.raises(FileExistsError), transfer.new_file(folder, selected.name) as descriptor:
            os.write(descriptor, b"new data")
    finally:
        os.close(folder)
    assert target.read_bytes() == b"original"
    assert selected.is_symlink() if link else selected.read_bytes() == b"existing"
    assert not list(tmp_path.glob(".incoming-*"))


def test_inbox_cannot_follow_existing_ancestor_symlink(home: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (home / ".local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(OSError, match=r"Not a directory|Too many levels"):
        receive_bytes(upload_bytes([SelectedFile("image.png", b"selected")]), home, tmp_path)
    assert not list(outside.iterdir())


def test_existing_public_inbox_is_rejected_without_changing_permissions(home: Path, tmp_path: Path) -> None:
    inbox = home / ".local/share/long-running-agent/inbox"
    inbox.mkdir(parents=True, mode=0o755)
    inbox.chmod(0o755)
    with pytest.raises(RuntimeError, match="VM inbox ancestors"):
        receive_bytes(upload_bytes([SelectedFile("image.png", b"selected")]), home, tmp_path)
    assert inbox.stat().st_mode & 0o777 == 0o755


def test_native_send_reads_only_selected_owned_file_and_preserves_binary_stream(home: Path, tmp_path: Path) -> None:
    selected = home / "literal ; $(command) 雪.png"
    payload = bytes(range(256)) * 300
    selected.write_bytes(payload)
    destination = tmp_path / "native-download-stream"
    with destination.open("wb") as stream:
        receipt = transfer.send(selected, stream, home=home)
    header, actual = destination.read_bytes().split(b"\n", 1)
    metadata = transfer.FileSpec.model_validate_json(header)
    assert metadata.name == selected.name
    assert metadata.size == len(payload)
    assert metadata.sha256 == hashlib.sha256(payload).hexdigest()
    assert actual == payload
    assert receipt.path == str(selected)


@pytest.mark.parametrize("kind", ["outside", "relative", "leaf-link", "ancestor-link", "hard-link", "fifo"])
def test_unknown_vm_source_paths_are_never_read(home: Path, tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"must remain private")
    selected = home / "selected.bin"
    match kind:
        case "outside":
            selected = outside
        case "relative":
            selected = Path("relative.bin")
        case "leaf-link":
            selected.symlink_to(outside)
        case "ancestor-link":
            (home / "alias").symlink_to(tmp_path, target_is_directory=True)
            selected = home / "alias/outside.bin"
        case "hard-link":
            os.link(outside, selected)
        case _:
            os.mkfifo(selected)
    destination = tmp_path / "output"
    with destination.open("wb") as stream, pytest.raises((RuntimeError, OSError)):
        transfer.send(selected, stream, home=home)
    assert destination.read_bytes() == b""
    assert outside.read_bytes() == b"must remain private"


def test_sparse_oversized_source_fails_before_hashing_or_streaming(home: Path, tmp_path: Path) -> None:
    selected = home / "large.bin"
    with selected.open("wb") as stream:
        stream.truncate(transfer.MAX_FILE_BYTES + 1)
    destination = tmp_path / "output"
    with destination.open("wb") as stream, pytest.raises(RuntimeError, match="100 MiB"):
        transfer.send(selected, stream, home=home)
    assert destination.stat().st_size == 0


@pytest.mark.parametrize("name", ["../escape", "directory/image.png", "line\nbreak.png", "back\\slash", ".", ".."])
def test_manifest_rejects_unsafe_names_before_creating_inbox(home: Path, tmp_path: Path, name: str) -> None:
    payload = (
        json.dumps({"version": 1, "files": [{"name": name, "size": 0, "sha256": hashlib.sha256(b"").hexdigest()}]})
        + "\n"
    ).encode()
    with pytest.raises(ValueError, match=r"Transfer|transfer"):
        receive_bytes(payload, home, tmp_path)
    assert not (home / ".local").exists()


def test_metadata_and_stalled_pipe_reads_obey_native_bounds(home: Path, tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="metadata exceeded"):
        receive_bytes(b"x" * (transfer.MAX_METADATA_BYTES + 1), home, tmp_path)
    read, write = os.pipe()
    try:
        with pytest.raises(TimeoutError, match="bounded deadline"):
            transfer.Channel(read, time.monotonic() + 0.01).read(1)
    finally:
        os.close(read)
        os.close(write)


def test_native_changed_source_is_detected_before_second_pass(home: Path) -> None:
    selected = home / "selected.bin"
    selected.write_bytes(b"first")
    with transfer.source_file(selected) as descriptor:
        file = transfer.specification(selected, descriptor, time.monotonic() + 10)
        selected.write_bytes(b"other")
        read, write = os.pipe()
        try:
            with pytest.raises(RuntimeError, match="checksum"):
                transfer.copy_file(
                    transfer.Channel(descriptor, time.monotonic() + 10),
                    transfer.Channel(write, time.monotonic() + 10),
                    file,
                )
        finally:
            os.close(read)
            os.close(write)
