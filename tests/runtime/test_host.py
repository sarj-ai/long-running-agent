from __future__ import annotations

from contextlib import contextmanager
import errno
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
from socketserver import BaseRequestHandler, UnixStreamServer
import subprocess
import tempfile
from threading import Thread
from typing import TYPE_CHECKING, override
from urllib.parse import urlunsplit

from long_running_agent.host_connection import HostRegistration, HostReply, HostRequest, read_message, website_url
from long_running_agent.runtime import host as host_module
from long_running_agent.runtime.host import choose_connection, connection_directory, registered
from pydantic import ValidationError
import pytest
from typer.main import get_group


if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence
    import socket
    from typing import TextIO


@pytest.fixture
def home() -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="lra-host-test-", dir="/tmp") as temporary:
        yield Path(temporary)


@contextmanager
def live_socket(home: Path, identifier: str) -> Generator[Path]:
    endpoint = connection_directory(home) / (identifier + ".sock")

    class Handler(BaseRequestHandler):
        @override
        def handle(self) -> None:
            channel: socket.socket = self.request  # pyright: ignore[reportAny] -- UnixStreamServer supplies a connected stream socket.
            request = HostRequest.model_validate_json(read_message(channel))
            assert request.action == "status"
            channel.sendall(HostReply(ok=True, message="Connected").model_dump_json().encode() + b"\n")

    with UnixStreamServer(str(endpoint), Handler) as server:
        endpoint.chmod(0o600)
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield endpoint
        finally:
            server.shutdown()
            thread.join(timeout=2)


def test_registered_connection_is_usable_from_existing_panes_and_cleaned_on_detach(home: Path) -> None:
    identifier = "a" * 32
    with live_socket(home, identifier) as endpoint:
        with registered(identifier, "My Mac", home=home):
            selected = choose_connection(home=home)
            assert selected.socket == str(endpoint)
            receipt = endpoint.with_suffix(".json")
            assert receipt.stat().st_mode & 0o777 == 0o600
        assert not endpoint.exists()
        assert not receipt.exists()


def test_attach_cli_accepts_selected_session_option() -> None:
    identifier = "a" * 32
    command = get_group(host_module.app).commands["attach"]
    with command.make_context("attach", [identifier, "My Mac", "--session", "chosen-session"]) as context:
        assert context.params == {"connection": identifier, "label": "My Mac", "session": "chosen-session"}


def test_multiple_computers_require_explicit_selection(home: Path) -> None:
    first, second = "a" * 32, "b" * 32
    with (
        live_socket(home, first),
        live_socket(home, second),
        registered(first, "Mac One", home=home),
        registered(second, "Mac Two", home=home),
    ):
        with pytest.raises(RuntimeError, match="Several computers"):
            choose_connection(home=home)
        assert choose_connection(second, home=home).label == "Mac Two"


def test_stale_and_symlink_receipts_do_not_choose_a_different_socket(home: Path) -> None:
    root = connection_directory(home)
    receipt = HostRegistration(connection="a" * 32, socket=str(root / ("a" * 32 + ".sock")), label="Old Mac")
    path = root / (receipt.connection + ".json")
    path.write_text(receipt.model_dump_json())
    path.chmod(0o600)
    (root / ("b" * 32 + ".json")).symlink_to(path)
    with pytest.raises(RuntimeError, match="No Mac terminal connection"):
        choose_connection(home=home)


def test_host_connection_directory_rejects_public_permissions_and_symlink(home: Path) -> None:
    root = connection_directory(home)
    root.chmod(0o755)
    with pytest.raises(RuntimeError, match="private directory"):
        connection_directory(home)
    root.rmdir()
    root.symlink_to(home, target_is_directory=True)
    with pytest.raises(RuntimeError, match="private directory"):
        connection_directory(home)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        urlunsplit(("https", "user:password@example.com", "", "", "")),
        "https://example.com\n",
        "https://example.com:bad",
        "http://",
        "https://example.com/\u001b",
    ],
)
def test_website_navigation_rejects_unsafe_schemes_credentials_and_controls(url: str) -> None:
    with pytest.raises(ValueError, match="complete http") as caught:
        website_url(url)
    assert "password" not in str(caught.value)


