"""Pure retry decisions. Dispatch is durable before any terminal input is sent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final, Literal, assert_never

from pydantic import BaseModel, ConfigDict, Field


INITIAL_DELAY: Final = 30
MAX_ATTEMPTS: Final = 8
MAX_DELAY: Final = 900
COOLDOWN: Final = 3600
Timestamp = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Fingerprint = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
SubmittedCount = Annotated[int, Field(ge=1, le=MAX_ATTEMPTS)]


class RetryRecord(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, hide_input_in_errors=True)
    error: Fingerprint
    due: Timestamp


class WaitingAttempt(RetryRecord):
    phase: Literal["waiting"] = "waiting"
    attempts: Annotated[int, Field(ge=0, lt=MAX_ATTEMPTS)] = 0


class DispatchingAttempt(RetryRecord):
    phase: Literal["dispatching"] = "dispatching"
    attempts: SubmittedCount


class SentAttempt(RetryRecord):
    phase: Literal["sent"] = "sent"
    attempts: SubmittedCount


class UncertainAttempt(RetryRecord):
    phase: Literal["uncertain"] = "uncertain"
    attempts: SubmittedCount


class ExhaustedAttempt(RetryRecord):
    phase: Literal["exhausted"] = "exhausted"
    attempts: Literal[8] = MAX_ATTEMPTS


type Attempt = Annotated[
    WaitingAttempt | DispatchingAttempt | SentAttempt | UncertainAttempt | ExhaustedAttempt,
    Field(discriminator="phase"),
]


def uncertain(attempt: DispatchingAttempt | SentAttempt | UncertainAttempt) -> UncertainAttempt:
    return UncertainAttempt(error=attempt.error, attempts=attempt.attempts, due=attempt.due)


def acknowledged(attempt: DispatchingAttempt) -> SentAttempt:
    return SentAttempt(error=attempt.error, attempts=attempt.attempts, due=attempt.due)


@dataclass(frozen=True)
class Wait:
    attempt: WaitingAttempt | SentAttempt


@dataclass(frozen=True)
class Block:
    attempt: UncertainAttempt


@dataclass(frozen=True)
class Cooldown:
    attempt: ExhaustedAttempt


@dataclass(frozen=True)
class Dispatch:
    attempt: DispatchingAttempt


type Decision = Wait | Block | Cooldown | Dispatch


def observe_capacity(attempt: Attempt | None, error: str, now: float, *, available: bool) -> Decision:
    """Only a fresh native failure can clear uncertainty; time alone never does."""
    if attempt is None:
        return Wait(WaitingAttempt(error=error, due=now + INITIAL_DELAY))
    match attempt:
        case DispatchingAttempt() | SentAttempt() | UncertainAttempt():
            if attempt.error == error:
                if isinstance(attempt, SentAttempt) and now < attempt.due:
                    return Wait(attempt)
                return Block(uncertain(attempt))
        case WaitingAttempt() | ExhaustedAttempt():
            pass
        case _:
            assert_never(attempt)
    if attempt.attempts == MAX_ATTEMPTS:
        if now < attempt.due:
            return Cooldown(ExhaustedAttempt(error=attempt.error, due=attempt.due))
        return Wait(WaitingAttempt(error=error, due=now + INITIAL_DELAY))
    if now < attempt.due or not available:
        return Wait(WaitingAttempt(error=attempt.error, attempts=attempt.attempts, due=attempt.due))
    count = attempt.attempts + 1
    delay = COOLDOWN if count == MAX_ATTEMPTS else min(MAX_DELAY, 60 * (1 << (count - 1)))
    return Dispatch(DispatchingAttempt(error=error, attempts=count, due=now + delay))
