from __future__ import annotations

import shlex
import subprocess
import sys
from typing import TYPE_CHECKING

from long_running_agent.cli import app, connect_app, vm_arguments
from long_running_agent.client.controller import Client
from long_running_agent.config import Settings
from pydantic import RootModel
import pytest
from rich.text import Text
from typer.testing import CliRunner


if TYPE_CHECKING:
    from pathlib import Path


class SshCalls(RootModel[list[list[str]]]):
    pass


@pytest.mark.parametrize("session", [None, "project-work"], ids=["default-session", "selected-session"])
def test_actual_terminal_cli_preserves_interactive_transport_and_selected_session(
    tmp_path: Path, session: str | None
) -> None:
    record = tmp_path / "calls.jsonl"
    home = tmp_path / "home"
    local_bin = home / ".local/bin"
    local_bin.mkdir(parents=True)
    selected = tmp_path / "zellij.json"
    zellij = local_bin / "zellij"
    zellij.write_text(
        f"#!{sys.executable}\nimport json,pathlib,sys\n"
        f"pathlib.Path({str(selected)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        "sys.exit(255)\n",
        encoding="utf-8",
    )
    zellij.chmod(0o755)
    ssh = tmp_path / "ssh"
    ssh.write_text(
        f"#!{sys.executable}\nimport json,pathlib,subprocess,sys\n"
        f"with pathlib.Path({str(record)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps(sys.argv[1:])+'\\n')\n"
        "sys.exit(subprocess.run(['/bin/sh','-c',sys.argv[-1]],check=False).returncode if '-tt' in sys.argv else 0)\n",
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    arguments = ["--account", "developer@example.com", "terminal", "--no-host", *([session] if session else [])]
    result = CliRunner().invoke(connect_app, arguments, env={"PATH": str(tmp_path), "HOME": str(home)})
    assert result.exit_code == 255
    calls = SshCalls.model_validate_json("[" + ",".join(record.read_text().splitlines()) + "]").root
    assert len(calls) == 3
    assert calls[-1][:2] == ["-tt", "sarj-codex"]
    expected = ["attach", "--create", session or "sarj-home"]
    assert shlex.split(calls[-1][-1])[-len(expected) :] == expected
    assert SshCalls.model_validate_json("[" + selected.read_text() + "]").root == [expected]
    assert "VM: agent-vm" in result.output
    assert "Project: example-project" in result.output
    assert "Account: developer@example.com" in result.output
    assert f"Reconnect: sarj-connect terminal {session or 'sarj-home'}" in result.output


@pytest.mark.parametrize("session", ["", "-option", "space name", "sarj;touch bad", "line\nbreak", "x" * 65])
def test_invalid_terminal_session_fails_before_connection(session: str) -> None:
    class NoConnection(Client):
        def ensure_connection(self) -> None:
            pytest.fail("Invalid session attempted an SSH connection")

    with pytest.raises(ValueError, match="Zellij session name"):
        NoConnection(Settings()).terminal(session)


def test_vm_entrypoint_preserves_caller_arguments_after_disconnect(tmp_path: Path) -> None:
    ssh = tmp_path / "ssh"
    ssh.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(255 if '-tt' in sys.argv else 0)\n")
    ssh.chmod(0o700)
    program = (
        "import sys\nfrom long_running_agent.cli import vm_main\n"
        "original=sys.argv.copy()\n"
        "try: vm_main()\n"
        "except SystemExit as error: assert error.code == 255\n"
        "else: raise AssertionError('Expected disconnected transport')\n"
        "assert sys.argv == original\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, "terminal", "--no-host"],
        env={"PATH": str(tmp_path), "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Reconnect:" in result.stderr


def test_connect_help_lists_connection_modes_and_existing_vm_aliases_stay_available() -> None:
    runner = CliRunner()
    help_result = runner.invoke(connect_app, ["--help"], color=False)
    assert help_result.exit_code == 0
    help_text = Text.from_ansi(help_result.output).plain
    assert "ui" not in help_text
    assert "terminal" in help_text
    assert "browser" in help_text
    assert "--account" not in help_text
    assert "--config" not in help_text
    assert vm_arguments([]) == ["vm", "terminal"]
    assert vm_arguments(["terminal"]) == ["vm", "terminal"]
    assert runner.invoke(connect_app, ["ui"]).exit_code == 2
    assert vm_arguments(["browser"]) == ["vm", "browser"]
    assert vm_arguments(["missing-mode"]) == ["vm", "missing-mode"]
    assert runner.invoke(connect_app, ["missing-mode"]).exit_code == 2


@pytest.mark.parametrize(
    "retired", ["send", "get", "codex", "ssh", "wake", "sleep", "stop", "status", "health", "info"]
)
def test_unused_commands_are_rejected_before_any_connection(tmp_path: Path, retired: str) -> None:
    runner = CliRunner()
    environment = {"PATH": str(tmp_path), "HOME": str(tmp_path)}
    assert runner.invoke(connect_app, [retired], env=environment).exit_code == 2
    assert runner.invoke(app, ["vm", retired], env=environment).exit_code == 2
