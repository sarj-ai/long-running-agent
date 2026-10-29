from __future__ import annotations

from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat
import time
from typing import Annotated, Final, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, RootModel

from .settings import private_write


DAY: Final = 86400
MAX_ATTEMPTS: Final = 2
CAPACITY_ATTEMPTS: Final = 256
PRIVATE_LIMIT: Final = 256 * 1024
CURSOR_LIMIT: Final = 8192
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def private_payload(path: Path, limit: int = PRIVATE_LIMIT) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > limit
        ):
            message = "Maintenance state must be a bounded private regular file owned by your user"
            raise RuntimeError(message)
        payload = stream.read(limit + 1)
    if len(payload) > limit:
        message = "Maintenance state exceeded its bounded size"
        raise RuntimeError(message)
    return payload


def optional_payload(path: Path) -> bytes | None:
    try:
        return private_payload(path)
    except FileNotFoundError:
        return None


class AttemptStamp(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    wall: Timestamp
    uptime: Timestamp
    boot_id: UUID

    @classmethod
    def current(cls) -> Self:
        return cls(
            wall=time.time(),
            uptime=time.clock_gettime(time.CLOCK_BOOTTIME),
            boot_id=UUID(Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()),
        )

    def expired(self, current: AttemptStamp) -> bool:
        if self.boot_id == current.boot_id:
            return current.uptime - self.uptime >= DAY
        # A new boot must itself observe a full day before old receipts can expire.
        return current.uptime >= DAY and current.wall - self.wall >= DAY


class AttemptReceipt(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    stamp: AttemptStamp
    path: Path
    head: Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class AttemptHistory(RootModel[list[AttemptReceipt]]):
    model_config = ConfigDict(strict=True)
    root: list[AttemptReceipt] = Field(max_length=CAPACITY_ATTEMPTS)


class ReviewCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Annotated[int, Field(ge=1, le=1)]
    modified: Timestamp
    path: Path

    def within(self, root: Path) -> None:
        if (
            not self.path.is_absolute()
            or self.path == root
            or not self.path.is_relative_to(root)
            or ".." in self.path.parts
            or len(os.fsencode(self.path)) > CURSOR_LIMIT // 2
        ):
            message = "The cleanup inspection cursor has an unknown checkout path; retained"
            raise RuntimeError(message)


def review_cursor(state: Path, root: Path) -> ReviewCursor | None:
    try:
        payload = private_payload(state / "cleanup-cursor.json", CURSOR_LIMIT)
    except FileNotFoundError:
        return None
    cursor = ReviewCursor.model_validate_json(payload)
    cursor.within(root)
    return cursor


def save_review_cursor(state: Path, root: Path, cursor: ReviewCursor) -> None:
    cursor.within(root)
    # Unknown or non-private state must never be silently replaced by a fresh hint.
    review_cursor(state, root)
    payload = cursor.model_dump_json() + "\n"
    if len(payload.encode("utf-8")) > CURSOR_LIMIT:
        message = "The cleanup inspection cursor exceeds its private state limit; retained"
        raise RuntimeError(message)
    private_write(state / "cleanup-cursor.json", payload)


def quarantine_advisory(path: Path, *, limit: int = PRIVATE_LIMIT) -> None:
    # Only derived progress/status may be reset. Removal receipts and capacity
    # attestations must retain their original fail-closed semantics.
    if path.name not in {"cleanup-daemon.json", "cleanup-inventory-cursor.json"}:
        message = "Only known derived maintenance status may be quarantined"
        raise ValueError(message)
    private_payload(path, limit)
    backup = path.with_name(path.name + ".invalid")
    if backup.exists() or backup.is_symlink():
        private_payload(backup, limit)
    path.replace(backup)


class InventoryCursor(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Annotated[int, Field(ge=1, le=1)] = 1
    repository: Path
    worktree: Path | None = None
    errors: Annotated[int, Field(ge=0, le=4096)] = 0

    def within(self, root: Path) -> None:
        if self.worktree == root:
            message = "Primary code root is not a candidate worktree"
            raise ValueError(message)
        for path in (self.repository, self.worktree):
            if path is None:
                continue
            if (
                not path.is_absolute()
                or not path.is_relative_to(root)
                or ".." in path.parts
                or len(os.fsencode(path)) > CURSOR_LIMIT // 2
            ):
                message = "Unknown candidate-discovery progress path"
                raise ValueError(message)


def inventory_cursor(state: Path, root: Path) -> InventoryCursor | None:
    path = state / "cleanup-inventory-cursor.json"
    try:
        payload = private_payload(path, CURSOR_LIMIT)
    except FileNotFoundError:
        return None
    try:
        cursor = InventoryCursor.model_validate_json(payload)
        cursor.within(root)
    except ValueError:
        quarantine_advisory(path, limit=CURSOR_LIMIT)
        return None
    return cursor


def save_inventory_cursor(state: Path, root: Path, cursor: InventoryCursor | None) -> None:
    path = state / "cleanup-inventory-cursor.json"
    inventory_cursor(state, root)
    if cursor is None:
        path.unlink(missing_ok=True)
        return
    cursor.within(root)
    payload = cursor.model_dump_json() + "\n"
    if len(payload.encode()) > CURSOR_LIMIT:
        message = "Candidate-discovery progress exceeds its private limit"
        raise ValueError(message)
    private_write(path, payload)


@dataclass(frozen=True)
class ActiveReceipts:
    receipts: tuple[AttemptReceipt, ...]
    initialized: bool


def active_receipts(state: Path, current: AttemptStamp) -> ActiveReceipts:
    ledger = state / "cleanup-attempts.json"
    marker = state / "cleanup-attempts-initialized"
    payload = optional_payload(ledger)
    initialized = optional_payload(marker)
    if initialized not in {None, b"1\n"} or (initialized is not None and payload is None):
        message = "The native-removal budget is missing or unknown; retained"
        raise RuntimeError(message)
    history = AttemptHistory.model_validate_json(b"[]" if payload is None else payload)
    if any(not receipt.path.is_absolute() for receipt in history.root):
        message = "A native-removal receipt has an unknown checkout path; retained"
        raise RuntimeError(message)
    return ActiveReceipts(
        receipts=tuple(receipt for receipt in history.root if not receipt.stamp.expired(current)),
        initialized=initialized is not None,
    )


def attempt_limit(limit: int) -> int:
    if type(limit) is not int or not 1 <= limit <= CAPACITY_ATTEMPTS:
        message = "The native-removal policy has an invalid daily limit; retained"
        raise ValueError(message)
    return limit


def remaining_attempts(state: Path, current: AttemptStamp, *, limit: int = MAX_ATTEMPTS) -> int:
    return max(0, attempt_limit(limit) - len(active_receipts(state, current).receipts))


def reserve_attempt(state: Path, current: AttemptStamp, path: Path, head: str, *, limit: int = MAX_ATTEMPTS) -> bool:
    # Charge before native deletion; callers hold the cleanup flock throughout.
    active = active_receipts(state, current)
    if not path.is_absolute():
        message = "A native-removal receipt has an unknown checkout path; retained"
        raise RuntimeError(message)
    if len(active.receipts) >= attempt_limit(limit):
        return False
    receipt = AttemptReceipt(stamp=current, path=path, head=head)
    payload = AttemptHistory([*active.receipts, receipt]).model_dump_json() + "\n"
    if len(payload.encode("utf-8")) > PRIVATE_LIMIT:
        message = "The next native-removal receipt exceeds the private state limit; retained"
        raise RuntimeError(message)
    private_write(state / "cleanup-attempts.json", payload)
    if not active.initialized:
        private_write(state / "cleanup-attempts-initialized", "1\n")
    return True


def valid_time(value: float) -> bool:
    return math.isfinite(value) and value >= 0
