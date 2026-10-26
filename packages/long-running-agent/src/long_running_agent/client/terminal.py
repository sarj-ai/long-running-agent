from __future__ import annotations

import os
import select
import signal
import termios
from threading import current_thread, main_thread
import time
from typing import TYPE_CHECKING, Literal, Self


if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType, TracebackType

type TerminalAttributes = list[int | list[bytes | int]] | list[int | list[bytes]] | list[int | list[int]]

type SignalHandler = Callable[[int, FrameType | None], object] | int | None
# Restore local pointer/keyboard behavior; keep terminal contents intact.
REMOTE_MODES = (9, 1000, 1001, 1002, 1003, 1004, 1005, 1006, 1015, 1016, 2004)
# Return to the main buffer after a lost full-screen SSH application. These
# mode resets preserve its scrollback; RIS and erase-display are deliberately
# absent. Restore the saved cursor before enabling it in the local shell.
# https://invisible-island.net/xterm/ctlseqs/ctlseqs.html
RESTORE_MODES = (
    b"".join(f"\x1b[?{mode}l".encode() for mode in REMOTE_MODES) + b"\x1b[?1049l\x1b[?1047l\x1b[?47l\x1b[?25h"
)


def _interrupted(signum: int, _frame: FrameType | None) -> None:
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


class TerminalState:
    def __init__(self, descriptor: int | None = None) -> None:
        self._requested_descriptor = descriptor
        self._descriptor: int | None = None
        self._attributes: TerminalAttributes | None = None
        self._handlers: dict[int, SignalHandler] = {}

    def __enter__(self) -> Self:
        if self._requested_descriptor is not None:
            if not os.isatty(self._requested_descriptor):
                return self
            descriptor = os.dup(self._requested_descriptor)
        else:
            try:
                descriptor = os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
            except OSError:
                return self
        try:
            self._attributes = termios.tcgetattr(descriptor)
        except OSError, termios.error:
            os.close(descriptor)
            return self
        self._descriptor = descriptor
        if current_thread() is main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self._handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, _interrupted)
        return self

    def restore(self) -> None:
        descriptor, self._descriptor = self._descriptor, None
        if descriptor is None:
            return
        try:
            # A saturated/disconnected TTY must not hang cleanup. The duplicate
            # descriptor shares its file status flags, so restore them too.
            blocking = os.get_blocking(descriptor)
            try:
                os.set_blocking(descriptor, False)
                remaining = RESTORE_MODES
                deadline = time.monotonic() + 0.25
                while remaining and time.monotonic() < deadline:
                    try:
                        written = os.write(descriptor, remaining)
                        remaining = remaining[written:]
                    except BlockingIOError:
                        select.select([], [descriptor], [], max(0, deadline - time.monotonic()))
                    except OSError:
                        break
            finally:
                os.set_blocking(descriptor, blocking)
            try:
                # Bytes intended for the lost remote session must never become
                # accidental commands or pointer reports in the local shell.
                termios.tcflush(descriptor, termios.TCIFLUSH)
                if self._attributes is not None:
                    termios.tcsetattr(descriptor, termios.TCSANOW, self._attributes)
            except OSError, termios.error:
                pass  # A disconnected controlling terminal cannot be repaired.
        finally:
            os.close(descriptor)
            for signum, handler in self._handlers.items():
                signal.signal(signum, handler)
            self._handlers.clear()

    def __exit__(
        self,
        _exception_type: type[BaseException] | None,
        _exception: BaseException | None,
        _traceback: TracebackType | None,
    ) -> Literal[False]:
        self.restore()
        return False
