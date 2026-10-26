from __future__ import annotations

from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import re
import secrets
import select
import stat
import sys
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator
import typer


if TYPE_CHECKING:
    from collections.abc import Generator
    from typing import IO, BinaryIO, Self


MAX_FILES: Final = 16
MAX_FILE_BYTES: Final = 100 * 1024 * 1024
MAX_METADATA_BYTES: Final = 16384
MAX_RECEIPT_BYTES: Final = 131072
TRANSFER_SECONDS: Final = 120
CHUNK_BYTES: Final = 65536
MAX_NAME_BYTES: Final = 255
app = typer.Typer(help="Exchange explicitly selected files through private SSH streams.", no_args_is_help=True)


def basename(value: str) -> str:
    name = re.sub(r"[\x00-\x1f\x7f/\\]", "_", value)
    name = "".join(character if character.isprintable() else "_" for character in name)
    if name in {"", ".", ".."} or len(os.fsencode(name)) > MAX_NAME_BYTES:
        message = "Transfer filename is empty or exceeds the native filename limit"
        raise ValueError(message)
    return name


class FileSpec(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    name: Annotated[str, Field(min_length=1, max_length=255)]
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if basename(value) != value:
            message = "Transfer metadata requires a single safe filename"
            raise ValueError(message)
        return value


class Manifest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    version: Literal[1]
    files: Annotated[list[FileSpec], Field(min_length=1, max_length=MAX_FILES)]

    @model_validator(mode="after")
    def unique_names(self) -> Self:
        if len({file.name for file in self.files}) != len(self.files):
            message = "Transfer filenames must be distinct after normalization"
            raise ValueError(message)
        return self


class Receipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    size: Annotated[int, Field(ge=0, le=MAX_FILE_BYTES)]
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

    @field_validator("path")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\0" in value:
            message = "Transfer receipt has an unknown destination path"
            raise ValueError(message)
        return value


class Receipts(RootModel[list[Receipt]]):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)
    root: list[Receipt] = Field(min_length=1, max_length=MAX_FILES)


@dataclass(frozen=True)
class FileState:
    device: int
    inode: int
    size: int
    modified: int
    changed: int

    @classmethod
    def read(cls, descriptor: int) -> Self:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            message = "Select a regular file without symbolic or hard links"
            raise RuntimeError(message)
        if metadata.st_size > MAX_FILE_BYTES:
            message = "A selected file exceeds the 100 MiB transfer limit"
            raise RuntimeError(message)
        return cls(metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)


def remaining(deadline: float) -> float:
    duration = deadline - time.monotonic()
    if duration <= 0:
        message = "File transfer exceeded its bounded deadline"
        raise TimeoutError(message)
    return duration


@dataclass(frozen=True)
class Channel:
    descriptor: int
    deadline: float

    def read(self, maximum: int) -> bytes:
        while True:
            if not select.select([self.descriptor], [], [], remaining(self.deadline))[0]:
                remaining(self.deadline)
                continue
            try:
                return os.read(self.descriptor, maximum)
            except BlockingIOError:
                continue

    def write(self, payload: bytes) -> None:
        pending = memoryview(payload)
        while pending:
            if not select.select([], [self.descriptor], [], remaining(self.deadline))[1]:
                remaining(self.deadline)
                continue
            try:
                written = os.write(self.descriptor, pending)
            except BlockingIOError:
                continue
            if written == 0:
                message = "File transfer stream closed before completion"
                raise RuntimeError(message)
            pending = pending[written:]

    def line(self) -> bytes:
        payload = bytearray()
        while len(payload) <= MAX_METADATA_BYTES:
            value = self.read(1)
            if value == b"\n":
                return bytes(payload)
            if not value:
                message = "File transfer metadata was interrupted"
                raise RuntimeError(message)
            payload.extend(value)
        message = "File transfer metadata exceeded its bounded size"
        raise RuntimeError(message)

    def end(self) -> None:
        if self.read(1):
            message = "File transfer contains unexpected trailing bytes"
            raise RuntimeError(message)


@contextmanager
def channel(stream: IO[bytes], deadline: float) -> Generator[Channel]:
    descriptor = stream.fileno()
    blocking = os.get_blocking(descriptor)
    os.set_blocking(descriptor, False)
    try:
        yield Channel(descriptor, deadline)
    finally:
        os.set_blocking(descriptor, blocking)


@contextmanager
def source_file(path: Path) -> Generator[int]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        FileState.read(descriptor)
        yield descriptor
    finally:
        os.close(descriptor)


def specification(path: Path, descriptor: int, deadline: float) -> FileSpec:
    before = FileState.read(descriptor)
    digest = hashlib.sha256()
    size = 0
    while True:
        remaining(deadline)
        payload = os.read(descriptor, CHUNK_BYTES)
        if not payload:
            break
        size += len(payload)
        if size > MAX_FILE_BYTES:
            message = "A selected file grew beyond the transfer limit"
            raise RuntimeError(message)
        digest.update(payload)
    if FileState.read(descriptor) != before or size != before.size:
        message = "A selected file changed while preparing the transfer"
        raise RuntimeError(message)
    os.lseek(descriptor, 0, os.SEEK_SET)
    return FileSpec(name=basename(path.name), size=size, sha256=digest.hexdigest())


