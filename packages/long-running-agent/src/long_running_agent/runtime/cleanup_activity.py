from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
import os
from pathlib import Path
import re
import sqlite3
import stat
import time
import tomllib
from typing import TYPE_CHECKING, Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator
    import datetime as dt


type TomlValue = str | bool | int | float | dt.datetime | dt.date | dt.time | list[TomlValue] | dict[str, TomlValue]


MAX_HOMES: Final = 128
MAX_DATABASES: Final = 128
MAX_ENTRIES: Final = 4096
MAX_ROWS: Final = 32768
MAX_BYTES: Final = 8 * 1024 * 1024
MAX_FILE_BYTES: Final = 256 * 1024
MAX_RECORD_BYTES: Final = 16384
MAX_PATH_BYTES: Final = 4096
COLLECTION_SECONDS: Final = 30
QUERY_SECONDS: Final = 5
DATABASE_NAME: Final = re.compile(r"state_[0-9]+\.sqlite")
THREAD_COLUMNS: Final = {
    "id": "TEXT",
    "cwd": "TEXT",
    "created_at": "INTEGER",
    "updated_at": "INTEGER",
    "archived": "INTEGER",
}


class MetadataChangedError(RuntimeError):
    """A fresh complete inspection may recover from an atomic metadata replacement."""


class DatabaseBusyError(RuntimeError):
    """Native SQLite could not obtain its read lock within the bounded wait."""


def database_inspection_error(error: sqlite3.Error) -> RuntimeError:
    message = "Codex activity database could not be verified"
    if hasattr(error, "sqlite_errorcode") and error.sqlite_errorcode & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return DatabaseBusyError(message)
    return RuntimeError(message)


@dataclass(frozen=True)
class ActivityReferences:
    paths: frozenset[Path]
    homes: int
    databases: int
    storage: frozenset[Path] = frozenset()


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


class ActivityRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore")
    id: UUID
    agent: Literal["codex", "claude"]
    uid: int
    store: str


@dataclass
class InventoryBudget:
    deadline: float
    entries: int = 0
    rows: int = 0
    bytes_read: int = 0
    paths: set[Path] = field(default_factory=set)
    storage: set[Path] = field(default_factory=set)

    def check(self) -> None:
        if time.monotonic() > self.deadline:
            message = "Codex activity metadata exceeded its inspection deadline"
            raise RuntimeError(message)

    def entry(self) -> None:
        self.check()
        self.entries += 1
        if self.entries > MAX_ENTRIES:
            message = "Codex activity metadata exceeded its directory-entry limit"
            raise RuntimeError(message)

    def charge_bytes(self, amount: int) -> None:
        self.check()
        self.bytes_read += amount
        if self.bytes_read > MAX_BYTES:
            message = "Codex activity metadata exceeded its byte limit"
            raise RuntimeError(message)

    def reference(self, value: object) -> None:
        self.check()
        self.rows += 1
        if self.rows > MAX_ROWS:
            message = "Codex activity metadata exceeded its row limit"
            raise RuntimeError(message)
        if not isinstance(value, str) or not value or "\0" in value or not Path(value).is_absolute():
            message = "Codex activity metadata contains an unknown workspace path"
            raise RuntimeError(message)
        size = len(os.fsencode(value))
        self.charge_bytes(size)
        if size > MAX_PATH_BYTES:
            message = "Codex activity metadata contains an oversized workspace path"
            raise RuntimeError(message)
        path = Path(value)
        self.paths.add(path)

    def resolved_paths(self) -> frozenset[Path]:
        # Resolve workspace aliases after SQLite statements finish. Filesystem
        # latency must not consume the SQL VM's separate progress deadline.
        for path in tuple(self.paths):
            self.check()
            self.paths.add(path.resolve())
        self.check()
        return frozenset(self.paths)


def present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(metadata.st_dev, metadata.st_ino)


def validate_component(component: Path, *, directory: bool, private: bool, allow_links: bool) -> None:
    metadata = component.lstat()
    if metadata.st_uid != os.getuid():
        message = "Codex activity storage is not owned by the current user"
        raise RuntimeError(message)
    if stat.S_ISLNK(metadata.st_mode):
        if allow_links:
            return
        message = "Codex activity metadata contains an unexpected symbolic link"
        raise RuntimeError(message)
    expected = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not expected or metadata.st_mode & (0o077 if private else 0o022):
        message = "Codex activity storage has an unsafe type or permissions"
        raise RuntimeError(message)


