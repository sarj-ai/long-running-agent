from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import stat
from typing import TYPE_CHECKING, Final, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from . import codex_panes


if TYPE_CHECKING:
    from pathlib import Path


TAIL_BYTES: Final = 1_048_576
CAPACITY_MESSAGE: Final = re.compile(
    r"(?:unexpected status 503 [^:]{1,80}:\s*)?(?:The )?selected model is (?:currently )?at capacity[.!]?"
    r"(?:\s*Please try (?:a different model|again(?: later)?)[.!]?)?"
    r"(?:, url: https?://\S+)?",
    re.IGNORECASE,
)


class NativeError(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    message: str


class NativeEvent(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: str
    turn_id: str | None = None
    error: NativeError | None = None


class NativeRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["event_msg"]
    timestamp: str
    payload: NativeEvent


@dataclass(frozen=True)
class Completion:
    state: Literal["capacity", "success", "failed", "active", "aborted", "unknown"]
    fingerprint: str = ""


class NativeEventError(ValueError):
    pass


def decode_event(line: bytes) -> NativeRecord | None:
    try:
        return NativeRecord.model_validate_json(line)
    except ValidationError as error:
        failures = error.errors(include_input=False)
        if any(failure["loc"] == ("type",) and failure["type"] == "literal_error" for failure in failures):
            return None
        message = "Malformed native event"
        raise NativeEventError(message) from None


def latest_completion(data: bytes) -> Completion:
    if not data.endswith(b"\n"):
        return Completion("unknown")
    for line in reversed(data.splitlines()):
        try:
            item = decode_event(line)
        except NativeEventError:
            return Completion("unknown")
        if item is None:
            continue
        event = item.payload
        if event.type in {"turn_aborted", "task_aborted"}:
            return Completion("aborted")
        if event.type in {"task_started", "user_message"}:
            return Completion("active")
        if event.type != "task_complete":
            continue
        if "error" not in event.model_fields_set:
            return Completion("unknown")
        if event.error is None:
            return Completion("success")
        if CAPACITY_MESSAGE.fullmatch(" ".join(event.error.message.split())) is None:
            return Completion("failed")
        identity = f"{item.timestamp}\0{event.turn_id}\0{event.error.message}"
        return Completion("capacity", hashlib.sha256(identity.encode()).hexdigest())
    return Completion("unknown")


def same_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )


def read_tail(writer: Path) -> bytes:
    path = writer.readlink()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & 0o077
            or before.st_nlink != 1
            or not same_snapshot(before, writer.stat())
        ):
            return b""
        offset = max(0, before.st_size - TAIL_BYTES)
        stream.seek(offset)
        data = stream.read(TAIL_BYTES)
        if not same_snapshot(before, os.fstat(stream.fileno())) or not same_snapshot(before, writer.stat()):
            return b""
    return data.partition(b"\n")[2] if offset else data


def completion(target: codex_panes.PaneTarget, *, proc: Path) -> Completion:
    try:
        writers = [
            descriptor
            for descriptor in (proc / str(target.process.pid) / "fd").iterdir()
            if codex_panes.writable_rollout(descriptor, target.home) == target.thread_id
        ]
        if len(writers) != 1:
            return Completion("unknown")
        return latest_completion(read_tail(writers[0]))
    except OSError, ValueError:
        return Completion("unknown")
