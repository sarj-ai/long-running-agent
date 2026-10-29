from __future__ import annotations

from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import asdict, dataclass, field, replace
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import sqlite3
import stat
import time
from typing import TYPE_CHECKING, Final, NewType, Protocol
from uuid import uuid7

from pydantic import BaseModel, ConfigDict, RootModel, ValidationError

from long_running_agent.process import environment_snapshot, run

from .cleanup import bounded_bytes, live_sessions
from .settings import private_write


if TYPE_CHECKING:
    from collections.abc import Sequence

    from long_running_agent.process import Completed

PROC: Final = Path("/proc")
PaneId = NewType("PaneId", int)
TabId = NewType("TabId", int)
UUID: Final = r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
FLAGS: Final = frozenset(
    {
        "--approve-for-me",
        "--not-so-yolo",
        "--yolo",
        "--dangerously-bypass-approvals-and-sandbox",
        "--dangerously-bypass-hook-trust",
        "--search",
        "--no-alt-screen",
        "--no-daemon",
        "--strict-config",
    }
)
VALUES: Final = frozenset(
    {"-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--add-dir", "--enable", "--disable"}
)
CONFIG: Final = frozenset(
    {
        "model",
        "model_reasoning_effort",
        "approval_policy",
        "approvals_reviewer",
        "sandbox_mode",
        "log_dir",
        "agents.default_subagent_model",
        "agents.default_subagent_reasoning_effort",
        "agents.max_concurrent_threads_per_session",
    }
)
NONINTERACTIVE: Final = frozenset(
    {
        "exec",
        "e",
        "review",
        "app-server",
        "login",
        "logout",
        "sandbox",
        "mcp",
        "plugin",
        "debug",
        "agents",
        "help",
        "completion",
        "features",
        "doctor",
        "update",
        "remote-control",
        "app",
        "apply",
        "a",
        "queue",
        "archive",
        "delete",
        "migrate-rollouts",
        "unarchive",
        "cloud",
    }
)
PRIVATE_DIRECTORY: Final = 0o700
RESTORED_FLAGS: Final = frozenset(
    {"--approve-for-me", "--not-so-yolo", "--yolo", "--dangerously-bypass-approvals-and-sandbox"}
)
RESTORED_VALUES: Final = frozenset(
    {"-m", "--model", "-s", "--sandbox", "-a", "--ask-for-approval", "-C", "--cd", "--add-dir"}
)
RESTORED_CONFIG: Final = frozenset(
    {"model", "model_reasoning_effort", "approval_policy", "approvals_reviewer", "sandbox_mode"}
)


class PaneError(RuntimeError):
    pass


@dataclass(frozen=True)
class Process:
    pid: int
    birth: str
    parent: int
    group: int
    foreground: int
    uid: int = field(default_factory=os.getuid)
    state: str = field(default="S", compare=False)


@dataclass(frozen=True, repr=False)
class PaneTarget:
    process: Process
    session: str
    pane_id: PaneId
    tab_id: TabId
    home: Path
    cwd: Path
    arguments: tuple[str, ...] = field(repr=False)
    recovery: tuple[str, ...] = field(repr=False)
    thread_id: str = field(repr=False)


@dataclass(frozen=True)
class PanePlan:
    home: Path = field(repr=False)
    executable: Path = field(repr=False)
    targets: tuple[PaneTarget, ...] = field(repr=False)
    checkpoint: Path


@dataclass(frozen=True)
class PaneOutcome:
    session: str
    pane_id: PaneId
    state: str
    detail: str


@dataclass(frozen=True)
class RestartReport:
    outcomes: tuple[PaneOutcome, ...]
    checkpoint: Path

    @property
    def reopened(self) -> int:
        return sum(outcome.state == "reopened" for outcome in self.outcomes)

    @property
    def failed(self) -> int:
        return len(self.outcomes) - self.reopened


