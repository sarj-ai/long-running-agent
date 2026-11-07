from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from long_running_agent.cli import connect_app
from long_running_agent.client.azure import AzureCloud, AzureSettings
from long_running_agent.client.controller import Client
from long_running_agent.client.ssh_config import managed_configuration, render
from long_running_agent.config import Settings, load_settings
from long_running_agent.process import CommandError
from pydantic import ValidationError
import pytest
from typer.testing import CliRunner


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


SUBSCRIPTION = "12345678-1234-1234-1234-123456789abc"


def _settings() -> AzureSettings:
    return AzureSettings(
        subscription=SUBSCRIPTION,
        resource_group="personal-agent",
        instance="personal-azure",
        username="personal_user",
        hostname="personal-azure.example.com",
    )


class _AzureRunner:
    def __init__(self, responses: list[tuple[int, str, str]]) -> None:
        self.responses = responses
        self.commands: list[list[str]] = []
        self.options: list[dict[str, object]] = []

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
        self.commands.append(list(arguments))
        self.options.append(
            {
                "capture_output": capture_output,
                "check": check,
                "timeout": timeout,
                "input_text": input_text,
                "cwd": cwd,
                "env": env,
            }
        )
        return subprocess.CompletedProcess(list(arguments), *self.responses.pop(0))


def test_azure_profile_is_separate_and_keeps_original_google_settings_minimal(tmp_path: Path) -> None:
    profile = tmp_path / "azure.toml"
    profile.write_text(
        f'subscription="{SUBSCRIPTION}"\nresource_group="personal-agent"\n'
        'instance="personal-azure"\nusername="personal_user"\nhostname="personal-azure.example.com"\n'
    )
    selected = load_settings(profile, model=AzureSettings)
    assert selected.project == SUBSCRIPTION
    assert selected.zone == "personal-agent"
    assert selected.ssh_alias == "sarj-codex-azure"
    assert Settings().ssh_alias == "sarj-codex"
    assert set(Settings.model_fields) == {"account", "project", "instance", "zone"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subscription", "not-a-subscription"),
        ("resource_group", "group;touch bad"),
        ("username", "-root"),
        ("username", "user\ncommand"),
        ("hostname", "host;touch bad"),
        ("hostname", "https://host.example.com"),
        ("hostname", "--hostname"),
        ("hostname", "host.example.com\nProxyCommand bad"),
    ],
)
def test_invalid_azure_coordinates_are_rejected(field: str, value: str) -> None:
    values = _settings().model_dump(by_alias=True)
    values[field] = value
    with pytest.raises(ValidationError):
        AzureSettings.model_validate(values)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("running", "RUNNING"),
        ("starting", "PROVISIONING"),
        ("stopped", "TERMINATED"),
        ("deallocated", "TERMINATED"),
        ("stopping", "STOPPING"),
        ("deallocating", "STOPPING"),
        ("hibernated", "SUSPENDED"),
        ("unexpected", "UNKNOWN"),
    ],
)
def test_azure_inventory_uses_exact_subscription_and_native_instance_view(state: str, expected: str) -> None:
    runner = _AzureRunner([(0, f"PowerState/{state}\n", "")])
    assert AzureCloud(_settings(), runner=runner).state(timeout=7) == expected
    command = runner.commands[0]
    assert command[:4] == ["az", "rest", "--method", "get"]
    assert command[-3:] == ["--subscription", SUBSCRIPTION, "--only-show-errors"]
    assert f"/subscriptions/{SUBSCRIPTION}/resourceGroups/personal-agent/" in command[5]
    assert "gcloud" not in command
    timeout = runner.options[0]["timeout"]
    assert isinstance(timeout, float)
    assert timeout <= 7


def test_wake_requests_one_azure_start_and_preserves_subscription() -> None:
    runner = _AzureRunner([(0, "PowerState/deallocated", ""), (0, "", ""), (0, "PowerState/running", "")])
    controller = Client(_settings(), runner=runner, cloud_factory=AzureCloud, sleeper=lambda _seconds: None)
    controller.wake()
    starts = [command for command in runner.commands if command[1:3] == ["vm", "start"]]
    assert len(starts) == 1
    assert "--no-wait" in starts[0]
    assert starts[0][-3:] == ["--subscription", SUBSCRIPTION, "--only-show-errors"]


