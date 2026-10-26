from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

import errno
import os
import pty
import select
import signal
import subprocess
import sys
import termios
import time

from long_running_agent.client.terminal import REMOTE_MODES, RESTORE_MODES, TerminalAttributes, TerminalState
import pytest


def collect(master: int, child: subprocess.Popen[bytes], timeout: float = 5) -> bytes:
    deadline = time.monotonic() + timeout
    result = bytearray()
    while time.monotonic() < deadline:
        if select.select([master], [], [], 0.05)[0]:
            try:
                block = os.read(master, 65536)
            except OSError as error:
                if error.errno == errno.EIO:
                    break
                raise
            if not block:
                break
            result.extend(block)
        elif child.poll() is not None:
            break
    child.wait(timeout=2)
    return bytes(result)


def assert_restored(descriptor: int, before: TerminalAttributes) -> None:
    actual: TerminalAttributes = termios.tcgetattr(descriptor)
    # Darwin sets its transient PENDIN flag when canonical input is restored.
    # The kernel clears it on the next read; every persistent flag must match.
    actual_flags = actual[3]
    assert isinstance(actual_flags, int)
    actual[3] = actual_flags & ~termios.PENDIN
    expected: list[object] = list(before)
    before_flags = expected[3]
    assert isinstance(before_flags, int)
    expected[3] = before_flags & ~termios.PENDIN
    assert actual == expected


def test_abrupt_remote_disconnect_restores_real_terminal_and_mouse_modes() -> None:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    # Simulate SSH's remote application changing tty flags and then losing its
    # connection without a chance to send mouse-reset sequences itself.
    remote = "import os,tty; tty.setraw(0); os.write(1,b'\\x1b[?1049h\\x1b[?25l\\x1b[?1003h\\x1b[?1006h\\x1b[?2004hREMOTE_READY'); os._exit(255)"
    program = (
        "import subprocess,sys; from long_running_agent.client.terminal import TerminalState; "
        "state=TerminalState(0); "
        "exec(\"with state:\\n subprocess.run([sys.executable, '-c', sys.argv[1]], check=False)\")"
    )
    child = subprocess.Popen([sys.executable, "-c", program, remote], stdin=slave, stdout=slave, stderr=slave)
    try:
        output = collect(master, child)
        assert b"REMOTE_READY" in output
        assert_restored(slave, before)
        assert RESTORE_MODES in output
        for mode in REMOTE_MODES:
            assert f"\x1b[?{mode}l".encode() in output
        assert b"\x1b[?1049l" in output
        assert b"\x1b[?25h" in output
        assert b"\x1b[3J" not in output  # Never erase scrollback.
        assert b"\x1b[2J" not in output  # Never clear the main display.
        assert b"\x1bc" not in output  # Never perform a destructive full reset.
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("interrupt", [signal.SIGTERM, signal.SIGHUP, signal.SIGINT])
def test_signal_interrupt_restores_attributes_and_terminal_modes(interrupt: signal.Signals) -> None:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    program = """
import os,signal,time,tty
from long_running_agent.client.terminal import TerminalState
with TerminalState(0):
    tty.setraw(0)
    os.write(1,b'READY')
    while True: time.sleep(1)
"""
    child = subprocess.Popen([sys.executable, "-c", program], stdin=slave, stdout=slave, stderr=slave)
    try:
        assert select.select([master], [], [], 3)[0]
        assert b"READY" in os.read(master, 4096)
        child.send_signal(interrupt)
        output = collect(master, child)
        assert RESTORE_MODES in output
        assert_restored(slave, before)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
        os.close(slave)


def test_context_is_harmless_without_a_terminal(tmp_path: Path) -> None:
    descriptor = os.open(tmp_path / "pipe-output", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with TerminalState(descriptor):
            pass
        assert os.fstat(descriptor).st_size == 0
    finally:
        os.close(descriptor)


def test_restore_is_idempotent() -> None:
    master, slave = pty.openpty()
    try:
        state = TerminalState(slave)
        with state:
            state.restore()
        assert os.read(master, len(RESTORE_MODES)) == RESTORE_MODES
        assert not select.select([master], [], [], 0.01)[0]
    finally:
        os.close(master)
        os.close(slave)


def test_queued_remote_mouse_reports_are_not_left_for_the_local_shell() -> None:
    master, slave = pty.openpty()
    try:
        with TerminalState(slave):
            attributes = termios.tcgetattr(slave)
            attributes[3] &= ~(termios.ECHO | termios.ICANON)
            termios.tcsetattr(slave, termios.TCSANOW, attributes)
            os.write(master, b"\x1b[<35;94;1M")
            assert select.select([slave], [], [], 1)[0]
        assert not select.select([slave], [], [], 0.05)[0]
    finally:
        os.close(master)
        os.close(slave)


def test_terminating_interactive_client_reaps_owned_remote_child() -> None:
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    remote = "import os,time,tty;tty.setraw(0);os.write(1,('CHILD:'+str(os.getpid())+'\\n').encode());time.sleep(60)"
    program = (
        "import sys;from long_running_agent.client.terminal import TerminalState;"
        "from long_running_agent.process import run;"
        "exec(\"with TerminalState(0):\\n run([sys.executable,'-c',sys.argv[1]])\")"
    )
    child = subprocess.Popen([sys.executable, "-c", program, remote], stdin=slave, stdout=slave, stderr=slave)
    try:
        assert select.select([master], [], [], 3)[0]
        ready = os.read(master, 4096)
        assert ready.startswith(b"CHILD:")
        remote_pid = int(ready.split(b":", 1)[1].strip())
        child.send_signal(signal.SIGTERM)
        output = collect(master, child)
        assert RESTORE_MODES in output
        assert_restored(slave, before)
        with pytest.raises(ProcessLookupError):
            os.kill(remote_pid, 0)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        os.close(master)
        os.close(slave)
