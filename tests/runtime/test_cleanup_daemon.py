from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from threading import Event, Thread
import time
from typing import TYPE_CHECKING, Literal, TypedDict, Unpack
from uuid import UUID

from long_running_agent.process import DEVNULL, spawn
from long_running_agent.runtime import cleanup, cleanup_capacity, cleanup_daemon, cleanup_state
from long_running_agent.runtime.health import Pressure
from long_running_agent.runtime.settings import RuntimePaths, private_write
from pydantic import JsonValue, RootModel
import pytest
from typer.testing import CliRunner


if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import Never

    from long_running_agent.process import ChildProcess


class WorktreePayload(TypedDict):
    path: str
    reason: str
    removed: bool
    attempted: bool


class ReportPayload(TypedDict):
    apply: bool
    max_removals: Literal[2]
    worktrees: list[WorktreePayload]
    cache: str


class SpawnOptions(TypedDict, total=False):
    stdin: int
    stdout: int
    stderr: int
    start_new_session: bool


GIB = 1024**3
pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="resident maintenance requires Linux process identities"
)


def process_has_exited(status: Path) -> bool:
    try:
        payload = status.read_text(encoding="utf-8")
    except FileNotFoundError, ProcessLookupError:
        return True
    return payload.rsplit(")", 1)[1].split()[0] == "Z"


def summary(*, removed: int = 0, reason: str = "referenced by saved workspace") -> cleanup_daemon.Summary:
    return cleanup_daemon.Summary(
        worktrees=1, attempted=removed, removed=removed, reasons={reason: 1}, cache="skipped: fixture guards"
    )


def worker_script(payload: str) -> list[str]:
    return [sys.executable, "-c", "import sys;sys.stdout.write(sys.argv[1])", payload]


def worktree_payload(
    *, path: str = "/fixture/worktree", reason: str = "retained", removed: bool = False, attempted: bool = False
) -> WorktreePayload:
    return WorktreePayload(path=path, reason=reason, removed=removed, attempted=attempted)


def worker_payload() -> ReportPayload:
    return ReportPayload(apply=True, max_removals=2, worktrees=[worktree_payload()], cache="skipped: fixture guards")


def report_payload() -> str:
    return json.dumps(worker_payload())


def heartbeat(
    *,
    phase: cleanup_daemon.Phase = "blocked",
    next_sweep_at: float = 2800,
    last_run_at: float | None = 900,
    failures: int = 2,
    result: cleanup_daemon.Summary | None = None,
) -> cleanup_daemon.Heartbeat:
    return cleanup_daemon.Heartbeat(
        version=1,
        pid=os.getpid(),
        phase=phase,
        checked_at=1000,
        next_sweep_at=next_sweep_at,
        space=cleanup_daemon.Space(total=100, available=10),
        last_run_at=last_run_at,
        last_result=result,
        consecutive_failures=failures,
    )


@pytest.fixture
def completed_sweep_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    def idle_pressure(_path: Path) -> Pressure:
        return Pressure(some=0, full=0)

    def unexpected_worker(_arguments: Sequence[str], **_options: Unpack[SpawnOptions]) -> Never:
        message = "A diagnostic fixture must explicitly select its private child"
        raise AssertionError(message)

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native PSI admission is controlled while every subprocess remains a private reporting fixture.
        cleanup, "pressure", idle_pressure
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Deny actual cleanup launches even if a guard regresses; native-child cases explicitly replace this boundary.
        cleanup_daemon, "spawn", unexpected_worker
    )
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(phase="cleaned", next_sweep_at=0, failures=0, result=summary(removed=1))
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    private_write(state / "cleanup-attempts.json", "[]\n")
    private_write(state / "cleanup-attempts-initialized", "1\n")
    return state


def test_low_capacity_sweeps_earlier_without_changing_retention_or_budget() -> None:
    schedule = cleanup_daemon.Schedule.new()
    phase = schedule.completed(summary(), cleanup_daemon.Space(total=300 * GIB, available=GIB), 100, 1000)
    assert phase == "retained"
    assert schedule.due == 100 + cleanup_daemon.LOW_SPACE_SECONDS
    schedule.completed(summary(), cleanup_daemon.Space(total=300 * GIB, available=275 * GIB), 200, 1100)
    assert schedule.due == 200 + cleanup_daemon.NORMAL_SECONDS


def test_repeated_failures_use_capped_backoff_and_success_resets_it() -> None:
    schedule = cleanup_daemon.Schedule.new()
    delays: list[float] = []
    for _ in range(20):
        schedule.failed(100)
        delays.append(schedule.due - 100)
    assert delays[0] == cleanup_daemon.PRESSURE_SECONDS
    assert delays[-1] == cleanup_daemon.NORMAL_SECONDS
    assert delays == sorted(delays)
    assert schedule.failures == cleanup_daemon.MAX_FAILURES
    schedule.completed(summary(), cleanup_daemon.Space(total=300 * GIB, available=275 * GIB), 200, 1100)
    assert schedule.failures == 0


@pytest.mark.parametrize(
    ("reason", "removed", "phase"),
    [
        pytest.param("live reference inspection failed; retained", 0, "blocked", id="unknown-live-reference"),
        pytest.param("rolling 24-hour native-removal budget exhausted; retained", 0, "budget", id="daily-budget"),
        pytest.param("referenced by saved workspace", 0, "retained", id="protected-workspace"),
        pytest.param("removed clean merged worktree; branch retained", 1, "cleaned", id="actual-native-removal"),
    ],
)
def test_status_reports_real_outcomes_without_claiming_reclaimed_bytes(reason: str, removed: int, phase: str) -> None:
    schedule = cleanup_daemon.Schedule.new()
    assert (
        schedule.completed(
            summary(removed=removed, reason=reason), cleanup_daemon.Space(total=100, available=10), 1, 10
        )
        == phase
    )


@pytest.mark.parametrize("removed", [0, 1], ids=["fully-deferred", "partially-cleaned"])
def test_pressure_retained_worktrees_use_the_short_retry_delay(removed: int) -> None:
    schedule = cleanup_daemon.Schedule.new()
    reasons = {"memory or disk pressure is high or unknown; retained": 1}
    if removed:
        reasons["removed clean merged worktree; branch retained"] = 1
    result = cleanup_daemon.Summary(
        worktrees=1 + removed,
        attempted=removed,
        removed=removed,
        reasons=reasons,
        cache="skipped: fixture guards",
    )
    phase = schedule.completed(result, cleanup_daemon.Space(total=300 * GIB, available=275 * GIB), 100, 1000)
    assert phase == ("cleaned" if removed else "pressure")
    assert schedule.due == 100 + cleanup_daemon.PRESSURE_SECONDS
    assert schedule.last_result == result


def test_cache_only_pressure_does_not_repeat_an_unneeded_worktree_sweep() -> None:
    result = cleanup_daemon.Summary(
        worktrees=1,
        attempted=0,
        removed=0,
        reasons={"referenced by saved workspace": 1},
        cache="skipped: memory or disk pressure is high or unknown",
    )
    schedule = cleanup_daemon.Schedule.new()
    assert (
        schedule.completed(result, cleanup_daemon.Space(total=300 * GIB, available=275 * GIB), 100, 1000) == "retained"
    )
    assert schedule.due == 100 + cleanup_daemon.NORMAL_SECONDS


