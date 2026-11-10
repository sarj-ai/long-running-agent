from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import re
import stat
from typing import TYPE_CHECKING, Annotated, Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

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


class TaskCompleteEvent(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["task_complete"]
    turn_id: str | None = None
    error: NativeError | None


class TurnActivity(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["task_started", "user_message"]


class TurnAborted(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["turn_aborted", "task_aborted"]


class NativeEnvelope(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: str
    timestamp: str
    payload: dict[str, JsonValue]


class NativeRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["event_msg"]
    timestamp: str
    payload: Annotated[TaskCompleteEvent | TurnActivity | TurnAborted, Field(discriminator="type")]


class NativeToolActivity(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    type: Literal["response_item"]
    timestamp: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True)
class ObservedCompletion:
    state: Literal["success", "failed", "active", "aborted", "unknown"]


@dataclass(frozen=True)
class CapacityCompletion:
    fingerprint: str
    state: Literal["capacity"] = "capacity"


type Completion = ObservedCompletion | CapacityCompletion


class NativeEventError(ValueError):
    pass


def _decode_envelope(envelope: NativeEnvelope) -> NativeRecord | NativeToolActivity | None:
    if envelope.type == "response_item":
        return NativeToolActivity.model_validate(envelope.model_dump())
    if envelope.type != "event_msg":
        return None
    kind = envelope.payload.get("type")
    if not isinstance(kind, str):
        message = "Malformed native event"
        raise NativeEventError(message)
    if kind not in {"task_complete", "task_started", "user_message", "turn_aborted", "task_aborted"}:
        return None
    return NativeRecord.model_validate(envelope.model_dump())


def decode_event(line: bytes) -> NativeRecord | NativeToolActivity | None:
    try:
        return _decode_envelope(NativeEnvelope.model_validate_json(line))
    except ValidationError:
        message = "Malformed native event"
        raise NativeEventError(message) from None


def latest_completion(data: bytes) -> Completion:
    if not data.endswith(b"\n"):
        return ObservedCompletion("unknown")
    for line in reversed(data.splitlines()):
        try:
            item = decode_event(line)
        except NativeEventError:
            return ObservedCompletion("unknown")
        if item is None:
            continue
        if isinstance(item, NativeToolActivity):
            # A tool/output item after an old completion proves intervening activity,
            # never permission to replay an earlier capacity failure.
            return ObservedCompletion("active")
        event = item.payload
        match event:
            case TurnAborted():
                return ObservedCompletion("aborted")
            case TurnActivity():
                return ObservedCompletion("active")
            case TaskCompleteEvent():
                pass
            case _:
                assert_never(event)
        if event.error is None:
            return ObservedCompletion("success")
        if CAPACITY_MESSAGE.fullmatch(" ".join(event.error.message.split())) is None:
            return ObservedCompletion("failed")
        identity = f"{item.timestamp}\0{event.turn_id}\0{event.error.message}"
        return CapacityCompletion(hashlib.sha256(identity.encode()).hexdigest())
    return ObservedCompletion("unknown")


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
            return ObservedCompletion("unknown")
        return latest_completion(read_tail(writers[0]))
    except OSError, ValueError:
        return ObservedCompletion("unknown")
