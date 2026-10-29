from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import contextmanager, suppress
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import signal
import stat
import sys
from threading import Event
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal
from uuid import uuid7

from pydantic import BaseModel, ConfigDict, Field, ValidationError
import typer

from long_running_agent.process import run

from . import codex_events, codex_panes
from .settings import RuntimePaths, private_write, systemd_value, user_environment


if TYPE_CHECKING:
    from collections.abc import Generator


SERVICE: Final = "lra-codex-recovery.service"
INTERVAL: Final = 15
STALE_SECONDS: Final = 120
INITIAL_DELAY: Final = 30
ACK_SECONDS: Final = 2
MAX_ATTEMPTS: Final = 8
MAX_DELAY: Final = 900
COOLDOWN: Final = 3600
SGR_DIM: Final = 2
SGR_RGB: Final = 2
PAGE_SIZE: Final = 16
MAX_BYTES: Final = 1_048_576
ANSI: Final = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
CAPACITY: Final = re.compile(r"(?:■|⚠|Error:)\s*(" + codex_events.CAPACITY_MESSAGE.pattern + ")", re.IGNORECASE)

BLOCKED: Final = re.compile(
    r"esc to interrupt|(?:^|\n)\s*[•●]\s*(?:working|thinking)|approve this|trust this|sign in|device code|press enter to",
    re.IGNORECASE,
)
app = typer.Typer(
    help="Automatically continue Codex panes after temporary model-capacity failures.", no_args_is_help=True
)


class RecoveryError(RuntimeError):
    pass


class RecoveryBusyError(RecoveryError):
    pass


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)


class Attempt(Record):
    error: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempts: int = Field(default=0, ge=0, le=MAX_ATTEMPTS)
    due: float = Field(ge=0, allow_inf_nan=False)
    phase: Literal["waiting", "dispatching", "sent", "uncertain", "exhausted"] = "waiting"


class Ledger(Record):
    version: Literal[1] = 1
    cursor: int = Field(default=0, ge=0)
    attempts: dict[str, Attempt] = Field(default_factory=dict, max_length=2048)


class PaneStatus(Record):
    session: str
    pane_id: int = Field(ge=0)
    phase: Literal["waiting", "dispatching", "sent", "uncertain", "exhausted", "preview"]
    attempts: int = Field(ge=0, le=MAX_ATTEMPTS)
    due: float = Field(ge=0, allow_inf_nan=False)


class Sweep(Record):
    observed_at: float = Field(ge=0, allow_inf_nan=False)
    checked: int = 0
    capacity: int = 0
    waiting: int = 0
    continued: int = 0
    attempted: int = 0
    blocked: int = 0
    uncertain: int = 0
    exhausted: int = 0
    preview: bool = True
    panes: list[PaneStatus] = Field(default_factory=list, max_length=PAGE_SIZE)


class WatcherStatus(Record):
    service: str
    sweep: Sweep | None


@dataclass(frozen=True)
class Screen:
    plain: str
    composer: int | None
    empty: bool


def directory(home: Path) -> Path:
    current = home
    for part in (".local", "state", "long-running-agent", "codex"):
        current /= part
        current.mkdir(mode=0o700, exist_ok=True)
        metadata = current.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid() or metadata.st_mode & 0o022:
            message = "Codex recovery state requires owned directories without shared write access"
            raise RecoveryError(message)
    if current.stat().st_mode & 0o077:
        message = "Codex recovery state must be private to this Linux user"
        raise RecoveryError(message)
    return current


def read_record(path: Path, model: type[Ledger | Sweep]) -> Ledger | Sweep:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_BYTES
        ):
            message = "Codex recovery metadata is not a private bounded record"
            raise RecoveryError(message)
        try:
            return model.model_validate_json(stream.read(MAX_BYTES + 1))
        except ValidationError:
            message = "Codex recovery metadata is invalid; no automatic input was authorized"
            raise RecoveryError(message) from None


@contextmanager
def guard(home: Path, name: str = "operation.lock") -> Generator[Path]:
    root = directory(home)
    descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(descriptor, "a") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o077
            or metadata.st_nlink != 1
        ):
            message = "Codex recovery lock must be a private owned file"
            raise RecoveryError(message)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            message = "Codex account switching or recovery is already active"
            raise RecoveryBusyError(message) from None
        yield root


