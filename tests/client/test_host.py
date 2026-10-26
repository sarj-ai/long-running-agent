from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import socket
import subprocess
import sys
from threading import Event
import time
from typing import TYPE_CHECKING, override

from long_running_agent.client.browser import BrowserRoute
from long_running_agent.client.controller import Client
from long_running_agent.client.host import HostBridge
from long_running_agent.config import Settings
from long_running_agent.host_connection import HostRegistration, HostReply, HostRequest, exchange, read_message
from long_running_agent.process import DEVNULL, PIPE, spawn
import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence


class HostController(Client):
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        super().__init__(Settings(), runner=self.record)

    @override
    def ensure_connection(self) -> None:
        pass

    def record(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert cwd is None
        assert env is None
        assert input_text is None
        assert timeout is not None or not check
        assert capture_output or not check
        self.commands.append(list(arguments))
        output = ""
        if "runtime host prepare " in arguments[-1]:
            identifier = arguments[-1].split()[-1]
            output = f"/run/user/{os.getuid()}/lra-host/{identifier}.sock\n"
        return subprocess.CompletedProcess(list(arguments), 0, output, "")


@pytest.fixture
def bridge() -> Iterator[HostBridge]:
    def route(_controller: Client) -> BrowserRoute:
        child = spawn(
            [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"], stdin=PIPE, stdout=PIPE, stderr=DEVNULL
        )
        return BrowserRoute(child, "http://127.0.0.1:28000/vnc.html?autoconnect=true&resize=remote")

    with HostBridge(HostController(), route_factory=route) as connected:
        yield connected


def registration(bridge: HostBridge) -> HostRegistration:
    assert bridge.local_socket is not None
    return HostRegistration(connection=bridge.connection, socket=str(bridge.local_socket), label="Mac")


def test_private_return_channel_opens_only_its_owned_vm_viewer(bridge: HostBridge) -> None:
    endpoint = registration(bridge)
    assert Path(endpoint.socket).stat().st_mode & 0o777 == 0o600
    assert Path(endpoint.socket).parent.stat().st_mode & 0o777 == 0o700
    assert exchange(endpoint, HostRequest(action="status")).ok
    assert exchange(endpoint, HostRequest(action="browser")).ok
    controller = bridge.controller
    assert isinstance(controller, HostController)
    assert controller.commands[-1] == [
        "/usr/bin/open",
        "http://127.0.0.1:28000/vnc.html?autoconnect=true&resize=remote",
    ]
    assert all("-O" not in command for command in controller.commands)


@pytest.mark.parametrize(
    "payload",
    [
        b'{"action":"exec","command":"touch bad"}\n',
        b'{"action":"image","path":"/Users/me/private"}\n',
        b"x" * 16385 + b"\n",
    ],
)
def test_arbitrary_host_commands_and_mac_paths_never_reach_native_ui(bridge: HostBridge, payload: bytes) -> None:
    endpoint = registration(bridge)
    before = list(bridge.controller.commands) if isinstance(bridge.controller, HostController) else []
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as channel:
        channel.settimeout(2)
        channel.connect(endpoint.socket)
        channel.sendall(payload)
        response = channel.recv(16384)
    assert not HostReply.model_validate_json(response).ok
    assert isinstance(bridge.controller, HostController)
    assert bridge.controller.commands == before


def test_status_remains_responsive_while_a_user_action_is_busy(bridge: HostBridge) -> None:
    endpoint = registration(bridge)
    with bridge.action_lock:
        assert exchange(endpoint, HostRequest(action="status"), timeout=1).ok
        reply = exchange(endpoint, HostRequest(action="image"), timeout=1)
        assert not reply.ok
        assert "another request" in reply.message


def test_terminal_owns_reverse_socket_and_does_not_use_shared_master_forward(bridge: HostBridge) -> None:
    assert bridge.terminal("chosen-session") == 0
    controller = bridge.controller
    assert isinstance(controller, HostController)
    arguments = controller.commands[-1]
    assert "ControlPath=none" in arguments
    assert "StreamLocalBindUnlink=no" in arguments
    assert arguments[arguments.index("-R") + 1] == f"{bridge.remote_socket}:{bridge.local_socket}"
    assert arguments[-1].endswith(f"runtime host attach {bridge.connection} {bridge.label} --session chosen-session")
    assert "-O" not in arguments


def test_disconnect_closes_only_owned_route_and_removes_private_socket() -> None:
    unrelated = spawn(
        [sys.executable, "-c", "import time;time.sleep(20)"], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL
    )
    route_child = spawn(
        [sys.executable, "-c", "import sys;sys.stdin.buffer.read()"], stdin=PIPE, stdout=PIPE, stderr=DEVNULL
    )
    try:
        with HostBridge(
            HostController(), route_factory=lambda _controller: BrowserRoute(route_child, "http://127.0.0.1:28000/")
        ) as connected:
            endpoint = connected.local_socket
            assert endpoint is not None
            assert endpoint.exists()
        assert not endpoint.exists()
        assert route_child.poll() is not None
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=2)


@pytest.mark.parametrize("ignore_term", [False, True], ids=["terminates", "requires-kill"])
def test_disconnect_cancels_owned_native_dialog_without_waiting_for_its_deadline(
    bridge: HostBridge, tmp_path: Path, *, ignore_term: bool
) -> None:
    entered = Event()
    ready = tmp_path / "native-ready"

    def native_request() -> str:
        entered.set()
        program = "import pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN if sys.argv[2]=='yes' else signal.SIG_DFL);pathlib.Path(sys.argv[1]).touch();time.sleep(20)"
        return bridge.command([sys.executable, "-c", program, str(ready), "yes" if ignore_term else "no"], timeout=15)

    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(native_request)
        assert entered.wait(timeout=1)
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        assert bridge.child is not None
        start = time.monotonic()
        bridge.__exit__()
        with pytest.raises(RuntimeError, match="cancelled"):
            pending.result(timeout=3)
        assert time.monotonic() - start < 3


def test_native_file_selection_preserves_intentional_leading_and_trailing_spaces(bridge: HostBridge) -> None:
    selected = " /tmp/chosen image.png "
    output = bridge.command([sys.executable, "-c", "import sys;sys.stdout.write(sys.argv[1]+'\\n')", selected])
    assert output == selected


def test_slow_fragmented_request_has_an_absolute_message_deadline() -> None:
    reader, writer = socket.socketpair()
    with reader, writer, ThreadPoolExecutor(max_workers=1) as executor:
        reader.settimeout(0.08)

        def drip() -> None:
            for _ in range(30):
                try:
                    writer.sendall(b"x")
                except OSError:
                    return
                time.sleep(0.01)

        pending = executor.submit(drip)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            read_message(reader)
        assert time.monotonic() - started < 0.3
        reader.close()
        pending.result(timeout=1)
