from __future__ import annotations

from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator


if TYPE_CHECKING:
    from collections.abc import Mapping

CloudIdentifier = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
# Connection runtime updates must preserve existing MCP installations.
RUNTIME_DIRECTORY = ".local/share/long-running-agent/terminal-tools/long-running-agent"
REMOTE_RUNTIME = f'"$HOME/{RUNTIME_DIRECTORY}/bin/lra"'


class Settings(BaseModel):
    """Connection coordinates and personal identity; this contains no credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    account: str | None = Field(default=None, examples=["you@example.com"])
    project: CloudIdentifier = Field(default="example-project", examples=["your-project"])
    instance: CloudIdentifier = Field(default="agent-vm", examples=["your-vm"])
    zone: CloudIdentifier = Field(default="us-central1-a", examples=["us-central1-a"])

    ssh_alias: ClassVar[str] = "sarj-codex"
    gcloud_binary: ClassVar[str] = "gcloud"

    @property
    def ssh_key(self) -> Path:
        return Path.home() / ".ssh/sarj_codex"

    @field_validator("account")
    @classmethod
    def validate_account(cls, value: str | None) -> str | None:
        if value is not None and ("@" not in value or any(character.isspace() for character in value)):
            message = "Use the email address of your own Google account"
            raise ValueError(message)
        return value


def settings_path(home: Path | None = None) -> Path:
    return (Path.home() if home is None else home) / ".config/long-running-agent/config.toml"


def load_settings(
    path: Path | None = None,
    *,
    overrides: Mapping[str, object] | None = None,
) -> Settings:
    selected = (path or settings_path()).expanduser()
    if path is not None and not selected.is_file():
        message = f"Configuration does not exist: {selected}"
        raise FileNotFoundError(message)
    values: dict[str, object] = {}
    if selected.exists():
        with selected.open("rb") as stream:
            values.update(tomllib.load(stream))
        for retired in ("github_org", "ssh_alias", "ssh_key", "gcloud_binary"):
            values.pop(retired, None)
    if overrides:
        values.update({key: value for key, value in overrides.items() if value is not None})
    return Settings.model_validate(values)