def owned_path(path: Path, home: Path, *, directory: bool, private: bool = False, allow_links: bool = False) -> Path:
    if not path.is_absolute() or not path.is_relative_to(home) or ".." in path.parts:
        message = "Codex activity storage escapes the owned user home"
        raise RuntimeError(message)
    for component in (path, *path.parents):
        validate_component(
            component,
            directory=component != path or directory,
            private=component == path and private,
            allow_links=allow_links,
        )
        if component == home:
            break
    resolved = path.resolve(strict=True)
    if resolved != path:
        return owned_path(resolved, home, directory=directory, private=private)
    return resolved


def entries(directory: Path, budget: InventoryBudget) -> Iterator[os.DirEntry[str]]:
    with os.scandir(directory) as children:
        for child in children:
            budget.entry()
            yield child


def path_value(value: object, home: Path) -> Path:
    if not isinstance(value, str) or not value or "\0" in value or len(os.fsencode(value)) > MAX_PATH_BYTES:
        message = "Codex activity metadata contains an unknown storage path"
        raise RuntimeError(message)
    path = home / value[2:] if value.startswith("~/") else Path(value)
    if not path.is_absolute():
        message = "Codex activity storage path is not absolute"
        raise RuntimeError(message)
    return path


def metadata_bytes(
    path: Path, home: Path, budget: InventoryBudget, *, private: bool = False, limit: int = MAX_FILE_BYTES
) -> bytes:
    resolved = owned_path(path, home, directory=False, private=private, allow_links=not private)
    expected = resolved.stat()
    descriptor = os.open(resolved, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        actual = os.fstat(stream.fileno())
        if identity(actual) != identity(expected):
            message = "Codex activity metadata changed identity during inspection"
            raise MetadataChangedError(message)
        if (
            not stat.S_ISREG(actual.st_mode)
            or actual.st_uid != os.getuid()
            or actual.st_mode & (0o077 if private else 0o022)
            or actual.st_nlink != 1
        ):
            message = "Codex activity metadata permissions changed during inspection"
            raise RuntimeError(message)
        value = stream.read(limit + 1)
    budget.charge_bytes(len(value))
    if len(value) > limit:
        message = "Codex activity metadata file exceeded its byte limit"
        raise RuntimeError(message)
    return value


def registry_homes(home: Path, budget: InventoryBudget) -> set[Path]:
    directory = home / ".local/share/long-running-agent/activity"
    if not present(directory):
        return set()
    owned_path(directory, home, directory=True, private=True)
    homes: set[Path] = set()
    for child in entries(directory, budget):
        if not child.name.endswith(".json"):
            continue
        path = Path(child.path)
        try:
            record = ActivityRecord.model_validate_json(
                metadata_bytes(path, home, budget, private=True, limit=MAX_RECORD_BYTES)
            )
        except ValueError:
            message = "Codex activity registry record has an unknown schema"
            raise RuntimeError(message) from None
        if str(record.id) != path.stem or record.uid != os.getuid():
            message = "Codex activity registry identity is invalid"
            raise RuntimeError(message)
        declared = path_value(record.store, home)
        resolved = owned_path(declared, home, directory=True, allow_links=True)
        budget.storage.update((declared, resolved))
        if record.agent == "codex":
            homes.add(declared)
    return homes


def profile_homes(home: Path, budget: InventoryBudget) -> set[Path]:
    directory = home / ".local/share/long-running-agent/account-switch"
    if not present(directory):
        return set()
    owned_path(directory, home, directory=True, private=True)
    homes: set[Path] = set()
    for child in entries(directory, budget):
        if child.name == "backups":
            continue
        if child.is_symlink():
            message = "Codex account storage contains an unexpected symbolic link"
            raise RuntimeError(message)
        if child.is_dir(follow_symlinks=False):
            homes.add(owned_path(Path(child.path), home, directory=True, private=True))
    return homes


def configured_sqlite_homes(configuration: dict[str, TomlValue], budget: InventoryBudget) -> Iterator[object]:
    pending: list[TomlValue] = [configuration]
    while pending:
        budget.entry()
        value = pending.pop()
        children: Iterable[TomlValue]
        if isinstance(value, dict):
            table: dict[str, TomlValue] = value
            if "sqlite_home" in table:
                yield table["sqlite_home"]
            children = table.values()
        elif isinstance(value, list):
            array: list[TomlValue] = value
            children = array
        else:
            continue
        for child in children:
            budget.entry()
            if isinstance(child, (dict, list)):
                pending.append(child)


def sqlite_roots(store: Path, home: Path, budget: InventoryBudget) -> set[Path]:
    roots = {store}
    budget.storage.add(store)
    configuration = store / "config.toml"
    if not present(configuration):
        return roots
    try:
        data: dict[str, TomlValue] = tomllib.loads(metadata_bytes(configuration, home, budget).decode("utf-8"))
    except UnicodeError:
        message = "Codex SQLite storage configuration has an unknown encoding"
        raise RuntimeError(message) from None
    except ValueError:
        message = "Codex SQLite storage configuration is invalid"
        raise RuntimeError(message) from None
    for value in configured_sqlite_homes(data, budget):
        declared = path_value(value, home)
        resolved = owned_path(declared, home, directory=True, allow_links=True)
        budget.storage.update((declared, resolved))
        roots.add(resolved)
    return roots


def saved_state_without_database(store: Path, home: Path, budget: InventoryBudget) -> bool:
    for name in ("sessions", "archived_sessions"):
        directory = store / name
        if present(directory):
            owned_path(directory, home, directory=True, allow_links=True)
            if next(entries(directory, budget), None) is not None:
                return True
    for name in ("session_index.jsonl", "history.jsonl"):
        path = store / name
        if present(path):
            owned_path(path, home, directory=False, allow_links=True)
            if path.stat().st_size:
                return True
    return False


def database_paths(store: Path, home: Path, budget: InventoryBudget) -> set[Path]:
    databases: set[Path] = set()
    for root in sqlite_roots(store, home, budget):
        root_databases: set[Path] = set()
        for child in entries(root, budget):
            if DATABASE_NAME.fullmatch(child.name):
                declared = Path(child.path)
                resolved = owned_path(declared, home, directory=False, allow_links=True)
                budget.storage.update((declared, resolved))
                root_databases.add(resolved)
            elif child.name.startswith("state_") and child.name.endswith(".sqlite"):
                message = "Codex activity database filename is unknown"
                raise RuntimeError(message)
        if root != store and not root_databases:
            message = "Configured Codex SQLite storage has no native metadata database"
            raise RuntimeError(message)
        databases.update(root_databases)
    if not databases and saved_state_without_database(store, home, budget):
        message = "Saved Codex activity has no readable native metadata database"
        raise RuntimeError(message)
    return databases


def check_schema(connection: sqlite3.Connection) -> bool:
    table_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT name,type FROM sqlite_schema WHERE name IN ('threads','project_roots')"
    ).fetchall()
    tables = {str(row[0]): str(row[1]) for row in table_rows}
    column_rows: list[tuple[object, ...]] = connection.execute("PRAGMA table_info(threads)").fetchall()
    columns = {str(row[1]): str(row[2]) for row in column_rows}
    if tables.get("threads") != "table" or any(columns.get(name) != kind for name, kind in THREAD_COLUMNS.items()):
        message = "Codex activity database schema is unknown"
        raise RuntimeError(message)
    if "project_roots" not in tables:
        return False
    column_rows = connection.execute("PRAGMA table_info(project_roots)").fetchall()
    columns = {str(row[1]): str(row[2]) for row in column_rows}
    if tables["project_roots"] != "table" or columns.get("path") != "TEXT":
        message = "Codex project-root database schema is unknown"
        raise RuntimeError(message)
    return True


