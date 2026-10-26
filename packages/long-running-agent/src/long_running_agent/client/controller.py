from __future__ import annotations

from pathlib import Path
import platform
import re
import shlex
import time
from typing import TYPE_CHECKING, Final

from rich.console import Console

from long_running_agent.client import browser
from long_running_agent.client.cloud import Cloud, PowerAction, Runner
from long_running_agent.client.connection import launch_master
from long_running_agent.client.host import HostBridge
from long_running_agent.client.ssh_config import install
from long_running_agent.client.terminal import TerminalState
from long_running_agent.config import REMOTE_RUNTIME, Settings, load_settings
from long_running_agent.process import CommandError, Completed, ProcessTimeout, run


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

TIMEOUT_STATUS = 124
SSH_CONNECTION_CLOSED = 255
WAKE_ACTIONS: Final[dict[str, PowerAction]] = {"TERMINATED": "start", "SUSPENDED": "resume"}
TRANSITIONAL = frozenset({"PROVISIONING", "STAGING", "REPAIRING", "RESUMING", "STOPPING", "SUSPENDING"})
CONNECTION_ATTEMPTS = 3


class Client:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        runner: Runner = run,
        home: Path | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        master_launcher: Callable[[Sequence[str]], int] = launch_master,
    ) -> None:
        self.settings, self.runner = settings or load_settings(), runner
        self.home = Path.home() if home is None else home
        self.sleeper, self.clock = sleeper, clock
        self.master_launcher = master_launcher
        self.cloud = Cloud(self.settings, runner=runner, sleeper=sleeper, clock=clock)

    def ssh(
        self,
        arguments: Sequence[str],
        *,
        timeout: float | None = None,
        capture_output: bool = False,
        check: bool = True,
        input_text: str | None = None,
    ) -> Completed:
        return self.runner(
            ["ssh", *arguments], timeout=timeout, capture_output=capture_output, check=check, input_text=input_text
        )

    def wake(self) -> None:
        requested: PowerAction | None = None
        deadline = self.clock() + 300
        for _ in range(150):
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            state = self.cloud.state(timeout=min(30, remaining))
            if state == "RUNNING":
                return
            if state in WAKE_ACTIONS:
                action = WAKE_ACTIONS[state]
                if requested and requested != action:
                    message = f"VM changed to {state}; no further power operation attempted"
                    raise RuntimeError(message)
                if not requested:
                    requested = action
                    if self._request_wake(action, deadline):
                        return
            elif state not in TRANSITIONAL:
                message = f"Unexpected VM state: {state}; no power operation attempted"
                raise RuntimeError(message)
            self.sleeper(min(2, max(0, deadline - self.clock())))
        message = "VM wake exceeded five minutes; check the VM in Google Cloud before trying again"
        raise TimeoutError(message)

    def _request_wake(self, action: PowerAction, deadline: float) -> bool:
        try:
            self.cloud.change_power(action, asynchronous=True, timeout=max(0.01, deadline - self.clock()))
        except CommandError:
            observed = self.cloud.state(timeout=min(30, max(0.01, deadline - self.clock())))
            if observed == "RUNNING":
                return True
            if observed not in {"PROVISIONING", "STAGING", "RESUMING"}:
                raise
        return False

    def ensure_connection(self) -> None:
        alias = self.settings.ssh_alias
        master = self.ssh(["-O", "check", alias], timeout=4, capture_output=True, check=False)
        if master.returncode == 0:
            try:
                healthy = self.ssh(
                    [
                        "-T",
                        "-o",
                        "BatchMode=yes",
                        "-o",
                        "ProxyCommand=false",
                        "-o",
                        "ConnectionAttempts=1",
                        alias,
                        "true",
                    ],
                    timeout=12,
                    capture_output=True,
                    check=False,
                )
                if healthy.returncode == 0:
                    return
            except (ProcessTimeout, CommandError) as error:
                if isinstance(error, CommandError) and error.returncode != TIMEOUT_STATUS:
                    raise
            # stop only the stale master's listening socket. Existing attached
            # channels are not terminated as they would be with `-O exit`.
            self.ssh(["-O", "stop", alias], timeout=4, capture_output=True, check=False)
        self.wake()
        for attempt in range(CONNECTION_ATTEMPTS):
            try:
                code = self.master_launcher(["ssh", "-Nf", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", alias])
                if code == 0:
                    return
            except (ProcessTimeout, CommandError) as error:
                if isinstance(error, CommandError) and error.returncode != TIMEOUT_STATUS:
                    raise
            if attempt + 1 < CONNECTION_ATTEMPTS:
                self.sleeper(1 + attempt)
        message = "SSH connection failed after three bounded attempts; inspect network and gcloud login"
        raise RuntimeError(message)

    def remote(
        self,
        command: str,
        *,
        tty: bool = False,
        check: bool = True,
        capture_output: bool = False,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> Completed:
        return self.ssh(
            ["-tt" if tty else "-T", self.settings.ssh_alias, f"exec {REMOTE_RUNTIME} {command}"],
            timeout=timeout,
            capture_output=capture_output,
            input_text=input_text,
            check=check,
        )

    def summary(self, reconnect: Sequence[str] = ("vm",)) -> str:
        settings = self.settings
        return (
            f"\nVM: {settings.instance}\nProject: {settings.project} | Zone: {settings.zone}\n"
            f"Account: {settings.account or 'active gcloud account'}\n"
            f"Reconnect: {shlex.join(reconnect)}\n"
        )

    def browser(self, *, no_open: bool = False, url: str | None = None) -> int:
        try:
            return browser.connect(self, no_open=no_open, url=url)
        finally:
            Console(stderr=True).print(self.summary(("sarj-connect", "browser")), markup=False)

    def shell(self, command: Sequence[str], *, tty: bool = False, reconnect: Sequence[str] = ("vm",)) -> int:
        try:
            self.ensure_connection()
            with TerminalState():
                code = self.ssh(
                    [
                        "-tt" if tty or not command else "-T",
                        self.settings.ssh_alias,
                        shlex.join(command) if command else 'exec "$SHELL" -l',
                    ],
                    check=False,
                ).returncode
            if code == SSH_CONNECTION_CLOSED:
                Console(stderr=True).print(
                    f"SSH connection closed. Run {shlex.join(reconnect)} to reconnect.", markup=False
                )
            return code
        finally:
            Console(stderr=True).print(self.summary(reconnect), markup=False)

    def terminal(self, session: str = "sarj-home", *, host_bridge: bool = True) -> int:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", session) is None:
            message = "Use a Zellij session name of 1–64 letters, numbers, dots, underscores or hyphens, starting with a letter or number."
            raise ValueError(message)
        if host_bridge and platform.system() == "Darwin":
            try:
                with TerminalState(), HostBridge(self, route_factory=browser.start_route) as bridge:
                    return bridge.terminal(session)
            finally:
                Console(stderr=True).print(self.summary(("sarj-connect", "terminal", session)), markup=False)
        command = [
            "/bin/sh",
            "-c",
            'export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"; exec zellij "$@"',
            "zellij",
            "attach",
            "--create",
            session,
        ]
        reconnect = ["sarj-connect", "terminal", session]
        return self.shell(command, tty=True, reconnect=reconnect)

    def configure_ssh(self) -> Path:
        key = self.settings.ssh_key
        if not key.exists():
            key.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self.runner(
                ["ssh-keygen", "-q", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "lra-oslogin"], timeout=30
            )
        result = self.cloud.execute(
            [
                "compute",
                "ssh",
                self.settings.instance,
                "--zone=" + self.settings.zone,
                "--tunnel-through-iap",
                f"--ssh-key-file={key}",
                "--quiet",
                "--command=id -un",
            ],
            capture_output=True,
            timeout=90,
        )
        username = result.stdout.strip()
        path = install(self.settings, username, home=self.home)
        self.runner(["ssh", "-G", "-T", self.settings.ssh_alias], capture_output=True, timeout=10)
        return path

    def setup(self) -> None:
        self.configure_ssh()
        self.ensure_connection()
        self.remote("runtime setup", timeout=300)