def ledger(root: Path) -> Ledger:
    try:
        result = read_record(root / "retries.json", Ledger)
    except FileNotFoundError:
        return Ledger()
    if not isinstance(result, Ledger):
        message = "Unexpected Codex recovery record"
        raise RecoveryError(message)
    for attempt in result.attempts.values():
        if attempt.phase == "dispatching":
            attempt.phase = "uncertain"
    return result


def save(root: Path, state: Ledger) -> None:
    private_write(root / "retries.json", state.model_dump_json())


def sgr_dim(escape: str, *, previous: bool) -> bool:
    codes = [int(value or "0") for value in escape[2:-1].split(";")]
    dim = previous
    index = 0
    while index < len(codes):
        code = codes[index]
        if code in {38, 48, 58} and index + 1 < len(codes):
            index += 5 if codes[index + 1] == SGR_RGB else 3
            continue
        if code in {0, 22}:
            dim = False
        elif code == SGR_DIM:
            dim = True
        index += 1
    return dim


def empty_composer(raw: str) -> bool:
    # Empty native placeholders are dim; RGB channel values are not attributes.
    dim = False
    after_marker = False
    for part in re.split(r"(\x1b\[[0-9;]*m)", raw):
        if part.startswith("\x1b[") and part.endswith("m"):
            dim = sgr_dim(part, previous=dim)
            continue
        content = part
        if not after_marker:
            content = part.lstrip()
            if not content:
                continue
            if content[0] not in {"›", "❯", "»"}:
                return False
            after_marker = True
            content = content[1:]
        if content.strip() and not dim:
            return False
    return after_marker


def screen_view(raw: str) -> Screen:
    plain = ANSI.sub("", raw)
    lines = plain.splitlines()
    composers = [index for index, line in enumerate(lines) if line.strip().startswith(("›", "❯", "»"))]
    if not composers:
        return Screen(plain, None, empty=False)
    composer = composers[-1]
    return Screen(plain, composer, empty_composer(raw.splitlines()[composer]))


def capacity_error(raw: str) -> str | None:
    if len(raw.encode()) > MAX_BYTES:
        return None
    view = screen_view(raw)
    if view.composer is None or not view.empty or BLOCKED.search(view.plain):
        return None
    lines = view.plain.splitlines()
    before = lines[max(0, view.composer - 40) : view.composer]
    for index, line in enumerate(before):
        if not line.lstrip().startswith(("■", "⚠", "Error:")):
            continue
        paragraph = " ".join(part.strip() for part in before[index:] if part.strip())
        if CAPACITY.fullmatch(paragraph):
            return hashlib.sha256(paragraph.casefold().encode()).hexdigest()
    return None


def successful_idle(raw: str) -> bool:
    view = screen_view(raw)
    return (
        view.empty
        and not BLOCKED.search(view.plain)
        and not re.search(r"at capacity|(?:■|⚠|Error:)", view.plain, re.IGNORECASE)
    )


def inspect_screen(target: codex_panes.PaneTarget) -> str:
    result = run(
        ["zellij", "--session", target.session, "action", "dump-screen", "--ansi", "--pane-id", str(target.pane_id)],
        capture_output=True,
        timeout=2,
    )
    if len(result.stdout.encode()) > MAX_BYTES:
        message = "Codex viewport exceeded the recovery inspection limit"
        raise RecoveryError(message)
    return result.stdout


@dataclass(frozen=True)
class Inventory:
    panes: list[codex_panes.PaneTarget]
    blocked: int


def targets(*, proc: Path) -> Inventory:
    inventory = codex_panes.pane_inventory()
    found: list[codex_panes.PaneTarget] = []
    blocked = 0
    for process in codex_panes.process_states(proc).values():
        if process.uid != os.getuid() or process.state in {"T", "t"}:
            continue
        location = proc / str(process.pid)
        try:
            metadata = codex_panes.candidate_metadata(process, location)
            if metadata is not None:
                found.append(codex_panes.verified_target(process, location, metadata, inventory))
        except FileNotFoundError, ProcessLookupError:
            continue
        except OSError, ValueError, codex_panes.PaneError:
            blocked += 1
    owners = Counter((target.session, target.pane_id) for target in found)
    threads = Counter((target.home, target.thread_id) for target in found)
    selected = [
        target
        for target in found
        if owners[target.session, target.pane_id] == 1 and threads[target.home, target.thread_id] == 1
    ]
    return Inventory(sorted(selected, key=lambda target: target.process.pid), blocked + len(found) - len(selected))


def identity(target: codex_panes.PaneTarget) -> str:
    return hashlib.sha256(os.fsencode(target.home) + b"\0" + target.thread_id.encode()).hexdigest()


