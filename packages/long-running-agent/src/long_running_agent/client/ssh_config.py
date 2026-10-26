from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from long_running_agent.config import Settings


LEGACY_START = "# BEGIN SARJ CODEX VM"
LEGACY_END = "# END SARJ CODEX VM"


def managed_configuration(old: str, block: str, alias: str) -> str:
    start, end = f"# BEGIN LONG RUNNING AGENT {alias}", f"# END LONG RUNNING AGENT {alias}"
    text = _without_block(old, start, end)
    if alias == "sarj-codex":
        text = _without_block(text, LEGACY_START, LEGACY_END)
    lines = text.splitlines(keepends=True)
    for line in lines:
        fields = shlex.split(line, comments=True)
        if fields and fields[0].lower() == "host" and alias.lower() in [item.lower() for item in fields[1:]]:
            message = f"An unmanaged SSH host named {alias} already exists"
            raise ValueError(message)
    insertion = 0
    for index, line in enumerate(lines):
        if shlex.split(line, comments=True):
            break
        insertion = index + 1
    prefix, suffix = "".join(lines[:insertion]), "".join(lines[insertion:])
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    return prefix + block + "\n" + suffix


def render(settings: Settings, username: str, *, python: Path | None = None) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", username):
        message = "OS Login returned an invalid user name"
        raise ValueError(message)
    executable = str(python or Path(sys.executable))
    # OpenSSH expands percent tokens even inside shell quotes. Only the explicit
    # port placeholder is allowed to expand; all other values are literals.
    command = [
        executable,
        "-m",
        "long_running_agent.cli",
        "--project",
        settings.project,
        "--instance",
        settings.instance,
        "--zone",
        settings.zone,
    ]
    if settings.account:
        command.extend(("--account", settings.account))
    command.extend(("vm", "tunnel"))
    proxy = shlex.join([item.replace("%", "%%") for item in command]) + " %p"
    alias = settings.ssh_alias
    known_hosts = "sarj_codex_known_hosts" if alias == "sarj-codex" else f"{alias}_known_hosts"
    return f"""# BEGIN LONG RUNNING AGENT {alias}
Host {alias}
  HostName {settings.instance}
  User {username}
  IdentityFile {_quoted(str(settings.ssh_key).replace("%", "%%"))}
  IdentitiesOnly yes
  ProxyCommand {proxy}
  HostKeyAlias {alias}.{settings.project}.{settings.zone}
  UserKnownHostsFile ~/.ssh/{known_hosts}
  StrictHostKeyChecking accept-new
  ServerAliveInterval 20
  ServerAliveCountMax 3
  TCPKeepAlive yes
  ControlMaster auto
  ControlPath ~/.ssh/{alias}-%C
  ControlPersist 10m
  ForwardAgent no
Host *
# END LONG RUNNING AGENT {alias}"""


def install(settings: Settings, username: str, *, home: Path | None = None, python: Path | None = None) -> Path:
    directory = (home or Path.home()) / ".ssh"
    path = directory / "config"
    old = path.read_text() if path.exists() else ""
    new = managed_configuration(old, render(settings, username, python=python), settings.ssh_alias)
    if new == old:
        return path
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"config.lra-backup-{stamp}")
        backup.write_text(old)
        backup.chmod(0o600)
    descriptor, temporary = tempfile.mkstemp(prefix=".lra-ssh-", dir=directory)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(new)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).chmod(0o600)
        Path(temporary).replace(path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return path


def _without_block(text: str, start: str, end: str) -> str:
    if text.count(start) != text.count(end) or text.count(start) > 1:
        message = "Managed SSH block is malformed; configuration was not changed"
        raise ValueError(message)
    if start not in text:
        return text
    pattern = re.compile(r"^" + re.escape(start) + r"\n.*?^" + re.escape(end) + r"(?:\n|$)", re.MULTILINE | re.DOTALL)
    result, count = pattern.subn("", text)
    if count != 1:
        message = "Managed SSH block is malformed; configuration was not changed"
        raise ValueError(message)
    return result


def _quoted(value: str) -> str:
    if any(character in value for character in ("\n", "\r", "\x00")):
        message = "SSH values cannot contain control characters"
        raise ValueError(message)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
