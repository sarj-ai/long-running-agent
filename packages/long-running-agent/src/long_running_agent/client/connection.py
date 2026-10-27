from __future__ import annotations

from contextlib import suppress
import os
import signal
import socket
from typing import TYPE_CHECKING

from long_running_agent.process import DEVNULL, PIPE, ChildProcess, ProcessTimeout, spawn


if TYPE_CHECKING:
    from collections.abc import Sequence


def loopback_port(preferred_port: int) -> int:
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # Keep the browser origin stable across reconnects. Never reuse another listener.
        try:
            probe.bind(("127.0.0.1", preferred_port))
        except OSError:
            probe.bind(("127.0.0.1", 0))
        address: tuple[str, int] = probe.getsockname()  # pyright: ignore[reportAny] -- AF_INET sockets expose a concrete host and port.
        return address[1]


def start_channel(arguments: Sequence[str], *, stdout: int = DEVNULL) -> ChildProcess:
    return spawn(arguments, stdin=PIPE, stdout=stdout, stderr=DEVNULL)


def close_child(child: ChildProcess) -> None:
    if child.stdin is not None:
        child.stdin.close()
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=5)
        except ProcessTimeout:
            child.kill()
            child.wait(timeout=5)


def launch_master(arguments: Sequence[str], *, timeout: float = 30) -> int:
    # OpenSSH forks its master after starting ProxyCommand. Isolate the whole
    # initial process tree so the proxy cannot inherit the interactive TTY's
    # session and receive SIGHUP when that terminal's owner exits.
    child = spawn(arguments, stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, start_new_session=True)
    try:
        return child.wait(timeout=timeout)
    except ProcessTimeout:
        _kill_master_group(child)
        return 124
    except BaseException:
        _kill_master_group(child)
        raise


def _kill_master_group(child: ChildProcess) -> None:
    # An unreaped leader reserves the process-group ID. Never signal a reused ID.
    if child.returncode is None:
        with suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGKILL)
    child.wait()
