from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import time
from typing import Final


NAME: Final = r"[a-z0-9][a-z0-9_-]{0,127}"
VERSION: Final = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?"
PLATFORM: Final = r"linux_(?:amd64|arm64|386)"
MAX_CHILDREN: Final = 256
MAX_PACKAGES: Final = 256
SECONDS: Final = 2
DOCUMENTS: Final = frozenset(
    {"LICENSE", "LICENSE.txt", "LICENSE.md", "README", "README.md", "NOTICE", "NOTICE.txt", "CHANGELOG.md"}
)


def owned_directory(path: Path) -> bool:
    metadata = path.lstat()
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid() and not metadata.st_mode & 0o022


def children(path: Path, pattern: str, deadline: float) -> tuple[Path, ...] | None:
    if not owned_directory(path):
        return None
    found: list[Path] = []
    with os.scandir(path) as entries:
        for entry in entries:
            if time.monotonic() >= deadline or len(found) >= MAX_CHILDREN or re.fullmatch(pattern, entry.name) is None:
                return None
            found.append(Path(entry.path))
    return tuple(found)


def regular_file(path: Path, *, executable: bool) -> bool:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
        or (executable and not metadata.st_mode & stat.S_IXUSR)
    ):
        return False
    if not executable:
        return True
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        return (opened.st_dev, opened.st_ino, opened.st_mode) == (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_mode,
        ) and os.read(descriptor, 4) == b"\x7fELF"
    finally:
        os.close(descriptor)


def package(path: Path, provider: str, version: str, deadline: float) -> int:
    # Terraform's shared plugin cache may link an entire platform package.
    if path.is_symlink():
        return 0
    if not owned_directory(path):
        return -1
    binary = rf"terraform-provider-{re.escape(provider)}(?:_v{re.escape(version)}(?:_x[0-9]+)?)?"
    binaries = entries_seen = 0
    with os.scandir(path) as entries:
        for entry in entries:
            entries_seen += 1
            if time.monotonic() >= deadline or entries_seen > MAX_CHILDREN:
                return -1
            if re.fullmatch(binary, entry.name) is not None:
                if not regular_file(Path(entry.path), executable=True):
                    return -1
                binaries += 1
            elif entry.name not in DOCUMENTS or not regular_file(Path(entry.path), executable=False):
                return -1
    return 1 if binaries == 1 else -1


def package_paths(path: Path, deadline: float) -> list[Path] | None:
    pending = [path]
    for pattern in (r"registry\.terraform\.io", NAME, NAME, VERSION, PLATFORM):
        following: list[Path] = []
        for parent in pending:
            found = children(parent, pattern, deadline)
            if found is None or len(following) + len(found) > MAX_PACKAGES:
                return None
            following.extend(found)
        pending = following
    return pending


def installed(path: Path) -> bool:
    deadline = time.monotonic() + SECONDS
    regular_packages = 0
    try:
        packages = package_paths(path, deadline)
        if not packages:
            return False
        for platform in packages:
            result = package(platform, platform.parent.parent.name, platform.parent.name, deadline)
            if result < 0:
                return False
            regular_packages += result
    except OSError:
        return False
    return regular_packages > 0 and time.monotonic() < deadline
