from __future__ import annotations

from collections import deque
from contextlib import suppress
from importlib.metadata import version
import json
import os
import selectors
import time
from typing import TYPE_CHECKING, Annotated, Final, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError
import typer

from long_running_agent.process import DEVNULL, PIPE, ChildProcess, ProcessTimeout, environment_snapshot, spawn

from . import codex_events, codex_panes, codex_recovery
from .settings import RuntimePaths


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path
    from types import TracebackType
    from typing import Self

MAX_MESSAGE_BYTES: Final = 1_048_576
UNAVAILABLE: Final = "Native Codex authentication server disconnected; existing panes were left running"
app = typer.Typer(
    help="Recover Codex capacity failures and switch accounts while preserving conversations.", no_args_is_help=True
)
app.add_typer(codex_recovery.app, name="retry")


class CodexError(RuntimeError):
    pass


class Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True, frozen=True)


class AccountStatus(Record):
    method: str
    email: str | None = None
    plan: str | None = None


class DeviceCode(Record):
    login_id: str = Field(alias="loginId", min_length=1)
    verification_url: str = Field(alias="verificationUrl", min_length=1)
    user_code: str = Field(alias="userCode", min_length=1)


class RpcPeer(Protocol):
    def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue: ...

    def login_completed(self, login_id: str) -> bool: ...


class WireError(Record):
    code: int


class Message(Record):
    id: int | str | None = None
    method: str | None = None
    params: dict[str, JsonValue] = Field(default_factory=dict)
    result: JsonValue = None
    error: WireError | None = None


class LoginCompleted(Record):
    login_id: str | None = Field(default=None, alias="loginId")
    success: bool


