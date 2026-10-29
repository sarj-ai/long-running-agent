from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import errno
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel
import typer

from long_running_agent.process import CommandError, environment_snapshot, run

from . import cleanup_activity, cleanup_capacity, cleanup_inspection, cleanup_publication
from .cleanup_state import (
    CAPACITY_ATTEMPTS,
    MAX_ATTEMPTS,
    AttemptStamp,
    InventoryCursor,
    ReviewCursor,
    active_receipts,
    inventory_cursor,
    remaining_attempts,
    reserve_attempt,
    review_cursor,
    save_inventory_cursor,
    save_review_cursor,
    valid_time,
)
from .health import pressure
from .settings import RuntimePaths, private_write, systemd_value, user_environment
from .worktrees import boot_aliases


if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Mapping, Sequence

    from long_running_agent.process import Completed


DAY: Final = 86400
MAX_REMOVALS: Final = 2
RECOVERY_AGE_DAYS: Final = 0.25
MAX_FETCH_REPOSITORIES: Final = 2
MAX_PUBLICATION_LOOKUPS: Final = 6
CAPACITY_PUBLICATION_LOOKUPS: Final = 24
YARN_STATE_BYTES: Final = 64 * 1024 * 1024
YARN_STATE_HEADER: Final = b"\x1f\x8b\x08"
PUBLICATION_LOOKUPS: Final = ContextVar[int | None]("cleanup_publication_lookups", default=None)
STALE_REMOTE: Final = "remote view is stale; fetch origin before cleanup"
MISSING_DEFAULT_FETCH: Final = "origin's default branch has no recent verified fetch"
MAX_DIRECTORIES: Final = 4096
MAX_DISCOVERY_ENTRIES: Final = 262144
MAX_WORKTREES: Final = 1024
INVENTORY_PAGE_SIZE: Final = 16
INVENTORY_PAGE_SECONDS: Final = 45
MAX_FDS: Final = 32768
GENERATED_ENTRIES: Final = 1048576
GENERATED_DIRECTORIES: Final = 32768
GENERATED_SECONDS: Final = 45
MAX_OBJECT_DEPTH: Final = 16
MAX_PROCESS_BYTES: Final = 1024 * 1024
PROCESS_START_FIELD: Final = 19
PROCESS_START_DIGITS: Final = 20
PROCESS_STATES: Final = frozenset({b"R", b"S", b"D", b"Z", b"T", b"t", b"K", b"W", b"P", b"I"})
DISCOVERY_DEPTH: Final = 2
DISCOVERY_SECONDS: Final = 15
MAP_FIELDS: Final = 6
FETCH_FIELDS: Final = 3
CACHE_IO_THRESHOLD: Final = 10
CACHE_MEMORY_THRESHOLD: Final = 1
CACHE_START_SECONDS: Final = 150
REVIEW_SECONDS: Final = 60
FINAL_PROOF_SECONDS: Final = 150
REPORT_SECONDS: Final = 120
COMMAND_TIMEOUT_STATUS: Final = 124
DEFERRED: Final = "inspection deferred: sweep time budget exhausted; retained"
REFERENCE_ATTEMPTS: Final = 2
REFERENCE_RETRY_SECONDS: Final = 0.05
ReferenceStage = Literal["processes", "terminals", "boot-policy", "containers", "workspaces", "workspace-containers"]
ReferenceKind = Literal[
    "not-found",
    "process-exited",
    "permission",
    "memory",
    "file-limit",
    "io",
    "timeout",
    "database-busy",
    "metadata-changed",
    "invalid-data",
    "command-failed",
    "unavailable",
]
REFERENCE_STAGES: Final = frozenset(
    {"processes", "terminals", "boot-policy", "containers", "workspaces", "workspace-containers"}
)
REFERENCE_KINDS: Final = frozenset(
    {
        "not-found",
        "process-exited",
        "permission",
        "memory",
        "file-limit",
        "io",
        "timeout",
        "database-busy",
        "metadata-changed",
        "invalid-data",
        "command-failed",
        "unavailable",
    }
)
TRANSIENT_REFERENCE_KINDS: Final = frozenset(
    {"not-found", "process-exited", "timeout", "database-busy", "metadata-changed"}
)
REFERENCE_OS_KINDS: Final[dict[int, ReferenceKind]] = {
    errno.ENOENT: "not-found",
    errno.ESRCH: "process-exited",
    errno.EACCES: "permission",
    errno.EPERM: "permission",
    errno.ENOMEM: "memory",
    errno.EMFILE: "file-limit",
    errno.ENFILE: "file-limit",
    errno.EIO: "io",
}
REFERENCED: Final = "referenced by a process, saved workspace, terminal, or boot policy"
INSPECTION_DEADLINE: Final = ContextVar[float | None]("cleanup_inspection_deadline", default=None)
DiscoveryLimitKind = Literal["entries", "directories", "deadline", "worktrees"]
SERVICE: Final = "lra-cleanup.service"
TIMER: Final = "lra-cleanup.timer"
GENERATED: Final = frozenset(
    {
        "node_modules",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".import_linter_cache",
        ".ruff_cache",
        ".turbo",
        ".next",
    }
)
OPERATIONS: Final = (
    "index.lock",
    "HEAD.lock",
    "MERGE_HEAD",
    "CHERRY_PICK_HEAD",
    "REVERT_HEAD",
    "REBASE_HEAD",
    "BISECT_LOG",
    "rebase-merge",
    "rebase-apply",
    "sequencer",
)
app = typer.Typer(
    help="Conservatively retire unused, merged Git worktrees as your own Linux user.", no_args_is_help=True
)


@dataclass(frozen=True)
class Worktree:
    repository: Path
    path: Path
    head: str
    branch: str
    blocked: bool
    detached: bool = False


@dataclass(frozen=True)
class Decision:
    path: str
    reason: str
    removed: bool = False
    attempted: bool = False


@dataclass(frozen=True)
class InventoryPage:
    entries: list[Worktree]
    cursor: InventoryCursor | None
    pending: bool
    errors: int = 0


@dataclass
class ReviewProgress:
    examined: int = 0
    last_started: Worktree | None = None


@dataclass(frozen=True)
class RemovalBudget:
    allowance: int = MAX_REMOVALS
    recent: frozenset[tuple[Path, str]] = frozenset()


@dataclass
class ReviewTiming:
    final_seconds: float = 0.0


@dataclass(frozen=True)
class MergeProof:
    head: str
    origin: str = ""
    branch: str = ""


@dataclass(frozen=True)
class ProcessIdentity:
    device: int
    inode: int
    uid: int
    start: int


@dataclass(frozen=True)
class DirectoryScan:
    children: list[Path]
    inspected: int


@dataclass
class ObjectStorageBudget:
    deadline: float
    inspected: int = 0


@dataclass(frozen=True)
class ObjectDirectoryIdentity:
    device: int
    inode: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class References:
    paths: frozenset[Path]
    arguments: tuple[str, ...] = ()
    package_writer: bool = False
    fd_count: int = 0
    mounted: frozenset[Path] = frozenset()
    workspaces: frozenset[Path] = frozenset()
    workspace_containers: frozenset[Path] = frozenset()
    storage: frozenset[Path] = frozenset()

    def contains(self, candidate: Path) -> bool:
        return (
            any(path.is_relative_to(candidate) for path in self.paths)
            or any(str(candidate) in argument for argument in self.arguments)
            or any(candidate.is_relative_to(path) or path.is_relative_to(candidate) for path in self.mounted)
            or any(candidate.is_relative_to(path) or path.is_relative_to(candidate) for path in self.storage)
            or any(
                path.is_relative_to(candidate)
                or (path not in self.workspace_containers and candidate.is_relative_to(path))
                for path in self.workspaces
            )
        )


@dataclass
class RetentionHints:
    snapshot: References | None = None


RETENTION_HINTS: Final[ContextVar[RetentionHints | None]] = ContextVar("cleanup_retention_hints", default=None)