@pytest.mark.parametrize(
    ("worktrees", "attempted", "removed", "reasons"),
    [
        pytest.param(1, 0, 1, {"removed": 1}, id="removal-without-attempt"),
        pytest.param(1, 2, 0, {"retained": 1}, id="more-attempts-than-worktrees"),
        pytest.param(1, 0, 0, {"retained": -1}, id="negative-reason-count"),
        pytest.param(1, 0, 0, {"retained": 0}, id="zero-reason-count"),
        pytest.param(1, 0, 0, {"retained": 2}, id="more-decisions-than-worktrees"),
    ],
)
def test_saved_summary_rejects_impossible_outcome_counts(
    worktrees: int, attempted: int, removed: int, reasons: dict[str, int]
) -> None:
    with pytest.raises(ValueError, match="validation error"):
        cleanup_daemon.Summary(
            worktrees=worktrees, attempted=attempted, removed=removed, reasons=reasons, cache="fixture"
        )


def test_legacy_truncated_summary_remains_readable_after_restart(tmp_path: Path) -> None:
    result = cleanup_daemon.Summary(
        worktrees=40, attempted=0, removed=0, reasons={"retained legacy category": 15}, cache="fixture"
    )
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(result=result)
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    restored = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert restored.last_result == result
    assert restored.due == 1900


def test_stopped_daemon_restart_waits_without_losing_outcome_or_budget(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    state = cleanup.state_directory(paths)
    now, wall = time.monotonic(), time.time()
    previous = heartbeat(phase="stopped", next_sweep_at=wall + 1800, failures=2, result=summary())
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    ledger, marker = state / "cleanup-attempts.json", state / "cleanup-attempts-initialized"
    private_write(ledger, "[]\n")
    private_write(marker, "1\n")
    before = ledger.read_bytes(), marker.read_bytes()
    restored = cleanup_daemon.Schedule.restore(state, now, wall)
    assert restored.phase == "waiting"
    assert restored.due == now + 1800
    assert restored.failures == previous.consecutive_failures
    assert restored.last_result == previous.last_result
    assert restored.last_run_at == previous.last_run_at
    cleanup_daemon.sweep(paths, state, restored, Event())
    published = cleanup_daemon.Heartbeat.model_validate_json((state / cleanup_daemon.STATUS_FILE).read_bytes())
    assert published.phase == "waiting"
    assert published.last_result == previous.last_result
    assert published.consecutive_failures == previous.consecutive_failures
    assert (ledger.read_bytes(), marker.read_bytes()) == before


def test_restart_restores_last_outcome_and_backoff(tmp_path: Path) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(result=summary())
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    schedule = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert schedule.due == 1900
    assert schedule.last_result == previous.last_result
    assert schedule.failures == 2


def test_interrupted_previous_worker_gets_a_conservative_restart_delay(tmp_path: Path) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(phase="running", next_sweep_at=1000, last_run_at=None, failures=0)
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    schedule = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert schedule.phase == "error"
    assert schedule.due >= 100 + cleanup_daemon.PRESSURE_SECONDS


def test_native_worker_parses_only_a_complete_consistent_report() -> None:
    result = cleanup_daemon.execute_worker(Event(), worker_script(report_payload()), timeout=10)
    assert result.worktrees == 1
    assert result.removed == result.attempted == 0
    assert result.cache == "skipped: fixture guards"


@pytest.mark.parametrize("payload", ["", "not-json", "{}"], ids=["empty", "non-json", "missing-fields"])
def test_malformed_worker_output_is_never_a_success(payload: str) -> None:
    with pytest.raises(ValueError, match="validation error"):
        cleanup_daemon.execute_worker(Event(), worker_script(payload), timeout=10)


def test_worker_nonzero_exit_does_not_publish_a_success() -> None:
    context = cleanup_daemon.FailureContext()
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="Cleanup worker failed") as caught:
        cleanup_daemon.execute_worker(
            Event(), [sys.executable, "-c", "raise SystemExit(3)"], timeout=10, context=context
        )
    assert caught.value.kind == "exit-status"
    assert context.stage == "worker-exit"


def test_worker_output_is_bounded_before_parent_memory_grows_without_limit() -> None:
    script = "import sys,time;sys.stdout.write('x' * int(sys.argv[1]));sys.stdout.flush();time.sleep(30)"
    context = cleanup_daemon.FailureContext()
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="output exceeded its bounded size") as caught:
        cleanup_daemon.execute_worker(
            Event(), [sys.executable, "-c", script, str(cleanup_daemon.REPORT_LIMIT + 1)], timeout=10, context=context
        )
    assert caught.value.kind == "output-limit"
    assert context.stage == "worker-read"


def test_inflight_stop_cancels_only_the_spawned_worker_and_preserves_another_child(tmp_path: Path) -> None:
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    stopped = Event()
    marker = tmp_path / "started"

    def stop_after_start() -> None:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        stopped.set()

    watcher = Thread(target=stop_after_start)
    watcher.start()
    script = "import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text('started');time.sleep(30)"
    try:
        with pytest.raises(InterruptedError, match="worker cancelled"):
            cleanup_daemon.execute_worker(stopped, [sys.executable, "-c", script, str(marker)], timeout=15)
        assert marker.exists()
        assert other.poll() is None
    finally:
        watcher.join(timeout=11)
        other.terminate()
        other.wait(timeout=5)


def test_stop_before_launch_does_not_start_a_fast_worker(tmp_path: Path) -> None:
    stopped = Event()
    stopped.set()
    marker = tmp_path / "should-not-exist"
    script = "import pathlib,sys;pathlib.Path(sys.argv[1]).write_text('started')"
    with pytest.raises(InterruptedError, match="cancelled before launch"):
        cleanup_daemon.execute_worker(stopped, [sys.executable, "-c", script, str(marker)])
    assert not marker.exists()


def test_group_escalation_kills_a_term_ignoring_descendant_after_leader_exit(tmp_path: Path) -> None:
    marker = tmp_path / "descendant"
    script = (
        "import os,pathlib,signal,sys,time\n"
        "if os.fork() == 0:\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " target=pathlib.Path(sys.argv[1]);pending=target.with_suffix('.pending')\n"
        " pending.write_text(str(os.getpid()));pending.replace(target)\n"
        "time.sleep(30)\n"
    )
    child = spawn(
        [sys.executable, "-c", script, str(marker)],
        stdin=DEVNULL,
        stdout=DEVNULL,
        stderr=DEVNULL,
        start_new_session=True,
    )
    other = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists()
        descendant = int(marker.read_text())
        cleanup_daemon.terminate_worker(child, poll_seconds=cleanup_daemon.CHILD_POLL_SECONDS)
        assert child.returncode == -signal.SIGTERM
        process = Path("/proc") / str(descendant) / "stat"
        deadline = time.monotonic() + 5
        while not process_has_exited(process) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert process_has_exited(process)
        assert other.poll() is None
    finally:
        cleanup_daemon.terminate_worker(child, poll_seconds=cleanup_daemon.CHILD_POLL_SECONDS)
        other.terminate()
        other.wait(timeout=5)


