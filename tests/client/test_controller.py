from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

import subprocess

from long_running_agent.client.cloud import Cloud, PowerAction, sanitized_environment
from long_running_agent.client.controller import Client
from long_running_agent.config import REMOTE_RUNTIME, Settings
from long_running_agent.process import CommandError
import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, amount: float) -> None:
        self.now += amount


class ScriptedRunner:  # sarj-noqa: SARJ428 -- Only this module scripts cloud and SSH responses; it has no cross-module users.
    def __init__(self, responses: Sequence[tuple[int, str, str] | BaseException]) -> None:
        self.responses = list(responses)
        self.commands: list[list[str]] = []
        self.options: list[dict[str, object]] = []
        self.environments: list[Mapping[str, str] | None] = []

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
    ) -> subprocess.CompletedProcess[str]:
        self.options.append(
            {"capture_output": capture_output, "timeout": timeout, "input_text": input_text, "cwd": cwd}
        )
        self.commands.append(list(arguments))
        self.environments.append(env)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        result = subprocess.CompletedProcess(list(arguments), *response)
        if check and result.returncode:
            raise CommandError(arguments[0], result.returncode)
        return result


def test_only_transient_inventory_reads_retry_and_sanitize_identity() -> None:
    runner = ScriptedRunner([(1, "", "SSL unexpected EOF"), (0, "RUNNING", "")])
    clock = Clock()
    cloud = Cloud(Settings(account="person@example.com"), runner=runner, clock=clock, sleeper=clock.sleep)
    assert cloud.state() == "RUNNING"
    assert len(runner.commands) == 2
    assert "--account=person@example.com" in runner.commands[0]
    assert runner.environments[0] is not None
    assert not runner.environments[0]["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"]
    assert clock.now == 1


@pytest.mark.parametrize("operation", ["start", "resume"])
def test_power_mutations_never_retry_even_on_transient_error(operation: PowerAction) -> None:
    runner = ScriptedRunner([(1, "", "SSL unexpected EOF")])
    cloud = Cloud(Settings(), runner=runner)
    with pytest.raises(CommandError, match="gcloud exited"):
        cloud.change_power(operation)
    assert len(runner.commands) == 1


def test_permission_failure_does_not_retry_or_mutate() -> None:
    runner = ScriptedRunner([(1, "", "PERMISSION_DENIED")])
    cloud = Cloud(Settings(), runner=runner)
    with pytest.raises(CommandError, match="gcloud exited"):
        cloud.state()
    assert len(runner.commands) == 1


@pytest.mark.parametrize("failure", ["Reauthentication failed", "invalid_grant", "invalid_rapt"])
def test_expired_google_login_explains_recovery_without_echoing_credentials(failure: str) -> None:
    private = "private-authentication-value-do-not-print"
    runner = ScriptedRunner([(1, private, f"{failure}: {private}")])
    cloud = Cloud(Settings(account="person@example.com"), runner=runner)
    with pytest.raises(RuntimeError, match="Google Cloud sign-in needs refreshing") as caught:
        cloud.state()
    assert "gcloud auth login person@example.com --force" in str(caught.value)
    assert "Then run vm again" in str(caught.value)
    assert private not in str(caught.value)
    assert len(runner.commands) == 1


def test_unchecked_authentication_failure_remains_available_to_caller() -> None:
    runner = ScriptedRunner([(1, "", "Reauthentication failed")])
    result = Cloud(Settings(), runner=runner).execute(
        ["projects", "describe", "example"], capture_output=True, check=False
    )
    assert result.returncode == 1
    assert len(runner.commands) == 1


def test_wake_requests_start_once_while_async_operation_reports_old_state() -> None:
    runner = ScriptedRunner(
        [(0, "TERMINATED", ""), (0, "", ""), (0, "TERMINATED", ""), (0, "STAGING", ""), (0, "RUNNING", "")]
    )
    clock = Clock()
    controller = Client(Settings(), runner=runner, sleeper=clock.sleep, clock=clock)
    controller.wake()
    assert len([command for command in runner.commands if "start" in command]) == 1
    assert clock.now == 6


def test_stale_master_stops_listener_without_killing_existing_channels() -> None:
    runner = ScriptedRunner([(0, "", ""), subprocess.TimeoutExpired("ssh", 12), (0, "", ""), (0, "RUNNING", "")])
    launches: list[list[str]] = []

    def launch(arguments: Sequence[str]) -> int:
        launches.append(list(arguments))
        return 0

    Client(Settings(), runner=runner, master_launcher=launch).ensure_connection()
    assert runner.commands[2][:3] == ["ssh", "-O", "stop"]
    assert not any("exit" in command for command in runner.commands)
    assert launches == [["ssh", "-Nf", "-o", "ConnectTimeout=20", "-o", "BatchMode=yes", "sarj-codex"]]


def test_child_credential_overrides_removed_without_mutating_parent() -> None:
    source = {
        "PATH": "/bin",
        "GOOGLE_APPLICATION_CREDENTIALS": "/private/key",
        "CLOUDSDK_AUTH_ACCESS_TOKEN": "secret",
        "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE": "/other/key",
        "KEEP": "value",
    }
    child = sanitized_environment(source)
    assert child["KEEP"] == "value"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in child
    assert "CLOUDSDK_AUTH_ACCESS_TOKEN" not in child
    assert not child["CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE"]
    assert source["GOOGLE_APPLICATION_CREDENTIALS"] == "/private/key"


def test_transient_inventory_timeout_uses_bounded_retry_without_replaying_mutation() -> None:
    runner = ScriptedRunner([subprocess.TimeoutExpired("gcloud", 1), (0, "RUNNING", "")])
    clock = Clock()
    assert Cloud(Settings(), runner=runner, clock=clock, sleeper=clock.sleep).state() == "RUNNING"
    assert len(runner.commands) == 2


def test_raw_ssh_keeps_program_arguments_literal(tmp_path: Path) -> None:
    runner = ScriptedRunner([(0, "", ""), (0, "", ""), (0, "", "")])
    controller = Client(Settings(), runner=runner)
    assert controller.shell(["printf", "%s", "a;touch bad"]) == 0
    remote = runner.commands[-1][-1]
    result = subprocess.run(["/bin/sh", "-c", remote], text=True, capture_output=True, check=True, cwd=tmp_path)
    assert result.stdout == "a;touch bad"
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize(
    "failure", [KeyboardInterrupt(), CommandError("ssh", 124)], ids=["keyboard-interrupt", "transport-timeout"]
)
def test_interrupted_shell_still_prints_its_vm_and_project(
    capsys: pytest.CaptureFixture[str], failure: BaseException
) -> None:
    runner = ScriptedRunner([(0, "", ""), (0, "", ""), failure])
    with pytest.raises(type(failure)):
        Client(Settings(), runner=runner).shell([])
    output = capsys.readouterr().err
    assert "VM: agent-vm" in output
    assert "Project: example-project" in output
    assert "Reconnect: vm" in output


def test_setup_only_configures_the_native_runtime() -> None:
    class NativeController(Client):
        def configure_ssh(self) -> Path:
            return self.home / ".ssh/config"

        def ensure_connection(self) -> None:
            pass

    runner = ScriptedRunner([(0, "", "")])
    NativeController(Settings(), runner=runner).setup()
    assert len(runner.commands) == 1
    assert runner.commands[0] == ["ssh", "-T", "sarj-codex", f"exec {REMOTE_RUNTIME} runtime setup"]
    assert runner.options[0]["timeout"] == 300
    assert runner.options[0]["input_text"] is None