def check_completeness(connection: sqlite3.Connection) -> None:
    table_rows: list[tuple[object, ...]] = connection.execute(
        "SELECT name,type FROM sqlite_schema WHERE name IN ('backfill_state','rollout_migration_skipped_rollouts')"
    ).fetchall()
    tables = {str(row[0]): str(row[1]) for row in table_rows}
    if tables.get("backfill_state") != "table" or tables.get("rollout_migration_skipped_rollouts") != "table":
        message = "Codex activity completeness metadata schema is unknown"
        raise RuntimeError(message)
    column_rows: list[tuple[object, ...]] = connection.execute("PRAGMA table_info(backfill_state)").fetchall()
    if not any(row[1] == "status" and row[2] == "TEXT" for row in column_rows):
        message = "Codex activity completeness status schema is unknown"
        raise RuntimeError(message)
    status: list[tuple[object, ...]] = connection.execute("SELECT status FROM backfill_state LIMIT 2").fetchall()
    if len(status) != 1 or status[0][0] != "complete":
        message = "Codex activity metadata backfill is incomplete or unknown"
        raise RuntimeError(message)
    if connection.execute("SELECT 1 FROM rollout_migration_skipped_rollouts LIMIT 1").fetchone() is not None:
        message = "Codex activity metadata contains skipped native rollouts"
        raise RuntimeError(message)