@pytest.mark.parametrize("mode", ["malformed", "nonzero", "success"])
def test_worker_exit_also_reaps_its_descendants_before_returning(tmp_path: Path, mode: str) -> None:
    marker = tmp_path / "descendant"
    script = (
        "import os,pathlib,signal,sys,time\n"
        "reader,writer=os.pipe()\n"
        "if os.fork() == 0:\n"
        " os.close(reader)\n"
        " signal.signal(signal.SIGTERM,signal.SIG_IGN)\n"
        " os.close(1)\n"
        " pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        " os.write(writer,b'1')\n"
        " os.close(writer)\n"
        " time.sleep(30)\n"
        "else:\n"
        " os.close(writer)\n"
        " os.read(reader,1)\n"
        " os.close(reader)\n"
        " print(sys.argv[2],flush=True)\n"
        " raise SystemExit(int(sys.argv[3]))\n"
    )
    payload = "not-json" if mode == "malformed" else report_payload()
    arguments = [sys.executable, "-c", script, str(marker), payload, "3" if mode == "nonzero" else "0"]
    match mode:
        case "success":
            assert cleanup_daemon.execute_worker(Event(), arguments, timeout=10).removed == 0
        case "malformed":
            with pytest.raises(ValueError, match="validation error"):
                cleanup_daemon.execute_worker(Event(), arguments, timeout=10)
        case _:
            with pytest.raises(RuntimeError, match="Cleanup worker failed"):
                cleanup_daemon.execute_worker(Event(), arguments, timeout=10)
    assert marker.exists()
    process = Path("/proc") / marker.read_text() / "stat"
    deadline = time.monotonic() + 5
    while not process_has_exited(process) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert process_has_exited(process)


@pytest.mark.parametrize(
    "mutation",
    ["report-only", "duplicate", "relative", "unattempted-removal", "over-budget"],
)
def test_inconsistent_native_worker_results_fail_closed(mutation: str) -> None:
    payload = worker_payload()
    row = payload["worktrees"][0]
    match mutation:
        case "report-only":
            payload["apply"] = False
        case "duplicate":
            payload["worktrees"].append(row.copy())
        case "relative":
            row["path"] = "relative"
        case "unattempted-removal":
            row["removed"] = True
        case _:
            payload["worktrees"] = [worktree_payload(path=f"/fixture/{index}", attempted=True) for index in range(3)]
    with pytest.raises(ValueError, match="inconsistent results"):
        cleanup_daemon.WorkerReport.model_validate_json(json.dumps(payload))


def test_summarized_reason_count_stays_bounded() -> None:
    payload = worker_payload()
    payload["worktrees"] = [worktree_payload(path=f"/fixture/{index}", reason=f"reason {index}") for index in range(40)]
    result = cleanup_daemon.summarize(cleanup_daemon.WorkerReport.model_validate_json(json.dumps(payload)))
    assert len(result.reasons) == cleanup_daemon.MAX_REASONS
    assert sum(result.reasons.values()) == 40
    assert result.worktrees == 40
    assert result.removed == 0


@pytest.mark.parametrize(
    ("reason", "phase"),
    [
        pytest.param("live reference inspection failed; retained", "blocked", id="rare-inspection-failure"),
        pytest.param("rolling 24-hour native-removal budget exhausted; retained", "budget", id="rare-budget"),
        pytest.param("memory or disk pressure is high or unknown; retained", "pressure", id="rare-pressure"),
    ],
)
def test_rare_blockers_survive_bounded_summary_and_control_retry(reason: str, phase: str) -> None:
    payload = worker_payload()
    payload["worktrees"] = [worktree_payload(path=f"/fixture/{index}", reason=f"reason {index}") for index in range(40)]
    payload["worktrees"].append(worktree_payload(path="/fixture/blocked", reason=reason))
    result = cleanup_daemon.summarize(cleanup_daemon.WorkerReport.model_validate_json(json.dumps(payload)))
    assert result.reasons[reason] == 1
    assert sum(result.reasons.values()) == result.worktrees == 41
    assert len(result.reasons) == cleanup_daemon.MAX_REASONS
    schedule = cleanup_daemon.Schedule.new()
    assert schedule.completed(result, cleanup_daemon.Space(total=100, available=50), 100, 1000) == phase


def test_status_write_failure_blocks_a_sweep_before_worker_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "not-a-directory"
    state.write_text("untouched")
    spawned: list[bool] = []

    def idle_pressure(_path: Path) -> Pressure:
        return Pressure(some=0, full=0)

    def unexpected_spawn(_arguments: Sequence[str], **_options: Unpack[SpawnOptions]) -> Never:
        spawned.append(True)
        message = "A worker was spawned before durable status"
        raise AssertionError(message)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- PSI is an OS boundary; host pressure must not bypass the failing filesystem preflight.
        cleanup, "pressure", idle_pressure
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe the native subprocess boundary without permitting actual user cleanup from a fixture.
        cleanup_daemon, "spawn", unexpected_spawn
    )
    with pytest.raises(OSError, match=r"Not a directory|File exists"):
        cleanup_daemon.sweep(RuntimePaths(tmp_path), state, cleanup_daemon.Schedule.new(), Event())
    assert not spawned
    assert state.read_text() == "untouched"


@pytest.mark.parametrize("stalls", [Pressure(some=2, full=0), Pressure(some=None, full=None)], ids=["busy", "unknown"])
def test_pressure_defers_a_due_sweep_without_spawning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stalls: Pressure
) -> None:
    def kernel_pressure(_path: Path) -> Pressure:
        return stalls

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply native kernel PSI samples to exercise the actual supervisor admission guard.
        cleanup, "pressure", kernel_pressure
    )
    paths = RuntimePaths(tmp_path)
    state = cleanup.state_directory(paths)
    schedule = cleanup_daemon.Schedule.new()
    before = time.monotonic()
    cleanup_daemon.sweep(paths, state, schedule, Event())
    heartbeat = cleanup_daemon.Heartbeat.model_validate_json((state / cleanup_daemon.STATUS_FILE).read_bytes())
    assert heartbeat.phase == "pressure"
    assert heartbeat.last_result is None
    assert schedule.due >= before + cleanup_daemon.PRESSURE_SECONDS
    assert heartbeat.last_run_at is None


