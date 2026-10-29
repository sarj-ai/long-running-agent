from __future__ import annotations

from collections import Counter
from contextlib import contextmanager, suppress
from dataclasses import asdict, dataclass, field
import errno
import json
import os
from pathlib import Path
import secrets
import selectors
import signal
import sys
from threading import Event
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
import typer

from long_running_agent.process import DEVNULL, PIPE, ChildProcess, ProcessTimeout, run, spawn

from . import cleanup, cleanup_capacity
from .cleanup_state import CAPACITY_ATTEMPTS, MAX_ATTEMPTS, Timestamp, private_payload, valid_time
from .settings import RuntimePaths, private_write, systemd_value, user_environment


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence
    from types import FrameType


POLL_SECONDS: Final = 15
NORMAL_SECONDS: Final = 30 * 60
LOW_SPACE_SECONDS: Final = 30
CAPACITY_FAILURE_SECONDS: Final = 5 * 60
PRESSURE_SECONDS: Final = 2 * 60
GROWTH_HORIZON_SECONDS: Final = 60 * 60
GROWTH_MIN_SECONDS: Final = 2 * 60
GROWTH_MIN_SAMPLES: Final = 2
GROWTH_MAX_GAP_SECONDS: Final = 5 * 60
GROWTH_NOISE_BYTES: Final = 1024**2
WORKER_SECONDS: Final = 285
REPORT_LIMIT: Final = 4 * 1024 * 1024
READ_SIZE: Final = 65536
CHILD_POLL_SECONDS: Final = 0.05
MAX_FAILURES: Final = 8
MAX_REASONS: Final = 16
URGENT_DECISIONS: Final = (
    "inspection failed",
    "24-hour",
    "pressure is high or unknown",
    "native removal did not complete",
)
SERVICE: Final = "lra-cleanup-daemon.service"
STATUS_FILE: Final = "cleanup-daemon.json"
FAILURE_FILE: Final = "cleanup-daemon-failure.json"
Phase = Literal["waiting", "running", "pressure", "blocked", "budget", "retained", "cleaned", "error", "stopped"]
FailureStage = Literal[
    "repository-discovery",
    "capacity",
    "pressure",
    "heartbeat",
    "worker-start",
    "worker-read",
    "worker-exit",
    "worker-report",
    "worker-stop",
]
FailureKind = Literal[
    "discovery-entries",
    "discovery-directories",
    "discovery-deadline",
    "discovery-worktrees",
    "deadline",
    "output-limit",
    "output-unavailable",
    "exit-status",
    "signal",
    "exit-wait",
    "identity",
    "reap-timeout",
    "no-space",
    "quota",
    "permission",
    "memory",
    "file-limit",
    "io",
    "not-found",
    "os-error",
    "invalid-data",
    "runtime-error",
]
DISCOVERY_FAILURE_KINDS: Final[dict[cleanup.DiscoveryLimitKind, FailureKind]] = {
    "entries": "discovery-entries",
    "directories": "discovery-directories",
    "deadline": "discovery-deadline",
    "worktrees": "discovery-worktrees",
}
OS_FAILURE_KINDS: Final[dict[int, FailureKind]] = {
    errno.ENOSPC: "no-space",
    errno.EDQUOT: "quota",
    errno.EACCES: "permission",
    errno.EPERM: "permission",
    errno.ENOMEM: "memory",
    errno.EMFILE: "file-limit",
    errno.ENFILE: "file-limit",
    errno.EIO: "io",
    errno.ENOENT: "not-found",
}
app = typer.Typer(help="Continuously monitor capacity and schedule guarded low-priority cleanup.", no_args_is_help=True)


class WorkerInspectionError(RuntimeError):
    def __init__(self, message: str, *, kind: FailureKind) -> None:
        super().__init__(message)
        self.kind: FailureKind = kind


class Failure(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1] = 1
    stage: FailureStage
    kind: FailureKind
    at: Timestamp


@dataclass
class FailureContext:
    stage: FailureStage = "capacity"