def test_complete_auth_urls_preserve_query_and_fragment_without_shell_interpretation() -> None:
    url = "https://accounts.example.com/auth?state=a%20b&continue=https%3A%2F%2Fexample.com#step"
    assert website_url(url) == url


@pytest.mark.parametrize(
    "data",
    [
        {"action": "exec"},
        {"action": "image", "path": "/Users/me/secret"},
        {"action": "download"},
        {"action": "browser", "extra": "private"},
    ],
    ids=["arbitrary-command", "host-file-path", "missing-vm-path", "unexpected-field"],
)
def test_return_channel_has_no_arbitrary_command_or_mac_file_interface(data: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        HostRequest.model_validate_json(json.dumps(data))


def test_more_than_32_dead_connections_do_not_block_a_new_terminal(home: Path) -> None:
    root = connection_directory(home)
    for number in range(40):
        identifier = f"{number:032x}"
        saved = HostRegistration(connection=identifier, socket=str(root / (identifier + ".sock")), label="Old Mac")
        path = root / (identifier + ".json")
        path.write_text(saved.model_dump_json())
        path.chmod(0o600)
    current = "f" * 32
    with live_socket(home, current), registered(current, "Current Mac", home=home):
        assert choose_connection(home=home).connection == current
        assert len(list(root.glob("*.json"))) == 1


def test_full_disk_during_registration_removes_only_its_partial_receipt(home: Path) -> None:
    identifier = "c" * 32

    def full_disk(stream: TextIO, text: str) -> None:
        stream.write(text[:10])
        message = "No space left on device"
        raise OSError(errno.ENOSPC, message)

    with live_socket(home, identifier) as endpoint:
        unrelated = endpoint.parent / "unrelated"
        unrelated.write_text("preserved")
        with pytest.raises(OSError, match="No space"), registered(identifier, "Mac", home=home, writer=full_disk):
            pytest.fail("A full disk must not publish a registration")
        assert not endpoint.with_suffix(".json").exists()
        assert unrelated.read_text() == "preserved"


def test_website_open_uses_new_target_and_activates_only_that_tab(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, str]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            calls.append(("PUT", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"id":"OWN_NEW_TAB","url":"private-auth-fixture"}')

        def do_GET(self) -> None:
            calls.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Target activated")

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    def native(arguments: Sequence[str], *, capture_output: bool, timeout: float) -> subprocess.CompletedProcess[str]:
        assert capture_output
        assert timeout <= 45
        assert Path(arguments[0]).name == "sarj-browser"
        assert arguments[1] in {"connection", "start"}
        metadata = {"uid": os.getuid(), "display": 100, "cdp_port": 20000, "vnc_port": 20001, "viewer_port": 20002}
        return subprocess.CompletedProcess(list(arguments), 0, json.dumps(metadata), "")

    original = HTTPConnection
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def connection(host: str, port: int, *, timeout: float) -> HTTPConnection:
            assert host == "127.0.0.1"
            assert port == 20000
            return original(host, server.server_port, timeout=timeout)

        try:
            host_module.open_website(
                "https://example.com/login?state=a%20b&private-auth-fixture=1#signin",
                runner=native,
                connector=connection,
            )
        finally:
            server.shutdown()
            thread.join(timeout=2)
    assert calls == [
        ("PUT", "/json/new?https%3A%2F%2Fexample.com%2Flogin%3Fstate%3Da%2520b%26private-auth-fixture%3D1%23signin"),
        ("GET", "/json/activate/OWN_NEW_TAB"),
    ]
    output = capsys.readouterr()
    assert "private-auth-fixture" not in output.out + output.err
