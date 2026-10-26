from __future__ import annotations

from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

from long_running_agent.process import environment_snapshot


if TYPE_CHECKING:
    from collections.abc import Mapping

CloudIdentifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
# Connection runtime updates must preserve existing MCP installations.
RUNTIME_DIRECTORY = ".local/share/long-running-agent/terminal-tools/long-running-agent"
REMOTE_RUNTIME = f'"$HOME/{RUNTIME_DIRECTORY}/bin/lra"'


class Settings(BaseModel):
    """Connection coordinates and personal identity; this contains no credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    project: CloudIdentifier = "example-project"
    instance: CloudIdentifier = "agent-vm"
    zone: CloudIdentifier = "us-central1-a"
    account: str | None = None
    ssh_alias: str = "sarj-codex"
    ssh_key: Path = Field(default_factory=lambda: Path.home() / ".ssh/sarj_codex")
    gcloud_binary: str = "gcloud"

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str | None) -> str | None:
        if value is not None and ("@" not in value or any(character.isspace() for character in value)):
            message = "Use the email address of your own Google account"
            raise ValueError(message)
        return value

    @field_validator("ssh_alias")
    @classmethod
    def validate_ssh_alias(cls, value: str) -> str:
        if (
            not value
            or value.startswith("-")
            or any(
                character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
                for character in value
            )
        ):
            message = "SSH alias must be a single host name"
            raise ValueError(message)
        return value

    @field_validator("ssh_key")
    @classmethod
    def expand_ssh_key(cls, value: Path) -> Path:
        return value.expanduser().absolute()


def settings_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".config/long-running-agent/config.toml"


def load_settings(
    path: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    env = environment_snapshot() if environment is None else environment
    selected = (path or (Path(env["LRA_CONFIG"]) if "LRA_CONFIG" in env else settings_path())).expanduser()
    if (path is not None or "LRA_CONFIG" in env) and not selected.is_file():
        message = f"Configuration does not exist: {selected}"
        raise FileNotFoundError(message)
    values: dict[str, object] = {}
    if selected.exists():
        with selected.open("rb") as stream:
            values.update(tomllib.load(stream))
        values.pop("github_org", None)
    for field in Settings.model_fields:
        key = "LRA_" + field.upper()
        if key in env:
            values[field] = env[key]
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})
    return Settings.model_validate(values)
