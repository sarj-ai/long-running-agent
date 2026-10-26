from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from long_running_agent.config import Settings, load_settings
from pydantic import ValidationError
import pytest


def test_profile_and_internal_connection_coordinates_have_clear_precedence(tmp_path: Path) -> None:
    profile = tmp_path / "config.toml"
    profile.write_text('account="profile@example.com"\nproject="profile-project"\n')
    settings = load_settings(
        profile,
        overrides={"account": "explicit@example.com", "zone": None},
    )
    assert settings.account == "explicit@example.com"
    assert settings.project == "profile-project"
    assert set(Settings.model_fields) == {"account", "project", "instance", "zone"}


def test_explicit_missing_profile_fails_and_toml_never_evaluates_shell(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"
    with pytest.raises(FileNotFoundError):
        load_settings(missing)
    marker = tmp_path / "never-created"
    profile = tmp_path / "literal.toml"
    profile.write_text(f'project="$(touch {marker})"\n')
    with pytest.raises(ValidationError):
        load_settings(profile)
    assert not marker.exists()


@pytest.mark.parametrize("retired", ["github_org", "ssh_alias", "ssh_key", "gcloud_binary"])
def test_retired_preferences_do_not_break_an_existing_profile(tmp_path: Path, retired: str) -> None:
    profile = tmp_path / "config.toml"
    original = f'{retired}="example"\naccount="person@example.com"\n'
    profile.write_text(original)
    assert load_settings(profile).account == "person@example.com"
    assert profile.read_text() == original


@pytest.mark.parametrize("retired", ["ssh_alias", "ssh_key", "gcloud_binary"])
def test_fixed_transport_defaults_are_not_configurable(retired: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Settings.model_validate({retired: "-oProxyCommand"})


def test_invalid_client_profile_does_not_echo_private_extra_fields_or_rewrite_source(tmp_path: Path) -> None:
    profile = tmp_path / "private-profile.toml"
    original = 'api_key = "fixture-private-profile-value"\n'
    profile.write_text(original, encoding="utf-8")
    with pytest.raises(ValidationError) as caught:
        load_settings(profile)
    assert "fixture-private-profile-value" not in str(caught.value)
    assert profile.read_text(encoding="utf-8") == original
