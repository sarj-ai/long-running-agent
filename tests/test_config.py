from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from long_running_agent.config import Settings, load_settings
from pydantic import ValidationError
import pytest


def test_profile_environment_and_explicit_identity_have_clear_precedence(tmp_path: Path) -> None:
    profile = tmp_path / "config.toml"
    profile.write_text('account="profile@example.com"\nproject="profile-project"\n')
    settings = load_settings(
        profile,
        environment={"LRA_ACCOUNT": "environment@example.com"},
        overrides={"account": "explicit@example.com", "zone": None},
    )
    assert settings.account == "explicit@example.com"
    assert settings.project == "profile-project"
    with pytest.raises(ValidationError):
        load_settings(profile, environment={"LRA_SSH_ALIAS": "host\nProxyCommand bad"})


def test_explicit_missing_profile_fails_and_toml_never_evaluates_shell(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(FileNotFoundError):
        load_settings(environment={"LRA_CONFIG": str(missing)})
    marker = tmp_path / "never-created"
    profile = tmp_path / "literal.toml"
    profile.write_text(f'gcloud_binary="$(touch {marker})"\n')
    assert load_settings(profile, environment={}).gcloud_binary.startswith("$(touch")
    assert not marker.exists()


def test_retired_repository_preference_does_not_break_an_existing_profile(tmp_path: Path) -> None:
    profile = tmp_path / "config.toml"
    original = 'github_org="example"\naccount="person@example.com"\n'
    profile.write_text(original)
    assert load_settings(profile, environment={}).account == "person@example.com"
    assert profile.read_text() == original


@pytest.mark.parametrize("alias", ["-V", "-F", "-oProxyCommand"])
def test_ssh_alias_rejects_values_openssh_treats_as_options(alias: str) -> None:
    with pytest.raises(ValidationError, match="SSH alias must be a single host name"):
        Settings(ssh_alias=alias)


def test_invalid_client_profile_does_not_echo_private_extra_fields_or_rewrite_source(tmp_path: Path) -> None:
    profile = tmp_path / "private-profile.toml"
    original = 'api_key = "fixture-private-profile-value"\n'
    profile.write_text(original, encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_settings(profile, environment={})
    assert "fixture-private-profile-value" not in str(caught.value)
    assert profile.read_text(encoding="utf-8") == original