def copy_file(source: Channel, destination: Channel, file: FileSpec) -> None:
    digest = hashlib.sha256()
    size = 0
    while size < file.size:
        payload = source.read(min(CHUNK_BYTES, file.size - size))
        if not payload:
            message = "File transfer was interrupted before all bytes arrived"
            raise RuntimeError(message)
        destination.write(payload)
        size += len(payload)
        digest.update(payload)
    if digest.hexdigest() != file.sha256:
        message = "File transfer checksum did not match; destination was not published"
        raise RuntimeError(message)


def directory(path: Path) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    metadata = os.fstat(descriptor)
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        os.close(descriptor)
        message = "Transfer directory must be owned and writable only by your user"
        raise RuntimeError(message)
    return descriptor


@contextmanager
def new_file(folder: int, name: str) -> Generator[int]:
    temporary = ".incoming-" + secrets.token_hex(16)
    descriptor = os.open(
        temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder
    )
    try:
        yield descriptor
        os.fsync(descriptor)
        os.link(temporary, name, src_dir_fd=folder, dst_dir_fd=folder, follow_symlinks=False)
        os.fsync(folder)
    finally:
        os.close(descriptor)
        os.unlink(temporary, dir_fd=folder)


def owned_home_path(path: Path, home: Path) -> Path:
    if not path.is_absolute() or not path.is_relative_to(home) or ".." in path.parts:
        message = "VM transfers require an explicit path inside your own home"
        raise RuntimeError(message)
    for component in (path, *path.parents):
        metadata = component.lstat()
        if metadata.st_uid != os.getuid() or stat.S_ISLNK(metadata.st_mode):
            message = "VM transfer paths must be owned and contain no symbolic links"
            raise RuntimeError(message)
        if component != path and (not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & 0o022):
            message = "VM transfer ancestors must be private writable directories"
            raise RuntimeError(message)
        if component == home:
            break
    return path


@contextmanager
def vm_source(path: Path, home: Path) -> Generator[int]:
    owned_home_path(path, home)
    parent = directory(home)
    try:
        for name in path.relative_to(home).parts[:-1]:
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            metadata = os.fstat(child)
            if metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
                os.close(child)
                message = "VM source ancestors changed ownership or permissions"
                raise RuntimeError(message)
            os.close(parent)
            parent = child
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=parent)
        try:
            if os.fstat(descriptor).st_uid != os.getuid():
                message = "VM source file is not owned by your user"
                raise RuntimeError(message)
            FileState.read(descriptor)
            yield descriptor
        finally:
            os.close(descriptor)
    finally:
        os.close(parent)


def inbox(home: Path) -> Path:
    owned_home_path(home, home)
    parent = directory(home)
    try:
        for name in (".local", "share", "long-running-agent", "inbox"):
            with suppress(FileExistsError):
                os.mkdir(name, mode=0o700, dir_fd=parent)
            child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            metadata = os.fstat(child)
            mask = 0o077 if name == "inbox" else 0o022
            if metadata.st_uid != os.getuid() or metadata.st_mode & mask:
                os.close(child)
                message = "VM inbox ancestors must be owned and writable only by your user"
                raise RuntimeError(message)
            os.close(parent)
            parent = child
        name = secrets.token_hex(16)
        os.mkdir(name, mode=0o700, dir_fd=parent)
        return home / ".local/share/long-running-agent/inbox" / name
    finally:
        os.close(parent)


def receive(source: BinaryIO, *, home: Path) -> list[Receipt]:
    deadline = time.monotonic() + TRANSFER_SECONDS
    with channel(source, deadline) as incoming:
        manifest = Manifest.model_validate_json(incoming.line())
        folder = inbox(home)
        descriptor = directory(folder)
        receipts: list[Receipt] = []
        try:
            with ExitStack() as outputs:
                for file in manifest.files:
                    output = outputs.enter_context(new_file(descriptor, file.name))
                    copy_file(incoming, Channel(output, deadline), file)
                    receipts.append(Receipt(path=str(folder / file.name), size=file.size, sha256=file.sha256))
                incoming.end()
        finally:
            os.close(descriptor)
    return receipts


def send(path: Path, destination: BinaryIO, *, home: Path) -> Receipt:
    deadline = time.monotonic() + TRANSFER_SECONDS
    with vm_source(path, home) as descriptor, channel(destination, deadline) as outgoing:
        before = FileState.read(descriptor)
        file = specification(path, descriptor, deadline)
        outgoing.write(file.model_dump_json().encode() + b"\n")
        copy_file(Channel(descriptor, deadline), outgoing, file)
        if FileState.read(descriptor) != before:
            message = "Selected VM file changed during transfer"
            raise RuntimeError(message)
    return Receipt(path=str(path), size=file.size, sha256=file.sha256)


@app.command("receive")
def receive_command() -> None:
    receipts = receive(sys.stdin.buffer, home=Path.home())
    payload = Receipts(receipts).model_dump_json().encode() + b"\n"
    with channel(sys.stdout.buffer, time.monotonic() + TRANSFER_SECONDS) as outgoing:
        outgoing.write(payload)


@app.command("send")
def send_command(path: str) -> None:
    selected = Path.home() / path[2:] if path.startswith("~/") else Path(path)
    send(selected, sys.stdout.buffer, home=Path.home())
