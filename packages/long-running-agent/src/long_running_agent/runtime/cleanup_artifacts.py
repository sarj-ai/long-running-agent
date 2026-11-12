from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
from typing import TYPE_CHECKING, Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import cleanup, cleanup_activity, cleanup_node_cache, cleanup_providers
from .cleanup_state import private_payload
from .settings import private_write


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from .settings import RuntimePaths


RETENTION_SECONDS: Final = 2 * 24 * 60 * 60
SWEEP_SECONDS: Final = 60
MAX_REMOVALS: Final = 2
MAX_ROOTS: Final = 8192
MAX_CANDIDATES: Final = 16
MAX_ENTRIES: Final = 4096
MOUNT_FIELDS: Final = 6
MOUNT_BYTES: Final = 1024 * 1024
CURSOR: Final = "cleanup-artifact-cursor.json"
TEMPORARY_ROOT: Final = Path("/var/tmp")  # ruff: ignore[hardcoded-temp-file] — Inventory only; removals require ownership and fresh identity, mount and activity proofs.
PROVIDER_PATHS: Final = (
    ".cache/terraform/providers",
    ".terraform/providers",
    "terraform/.terraform/providers",
    "terraform-data/providers",
)
CACHE_PATHS: Final = (*PROVIDER_PATHS, "node-compile-cache")
PROVIDER_DIRECTORIES: Final = frozenset(
    parent
    for relative in PROVIDER_PATHS
    for parent in (Path(relative), *Path(relative).parents)
    if parent not in {Path(), Path("terraform")}
)


class Report(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    inspected: Annotated[int, Field(ge=0, le=MAX_CANDIDATES)] = 0
    removed: Annotated[int, Field(ge=0, le=MAX_REMOVALS)] = 0
    reclaimed_bytes: Annotated[int, Field(ge=0)] = 0
    reasons: Annotated[dict[str, Annotated[int, Field(ge=1)]], Field(max_length=MAX_CANDIDATES + 1)] = Field(
        default_factory=dict
    )

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.removed > self.inspected or (not self.removed and self.reclaimed_bytes):
            message = "Artifact cleanup returned inconsistent counts"
            raise ValueError(message)
        return self


@dataclass(frozen=True)
class Cache:
    root: Path
    path: Path
    device: int
    inode: int
    modified: int
    changed: int
    digest: bytes
    allocated: int


def mounted(target: Path, *, root: Path, table: Path = Path("/proc/self/mountinfo")) -> bool:
    with table.open("rb") as stream:
        payload = stream.read(MOUNT_BYTES + 1)
    if len(payload) > MOUNT_BYTES:
        return True
    for line in payload.decode().splitlines():
        fields = line.split()
        if len(fields) < MOUNT_FIELDS:
            return True
        value = fields[4]
        for escaped, literal in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            value = value.replace(escaped, literal)
        mount = Path(value)
        if mount.is_relative_to(target) or (mount.is_relative_to(root) and target.is_relative_to(mount)):
            return True
    return False


def empty_provider_directory(root: Path, relative: Path) -> bool:
    if relative not in PROVIDER_DIRECTORIES:
        return False
    pending = [relative]
    while pending:
        cleanup.inspection_check()
        target = root / pending.pop()
        if not cleanup.owned_path(target, root) or mounted(target, root=root):
            return False
        with os.scandir(target) as entries:
            for entry in entries:
                child = Path(entry.path).relative_to(root)
                if child not in PROVIDER_DIRECTORIES or not entry.is_dir(follow_symlinks=False):
                    return False
                pending.append(child)
    return True


def checked_metadata(item: Path, *, device: int, now: float) -> os.stat_result:
    metadata = item.lstat()
    if (
        metadata.st_uid != os.getuid()
        or metadata.st_mode & (0o022 | stat.S_ISUID | stat.S_ISGID)
        or metadata.st_dev != device
        or not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode))
    ):
        message = "unsafe cache contents"
        raise ValueError(message)
    if max(metadata.st_mtime, metadata.st_ctime) > now - RETENTION_SECONDS:
        message = "cache contents are too recent"
        raise ValueError(message)
    return metadata