class CodexProcess:
    def __init__(self, process: ChildProcess, *, timeout: float = 30, login_timeout: float = 600) -> None:
        if process.stdin is None or process.stdout is None:
            message = "Codex authentication server requires stdio pipes"
            raise CodexError(message)
        self.process = process
        self.input = process.stdin
        self.output = process.stdout
        self.timeout = timeout
        self.login_timeout = login_timeout
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.output, selectors.EVENT_READ)
        self.buffer = b""
        self.counter = 0
        self.logins: deque[LoginCompleted] = deque(maxlen=8)
        self.closed = False

    def __enter__(self) -> Self:
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": "sarj_connect", "version": version("long-running-agent")},
                    "capabilities": {"experimentalApi": True},
                },
            )
            self.send({"method": "initialized"})
        except BaseException:
            self.close()
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.selector.close()
        with suppress(OSError):
            self.input.close()
        self.output.close()
        try:
            self.process.wait(timeout=2)
        except ProcessTimeout:
            # Only our temporary server is signaled, never an existing pane or daemon.
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except ProcessTimeout:
                self.process.kill()
                with suppress(ProcessTimeout):
                    self.process.wait(timeout=2)

    def send(self, message: dict[str, JsonValue]) -> None:
        try:
            self.input.write(json.dumps(message).encode() + b"\n")
            self.input.flush()
        except OSError:
            raise CodexError(UNAVAILABLE) from None

    def receive(self, deadline: float) -> Message:
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                message = "Native Codex request timed out; existing work was left running"
                raise CodexError(message)
            chunk = os.read(self.output.fileno(), 65_536)
            if not chunk:
                raise CodexError(UNAVAILABLE)
            self.buffer += chunk
            if len(self.buffer) > MAX_MESSAGE_BYTES:
                message = "Native Codex response exceeded the metadata limit"
                raise CodexError(message)
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            return Message.model_validate_json(line)
        except ValidationError:
            message = "Native Codex returned an unsupported protocol message"
            raise CodexError(message) from None

    def remember_login(self, response: Message) -> None:
        try:
            self.logins.append(LoginCompleted.model_validate(response.params))
        except ValidationError:
            message = "Native Codex returned an unsupported sign-in response"
            raise CodexError(message) from None

    def request(self, method: str, params: dict[str, JsonValue]) -> JsonValue:
        self.counter += 1
        request_id = self.counter
        self.send({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            response = self.receive(deadline)
            if response.method == "account/login/completed":
                self.remember_login(response)
            if response.id != request_id or response.method is not None:
                continue
            if response.error is not None:
                message = f"Native Codex rejected {method} (RPC {response.error.code})"
                raise CodexError(message)
            return response.result

    def login_completed(self, login_id: str) -> bool:
        deadline = time.monotonic() + self.login_timeout
        while True:
            while self.logins:
                event = self.logins.popleft()
                if event.login_id == login_id:
                    return event.success
            response = self.receive(deadline)
            if response.method == "account/login/completed":
                self.remember_login(response)


def connect(
    codex_home: Path | None = None,
    executable: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> CodexProcess:
    child_environment = dict(environment_snapshot() if environment is None else environment)
    if codex_home is not None:
        child_environment["CODEX_HOME"] = str(codex_home)
    # Native, process-local suppression; leave persisted remote-control preferences alone.
    child_environment["CODEX_INTERNAL_APP_SERVER_REMOTE_CONTROL_DISABLED"] = "1"
    return CodexProcess(
        spawn(
            [str(executable) if executable is not None else "codex", "app-server", "--stdio"],
            env=child_environment,
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
        )
    )


class NativeAccount(Record):
    type: str
    email: str | None = None
    plan_type: str | None = Field(default=None, alias="planType")


class AccountResponse(Record):
    account: NativeAccount | None


class NativeCodex:
    def __init__(self, peer: RpcPeer) -> None:
        self.peer = peer

    def account_status(self) -> AccountStatus:
        response = AccountResponse.model_validate(self.peer.request("account/read", {"refreshToken": False}))
        account = response.account
        return AccountStatus(
            method=account.type if account else "signed_out",
            email=account.email if account else None,
            plan=account.plan_type if account else None,
        )

    def switch_account(self, show_device_code: Callable[[DeviceCode], None]) -> AccountStatus:
        if self.account_status().method not in {"chatgpt", "signed_out"}:
            message = "API-key or unsupported native accounts require a manual account switch"
            raise CodexError(message)
        challenge = DeviceCode.model_validate(self.peer.request("account/login/start", {"type": "chatgptDeviceCode"}))
        try:
            show_device_code(challenge)
            success = self.peer.login_completed(challenge.login_id)
        except BaseException:
            with suppress(CodexError):
                self.peer.request("account/login/cancel", {"loginId": challenge.login_id})
            raise
        if not success:
            message = "Native Codex sign-in did not complete"
            raise CodexError(message)
        account = self.account_status()
        if account.method != "chatgpt":
            message = "Native sign-in completed, but ChatGPT authentication could not be verified"
            raise CodexError(message)
        return account


@app.command("status")
def status(*, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the account reported by native Codex without reading or printing credential files."""
    with connect() as peer:
        account = NativeCodex(peer).account_status()
    if as_json:
        typer.echo(account.model_dump_json())
    else:
        typer.echo(f"Codex account: {account.email or account.method}" + (f" ({account.plan})" if account.plan else ""))


def show_device_code(challenge: DeviceCode) -> None:
    typer.echo(
        f"Sign in as your own Codex account: {challenge.verification_url}\nCode: {challenge.user_code}", err=True
    )


def native_switch(codex_home: Path, executable: Path) -> AccountStatus:
    with connect(codex_home, executable) as peer:
        return NativeCodex(peer).switch_account(show_device_code)


@app.command("switch")
def switch(*, as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Sign in with another account and reopen verified Codex panes from their saved conversations."""
    with codex_recovery.guard(RuntimePaths.current().home):
        switch_panes(plan_switch, native_switch, restart_idle_panes, as_json=as_json)


def require_idle(target: codex_panes.PaneTarget, *, proc: Path, io: codex_recovery.PaneIO | None = None) -> None:
    observed = codex_events.completion(target, proc=proc)
    screen = (io or codex_recovery.PaneIO()).screen(target)
    if (
        observed.state not in {"success", "capacity", "failed", "aborted"}
        or not codex_recovery.screen_view(screen).empty
        or codex_recovery.BLOCKED.search(codex_recovery.ANSI.sub("", screen))
    ):
        message = f"{target.session} / pane {target.pane_id} is working or has pending input; let it finish before switching accounts"
        raise codex_panes.PaneError(message)


def plan_switch() -> codex_panes.PanePlan:
    plan = codex_panes.plan_panes(allow_empty=True)
    for target in plan.targets:
        require_idle(target, proc=codex_panes.PROC)
    return plan


def verify_idle_checkpoint(target: codex_panes.PaneTarget, *, proc: Path) -> None:
    codex_panes.verify_frozen_checkpoint(target, proc=proc)
    require_idle(target, proc=proc)


def replace_idle_target(target: codex_panes.PaneTarget, *, proc: Path, timeout: float) -> None:
    require_idle(target, proc=proc)
    codex_panes.replace_target(target, proc=proc, timeout=timeout, stop=stop_idle_target)


def stop_idle_target(target: codex_panes.PaneTarget, *, proc: Path, timeout: float) -> None:
    codex_panes.stop_target(target, proc=proc, timeout=timeout, checkpoint=verify_idle_checkpoint)


def restart_idle_panes(plan: codex_panes.PanePlan) -> codex_panes.RestartReport:
    return codex_panes.restart_panes(plan, replacement=replace_idle_target)


def switch_panes(
    plan_panes: Callable[[], codex_panes.PanePlan],
    authenticate: Callable[[Path, Path], AccountStatus],
    restart_panes: Callable[[codex_panes.PanePlan], codex_panes.RestartReport],
    *,
    as_json: bool = True,
) -> None:
    plan = plan_panes()
    typer.echo(f"After sign-in, restart {len(plan.targets)} active Codex panes from saved checkpoints.", err=True)
    account = authenticate(plan.home, plan.executable)
    if as_json:
        typer.echo(json.dumps({"event": "account_switched", "account": account.model_dump(mode="json")}))
    else:
        typer.echo(f"Using Codex account: {account.email or account.method}")
    report = restart_panes(plan)
    if not as_json:
        if report.outcomes:
            typer.echo(f"Reopened {report.reopened} conversations in their original panes.")
        else:
            typer.echo("New Codex windows will use this account.")
        if report.failed:
            typer.echo(f"{report.failed} panes need attention. Recovery checkpoint: {report.checkpoint}", err=True)
            for outcome in report.outcomes:
                if outcome.state != "reopened":
                    typer.echo(f"{outcome.session} / pane {outcome.pane_id}: {outcome.detail}", err=True)
            raise typer.Exit(1)
        return
    typer.echo(
        json.dumps(
            {
                "event": "pane_results",
                "reopened": report.reopened,
                "failed": report.failed,
                "checkpoint": str(report.checkpoint),
                "outcomes": [
                    {
                        "session": outcome.session,
                        "pane_id": outcome.pane_id,
                        "state": outcome.state,
                        "detail": outcome.detail,
                    }
                    for outcome in report.outcomes
                ],
            }
        )
    )
    if report.failed:
        raise typer.Exit(1)
