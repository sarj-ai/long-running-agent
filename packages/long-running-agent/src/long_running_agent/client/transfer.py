from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
from threading import Event, Lock
import time
from typing import TYPE_CHECKING, Final

from long_running_agent.client.connection import close_child
from long_running_agent.config import REMOTE_RUNTIME
from long_running_agent.process import DEVNULL, PIPE, ChildProcess, ProcessTimeout, spawn
from long_running_agent.runtime.transfer import (
    MAX_FILES,
    MAX_METADATA_BYTES,
    MAX_RECEIPT_BYTES,
    TRANSFER_SECONDS,
    Channel,
    FileSpec,
    FileState,
    Manifest,
    Receipt as Receipt,
    Receipts,
    channel,
    copy_file,
    directory,
    new_file,
    remaining,
    source_file,
    specification,
)


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from long_running_agent.client.controller import Client


MAX_REMOTE_PATH_BYTES: Final = 4096


@dataclass(frozen=True)
class PreparedFile:
    descriptor: int
    state: FileState
    file: FileSpec


def stop_transfer(child: ChildProcess) -> None:
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=1)
    except ProcessTimeout:
        child.kill()
        child.wait(timeout=1)


class TransferControl:
    def __init__(self) -> None:
        self.cancelled = Event()
        self._lock = Lock()
        self._children: set[ChildProcess] = set()

    def check(self) -> None:
        if self.cancelled.is_set():
            message = "File transfer cancelled because its terminal connection closed"
            raise InterruptedError(message)

    def launch(self, arguments: Sequence[str]) -> ChildProcess:
        with self._lock:
            self.check()
            child = spawn(arguments, stdin=PIPE, stdout=PIPE, stderr=DEVNULL)
            self._children.add(child)
            return child

    def forget(self, child: ChildProcess) -> None:
        with self._lock:
            self._children.discard(child)

    def cancel(self) -> None:
        with self._lock:
            self.cancelled.set()
            children = tuple(self._children)
        failed = False
        for child in children:
            try:
                stop_transfer(child)
            except OSError, ProcessTimeout:
                failed = True
        if failed:
            message = "An owned file transfer could not be reaped after bounded cancellation"
            raise RuntimeError(message)


def check_control(control: TransferControl | None) -> None:
    if control is not None:
        control.check()


@contextmanager
def transport(controller: Client, arguments: Sequence[str], control: TransferControl | None) -> Generator[ChildProcess]:
    command = f"exec {REMOTE_RUNTIME} " + shlex.join(["runtime", "transfer", *arguments])
    native = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", controller.settings.ssh_alias, command]
    child = spawn(native, stdin=PIPE, stdout=PIPE, stderr=DEVNULL) if control is None else control.launch(native)
    try:
        yield child
        check_control(control)
    except OSError, RuntimeError, ValueError:
        check_control(control)
        raise
    finally:
        try:
            close_child(child)
        finally:
            try:
                if child.stdout is not None:
                    child.stdout.close()
            finally:
                if control is not None:
                    control.forget(child)


def completed(child: ChildProcess, deadline: float) -> None:
    try:
        code = child.wait(timeout=remaining(deadline))
    except ProcessTimeout:
        message = "File transfer exceeded its bounded deadline"
        raise TimeoutError(message) from None
    if code:
        message = "Private SSH file transfer failed; destination was not confirmed"
        raise RuntimeError(message)


def verify_receipts(receipts: Sequence[Receipt], prepared: Sequence[PreparedFile]) -> None:
    if len(receipts) != len(prepared) or len({receipt.path for receipt in receipts}) != len(receipts):
        message = "VM transfer receipts do not match the selected files"
        raise RuntimeError(message)
    for receipt, item in zip(receipts, prepared, strict=True):
        path = Path(receipt.path)
        if (
            any(not character.isprintable() for character in receipt.path)
            or ".." in path.parts
            or path.parts[0] != "/"
            or path.parts[-6:-2] != (".local", "share", "long-running-agent", "inbox")
            or re.fullmatch(r"[0-9a-f]{32}", path.parent.name) is None
        ):
            message = "VM transfer receipt is outside a private inbox destination"
            raise RuntimeError(message)
        if path.name != item.file.name or receipt.size != item.file.size or receipt.sha256 != item.file.sha256:
            message = "VM transfer receipt failed size or checksum verification"
            raise RuntimeError(message)