def fingerprint(root: Path, target: Path, *, now: float) -> Cache:
    if not cleanup.owned_path(target, root) or mounted(target, root=root):
        message = "unsafe ownership, link, or mount"
        raise ValueError(message)
    observed = target.lstat()
    digest = hashlib.sha256()
    pending = [target]
    count = allocated = 0
    maximum = cleanup_node_cache.MAX_ENTRIES if target == root / "node-compile-cache" else MAX_ENTRIES
    while pending:
        cleanup.inspection_check()
        item = pending.pop()
        metadata = checked_metadata(item, device=observed.st_dev, now=now)
        count += 1
        if count > maximum:
            message = "inspection deferred: cache entry budget"
            raise ValueError(message)
        digest.update(os.fsencode(item.relative_to(target)))
        digest.update(
            str(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                    metadata.st_nlink,
                )
            ).encode()
        )
        # Shared inodes can remain allocated elsewhere; never claim those bytes.
        if metadata.st_nlink == 1 and stat.S_ISREG(metadata.st_mode):
            allocated += metadata.st_blocks * 512
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(item) as entries:
                for entry in entries:
                    if len(pending) + count >= maximum:
                        message = "inspection deferred: cache entry budget"
                        raise ValueError(message)
                    pending.append(Path(entry.path))
    return Cache(
        root,
        target,
        observed.st_dev,
        observed.st_ino,
        observed.st_mtime_ns,
        observed.st_ctime_ns,
        digest.digest(),
        allocated,
    )


def git_arguments(root: Path) -> tuple[str, ...]:
    bare = cleanup.git(root, "rev-parse", "--is-bare-repository").strip()
    if bare == "false":
        return ()
    entries = cleanup.parse_worktrees(cleanup.git(root, "worktree", "list", "--porcelain", "-z"), root)
    if bare != "true" or not entries or not entries[0].bare or entries[0].path != root:
        message = "Bare-primary cache root is unverified"
        raise ValueError(message)
    # Inspect this verified primary's downloads without changing core.bare or
    # making its shared Git storage eligible for checkout retirement.
    return (f"--work-tree={root}",)


def inspect(root: Path, target: Path, *, now: float) -> Cache:
    observed = fingerprint(root, target, now=now)
    node_cache = target == root / "node-compile-cache"
    recognized = cleanup_node_cache.installed(target) if node_cache else cleanup_providers.installed(target)
    if not recognized:
        message = "unrecognized Node compile cache contents" if node_cache else "unrecognized provider package contents"
        raise ValueError(message)
    if cleanup_activity.present(root / ".git"):
        arguments = git_arguments(root)
        if Path(cleanup.git(root, *arguments, "rev-parse", "--show-toplevel").strip()) != root:
            message = "Git checkout root differs"
            raise ValueError(message)
        relative = str(target.relative_to(root))
        if cleanup.git_process(root, *arguments, "check-ignore", "-q", "--", relative, check=False).returncode:
            message = "cache directory is not ignored"
            raise ValueError(message)
        literal = f":(literal){relative}"
        if cleanup.git(root, *arguments, "ls-files", "-z", "--", literal) or cleanup.git(
            root, *arguments, "ls-tree", "-r", "--name-only", "-z", "HEAD", "--", literal
        ):
            message = "cache directory contains tracked files"
            raise ValueError(message)
        directory = Path(cleanup.git(root, "rev-parse", "--absolute-git-dir").strip())
        if any(cleanup_activity.present(directory / marker) for marker in cleanup.OPERATIONS):
            message = "Git operation is in progress"
            raise ValueError(message)
    elif not node_cache and not root.name.startswith(("sarj-", "lra-")):
        message = "temporary directory has no recognized fixture owner"
        raise ValueError(message)
    if fingerprint(root, target, now=now) != observed:
        message = "cache contents changed during inspection"
        raise ValueError(message)
    return observed


def remove(paths: RuntimePaths, observed: Cache) -> str:
    # Worktree locks, source edits and unmerged commits protect the checkout;
    # they do not describe whether these validated downloads are disposable.
    if inspect(observed.root, observed.path, now=time.time()) != observed:
        return "cache contents changed before removal"
    protected = cleanup.fresh_references(paths, observed.root)
    if protected.contains(observed.root):
        return "referenced by process, workspace, terminal, or container"
    if not shutil.rmtree.avoids_symlink_attacks or mounted(observed.path, root=observed.root):
        return "unsafe removal or mount"
    if fingerprint(observed.root, observed.path, now=time.time()) != observed:
        return "cache contents changed after live inspection"
    cleanup.inspection_check()
    receipt = (
        paths.home
        / ".local/state/long-running-agent/cleanup-artifact-removals"
        / (hashlib.sha256(os.fsencode(observed.path) + observed.digest).hexdigest() + ".json")
    )
    receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cleanup.owned_path(receipt.parent, paths.home, require_private=True):
        return "cleanup receipt directory is unsafe"
    event = {"at": time.time(), "path": str(observed.path), "reclaimed_bytes": observed.allocated, "removed": False}
    private_write(receipt, json.dumps(event) + "\n")
    parent = os.open(observed.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.stat(observed.path.name, dir_fd=parent, follow_symlinks=False)
        if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_ctime_ns) != (
            observed.device,
            observed.inode,
            observed.modified,
            observed.changed,
        ):
            return "cache directory identity changed"
        shutil.rmtree(observed.path.name, dir_fd=parent)
    finally:
        os.close(parent)
    event["removed"] = True
    private_write(receipt, json.dumps(event) + "\n")
    return "removed"