class Pane(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    id: PaneId
    tab_id: TabId
    is_plugin: bool
    exited: bool


class Panes(RootModel[list[Pane]]):
    pass


class ThreadRows(RootModel[list[tuple[str, str]]]):
    pass


@dataclass
class FrozenTree:
    descriptors: dict[int, int] = field(default_factory=dict)
    stopped: set[int] = field(default_factory=set)
    terminating: bool = False


class PaneCommand(Protocol):
    def __call__(self, arguments: Sequence[str], /, *, capture_output: bool, timeout: float) -> Completed: ...


class PaneOperation(Protocol):
    def __call__(self, target: PaneTarget, /, *, proc: Path, timeout: float) -> None: ...


class CheckpointVerifier(Protocol):
    def __call__(self, target: PaneTarget, /, *, proc: Path) -> None: ...


class ReadinessCheck(Protocol):
    def __call__(
        self, target: PaneTarget, pane_id: PaneId | None, /, *, restart_id: str, proc: Path, timeout: float
    ) -> None: ...


def pane_inventory() -> dict[tuple[str, PaneId], Pane]:
    sessions = run(["zellij", "list-sessions", "--no-formatting"], capture_output=True, check=False, timeout=10)
    if sessions.returncode:
        if "No active zellij sessions found" in sessions.stderr + sessions.stdout:
            return {}
        message = "Zellij session inventory is unavailable"
        raise PaneError(message)
    result: dict[tuple[str, PaneId], Pane] = {}
    for session in live_sessions(sessions.stdout):
        result.update(session_inventory(session))
    return result


def session_inventory(session: str) -> dict[tuple[str, PaneId], Pane]:
    return {(session, pane.id): pane for pane in session_panes(session) if not pane.is_plugin}


def session_panes(
    session: str,
    *,
    sleeper: Callable[[float], None] = time.sleep,
    runner: PaneCommand = run,
) -> list[Pane]:
    for _ in range(5):
        output = runner(
            ["zellij", "--session", session, "action", "list-panes", "--json"], capture_output=True, timeout=2
        )
        if output.stdout.strip():
            try:
                return Panes.model_validate_json(output.stdout).root
            except ValidationError:
                message = "Zellij returned unsupported pane metadata"
                raise PaneError(message) from None
        # Native CLI can receive completion before its separate Log payload.
        sleeper(0.03)
    message = "Zellij pane metadata was not acknowledged; retry when the session is ready"
    raise PaneError(message)


def process_states(proc: Path) -> dict[int, Process]:
    result: dict[int, Process] = {}
    for directory in proc.iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            uid = directory.stat().st_uid
            fields = bounded_bytes(directory / "stat").rsplit(b")", 1)[1].split()
            if fields[0] != b"Z":
                pid = int(directory.name)
                result[pid] = Process(
                    pid, fields[19].decode(), int(fields[1]), int(fields[2]), int(fields[5]), uid, fields[0].decode()
                )
        except FileNotFoundError, ProcessLookupError:
            continue
    return result


@dataclass(frozen=True)
class ProcessAccess:
    read: Callable[[Path], dict[int, Process]] = process_states
    open: Callable[[int], int] = os.pidfd_open
    send: Callable[[int, int], None] = signal.pidfd_send_signal
    close: Callable[[int], None] = os.close


def preserved_config(value: str) -> bool:
    key, _, setting = value.partition("=")
    key = key.strip()
    trust = (
        key.startswith("projects.")
        and key.endswith(".trust_level")
        and setting.strip() in {'"trusted"', '"untrusted"', "'trusted'", "'untrusted'"}
    )
    if (
        not trust
        and key not in CONFIG
        and not re.fullmatch(r"features\.[A-Za-z0-9_-]+|mcp_servers\.[A-Za-z0-9_-]+\.enabled", key)
    ):
        message = "Codex config override requires a manual restart"
        raise PaneError(message)
    return key not in RESTORED_CONFIG


def preserved_setting(name: str, value: str) -> bool:
    if name in {"-c", "--config"}:
        return preserved_config(value)
    return name not in RESTORED_VALUES


def flag_arguments(name: str, equal: str) -> tuple[str, ...]:
    if equal:
        message = "Codex boolean flag syntax is unsupported"
        raise PaneError(message)
    if name == "--no-daemon" or name in RESTORED_FLAGS:
        return ()
    return (name,)


def recovery_arguments(arguments: tuple[str, ...], thread_id: str) -> tuple[str, ...]:
    if not native_tui(arguments):
        message = "Only native local Codex terminal sessions can be restarted"
        raise PaneError(message)
    kept: list[str] = []
    index = 1
    while index < len(arguments):
        word = arguments[index]
        name, equal, inline = word.partition("=")
        if word == "--":
            break
        if name in FLAGS:
            kept.extend(flag_arguments(name, equal))
        elif name in VALUES | {"-c", "--config"}:
            index += not bool(equal)
            if index >= len(arguments):
                message = "Codex launch settings are incomplete"
                raise PaneError(message)
            value = inline if equal else arguments[index]
            if preserved_setting(name, value):
                kept.extend((name, value))
        elif word.startswith("-") and word not in {"--last", "--all", "--include-non-interactive"}:
            message = "Codex launch flag requires a manual restart"
            raise PaneError(message)
        index += 1
    return (arguments[0], "--no-daemon", *kept, "resume", thread_id)


def native_tui(arguments: tuple[str, ...]) -> bool:
    index = 1
    while index < len(arguments):
        word = arguments[index]
        name, equal, _ = word.partition("=")
        if word.startswith("--codex-run-as-"):
            return False
        if name in VALUES | {
            "-c",
            "--config",
            "--remote",
            "--remote-auth-token-env",
            "--profile",
            "-p",
            "--image",
            "-i",
        }:
            index += 1 + (not bool(equal))
            continue
        if word == "--":
            return True
        if not word.startswith("-"):
            return word not in NONINTERACTIVE
        index += 1
    return True


def writable_rollout(descriptor: Path, home: Path) -> str | None:
    target = descriptor.readlink()
    match = re.fullmatch(rf"rollout-.*-({UUID})\.jsonl", target.name)
    # Native writers reserve locks before persist() creates a resumable rollout.
    if match is None or not any(target.is_relative_to(home / folder) for folder in ("sessions", "archived_sessions")):
        return None
    details = bounded_bytes(descriptor.parent.parent / "fdinfo" / descriptor.name).decode()
    flags = int(next(line.partition(":")[2].strip() for line in details.splitlines() if line.startswith("flags:")), 8)
    if flags & os.O_ACCMODE in {os.O_WRONLY, os.O_RDWR} and descriptor.readlink() == target:
        return match[1]
    return None


def checkpoint_rows(home: Path, identifiers: set[str]) -> list[tuple[str, str]]:
    database = home / "state_5.sqlite"
    if database.stat().st_uid != os.getuid() or database.is_symlink():
        message = "Codex checkpoint index ownership is unsupported"
        raise PaneError(message)
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as connection:
            connection.execute("PRAGMA query_only=ON")
            rows: list[tuple[str, str]] = []
            for identifier in identifiers:
                rows.extend(
                    ThreadRows.model_validate(
                        connection.execute("SELECT id,source FROM threads WHERE id = ?", (identifier,)).fetchall()
                    ).root
                )
    except sqlite3.Error, ValidationError:
        message = "Codex checkpoint index could not be read; the session needs attention"
        raise PaneError(message) from None
    return rows


def current_root(process: Path, home: Path) -> str:
    identifiers = {
        identifier for descriptor in (process / "fd").iterdir() if (identifier := writable_rollout(descriptor, home))
    }
    if not identifiers:
        message = "Codex has no identifiable writable checkpoint"
        raise PaneError(message)
    rows = checkpoint_rows(home, identifiers)
    roots = [identifier for identifier, source in rows if source in {"cli", "exec"}]
    if (
        len(rows) != len(identifiers)
        or len(roots) != 1
        or any(source not in {"cli", "exec"} and not source.startswith('{"subagent":') for _, source in rows)
    ):
        message = "Codex current root is ambiguous; restart manually"
        raise PaneError(message)
    return str(roots[0])


def candidate_metadata(process: Process, directory: Path) -> tuple[Path, tuple[str, ...], dict[str, str]] | None:
    if process.foreground <= 0 or process.group != process.foreground:
        return None
    executable = (directory / "exe").readlink()
    if executable.name.removesuffix(" (deleted)") != "codex":
        return None
    arguments = tuple(os.fsdecode(bounded_bytes(directory / "cmdline")).rstrip("\0").split("\0"))
    if (
        Path(arguments[0]).name != "codex"
        or not native_tui(arguments)
        or not str((directory / "fd/0").readlink()).startswith("/dev/pts/")
    ):
        return None
    environment = {
        key: value
        for item in os.fsdecode(bounded_bytes(directory / "environ")).split("\0")
        for key, separator, value in [item.partition("=")]
        if separator
        and key
        in {
            "HOME",
            "CODEX_HOME",
            "ZELLIJ_SESSION_NAME",
            "ZELLIJ_PANE_ID",
            "OPENAI_API_KEY",
            "CODEX_API_KEY",
            "CODEX_ACCESS_TOKEN",
            "CODEX_REMOTE",
            "CODEX_EXEC_SERVER_URL",
            "LRA_CODEX_RESTART_ID",
        }
    }
    if not environment.get("ZELLIJ_SESSION_NAME") or "ZELLIJ_PANE_ID" not in environment:
        return None
    if executable.name.endswith(" (deleted)"):
        message = "Native Codex executable changed; restart manually"
        raise PaneError(message)
    return executable, arguments, environment


def verified_target(
    process: Process,
    directory: Path,
    metadata: tuple[Path, tuple[str, ...], dict[str, str]],
    inventory: dict[tuple[str, PaneId], Pane],
) -> PaneTarget:
    executable, arguments, environment = metadata
    if any(
        environment.get(key)
        for key in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_REMOTE", "CODEX_EXEC_SERVER_URL")
    ):
        message = "API-key, remote, or external-executor Codex sessions require a manual account switch"
        raise PaneError(message)
    session, pane_id = environment["ZELLIJ_SESSION_NAME"], PaneId(int(environment["ZELLIJ_PANE_ID"]))
    pane = inventory.get((session, pane_id))
    if pane is None or pane.exited:
        message = "A foreground Codex pane cannot be verified"
        raise PaneError(message)
    cwd = (directory / "cwd").resolve(strict=True)
    home = Path(environment.get("CODEX_HOME") or str(Path(environment.get("HOME", str(Path.home()))) / ".codex"))
    home = (cwd / home).resolve(strict=True)
    mode = home.stat()
    if mode.st_uid != os.getuid() or not stat.S_ISDIR(mode.st_mode) or mode.st_mode & 0o077:
        message = "Codex home must be a private directory owned by the current user"
        raise PaneError(message)
    if executable.stat() != (directory / "exe").stat():
        message = "Native Codex executable changed; restart manually"
        raise PaneError(message)
    thread_id = current_root(directory, home)
    recovery = recovery_arguments((str(executable), *arguments[1:]), thread_id)
    return PaneTarget(process, session, pane_id, pane.tab_id, home, cwd, arguments, recovery, thread_id)


def empty_plan() -> PanePlan:
    environment = environment_snapshot()
    home = Path(environment.get("CODEX_HOME") or str(Path.home() / ".codex")).resolve()
    executable = shutil.which("codex")
    if executable is None:
        message = "Install the official Codex CLI before switching accounts"
        raise PaneError(message)
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = home.stat()
    if metadata.st_uid != os.getuid() or metadata.st_mode & 0o077:
        message = "Codex home must be a private directory owned by the current user"
        raise PaneError(message)
    return PanePlan(home, Path(executable).resolve(), (), home / "sarj-restarts" / f"{time.time_ns()}.json")


def plan_panes(*, proc: Path = PROC, allow_empty: bool = False) -> PanePlan:
    inventory = pane_inventory()
    targets: list[PaneTarget] = []
    for process in process_states(proc).values():
        if process.uid != os.getuid():
            continue
        directory = proc / str(process.pid)
        try:
            metadata = candidate_metadata(process, directory)
        except FileNotFoundError, ProcessLookupError:
            continue
        if metadata is not None:
            try:
                targets.append(verified_target(process, directory, metadata, inventory))
            except FileNotFoundError, ProcessLookupError:
                message = "Codex pane inventory changed; retry before signing in"
                raise PaneError(message) from None
    if not targets and allow_empty:
        return empty_plan()
    if (
        not targets
        or len({target.home for target in targets}) != 1
        or len({target.recovery[0] for target in targets}) != 1
        or len({(target.session, target.pane_id) for target in targets}) != len(targets)
    ):
        message = "Expected uniquely mapped active Codex panes sharing one private home and executable"
        raise PaneError(message)
    ordered = tuple(sorted(targets, key=lambda target: (target.session, target.pane_id)))
    states = process_states(proc)
    for target in ordered:
        target_members(target, states)
    home = ordered[0].home
    return PanePlan(home, Path(ordered[0].recovery[0]), ordered, home / "sarj-restarts" / f"{time.time_ns()}.json")


def same_process(observed: Process | None, expected: Process, *, foreground: bool = True) -> bool:
    return (
        observed is not None
        and (observed if foreground else replace(observed, foreground=expected.foreground)) == expected
    )


def target_members(target: PaneTarget, states: dict[int, Process], *, frozen: bool = False) -> set[int]:
    if not same_process(states.get(target.process.pid), target.process, foreground=not frozen):
        message = "Codex process identity or foreground changed; restart was refused"
        raise PaneError(message)
    members = {target.process.pid}
    while added := {pid for pid, state in states.items() if state.parent in members} - members:
        members.update(added)
    if os.getpid() in members:
        message = "Run account switching from a separate terminal; the controller is inside a target Codex process tree"
        raise PaneError(message)
    if any(state.group == target.process.group and pid not in members for pid, state in states.items()):
        message = "Codex foreground group has unrelated processes"
        raise PaneError(message)
    if any(states[pid].uid != os.getuid() for pid in members):
        message = "Codex has descendants owned by another user; restart manually"
        raise PaneError(message)
    return members


def stop_target(
    target: PaneTarget,
    *,
    proc: Path,
    timeout: float,
    access: ProcessAccess | None = None,
    checkpoint: CheckpointVerifier | None = None,
) -> None:
    access = access or ProcessAccess()
    tree = FrozenTree()
    try:
        terminate_tree(target, tree, proc=proc, timeout=timeout, access=access, checkpoint=checkpoint)
    except (OSError, PaneError) as error:
        if tree.stopped and not tree.terminating:
            reason = str(error) if isinstance(error, PaneError) else "Restart refused"
            message = f"{reason}; recovery checkpoint is available; the existing job may need native foreground restore"
            raise PaneError(message) from None
        raise
    finally:
        for pid in tree.stopped:
            with suppress(ProcessLookupError):
                access.send(tree.descriptors[pid], signal.SIGCONT)
        for descriptor in tree.descriptors.values():
            access.close(descriptor)


def pause_members(
    target: PaneTarget,
    tree: FrozenTree,
    states: dict[int, Process],
    members: set[int],
    *,
    proc: Path,
    access: ProcessAccess,
) -> None:
    for pid in [target.process.pid, *sorted(members - {target.process.pid})]:
        if pid not in tree.descriptors:
            descriptor = access.open(pid)
            tree.descriptors[pid] = descriptor
            if not same_process(access.read(proc).get(pid), states[pid], foreground=not bool(tree.stopped)):
                message = "Codex process identity changed; restart was refused"
                raise PaneError(message)
            if states[pid].state not in {"T", "t"}:
                tree.stopped.add(pid)
                access.send(descriptor, signal.SIGSTOP)


def freeze_tree(
    target: PaneTarget,
    tree: FrozenTree,
    *,
    proc: Path,
    timeout: float,
    access: ProcessAccess,
    sleeper: Callable[[float], None],
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        states = access.read(proc)
        members = target_members(target, states, frozen=bool(tree.descriptors))
        pause_members(target, tree, states, members, proc=proc, access=access)
        if all(state.state in {"T", "t"} for pid, state in states.items() if pid in members):
            return
        if time.monotonic() >= deadline:
            message = "Codex process tree could not be paused safely"
            raise PaneError(message)
        sleeper(0.01)


def wait_for_exit(tree: FrozenTree, *, timeout: float) -> None:
    with selectors.DefaultSelector() as selector:
        for descriptor in tree.descriptors.values():
            selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        while selector.get_map():
            if not (ready := selector.select(max(0, deadline - time.monotonic()))):
                message = "Old Codex processes did not exit; no replacement was started"
                raise PaneError(message)
            for key, _ in ready:
                selector.unregister(key.fd)


def terminate_tree(
    target: PaneTarget,
    tree: FrozenTree,
    *,
    proc: Path,
    timeout: float,
    sleeper: Callable[[float], None] = time.sleep,
    access: ProcessAccess | None = None,
    checkpoint: CheckpointVerifier | None = None,
) -> None:
    access = access or ProcessAccess()
    freeze_tree(target, tree, proc=proc, timeout=timeout, access=access, sleeper=sleeper)
    (checkpoint or verify_frozen_checkpoint)(target, proc=proc)
    tree.terminating = True
    for descriptor in tree.descriptors.values():
        with suppress(ProcessLookupError):
            access.send(descriptor, signal.SIGHUP)
            access.send(descriptor, signal.SIGCONT)
    wait_for_exit(tree, timeout=timeout)
    if any(state.group == target.process.group for state in access.read(proc).values()):
        message = "Old foreground group is still running; no replacement was started"
        raise PaneError(message)


def verify_frozen_checkpoint(target: PaneTarget, *, proc: Path) -> None:
    directory = proc / str(target.process.pid)
    pane = session_inventory(target.session).get((target.session, target.pane_id))
    if (
        current_root(directory, target.home) != target.thread_id
        or (directory / "cwd").resolve(strict=True) != target.cwd
        or str((directory / "exe").readlink()) != target.recovery[0]
        or pane is None
        or pane.tab_id != target.tab_id
    ):
        message = "Codex current checkpoint or pane changed; the new session was left running"
        raise PaneError(message)


def restart_panes(
    plan: PanePlan,
    *,
    proc: Path = PROC,
    timeout: float = 10,
    replacement: PaneOperation | None = None,
) -> RestartReport:
    fresh = plan_panes(proc=proc, allow_empty=not plan.targets)
    if fresh.home != plan.home or fresh.targets != plan.targets:
        message = "Active Codex panes changed during sign-in; nothing was restarted"
        raise PaneError(message)
    if not plan.targets:
        return RestartReport((), plan.checkpoint)
    directory = plan.checkpoint.parent
    if directory.exists() or directory.is_symlink():
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != PRIVATE_DIRECTORY
        ):
            message = "Recovery directory must be private and owned by the current user"
            raise PaneError(message)
    private_write(
        plan.checkpoint,
        json.dumps(
            {
                "version": 1,
                "targets": [
                    {
                        "process": asdict(target.process),
                        "session": target.session,
                        "pane_id": target.pane_id,
                        "tab_id": target.tab_id,
                        "home": str(target.home),
                        "cwd": str(target.cwd),
                        "argv": target.recovery,
                        "thread_id": target.thread_id,
                    }
                    for target in plan.targets
                ],
            }
        ),
    )
    outcomes: list[PaneOutcome] = []
    for target in plan.targets:
        try:
            (replacement or replace_target)(target, proc=proc, timeout=timeout)
            outcomes.append(
                PaneOutcome(
                    target.session,
                    target.pane_id,
                    "reopened",
                    "Verified the saved native session reopened",
                )
            )
        except (OSError, RuntimeError) as error:
            detail = (
                str(error)
                if isinstance(error, PaneError)
                else "Native restart failed; use the saved recovery checkpoint"
            )
            outcomes.append(PaneOutcome(target.session, target.pane_id, "failed", detail))
    return RestartReport(tuple(outcomes), plan.checkpoint)


def replace_target(
    target: PaneTarget,
    *,
    proc: Path,
    timeout: float,
    stop: PaneOperation = stop_target,
    runner: PaneCommand = run,
    ready: ReadinessCheck | None = None,
) -> None:
    stop(target, proc=proc, timeout=timeout)
    pane = session_inventory(target.session).get((target.session, target.pane_id))
    if pane is None or pane.tab_id != target.tab_id:
        message = "Original pane no longer exists; use the saved recovery checkpoint"
        raise PaneError(message)
    restart_id = uuid7().hex
    created = runner(
        [
            "zellij",
            "--session",
            target.session,
            "action",
            "new-pane",
            "--in-place",
            "--close-replaced-pane",
            "--no-focus",
            "--pane-id",
            f"terminal_{target.pane_id}",
            "--cwd",
            str(target.cwd),
            "--",
            "env",
            f"CODEX_HOME={target.home}",
            f"LRA_CODEX_RESTART_ID={restart_id}",
            *target.recovery,
        ],
        capture_output=True,
        timeout=15,
    )
    match = re.fullmatch(r"terminal_([0-9]+)\s*", created.stdout)
    if match is None and created.stdout.strip():
        message = "Zellij replacement could not be verified; use the saved recovery checkpoint"
        raise PaneError(message)
    (ready or wait_ready)(
        target, PaneId(int(match[1])) if match is not None else None, restart_id=restart_id, proc=proc, timeout=timeout
    )


def replacement_invocation(
    state: Process, environment: dict[str, str], target: PaneTarget, pane_id: PaneId | None, restart_id: str
) -> bool:
    return (
        environment.get("ZELLIJ_SESSION_NAME") == target.session
        and environment.get("LRA_CODEX_RESTART_ID") == restart_id
        and (pane_id is None or environment.get("ZELLIJ_PANE_ID") == str(pane_id))
        and (state.pid, state.birth) != (target.process.pid, target.process.birth)
    )


def replacement_identity(observed: PaneTarget, target: PaneTarget, inventory: dict[tuple[str, PaneId], Pane]) -> bool:
    if (
        observed.home != target.home
        or observed.recovery[0] != target.recovery[0]
        or observed.thread_id != target.thread_id
    ):
        message = "Replacement checkpoint identity differs; the new pane needs attention"
        raise PaneError(message)
    return (
        observed.pane_id != target.pane_id
        and observed.tab_id == target.tab_id
        and (target.session, target.pane_id) not in inventory
    )


def replacement_target(
    state: Process, target: PaneTarget, pane_id: PaneId | None, *, restart_id: str, proc: Path
) -> PaneTarget | None:
    if state.uid != os.getuid():
        return None
    directory = proc / str(state.pid)
    try:
        metadata = candidate_metadata(state, directory)
    except FileNotFoundError, ProcessLookupError:
        return None
    if metadata is None or not replacement_invocation(state, metadata[2], target, pane_id, restart_id):
        return None
    try:
        inventory = session_inventory(target.session)
        observed = verified_target(state, directory, metadata, inventory)
    except OSError, PaneError:
        return None
    return observed if replacement_identity(observed, target, inventory) else None


def wait_ready(
    target: PaneTarget,
    pane_id: PaneId | None,
    *,
    restart_id: str,
    proc: Path,
    timeout: float,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        states = process_states(proc)
        matched = [
            observed
            for state in states.values()
            if (observed := replacement_target(state, target, pane_id, restart_id=restart_id, proc=proc))
        ]
        if len(matched) == 1 and not any(state.group == target.process.group for state in states.values()):
            return
        if len(matched) > 1:
            message = "Multiple replacement processes matched; the new panes need attention"
            raise PaneError(message)
        if time.monotonic() >= deadline:
            message = "Native resume needs attention; checkpoint ownership was not verified"
            raise PaneError(message)
        sleeper(0.05)
