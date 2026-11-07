from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal

from pydantic import ConfigDict, Field
import typer

from long_running_agent.client.cloud import Cloud, PowerAction
from long_running_agent.client.controller import Client
from long_running_agent.client.ssh_config import install
from long_running_agent.client.terminal import validate_session_name
from long_running_agent.config import CloudIdentifier, Settings, load_settings, settings_path
from long_running_agent.host_connection import website_url
from long_running_agent.process import CommandError, Completed


if TYPE_CHECKING:
    from collections.abc import Sequence


class AzureSettings(Settings):
    """An Azure VM and its private Cloudflare SSH route; no credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True, validate_by_name=True)

    project: str = Field(
        alias="subscription",
        pattern=r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$",
        examples=["12345678-1234-1234-1234-123456789abc"],
    )
    zone: CloudIdentifier = Field(alias="resource_group", examples=["your-agent-resources"])
    username: Annotated[str, Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$", examples=["your_linux_user"])]
    hostname: Annotated[
        str,
        Field(
            pattern=r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$",
            max_length=253,
            examples=["your-agent.example.com"],
        ),
    ]

    ssh_alias: ClassVar[str] = "sarj-codex-azure"
    gcloud_binary: ClassVar[str] = "az"


class AzureCloud(Cloud):
    connect_prefix: ClassVar[tuple[str, ...]] = ("sarj-connect", "azure")
    account_label: ClassVar[str] = "active Azure CLI account"
    inventory_prefixes: ClassVar[tuple[tuple[str, ...], ...]] = (("rest", "--method", "get"), ("account", "show"))

    def azure_settings(self) -> AzureSettings:
        return AzureSettings.model_validate(self.settings.model_dump())

    def arguments(self, arguments: Sequence[str]) -> list[str]:
        return ["az", *arguments, "--subscription", self.azure_settings().project, "--only-show-errors"]

    def _error(self, result: Completed) -> RuntimeError:
        stderr = (result.stderr or "").lower()
        if any(marker in stderr for marker in ("az login", "aadsts", "refresh token has expired")):
            return RuntimeError("Azure sign-in needs refreshing.\nRun: az login\nThen run sarj-connect azure again.")
        return CommandError("az", result.returncode)

    def verify_account(self, *, timeout: float = 30) -> None:
        if self.settings.account is None:
            return
        result = self.execute(
            ["account", "show", "--query", "user.name", "--output", "tsv"], capture_output=True, timeout=timeout
        )
        if result.stdout.strip().casefold() != self.settings.account.casefold():
            message = (
                "The Azure subscription is signed in as a different account; use your own az login before connecting."
            )
            raise RuntimeError(message)

    def state(self, *, timeout: float = 30) -> str:
        started = self.clock()
        self.verify_account(timeout=timeout)
        remaining = timeout - (self.clock() - started)
        if remaining <= 0:
            raise CommandError(self.settings.gcloud_binary, 124)
        settings = self.settings
        resource = (
            f"/subscriptions/{settings.project}/resourceGroups/{settings.zone}"
            f"/providers/Microsoft.Compute/virtualMachines/{settings.instance}"
        )
        result = self.execute(
            [
                "rest",
                "--method",
                "get",
                "--url",
                "https://management.azure.com" + resource + "/instanceView?api-version=2025-04-01",
                "--query",
                "statuses[?starts_with(code, 'PowerState/')].code | [0]",
                "--output",
                "tsv",
            ],
            capture_output=True,
            timeout=remaining,
        )
        states = {
            "PowerState/running": "RUNNING",
            "PowerState/starting": "PROVISIONING",
            "PowerState/stopped": "TERMINATED",
            "PowerState/deallocated": "TERMINATED",
            "PowerState/stopping": "STOPPING",
            "PowerState/deallocating": "STOPPING",
            "PowerState/hibernated": "SUSPENDED",
        }
        return states.get(result.stdout.strip(), "UNKNOWN")

    def change_power(self, action: PowerAction, *, asynchronous: bool = False, timeout: float = 300) -> None:
        if action not in {"start", "resume"}:
            message = "Unsupported power operation"
            raise ValueError(message)
        self.verify_account()
        arguments = ["vm", "start", "--resource-group", self.settings.zone, "--name", self.settings.instance]
        if asynchronous:
            arguments.append("--no-wait")
        self.execute(arguments, timeout=min(timeout, 75 if asynchronous else 300))

    def location(self) -> str:
        return f"Subscription: {self.settings.project} | Resource group: {self.settings.zone}"

    def ssh_proxy(self) -> list[str]:
        return ["cloudflared", "access", "ssh", "--hostname", self.azure_settings().hostname]

    def authenticate_tunnel(self) -> None:
        hostname = self.azure_settings().hostname
        # Native login opens the user's browser before SSH backgrounds its
        # master. Capture its output because cloudflared can print a bearer JWT.
        result = self.runner(
            ["cloudflared", "access", "login", "https://" + hostname],
            capture_output=True,
            check=False,
            timeout=300,
        )
        if result.returncode:
            message = "Cloudflare Access sign-in failed. Run cloudflared access login https://" + hostname
            raise RuntimeError(message)

    def ssh_username(self, key: Path) -> str:
        self.verify_account()
        settings = self.azure_settings()
        self.execute(
            [
                "vm",
                "user",
                "update",
                "--resource-group",
                settings.zone,
                "--name",
                settings.instance,
                "--username",
                settings.username,
                "--ssh-key-value",
                str(key.with_suffix(".pub")),
            ],
            timeout=300,
        )
        return settings.username


def connect(
    mode: Annotated[
        Literal["terminal", "browser"], typer.Argument(help="Open the Azure terminal or browser.")
    ] = "terminal",
    target: Annotated[str | None, typer.Argument(help="A terminal session name or browser URL.")] = None,
    *,
    no_open: Annotated[bool, typer.Option("--no-open", help="Print the forwarded browser address.")] = False,
) -> None:
    if no_open and mode != "browser":
        message = "--no-open applies to the Azure browser."
        raise ValueError(message)
    if mode == "terminal":
        validate_session_name(target or "sarj-home")
    elif target is not None:
        website_url(target)
    settings = load_settings(settings_path().with_name("azure.toml"), model=AzureSettings)
    cloud = AzureCloud(settings)
    controller = Client(settings, cloud_factory=AzureCloud)
    cloud.authenticate_tunnel()
    config = Path.home() / ".ssh/config"
    old = config.read_text() if config.exists() else ""
    marker = f"# BEGIN LONG RUNNING AGENT {settings.ssh_alias}\n"
    if not settings.ssh_key.exists() or marker not in old:
        controller.wake()
        controller.configure_ssh()
    else:
        install(settings, cloud.azure_settings().username, proxy_command=cloud.ssh_proxy())
    code = (
        controller.browser(no_open=no_open, url=target)
        if mode == "browser"
        else controller.terminal(target or "sarj-home")
    )
    raise typer.Exit(code)
