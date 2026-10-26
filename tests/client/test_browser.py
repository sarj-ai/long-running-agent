from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import json
import socket
import subprocess
import sys
from threading import Event
from typing import TYPE_CHECKING, override

from long_running_agent import Client
from long_running_agent.client.browser import HELPER, await_forward, connect, viewer_ready
from long_running_agent.config import Settings
from long_running_agent.process import DEVNULL, PIPE, ChildProcess, CommandError, spawn
import pytest


if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping, Sequence
    from pathlib import Path


CONNECTION = {"uid": 1000, "display": 100, "cdp_port": 20000, "vnc_port": 20001, "viewer_port": 20002}
METADATA = json.dumps(CONNECTION)


class BrowserController(Client):
    def __init__(self, *, metadata: str = METADATA, helper_error: int = 0) -> None:
        self.commands: list[tuple[str, ...]] = []
        self.metadata = metadata
        self.helper_error = helper_error
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
        assert capture_output
        assert check
        assert timeout is not None
        assert input_text is None
        assert cwd is None
        assert env is None
        self.commands.append(tuple(arguments))
        if self.helper_error:
            program = "ssh"
            raise CommandError(program, self.helper_error)
        return subprocess.CompletedProcess(list(arguments), 0, self.metadata, "")


@dataclass
class Transport:
    arguments: list[tuple[str, ...]] = field(default_factory=list)
    children: list[ChildProcess] = field(default_factory=list)

    def channel(self, arguments: Sequence[str]) -> ChildProcess:
        self.arguments.append(tuple(arguments))
        program = "import sys;print('lra-browser-ready',flush=True);sys.stdin.buffer.read()"
        child = spawn([sys.executable, "-c", program], stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
        self.children.append(child)
        return child

    def ready(self, _port: int, _timeout: float) -> bool:
        assert self.children[-1].stdin is not None
        self.children[-1].stdin.close()
        return True


@pytest.fixture
def transport() -> Iterator[Transport]:
    state = Transport()
    yield state
    for child in state.children:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()


def test_existing_browser_opens_registered_viewer_through_its_own_transport(transport: Transport) -> None:
    controller = BrowserController()
    assert connect(controller, allocate_port=lambda: 28000, ready=transport.ready, channel=transport.channel) == 0
    assert controller.commands[:2] == [
        ("ssh", "-T", "sarj-codex", HELPER + " connection"),
        ("ssh", "-T", "sarj-codex", HELPER + " start"),
    ]
    assert transport.arguments == [
        (
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
            "127.0.0.1:28000:127.0.0.1:20002",
            "sarj-codex",
            "printf 'lra-browser-ready\\n'; exec cat",
        )
    ]
    assert controller.commands[-1] == (
        "/usr/bin/open",
        "http://127.0.0.1:28000/vnc.html?autoconnect=true&resize=remote",
    )
    assert transport.children[0].poll() is not None
    assert not any(
        any(word in argument for word in ("restart", "install", "stop", "9222"))
        for call in controller.commands
        for argument in call
    )


def test_no_open_keeps_viewer_forward_without_opening_a_mac_browser(transport: Transport) -> None:
    controller = BrowserController()
    assert (
        connect(controller, no_open=True, allocate_port=lambda: 28000, ready=transport.ready, channel=transport.channel)
        == 0
    )
    assert len(controller.commands) == 2


@pytest.mark.parametrize("status", [127, 1, 255])
def test_absent_helper_is_actionable_and_other_failures_are_preserved(status: int, transport: Transport) -> None:
    controller = BrowserController(helper_error=status)
    if status == 127:
        with pytest.raises(RuntimeError, match="No persistent browser is installed for this user"):
            connect(controller, channel=transport.channel)
    else:
        with pytest.raises(CommandError) as caught:
            connect(controller, channel=transport.channel)
        assert caught.value.returncode == status
    assert len(controller.commands) == 1
    assert not transport.children


@pytest.mark.parametrize(
    "metadata",
    [
        "private-invalid-json",
        json.dumps({**CONNECTION, "viewer_port": True}),
        json.dumps({**CONNECTION, "uid": 0}),
        json.dumps({**CONNECTION, "viewer_port": 65535}),
        json.dumps({**CONNECTION, "viewer_port": 20001}),
        json.dumps({**CONNECTION, "private-extra-field": "secret"}),
    ],
    ids=("invalid-json", "boolean-port", "system-uid", "unallocated-port", "duplicate-port", "extra-field"),
)
def test_invalid_metadata_is_redacted_before_start_or_forward(metadata: str, transport: Transport) -> None:
    controller = BrowserController(metadata=metadata)
    with pytest.raises(ValueError, match="invalid connection metadata") as caught:
        connect(controller, channel=transport.channel)
    assert "private" not in str(caught.value)
    assert "secret" not in str(caught.value)
    assert len(controller.commands) == 1
    assert not transport.children


@pytest.mark.parametrize("interrupted", [False, True])
def test_readiness_failure_or_interrupt_closes_only_owned_child(interrupted: bool, transport: Transport) -> None:
    controller = BrowserController()

    def fail(_port: int, _timeout: float) -> bool:
        if interrupted:
            raise KeyboardInterrupt
        message = "viewer readiness fixture failed"
        raise RuntimeError(message)

    if interrupted:
        with pytest.raises(KeyboardInterrupt):
            connect(controller, allocate_port=lambda: 28000, ready=fail, channel=transport.channel)
    else:
        with pytest.raises(RuntimeError, match="viewer readiness fixture failed"):
            connect(controller, allocate_port=lambda: 28000, ready=fail, channel=transport.channel)
    assert transport.children[0].poll() is not None
    assert len(controller.commands) == 2


def test_viewer_deadline_closes_forward_without_opening_browser(transport: Transport) -> None:
    controller = BrowserController()
    ticks = iter([0.0, 21.0])
    with pytest.raises(TimeoutError, match="within twenty seconds"):
        connect(controller, allocate_port=lambda: 28000, clock=lambda: next(ticks), channel=transport.channel)
    assert transport.children[0].poll() is not None
    assert len(controller.commands) == 2


def test_failed_owned_forward_cannot_open_an_occupied_local_listener(transport: Transport) -> None:
    controller = BrowserController()
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        address: tuple[str, int] = occupied.getsockname()  # pyright: ignore[reportAny] -- AF_INET fixture exposes a concrete host and port.

        def failed_channel(arguments: Sequence[str]) -> ChildProcess:
            transport.arguments.append(tuple(arguments))
            child = spawn([sys.executable, "-c", "raise SystemExit(255)"], stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
            transport.children.append(child)
            return child

        def unrelated_listener(_port: int, _timeout: float) -> bool:
            pytest.fail("An unrelated listener must not qualify as the owned SSH forward")

        with pytest.raises(RuntimeError, match="failed before forwarding was ready"):
            connect(controller, allocate_port=lambda: address[1], ready=unrelated_listener, channel=failed_channel)
    assert len(controller.commands) == 2
    assert transport.children[0].poll() is not None


def test_readiness_marker_timeout_is_bounded(transport: Transport) -> None:
    child = spawn([sys.executable, "-c", "import time;time.sleep(5)"], stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
    transport.children.append(child)
    with pytest.raises(TimeoutError, match="forwarding did not become ready"):
        await_forward(child, timeout=0.01)


def test_one_failed_client_does_not_cancel_another_clients_forward(transport: Transport) -> None:
    controller, other = BrowserController(), BrowserController()
    established = Event()

    def ready(_port: int, _timeout: float) -> bool:
        established.set()
        return True

    def fail(_port: int, _timeout: float) -> bool:
        message = "second viewer failed"
        raise RuntimeError(message)

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            connect, controller, no_open=True, allocate_port=lambda: 28000, ready=ready, channel=transport.channel
        )
        assert established.wait(timeout=2)
        with pytest.raises(RuntimeError, match="second viewer failed"):
            connect(other, no_open=True, allocate_port=lambda: 28001, ready=fail, channel=transport.channel)
        assert transport.children[0].poll() is None
        assert transport.children[1].poll() is not None
        assert transport.children[0].stdin is not None
        transport.children[0].stdin.close()
        assert first.result(timeout=2) == 0
    assert len(controller.commands) == len(other.commands) == 2


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", True),
        (b"HTTP/1.1 503 Unavailable\r\nContent-Length: 0\r\n\r\n", False),
        (b"not HTTP\r\n\r\n", False),
    ],
)
def test_viewer_readiness_checks_novnc_route(response: bytes, *, expected: bool) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(2)
        address: tuple[str, int] = listener.getsockname()  # pyright: ignore[reportAny] -- AF_INET fixture exposes a concrete host and port.

        def respond() -> None:
            with listener.accept()[0] as client:
                client.settimeout(2)
                assert client.recv(4096).startswith(b"GET /vnc.html HTTP/1.1")
                client.sendall(response)

        with ThreadPoolExecutor(max_workers=1) as executor:
            exchange = executor.submit(respond)
            assert viewer_ready(address[1], 1) is expected
            exchange.result(timeout=2)