def current_target(target: codex_panes.PaneTarget, *, proc: Path) -> codex_panes.PaneTarget:
    observed = codex_panes.process_states(proc).get(target.process.pid)
    if observed is None or observed != target.process or observed.state in {"T", "t"}:
        message = "Codex process identity or foreground changed before recovery"
        raise RecoveryError(message)
    location = proc / str(target.process.pid)
    metadata = codex_panes.candidate_metadata(observed, location)
    if metadata is None:
        message = "Codex no longer owns the target terminal"
        raise RecoveryError(message)
    current = codex_panes.verified_target(observed, location, metadata, codex_panes.session_inventory(target.session))
    if current != target:
        message = "Codex pane or checkpoint changed before recovery"
        raise RecoveryError(message)
    return current


def verify(target: codex_panes.PaneTarget, error: str, *, proc: Path, io: PaneIO) -> None:
    current = current_target(target, proc=proc)
    if capacity_error(io.screen(current)) is None or codex_events.completion(current, proc=proc).fingerprint != error:
        message = "Codex pane, checkpoint, or input changed before recovery"
        raise RecoveryError(message)


def draft_text(raw: str) -> str | None:
    view = screen_view(raw)
    if view.composer is None or view.empty:
        return None
    return view.plain.splitlines()[view.composer].strip()[1:].strip()


def wait_for_draft(
    target: codex_panes.PaneTarget, error: str, *, sleeper: Callable[[float], None] = time.sleep
) -> None:
    deadline = time.monotonic() + ACK_SECONDS
    while True:
        draft = draft_text(inspect_screen(target))
        if draft is not None:
            if draft != "continue":
                message = "Codex input changed after paste; no Enter was sent"
                raise RecoveryError(message)
            current_target(target, proc=codex_panes.PROC)
            if codex_events.completion(target, proc=codex_panes.PROC).fingerprint != error:
                message = "Codex turn changed after paste; no Enter was sent"
                raise RecoveryError(message)
            return
        if time.monotonic() >= deadline:
            message = "Codex did not acknowledge the pasted continuation; no Enter was sent"
            raise RecoveryError(message)
        sleeper(0.05)


def continue_target(target: codex_panes.PaneTarget) -> None:
    observed = codex_events.completion(target, proc=codex_panes.PROC)
    if observed.state != "capacity":
        message = "Codex no longer has a current capacity failure"
        raise RecoveryError(message)
    prefix = ["zellij", "--session", target.session, "action"]
    run([*prefix, "paste", "--pane-id", str(target.pane_id), "continue"], capture_output=True, timeout=2)
    wait_for_draft(target, observed.fingerprint)
    run([*prefix, "send-keys", "--pane-id", str(target.pane_id), "Enter"], capture_output=True, timeout=2)


def acknowledged(
    target: codex_panes.PaneTarget, error: str, *, proc: Path, sleeper: Callable[[float], None] = time.sleep
) -> bool:
    deadline = time.monotonic() + ACK_SECONDS
    while True:
        observed = codex_events.completion(target, proc=proc)
        if observed.state in {"active", "success", "failed"} or (
            observed.state == "capacity" and observed.fingerprint != error
        ):
            return True
        if time.monotonic() >= deadline:
            return False
        sleeper(0.05)


@dataclass(frozen=True)
class PaneIO:
    screen: Callable[[codex_panes.PaneTarget], str] = inspect_screen
    submit: Callable[[codex_panes.PaneTarget], None] = continue_target


