from __future__ import annotations

import errno
import os
from pathlib import Path
import pty
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time

from long_running_agent.client.connection import launch_master
import pytest


WORKER = r"""
import os,pathlib,socket,sys
server=socket.socket(socket.AF_UNIX)
server.bind(sys.argv[1]);server.listen();server.settimeout(12)
pathlib.Path(sys.argv[2]).write_text(f'{os.getpid()}\n{os.getsid(0)}\n{os.isatty(0)}\n')
for _ in range(2):
    connection,_=server.accept()
    with connection:
        connection.recv(1);connection.sendall(b'alive')
"""

OWNER = r"""
import fcntl,pathlib,signal,sys,termios
from long_running_agent.client.connection import launch_master
from long_running_agent.client.terminal import TerminalState
fcntl.ioctl(0,termios.TIOCSCTTY,0)
launcher='import subprocess,sys;subprocess.Popen([sys.executable,"-c",sys.argv[1],*sys.argv[2:]])'
with TerminalState():
    assert launch_master([sys.executable,'-c',launcher,sys.argv[1],sys.argv[2],sys.argv[3]])==0
    pathlib.Path(sys.argv[4]).touch()
    signal.pause()
"""


def ping(path: Path) -> bytes:
    with socket.socket(socket.AF_UNIX) as channel:
        channel.settimeout(2)
        channel.connect(str(path))
        channel.sendall(b"?")
        return channel.recv(16)


def await_ready(path: Path, descriptor: int, owner: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        if select.select([descriptor], [], [], 0.01)[0]:
            try:
                os.read(descriptor, 65536)
            except OSError as error:
                if error.errno != errno.EIO:
                    raise
        assert owner.poll() is None
    assert path.exists()


def verify_transport_survives(directory: Path) -> None:
    # Like OpenSSH's ProxyCommand, the worker is spawned before its launcher
    # exits and inherits that launcher's session. Its independent second
    # channel must keep working when the interactive client receives SIGTERM.
    socket_path, metadata, ready = directory / "socket", directory / "metadata", directory / "ready"
    master, slave = pty.openpty()
    owner = subprocess.Popen(
        [sys.executable, "-c", OWNER, WORKER, str(socket_path), str(metadata), str(ready)],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    try:
        await_ready(ready, master, owner)
        await_ready(metadata, master, owner)
        _pid, session, has_tty = metadata.read_text().splitlines()
        assert int(session) != owner.pid
        assert has_tty == "False"
        assert ping(socket_path) == b"alive"
        owner.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 3
        while owner.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
        assert owner.wait(timeout=1) == 143
        assert ping(socket_path) == b"alive"
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=2)
        os.close(master)
        os.close(slave)


def test_transport_descendant_survives_owning_interactive_tty_exit() -> None:
    # A short pathname fits macOS's sockaddr_un limit regardless of pytest's
    # platform-specific temporary directory prefix.
    with tempfile.TemporaryDirectory(prefix="lra-master-", dir="/tmp") as temporary:
        verify_transport_survives(Path(temporary))


def test_stalled_master_launcher_is_bounded_and_reaped(tmp_path: Path) -> None:
    record = tmp_path / "pid"
    program = "import os,pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));time.sleep(10)"
    assert launch_master([sys.executable, "-c", program, str(record)], timeout=0.5) == 124
    with pytest.raises(ProcessLookupError):
        os.kill(int(record.read_text()), 0)
