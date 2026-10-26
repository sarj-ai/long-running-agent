from __future__ import annotations

from contextlib import contextmanager
import errno
from http import HTTPStatus
from http.client import HTTPConnection
from importlib.metadata import version
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
from typing import TYPE_CHECKING, Annotated, Protocol
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field
import typer

from long_running_agent.client.browser import BrowserConnection
from long_running_agent.client.terminal import TerminalState
from long_running_agent.host_connection import (
    MAX_MESSAGE,
    HostRegistration,
    HostReply,
    HostRequest,
    exchange,
    website_url,
)
from long_running_agent.process import Completed, run


if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Sequence
    from typing import TextIO


app = typer.Typer(
    help="Use the Mac connected to your VM terminal.",
    hidden=True,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
CONNECTION_PATTERN = r"[a-f0-9]{32}"
MAX_CONNECTIONS = 32
MAX_SAVED_CONNECTIONS = 1024
MAX_TARGET_MESSAGE = 65536


class BrowserTarget(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")


class BrowserRunner(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed: ...


class BrowserConnector(Protocol):
    def __call__(self, host: str, port: int, *, timeout: float) -> HTTPConnection: ...


def write_registration(stream: TextIO, text: str) -> None:
    stream.write(text)


def connection_directory(home: Path | None = None) -> Path:
    root = Path(f"/run/user/{os.getuid()}/lra-host") if home is None else home / ".lra-host"
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        message = "Host connections require a private directory owned by your Linux account"
        raise RuntimeError(message)
    return root


def connection_path(connection: str, *, home: Path | None = None) -> Path:
    if re.fullmatch(CONNECTION_PATTERN, connection) is None:
        message = "Invalid host connection identifier"
        raise ValueError(message)
    return connection_directory(home) / connection


@app.command(hidden=True)
def prepare(connection: str) -> None:
    """Prepare only this account's private return-channel directory."""
    typer.echo(str(connection_path(connection).with_suffix(".sock")))


@contextmanager
def registered(
    connection: str,
    label: str,
    *,
    home: Path | None = None,
    writer: Callable[[TextIO, str], None] = write_registration,
) -> Generator[HostRegistration]:
    base = connection_path(connection, home=home)
    endpoint = base.with_suffix(".sock")
    info = endpoint.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        message = "The private SSH return channel is not ready for this user"
        raise RuntimeError(message)
    registration = HostRegistration(connection=connection, socket=str(endpoint), label=label)
    if not exchange(registration, HostRequest(action="status"), timeout=3).ok:
        message = "The connecting computer did not acknowledge its private return channel"
        raise RuntimeError(message)
    receipt = base.with_suffix(".json")
    receipt_info: os.stat_result | None = None
    try:
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        receipt_info = os.fstat(descriptor)
        with os.fdopen(descriptor, "w") as stream:
            writer(stream, registration.model_dump_json() + "\n")
        yield registration
    finally:
        for path, expected in ((receipt, receipt_info), (endpoint, info)):
            if expected is None:
                continue
            try:
                current = path.lstat()
                if (current.st_dev, current.st_ino) == (expected.st_dev, expected.st_ino):
                    path.unlink()
            except FileNotFoundError:
                pass


@app.command(hidden=True)
def attach(connection: str, label: str, session: str = "sarj-home") -> None:
    """Register this SSH connection while attaching to the selected native Zellij session."""
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", session) is None:
        message = "Invalid Zellij session name"
        raise ValueError(message)
    with TerminalState(), registered(connection, label):
        command = [
            "/bin/sh",
            "-c",
            'export PATH="$HOME/.local/bin:$HOME/.local/share/mise/shims:$PATH"; exec zellij "$@"',
            "zellij",
            "attach",
            "--create",
            session,
        ]
        raise typer.Exit(run(command, check=False).returncode)


def saved_registration(path: Path, root: Path) -> HostRegistration | None:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            return None
        data = stream.read(MAX_MESSAGE + 1)
    if len(data) > MAX_MESSAGE:
        return None
    registration = HostRegistration.model_validate_json(data)
    if path.stem != registration.connection or registration.socket != str(root / (path.stem + ".sock")):
        return None
    return registration


def live_connections(*, home: Path | None = None) -> list[HostRegistration]:
    root = connection_directory(home)
    candidates = sorted(root.glob("*.json"))
    if len(candidates) > MAX_SAVED_CONNECTIONS:
        message = "Too many saved host connections; inspect the private connection directory"
        raise RuntimeError(message)
    result: list[HostRegistration] = []
    for path in candidates:
        try:
            registration = saved_registration(path, root)
            if registration is not None and exchange(registration, HostRequest(action="status"), timeout=0.3).ok:
                result.append(registration)
        except OSError as error:
            if error.errno in {errno.ENOENT, errno.ECONNREFUSED}:
                forget_dead_connection(path, root)
            continue
        except ValueError:
            continue
    if len(result) > MAX_CONNECTIONS:
        message = "More than 32 live terminal return channels exist; detach unused client connections"
        raise RuntimeError(message)
    return result


def forget_dead_connection(path: Path, root: Path) -> None:
    # Only a missing/refused local SSH listener is conclusive. Timeouts and
    # malformed files retain their receipt; no browser or Zellij session ends.
    try:
        info = path.lstat()
        registration = saved_registration(path, root)
        if registration is not None and path.lstat().st_ino == info.st_ino:
            path.unlink()
    except OSError, ValueError:
        return


def choose_connection(connection: str | None = None, *, home: Path | None = None) -> HostRegistration:
    candidates = live_connections(home=home)
    if connection is not None:
        candidates = [candidate for candidate in candidates if candidate.connection == connection]
    if not candidates:
        message = "No Mac terminal connection is available. Reconnect from your Mac with sarj-connect terminal."
        raise RuntimeError(message)
    if len(candidates) > 1:
        choices = ", ".join(f"{item.label}: {item.connection}" for item in candidates)
        message = f"Several computers are connected; select one with --connection. {choices}"
        raise RuntimeError(message)
    return candidates[0]


def open_website(url: str, *, runner: BrowserRunner = run, connector: BrowserConnector = HTTPConnection) -> None:
    selected = website_url(url)
    helper = str(Path.home() / ".local/bin/sarj-browser")
    result = runner([helper, "connection"], capture_output=True, timeout=15)
    endpoint = BrowserConnection.model_validate_json(result.stdout)
    if endpoint.uid != os.getuid():
        message = "The browser endpoint belongs to another Linux account"
        raise RuntimeError(message)
    runner([helper, "start"], capture_output=True, timeout=45)
    channel = connector("127.0.0.1", endpoint.cdp_port, timeout=5)
    try:
        channel.request("PUT", "/json/new?" + quote(selected, safe=""))
        response = channel.getresponse()
        if response.status != HTTPStatus.OK:
            message = "The persistent VM browser could not open the selected website"
            raise RuntimeError(message)
        target = BrowserTarget.model_validate_json(response.read(MAX_TARGET_MESSAGE))
        channel.request("GET", "/json/activate/" + target.id)
        activated = channel.getresponse()
        if activated.status != HTTPStatus.OK:
            message = "The website opened, but Chrome could not bring its new tab to the foreground"
            raise RuntimeError(message)
        activated.read(MAX_TARGET_MESSAGE)
    finally:
        channel.close()


@app.command("open-site", hidden=True)
def open_site(url: str) -> None:
    open_website(url)


def perform(action: HostRequest, connection: str | None) -> HostReply:
    reply = exchange(choose_connection(connection), action)
    if not reply.ok:
        raise RuntimeError(reply.message)
    return reply


@app.command()
def browser(
    url: Annotated[str | None, typer.Argument(help="Website to open in your existing VM Chrome profile.")] = None,
    connection: Annotated[
        str | None, typer.Option(help="Choose a computer when several terminal connections are active.")
    ] = None,
) -> None:
    """Open the VM's persistent browser on your connected Mac for website sign-in."""
    selected = choose_connection(connection)
    if url is not None:
        open_website(url)
    reply = exchange(selected, HostRequest(action="browser"))
    if not reply.ok:
        raise RuntimeError(reply.message)
    typer.echo(reply.message)


@app.command()
def image(connection: Annotated[str | None, typer.Option()] = None) -> None:
    """Ask your Mac to choose an image or explicitly send its clipboard image."""
    reply = perform(HostRequest(action="image"), connection)
    for path in reply.paths:
        typer.echo(shlex.quote(path))


@app.command()
def download(path: str, connection: Annotated[str | None, typer.Option()] = None) -> None:
    """Ask the Mac to save a selected VM file after a native confirmation."""
    reply = perform(HostRequest(action="download", path=path), connection)
    typer.echo(reply.message)


@app.command()
def status() -> None:
    """List only live return channels belonging to this Linux user."""
    connections = live_connections()
    if not connections:
        typer.echo("No Mac return channel is active. Reconnect from the Mac with sarj-connect terminal.")
    for item in connections:
        typer.echo(f"{item.connection}  {item.label}")


@app.command()
def info() -> None:
    """Return this Linux user's identity, disk capacity, browser state, and live connection count."""
    disk = os.statvfs(Path.home())
    browser = run(
        ["systemctl", "--user", "is-active", "sarj-browser.service"], check=False, capture_output=True, timeout=5
    )
    typer.echo(
        json.dumps(
            {
                "runtime_version": version("long-running-agent"),
                "uid": os.getuid(),
                "username": pwd.getpwuid(os.getuid()).pw_name,
                "disk_free_bytes": disk.f_bavail * disk.f_frsize,
                "disk_total_bytes": disk.f_blocks * disk.f_frsize,
                "browser_running": browser.returncode == 0,
                "host_connections": len(live_connections()),
            }
        )
    )


def invoke(application: typer.Typer, name: str) -> None:
    try:
        application(prog_name=name)
    except (OSError, ValueError, RuntimeError) as error:
        detail = (
            str(error)
            if not isinstance(error, OSError)
            else "The host connection is unavailable; reconnect using sarj-connect terminal."
        )
        typer.echo(detail, err=True)
        raise SystemExit(1) from None


def main() -> None:
    invoke(app, "sarj-host")