class RecoverySweep:
    def __init__(self, root: Path, *, proc: Path, io: PaneIO, now: float, apply: bool) -> None:
        self.root = root
        self.proc = proc
        self.io = io
        self.state = ledger(root)
        self.report = Sweep(observed_at=now, preview=not apply)

    def inspect(self, target: codex_panes.PaneTarget) -> None:
        raw = self.io.screen(target)
        self.report.checked += 1
        key = identity(target)
        observed = codex_events.completion(target, proc=self.proc)
        if capacity_error(raw) is None:
            if successful_idle(raw) and observed.state == "success":
                self.state.attempts.pop(key, None)
            return
        if observed.state != "capacity":
            self.report.blocked += 1
            return
        self.report.capacity += 1
        if not self.report.preview:
            self.retry(target, key, observed.fingerprint)
        attempt = self.state.attempts.get(key)
        self.report.panes.append(
            PaneStatus(
                session=target.session,
                pane_id=target.pane_id,
                phase=attempt.phase if attempt else "preview",
                attempts=attempt.attempts if attempt else 0,
                due=attempt.due if attempt else self.report.observed_at,
            )
        )

    def retry(self, target: codex_panes.PaneTarget, key: str, error: str) -> None:
        attempt = self.state.attempts.get(key)
        if attempt is None:
            self.state.attempts[key] = Attempt(error=error, due=self.report.observed_at + INITIAL_DELAY)
            self.report.waiting += 1
            return
        if attempt.phase in {"dispatching", "uncertain", "sent"} and attempt.error == error:
            if attempt.phase != "sent" or self.report.observed_at >= attempt.due:
                attempt.phase = "uncertain"
                self.report.uncertain += 1
            else:
                self.report.waiting += 1
            return
        if attempt.phase in {"dispatching", "uncertain", "sent"}:
            attempt.phase = "waiting"
        if attempt.attempts >= MAX_ATTEMPTS:
            if self.report.observed_at < attempt.due:
                attempt.phase = "exhausted"
                self.report.exhausted += 1
            else:
                self.state.attempts[key] = Attempt(error=error, due=self.report.observed_at + INITIAL_DELAY)
                self.report.waiting += 1
            return
        if self.report.observed_at < attempt.due or self.report.attempted:
            self.report.waiting += 1
            return
        verify(target, error, proc=self.proc, io=self.io)
        attempt.error = error
        attempt.attempts += 1
        attempt.phase = "dispatching"
        delay = COOLDOWN if attempt.attempts == MAX_ATTEMPTS else min(MAX_DELAY, 60 * (1 << (attempt.attempts - 1)))
        attempt.due = self.report.observed_at + delay
        save(self.root, self.state)
        self.report.attempted += 1
        try:
            self.io.submit(target)
        except OSError, RuntimeError:
            attempt.phase = "uncertain"
            self.report.uncertain += 1
            save(self.root, self.state)
            return
        if acknowledged(target, error, proc=self.proc):
            attempt.phase = "sent"
            self.report.continued += 1
        else:
            attempt.phase = "uncertain"
            self.report.uncertain += 1
        save(self.root, self.state)

    def run(self) -> Sweep:
        inventory = targets(proc=self.proc)
        self.report.blocked = inventory.blocked
        ordered = [target for target in inventory.panes if target.process.pid > self.state.cursor] + [
            target for target in inventory.panes if target.process.pid <= self.state.cursor
        ]
        for target in ordered[:PAGE_SIZE]:
            self.state.cursor = target.process.pid
            try:
                self.inspect(target)
            except OSError, ValueError, RuntimeError:
                self.report.blocked += 1
        if not self.report.preview:
            save(self.root, self.state)
            private_write(self.root / "status.json", self.report.model_dump_json())
        return self.report


def sweep(
    home: Path,
    *,
    apply: bool = False,
    proc: Path = codex_panes.PROC,
    now: float | None = None,
    io: PaneIO | None = None,
) -> Sweep:
    with guard(home) as root:
        return RecoverySweep(
            root, proc=proc, io=io or PaneIO(), now=time.time() if now is None else now, apply=apply
        ).run()


@app.command("once")
def once(*, apply: bool = False) -> None:
    """Preview matching panes; --apply schedules guarded retries with a 30-second initial delay."""
    typer.echo(sweep(RuntimePaths.current().home, apply=apply).model_dump_json())


