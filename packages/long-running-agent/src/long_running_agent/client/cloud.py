from __future__ import annotations

import shlex
from subprocess import CompletedProcess  # ruff: ignore[suspicious-subprocess-import] -- Construct data records only; child execution uses the common process boundary.
import time
from typing import TYPE_CHECKING, Literal, Protocol

from long_running_agent.process import CommandError, Completed, ProcessTimeout, environment_snapshot, run


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence
    from pathlib import Path

    from long_running_agent.config import Settings

type PowerAction = Literal["start", "resume", "suspend", "stop"]


TIMEOUT_STATUS = 124
REMOVED_OVERRIDES = (
    "GOOGLE_CREDENTIALS",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_BACKEND_CREDENTIALS",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT",
    "GOOGLE_IMPERSONATE_SERVICE_ACCOUNT_DELEGATES",
    "GOOGLE_BACKEND_IMPERSONATE_SERVICE_ACCOUNT_DELEGATES",
    "GOOGLE_OAUTH_ACCESS_TOKEN",
    "CLOUDSDK_AUTH_ACCESS_TOKEN",
)
EMPTY_OVERRIDES = (
    "CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "CLOUDSDK_AUTH_ACCESS_TOKEN_FILE",
)
INVENTORY_PREFIXES = (("projects", "describe"), ("compute", "instances", "describe"), ("compute", "instances", "list"))
AUTHENTICATION_FAILURES = (
    "reauthentication",
    "invalid_grant",
    "invalid_rapt",
    "do not currently have an active account",
)


class Runner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed: ...


def sanitized_environment(environment: Mapping[str, str]) -> dict[str, str]:
    child = dict(environment)
    for name in REMOVED_OVERRIDES:
        child.pop(name, None)
    for name in EMPTY_OVERRIDES:
        child[name] = ""
    child["CLOUDSDK_CORE_HTTP_TIMEOUT"] = "60"
    return child


class Cloud:
    def __init__(
        self,
        settings: Settings,
        *,
        runner: Runner = run,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings, self.runner, self.sleeper, self.clock = settings, runner, sleeper, clock

    def arguments(self, arguments: Sequence[str]) -> list[str]:
        command = [self.settings.gcloud_binary, "--project=" + self.settings.project]
        if self.settings.account:
            command.append("--account=" + self.settings.account)
        return [*command, *arguments]

    def execute(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        timeout: float = 75,
        check: bool = True,
    ) -> Completed:
        inventory = any(tuple(arguments[: len(prefix)]) == prefix for prefix in INVENTORY_PREFIXES)
        attempts = 3 if inventory else 1
        deadline = self.clock() + timeout
        for attempt in range(attempts):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise CommandError(self.settings.gcloud_binary, TIMEOUT_STATUS)
            result = self._attempt(arguments, capture_output=capture_output, timeout=remaining)
            if result.returncode == 0:
                return result
            stderr = (result.stderr or "").lower()
            transient = result.returncode == TIMEOUT_STATUS or any(
                marker in stderr
                for marker in (
                    "ssl",
                    "unexpected eof",
                    "connection reset",
                    "connectionerror",
                    "timed out",
                    "temporarily unavailable",
                    "service unavailable",
                    "status=[503]",
                    "status=[502]",
                    "status=[429]",
                )
            )
            if not inventory or not transient or attempt + 1 == attempts:
                if check:
                    raise self._error(result)
                return result
            self.sleeper(min(1 + attempt, max(0, deadline - self.clock())))
        message = "Cloud inventory retry limit exhausted"
        raise RuntimeError(message)

    def _error(self, result: Completed) -> RuntimeError:
        stderr = (result.stderr or "").lower()
        if not any(marker in stderr for marker in AUTHENTICATION_FAILURES):
            return CommandError(self.settings.gcloud_binary, result.returncode)
        login = [self.settings.gcloud_binary, "auth", "login"]
        if self.settings.account:
            login.append(self.settings.account)
        login.append("--force")
        # Cloud output can contain credentials. Explain the recognized failure
        # without echoing captured output or changing the user's account.
        return RuntimeError(f"Google Cloud sign-in needs refreshing.\nRun: {shlex.join(login)}\nThen run vm again.")

    def _attempt(self, arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        command = self.arguments(arguments)
        try:
            return self.runner(
                command,
                capture_output=capture_output,
                check=False,
                timeout=timeout,
                env=sanitized_environment(environment_snapshot()),
            )
        except ProcessTimeout:
            return CompletedProcess(command, TIMEOUT_STATUS, "", "timed out")
        except CommandError as error:
            if error.returncode != TIMEOUT_STATUS:
                raise
            return CompletedProcess(command, TIMEOUT_STATUS, "", "timed out")

    def state(self, *, timeout: float = 30) -> str:
        result = self.execute(
            [
                "compute",
                "instances",
                "describe",
                self.settings.instance,
                "--zone=" + self.settings.zone,
                "--format=value(status)",
            ],
            capture_output=True,
            timeout=timeout,
        )
        return result.stdout.strip()

    def change_power(self, action: PowerAction, *, asynchronous: bool = False, timeout: float = 300) -> None:
        if action not in {"start", "resume", "suspend", "stop"}:
            message = "Unsupported power operation"
            raise ValueError(message)
        arguments = ["compute", "instances", action, self.settings.instance, "--zone=" + self.settings.zone, "--quiet"]
        if asynchronous:
            arguments.append("--async")
        self.execute(arguments, timeout=min(timeout, 75 if asynchronous else 300))