def candidates(parents: Sequence[Path]) -> list[tuple[Path, Path]]:
    found: list[tuple[Path, Path]] = []
    inspected = 0
    # Fixed depth avoids traversing source, dependency trees and private stores.
    for parent in parents:
        if not parent.is_dir():
            continue
        with os.scandir(parent) as roots:
            for entry in roots:
                cleanup.inspection_check()
                inspected += 1
                if inspected > MAX_ROOTS:
                    message = "inspection deferred: cache root budget"
                    raise ValueError(message)
                if (
                    entry.name.startswith(".")
                    or not entry.is_dir(follow_symlinks=False)
                    or entry.stat(follow_symlinks=False).st_uid != os.getuid()
                ):
                    continue
                root = Path(entry.path)
                for relative in CACHE_PATHS:
                    target = root / relative
                    if cleanup_activity.present(target):
                        found.append((root, target))
    return sorted(found, key=lambda item: str(item[1]))


@contextmanager
def legacy_lock(paths: RuntimePaths) -> Generator[None]:
    directory = paths.home / ".local/state/sarj-disk-cleaner"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cleanup.owned_path(directory, paths.home, require_private=True):
        message = "Legacy cleanup lock directory is unsafe"
        raise RuntimeError(message)
    descriptor = os.open(directory / "run.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            message = "Legacy cleanup lock is unsafe"
            raise RuntimeError(message)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def recover(paths: RuntimePaths, state: Path, *, deadline: float, temporary: Path = TEMPORARY_ROOT) -> Report:
    report = Report()
    reasons: Counter[str] = Counter()
    outcomes: list[dict[str, str]] = []
    with cleanup.inspection_window(deadline):
        try:
            with legacy_lock(paths):
                report = recover_locked(paths, state, deadline=deadline, temporary=temporary, outcomes=outcomes)
        except OSError, RuntimeError, ValueError, cleanup.LiveInspectionFailedError, cleanup.InspectionDeferredError:
            reasons["inspection failed or deferred; remaining caches retained"] += 1
    report.reasons.update(reasons)
    private_write(
        state / "cleanup-artifacts.json",
        json.dumps({"at": time.time(), "report": report.model_dump(), "outcomes": outcomes}) + "\n",
    )
    return report


def recover_locked(
    paths: RuntimePaths, state: Path, *, deadline: float, temporary: Path, outcomes: list[dict[str, str]]
) -> Report:
    report = Report()
    reasons: Counter[str] = Counter()
    rows = candidates((paths.code / "worktrees", paths.code / "sarj-ai", temporary))
    cursor = private_payload(state / CURSOR).decode().strip() if (state / CURSOR).exists() else ""
    rows.sort(key=lambda row: (str(row[1]) <= cursor, str(row[1])))
    for root, target in rows[:MAX_CANDIDATES]:
        if report.removed >= MAX_REMOVALS or time.monotonic() >= deadline:
            break
        report.inspected += 1
        stop = False
        try:
            observed = inspect(root, target, now=time.time())
            reason = remove(paths, observed)
            if reason == "removed":
                report.removed += 1
                report.reclaimed_bytes += observed.allocated
        except ValueError as error:
            reason = str(error)[:160] or "inspection failed"
        except OSError, RuntimeError:
            reason = "inspection failed: cache filesystem or Git check unavailable"
            stop = True
        except cleanup.LiveInspectionFailedError, cleanup.InspectionDeferredError:
            reason = "inspection failed or deferred; cache retained"
            stop = True
        reasons[reason] += 1
        outcomes.append({"path": str(target), "reason": reason})
        private_write(state / CURSOR, str(target) + "\n")
        if stop:
            break
    report.reasons = dict(reasons)
    return report