def database_layout(database: Path, home: Path) -> dict[Path, FileIdentity]:
    owned_path(database, home, directory=False)
    descriptor = os.open(database, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
        ):
            message = "Codex activity database has unsafe permissions or ambiguous hard links"
            raise RuntimeError(message)
    identities = {database: identity(metadata)}
    sidecars = [database.with_name(database.name + suffix) for suffix in ("-wal", "-shm")]
    present_sidecars = [path for path in sidecars if present(path)]
    # A checkpointed WAL database can legitimately have no sidecars.
    # Native SQLite mode=ro manages its coordination files and locking.
    # An existing incomplete pair remains an inspection failure.
    if present_sidecars and len(present_sidecars) != len(sidecars):
        message = "Codex WAL metadata requires existing native WAL and SHM files"
        raise RuntimeError(message)
    for path in present_sidecars:
        owned_path(path, home, directory=False)
        metadata = path.stat()
        if metadata.st_nlink != 1:
            message = "Codex activity database sidecar has ambiguous hard links"
            raise RuntimeError(message)
        identities[path] = identity(metadata)
    return identities


def include_workspaces(
    rows: Iterable[tuple[object, ...]], budget: InventoryBudget, *, begin_step: Callable[[], None]
) -> None:
    iterator = iter(rows)
    while True:
        begin_step()
        try:
            row = next(iterator)
        except StopIteration:
            return
        budget.reference(row[0])


def read_database(database: Path, home: Path, budget: InventoryBudget) -> None:
    budget.check()
    before = database_layout(database, home)
    deadline = min(budget.deadline, time.monotonic() + QUERY_SECONDS)

    def deadline_reached() -> int:
        return int(time.monotonic() > deadline)

    def begin_step() -> None:
        nonlocal deadline
        budget.check()
        deadline = min(budget.deadline, time.monotonic() + QUERY_SECONDS)

    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=1)) as connection:
        connection.set_progress_handler(deadline_reached, 1000)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("BEGIN")
        projects = check_schema(connection)
        begin_step()
        check_completeness(connection)
        begin_step()
        include_workspaces(
            connection.execute("SELECT cwd FROM threads LIMIT ?", (MAX_ROWS + 1,)), budget, begin_step=begin_step
        )
        if projects:
            begin_step()
            include_workspaces(
                connection.execute("SELECT path FROM project_roots LIMIT ?", (MAX_ROWS + 1,)),
                budget,
                begin_step=begin_step,
            )
    if any(identity(path.stat()) != previous for path, previous in before.items()):
        message = "Codex activity database changed identity during inspection"
        raise MetadataChangedError(message)


def collect(home: Path, *, codex_home: Path | None = None) -> ActivityReferences:
    budget = InventoryBudget(time.monotonic() + COLLECTION_SECONDS)
    home = owned_path(home, home, directory=True)
    registered = registry_homes(home, budget)
    required = set(registered)
    candidates = registered | profile_homes(home, budget) | {home / ".codex"}
    if codex_home is not None:
        explicit = path_value(str(codex_home), home)
        candidates.add(explicit)
        required.add(explicit)
    if len(candidates) > MAX_HOMES:
        message = "Codex activity home inventory exceeded its limit"
        raise RuntimeError(message)
    stores: set[Path] = set()
    seen: set[FileIdentity] = set()
    for candidate in sorted(candidates):
        budget.check()
        if not present(candidate) and candidate not in required:
            continue
        store = owned_path(candidate, home, directory=True, allow_links=True)
        stores.add(store)
        budget.storage.update((candidate, store))
        for database in sorted(database_paths(store, home, budget)):
            metadata = database.stat()
            database_identity = identity(metadata)
            if database_identity in seen:
                continue
            seen.add(database_identity)
            if len(seen) > MAX_DATABASES:
                message = "Codex activity database inventory exceeded its limit"
                raise RuntimeError(message)
            try:
                read_database(database, home, budget)
            except sqlite3.Error as error:
                raise database_inspection_error(error) from None
    return ActivityReferences(budget.resolved_paths(), len(stores), len(seen), frozenset(budget.storage))
