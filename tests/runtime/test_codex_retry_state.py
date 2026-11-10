from __future__ import annotations

from long_running_agent.runtime.codex_retry_state import (
    Attempt,
    Block,
    Cooldown,
    Dispatch,
    DispatchingAttempt,
    ExhaustedAttempt,
    SentAttempt,
    UncertainAttempt,
    Wait,
    WaitingAttempt,
    acknowledged,
    observe_capacity,
    uncertain,
)
from pydantic import RootModel, ValidationError
import pytest


class RetryPayload(RootModel[Attempt]):
    pass


FINGERPRINT = "a" * 64
NEXT_FAILURE = "b" * 64


def test_dispatch_is_a_distinct_immutable_state_and_only_a_fresh_failure_allows_retry() -> None:
    waiting = observe_capacity(None, FINGERPRINT, 1000.0, available=True)
    assert isinstance(waiting, Wait)
    assert isinstance(waiting.attempt, WaitingAttempt)
    assert waiting.attempt.due == pytest.approx(1030.0)
    dispatched = observe_capacity(waiting.attempt, FINGERPRINT, 1030.0, available=True)
    assert isinstance(dispatched, Dispatch)
    assert dispatched.attempt.attempts == 1
    for attempt in (dispatched.attempt, uncertain(dispatched.attempt), acknowledged(dispatched.attempt)):
        blocked = observe_capacity(attempt, FINGERPRINT, 1e10, available=True)
        assert isinstance(blocked, Block)
        assert isinstance(blocked.attempt, UncertainAttempt)
        fresh = observe_capacity(attempt, NEXT_FAILURE, 1e10, available=True)
        assert isinstance(fresh, Dispatch)
        assert fresh.attempt.attempts == 2
    with pytest.raises(ValidationError, match="frozen"):
        dispatched.attempt.due = 0.0


@pytest.mark.parametrize("phase", ["waiting", "dispatching", "sent", "uncertain", "exhausted"])
@pytest.mark.parametrize("count", [-1, 0, 1, 7, 8, 9])
def test_persisted_phase_count_combinations(phase: str, count: int) -> None:
    valid = (
        (phase == "waiting" and 0 <= count < 8)
        or (phase == "exhausted" and count == 8)
        or (phase in {"dispatching", "sent", "uncertain"} and 1 <= count <= 8)
    )
    payload = {"phase": phase, "attempts": count, "error": FINGERPRINT, "due": 1000.0}
    adapter = RetryPayload
    if valid:
        assert adapter.model_validate(payload).root.attempts == count
    else:
        with pytest.raises(ValidationError):
            adapter.model_validate(payload)


def test_cooldown_and_sweep_limit_never_reissue_unknown_input() -> None:
    sent = SentAttempt(error=FINGERPRINT, attempts=8, due=2000.0)
    assert isinstance(observe_capacity(sent, FINGERPRINT, 1999.0, available=True), Wait)
    assert isinstance(observe_capacity(sent, FINGERPRINT, 2000.0, available=True), Block)
    cooling = observe_capacity(sent, NEXT_FAILURE, 1999.0, available=True)
    assert isinstance(cooling, Cooldown)
    assert isinstance(cooling.attempt, ExhaustedAttempt)
    restarted = observe_capacity(cooling.attempt, NEXT_FAILURE, 2000.0, available=True)
    assert isinstance(restarted, Wait)
    assert restarted.attempt.attempts == 0
    assert isinstance(observe_capacity(restarted.attempt, NEXT_FAILURE, 2030.0, available=False), Wait)


def test_dispatch_result_preserves_exact_receipt() -> None:
    dispatched = DispatchingAttempt(error=FINGERPRINT, attempts=1, due=1000.0)
    assert acknowledged(dispatched).model_dump(exclude={"phase"}) == dispatched.model_dump(exclude={"phase"})
    assert uncertain(dispatched).model_dump(exclude={"phase"}) == dispatched.model_dump(exclude={"phase"})
