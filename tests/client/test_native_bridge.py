from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shlex
import stat
import struct
from subprocess import CompletedProcess
from typing import TYPE_CHECKING, override
import zlib

from long_running_agent import Client
from long_running_agent.client import transfer
from long_running_agent.client.browser import await_forward, start_route
from long_running_agent.client.host import HostBridge
from long_running_agent.config import Settings
from long_running_agent.host_connection import HostReply
from long_running_agent.process import DEVNULL, PIPE, ChildProcess, Completed, run, spawn
from pydantic import BaseModel, ConfigDict
import pytest


if TYPE_CHECKING:
    from collections.abc import Generator, Mapping, Sequence


ALIAS = "lra-native-check"
RUNTIME_PYTHON = '"$HOME/.local/share/uv/tools/long-running-agent/bin/python"'
REGISTER = """import sys
from long_running_agent.runtime.host import registered
with registered(sys.argv[1], 'Validation Mac'):
    print('lra-browser-ready', flush=True)
    sys.stdin.buffer.read()
"""
OPEN_BROWSER = """import sys
from long_running_agent.host_connection import HostRequest, exchange
from long_running_agent.runtime.host import choose_connection
selected = choose_connection(sys.argv[1])
reply = exchange(selected, HostRequest(action='browser'), timeout=10)
print(reply.model_dump_json())
"""
CLOSED_CONNECTION = """import sys
from long_running_agent.runtime.host import connection_path
base = connection_path(sys.argv[1])
for suffix in ('.json', '.sock'):
    assert not base.with_suffix(suffix).exists(follow_symlinks=False)
print('closed')
"""
UPLOADED_FILE = """import hashlib, json, os, re, stat, sys
from pathlib import Path
from long_running_agent.runtime.transfer import vm_source
path = Path(sys.argv[1])
inbox = Path.home() / '.local/share/long-running-agent/inbox'
assert path.parent.parent == inbox
assert re.fullmatch('[0-9a-f]{32}', path.parent.name)
folder = path.parent.lstat()
assert stat.S_ISDIR(folder.st_mode) and folder.st_uid == os.getuid()
with vm_source(path, Path.home()) as descriptor:
    before = os.fstat(descriptor)
    digest = hashlib.sha256()
    while data := os.read(descriptor, 65536):
        digest.update(data)
    assert digest.hexdigest() == sys.argv[2]
    assert before.st_size == int(sys.argv[3])
    current = path.lstat()
    assert (current.st_dev, current.st_ino) == (before.st_dev, before.st_ino)
    evidence = dict(size=before.st_size, sha256=digest.hexdigest(),
                    file_mode=stat.S_IMODE(before.st_mode), batch_mode=stat.S_IMODE(folder.st_mode))
    if sys.argv[4] == 'remove':
        path.unlink()
        path.parent.rmdir()
print(json.dumps(evidence))
"""