def test_native_worker_deadline_is_bounded() -> None:
    start = time.monotonic()
    context = cleanup_daemon.FailureContext()
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="worker deadline exceeded") as caught:
        cleanup_daemon.execute_worker(
            Event(), [sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1, context=context
        )
    assert time.monotonic() - start < 10
    assert caught.value.kind == "deadline"
    assert context.stage == "worker-read"


def test_signal_handlers_are_restored_after_daemon_scope() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with cleanup_daemon.stop_requested() as stopped:
        os.kill(os.getpid(), signal.SIGTERM)
        assert stopped.is_set()
    assert signal.getsignal(signal.SIGTERM) == before


def test_parent_lock_does_not_block_the_cleanup_worker_but_excludes_another_daemon(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    with (
        cleanup.cleanup_lock(paths, daemon=True),
        cleanup.cleanup_lock(paths),
        pytest.raises(BlockingIOError, match=r"temporarily unavailable|would block"),
        cleanup.cleanup_lock(paths, daemon=True),
    ):
        pass


def test_resident_unit_has_worker_isolation_and_low_resource_limits() -> None:
    text = cleanup_daemon.service_text(Path("/home/a%user/tool path/bin/python"))
    assert 'ExecStart="/home/a%%user/tool path/bin/python" -m long_running_agent.cli runtime cleanup daemon run' in text
    for directive in (
        "Type=simple",
        "Nice=19",
        "IOSchedulingClass=idle",
        "CPUQuota=10%",
        "MemoryMax=256M",
        "MemorySwapMax=0",
        "TasksMax=32",
        "Environment=OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 UV_CONCURRENT_BUILDS=1",
        "Restart=always",
        "KillMode=mixed",
        "UMask=0077",
    ):
        assert directive in text
    assert "sudo" not in text


@pytest.mark.parametrize(
    ("available", "capacity_reached"),
    [
        pytest.param(76 * GIB, False, id="below-bootstrap-quarter"),
        pytest.param(75 * GIB, True, id="exact-bootstrap-quarter"),
        pytest.param(74 * GIB, True, id="above-bootstrap-quarter"),
    ],
)
def test_capacity_threshold_uses_the_bootstrap_quarter_target(available: int, capacity_reached: bool) -> None:
    assert cleanup_daemon.low_space(cleanup_daemon.Space(total=100 * GIB, available=available)) is capacity_reached


def test_capacity_latch_keeps_the_short_schedule_until_below_bootstrap_stop() -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.due = 10000
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=20 * GIB), 100)
    assert schedule.capacity_recovery
    assert schedule.due == 100 + cleanup_daemon.LOW_SPACE_SECONDS
    for now, available in ((160, 76 * GIB), (220, 80 * GIB)):
        schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available), now)
        assert schedule.capacity_recovery
    schedule.completed(summary(), cleanup_daemon.Space(total=100 * GIB, available=81 * GIB), 280, 1000)
    assert not schedule.capacity_recovery
    assert schedule.due == 280 + cleanup_daemon.NORMAL_SECONDS


@pytest.mark.parametrize(
    "available",
    [pytest.param(76 * GIB, id="just-below-bootstrap-trigger"), pytest.param(80 * GIB, id="exact-bootstrap-stop")],
)
def test_sustained_growth_enters_capacity_recovery_before_the_bootstrap_target(available: int) -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.due = 10000
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available + 2 * GIB), 100)
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available + GIB), 160)
    assert not schedule.capacity_recovery
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available), 220)
    assert schedule.capacity_recovery
    assert schedule.due == 220 + cleanup_daemon.LOW_SPACE_SECONDS


def test_growth_prediction_requires_at_least_two_minutes_of_observation() -> None:
    schedule = cleanup_daemon.Schedule.new()
    for now, available in ((100, 81 * GIB), (130, 80 * GIB), (160, 76 * GIB)):
        schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available), now)
    assert not schedule.capacity_recovery
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB - GIB // 2), 220)
    assert schedule.capacity_recovery


@pytest.mark.parametrize(
    ("available", "decrement"),
    [
        pytest.param(88 * GIB, GIB, id="growth-below-capacity-floor"),
        pytest.param(76 * GIB, 1024**2, id="target-more-than-an-hour-away"),
        pytest.param(76 * GIB, 512, id="metadata-sized-stat-noise"),
    ],
)
def test_growth_that_does_not_require_recovery_keeps_the_normal_schedule(available: int, decrement: int) -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.due = 10000
    for index in range(3):
        sample = available + (2 - index) * decrement
        schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=sample), 100 + index * 60)
    assert not schedule.capacity_recovery
    assert schedule.due == 10000


def test_a_single_growth_burst_does_not_predict_continuing_consumption() -> None:
    schedule = cleanup_daemon.Schedule.new()
    for now, available in ((100, 81 * GIB), (160, 76 * GIB), (220, 76 * GIB)):
        schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available), now)
    assert not schedule.capacity_recovery
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB - GIB // 2), 280)
    assert not schedule.capacity_recovery


@pytest.mark.parametrize(
    "reset_time",
    [
        pytest.param(150, id="backward-monotonic-sample"),
        pytest.param(160, id="duplicate-monotonic-sample"),
        pytest.param(461, id="gap-longer-than-five-minutes"),
    ],
)
def test_nonsequential_samples_reset_the_growth_forecast(reset_time: int) -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=81 * GIB), 100)
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=80 * GIB), 160)
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB), reset_time)
    assert not schedule.capacity_recovery
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB - GIB // 4), reset_time + 60)
    assert not schedule.capacity_recovery


def test_filesystem_resize_starts_a_new_growth_baseline() -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=81 * GIB), 100)
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=80 * GIB), 160)
    schedule.observe(cleanup_daemon.Space(total=101 * GIB, available=76 * GIB), 220)
    assert not schedule.capacity_recovery
    assert schedule.growth.started == schedule.growth.previous
    assert schedule.growth.positive_samples == 0


def test_capacity_polls_bound_error_backoff_without_postponing_an_earlier_sweep() -> None:
    space = cleanup_daemon.Space(total=100 * GIB, available=20 * GIB)
    schedule = cleanup_daemon.Schedule.new()
    for _ in range(4):
        schedule.failed(100)
    schedule.observe(space, 160)
    assert schedule.capacity_recovery
    assert schedule.due == 160 + cleanup_daemon.CAPACITY_FAILURE_SECONDS
    assert schedule.failures == 4
    schedule.completed(summary(), space, 200, 1000)
    due = 200 + cleanup_daemon.LOW_SPACE_SECONDS
    assert schedule.due == due
    for now in (260, 320, 380, 440):
        schedule.observe(space, now)
        assert schedule.due == due


def test_known_capacity_failures_keep_exponential_backoff_with_a_five_minute_cap() -> None:
    schedule = cleanup_daemon.Schedule.new()
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=2 * GIB), 100)
    for index in range(cleanup_daemon.MAX_FAILURES):
        schedule.failed(100)
        assert schedule.due == 100 + min(
            cleanup_daemon.PRESSURE_SECONDS * (1 << index), cleanup_daemon.CAPACITY_FAILURE_SECONDS
        )
    assert schedule.failures == cleanup_daemon.MAX_FAILURES
    assert schedule.phase == "error"


def test_capacity_observation_clamps_a_restored_failed_schedule_without_resetting_saved_state(tmp_path: Path) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(
        phase="error", failures=7, next_sweep_at=1000 + cleanup_daemon.NORMAL_SECONDS, result=summary()
    )
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    private_write(state / "cleanup-attempts.json", "[]\n")
    private_write(state / "cleanup-attempts-initialized", "1\n")
    before = {path.name: path.read_bytes() for path in state.iterdir()}
    restored = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert restored.due == 100 + cleanup_daemon.NORMAL_SECONDS
    restored.observe(cleanup_daemon.Space(total=100 * GIB, available=2 * GIB), 160)
    assert restored.due == 160 + cleanup_daemon.CAPACITY_FAILURE_SECONDS
    assert restored.failures == previous.consecutive_failures
    assert restored.last_result == previous.last_result
    assert restored.last_run_at == previous.last_run_at
    assert {path.name: path.read_bytes() for path in state.iterdir()} == before


