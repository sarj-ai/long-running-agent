from __future__ import annotations

from contextlib import suppress
from dataclasses import dataclass
from http import HTTPStatus
from http.client import HTTPConnection, HTTPException
import os
import select
import shlex
import time
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from rich.console import Console

from long_running_agent.client.connection import close_child, loopback_port, start_channel
from long_running_agent.client.terminal import TerminalState
from long_running_agent.host_connection import website_url
from long_running_agent.process import PIPE, ChildProcess, CommandError


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from long_running_agent.client.controller import Client


HELPER = 'exec "$HOME/.local/bin/sarj-browser"'
FORWARD_READY = b"lra-browser-ready\n"
MIN_PORT = 1024
MAX_PORT = 65535
COMMAND_NOT_FOUND = 127


class BrowserConnection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    uid: int = Field(ge=1000)
    display: int = Field(ge=1, le=999)
    cdp_port: int = Field(ge=1024, le=32767)
    vnc_port: int = Field(ge=1024, le=32767)
    viewer_port: int = Field(ge=1024, le=32767)

    @model_validator(mode="after")
    def distinct_ports(self) -> Self:
        ports = (self.cdp_port, self.vnc_port, self.viewer_port)
        if len(set(ports)) != len(ports):
            message = "Browser endpoint ports must be distinct"
            raise ValueError(message)
        return self


def viewer_ready(port: int, timeout: float) -> bool:
    connection = HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", "/vnc.html")
        return connection.getresponse().status == HTTPStatus.OK
    except OSError, HTTPException:
        return False
    finally:
        connection.close()


def start_forward(arguments: Sequence[str]) -> ChildProcess:
    return start_channel(arguments, stdout=PIPE)


def await_forward(child: ChildProcess, *, timeout: float = 20) -> None:
    if child.stdout is None:
        message = "The private browser connection has no readiness channel"
        raise RuntimeError(message)
    deadline = time.monotonic() + timeout
    received = b""
    while time.monotonic() < deadline:
        if not select.select([child.stdout], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        data = os.read(child.stdout.fileno(), len(FORWARD_READY) - len(received))
        received += data
        if not data or not FORWARD_READY.startswith(received):
            message = "The private browser connection failed before forwarding was ready"
            raise RuntimeError(message)
        if received == FORWARD_READY:
            return
    message = "The private browser forwarding did not become ready within twenty seconds"
    raise TimeoutError(message)


@dataclass
class BrowserRoute:
    child: ChildProcess
    url: str

    def close(self) -> None:
        close_child(self.child)
        if self.child.stdout is not None:
            self.child.stdout.close()


def await_viewer(
    child: ChildProcess,
    port: int,
    *,
    ready: Callable[[int, float], bool],
    clock: Callable[[], float],
    sleeper: Callable[[float], None],
) -> None:
    deadline = clock() + 20
    while clock() < deadline:
        if child.poll() is not None:
            message = "The private browser connection exited; run sarj-connect browser to reconnect"
            raise RuntimeError(message)
        if ready(port, min(0.5, max(0.01, deadline - clock()))):
            return
        sleeper(min(0.2, max(0, deadline - clock())))
    message = "The private browser connection did not become ready within twenty seconds"
    raise TimeoutError(message)


def start_route(
    controller: Client,
    *,
    allocate_port: Callable[[], int] | None = None,
    ready: Callable[[int, float], bool] = viewer_ready,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    channel: Callable[[Sequence[str]], ChildProcess] = start_forward,
) -> BrowserRoute:
    controller.ensure_connection()
    alias = controller.settings.ssh_alias
    try:
        result = controller.ssh(["-T", alias, HELPER + " connection"], capture_output=True, timeout=15)
    except CommandError as error:
        if error.returncode != COMMAND_NOT_FOUND:
            raise
        message = "No persistent browser is installed for this user. Ask an administrator to prepare sarj-browser."
        raise RuntimeError(message) from None
    try:
        connection = BrowserConnection.model_validate_json(result.stdout)
    except ValueError:
        message = "The persistent browser returned invalid connection metadata; no forwarding was opened"
        raise ValueError(message) from None
    controller.ssh(["-T", alias, HELPER + " start"], capture_output=True, timeout=45)
    port = loopback_port(0) if allocate_port is None else allocate_port()
    if isinstance(port, bool) or not MIN_PORT <= port <= MAX_PORT:
        message = "Invalid local browser port"
        raise ValueError(message)
    forward = f"127.0.0.1:{port}:127.0.0.1:{connection.viewer_port}"
    url = f"http://127.0.0.1:{port}/vnc.html?autoconnect=true&resize=remote"
    # A separate transport owns its listener, so simultaneous clients cannot
    # cancel a shared master's identical forward after a port-allocation race.
    child = channel(
        [
            "ssh",
            "-T",
            "-o",
            "ControlMaster=no",
            "-o",
            "ControlPath=none",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=20",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ClearAllForwardings=no",
            "-L",
            forward,
            alias,
            "printf 'lra-browser-ready\\n'; exec cat",
        ]
    )
    try:
        await_forward(child)
        await_viewer(child, port, ready=ready, clock=clock, sleeper=sleeper)
        return BrowserRoute(child, url)
    except BaseException:
        BrowserRoute(child, url).close()
        raise


def connect(
    controller: Client,
    *,
    no_open: bool = False,
    url: str | None = None,
    allocate_port: Callable[[], int] | None = None,
    ready: Callable[[int, float], bool] = viewer_ready,
    clock: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    channel: Callable[[Sequence[str]], ChildProcess] = start_forward,
) -> int:
    if url is not None:
        website_url(url)
    with TerminalState():
        route = start_route(
            controller, allocate_port=allocate_port, ready=ready, clock=clock, sleeper=sleeper, channel=channel
        )
        try:
            if url is not None:
                controller.remote("runtime host open-site -- " + shlex.quote(url), capture_output=True, timeout=60)
            if not no_open:
                controller.runner(["/usr/bin/open", route.url], capture_output=True, timeout=15)
            Console(stderr=True).print(
                f"Browser: {route.url}\nSign in here once; website sessions stay in this VM browser.\n"
                "For passwords, open the viewer sidebar's Clipboard panel, paste, then use Paste in Chrome.\n"
                "Ctrl-C closes this connection; the VM browser keeps running.",
                markup=False,
            )
            with suppress(KeyboardInterrupt):
                return route.child.wait()
            return 0
        finally:
            route.close()