class FileEvidence(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    size: int
    sha256: str
    file_mode: int
    batch_mode: int


class NativeController(Client):
    def __init__(self) -> None:
        self.opened: list[str] = []
        super().__init__(Settings(ssh_alias=ALIAS), runner=self.execute)

    @override
    def ensure_connection(self) -> None:
        pass

    def execute(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed:
        if arguments[0] == "/usr/bin/open":
            assert len(arguments) == 2
            self.opened.append(arguments[1])
            return CompletedProcess(list(arguments), 0, "", "")
        return run(
            arguments,
            capture_output=capture_output,
            check=check,
            timeout=timeout,
            input_text=input_text,
            cwd=cwd,
            env=env,
        )


@pytest.fixture
def native_ssh_config() -> Path:
    selected = os.getenv("LRA_NATIVE_SSH_CONFIG")  # ruff: ignore[banned-api] -- Read only the explicit opt-in native-test configuration path.
    if selected is None:
        pytest.skip("Set LRA_NATIVE_SSH_CONFIG to opt in to the installed-runtime loopback SSH test")
    path = Path(selected).expanduser().absolute()
    metadata = path.lstat()
    assert stat.S_ISREG(metadata.st_mode)
    assert metadata.st_uid == os.getuid()
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert metadata.st_nlink == 1
    return path


@pytest.fixture
def native_controller(native_ssh_config: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NativeController:
    binaries = tmp_path / "native-bin"
    binaries.mkdir(mode=0o700)
    wrapper = binaries / "ssh"
    wrapper.write_text("#!/bin/sh\nexec /usr/bin/ssh -F " + shlex.quote(str(native_ssh_config)) + ' "$@"\n')
    wrapper.chmod(0o700)
    monkeypatch.setenv("PATH", str(binaries) + os.pathsep + os.defpath)
    return NativeController()


def remote_python(controller: NativeController, source: str, arguments: Sequence[str]) -> Completed:
    command = "exec " + RUNTIME_PYTHON + " -c " + shlex.join([source, *arguments])
    return controller.ssh(["-T", ALIAS, command], capture_output=True, timeout=20)


def browser_pid(controller: NativeController) -> int:
    result = controller.ssh(
        ["-T", ALIAS, "systemctl --user show sarj-browser.service --property=MainPID --value"],
        capture_output=True,
        timeout=10,
    )
    pid = int(result.stdout.strip())
    assert pid > 0, "The persistent browser must already be running before opting in"
    return pid


@contextmanager
def reverse_channel(bridge: HostBridge) -> Generator[ChildProcess]:
    assert bridge.remote_socket is not None
    assert bridge.local_socket is not None
    command = "exec " + RUNTIME_PYTHON + " -c " + shlex.join([REGISTER, bridge.connection])
    child = spawn(
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
            "StreamLocalBindMask=0177",
            "-o",
            "StreamLocalBindUnlink=no",
            "-R",
            f"{bridge.remote_socket}:{bridge.local_socket}",
            ALIAS,
            command,
        ],
        stdin=PIPE,
        stdout=PIPE,
        stderr=DEVNULL,
    )
    try:
        await_forward(child)
        yield child
    finally:
        try:
            if child.stdin is not None:
                child.stdin.close()
            assert child.wait(timeout=10) == 0
        finally:
            try:
                transfer.stop_transfer(child)
            finally:
                if child.stdout is not None:
                    child.stdout.close()


@pytest.fixture
def pixel_png(tmp_path: Path) -> Path:
    def chunk(kind: bytes, body: bytes) -> bytes:
        return struct.pack("!I", len(body)) + kind + body + struct.pack("!I", zlib.crc32(kind + body))

    header = chunk(b"IHDR", struct.pack("!2I5B", 1, 1, 8, 6, 0, 0, 0))
    pixels = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00\xff"))
    selected = tmp_path / "native 1px image.png"
    selected.write_bytes(b"\x89PNG\r\n\x1a\n" + header + pixels + chunk(b"IEND", b""))
    return selected


def test_real_ssh_return_channel_browser_forward_and_installed_binary_transfer(
    native_controller: NativeController,
    pixel_png: Path,
    tmp_path: Path,
) -> None:
    before = browser_pid(native_controller)
    downloads = tmp_path / "Downloads"
    expected = pixel_png.read_bytes()
    checksum = hashlib.sha256(expected).hexdigest()
    with HostBridge(native_controller, route_factory=start_route) as bridge, reverse_channel(bridge):
        assert bridge.route is not None
        reply = HostReply.model_validate_json(
            remote_python(native_controller, OPEN_BROWSER, [bridge.connection]).stdout
        )
        assert reply.ok
        assert native_controller.opened == [bridge.route.url]
        uploaded = transfer.send(native_controller, [pixel_png], control=bridge.transfers)
        assert len(uploaded) == 1
        receipt = uploaded[0]
        try:
            assert receipt.sha256 == checksum
            assert receipt.size == len(expected)
            evidence = FileEvidence.model_validate_json(
                remote_python(
                    native_controller,
                    UPLOADED_FILE,
                    [receipt.path, checksum, str(len(expected)), "inspect"],
                ).stdout
            )
            assert evidence == FileEvidence(size=len(expected), sha256=checksum, file_mode=0o600, batch_mode=0o700)
            downloaded = transfer.get(native_controller, receipt.path, downloads, control=bridge.transfers)
            selected = Path(downloaded.path)
            assert downloaded.sha256 == checksum
            assert selected.read_bytes() == expected
            assert stat.S_IMODE(selected.stat().st_mode) == 0o600
            with pytest.raises(FileExistsError):
                transfer.get(native_controller, receipt.path, downloads, control=bridge.transfers)
            assert selected.read_bytes() == expected
            assert not list(downloads.glob(".incoming-*"))
            assert browser_pid(native_controller) == before
        finally:
            remote_python(native_controller, UPLOADED_FILE, [receipt.path, checksum, str(len(expected)), "remove"])
    closed = remote_python(native_controller, CLOSED_CONNECTION, [bridge.connection])
    assert closed.stdout.strip() == "closed"
    assert browser_pid(native_controller) == before