def test_legacy_heartbeat_migration_resets_forecast_without_resetting_outcome_or_budget(tmp_path: Path) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(next_sweep_at=1000 + 6 * 3600, result=summary())
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    ledger, marker = state / "cleanup-attempts.json", state / "cleanup-attempts-initialized"
    private_write(ledger, "[]\n")
    private_write(marker, "1\n")
    before = ledger.read_bytes(), marker.read_bytes(), (state / cleanup_daemon.STATUS_FILE).read_bytes()
    restored = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert restored.due == 100 + cleanup_daemon.NORMAL_SECONDS
    assert restored.last_result == previous.last_result
    assert restored.last_run_at == previous.last_run_at
    assert restored.failures == previous.consecutive_failures
    assert not restored.capacity_recovery
    assert restored.growth.started is restored.growth.previous is None
    assert (ledger.read_bytes(), marker.read_bytes(), (state / cleanup_daemon.STATUS_FILE).read_bytes()) == before


@pytest.mark.parametrize(
    ("available", "previously_latched", "capacity_argument", "baseline_used"),
    [
        pytest.param(85 * GIB, False, False, None, id="normal-seven-day-worker"),
        pytest.param(75 * GIB, False, True, None, id="target-triggered-capacity-worker"),
        pytest.param(76 * GIB, True, True, None, id="hysteresis-keeps-capacity-worker"),
        pytest.param(81 * GIB, True, False, None, id="recovered-capacity-restores-normal-worker"),
        pytest.param(90 * GIB, False, True, 5, id="calibrated-volume-recovers-below-bootstrap"),
        pytest.param(99 * GIB, True, False, 5, id="calibrated-volume-stops-at-own-floor"),
    ],
)
def test_sweep_selects_guarded_capacity_worker_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: int,
    previously_latched: bool,
    capacity_argument: bool,
    baseline_used: int | None,
) -> None:
    calls: list[list[str]] = []
    current_available = available if baseline_used is None else (100 - baseline_used) * GIB

    def filesystem_capacity(_path: Path) -> os.statvfs_result:
        return os.statvfs_result(
            [1, 1, 100 * GIB, current_available, current_available, 0, 0, 0, 0, 255], {"f_fsid": 123}
        )

    def idle_pressure(_path: Path) -> Pressure:
        return Pressure(some=0, full=0)

    def report_worker(arguments: Sequence[str], **options: Unpack[SpawnOptions]) -> ChildProcess:
        calls.append(list(arguments))
        return spawn(worker_script(report_payload()), **options)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply filesystem capacity at the native statvfs boundary; do not depend on shared VM disk pressure.
        os, "statvfs", filesystem_capacity
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply known idle native PSI while exercising the actual supervisor admission path.
        cleanup, "pressure", idle_pressure
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only process creation with a real reporting child so this argv contract cannot invoke production cleanup.
        cleanup_daemon, "spawn", report_worker
    )
    paths = RuntimePaths(tmp_path)
    state = cleanup.state_directory(paths)
    if baseline_used is not None:
        with cleanup.cleanup_lock(paths):
            cleanup_capacity.calibrate(paths.home, apply=True)
    current_available = available
    schedule = cleanup_daemon.Schedule.new()
    schedule.capacity_recovery = previously_latched
    cleanup_daemon.sweep(paths, state, schedule, Event())
    assert len(calls) == 1
    assert calls[0][:7] == [sys.executable, "-m", "long_running_agent.cli", "runtime", "cleanup", "run", "--apply"]
    assert calls[0][7:9] == ["--min-age-days", "7"]
    assert ("--capacity-recovery" in calls[0]) is capacity_argument
    published = cleanup_daemon.Heartbeat.model_validate_json((state / cleanup_daemon.STATUS_FILE).read_bytes())
    assert published.version == 1
    assert published.last_result is not None
    assert published.last_result.attempted == published.last_result.removed == 0


def test_status_exposes_capacity_policy_without_changing_persisted_heartbeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(result=summary())
    path = state / cleanup_daemon.STATUS_FILE
    private_write(path, previous.model_dump_json())
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))

    def current_capacity(_path: Path) -> os.statvfs_result:
        return os.statvfs_result((1, 1, 100, 10, 10, 0, 0, 0, 0, 255))

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply fresh native capacity for the status view independently of its preserved historical heartbeat.
        os, "statvfs", current_capacity
    )
    result = CliRunner().invoke(cleanup_daemon.app, ["status"])
    assert result.exit_code == 0
    payload = RootModel[dict[str, JsonValue]].model_validate_json(result.stdout).root
    capacity = payload["capacity"]
    policy = payload["policy"]
    assert isinstance(capacity, dict)
    assert isinstance(policy, dict)
    assert capacity["used_percent"] == 90
    assert capacity["target_used_percent"] == 25
    assert capacity["recovery_used_percent"] == 20
    assert capacity["target_met"] is False
    assert policy["poll_seconds"] == 15
    assert policy["normal_sweep_seconds"] == 1800
    assert policy["capacity_sweep_seconds"] == 30
    assert policy["pressure_retry_seconds"] == 120
    assert policy["growth_horizon_seconds"] == 3600
    assert path.read_bytes() == before
    assert cleanup_daemon.Heartbeat.model_validate_json(path.read_bytes()) == previous


