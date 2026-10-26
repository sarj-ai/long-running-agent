from __future__ import annotations

from dataclasses import dataclass
import errno
import os
import pty
import select
import subprocess
import sys
import time

from long_running_agent.client.terminal import RESTORE_MODES


PROGRAM = r"""
import fcntl,os,subprocess,sys,termios,tty
fcntl.ioctl(0,termios.TIOCSCTTY,0)
from long_running_agent import Client
from long_running_agent.config import Settings
class Offline(Client):
    def ensure_connection(self):pass
    def ssh(self,*args,**kwargs):
        assert kwargs["check"] is False
        tty.setraw(0)
        os.write(1,b"\x1b[?1003h\x1b[?1006h")
        return subprocess.CompletedProcess(["ssh"],255,"","")
raise SystemExit(Offline(Settings()).shell([]))
"""


@dataclass(frozen=True)
class PtyResult:
    output: bytes
    returncode: int


def run_offline_client() -> PtyResult:
    master, slave = pty.openpty()
    child = subprocess.Popen(
        [sys.executable, "-c", PROGRAM], stdin=slave, stdout=slave, stderr=slave, start_new_session=True
    )
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                try:
                    block = os.read(master, 65536)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not block:
                    break
                output.extend(block)
            elif child.poll() is not None:
                break
        return PtyResult(bytes(output), child.wait(timeout=2))
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        os.close(master)
        os.close(slave)


def test_lost_connection_restores_owned_tty_before_friendly_message_and_vm_summary() -> None:
    result = run_offline_client()
    assert result.returncode == 255
    message = b"SSH connection closed. Run vm ssh to reconnect."
    assert message in result.output
    assert result.output.index(RESTORE_MODES) < result.output.index(message) < result.output.index(b"VM: agent-vm")
    assert b"Project: example-project" in result.output
    assert b"ssh exited" not in result.output