def failure_kind(error: OSError | RuntimeError | ValueError) -> FailureKind:
    match error:
        case WorkerInspectionError():
            return error.kind
        case FileNotFoundError():
            return "not-found"
        case OSError():
            return OS_FAILURE_KINDS.get(error.errno or 0, "os-error")
        case ValueError():
            return "invalid-data"
        case _:
            return "runtime-error"


def load_failure(state: Path) -> Failure | None:
    try:
        return Failure.model_validate_json(private_payload(state / FAILURE_FILE))
    except OSError, RuntimeError, ValueError:
        return None


def persist_failure(state: Path, failure: Failure | None) -> None:
    if failure is not None:
        # Diagnostics cannot replace the worker failure or authorize another attempt when storage is unavailable.
        with suppress(OSError, RuntimeError, ValueError):
            private_write(state / FAILURE_FILE, failure.model_dump_json() + "\n")


class WorktreeResult(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    path: Annotated[str, Field(min_length=1, max_length=4096)]
    reason: Annotated[str, Field(min_length=1, max_length=512)]
    removed: bool
    attempted: bool


class WorkerReport(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    apply: bool
    max_removals: Literal[2]
    worktrees: Annotated[list[WorktreeResult], Field(max_length=512)]
    cache: Annotated[str, Field(max_length=512)]

    @model_validator(mode="after")
    def consistent_results(self) -> Self:
        paths = [row.path for row in self.worktrees]
        if (
            not self.apply
            or len(set(paths)) != len(paths)
            or any(not Path(path).is_absolute() for path in paths)
            or sum(row.attempted for row in self.worktrees) > self.max_removals
            or any(row.removed and not row.attempted for row in self.worktrees)
        ):
            message = "Cleanup worker returned inconsistent results"
            raise ValueError(message)
        return self


class Summary(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    worktrees: Annotated[int, Field(ge=0, le=512)]
    attempted: Annotated[int, Field(ge=0, le=2)]
    removed: Annotated[int, Field(ge=0, le=2)]
    reasons: Annotated[dict[str, Annotated[int, Field(ge=1)]], Field(max_length=MAX_REASONS)]
    cache: Annotated[str, Field(max_length=512)]

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if (
            self.removed > self.attempted
            or self.attempted > self.worktrees
            or sum(self.reasons.values()) > self.worktrees
        ):
            message = "Cleanup summary returned inconsistent counts"
            raise ValueError(message)
        return self


class Space(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    total: Annotated[int, Field(gt=0)]
    available: Annotated[int, Field(ge=0)]

    @model_validator(mode="after")
    def valid_capacity(self) -> Self:
        if self.available > self.total:
            message = "Filesystem capacity is unknown"
            raise ValueError(message)
        return self


class Heartbeat(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    version: Literal[1]
    pid: Annotated[int, Field(gt=0)]
    phase: Phase
    checked_at: Timestamp
    next_sweep_at: Timestamp
    space: Space
    last_run_at: Timestamp | None
    last_result: Summary | None
    consecutive_failures: Annotated[int, Field(ge=0, le=MAX_FAILURES)]


@dataclass(frozen=True)
class CapacitySample:
    space: Space
    checked_at: float

    def growing_from(self, previous: Self) -> bool:
        elapsed = self.checked_at - previous.checked_at
        return (
            self.space.total == previous.space.total
            and 0 < elapsed <= GROWTH_MAX_GAP_SECONDS
            and previous.space.available - self.space.available >= GROWTH_NOISE_BYTES
        )


@dataclass
class GrowthTrend:
    started: CapacitySample | None = None
    previous: CapacitySample | None = None
    positive_samples: int = 0
    target_enter: int | None = None

    def observe(self, space: Space, now: float, *, thresholds: cleanup_capacity.Thresholds | None = None) -> bool:
        thresholds = capacity_thresholds(space, thresholds)
        if self.target_enter != thresholds.enter:
            self.started = self.previous = None
            self.positive_samples = 0
        self.target_enter = thresholds.enter
        sample = CapacitySample(space, now)
        previous, started = self.previous, self.started
        if previous is None or started is None or not sample.growing_from(previous):
            self.started = self.previous = sample
            self.positive_samples = 0
            return False
        self.previous = sample
        self.positive_samples += 1
        elapsed = now - started.checked_at
        if self.positive_samples < GROWTH_MIN_SAMPLES or elapsed < GROWTH_MIN_SECONDS:
            return False
        distance = thresholds.enter - (space.total - space.available)
        consumed = started.space.available - space.available
        self.started = previous
        self.positive_samples = 1
        return distance * elapsed <= consumed * GROWTH_HORIZON_SECONDS


def new_baseline_epoch() -> str:
    return secrets.token_hex(16)


@dataclass
class Schedule:
    due: float
    failures: int
    last_result: Summary | None
    last_run_at: float | None
    phase: Phase
    capacity_recovery: bool = False
    growth: GrowthTrend = field(default_factory=GrowthTrend)
    last_failure: Failure | None = None
    context: FailureContext = field(default_factory=FailureContext)
    baseline_epoch: str = field(default_factory=new_baseline_epoch)
    policy_identity: tuple[str | None, int, int, int, int, int] | None = None

    @classmethod
    def new(cls) -> Self:
        return cls(0, 0, None, None, "waiting")

    @classmethod
    def restore(cls, state: Path, now: float, wall: float) -> Self:
        try:
            previous = Heartbeat.model_validate_json(private_payload(state / STATUS_FILE))
        except FileNotFoundError:
            return cls.new()
        delay = min(NORMAL_SECONDS, max(0, previous.next_sweep_at - wall))
        phase = previous.phase
        if phase == "running":
            phase = "error"
            delay = max(delay, PRESSURE_SECONDS)
        elif phase == "stopped":
            phase = "waiting"
        return cls(
            now + delay,
            previous.consecutive_failures,
            previous.last_result,
            previous.last_run_at,
            phase,
            last_failure=load_failure(state),
        )

    def bind_volume(self, current: cleanup_capacity.Policy) -> None:
        identity = (
            current.volume.key,
            current.volume.total,
            current.volume.reserved,
            current.volume.unit,
            current.thresholds.enter,
            current.thresholds.stop,
        )
        changed = self.policy_identity is not None and self.policy_identity != identity
        if current.volume.key is None or changed:
            self.growth = GrowthTrend()
        if changed:
            self.capacity_recovery = False
            self.baseline_epoch = new_baseline_epoch()
        self.policy_identity = identity

    def observe(self, space: Space, now: float, *, thresholds: cleanup_capacity.Thresholds | None = None) -> None:
        thresholds = capacity_thresholds(space, thresholds)
        predicted = self.growth.observe(space, now, thresholds=thresholds)
        if low_space(space, thresholds=thresholds):
            self.capacity_recovery = True
        elif space.total - space.available < thresholds.stop:
            self.capacity_recovery = False
        elif predicted:
            self.capacity_recovery = True
        if self.capacity_recovery:
            pressured = self.phase == "pressure" or (
                self.last_result is not None
                and any("pressure is high or unknown" in reason for reason in self.last_result.reasons)
            )
            maximum_delay = (
                CAPACITY_FAILURE_SECONDS
                if self.phase == "error" or self.failures
                else (PRESSURE_SECONDS if pressured else LOW_SPACE_SECONDS)
            )
            self.due = min(self.due, now + maximum_delay)

    def failed(self, now: float) -> None:
        self.baseline_epoch = new_baseline_epoch()
        self.failures = min(self.failures + 1, MAX_FAILURES)
        retry_seconds = PRESSURE_SECONDS * (1 << (self.failures - 1))
        maximum_retry = CAPACITY_FAILURE_SECONDS if self.capacity_recovery else NORMAL_SECONDS
        self.due = now + min(maximum_retry, retry_seconds)
        self.phase = "error"

    def completed(
        self,
        result: Summary,
        space: Space,
        now: float,
        wall: float,
        *,
        thresholds: cleanup_capacity.Thresholds | None = None,
    ) -> Phase:
        self.failures = 0
        self.last_result = result
        self.last_run_at = wall
        self.observe(space, now, thresholds=thresholds)
        pressured = any("pressure is high or unknown" in reason for reason in result.reasons)
        delay = PRESSURE_SECONDS if pressured else (LOW_SPACE_SECONDS if self.capacity_recovery else NORMAL_SECONDS)
        self.due = now + delay
        if result.removed:
            self.phase = "cleaned"
        elif any("inspection failed" in reason for reason in result.reasons):
            self.phase = "blocked"
        elif any("24-hour" in reason for reason in result.reasons):
            self.phase = "budget"
        elif pressured:
            self.phase = "pressure"
        else:
            self.phase = "retained"
        return self.phase


def capacity_thresholds(space: Space, thresholds: cleanup_capacity.Thresholds | None) -> cleanup_capacity.Thresholds:
    current = cleanup_capacity.bootstrap_thresholds(space.total) if thresholds is None else thresholds
    if current.total != space.total:
        message = "Maintenance thresholds belong to a different volume capacity"
        raise ValueError(message)
    return current


def low_space(space: Space, *, thresholds: cleanup_capacity.Thresholds | None = None) -> bool:
    return space.total - space.available >= capacity_thresholds(space, thresholds).enter


def capacity(home: Path) -> Space:
    current = cleanup_capacity.read_volume(home)
    return Space(total=current.total, available=current.available)


def baseline_learning_eligible(result: Summary) -> bool:
    incomplete = ("inspection", "deferred", "24-hour", "per-run", "pressure")
    eligible = (
        result.attempted == result.removed
        and "other retained decisions" not in result.reasons
        and sum(result.reasons.values()) == result.worktrees
    ) and not any(fragment in reason for reason in result.reasons for fragment in incomplete)
    return eligible and not any(
        fragment in result.cache for fragment in ("active", "busy", "unavailable", "pressure", "deferred")
    )


def observe_lower_baseline(paths: RuntimePaths, schedule: Schedule, result: Summary) -> None:
    # Calibration is independent of deletion authority; serialize its optional
    # downward observations with explicit calibration and actual cleanup workers.
    try:
        with cleanup.cleanup_lock(paths):
            cleanup_capacity.observe_lower(
                paths.home,
                now=time.monotonic(),
                epoch=schedule.baseline_epoch,
                eligible=baseline_learning_eligible(result),
            )
    except BlockingIOError:
        schedule.baseline_epoch = new_baseline_epoch()


def reason_priority(item: tuple[str, int]) -> int:
    return 0 if any(marker in item[0] for marker in URGENT_DECISIONS) else 1


def summarize(report: WorkerReport) -> Summary:
    reasons = Counter(row.reason for row in report.worktrees)
    prioritized = sorted(reasons.most_common(), key=reason_priority)
    retained = dict(prioritized[: MAX_REASONS - 1])
    remainder = sum(reasons.values()) - sum(retained.values())
    if remainder:
        retained["other retained decisions"] = remainder
    return Summary(
        worktrees=len(report.worktrees),
        attempted=sum(row.attempted for row in report.worktrees),
        removed=sum(row.removed for row in report.worktrees),
        reasons=retained,
        cache=report.cache,
    )


def terminate_worker(child: ChildProcess, *, poll_seconds: float) -> None:
    # Do not poll/reap the session leader before group cleanup: its PID must not be reusable.
    if child.returncode is not None:
        return
    try:
        os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        message = "Cleanup worker identity could not be verified; no process group was signalled"
        raise WorkerInspectionError(message, kind="identity") from None
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
            break
        time.sleep(poll_seconds)
    with suppress(ProcessLookupError):
        os.killpg(child.pid, signal.SIGKILL)
    try:
        child.wait(timeout=5)
    except ProcessTimeout:
        message = "Cleanup worker could not be reaped after bounded group termination"
        raise WorkerInspectionError(message, kind="reap-timeout") from None


def successful_exit(child: ChildProcess, stopped: Event, *, poll_seconds: float) -> bool:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if stopped.is_set():
            message = "Cleanup worker cancelled; inspect reserved attempts"
            raise InterruptedError(message)
        result = os.waitid(os.P_PID, child.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        if result is not None:
            if result.si_code != os.CLD_EXITED:
                message = "Cleanup worker failed; inspect reserved attempts"
                raise WorkerInspectionError(message, kind="signal")
            return result.si_status == 0
        time.sleep(poll_seconds)
    message = "Cleanup worker did not finish; inspect reserved attempts"
    raise WorkerInspectionError(message, kind="exit-wait")


def read_report(child: ChildProcess, stopped: Event, timeout: float) -> bytes:
    if child.stdout is None:
        message = "Cleanup worker output is unavailable"
        raise WorkerInspectionError(message, kind="output-unavailable")
    deadline = time.monotonic() + timeout
    output = bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout, selectors.EVENT_READ)
        while time.monotonic() < deadline:
            if stopped.is_set():
                message = "Cleanup worker cancelled; inspect reserved attempts"
                raise InterruptedError(message)
            for _key, _events in selector.select(timeout=0.5):
                chunk = os.read(child.stdout.fileno(), READ_SIZE)
                if not chunk:
                    return bytes(output)
                if len(output) + len(chunk) > REPORT_LIMIT:
                    message = "Cleanup worker output exceeded its bounded size"
                    raise WorkerInspectionError(message, kind="output-limit")
                output.extend(chunk)
    message = "Cleanup worker deadline exceeded; inspect reserved attempts"
    raise WorkerInspectionError(message, kind="deadline")


def execute_worker(
    stopped: Event, arguments: Sequence[str], *, timeout: float = WORKER_SECONDS, context: FailureContext | None = None
) -> Summary:
    if not valid_time(timeout) or timeout == 0:
        message = "Cleanup worker needs a positive finite deadline"
        raise ValueError(message)
    if stopped.is_set():
        message = "Cleanup worker cancelled before launch"
        raise InterruptedError(message)
    current = FailureContext() if context is None else context
    current.stage = "worker-start"
    child = spawn(arguments, stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL, start_new_session=True)
    try:
        current.stage = "worker-read"
        output = read_report(child, stopped, timeout)
        current.stage = "worker-exit"
        if not successful_exit(child, stopped, poll_seconds=CHILD_POLL_SECONDS):
            try:
                failure = cleanup.InventoryFailure.model_validate_json(output)
            except ValueError:
                failure = None
            if failure is not None:
                current.stage = "repository-discovery"
                message = "Cleanup worker failed; repository discovery is incomplete"
                raise WorkerInspectionError(message, kind=DISCOVERY_FAILURE_KINDS[failure.kind])
            message = "Cleanup worker failed; inspect reserved attempts"
            raise WorkerInspectionError(message, kind="exit-status")
        current.stage = "worker-report"
        return summarize(WorkerReport.model_validate_json(output))
    finally:
        previous_stage = current.stage
        current.stage = "worker-stop"
        try:
            terminate_worker(child, poll_seconds=CHILD_POLL_SECONDS)
        finally:
            if child.stdout is not None:
                child.stdout.close()
        current.stage = previous_stage


@contextmanager
def stop_requested() -> Generator[Event]:
    stopped = Event()

    def request_stop(_number: int, _frame: FrameType | None) -> None:
        stopped.set()

    originals = {number: signal.signal(number, request_stop) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield stopped
    finally:
        for number, handler in originals.items():
            signal.signal(number, handler)


def publish(state: Path, schedule: Schedule, space: Space, phase: Phase) -> None:
    wall = time.time()
    heartbeat = Heartbeat(
        version=1,
        pid=os.getpid(),
        phase=phase,
        checked_at=wall,
        next_sweep_at=wall + max(0, schedule.due - time.monotonic()),
        space=space,
        last_run_at=schedule.last_run_at,
        last_result=schedule.last_result,
        consecutive_failures=schedule.failures,
    )
    private_write(state / STATUS_FILE, heartbeat.model_dump_json() + "\n")
    persist_failure(state, schedule.last_failure)


def record_failure(state: Path, schedule: Schedule, error: OSError | RuntimeError | ValueError) -> None:
    failure = Failure(stage=schedule.context.stage, kind=failure_kind(error), at=time.time())
    schedule.last_failure = failure
    persist_failure(state, failure)
    with suppress(OSError, RuntimeError, ValueError):
        typer.echo(f"Maintenance attempt failed: stage={failure.stage}; kind={failure.kind}.", err=True)


def sweep(paths: RuntimePaths, state: Path, schedule: Schedule, stopped: Event) -> None:
    schedule.context.stage = "capacity"
    current = cleanup_capacity.policy(paths.home)
    space = Space(total=current.volume.total, available=current.volume.available)
    now = time.monotonic()
    phase = schedule.phase
    schedule.bind_volume(current)
    schedule.observe(space, now, thresholds=current.thresholds)
    if now >= schedule.due:
        schedule.context.stage = "pressure"
        if cleanup.maintenance_idle():
            # A durable preflight status is required before any potentially destructive worker.
            schedule.context.stage = "heartbeat"
            publish(state, schedule, space, "running")
            arguments = [
                sys.executable,
                "-m",
                "long_running_agent.cli",
                "runtime",
                "cleanup",
                "run",
                "--apply",
                "--min-age-days",
                "7",
            ]
            if schedule.capacity_recovery:
                arguments.append("--capacity-recovery")
            result = execute_worker(stopped, arguments, context=schedule.context)
            schedule.context.stage = "capacity"
            current = cleanup_capacity.policy(paths.home)
            space = Space(total=current.volume.total, available=current.volume.available)
            schedule.bind_volume(current)
            phase = schedule.completed(result, space, time.monotonic(), time.time(), thresholds=current.thresholds)
            observe_lower_baseline(paths, schedule, result)
            current = cleanup_capacity.policy(paths.home)
            space = Space(total=current.volume.total, available=current.volume.available)
            schedule.bind_volume(current)
            schedule.observe(space, time.monotonic(), thresholds=current.thresholds)
        else:
            schedule.baseline_epoch = new_baseline_epoch()
            schedule.due = now + PRESSURE_SECONDS
            phase = "pressure"
            schedule.phase = phase
    schedule.context.stage = "heartbeat"
    publish(state, schedule, space, phase)


def supervise(paths: RuntimePaths, *, once: bool) -> None:
    with cleanup.cleanup_lock(paths, daemon=True) as state, stop_requested() as stopped:
        schedule = Schedule.restore(state, time.monotonic(), time.time())
        while not stopped.is_set():
            failed = False
            try:
                sweep(paths, state, schedule, stopped)
            except InterruptedError:
                break
            except (OSError, RuntimeError, ValueError) as error:
                failed = True
                schedule.failed(time.monotonic())
                record_failure(state, schedule, error)
                try:
                    publish(state, schedule, capacity(paths.home), "error")
                except OSError, RuntimeError, ValueError:
                    if schedule.failures == 1:
                        typer.echo("Maintenance paused: private status could not be persisted.", err=True)
            if once:
                if failed:
                    raise typer.Exit(code=1)
                return
            stopped.wait(POLL_SECONDS)
        try:
            publish(state, schedule, capacity(paths.home), "stopped")
        except OSError, RuntimeError, ValueError:
            typer.echo("Maintenance stopped; its last private status may be stale.", err=True)


@app.command("run")
def daemon_run(*, once: bool = False) -> None:
    """Remain resident with adaptive sweeps, or perform one bounded supervisor iteration."""
    if os.getuid() == 0 or not Path("/proc/self").exists():
        message = "Run maintenance as your own Linux user, never with sudo"
        raise RuntimeError(message)
    supervise(RuntimePaths.current(), once=once)


@app.command("status")
def status() -> None:
    """Show private capacity, last real outcome, and heartbeat freshness."""
    path = cleanup.state_directory(RuntimePaths.current()) / STATUS_FILE
    try:
        heartbeat = Heartbeat.model_validate_json(private_payload(path))
    except FileNotFoundError:
        typer.echo(json.dumps({"state": "not started"}))
        return
    age = time.time() - heartbeat.checked_at
    allowance = WORKER_SECONDS + 2 * POLL_SECONDS if heartbeat.phase == "running" else 2 * POLL_SECONDS
    current = cleanup_capacity.policy(RuntimePaths.current().home)
    space = Space(total=current.volume.total, available=current.volume.available)
    failure = load_failure(path.parent)
    typer.echo(
        json.dumps(
            {
                "heartbeat": heartbeat.model_dump(mode="json"),
                "fresh": 0 <= age <= allowance,
                "last_completed_at": heartbeat.last_run_at,
                "latest_attempt_failed": heartbeat.consecutive_failures > 0,
                "attempt_in_progress": heartbeat.phase == "running",
                "last_recorded_failure": None if failure is None else failure.model_dump(mode="json"),
                "deferred_worktrees": (
                    0 if heartbeat.last_result is None else heartbeat.last_result.reasons.get(cleanup.DEFERRED, 0)
                ),
                "inspection_failure_worktrees": (
                    0
                    if heartbeat.last_result is None
                    else sum(
                        count
                        for reason, count in heartbeat.last_result.reasons.items()
                        if "inspection failed" in reason
                    )
                ),
                "capacity": {
                    "used_percent": round(100 * (space.total - space.available) / space.total, 2),
                    "target_used_percent": round(100 * current.thresholds.enter / space.total, 3),
                    "recovery_used_percent": round(100 * current.thresholds.stop / space.total, 3),
                    "target_met": space.total - space.available < current.thresholds.enter,
                    "baseline": asdict(current.thresholds),
                },
                "policy": {
                    "poll_seconds": POLL_SECONDS,
                    "normal_sweep_seconds": NORMAL_SECONDS,
                    "capacity_sweep_seconds": LOW_SPACE_SECONDS,
                    "pressure_retry_seconds": PRESSURE_SECONDS,
                    "growth_horizon_seconds": GROWTH_HORIZON_SECONDS,
                    "normal_daily_attempts": MAX_ATTEMPTS,
                    "capacity_daily_attempts": CAPACITY_ATTEMPTS,
                    "max_attempts_per_sweep": cleanup.MAX_REMOVALS,
                },
            }
        )
    )


def service_text(executable: Path) -> str:
    return (
        "[Unit]\nDescription=Continuous guarded developer disk maintenance\nStartLimitIntervalSec=0\n"
        "[Service]\nType=simple\nUMask=0077\nNice=19\nIOSchedulingClass=idle\nNoNewPrivileges=true\n"
        "Environment=OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 UV_CONCURRENT_BUILDS=1\n"
        "CPUQuota=10%\nMemoryMax=256M\nMemorySwapMax=0\nTasksMax=32\nRestart=always\nRestartSec=60\nOOMPolicy=stop\n"
        "KillMode=mixed\nTimeoutStopSec=15\nLogRateLimitIntervalSec=1h\nLogRateLimitBurst=10\n"
        f"ExecStart={systemd_value(str(executable))} -m long_running_agent.cli runtime cleanup daemon run\n"
        "[Install]\nWantedBy=default.target\n"
    )


@app.command("enable")
def enable() -> None:
    """Enable the resident user daemon and retire only the previous cleanup timer."""
    if os.getuid() == 0 or not Path("/proc/self").exists():
        message = "Enable maintenance as your own Linux user, never with sudo"
        raise RuntimeError(message)
    paths = RuntimePaths.current()
    executable = Path(sys.executable)
    if not executable.is_relative_to(paths.home / ".local/share"):
        message = "Enable maintenance with the durable installed lra runtime"
        raise RuntimeError(message)
    folder = paths.home / ".config/systemd/user"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not cleanup.owned_path(folder, paths.home):
        message = "User unit directory must be private and owned by your user"
        raise RuntimeError(message)
    private_write(folder / SERVICE, service_text(executable))
    environment = user_environment(paths)
    run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15, env=environment)
    run(["systemctl", "--user", "enable", "--now", SERVICE], capture_output=True, timeout=15, env=environment)
    run(["systemctl", "--user", "is-active", "--quiet", SERVICE], capture_output=True, timeout=10, env=environment)
    timer = run(
        ["systemctl", "--user", "show", cleanup.TIMER, "--property=LoadState", "--value"],
        capture_output=True,
        timeout=10,
        env=environment,
    ).stdout.strip()
    if timer == "loaded":
        run(
            ["systemctl", "--user", "disable", "--now", cleanup.TIMER], capture_output=True, timeout=15, env=environment
        )
    elif timer != "not-found":
        message = "Previous cleanup timer state is unknown; inspect it before migration"
        raise RuntimeError(message)
    typer.echo(
        "Continuous guarded cleanup enabled: 15-second capacity checks, 30-minute sweeps, "
        "and guarded 30-second recovery toward this volume's clean-footprint target."
    )