@pytest.mark.parametrize(
    ("mode", "stage", "kind"),
    [
        pytest.param("nonzero", "worker-exit", "exit-status", id="native-nonzero-exit"),
        pytest.param("signal", "worker-exit", "signal", id="native-signalled-exit"),
        pytest.param("invalid-json", "worker-report", "invalid-data", id="native-invalid-report"),
        pytest.param("missing", "worker-start", "not-found", id="native-executable-missing"),
    ],
)
@pytest.mark.skipif(os.getuid() == 0, reason="The resident CLI deliberately rejects root execution")
def test_failed_native_sweep_records_only_fixed_codes_and_keeps_last_completed_result(
    completed_sweep_state: Path, monkeypatch: pytest.MonkeyPatch, mode: str, stage: str, kind: str
) -> None:
    private_value = "private-fixture-argument-and-output"
    previous = cleanup_daemon.Heartbeat.model_validate_json(
        (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    )
    budget = (completed_sweep_state / "cleanup-attempts.json").read_bytes()

    def fixture_worker(_arguments: Sequence[str], **options: Unpack[SpawnOptions]) -> ChildProcess:
        script = "import os,signal,sys;sys.stderr.write(sys.argv[1]);"
        match mode:
            case "nonzero":
                script += "raise SystemExit(3)"
            case "signal":
                script += "os.kill(os.getpid(),signal.SIGKILL)"
            case "invalid-json":
                script += "sys.stdout.write(sys.argv[1])"
            case _:
                return spawn([str(completed_sweep_state / private_value)], **options)
        return spawn([sys.executable, "-c", script, private_value], **options)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Exercise the native supervisor with actual failing private children, never a production cleanup child.
        cleanup_daemon, "spawn", fixture_worker
    )
    result = CliRunner().invoke(cleanup_daemon.app, ["run", "--once"])
    assert result.exit_code == 1
    published = cleanup_daemon.Heartbeat.model_validate_json(
        (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    )
    failure_path = completed_sweep_state / cleanup_daemon.FAILURE_FILE
    failure = cleanup_daemon.Failure.model_validate_json(failure_path.read_bytes())
    assert (failure.stage, failure.kind) == (stage, kind)
    assert failure.at > 0
    assert failure_path.stat().st_mode & 0o777 == 0o600
    assert published.phase == "error"
    assert published.consecutive_failures == 1
    assert published.last_run_at == previous.last_run_at
    assert published.last_result == previous.last_result
    assert "last_failure" not in published.model_dump()
    assert private_value not in result.output + failure.model_dump_json()
    assert f"stage={stage}; kind={kind}" in result.output
    assert (completed_sweep_state / "cleanup-attempts.json").read_bytes() == budget
    assert cleanup_state.active_receipts(completed_sweep_state, cleanup_state.AttemptStamp.current()).initialized


def test_failure_details_survive_pressure_and_restart_and_resolve_without_wall_clock_comparison(
    completed_sweep_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def busy_pressure(_path: Path) -> Pressure:
        return Pressure(some=2, full=0)

    schedule = cleanup_daemon.Schedule.restore(completed_sweep_state, time.monotonic(), time.time())
    schedule.last_failure = cleanup_daemon.Failure(stage="worker-read", kind="deadline", at=800)
    schedule.failed(time.monotonic())
    cleanup_daemon.publish(completed_sweep_state, schedule, cleanup_daemon.Space(total=100, available=10), "error")
    restored = cleanup_daemon.Schedule.restore(completed_sweep_state, time.monotonic(), time.time())
    assert restored.last_failure == schedule.last_failure
    restored.due = 0
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A real pressure deferral must retain the independently persisted previous failure classification.
        cleanup, "pressure", busy_pressure
    )
    cleanup_daemon.sweep(RuntimePaths.current(), completed_sweep_state, restored, Event())
    status = CliRunner().invoke(cleanup_daemon.app, ["status"])
    assert status.exit_code == 0
    payload = RootModel[dict[str, JsonValue]].model_validate_json(status.stdout).root
    assert payload["latest_attempt_failed"] is True
    assert payload["last_completed_at"] == 900
    assert payload["attempt_in_progress"] is False
    failure = payload["last_recorded_failure"]
    assert isinstance(failure, dict)
    assert failure["kind"] == "deadline"
    assert restored.phase == "pressure"
    assert restored.failures == 1
    assert restored.last_failure == schedule.last_failure
    restored.completed(summary(), cleanup_daemon.Space(total=100, available=10), time.monotonic(), 700)
    cleanup_daemon.publish(
        completed_sweep_state, restored, cleanup_daemon.Space(total=100, available=10), restored.phase
    )
    status = CliRunner().invoke(cleanup_daemon.app, ["status"])
    payload = RootModel[dict[str, JsonValue]].model_validate_json(status.stdout).root
    assert payload["latest_attempt_failed"] is False
    assert payload["last_completed_at"] == 700
    assert payload["last_recorded_failure"] == failure


@pytest.mark.parametrize("sidecar", ["missing", "malformed", "unknown-code", "symlink"])
def test_optional_failure_diagnostics_preserve_old_heartbeat_and_never_expose_untrusted_values(
    completed_sweep_state: Path, sidecar: str
) -> None:
    previous = heartbeat(phase="pressure", failures=1, result=summary())
    path = completed_sweep_state / cleanup_daemon.STATUS_FILE
    private_write(path, previous.model_dump_json())
    private_value = "private-fixture-value"
    failure_path = completed_sweep_state / cleanup_daemon.FAILURE_FILE
    match sidecar:
        case "malformed":
            private_write(failure_path, private_value)
        case "unknown-code":
            private_write(
                failure_path,
                json.dumps({"version": 1, "stage": "worker-read", "kind": private_value, "at": 1000}),
            )
        case "symlink":
            target = completed_sweep_state / "unrelated-fixture-data"
            private_write(target, private_value)
            failure_path.symlink_to(target)
        case _:
            assert not failure_path.exists()
    before = path.read_bytes()
    restored = cleanup_daemon.Schedule.restore(completed_sweep_state, 100, 1000)
    assert restored.last_failure is None
    assert restored.failures == 1
    assert restored.last_result == previous.last_result
    status = CliRunner().invoke(cleanup_daemon.app, ["status"])
    assert status.exit_code == 0
    payload = RootModel[dict[str, JsonValue]].model_validate_json(status.stdout).root
    assert payload["last_recorded_failure"] is None
    assert payload["latest_attempt_failed"] is True
    assert private_value not in status.output
    assert path.read_bytes() == before


@pytest.mark.skipif(os.getuid() == 0, reason="The resident CLI deliberately rejects root execution")
def test_diagnostic_enospc_cannot_replace_the_original_native_worker_failure(
    completed_sweep_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spawned: list[int] = []

    def fixture_worker(_arguments: Sequence[str], **options: Unpack[SpawnOptions]) -> ChildProcess:
        child = spawn([sys.executable, "-c", "raise SystemExit(3)"], **options)
        spawned.append(child.pid)
        return child

    def no_diagnostic_space(path: Path, text: str) -> None:
        if path.name == cleanup_daemon.FAILURE_FILE:
            message = "private-fixture-diagnostic-error"
            raise OSError(errno.ENOSPC, message)
        private_write(path, text)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Run an actual private nonzero child through the supervisor's subprocess boundary.
        cleanup_daemon, "spawn", fixture_worker
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Inject ENOSPC only at the selected diagnostic file write; never fill or alter the shared filesystem.
        cleanup_daemon, "private_write", no_diagnostic_space
    )
    result = CliRunner().invoke(cleanup_daemon.app, ["run", "--once"])
    assert result.exit_code == 1
    assert "stage=worker-exit; kind=exit-status" in result.output
    assert "private-fixture-diagnostic-error" not in result.output
    published = cleanup_daemon.Heartbeat.model_validate_json(
        (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    )
    assert published.phase == "error"
    assert published.consecutive_failures == 1
    assert published.last_run_at == 900
    assert not (completed_sweep_state / cleanup_daemon.FAILURE_FILE).exists()
    assert len(spawned) == 1


@pytest.mark.skipif(os.getuid() == 0, reason="The resident CLI deliberately rejects root execution")
def test_heartbeat_enospc_is_classified_before_any_worker_can_start(
    completed_sweep_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failures: list[Path] = []

    def no_preflight_space(path: Path, text: str) -> None:
        if path.name == cleanup_daemon.STATUS_FILE and not failures:
            failures.append(path)
            message = "private-fixture-heartbeat-error"
            raise OSError(errno.ENOSPC, message)
        private_write(path, text)

    def unexpected_spawn(_arguments: Sequence[str], **_options: Unpack[SpawnOptions]) -> Never:
        message = "A worker must not start after heartbeat ENOSPC"
        raise AssertionError(message)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Fail only the native durable-preflight write; all fixture storage remains capped and real.
        cleanup_daemon, "private_write", no_preflight_space
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe the native launch boundary to prove failed preflight cannot execute cleanup.
        cleanup_daemon, "spawn", unexpected_spawn
    )
    result = CliRunner().invoke(cleanup_daemon.app, ["run", "--once"])
    assert result.exit_code == 1
    failure = cleanup_daemon.Failure.model_validate_json(
        (completed_sweep_state / cleanup_daemon.FAILURE_FILE).read_bytes()
    )
    assert (failure.stage, failure.kind) == ("heartbeat", "no-space")
    assert "private-fixture-heartbeat-error" not in result.output
    assert len(failures) == 1
    published = cleanup_daemon.Heartbeat.model_validate_json(
        (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    )
    assert published.consecutive_failures == 1
    assert published.last_run_at == 900


@pytest.mark.parametrize(
    ("failures", "retry_seconds"),
    [(1, 120), (2, 240), (3, 300)],
    ids=["first-two-minutes", "second-four-minutes", "third-five-minute-cap"],
)
def test_faster_capacity_cadence_never_shortens_an_existing_error_backoff(failures: int, retry_seconds: int) -> None:
    space = cleanup_daemon.Space(total=100 * GIB, available=10 * GIB)
    schedule = cleanup_daemon.Schedule.new()
    schedule.observe(space, 100)
    for _ in range(failures):
        schedule.failed(100)
    assert schedule.due == 100 + retry_seconds
    for now in (130, 160, 190):
        schedule.observe(space, now)
        assert schedule.due == 100 + retry_seconds
        assert schedule.failures == failures
        assert schedule.phase == "error"
    schedule.completed(summary(), space, 200, 1000)
    assert schedule.due == 230
    assert schedule.failures == 0
    assert schedule.phase == "retained"


@pytest.mark.parametrize("removed", [0, 1], ids=["pressure-only", "cleaned-then-pressure"])
def test_fast_recovery_polls_preserve_pressure_backoff_even_after_a_removal(removed: int) -> None:
    space = cleanup_daemon.Space(total=100 * GIB, available=5 * GIB)
    reasons = {"memory or disk pressure is high or unknown; retained": 1}
    if removed:
        reasons["removed clean merged worktree; branch retained"] = 1
    result = cleanup_daemon.Summary(
        worktrees=1 + removed, attempted=removed, removed=removed, reasons=reasons, cache="skipped: fixture guards"
    )
    schedule = cleanup_daemon.Schedule.new()
    schedule.completed(result, space, 100, 1000)
    assert schedule.due == 220
    for now in (115, 130, 160, 205):
        schedule.observe(space, now)
        assert schedule.due == 220
    schedule.completed(summary(removed=1), space, 220, 1120)
    assert schedule.due == 250


def test_fast_recovery_resumes_after_a_restart_without_replaying_a_completed_sweep(tmp_path: Path) -> None:
    previous = heartbeat(phase="cleaned", next_sweep_at=1030, failures=0, result=summary(removed=1))
    private_write(tmp_path / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    restored = cleanup_daemon.Schedule.restore(tmp_path, 100, 1015)
    restored.observe(previous.space, 100)
    assert restored.due == 115
    assert restored.last_run_at == previous.last_run_at
    assert restored.last_result == previous.last_result
    assert restored.failures == 0


@pytest.mark.parametrize("phase", ["pressure", "cleaned"])
def test_restart_with_pressure_does_not_turn_fast_capacity_polling_into_a_retry(
    tmp_path: Path, phase: cleanup_daemon.Phase
) -> None:
    previous = heartbeat(
        phase=phase,
        next_sweep_at=1120,
        failures=0,
        result=summary(reason="memory or disk pressure is high or unknown; retained"),
    )
    private_write(tmp_path / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    restored = cleanup_daemon.Schedule.restore(tmp_path, 100, 1015)
    restored.observe(previous.space, 100)
    assert restored.due == 205
    assert restored.last_result == previous.last_result


def test_status_distinguishes_deferred_and_failed_inspections_without_mutating_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    outcome = cleanup_daemon.Summary(
        worktrees=380,
        attempted=0,
        removed=0,
        reasons={cleanup.DEFERRED: 370, "live reference inspection failed; retained": 5, "local files": 5},
        cache="skipped: fixture guards",
    )
    path = state / cleanup_daemon.STATUS_FILE
    private_write(path, heartbeat(failures=0, result=outcome).model_dump_json())
    before = path.read_bytes()
    monkeypatch.setenv("HOME", str(tmp_path))
    result = CliRunner().invoke(cleanup_daemon.app, ["status"])
    assert result.exit_code == 0
    payload = RootModel[dict[str, JsonValue]].model_validate_json(result.stdout).root
    assert payload["deferred_worktrees"] == 370
    assert payload["inspection_failure_worktrees"] == 5
    assert payload["latest_attempt_failed"] is False
    policy = payload["policy"]
    assert isinstance(policy, dict)
    assert policy["capacity_daily_attempts"] == 256
    assert policy["normal_daily_attempts"] == 2
    assert policy["max_attempts_per_sweep"] == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize(("failures", "retry_seconds"), [(1, 120), (2, 240), (3, 300)])
def test_graceful_stop_preserves_error_backoff_across_restart_and_fast_polling(
    tmp_path: Path, failures: int, retry_seconds: int
) -> None:
    previous = heartbeat(
        phase="stopped",
        next_sweep_at=1000 + retry_seconds,
        failures=failures,
        result=summary(removed=1),
    )
    private_write(tmp_path / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    restored = cleanup_daemon.Schedule.restore(tmp_path, 100, 1000)
    for now in (100, 115, 130):
        restored.observe(previous.space, now)
        assert restored.due == 100 + retry_seconds
        assert restored.failures == failures
    assert restored.last_result == previous.last_result
    assert restored.last_run_at == previous.last_run_at
    restored.completed(summary(), previous.space, 500, 1400)
    assert restored.due == 530
    assert restored.failures == 0


@pytest.mark.parametrize("kind", ["entries", "directories", "deadline", "worktrees"])
def test_native_discovery_failure_preserves_prior_success_and_records_fixed_reason(
    completed_sweep_state: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    private_value = "private-inventory-fixture-stderr"
    payload = json.dumps({"version": 1, "stage": "repository-discovery", "kind": kind})
    previous = (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    budget = (completed_sweep_state / "cleanup-attempts.json").read_bytes()

    def failing_worker(_arguments: Sequence[str], **options: Unpack[SpawnOptions]) -> ChildProcess:
        script = "import sys;sys.stdout.write(sys.argv[1]);sys.stderr.write(sys.argv[2]);raise SystemExit(1)"
        return spawn([sys.executable, "-c", script, payload, private_value], **options)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A real private subprocess exercises the full supervisor, failure receipt, and prior-success preservation.
        cleanup_daemon, "spawn", failing_worker
    )
    result = CliRunner().invoke(cleanup_daemon.app, ["run", "--once"])
    assert result.exit_code == 1
    after = cleanup_daemon.Heartbeat.model_validate_json(
        (completed_sweep_state / cleanup_daemon.STATUS_FILE).read_bytes()
    )
    before = cleanup_daemon.Heartbeat.model_validate_json(previous)
    failure = cleanup_daemon.Failure.model_validate_json(
        (completed_sweep_state / cleanup_daemon.FAILURE_FILE).read_bytes()
    )
    assert after.last_result == before.last_result
    assert after.last_run_at == before.last_run_at
    assert after.consecutive_failures == 1
    assert failure.stage == "repository-discovery"
    assert failure.kind == "discovery-" + kind
    assert private_value not in result.output + failure.model_dump_json()
    assert (completed_sweep_state / "cleanup-attempts.json").read_bytes() == budget


@pytest.mark.parametrize(
    "payload",
    [
        '{"version":2,"stage":"repository-discovery","kind":"entries"}',
        '{"version":1,"stage":"unknown","kind":"entries"}',
        '{"version":1,"stage":"repository-discovery","kind":"unknown"}',
        '{"version":1,"stage":"repository-discovery","kind":"entries","detail":"private-path"}',
    ],
    ids=["unknown-version", "unknown-stage", "unknown-kind", "extra-private-detail"],
)
def test_nonzero_worker_cannot_inject_unknown_discovery_diagnostics(payload: str) -> None:
    context = cleanup_daemon.FailureContext()
    script = "import sys;sys.stdout.write(sys.argv[1]);raise SystemExit(1)"
    with pytest.raises(cleanup_daemon.WorkerInspectionError, match="Cleanup worker failed") as caught:
        cleanup_daemon.execute_worker(Event(), [sys.executable, "-c", script, payload], timeout=10, context=context)
    assert caught.value.kind == "exit-status"
    assert context.stage == "worker-exit"


def test_zero_exit_discovery_failure_packet_is_never_a_success() -> None:
    payload = '{"version":1,"stage":"repository-discovery","kind":"entries"}'
    with pytest.raises(ValueError, match="validation error"):
        cleanup_daemon.execute_worker(Event(), worker_script(payload), timeout=10)


def test_lower_target_shortens_a_saved_routine_budget_wait_without_resetting_receipts(tmp_path: Path) -> None:
    state = cleanup.state_directory(RuntimePaths(tmp_path))
    previous = heartbeat(
        phase="budget",
        next_sweep_at=2800,
        failures=0,
        result=summary(reason="rolling 24-hour native-removal budget exhausted; retained"),
    ).model_copy(update={"space": cleanup_daemon.Space(total=100, available=25)})
    private_write(state / cleanup_daemon.STATUS_FILE, previous.model_dump_json())
    stamp = cleanup_state.AttemptStamp(wall=900.0, uptime=900.0, boot_id=UUID(int=1))
    for index in range(2):
        assert cleanup_state.reserve_attempt(state, stamp, tmp_path / f"previous-{index}", "f" * 40)
    ledger = state / "cleanup-attempts.json"
    marker = state / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes(), (state / cleanup_daemon.STATUS_FILE).read_bytes()
    restored = cleanup_daemon.Schedule.restore(state, 100, 1000)
    assert restored.due == 100 + cleanup_daemon.NORMAL_SECONDS
    restored.observe(previous.space, 100)
    assert restored.capacity_recovery
    assert restored.due == 100 + cleanup_daemon.LOW_SPACE_SECONDS
    assert restored.last_result == previous.last_result
    assert restored.last_run_at == previous.last_run_at
    assert (ledger.read_bytes(), marker.read_bytes(), (state / cleanup_daemon.STATUS_FILE).read_bytes()) == before


@pytest.mark.parametrize("second_key", ["b" * 64, None], ids=["replacement-volume", "unknown-volume"])
def test_same_size_volume_change_resets_forecast_and_recovery_latch(second_key: str | None) -> None:
    thresholds = cleanup_capacity.bootstrap_thresholds(100 * GIB)
    first = cleanup_capacity.VolumeReading(100 * GIB, 76 * GIB, 76 * GIB, 4096, "a" * 64)
    second = cleanup_capacity.VolumeReading(100 * GIB, 76 * GIB, 76 * GIB, 4096, second_key)
    schedule = cleanup_daemon.Schedule.new()
    schedule.bind_volume(cleanup_capacity.Policy(first, thresholds))
    schedule.capacity_recovery = True
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB), 100)
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB - GIB // 2), 160)
    assert schedule.growth.positive_samples == 1
    previous_epoch = schedule.baseline_epoch
    schedule.bind_volume(cleanup_capacity.Policy(second, thresholds))
    assert not schedule.capacity_recovery
    assert schedule.growth.started is schedule.growth.previous is None
    assert schedule.baseline_epoch != previous_epoch
    schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=76 * GIB - GIB // 2), 220)
    assert not schedule.capacity_recovery


@pytest.mark.parametrize("interruption", ["failure", "pressure", "lock", "incomplete-removal", "folded-summary"])
def test_interrupted_observations_cannot_certify_a_lower_baseline(
    completed_sweep_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str
) -> None:
    available = 700 * GIB
    now = 0.0

    def volume_capacity(_path: Path) -> os.statvfs_result:
        return os.statvfs_result((1, 1, 1000 * GIB, available, available, 0, 0, 0, 0, 255), {"f_fsid": 123})

    def monotonic_clock() -> float:
        return now

    def busy_pressure(_path: Path) -> Pressure:
        return Pressure(some=100, full=100)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Keep native filesystem identity and allocation counters inside the private calibration fixture.
        os, "statvfs", volume_capacity
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance only the native monotonic clock to exercise a thirty-minute observation window without sleeping.
        time, "monotonic", monotonic_clock
    )
    paths = RuntimePaths(tmp_path)
    with cleanup.cleanup_lock(paths):
        cleanup_capacity.calibrate(paths.home, apply=True)
    available = 800 * GIB
    schedule = cleanup_daemon.Schedule.new()
    for instant in (0.0, 900.0):
        now = instant
        cleanup_daemon.observe_lower_baseline(paths, schedule, summary())
    previous = cleanup_capacity.load_calibration(paths.home)
    assert previous is not None
    assert len(previous.observations) == 2
    now = 901.0
    match interruption:
        case "failure":
            schedule.failed(now)
        case "pressure":
            monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native PSI denies admission; the fixture also forbids spawning an actual cleanup worker.
                cleanup, "pressure", busy_pressure
            )
            cleanup_daemon.sweep(paths, completed_sweep_state, schedule, Event())
            assert schedule.phase == "pressure"
        case "lock":
            with cleanup.cleanup_lock(paths):
                cleanup_daemon.observe_lower_baseline(paths, schedule, summary())
        case _:
            reason = "native removal did not complete; inspect checkout; branch retained"
            attempted = 1
            if interruption == "folded-summary":
                reason = "other retained decisions"
                attempted = 0
            incomplete = cleanup_daemon.Summary(
                worktrees=1, attempted=attempted, removed=0, reasons={reason: 1}, cache="skipped: fixture guards"
            )
            assert not cleanup_daemon.baseline_learning_eligible(incomplete)
            cleanup_daemon.observe_lower_baseline(paths, schedule, incomplete)
    now = 1800.0
    cleanup_daemon.observe_lower_baseline(paths, schedule, summary())
    current = cleanup_capacity.load_calibration(paths.home)
    assert current is not None
    assert current.allocated == previous.allocated == 300 * GIB
    assert current.generation == previous.generation
    assert len(current.observations) == 1


def test_unknown_identity_keeps_bootstrap_hysteresis_while_disabling_forecast() -> None:
    schedule = cleanup_daemon.Schedule.new()
    thresholds = cleanup_capacity.bootstrap_thresholds(100 * GIB)
    for now, available, recovering in ((100, 75 * GIB, True), (160, 76 * GIB, True), (220, 81 * GIB, False)):
        volume = cleanup_capacity.VolumeReading(100 * GIB, available, available, 4096, None)
        schedule.bind_volume(cleanup_capacity.Policy(volume, thresholds))
        schedule.observe(cleanup_daemon.Space(total=100 * GIB, available=available), now, thresholds=thresholds)
        assert schedule.capacity_recovery is recovering
        assert schedule.growth.positive_samples == 0