def test_failed_azure_power_mutation_is_not_retried() -> None:
    runner = _AzureRunner([(1, "private-output", "connection reset")])
    with pytest.raises(CommandError, match="az exited"):
        AzureCloud(_settings(), runner=runner).change_power("start")
    assert len(runner.commands) == 1


def test_expired_azure_login_never_echoes_tokens_or_changes_identity() -> None:
    private = "private-access-value"
    runner = _AzureRunner([(1, private, "AADSTS700082 " + private)])
    with pytest.raises(RuntimeError, match="Run: az login") as caught:
        AzureCloud(_settings(), runner=runner).state()
    assert private not in str(caught.value)
    assert len(runner.commands) == 1


def test_wrong_azure_account_fails_before_power_or_ssh_enrollment() -> None:
    selected = _settings().model_copy(update={"account": "expected@example.com"})
    runner = _AzureRunner([(0, "someone-else@example.com", "")])
    with pytest.raises(RuntimeError, match="different account"):
        AzureCloud(selected, runner=runner).change_power("start")
    assert runner.commands[0][1:3] == ["account", "show"]
    assert len(runner.commands) == 1


def test_cloudflare_login_hides_bearer_output_before_background_ssh() -> None:
    runner = _AzureRunner([(1, "private-bearer-value", "private-error")])
    with pytest.raises(RuntimeError, match="Cloudflare Access sign-in failed") as caught:
        AzureCloud(_settings(), runner=runner).authenticate_tunnel()
    assert "private-bearer-value" not in str(caught.value)
    assert "private-error" not in str(caught.value)
    assert runner.commands == [["cloudflared", "access", "login", "https://personal-azure.example.com"]]


def test_azure_ssh_block_keeps_google_connections_and_disables_agent_forwarding() -> None:
    google = render(Settings(), "google_user")
    selected = _settings()
    block = render(selected, selected.username, proxy_command=AzureCloud(selected).ssh_proxy())
    combined = managed_configuration(google, block, selected.ssh_alias)
    assert "Host sarj-codex\n" in combined
    assert "Host sarj-codex-azure\n" in combined
    assert "ProxyCommand cloudflared access ssh --hostname personal-azure.example.com" in block
    assert "ForwardAgent no" in block
    assert f"HostKeyAlias sarj-codex-azure.{SUBSCRIPTION}.personal-agent" in block
    assert managed_configuration(combined, block, selected.ssh_alias) == combined


def test_azure_help_and_invalid_mode_do_not_load_or_modify_google_profile(tmp_path: Path) -> None:
    profile = tmp_path / ".config/long-running-agent/config.toml"
    profile.parent.mkdir(parents=True)
    profile.write_text('private_key="never-print-this-value"\n')
    runner = CliRunner()
    environment = {"HOME": str(tmp_path)}
    assert runner.invoke(connect_app, ["azure", "--help"], env=environment).exit_code == 0
    result = runner.invoke(connect_app, ["azure", "invalid"], env=environment)
    assert result.exit_code == 2
    assert "never-print-this-value" not in result.output
    assert profile.read_text() == 'private_key="never-print-this-value"\n'


@pytest.mark.parametrize("target", ["bad;command", "space name", "-option", "x" * 65])
def test_invalid_azure_session_is_rejected_before_login_or_profile_access(tmp_path: Path, target: str) -> None:
    result = CliRunner().invoke(connect_app, ["azure", "terminal", "--", target], env={"HOME": str(tmp_path)})
    assert result.exit_code == 1
    assert isinstance(result.exception, ValueError)
    assert "Zellij session name" in str(result.exception)


def test_transient_azure_inventory_failure_retries_without_a_power_mutation() -> None:
    runner = _AzureRunner([(1, "", "SSL unexpected EOF"), (0, "PowerState/running", "")])
    assert AzureCloud(_settings(), runner=runner, sleeper=lambda _seconds: None).state() == "RUNNING"
    assert len(runner.commands) == 2
    assert all(command[1] == "rest" for command in runner.commands)
