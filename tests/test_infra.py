from __future__ import annotations

import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from long_running_agent import infra
from long_running_agent.config import Settings
import pytest


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["fmt", "-check"], id="format"),
        pytest.param(["validate"], id="validate"),
        pytest.param(["version"], id="version"),
        pytest.param(["init", "-backend=false"], id="provider-only-init"),
        pytest.param([], id="default-help"),
        pytest.param(["-h"], id="short-help"),
        pytest.param(["--help"], id="long-help"),
        pytest.param(["-help"], id="native-help"),
        pytest.param(["-version"], id="native-version-flag"),
        pytest.param(["--version"], id="long-version-flag"),
        pytest.param(["plan", "-help"], id="plan-help"),
        pytest.param(["apply", "--help"], id="apply-help"),
    ],
)
def test_offline_terraform_does_not_request_credentials(arguments: list[str]) -> None:
    def refuse_cloud() -> str:
        pytest.fail("Offline Terraform must not initialize Google authentication")

    environment = infra.terraform_environment(
        Settings(),
        arguments,
        environment={"PATH": "/bin", "GOOGLE_APPLICATION_CREDENTIALS": "/unrelated-key"},
        token_provider=refuse_cloud,
    )
    assert environment["TF_IN_AUTOMATION"] == "1"
    assert "GOOGLE_APPLICATION_CREDENTIALS" not in environment
    assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in environment


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["plan"], id="plan"),
        pytest.param(["apply"], id="apply"),
        pytest.param(["plan", "-var=message=-help"], id="help-text-in-value"),
        pytest.param(["apply", "--", "-help"], id="help-named-plan-file"),
        pytest.param(["apply", "--", "--version"], id="version-named-plan-file"),
    ],
)
def test_cloud_terraform_uses_only_a_short_lived_child_token(arguments: list[str]) -> None:
    original = {
        "PATH": "/bin",
        "GOOGLE_APPLICATION_CREDENTIALS": "/other/key",
        "CLOUDSDK_AUTH_ACCESS_TOKEN": "wrong-identity",
    }
    child = infra.terraform_environment(
        Settings(), arguments, environment=original, token_provider=lambda: " selected-identity-token\n"
    )
    assert child["GOOGLE_OAUTH_ACCESS_TOKEN"] == "selected-identity-token"
    assert "CLOUDSDK_AUTH_ACCESS_TOKEN" not in child
    assert "GOOGLE_OAUTH_ACCESS_TOKEN" not in original
    with pytest.raises(RuntimeError, match="empty access token"):
        infra.terraform_environment(Settings(), arguments, environment={}, token_provider=lambda: "")


def test_module_resolution_fails_outside_a_checkout(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Terraform module"):
        infra.terraform_directory(tmp_path)
    module = tmp_path / "terraform"
    module.mkdir()
    (module / "main.tf").touch()
    nested = tmp_path / "packages/child"
    nested.mkdir(parents=True)
    assert infra.terraform_directory(nested) == module


@pytest.mark.parametrize("version", ["1.16.5", "1.18.0"])
def test_terraform_rejects_unpinned_native_versions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
) -> None:
    executable = tmp_path / "terraform"
    executable.write_text(f'#!{sys.executable}\nprint(\'{{"terraform_version":"{version}"}}\')\n')
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(infra.TerraformUnavailableError, match=r"1\.17\.0"):
        infra.find_terraform()