class Pane(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    pane_cwd: str | None = None
    pane_command: str | None = None
    terminal_command: str | None = None
    is_plugin: bool = False
    is_selectable: bool = True
    is_suppressed: bool = False
    plugin_url: str | None = None


class PaneInventory(RootModel[list[Pane]]):
    model_config = ConfigDict(strict=True)


class Mount(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    kind: str = Field(alias="Type")
    source: str = Field(alias="Source")


class MountInventory(RootModel[list[Mount]]):
    model_config = ConfigDict(strict=True)


class NpmPackage(BaseModel):
    name: Literal["npm"]


class InventoryFailure(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1]
    stage: Literal["repository-discovery"]
    kind: DiscoveryLimitKind


class InventoryLimitError(RuntimeError):
    def __init__(self, message: str, *, kind: DiscoveryLimitKind) -> None:
        super().__init__(message)
        self.kind: DiscoveryLimitKind = kind


class InspectionDeferredError(Exception):
    """An unfinished review must not advance the persistent ordering hint."""


class LiveInspectionFailedError(Exception):
    """Global uncertainty stops the sweep without losing earlier actual outcomes."""


class ReferenceInspectionError(RuntimeError):
    def __init__(self, stage: ReferenceStage, kind: ReferenceKind) -> None:
        super().__init__(f"Live references could not be verified: {stage} ({kind})")
        self.stage: ReferenceStage = stage
        self.kind: ReferenceKind = kind


def reference_failure_kind(error: OSError | RuntimeError | ValueError | sqlite3.Error) -> ReferenceKind:
    if isinstance(error, cleanup_activity.MetadataChangedError):
        return "metadata-changed"
    if isinstance(error, cleanup_activity.DatabaseBusyError):
        return "database-busy"
    if (
        isinstance(error, sqlite3.Error)
        and hasattr(error, "sqlite_errorcode")
        and error.sqlite_errorcode & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    ):
        return "database-busy"
    if isinstance(error, CommandError):
        return "timeout" if error.returncode == COMMAND_TIMEOUT_STATUS else "command-failed"
    if isinstance(error, OSError):
        return REFERENCE_OS_KINDS.get(error.errno or 0, "unavailable")
    return "invalid-data" if isinstance(error, ValueError) else "unavailable"


@contextmanager
def reference_stage(stage: ReferenceStage) -> Generator[None]:
    try:
        yield
    except cleanup_inspection.ProbeUnavailableError:
        # The authenticated probe already has its own bounded retry policy and safe reason codes.
        raise
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        raise ReferenceInspectionError(stage, reference_failure_kind(error)) from error


@contextmanager
def inspection_window(deadline: float | None) -> Generator[None]:
    token = INSPECTION_DEADLINE.set(deadline)
    try:
        yield
    finally:
        INSPECTION_DEADLINE.reset(token)


@contextmanager
def final_inspection_window(deadline: float | None, timing: ReviewTiming | None) -> Generator[None]:
    started = time.monotonic()
    try:
        with inspection_window(deadline):
            yield
    finally:
        if timing is not None:
            timing.final_seconds += max(0.0, time.monotonic() - started)


def inspection_check() -> None:
    deadline = INSPECTION_DEADLINE.get()
    if deadline is not None and time.monotonic() >= deadline:
        raise InspectionDeferredError(DEFERRED)


@contextmanager
def review_window(deadline: float, entry: Worktree, progress: ReviewProgress | None) -> Generator[None]:
    with inspection_window(deadline):
        inspection_check()
        # Count a candidate only after it was admitted to the bounded review.
        if progress is not None:
            progress.examined += 1
            progress.last_started = entry
        yield


def inspection_run(
    arguments: Sequence[str], *, timeout: float, check: bool = True, env: Mapping[str, str] | None = None
) -> Completed:
    inspection_check()
    deadline = INSPECTION_DEADLINE.get()
    bounded = timeout
    if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InspectionDeferredError(DEFERRED)
        bounded = min(timeout, remaining)
    try:
        result = run(arguments, capture_output=True, check=check, timeout=bounded, env=env)
    except CommandError as error:
        if (
            error.returncode == COMMAND_TIMEOUT_STATUS
            and deadline is not None
            and (bounded < timeout or time.monotonic() >= deadline)
        ):
            raise InspectionDeferredError(DEFERRED) from error
        raise
    inspection_check()
    return result


def git_process(path: Path, *arguments: str, timeout: float = 15, check: bool = True) -> Completed:
    environment = environment_snapshot()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_CONFIG",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_PARAMETERS",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        if key in environment:
            message = "Unset Git repository/configuration overrides before cleanup"
            raise RuntimeError(message)
    environment.update(
        {"GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_NO_REPLACE_OBJECTS": "1"}
    )
    return inspection_run(
        ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null", "-C", str(path), *arguments],
        check=check,
        timeout=timeout,
        env=environment,
    )


def git(path: Path, *arguments: str, timeout: float = 15) -> str:
    ancestry = bool(arguments and arguments[0] == "merge-base")
    result = git_process(path, *arguments, timeout=timeout, check=not ancestry)
    if ancestry and (result.stderr or (arguments[:2] == ("merge-base", "--is-ancestor") and result.stdout)):
        message = "Git ancestry inspection reported diagnostics; retained"
        raise RuntimeError(message)
    if result.returncode:
        program = "git"
        raise CommandError(program, result.returncode)
    return result.stdout


def parse_worktrees(text: str, repository: Path) -> list[Worktree]:
    entries: list[Worktree] = []
    for record in text.split("\0\0"):
        if not record:
            continue
        fields: dict[str, str] = {}
        unknown = False
        for field in record.split("\0"):
            if field:
                key, _, value = field.partition(" ")
                unknown |= key in fields or key not in {
                    "worktree",
                    "HEAD",
                    "branch",
                    "locked",
                    "prunable",
                    "bare",
                    "detached",
                }
                fields[key] = value
        path = Path(fields.get("worktree", ""))
        if not path.is_absolute() or not fields.get("HEAD"):
            message = "Git worktree inventory is incomplete; nothing will be removed"
            raise RuntimeError(message)
        entries.append(
            Worktree(
                repository,
                path,
                fields["HEAD"],
                fields.get("branch", ""),
                unknown
                or any(key in fields for key in ("locked", "prunable", "bare"))
                or ("detached" in fields and (bool(fields["detached"]) or "branch" in fields))
                or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields["HEAD"]) is None,
                "detached" in fields,
            )
        )
    return entries


def empty_git_marker(marker: Path) -> bool:
    metadata = marker.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
        return False
    with os.scandir(marker) as entries:
        return next(entries, None) is None


def child_directories(directory: Path, remaining: int) -> DirectoryScan:
    children: list[Path] = []
    inspected = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            inspection_check()
            inspected += 1
            if inspected > remaining:
                message = "Repository discovery exceeded its bounded entry limit; nothing will be removed"
                raise InventoryLimitError(message, kind="entries")
            if not entry.name.startswith(".") and entry.name not in GENERATED and entry.is_dir(follow_symlinks=False):
                children.append(Path(entry.path))
    return DirectoryScan(children, inspected)


def owned_discovery_directory(directory: Path) -> bool:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and metadata.st_uid == os.getuid()


def repositories(root: Path) -> Iterator[Path]:
    pending = deque([(root, 0)])
    inspected = 0
    inspected_entries = 0
    deadline = time.monotonic() + DISCOVERY_SECONDS
    while pending:
        inspection_check()
        directory, depth = pending.popleft()
        inspected += 1
        if inspected > MAX_DIRECTORIES:
            message = "Repository discovery exceeded its bounded inventory; nothing will be removed"
            raise InventoryLimitError(message, kind="directories")
        if time.monotonic() > deadline:
            message = "Repository discovery exceeded its bounded deadline; nothing will be removed"
            raise InventoryLimitError(message, kind="deadline")
        if not owned_discovery_directory(directory):
            continue
        marker = directory / ".git"
        if cleanup_activity.present(marker) and not empty_git_marker(marker):
            # Linked checkouts are inventoried once through their main repository,
            # rather than starting another Git process for every linked tree.
            if marker.is_dir() and not marker.is_symlink():
                yield directory
            continue
        if depth < DISCOVERY_DEPTH and directory != root / "worktrees":
            scan = child_directories(directory, MAX_DISCOVERY_ENTRIES - inspected_entries)
            inspected_entries += scan.inspected
            pending.extend((child, depth + 1) for child in scan.children)


def inventory(root: Path) -> list[Worktree]:
    inspection_check()
    if not root.exists():
        return []
    found: dict[Path, Worktree] = {}
    # Git subprocess latency is not directory-discovery time.
    for repository in list(repositories(root)):
        inspection_check()
        # Native ordering places the main worktree first; it is never a candidate.
        for entry in parse_worktrees(git(repository, "worktree", "list", "--porcelain", "-z"), repository)[1:]:
            if entry.path.is_relative_to(root):
                found[entry.path] = entry
            if len(found) > MAX_WORKTREES:
                message = "Worktree inventory exceeded its bounded limit; nothing will be removed"
                raise InventoryLimitError(message, kind="worktrees")
    return sorted(found.values(), key=lambda entry: str(entry.path))


def repository_candidates(repository: Path, root: Path, cursor: InventoryCursor | None) -> list[Worktree]:
    rows = parse_worktrees(git(repository, "worktree", "list", "--porcelain", "-z"), repository)[1:]
    after = None if cursor is None or cursor.repository != repository else cursor.worktree
    found: list[Worktree] = []
    for entry in rows:
        if not entry.path.is_relative_to(root) or entry.path == root or ".." in entry.path.parts:
            continue
        if after is not None and str(entry.path) <= str(after):
            continue
        found.append(entry)
    return sorted(found, key=lambda entry: str(entry.path))


def candidate_page(paths: RuntimePaths) -> InventoryPage:
    root = paths.code
    if not root.exists():
        return InventoryPage([], None, pending=False)
    cursor = inventory_cursor(state_directory(paths), root)
    deadline = time.monotonic() + INVENTORY_PAGE_SECONDS
    discovered = sorted(repositories(root), key=str)
    entries: dict[Path, Worktree] = {}
    errors = 0 if cursor is None else cursor.errors
    progress = cursor
    for repository in discovered:
        if cursor is not None and (
            str(repository) < str(cursor.repository) or (repository == cursor.repository and cursor.worktree is None)
        ):
            continue
        if time.monotonic() >= deadline:
            return InventoryPage(list(entries.values()), progress, pending=True, errors=errors)
        try:
            rows = repository_candidates(repository, root, cursor)
        except OSError, RuntimeError, ValueError:
            # One broken repository must not starve unrelated repositories.
            # It is retried on the next complete rotation and remains visible.
            errors = min(MAX_DIRECTORIES, errors + 1)
            progress = InventoryCursor(repository=repository, errors=errors)
            continue
        for entry in rows:
            if len(entries) >= INVENTORY_PAGE_SIZE:
                return InventoryPage(list(entries.values()), progress, pending=True, errors=errors)
            entries[entry.path] = entry
            progress = InventoryCursor(repository=repository, worktree=entry.path, errors=errors)
        progress = InventoryCursor(repository=repository, errors=errors)
    return InventoryPage(list(entries.values()), None, pending=False, errors=errors)


def bounded_bytes(path: Path, *, budget: cleanup_activity.InventoryBudget | None = None) -> bytes:
    inspection_check()
    if budget is not None:
        budget.check()
    with path.open("rb") as stream:
        value = stream.read(MAX_PROCESS_BYTES + 1)
    if budget is not None:
        budget.charge_bytes(len(value))
    if len(value) > MAX_PROCESS_BYTES:
        message = "Process reference inventory exceeded its bounded limit"
        raise RuntimeError(message)
    return value


def process_identity(process: Path, budget: cleanup_activity.InventoryBudget) -> ProcessIdentity:
    metadata = process.stat()
    contents = bounded_bytes(process / "stat", budget=budget)
    end = contents.rfind(b")")
    columns = contents[end + 2 :].split()
    if (
        end < 0
        or len(columns) <= PROCESS_START_FIELD
        or columns[0] not in PROCESS_STATES
        or len(columns[PROCESS_START_FIELD]) > PROCESS_START_DIGITS
        or not columns[PROCESS_START_FIELD].isdigit()
    ):
        message = "Process identity is incomplete or unknown; nothing will be removed"
        raise RuntimeError(message)
    if columns[0] == b"Z":
        message = "A zombie process leader requires whole-process exit inspection; nothing will be removed"
        raise RuntimeError(message)
    return ProcessIdentity(metadata.st_dev, metadata.st_ino, metadata.st_uid, int(columns[PROCESS_START_FIELD]))


def process_snapshot(process: Path, inspection: cleanup_activity.InventoryBudget) -> References:
    paths: set[Path] = set()
    arguments: list[str] = []
    inspected_fds = 0
    cwd = (process / "cwd").resolve(strict=True)
    inspection.charge_bytes(len(os.fsencode(cwd)))
    paths.add(cwd)
    words = os.fsdecode(bounded_bytes(process / "cmdline", budget=inspection)).split("\0")
    arguments.extend(words)
    package_writer = any(
        Path(word.partition(" ")[0]).name in {"uv", "uvx", "pip", "pip3", "pnpm", "npm", "npx", "yarn", "bun"}
        or "pnpm.cjs" in word
        or "npm-cli.js" in word
        for word in words
    )
    with os.scandir(process / "fd") as descriptors:
        for descriptor in descriptors:
            inspection_check()
            inspection.check()
            inspected_fds += 1
            if inspected_fds > MAX_FDS:
                message = "Open-file inventory exceeded its bounded limit"
                raise RuntimeError(message)
            try:
                target = str(Path(descriptor.path).readlink())
            except FileNotFoundError:
                continue
            inspection.charge_bytes(len(os.fsencode(target)))
            if target.startswith("/"):
                paths.add(Path(target.removesuffix(" (deleted)")).resolve())
    for line in os.fsdecode(bounded_bytes(process / "maps", budget=inspection)).splitlines():
        inspection_check()
        inspection.check()
        fields = line.split(maxsplit=MAP_FIELDS - 1)
        if len(fields) == MAP_FIELDS and fields[-1].startswith("/"):
            if "\\012" in fields[-1]:
                message = "Mapped-file path is ambiguous; nothing will be removed"
                raise RuntimeError(message)
            paths.add(Path(fields[-1].removesuffix(" (deleted)")).resolve())
    return References(frozenset(paths), tuple(arguments), package_writer, inspected_fds)


def process_reference(process: Path, *, budget: cleanup_activity.InventoryBudget | None = None) -> References:
    inspection = budget or cleanup_activity.InventoryBudget(time.monotonic() + cleanup_activity.COLLECTION_SECONDS)
    inspection.check()
    # Hold the original proc-directory inode until the ending identity check, including across PID reuse.
    descriptor = os.open(process, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid():
            return References(frozenset())
        before = process_identity(process, inspection)
        message = "Process identity changed during inspection; nothing will be removed"
        if (before.device, before.inode, before.uid) != (metadata.st_dev, metadata.st_ino, metadata.st_uid):
            raise RuntimeError(message)
        result = process_snapshot(process, inspection)
        if process_identity(process, inspection) != before:
            raise RuntimeError(message)
        inspection.check()
        return result
    finally:
        os.close(descriptor)


def process_references(
    proc: Path = Path("/proc"), *, budget: cleanup_activity.InventoryBudget | None = None
) -> References:
    inspection = budget or cleanup_activity.InventoryBudget(time.monotonic() + cleanup_activity.COLLECTION_SECONDS)
    inspection.check()
    paths: set[Path] = set()
    arguments: list[str] = []
    package_writer = False
    inspected_fds = 0
    for process in proc.iterdir():
        inspection_check()
        inspection.entry()
        if not process.name.isdigit():
            continue
        try:
            found = process_reference(process, budget=inspection)
        except FileNotFoundError:
            if not cleanup_activity.present(process):
                continue
            message = "A live process has unreadable references; nothing will be removed"
            raise RuntimeError(message) from None
        paths.update(found.paths)
        arguments.extend(found.arguments)
        package_writer |= found.package_writer
        inspected_fds += found.fd_count
        if inspected_fds > MAX_FDS:
            message = "Open-file inventory exceeded its bounded limit"
            raise RuntimeError(message)
    return References(frozenset(paths), tuple(arguments), package_writer, inspected_fds)


def inspected_processes(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> References:
    inspection_check()
    try:
        processes = process_references()
    except OSError, RuntimeError:
        # The optional administrator-owned probe returns candidate matches only,
        # never raw process arguments, credentials, or authority to delete files.
        requested = [entry.path for entry in inventory(paths.code)] if candidates is None else candidates
        inspection_check()
        result = cleanup_inspection.inspect(requested)
        inspection_check()
        return References(
            frozenset(Path(path) for path in result.protected),
            ("zellij",) if result.zellij_running else (),
            result.package_writer,
        )
    inspection_check()
    return processes


def live_sessions(output: str) -> Iterator[str]:
    for line in map(str.rstrip, output.splitlines()):
        if line.endswith("(EXITED - attach to resurrect)"):
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+) \[Created .+ ago\](?: \(current\))?", line)
        if match is None:
            message = "Zellij session inventory is ambiguous; nothing will be removed"
            raise RuntimeError(message)
        yield match[1]


def builtin_pane(pane: Pane) -> bool:
    return pane.is_plugin and (
        (not pane.is_selectable and pane.plugin_url in {"zellij:status-bar", "zellij:tab-bar", "zellij:compact-bar"})
        or (pane.is_suppressed and pane.plugin_url == "zellij:link")
    )


def pane_references(output: str) -> References:
    protected_paths: set[Path] = set()
    arguments: list[str] = []
    for pane in PaneInventory.model_validate_json(output).root:
        if builtin_pane(pane):
            continue
        if pane.pane_cwd is None or not Path(pane.pane_cwd).is_absolute():
            message = "Zellij pane has no known working directory; nothing will be removed"
            raise RuntimeError(message)
        protected_paths.add(Path(pane.pane_cwd).resolve(strict=True))
        arguments.extend(value for value in (pane.pane_command, pane.terminal_command) if value)
    return References(frozenset(protected_paths), tuple(arguments))


def zellij_references(paths: RuntimePaths, *, running: bool) -> References:
    executable = shutil.which("zellij", path=user_environment(paths)["PATH"])
    if executable is None or Path(executable).resolve().name == "mise":
        if running:
            message = "A running Zellij server cannot be inspected; nothing will be removed"
            raise RuntimeError(message)
        return References(frozenset())
    result = inspection_run([executable, "list-sessions", "--no-formatting"], check=False, timeout=10)
    if result.returncode:
        # Zellij distinguishes an empty session list by this exact native message.
        if "No active zellij sessions found" in result.stderr + result.stdout:
            return References(frozenset())
        message = "Zellij reference inventory unavailable; nothing will be removed"
        raise RuntimeError(message)
    protected_paths: set[Path] = set()
    arguments: list[str] = []
    for session in live_sessions(result.stdout):
        output = inspection_run([executable, "--session", session, "action", "list-panes", "--json"], timeout=15).stdout
        found = pane_references(output)
        protected_paths.update(found.paths)
        arguments.extend(found.arguments)
    return References(frozenset(protected_paths), tuple(arguments))


def container_daemons(proc: Path) -> set[str]:
    daemons: set[str] = set()
    for process in proc.iterdir():
        inspection_check()
        if process.name.isdigit():
            try:
                daemons.add((process / "comm").read_text().strip())
            except FileNotFoundError:
                continue
    return daemons


def parse_mounts(output: str, expected_rows: int) -> frozenset[Path]:
    lines = output.splitlines()
    if len(lines) != expected_rows:
        message = "Docker mount inventory is incomplete; nothing will be removed"
        raise RuntimeError(message)
    mounted: set[Path] = set()
    for line in lines:
        for mount in MountInventory.model_validate_json(line).root:
            if mount.kind != "bind":
                continue
            if not Path(mount.source).is_absolute():
                message = "Docker mount has an unknown source; nothing will be removed"
                raise RuntimeError(message)
            mounted.add(Path(mount.source).resolve())
    return frozenset(mounted)


def docker_mounts(executable: str, socket: Path) -> frozenset[Path]:
    command = [executable, "--host", "unix://" + str(socket)]
    identifiers = inspection_run([*command, "ps", "-q"], timeout=10).stdout.splitlines()
    if len(identifiers) > MAX_WORKTREES or any(
        re.fullmatch(r"[a-f0-9]{12,64}", identifier) is None for identifier in identifiers
    ):
        message = "Docker inventory is ambiguous; nothing will be removed"
        raise RuntimeError(message)
    if not identifiers:
        return frozenset()
    output = inspection_run([*command, "inspect", "--format", "{{json .Mounts}}", *identifiers], timeout=10).stdout
    return parse_mounts(output, len(identifiers))


def docker_references(paths: RuntimePaths, proc: Path = Path("/proc")) -> frozenset[Path]:
    daemons = container_daemons(proc)
    if "conmon" in daemons or ("containerd" in daemons and "dockerd" not in daemons):
        message = "A container runtime cannot be inspected; nothing will be removed"
        raise RuntimeError(message)
    sockets = [
        path
        for path in (Path("/var/run/docker.sock"), Path("/run/user") / str(os.getuid()) / "docker.sock")
        if path.exists()
    ]
    executable = shutil.which("docker", path=user_environment(paths)["PATH"])
    if not sockets:
        if "dockerd" in daemons:
            message = "Local Docker socket is unknown; nothing will be removed"
            raise RuntimeError(message)
        return frozenset()
    if executable is None:
        message = "Local Docker cannot be inspected; nothing will be removed"
        raise RuntimeError(message)
    mounted: set[Path] = set()
    for socket in sockets:
        mounted.update(docker_mounts(executable, socket))
    return frozenset(mounted)


def nongit_container(directory: Path) -> bool:
    for component in (directory, *directory.parents):
        metadata = component.lstat()
        if not stat.S_ISDIR(metadata.st_mode):
            return False
        for name in (".git", "HEAD", "objects", "refs"):
            marker = component / name
            try:
                marker.lstat()
            except FileNotFoundError:
                continue
            if name == ".git" and empty_git_marker(marker):
                continue
            return False
    return True


def workspace_containers(paths: RuntimePaths) -> frozenset[Path]:
    containers: set[Path] = set()
    for directory in (Path("/"), paths.home, paths.code, paths.code / "worktrees"):
        try:
            directory.lstat()
        except FileNotFoundError:
            continue
        if nongit_container(directory):
            containers.add(directory)
    return frozenset(containers)


def native_references(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> References:
    with reference_stage("processes"):
        processes = inspected_processes(paths, candidates)
    inspection_check()
    with reference_stage("terminals"):
        panes = zellij_references(paths, running=any(Path(word).name == "zellij" for word in processes.arguments))
    inspection_check()
    with reference_stage("boot-policy"):
        aliases = frozenset(Path(value).resolve() for value in boot_aliases(home=paths.home).values())
    inspection_check()
    with reference_stage("containers"):
        mounted = docker_references(paths)
    inspection_check()
    return References(
        processes.paths | panes.paths | aliases,
        (*processes.arguments, *panes.arguments),
        processes.package_writer,
        mounted=mounted,
    )


def references(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> References:
    inspection_check()
    codex_home = environment_snapshot().get("CODEX_HOME")
    native = native_references(paths, candidates)
    inspection_check()
    with reference_stage("workspaces"):
        activity = cleanup_activity.collect(paths.home, codex_home=Path(codex_home) if codex_home else None)
    inspection_check()
    with reference_stage("workspace-containers"):
        containers = workspace_containers(paths)
    return References(
        native.paths,
        native.arguments,
        native.package_writer,
        mounted=native.mounted,
        workspaces=activity.paths,
        workspace_containers=containers,
        storage=activity.storage | native.storage,
    )


def reference_snapshot(paths: RuntimePaths, candidates: Sequence[Path] | None) -> References:
    deadline = INSPECTION_DEADLINE.get()
    if deadline is None:
        deadline = time.monotonic() + FINAL_PROOF_SECONDS
    with inspection_window(deadline):
        for attempt in range(REFERENCE_ATTEMPTS):
            inspection_check()
            try:
                result = references(paths) if candidates is None else references(paths, candidates)
                inspection_check()
            except ReferenceInspectionError as error:
                if (
                    type(error.kind) is not str
                    or error.kind not in TRANSIENT_REFERENCE_KINDS
                    or attempt + 1 == REFERENCE_ATTEMPTS
                ):
                    raise
                inspection_check()
                # Discard every part of the failed census. Only a complete fresh
                # snapshot may proceed, under the original caller's deadline.
                time.sleep(min(REFERENCE_RETRY_SECONDS, max(0.0, deadline - time.monotonic())))
            else:
                return result
        message = "Reference inspection attempts were exhausted"
        raise RuntimeError(message)


def live_references(paths: RuntimePaths, candidates: Sequence[Path] | None = None) -> References:
    try:
        return reference_snapshot(paths, candidates)
    except cleanup_inspection.ProbeUnavailableError as error:
        reason = error.reason
        if type(reason) is not str or reason not in cleanup_inspection.PROBE_FAILURE_REASONS:
            reason = "unavailable"
        detail = "" if reason == "unavailable" else f" ({reason})"
        message = f"live reference inspection failed: read-only process probe unavailable{detail}; retained"
        raise LiveInspectionFailedError(message) from error
    except ReferenceInspectionError as error:
        detail = (
            f": {error.stage} ({error.kind})"
            if type(error.stage) is str
            and type(error.kind) is str
            and error.stage in REFERENCE_STAGES
            and error.kind in REFERENCE_KINDS
            else ""
        )
        message = f"live reference inspection failed{detail}; retained"
        raise LiveInspectionFailedError(message) from error
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        message = "live reference inspection failed; retained"
        raise LiveInspectionFailedError(message) from error


@contextmanager
def retention_window() -> Generator[None]:
    token = RETENTION_HINTS.set(RetentionHints())
    try:
        yield
    finally:
        RETENTION_HINTS.reset(token)


def retained_reference(path: Path) -> bool:
    hints = RETENTION_HINTS.get()
    return hints is not None and hints.snapshot is not None and hints.snapshot.contains(path)


def fresh_references(paths: RuntimePaths, candidate: Path) -> References:
    # A previous census can only retain other checkouts, never authorize deletion.
    protected = live_references(paths, (candidate,))
    hints = RETENTION_HINTS.get()
    if hints is not None:
        hints.snapshot = protected
    return protected


def owned_path(path: Path, root: Path, *, require_private: bool = False) -> bool:
    if not path.is_relative_to(root):
        return False
    group_writable = require_private
    reached_root = False
    for component in (path, *path.parents):
        metadata = component.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o002:
            return False
        if metadata.st_mode & 0o020:
            if metadata.st_gid != os.getgid():
                return False
            group_writable = True
        reached_root |= component == root
        if reached_root and (not group_writable or not metadata.st_mode & 0o077):
            # A private owner-only ancestor prevents other users reaching group-writable children.
            return True
    return False


def generated_git_marker(directory: Path, name: str, *, markers: set[str]) -> bool:
    if name in {".git", "HEAD"}:
        return True
    if re.fullmatch(r"pack-[0-9a-f]{40}(?:[0-9a-f]{24})?\.(?:pack|idx|rev|promisor)", name):
        return True
    if (
        re.fullmatch(r"[0-9a-f]{2}", directory.name) is not None
        and re.fullmatch(r"[0-9a-f]{38}(?:[0-9a-f]{24})?", name) is not None
    ):
        return True
    if name in {"objects", "refs", "info", "pack"}:
        markers.add(name)
    return {"objects", "refs"} <= markers or {"info", "pack"} <= markers


def private_environment_file(name: str) -> bool:
    return name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"})


def generated_scan_check(deadline: float, *, entries: int = 0, pending: int = 0) -> None:
    inspection_check()
    if time.monotonic() > deadline or entries > GENERATED_ENTRIES or pending > GENERATED_DIRECTORIES:
        message = "Generated files could not be proven free of nested source repositories"
        raise RuntimeError(message)


def generated_source(path: Path) -> bool:
    pending = [path]
    inspected = 0
    deadline = time.monotonic() + GENERATED_SECONDS
    while pending:
        generated_scan_check(deadline)
        directory = pending.pop()
        if directory.name == ".venv" and cleanup_activity.present(directory / "src"):
            return True
        markers: set[str] = set()
        with os.scandir(directory) as entries:
            for entry in entries:
                inspected += 1
                generated_scan_check(deadline, entries=inspected)
                if private_environment_file(entry.name):
                    return True
                if generated_git_marker(directory, entry.name, markers=markers):
                    return True
                if entry.is_dir(follow_symlinks=False):
                    generated_scan_check(deadline, pending=len(pending) + 1)
                    pending.append(Path(entry.path))
    generated_scan_check(deadline)
    return False


def recent_checkout(entry: Worktree, *, min_age_days: float, now: float) -> bool:
    return (
        max(
            entry.path.stat().st_mtime,
            entry.path.stat().st_ctime,
            (entry.path / ".git").stat().st_mtime,
            (entry.path / ".git").stat().st_ctime,
        )
        > now - min_age_days * DAY
    )


def initial_decision(entry: Worktree, root: Path, *, min_age_days: float, now: float) -> str | None:
    if entry.blocked or not (entry.branch.startswith("refs/heads/") if not entry.detached else not entry.branch):
        return "locked, prunable, or unknown worktree"
    if not owned_path(entry.path, root):
        return "path is missing, linked, foreign-owned, or writable by another user"
    if recent_checkout(entry, min_age_days=min_age_days, now=now):
        return "worktree checkout is too recent"
    return None


def worktree_age(entry: Worktree) -> float:
    try:
        age = entry.path.stat().st_mtime
    except OSError:
        age = sys.float_info.max
    return age if valid_time(age) else sys.float_info.max


def eligibility(
    entry: Worktree,
    root: Path,
    protected: References,
    *,
    min_age_days: float,
    now: float,
    merge_views: dict[Path, MergeProof | str] | None = None,
) -> str:
    if entry.blocked or not (entry.branch.startswith("refs/heads/") if not entry.detached else not entry.branch):
        return "locked, prunable, or unknown worktree"
    if not owned_path(entry.path, root):
        return "path is missing, linked, foreign-owned, or writable by another user"
    if protected.contains(entry.path):
        return REFERENCED
    if recent_checkout(entry, min_age_days=min_age_days, now=now):
        return "worktree checkout is too recent"
    merged = "eligible"
    if merge_views is not None:
        merged = merged_status(entry, now=now, proof=repository_proof(entry, merge_views, now=now))
        if merged != "eligible":
            return merged
    state = checkout_state(entry, min_age_days=min_age_days, now=now)
    if state != "eligible":
        return state
    if merge_views is None:
        merged = merged_status(entry, now=now)
    if merged != "eligible":
        return merged
    files = local_files(entry.path)
    if files != "eligible":
        return files
    # Observe mutable state again after status, keeping the same conservative age cutoff.
    reason = initial_decision(entry, root, min_age_days=min_age_days, now=now)
    if reason is not None:
        return reason
    return checkout_state(entry, min_age_days=min_age_days, now=now)


def object_directory_identity(metadata: os.stat_result) -> ObjectDirectoryIdentity:
    return ObjectDirectoryIdentity(metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns, metadata.st_ctime_ns)


def object_subdirectory(directory: int, child: os.DirEntry[str], budget: ObjectStorageBudget, *, depth: int) -> str:
    metadata = child.stat(follow_symlinks=False)
    descriptor = os.open(child.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        if object_directory_identity(os.fstat(descriptor)) != object_directory_identity(metadata):
            return "Git object storage changed during inspection"
        reason = object_entries(descriptor, budget, depth=depth + 1)
        current = os.stat(child.name, dir_fd=directory, follow_symlinks=False)
        if object_directory_identity(current) != object_directory_identity(metadata):
            return "Git object storage changed during inspection"
        return reason
    finally:
        os.close(descriptor)


def object_entries(directory: int, budget: ObjectStorageBudget, *, depth: int = 0) -> str:
    inspection_check()
    if depth > MAX_OBJECT_DEPTH or time.monotonic() > budget.deadline:
        return "Git object storage could not be inspected within its limits"
    before = os.fstat(directory)
    with os.scandir(directory) as entries:
        for child in entries:
            inspection_check()
            budget.inspected += 1
            if budget.inspected > MAX_FDS or time.monotonic() > budget.deadline:
                return "Git object storage could not be inspected within its limits"
            if child.is_symlink():
                return "Git object storage contains an unverified alias"
            if child.is_dir(follow_symlinks=False):
                reason = object_subdirectory(directory, child, budget, depth=depth)
                if reason != "eligible":
                    return reason
            elif not child.is_file(follow_symlinks=False):
                return "Git object storage contains an unverified special file"
    if object_directory_identity(os.fstat(directory)) != object_directory_identity(before):
        return "Git object storage changed during inspection"
    return "eligible"


def object_storage(common: Path) -> str:
    directory = common / "objects"
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return "Git object storage is missing or unknown"
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or not owned_path(directory, common)
        or directory.resolve(strict=True) != directory
    ):
        return "Git object storage is aliased, foreign-owned, or writable by another user"
    if any(cleanup_activity.present(directory / "info" / name) for name in ("alternates", "http-alternates")):
        return "Git alternate object storage could depend on another checkout"
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if object_directory_identity(os.fstat(descriptor)) != object_directory_identity(metadata):
            return "Git object storage changed during inspection"
        reason = object_entries(descriptor, ObjectStorageBudget(time.monotonic() + 2))
        if (
            object_directory_identity(directory.lstat()) != object_directory_identity(metadata)
            or directory.resolve(strict=True) != directory
        ):
            return "Git object storage changed during inspection"
        return reason
    finally:
        os.close(descriptor)


def preserved_detached(entry: Worktree) -> bool:
    result = git_process(
        entry.repository,
        "for-each-ref",
        f"--contains={entry.head}",
        "--count=1",
        "--format=%(refname)%00%(objectname)",
        "refs/heads/",
    )
    records = result.stdout.splitlines()
    if result.returncode or result.stderr or len(records) != 1:
        return False
    reference, separator, head = records[0].partition("\0")
    if (
        not separator
        or not reference.startswith("refs/heads/")
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is None
    ):
        return False
    valid = git_process(entry.repository, "check-ref-format", reference, check=False)
    return valid.returncode == 0 and not valid.stdout and not valid.stderr


def checkout_head(entry: Worktree) -> str:
    if git(entry.path, "rev-parse", "HEAD").strip() != entry.head:
        return "HEAD or branch changed since inventory"
    if entry.detached:
        symbolic = git_process(entry.path, "symbolic-ref", "--quiet", "HEAD", check=False)
        if symbolic.returncode != 1 or symbolic.stdout or symbolic.stderr:
            return "HEAD or branch changed since inventory"
        if not preserved_detached(entry):
            return "detached HEAD has no verified preserving local branch"
    elif git(entry.path, "symbolic-ref", "HEAD").strip() != entry.branch:
        return "HEAD or branch changed since inventory"
    return "eligible"


def checkout_activity(entry: Worktree, *, min_age_days: float, now: float, git_directory: Path | None = None) -> str:
    # This shortcut can only retain; its eligible result is never deletion evidence.
    directory = (
        Path(git(entry.path, "rev-parse", "--absolute-git-dir").strip()) if git_directory is None else git_directory
    )
    if any(cleanup_activity.present(directory / marker) for marker in OPERATIONS):
        return "Git operation or index update is in progress"
    activity = [entry.path, directory, *(directory / name for name in ("HEAD", "index", "logs/HEAD"))]
    if any(
        max(path.stat().st_mtime, path.stat().st_ctime) > now - min_age_days * DAY
        for path in activity
        if cleanup_activity.present(path)
    ):
        return "worktree checkout or Git metadata is too recent"
    return "eligible"


def checkout_state(entry: Worktree, *, min_age_days: float, now: float) -> str:
    git_directory = Path(git(entry.path, "rev-parse", "--absolute-git-dir").strip())
    common = Path(git(entry.repository, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    if cleanup_activity.present(common / "info/grafts"):
        return "legacy Git grafts could alter ancestry proofs"
    if (
        git_directory.parent != common / "worktrees"
        or Path((git_directory / "gitdir").read_text().strip()) != entry.path / ".git"
    ):
        return "Git registration does not match the checkout"
    if Path(git(entry.path, "rev-parse", "--show-toplevel").strip()) != entry.path:
        return "Git working-directory override does not match the checkout"
    activity = checkout_activity(entry, min_age_days=min_age_days, now=now, git_directory=git_directory)
    if activity != "eligible":
        return activity
    storage = object_storage(common)
    if storage != "eligible":
        return storage
    head = checkout_head(entry)
    if head != "eligible":
        return head
    for record in git(entry.path, "ls-files", "-v", "-z").split("\0"):
        if record and (record[0].islower() or record[0] == "S"):
            return "index flags could hide local changes"
    return "eligible"


def generated_install_state(root: Path, relative: Path) -> bool:
    if relative.parts[-2:] != (".yarn", "install-state.gz"):
        return False
    target = root / relative
    metadata = target.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o002
        or not len(YARN_STATE_HEADER) <= metadata.st_size <= YARN_STATE_BYTES
    ):
        return False
    group_writable = bool(metadata.st_mode & 0o020)
    if group_writable and metadata.st_gid != os.getgid():
        return False
    if not owned_path(target.parent, root, require_private=group_writable):
        return False
    descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        header = stream.read(len(YARN_STATE_HEADER))
        after = os.fstat(stream.fileno())
        current = target.lstat()
    versions = {
        (
            item.st_dev,
            item.st_ino,
            item.st_uid,
            item.st_mode,
            item.st_nlink,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        for item in (metadata, opened, after, current)
    }
    return header == YARN_STATE_HEADER and len(versions) == 1


def local_files(path: Path) -> str:
    generated_paths: set[Path] = set()
    for record in git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored=matching").split(
        "\0"
    ):
        inspection_check()
        if not record:
            continue
        relative = Path(record[3:])
        if not record.startswith("!! ") or relative.is_absolute() or ".." in relative.parts:
            return "tracked, untracked, or non-disposable ignored local files"
        if generated_install_state(path, relative):
            continue
        generated_parts = next((index + 1 for index, part in enumerate(relative.parts) if part in GENERATED), None)
        if generated_parts is None:
            return "tracked, untracked, or non-disposable ignored local files"
        generated_paths.add(path.joinpath(*relative.parts[:generated_parts]))
    for generated in sorted(generated_paths):
        if not owned_path(generated, path) or not generated.is_dir() or generated_source(generated):
            return "ignored files may contain nested source repositories"
    return "eligible"


def verified_default(entry: Worktree, *, now: float) -> MergeProof | str:
    fetch = Path(git(entry.repository, "rev-parse", "--git-path", "FETCH_HEAD").strip())
    if not fetch.is_absolute():
        fetch = entry.repository / fetch
    if not cleanup_activity.present(fetch) or not 0 <= now - fetch.stat().st_mtime <= DAY:
        return STALE_REMOTE
    upstream = git(entry.repository, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
    if not upstream.startswith("refs/remotes/origin/"):
        return "origin default branch is unknown"
    remote_head = git(entry.repository, "rev-parse", upstream).strip()
    remote_url = git(entry.repository, "remote", "get-url", "origin").strip()
    branch = upstream.removeprefix("refs/remotes/origin/")
    records = [line.split("\t", 2) for line in fetch.read_text().splitlines()]
    if not any(
        len(record) == FETCH_FIELDS
        and record[0] == remote_head
        and record[2].startswith(f"branch '{branch}' of ")
        and normalized_remote(record[2].split(" of ", 1)[1]) == normalized_remote(remote_url)
        for record in records
    ):
        return MISSING_DEFAULT_FETCH
    return MergeProof(remote_head, remote_url, branch)


def repository_proof(entry: Worktree, views: dict[Path, MergeProof | str], *, now: float) -> MergeProof | str:
    if entry.repository not in views:
        try:
            views[entry.repository] = verified_default(entry, now=now)
        except OSError, RuntimeError, ValueError:
            views[entry.repository] = "origin default-branch proof inspection failed; retained"
    return views[entry.repository]


def merged_status(entry: Worktree, *, now: float, proof: MergeProof | str | None = None) -> str:
    evidence = verified_default(entry, now=now) if proof is None else proof
    if isinstance(evidence, str):
        return evidence
    try:
        git(entry.repository, "merge-base", "--is-ancestor", entry.head, evidence.head)
    except CommandError as error:
        if error.returncode != 1:
            return "Git ancestry inspection failed; retained"
        return published_status(entry, evidence)
    return "eligible"


def published_status(entry: Worktree, evidence: MergeProof) -> str:
    retained = "commits are not proven merged into origin's default branch"
    if entry.detached:
        return retained
    if cleanup_publication.github_repository(evidence.origin) is None or not evidence.branch:
        return retained
    remaining = PUBLICATION_LOOKUPS.get()
    if remaining is not None:
        if remaining <= 0:
            # Leave the first unqueried candidate pending instead of repeatedly skipping it.
            raise InspectionDeferredError(DEFERRED)
        PUBLICATION_LOOKUPS.set(remaining - 1)
    proof = cleanup_publication.prove_github_publication(
        entry.repository,
        origin=evidence.origin,
        head=entry.head,
        branch=entry.branch,
        default_head=evidence.head,
        default_branch=evidence.branch,
        inspect=inspection_run,
        native_git=git_process,
    )
    return "eligible" if proof is not None else retained


@contextmanager
def publication_window(allowance: int = MAX_PUBLICATION_LOOKUPS) -> Generator[None]:
    token = PUBLICATION_LOOKUPS.set(allowance)
    try:
        yield
    finally:
        PUBLICATION_LOOKUPS.reset(token)


def normalized_remote(value: str) -> str:
    return value.split("://", 1)[-1].rsplit("@", 1)[-1].replace(":", "/", 1).removesuffix(".git").rstrip("/")


def capacity_recovery_needed(home: Path) -> bool:
    current = cleanup_capacity.policy(home)
    return current.volume.used >= current.thresholds.stop


def refresh_default(entry: Worktree) -> bool:
    upstream = git(entry.repository, "symbolic-ref", "refs/remotes/origin/HEAD").strip()
    if not upstream.startswith("refs/remotes/origin/"):
        return False
    branch = upstream.removeprefix("refs/remotes/origin/")
    git(
        entry.repository,
        "fetch",
        "--no-tags",
        "--no-prune",
        "--no-prune-tags",
        "--no-auto-maintenance",
        "--no-write-commit-graph",
        "--no-recurse-submodules",
        "origin",
        f"refs/heads/{branch}:{upstream}",
    )
    return merged_status(entry, now=time.time()) == "eligible"


def registered_candidate(entry: Worktree, root: Path) -> Worktree | None:
    # Reopen only the known repository, while preserving discovery's owned-main boundary.
    if not owned_path(entry.repository, root):
        return None
    marker = entry.repository / ".git"
    metadata = marker.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o002
        or empty_git_marker(marker)
    ):
        return None
    before = entry.repository.stat(), metadata
    entries = parse_worktrees(git(entry.repository, "worktree", "list", "--porcelain", "-z"), entry.repository)
    try:
        after = entry.repository.stat(), marker.lstat()
    except FileNotFoundError:
        return None
    if any(
        (left.st_dev, left.st_ino) != (right.st_dev, right.st_ino) for left, right in zip(before, after, strict=True)
    ):
        return None
    if not entries or entries[0].path != entry.repository or entries[0].blocked:
        return None
    matches = [item for item in entries[1:] if item.path == entry.path]
    return matches[0] if len(matches) == 1 else None


def visible_changes(path: Path) -> bool:
    # A negative-only shortcut: empty output never replaces the complete final proof.
    return bool(git(path, "status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored=no"))


def remove_reviewed(entry: Worktree, paths: RuntimePaths, *, min_age_days: float, recovery: bool = False) -> Decision:
    identity = entry.path.stat().st_dev, entry.path.stat().st_ino
    fresh = registered_candidate(entry, paths.code)
    if fresh != entry or identity != (entry.path.stat().st_dev, entry.path.stat().st_ino):
        return Decision(str(entry.path), "worktree identity changed during review")
    reason = eligibility(
        entry, paths.code, fresh_references(paths, entry.path), min_age_days=min_age_days, now=time.time()
    )
    if reason != "eligible":
        return Decision(str(entry.path), reason)
    if not maintenance_idle():
        return Decision(str(entry.path), "memory or disk pressure is high or unknown; retained")
    if recovery and not capacity_recovery_needed(paths.home):
        return Decision(str(entry.path), "capacity recovery reached its free-space target; retained")
    limit = CAPACITY_ATTEMPTS if recovery else MAX_ATTEMPTS
    inspection_check()
    if not reserve_attempt(state_directory(paths), AttemptStamp.current(), entry.path, entry.head, limit=limit):
        return Decision(str(entry.path), "rolling 24-hour native-removal budget exhausted; retained")
    try:
        inspection_check()
    except InspectionDeferredError:
        return Decision(
            str(entry.path), "native removal deferred after durable charge; branch retained", attempted=True
        )
    try:
        # Once charged, native removal keeps its existing independent 120-second bound.
        with inspection_window(None):
            git(entry.repository, "worktree", "remove", "--", str(entry.path), timeout=120)
    except OSError, RuntimeError:
        return Decision(
            str(entry.path), "native removal did not complete; inspect checkout; branch retained", attempted=True
        )
    return Decision(str(entry.path), "removed clean merged worktree; branch retained", removed=True, attempted=True)


def can_refresh(repository: Path, refreshed: set[Path]) -> bool:
    return repository not in refreshed and len(refreshed) < MAX_FETCH_REPOSITORIES and maintenance_idle()


def refresh_review(
    entry: Worktree,
    paths: RuntimePaths,
    *,
    min_age_days: float,
    refreshed: set[Path],
    merge_views: dict[Path, MergeProof | str] | None,
    final_deadline: float | None,
    timing: ReviewTiming | None = None,
) -> str:
    with final_inspection_window(final_deadline, timing):
        # Both full guards still precede writes; invalid checkout state can retain without a census.
        reason = checkout_state(entry, min_age_days=min_age_days, now=time.time())
        if reason != "eligible":
            return reason
        if fresh_references(paths, entry.path).contains(entry.path):
            return REFERENCED
        refreshed.add(entry.repository)
        try:
            refresh_default(entry)
        except OSError, RuntimeError:
            if merge_views is not None:
                merge_views.pop(entry.repository, None)
            return "origin default-branch refresh unavailable; retained"
        if merge_views is not None:
            merge_views.pop(entry.repository, None)
        proof = repository_proof(entry, merge_views, now=time.time()) if merge_views is not None else None
        return merged_status(entry, now=time.time(), proof=proof)


def preliminary_status(
    entry: Worktree,
    paths: RuntimePaths,
    *,
    min_age_days: float,
    merge_views: dict[Path, MergeProof | str] | None,
) -> str:
    # This preflight only retains; remove_reviewed always rebuilds the full fresh proof.
    reason = initial_decision(entry, paths.code, min_age_days=min_age_days, now=time.time())
    if reason is not None:
        return reason
    if retained_reference(entry.path):
        return REFERENCED
    activity = checkout_activity(entry, min_age_days=min_age_days, now=time.time())
    if activity != "eligible":
        return activity
    if visible_changes(entry.path):
        return "tracked, untracked, or non-disposable ignored local files"
    proof = repository_proof(entry, merge_views, now=time.time()) if merge_views is not None else None
    return merged_status(entry, now=time.time(), proof=proof)


def review(
    entry: Worktree,
    paths: RuntimePaths,
    protected: References | None,
    *,
    apply: bool,
    min_age_days: float,
    attempts: int,
    recovery: bool = False,
    refreshed: set[Path] | None = None,
    merge_views: dict[Path, MergeProof | str] | None = None,
    final_deadline: float | None = None,
    timing: ReviewTiming | None = None,
) -> Decision:
    if apply and attempts >= MAX_REMOVALS:
        return Decision(str(entry.path), "per-run native-removal limit reached; retained without further inspection")
    if apply:
        reason = preliminary_status(entry, paths, min_age_days=min_age_days, merge_views=merge_views)
    else:
        if protected is None:
            message = "A report requires verified live references"
            raise RuntimeError(message)
        reason = eligibility(
            entry, paths.code, protected, min_age_days=min_age_days, now=time.time(), merge_views=merge_views
        )
    if (
        apply
        and reason in {STALE_REMOTE, MISSING_DEFAULT_FETCH}
        and refreshed is not None
        and can_refresh(entry.repository, refreshed)
    ):
        reason = refresh_review(
            entry,
            paths,
            min_age_days=min_age_days,
            refreshed=refreshed,
            merge_views=merge_views,
            final_deadline=final_deadline,
            timing=timing,
        )
    if reason != "eligible" or not apply:
        return Decision(str(entry.path), reason)
    with final_inspection_window(final_deadline, timing):
        return remove_reviewed(entry, paths, min_age_days=min_age_days, recovery=recovery)


def apply_retention(
    entry: Worktree,
    paths: RuntimePaths,
    *,
    attempts: int,
    allowance: int,
    recovery: bool,
    recent: frozenset[tuple[Path, str]],
) -> str | None:
    if attempts >= allowance:
        reason = (
            "per-run native-removal limit reached"
            if allowance == MAX_REMOVALS
            else "rolling 24-hour native-removal budget exhausted"
        )
        return reason + "; retained without further inspection"
    if recovery and not capacity_recovery_needed(paths.home):
        return "capacity recovery reached its free-space target; retained"
    if (entry.path, entry.head) in recent:
        return "native removal already attempted for this checkout within 24 hours; retained"
    return None


def retained_inventory(entries: list[Worktree], reason: str) -> list[Decision]:
    return [Decision(str(entry.path), reason) for entry in entries]


def removal_budget(paths: RuntimePaths, limit: int) -> RemovalBudget:
    state = state_directory(paths)
    stamp = AttemptStamp.current()
    recent = frozenset((receipt.path, receipt.head) for receipt in active_receipts(state, stamp).receipts)
    return RemovalBudget(min(MAX_REMOVALS, remaining_attempts(state, stamp, limit=limit)), recent)


def ordered_reviews(
    entries: list[Worktree], cursor: ReviewCursor | None, *, inventory_order: bool = False
) -> list[tuple[ReviewCursor, Worktree]]:
    ordered = [(ReviewCursor(version=1, modified=worktree_age(entry), path=entry.path), entry) for entry in entries]
    if inventory_order:
        return ordered
    ordered.sort(key=lambda item: (item[0].modified, str(item[0].path)))
    if cursor is None:
        return ordered
    anchor = cursor.modified, str(cursor.path)
    start = next(
        (index for index, (key, _entry) in enumerate(ordered) if (key.modified, str(key.path)) > anchor), len(ordered)
    )
    return ordered[start:] + ordered[:start]


def checkpoint_legacy_review(paths: RuntimePaths, key: ReviewCursor, progress: ReviewProgress | None) -> None:
    # Paged reviews commit their inventory checkpoint after the worker finishes.
    # Legacy full-inventory callers retain their per-review durable age cursor.
    if progress is None:
        save_review_cursor(state_directory(paths), paths.code, key)


def review_inventory(
    entries: list[Worktree],
    paths: RuntimePaths,
    budget: RemovalBudget,
    *,
    apply: bool,
    recovery: bool,
    min_age_days: float,
    started: float,
    progress: ReviewProgress | None = None,
) -> list[Decision]:
    cursor = None
    if apply and progress is None:
        try:
            cursor = review_cursor(state_directory(paths), paths.code)
        except OSError, RuntimeError, ValueError:
            return retained_inventory(entries, "inspection cursor inspection failed; retained")
    ordered = ordered_reviews(entries, cursor, inventory_order=progress is not None)
    protected = None
    if not apply:
        try:
            protected = live_references(paths)
        except InspectionDeferredError:
            return retained_inventory(entries, DEFERRED)
        except LiveInspectionFailedError as error:
            return retained_inventory(entries, str(error))
    return review_page(
        ordered,
        paths,
        budget,
        protected,
        apply=apply,
        recovery=recovery,
        min_age_days=min_age_days,
        started=started,
        progress=progress,
    )


def review_page(
    ordered: list[tuple[ReviewCursor, Worktree]],
    paths: RuntimePaths,
    budget: RemovalBudget,
    protected: References | None,
    *,
    apply: bool,
    recovery: bool,
    min_age_days: float,
    started: float,
    progress: ReviewProgress | None = None,
) -> list[Decision]:
    decisions: list[Decision] = []
    attempts = 0
    refreshed: set[Path] = set()
    merge_views: dict[Path, MergeProof | str] = {}
    page_deadline = started + (REVIEW_SECONDS if apply else REPORT_SECONDS)
    final_deadline = started + FINAL_PROOF_SECONDS if apply else None
    timing = ReviewTiming()
    entries = [entry for _key, entry in ordered]

    def inspect_candidate(entry: Worktree, reason: str | None) -> Decision:
        deadline = page_deadline + timing.final_seconds
        if final_deadline is not None:
            deadline = min(deadline, final_deadline)
        with review_window(deadline, entry, progress):
            if reason is not None:
                return Decision(str(entry.path), reason)
            return review(
                entry,
                paths,
                protected,
                apply=apply,
                min_age_days=min_age_days,
                attempts=attempts,
                recovery=recovery,
                refreshed=refreshed,
                merge_views=merge_views,
                final_deadline=final_deadline,
                timing=timing,
            )

    for index, (key, entry) in enumerate(ordered):
        remaining = entries[index:]
        try:
            reason = (
                apply_retention(
                    entry, paths, attempts=attempts, allowance=budget.allowance, recovery=recovery, recent=budget.recent
                )
                if apply
                else None
            )
            if reason is not None and (attempts >= budget.allowance or reason.startswith("capacity recovery reached")):
                return decisions + retained_inventory(remaining, reason)
            result = inspect_candidate(entry, reason)
        except InspectionDeferredError:
            return decisions + retained_inventory(remaining, DEFERRED)
        except LiveInspectionFailedError as error:
            return decisions + retained_inventory(remaining, str(error))
        except OSError, RuntimeError, ValueError, sqlite3.Error:
            result = Decision(str(entry.path), "inspection failed; retained")
        attempts += result.attempted
        decisions.append(result)
        if not apply:
            continue
        try:
            checkpoint_legacy_review(paths, key, progress)
        except OSError, RuntimeError, ValueError:
            reason = "inspection failed: progress persistence failed; retained without further inspection"
            decisions[-1] = replace(result, reason=result.reason + "; inspection failed: progress persistence failed")
            return decisions + retained_inventory(remaining[1:], reason)
    return decisions


def reap(
    paths: RuntimePaths,
    *,
    apply: bool,
    min_age_days: int = 7,
    capacity_recovery: bool = False,
    batch: InventoryPage | None = None,
    progress: ReviewProgress | None = None,
) -> list[Decision]:
    if progress is not None and (batch is None or not apply):
        message = "Inventory progress requires an applied candidate page"
        raise ValueError(message)
    started = time.monotonic()
    with inspection_window(started + (FINAL_PROOF_SECONDS if apply else REPORT_SECONDS)):
        try:
            entries = inventory(paths.code) if batch is None else batch.entries
        except InspectionDeferredError as error:
            message = "Initial worktree inventory exceeded its inspection time budget; nothing will be removed"
            raise InventoryLimitError(message, kind="deadline") from error
        recovery = capacity_recovery and capacity_recovery_needed(paths.home)
        retention = RECOVERY_AGE_DAYS if recovery else min_age_days
        budget = RemovalBudget()
        if apply:
            try:
                budget = removal_budget(paths, CAPACITY_ATTEMPTS if recovery else MAX_ATTEMPTS)
            except OSError, RuntimeError, ValueError:
                return retained_inventory(entries, "native-removal budget inspection failed; retained")
            if not budget.allowance:
                return retained_inventory(entries, "rolling 24-hour native-removal budget exhausted; retained")
        allowance = CAPACITY_PUBLICATION_LOOKUPS if recovery else MAX_PUBLICATION_LOOKUPS
        with publication_window(allowance), retention_window():
            return review_inventory(
                entries,
                paths,
                budget,
                apply=apply,
                recovery=recovery,
                min_age_days=retention,
                started=started,
                progress=progress,
            )


def checkpoint_review(paths: RuntimePaths, batch: InventoryPage, progress: ReviewProgress) -> InventoryPage:
    if progress.examined == len(batch.entries):
        return batch
    entry = progress.last_started
    cursor = (
        inventory_cursor(state_directory(paths), paths.code)
        if entry is None
        else InventoryCursor(repository=entry.repository, worktree=entry.path, errors=batch.errors)
    )
    # Resume after the last started review, never after the unexamined suffix.
    # A timed-out candidate is retained and retried on the next rotation; it
    # cannot monopolize every sweep. This checkpoint grants no removal authority.
    return replace(batch, cursor=cursor, pending=True)


def npm_arguments(paths: RuntimePaths) -> list[str]:
    installations = paths.home / ".local/share/mise/installs/node"
    if not cleanup_activity.present(installations / "global"):
        executable = shutil.which("npm", path=user_environment(paths)["PATH"])
        if executable is not None and Path(executable).resolve().name not in {"mise", "corepack"}:
            return [executable]
        message = "Native npm is unavailable"
        raise RuntimeError(message)
    current = (installations / "global").resolve(strict=True)
    if not current.is_relative_to(installations.resolve(strict=True)):
        message = "Current Node installation is outside the managed user tools"
        raise RuntimeError(message)
    node = current / "bin/node"
    package = current / "lib/node_modules/npm"
    NpmPackage.model_validate_json(bounded_bytes(package / "package.json"))
    cli = package / "bin/npm-cli.js"
    if not os.access(node, os.X_OK) or not cli.is_file():
        message = "Native npm is unavailable"
        raise RuntimeError(message)
    return [str(node), str(cli)]


def maintenance_idle() -> bool:
    io_pressure = pressure(Path("/proc/pressure/io"))
    memory_pressure = pressure(Path("/proc/pressure/memory"))
    for stalls, threshold in ((io_pressure, CACHE_IO_THRESHOLD), (memory_pressure, CACHE_MEMORY_THRESHOLD)):
        if stalls.some is None or stalls.full is None or max(stalls.some, stalls.full) >= threshold:
            return False
    return True


def prune_cache(paths: RuntimePaths) -> str:
    if not maintenance_idle():
        return "skipped: memory or disk pressure is high or unknown"
    if inspected_processes(paths, ()).package_writer:
        return "skipped: package manager is active"
    # npm verifies/collects its download cache; it does not remove live _npx environments.
    run(
        [
            *npm_arguments(paths),
            "cache",
            "verify",
            "--cache",
            str(paths.home / ".npm"),
            "--offline",
            "--ignore-scripts",
        ],
        capture_output=True,
        cwd=paths.home,
        env=user_environment(paths),
        timeout=30,
    )
    return "npm download cache verified; runtime environments and agent/browser data retained"


def state_directory(paths: RuntimePaths) -> Path:
    state = paths.home / ".local/state/long-running-agent"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not owned_path(state, paths.home):
        message = "Cleanup state directory must be owned and writable only by your user"
        raise RuntimeError(message)
    return state


@contextmanager
def cleanup_lock(paths: RuntimePaths, *, daemon: bool = False) -> Generator[Path]:
    state = state_directory(paths)
    descriptor = os.open(
        state / ("cleanup-daemon.lock" if daemon else "cleanup.lock"),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
        0o600,
    )
    with os.fdopen(descriptor, "w") as lock:
        metadata = os.fstat(lock.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            message = "Cleanup lock must be your own regular file"
            raise RuntimeError(message)
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield state


def cache_maintenance(paths: RuntimePaths, state: Path, decisions: list[Decision], *, elapsed: float) -> str:
    stamp = state / "cache-pruned"
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 7 * DAY:
        return "skipped: weekly npm verification is not due"
    if any(decision.attempted for decision in decisions):
        return "skipped: native worktree removal used this sweep; npm verification deferred"
    if elapsed >= CACHE_START_SECONDS:
        return "skipped: worktree inspection used cache time budget; npm verification deferred"
    try:
        cache = prune_cache(paths)
    except OSError, RuntimeError:
        return "skipped: native package-store cleanup was unavailable or busy"
    if cache.startswith("npm"):
        private_write(stamp, "Native package-store prune completed.\n")
    return cache


baseline_app = typer.Typer(help="Record and inspect a per-volume clean-footprint baseline.", no_args_is_help=True)
app.add_typer(baseline_app, name="baseline")


@baseline_app.command("status")
def baseline_status() -> None:
    """Show computed thresholds and whether this volume has a calibrated baseline."""
    current = cleanup_capacity.policy(RuntimePaths.current().home)
    typer.echo(json.dumps(asdict(current)))


@baseline_app.command("calibrate")
def baseline_calibrate(*, apply: bool = False, replace: bool = False) -> None:
    """Preview the current footprint; use --apply only after finishing authorized cleanup."""
    if os.getuid() == 0:
        message = "Calibrate maintenance as your own user, never with sudo"
        raise RuntimeError(message)
    paths = RuntimePaths.current()
    if apply:
        with cleanup_lock(paths):
            current = cleanup_capacity.calibrate(paths.home, apply=True, replace=replace)
    else:
        current = cleanup_capacity.calibrate(paths.home, replace=replace)
    typer.echo(json.dumps({"applied": apply, "policy": asdict(current)}))


@app.command("run")
def cleanup_run(
    *,
    apply: bool = False,
    capacity_recovery: bool = False,
    paged: bool = False,
    min_age_days: Annotated[
        int,
        typer.Option(
            min=1,
            help="Normal retention is 7 days; guarded capacity recovery uses 6 hours above this volume's recovery floor.",
        ),
    ] = 7,
) -> None:
    """Report retained and eligible worktrees; pass --apply to remove at most two."""
    if os.getuid() == 0 or not Path("/proc/self").exists():
        message = "Run cleanup as your own Linux user, never with sudo"
        raise RuntimeError(message)
    paths = RuntimePaths.current()
    with cleanup_lock(paths) as state:
        started = time.monotonic()
        try:
            batch = candidate_page(paths) if paged else None
            progress = ReviewProgress() if apply and batch is not None else None
            decisions = reap(
                paths,
                apply=apply,
                min_age_days=min_age_days,
                capacity_recovery=capacity_recovery,
                batch=batch,
                progress=progress,
            )
        except InventoryLimitError as error:
            failure = InventoryFailure(version=1, stage="repository-discovery", kind=error.kind)
            typer.echo(failure.model_dump_json())
            raise typer.Exit(code=1) from error
        if batch is not None and progress is not None:
            batch = checkpoint_review(paths, batch, progress)
            save_inventory_cursor(state, paths.code, batch.cursor)
        cache = (
            cache_maintenance(paths, state, decisions, elapsed=time.monotonic() - started) if apply else "report only"
        )
        typer.echo(
            json.dumps(
                {
                    "apply": apply,
                    "max_removals": MAX_REMOVALS,
                    "worktrees": [asdict(item) for item in decisions],
                    "cache": cache,
                    "inventory_paged": paged,
                    "inventory_pending": False if batch is None else batch.pending,
                    "inventory_errors": 0 if batch is None else batch.errors,
                }
            )
        )


def service_text(executable: Path) -> str:
    return (
        "[Unit]\nDescription=Retire unused merged developer worktrees\n"
        "[Service]\nType=oneshot\nUMask=0077\nNice=19\nIOSchedulingClass=idle\nNoNewPrivileges=true\n"
        "CPUQuota=10%\nMemoryMax=256M\nTimeoutStartSec=5min\n"
        f"ExecStart={systemd_value(str(executable))} -m long_running_agent.cli runtime cleanup run --apply\n"
    )


@app.command("enable")
def enable() -> None:
    """Enable daily cleanup for your own user without restarting any agent services."""
    if os.getuid() == 0 or not Path("/proc/self").exists():
        message = "Enable cleanup as your own Linux user, never with sudo"
        raise RuntimeError(message)
    paths = RuntimePaths.current()
    executable = Path(sys.executable)
    if not executable.is_relative_to(paths.home / ".local/share"):
        message = "Use the installed lra command to enable cleanup; checkout Python environments are not durable"
        raise RuntimeError(message)
    folder = paths.home / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not owned_path(folder, paths.home):
        message = "User unit directory must be owned and writable only by your user"
        raise RuntimeError(message)
    private_write(folder / SERVICE, service_text(executable))
    private_write(
        folder / TIMER,
        "[Unit]\nDescription=Daily developer worktree cleanup\n[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\n[Install]\nWantedBy=timers.target\n",
    )
    run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15)
    run(["systemctl", "--user", "enable", "--now", TIMER], capture_output=True, timeout=15)
    typer.echo("Daily cleanup enabled for your user: 7-day retention, at most two unused merged worktrees per run.")
