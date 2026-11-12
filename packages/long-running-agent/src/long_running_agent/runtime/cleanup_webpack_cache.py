from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import struct
import time
from typing import Final

from . import cleanup


PATHS: Final = (
    ".next/cache/webpack/client-production",
    ".next/cache/webpack/server-production",
    ".next/cache/webpack/edge-server-production",
)
# Native format: webpack v5.98.0 lib/serialization/FileMiddleware.js.
MAGIC: Final = 0x0163_7077
HEADER: Final = struct.Struct("<II")
SECTION: Final = struct.Struct("<i")
POINTER: Final = struct.Struct("<Q")
MAX_PACK_NAME_BYTES: Final = 4
MAX_FILES: Final = 128
MAX_SECTIONS: Final = 4096
MAX_FILE_BYTES: Final = 256 * 1024 * 1024
MAX_BYTES: Final = 256 * 1024 * 1024
SECONDS: Final = 5
PACK_NAME: Final = re.compile(r"(?:0|[1-9][0-9]{0,3})")


@dataclass(frozen=True)
class Reference:
    name: str
    size: int


@dataclass(frozen=True)
class Pack:
    size: int
    references: dict[str, int]


def within_budget(deadline: float) -> bool:
    cleanup.inspection_check()
    return time.monotonic() < deadline


def pointer_reference(descriptor: int, offset: int, length: int) -> Reference | None:
    if not POINTER.size < length <= POINTER.size + MAX_PACK_NAME_BYTES:
        return None
    pointer = os.pread(descriptor, length, offset)
    if len(pointer) != length:
        return None
    name = pointer[POINTER.size :].decode("ascii")
    size = int.from_bytes(pointer[: POINTER.size], "little")
    if PACK_NAME.fullmatch(name) is None or not HEADER.size < size <= MAX_FILE_BYTES:
        return None
    return Reference(name + ".pack", size)


def pack_references(descriptor: int, size: int, deadline: float) -> dict[str, int] | None:
    header = os.pread(descriptor, HEADER.size, 0)
    if len(header) != HEADER.size:
        return None
    magic = int.from_bytes(header[: SECTION.size], "little")
    count = int.from_bytes(header[SECTION.size :], "little")
    if magic != MAGIC or not 0 < count <= MAX_SECTIONS:
        return None
    table = os.pread(descriptor, count * SECTION.size, HEADER.size)
    if len(table) != count * SECTION.size:
        return None
    offset = HEADER.size + len(table)
    references: dict[str, int] = {}
    for cursor in range(0, len(table), SECTION.size):
        length = int.from_bytes(table[cursor : cursor + SECTION.size], "little", signed=True)
        if not within_budget(deadline) or offset + abs(length) > size:
            return None
        if length < 0:
            reference = pointer_reference(descriptor, offset, -length)
            if reference is None:
                return None
            if reference.name in references and references[reference.name] != reference.size:
                return None
            references[reference.name] = reference.size
        offset += abs(length)
    return references if offset == size else None


def pack_file(path: Path, deadline: float) -> Pack | None:
    before = path.lstat()
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
        or not HEADER.size < before.st_size <= MAX_FILE_BYTES
    ):
        return None
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if os.fstat(descriptor) != before or not within_budget(deadline):
            return None
        references = pack_references(descriptor, before.st_size, deadline)
        if references is None:
            return None
        after = os.fstat(descriptor)
        if (
            before[:7] != after[:7]
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ctime_ns != after.st_ctime_ns
        ):
            return None
    finally:
        os.close(descriptor)
    return Pack(before.st_size, references)


def pack_files(target: Path, deadline: float) -> dict[str, Pack] | None:
    metadata = target.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        return None
    files: dict[str, Pack] = {}
    total = 0
    with os.scandir(target) as entries:
        for entry in entries:
            if len(files) >= MAX_FILES or not within_budget(deadline):
                return None
            if entry.name != "index.pack" and (
                not entry.name.endswith(".pack") or PACK_NAME.fullmatch(entry.name[:-5]) is None
            ):
                return None
            pack = pack_file(Path(entry.path), deadline)
            if pack is None:
                return None
            total += pack.size
            if total > MAX_BYTES:
                return None
            files[entry.name] = pack
    return files


def valid_files(target: Path, deadline: float) -> bool:
    files = pack_files(target, deadline)
    if files is None or "index.pack" not in files:
        return False
    referenced: set[str] = set()
    pending = ["index.pack"]
    while pending:
        current = pending.pop()
        if current in referenced:
            continue
        if not within_budget(deadline):
            return False
        referenced.add(current)
        for name, size in files[current].references.items():
            if name not in files or files[name].size != size:
                return False
            if name not in referenced:
                pending.append(name)
    return referenced == files.keys() and within_budget(deadline)


def installed(target: Path) -> bool:
    try:
        return valid_files(target, time.monotonic() + SECONDS)
    except OSError, ValueError:
        return False