def upload(child: ChildProcess, prepared: Sequence[PreparedFile], deadline: float) -> list[Receipt]:
    if child.stdin is None or child.stdout is None:
        message = "Private SSH file transfer streams are unavailable"
        raise RuntimeError(message)
    manifest = Manifest(version=1, files=[item.file for item in prepared])
    metadata = manifest.model_dump_json().encode()
    if len(metadata) > MAX_METADATA_BYTES:
        message = "File transfer manifest exceeded its bounded size"
        raise RuntimeError(message)
    with channel(child.stdin, deadline) as outgoing:
        outgoing.write(metadata + b"\n")
        for item in prepared:
            copy_file(Channel(item.descriptor, deadline), outgoing, item.file)
            if FileState.read(item.descriptor) != item.state:
                message = "Selected local file changed during upload"
                raise RuntimeError(message)
    child.stdin.close()
    payload = bytearray()
    with channel(child.stdout, deadline) as incoming:
        while chunk := incoming.read(min(65536, MAX_RECEIPT_BYTES + 1 - len(payload))):
            payload.extend(chunk)
            if len(payload) > MAX_RECEIPT_BYTES:
                message = "VM transfer receipts exceeded their bounded size"
                raise RuntimeError(message)
    completed(child, deadline)
    receipts = Receipts.model_validate_json(payload).root
    verify_receipts(receipts, prepared)
    return receipts


def send(controller: Client, paths: Sequence[Path], *, control: TransferControl | None = None) -> list[Receipt]:
    if not 1 <= len(paths) <= MAX_FILES:
        message = "Select between one and sixteen regular files to send"
        raise ValueError(message)
    check_control(control)
    if control is None:
        controller.ensure_connection()
    deadline = time.monotonic() + TRANSFER_SECONDS
    with ExitStack() as stack:
        prepared: list[PreparedFile] = []
        for path in paths:
            check_control(control)
            descriptor = stack.enter_context(source_file(path.expanduser()))
            file = specification(path, descriptor, deadline)
            prepared.append(PreparedFile(descriptor, FileState.read(descriptor), file))
        Manifest(version=1, files=[item.file for item in prepared])
        with transport(controller, ["receive"], control) as child:
            return upload(child, prepared, deadline)


def get(controller: Client, remote: str, destination: Path, *, control: TransferControl | None = None) -> Receipt:
    check_control(control)
    if not remote or "\0" in remote or len(os.fsencode(remote)) > MAX_REMOTE_PATH_BYTES:
        message = "Select one explicit VM file path"
        raise ValueError(message)
    destination = destination.expanduser().absolute()
    destination.mkdir(mode=0o700, exist_ok=True)
    folder = directory(destination)
    try:
        if control is None:
            controller.ensure_connection()
        deadline = time.monotonic() + TRANSFER_SECONDS
        with transport(controller, ["send", "--", remote], control) as child:
            if child.stdin is None or child.stdout is None:
                message = "Private SSH file transfer streams are unavailable"
                raise RuntimeError(message)
            child.stdin.close()
            with channel(child.stdout, deadline) as incoming:
                file = FileSpec.model_validate_json(incoming.line())
                with new_file(folder, file.name) as output:
                    copy_file(incoming, Channel(output, deadline), file)
                    incoming.end()
                    completed(child, deadline)
                    check_control(control)
            return Receipt(path=str(destination / file.name), size=file.size, sha256=file.sha256)
    finally:
        os.close(folder)
