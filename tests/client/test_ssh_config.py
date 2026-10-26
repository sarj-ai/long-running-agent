from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

import shutil
import subprocess

from long_running_agent.client.ssh_config import LEGACY_END, LEGACY_START, install, managed_configuration, render
from long_running_agent.config import Settings
import pytest


def test_managed_host_precedes_includes_and_broad_defaults_without_overwriting_them(tmp_path: Path) -> None:
    settings = Settings(account="person@example.com", ssh_key=tmp_path / "key with spaces")
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    includes = ssh / "defaults.conf"
    includes.write_text("Host *\n ForwardAgent yes\n ServerAliveInterval 999\n")
    original = f"# Personal comment\n\nInclude {includes}\nHost other\n HostName elsewhere\n"
    (ssh / "config").write_text(original)
    installed = install(settings, "person_example_com", home=tmp_path)
    text = installed.read_text()
    assert text.startswith("# Personal comment\n\n# BEGIN LONG RUNNING AGENT")
    assert text.index("ForwardAgent no") < text.index("Include ")
    assert original[original.index("Include ") :] in text
    assert next(ssh.glob("config.lra-backup-*")).read_text() == original
    assert installed.stat().st_mode & 0o777 == 0o600
    if shutil.which("ssh"):
        effective = subprocess.check_output(["ssh", "-G", "-F", str(installed), settings.ssh_alias], text=True)
        assert "forwardagent no\n" in effective
        assert "serveraliveinterval 20\n" in effective
        assert "requesttty false\n" in effective or "requesttty auto\n" in effective
        assert "remotecommand" not in effective


def test_migrates_legacy_block_and_is_idempotent() -> None:
    settings = Settings()
    original = f"{LEGACY_START}\nHost sarj-codex\n User old\nHost *\n{LEGACY_END}\nHost another\n HostName elsewhere\n"
    block = render(settings, "new_user")
    new = managed_configuration(original, block, settings.ssh_alias)
    assert LEGACY_START not in new
    assert new.count("Host sarj-codex\n") == 1
    assert managed_configuration(new, block, settings.ssh_alias) == new


@pytest.mark.parametrize("old", [f"{LEGACY_START}\nHost sarj-codex\n", "Host sarj-codex\n User unmanaged\n"])
def test_refuses_ambiguous_or_malformed_configuration(old: str) -> None:
    with pytest.raises(ValueError, match=r"malformed|unmanaged"):
        managed_configuration(old, render(Settings(), "user"), "sarj-codex")


def test_proxy_uses_installed_module_and_quotes_literal_percent_and_spaces(tmp_path: Path) -> None:
    block = render(Settings(account="person@example.com"), "user", python=tmp_path / "50% Python/bin/python")
    assert "50%% Python" in block
    assert " -m long_running_agent.cli " in block
    assert " vm tunnel %p" in block
    assert "ForwardAgent no" in block
    assert "RequestTTY" not in block
    assert "RemoteCommand" not in block
