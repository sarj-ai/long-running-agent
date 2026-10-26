from __future__ import annotations

import json
from typing import TYPE_CHECKING

from long_running_agent.runtime.doctor import Reporter, check_adc, repository_checkouts
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from pathlib import Path


def test_doctor_does_not_certify_wrong_expected_identity(capsys: pytest.CaptureFixture[str]) -> None:
    report = Reporter()
    assert not report.identity("Google account", ["/bin/echo", "other@example.invalid"], "owner@example.invalid")
    assert report.failures == ["Google account"]
    assert "expected owner@example.invalid" in capsys.readouterr().out


def test_token_refresh_check_does_not_print_child_token(capsys: pytest.CaptureFixture[str]) -> None:
    report = Reporter()
    secret = "fixture-token-never-shown"
    assert report.check("Google ADC refresh", ["/bin/echo", secret])
    output = capsys.readouterr().out
    assert "[OK] Google ADC refresh" in output
    assert secret not in output


def test_checkout_discovery_is_organization_neutral_and_skips_worktrees_and_aliases(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    expected = [paths.code / "direct", paths.code / "organization/project"]
    excluded = [
        paths.code / "worktrees/task",
        paths.code / ".private/project",
        paths.code / "organization/.private",
        paths.code / "other/deeper/project",
    ]
    for checkout in [*expected, *excluded]:
        (checkout / ".git").mkdir(parents=True)
    (paths.code / "alias").symlink_to(expected[0], target_is_directory=True)
    assert list(repository_checkouts(paths)) == expected
    assert list(repository_checkouts(paths, "organization")) == [expected[1]]


@pytest.mark.parametrize("organization", ["..", ".", "../outside", "/outside", "nested/organization"])
def test_configured_organization_cannot_escape_its_code_directory(tmp_path: Path, organization: str) -> None:
    with pytest.raises(ValueError, match="one organization directory"):
        list(repository_checkouts(RuntimePaths(tmp_path), organization))


def test_checkout_discovery_does_not_create_a_missing_code_directory(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path)
    assert list(repository_checkouts(paths)) == []
    assert not paths.code.exists()


@pytest.mark.parametrize("quota", ["", "expected-project", "different-project"])
def test_adc_without_a_quota_project_is_valid_but_a_different_configured_project_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], quota: str
) -> None:
    executable = tmp_path / "gcloud"
    executable.write_text("#!/bin/sh\nprintf '%s\\n' fixture-access-token\n", encoding="utf-8")
    executable.chmod(0o700)
    paths = RuntimePaths(tmp_path)
    credentials = paths.home / ".config/gcloud/application_default_credentials.json"
    credentials.parent.mkdir(parents=True)
    credentials.write_text(json.dumps({"quota_project_id": quota}), encoding="utf-8")
    report = Reporter(environment={"PATH": str(tmp_path)})
    check_adc(report, paths, "expected-project")
    assert report.failures == (["Google ADC quota project"] if quota == "different-project" else [])
    output = capsys.readouterr().out
    assert "fixture-access-token" not in output
    if not quota:
        assert "not configured; optional" in output