@contextmanager
def stopping() -> Generator[Event]:
    stopped = Event()

    def stop(_signal: int, _frame: object) -> None:
        stopped.set()

    previous = {number: signal.signal(number, stop) for number in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield stopped
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


@app.command("run", hidden=True)
def daemon_run() -> None:
    home = RuntimePaths.current().home
    with guard(home, "daemon.lock"), stopping() as stopped:
        while not stopped.is_set():
            try:
                report = sweep(home, apply=True)
                typer.echo(report.model_dump_json())
            except RecoveryBusyError:
                pass
            except OSError, ValueError, RuntimeError:
                # Native inspection can disappear during pane/account transitions.
                typer.echo("Codex recovery inspection unavailable; no input sent", err=True)
            stopped.wait(INTERVAL)


def service(paths: RuntimePaths, executable: Path) -> str:
    environment = user_environment(paths)
    return (
        "[Unit]\nDescription=Continue Codex after temporary model-capacity failures\n"
        "\n[Service]\nType=simple\n"
        f"ExecStart={systemd_value(str(executable))} -I -m long_running_agent.cli codex retry run\n"
        f"Environment={systemd_value('HOME=' + str(paths.home))}\n"
        f"Environment={systemd_value('PATH=' + environment['PATH'])}\n"
        "UMask=0077\nNoNewPrivileges=yes\nRestart=on-failure\nRestartSec=10\n"
        "TimeoutStopSec=15\nKillMode=control-group\nCPUQuota=25%\nMemoryMax=256M\nNice=10\n"
        "\n[Install]\nWantedBy=default.target\n"
    )


@app.command("enable")
def enable() -> None:
    """Enable automatic recovery for this Linux user's existing and future Codex panes."""
    paths = RuntimePaths.current()
    directory(paths.home)
    unit = paths.home / ".config/systemd/user" / SERVICE
    private_write(unit, service(paths, Path(sys.executable).absolute()))
    environment = user_environment(paths)
    run(["systemctl", "--user", "daemon-reload"], capture_output=True, timeout=15, env=environment)
    run(["systemctl", "--user", "enable", "--now", SERVICE], capture_output=True, timeout=15, env=environment)
    run(["systemctl", "--user", "restart", SERVICE], capture_output=True, timeout=20, env=environment)
    typer.echo(
        "Automatic Codex capacity recovery enabled. Check lra codex retry status; pause with lra codex retry disable."
    )


@app.command("disable")
def disable() -> None:
    """Pause automatic continuation without closing or modifying Codex panes."""
    run(["systemctl", "--user", "disable", "--now", SERVICE], capture_output=True, timeout=20)
    typer.echo("Automatic Codex capacity recovery paused.")


def service_state() -> str:
    try:
        result = run(["systemctl", "--user", "is-active", SERVICE], capture_output=True, check=False, timeout=5)
        return result.stdout.strip() or "unknown"
    except OSError, RuntimeError:
        return "unavailable"


def reset_receipts(home: Path) -> Path | None:
    with guard(home) as root:
        receipts: list[Path] = []
        for name in ("retries.json", "status.json"):
            path = root / name
            try:
                metadata = path.lstat()
            except FileNotFoundError:
                continue
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & 0o077
                or metadata.st_nlink != 1
            ):
                message = "Unsafe recovery metadata was preserved; repair ownership before resetting"
                raise RecoveryError(message)
            receipts.append(path)
        backup = root / f"reset-{uuid7().hex}" if receipts else None
        if backup is not None:
            backup.mkdir(mode=0o700)
            for path in receipts:
                path.rename(backup / path.name)
        save(root, Ledger())
        return backup


@app.command("reset")
def reset() -> None:
    """After checking uncertain panes, archive retry receipts and begin a new observation window."""
    backup = reset_receipts(RuntimePaths.current().home)
    typer.echo("Retry receipts reset. Matching panes will be observed again before continuation.")
    if backup is not None:
        typer.echo(f"Previous receipts preserved privately: {backup}")


@app.command("status")
def status(*, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show service health, pending retries, cooldowns, and uncertain panes."""
    root = directory(RuntimePaths.current().home)
    record: Ledger | Sweep | None = None
    with suppress(FileNotFoundError):
        record = read_record(root / "status.json", Sweep)
    if record is not None and not isinstance(record, Sweep):
        message = "Unexpected Codex recovery status"
        raise RecoveryError(message)
    view = WatcherStatus(service=service_state(), sweep=record)
    if as_json:
        typer.echo(view.model_dump_json())
        return
    typer.echo(f"Capacity recovery service: {view.service}")
    if record is None:
        typer.echo("No completed sweep yet. Enable automatic recovery with lra codex retry enable.")
        return
    age = max(0, time.time() - record.observed_at)
    typer.echo(
        f"Last sweep {age:.0f}s ago: {record.checked} panes checked, {record.capacity} capacity failures, {record.continued} continued."
    )
    for pane in record.panes:
        delay = max(0, pane.due - time.time())
        typer.echo(
            f"{pane.session} / pane {pane.pane_id}: {pane.phase}, {pane.attempts} attempts, next retry in {delay:.0f}s."
        )
    if record.exhausted:
        typer.echo("Retry budgets are cooling down; automatic recovery will resume after the cooldown.")
    if record.uncertain:
        typer.echo("Check uncertain panes, then continue them manually or run lra codex retry reset.")
    if age > STALE_SECONDS:
        typer.echo("Watcher heartbeat is stale. Run lra codex retry enable to restart it.")
