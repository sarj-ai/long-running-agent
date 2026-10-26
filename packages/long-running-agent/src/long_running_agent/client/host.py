from __future__ import annotations

from contextlib import AbstractContextManager, suppress
from pathlib import Path
import platform
import re
import secrets
import shlex
from socketserver import BaseRequestHandler, ThreadingMixIn, UnixStreamServer
import tempfile
from threading import BoundedSemaphore, Event, Lock, Thread
from typing import TYPE_CHECKING, Self, override

from rich.console import Console

from long_running_agent.client import transfer
from long_running_agent.config import REMOTE_RUNTIME
from long_running_agent.host_connection import HostReply, HostRequest, read_message
from long_running_agent.process import DEVNULL, PIPE, ChildProcess, CommandError, ProcessTimeout, spawn


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    import socket

    from long_running_agent.client.browser import BrowserRoute
    from long_running_agent.client.controller import Client


MAX_SELECTION = 8192
MAX_UNIX_SOCKET_PATH = 107


def native_output(child: ChildProcess, output: bytes) -> str:
    if child.returncode != 0:
        message = "The Mac request was cancelled or could not be completed"
        raise RuntimeError(message)
    if len(output) > MAX_SELECTION:
        message = "The Mac returned an oversized selection"
        raise RuntimeError(message)
    return output.decode("utf-8").removesuffix("\n")


IMAGE_PROMPT = """on run argv
display dialog ("Send an image to " & item 1 of argv & "?") with title "Sarj VM" buttons {"Cancel", "Choose File", "Use Clipboard"} default button "Use Clipboard" cancel button "Cancel" giving up after 90
if gave up of result then error number -128
return button returned of result
end run"""
FILE_PROMPT = """on run argv
set selectedFile to choose file with prompt "Choose an image or file to send to your VM"
return POSIX path of selectedFile
end run"""
DOWNLOAD_PROMPT = """on run argv
display dialog ("Save this file from " & item 1 of argv & " in your Mac Downloads folder?" & linefeed & item 2 of argv) with title "Sarj VM" buttons {"Cancel", "Save File"} default button "Save File" cancel button "Cancel" giving up after 90
if gave up of result then error number -128
return button returned of result
end run"""
CLIPBOARD_PNG = """ObjC.import('AppKit');
function run(argv) {
    const board = $.NSPasteboard.generalPasteboard;
    let image = board.dataForType($.NSPasteboardTypePNG);
    if (!image || image.isNil()) {
        const tiff = board.dataForType($.NSPasteboardTypeTIFF);
        if (!tiff || tiff.isNil() || Number(tiff.length) > 104857600) throw Error('No clipboard image');
        const bitmap = $.NSBitmapImageRep.imageRepWithData(tiff);
        if (!bitmap || bitmap.isNil()) throw Error('No clipboard image');
        image = bitmap.representationUsingTypeProperties($.NSBitmapImageFileTypePNG, $({}));
    }
    if (!image || image.isNil() || Number(image.length) > 104857600) throw Error('Clipboard image unavailable');
    if (!image.writeToFileAtomically($(argv[0]), true)) throw Error('Cannot save clipboard image');
    return 'saved';
}"""


class BoundedServer(ThreadingMixIn, UnixStreamServer):
    daemon_threads = True
    block_on_close = False
    request_queue_size = 4

    def __init__(self, address: str, handler: type[BaseRequestHandler]) -> None:
        self.slots = BoundedSemaphore(4)
        super().__init__(address, handler)

    @override
    def process_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: str) -> None:
        if not self.slots.acquire(blocking=False):
            channel = request[1] if isinstance(request, tuple) else request
            channel.close()
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    @override
    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket], client_address: str) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


class HostBridge(AbstractContextManager["HostBridge"]):
    def __init__(self, controller: Client, *, route_factory: Callable[[Client], BrowserRoute]) -> None:
        self.controller = controller
        self.route_factory = route_factory
        self.connection = secrets.token_hex(16)
        self.label = re.sub(r"[^A-Za-z0-9 ._-]", "-", platform.node()).strip(" ._-")[:80] or "Mac"
        self.local_socket: Path | None = None
        self.remote_socket: str | None = None
        self.temporary: tempfile.TemporaryDirectory[str] | None = None
        self.server: UnixStreamServer | None = None
        self.worker: Thread | None = None
        self.stopping = Event()
        self.process_lock = Lock()
        self.action_lock = Lock()
        self.child: ChildProcess | None = None
        self.route: BrowserRoute | None = None
        self.transfers = transfer.TransferControl()

    def command(self, arguments: Sequence[str], *, timeout: float = 90) -> str:
        with self.process_lock:
            if self.stopping.is_set():
                message = "This Mac connection is closing"
                raise RuntimeError(message)
            child = spawn(arguments, stdin=DEVNULL, stdout=PIPE, stderr=DEVNULL)
            self.child = child
        try:
            output, _error = child.communicate(timeout=timeout)
            return native_output(child, output)
        except ProcessTimeout:
            message = "The Mac request timed out"
            raise TimeoutError(message) from None
        finally:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except ProcessTimeout:
                    child.kill()
                    child.wait(timeout=2)
            with self.process_lock:
                self.child = None
            if child.stdout is not None:
                child.stdout.close()

    def clipboard_image(self, destination: Path) -> None:
        if platform.system() != "Darwin":
            message = "Clipboard images are available from the connecting Mac"
            raise RuntimeError(message)
        self.command(["/usr/bin/osascript", "-l", "JavaScript", "-e", CLIPBOARD_PNG, str(destination)], timeout=30)
        destination.chmod(0o600)
        if (
            not destination.is_file()
            or destination.is_symlink()
            or not 0 < destination.stat().st_size <= 100 * 1024 * 1024
        ):
            message = "The clipboard does not contain a supported image of at most 100 MiB"
            raise RuntimeError(message)

    def handle(self, request: HostRequest) -> HostReply:
        if request.action == "status":
            return HostReply(ok=True, message="Mac terminal return channel is connected")
        if request.action == "browser":
            if self.route is None or self.route.child.poll() is not None:
                message = "The browser route is unavailable; reconnect using sarj-connect terminal"
                raise RuntimeError(message)
            self.controller.runner(["/usr/bin/open", self.route.url], capture_output=True, timeout=15)
            return HostReply(
                ok=True, message="VM Chrome opened on your Mac. Sign in there; its website sessions stay on the VM."
            )
        if request.action == "download":
            if request.path is None:
                message = "Select a VM file to download"
                raise ValueError(message)
            self.command(["/usr/bin/osascript", "-e", DOWNLOAD_PROMPT, self.controller.settings.instance, request.path])
            receipt = transfer.get(
                self.controller, request.path, self.controller.home / "Downloads", control=self.transfers
            )
            return HostReply(ok=True, message="Saved in your Mac Downloads folder", paths=[receipt.path])
        choice = self.command(["/usr/bin/osascript", "-e", IMAGE_PROMPT, self.controller.settings.instance])
        match choice:
            case "Choose File":
                path = Path(self.command(["/usr/bin/osascript", "-e", FILE_PROMPT]))
                receipts = transfer.send(self.controller, [path], control=self.transfers)
            case "Use Clipboard":
                with tempfile.TemporaryDirectory(prefix="lra-image-") as temporary:
                    image = Path(temporary) / "clipboard.png"
                    self.clipboard_image(image)
                    receipts = transfer.send(self.controller, [image], control=self.transfers)
            case _:
                message = "The image request was cancelled"
                raise RuntimeError(message)
        return HostReply(
            ok=True, message="Image sent to your private VM inbox", paths=[receipt.path for receipt in receipts]
        )

    def dispatch(self, request: HostRequest) -> HostReply:
        if request.action == "status":
            return self.handle(request)
        if self.stopping.is_set() or not self.action_lock.acquire(blocking=False):
            return HostReply(ok=False, message="The Mac is handling another request; try again shortly.")
        try:
            return self.handle(request)
        finally:
            self.action_lock.release()

    @override
    def __enter__(self) -> Self:
        super().__enter__()
        try:
            return self.start()
        except BaseException:
            self.__exit__()
            raise

    def start(self) -> Self:
        self.controller.ensure_connection()
        try:
            prepared = self.controller.remote(
                "runtime host prepare " + self.connection, capture_output=True, timeout=15
            )
        except CommandError as error:
            if error.returncode not in {2, 127}:
                raise
            message = "Ask your administrator to install the matching VM connection runtime. Use sarj-connect terminal --no-host to attach meanwhile."
            raise RuntimeError(message) from None
        endpoint = prepared.stdout.strip()
        if (
            not endpoint.startswith("/run/user/")
            or not endpoint.endswith("/lra-host/" + self.connection + ".sock")
            or any(character in endpoint for character in ":\r\n\0 ")
            or len(endpoint.encode()) > MAX_UNIX_SOCKET_PATH
        ):
            message = "The VM returned an invalid private return-channel path"
            raise ValueError(message)
        self.remote_socket = endpoint
        try:
            self.route = self.route_factory(self.controller)
        except OSError, ValueError, RuntimeError:
            Console(stderr=True).print(
                "VM browser unavailable; image and file requests remain available.", markup=False
            )
        # Short, private paths fit both macOS and Linux sockaddr_un limits.
        self.temporary = tempfile.TemporaryDirectory(prefix="lra-host-", dir="/tmp")
        self.local_socket = Path(self.temporary.name) / "host.sock"
        bridge = self

        class Handler(BaseRequestHandler):
            @override
            def handle(self) -> None:
                channel: socket.socket = self.request  # pyright: ignore[reportAny] -- UnixStreamServer supplies a connected stream socket.
                channel.settimeout(3)
                try:
                    reply = bridge.dispatch(HostRequest.model_validate_json(read_message(channel)))
                except OSError, ValueError, RuntimeError:
                    reply = HostReply(
                        ok=False, message="Request cancelled or unavailable. Check the connecting Mac terminal."
                    )
                with suppress(OSError):
                    channel.sendall(reply.model_dump_json().encode() + b"\n")

        self.server = BoundedServer(str(self.local_socket), Handler)
        self.local_socket.chmod(0o600)
        server = self.server

        def serve() -> None:
            while not self.stopping.is_set():
                server.handle_request()

        server.timeout = 0.2
        self.worker = Thread(target=serve, name="lra-host-return-channel", daemon=True)
        self.worker.start()
        return self

    def terminal(self, session: str) -> int:
        if self.remote_socket is None or self.local_socket is None:
            message = "The private host connection is not ready"
            raise RuntimeError(message)
        command = f"exec {REMOTE_RUNTIME} runtime host attach " + shlex.join(
            [self.connection, self.label, "--session", session]
        )
        Console(stderr=True).print(
            "From the VM: sarj-host browser URL opens Chrome on this Mac; sarj-host image sends an image.\n"
            "sarj-host download PATH saves a selected VM file after confirmation.",
            markup=False,
        )
        result = self.controller.ssh(
            [
                "-tt",
                "-o",
                "ControlMaster=no",
                "-o",
                "ControlPath=none",
                "-o",
                "ExitOnForwardFailure=yes",
                "-o",
                "StreamLocalBindMask=0177",
                "-o",
                "StreamLocalBindUnlink=no",
                "-R",
                f"{self.remote_socket}:{self.local_socket}",
                self.controller.settings.ssh_alias,
                command,
            ],
            check=False,
        )
        return result.returncode

    @override
    def __exit__(self, *_exc: object) -> None:
        self.stopping.set()
        try:
            self.transfers.cancel()
        finally:
            self.close_local()

    def close_local(self) -> None:
        try:
            self.close_requests()
        finally:
            try:
                if self.route is not None:
                    self.route.close()
            finally:
                try:
                    if self.server is not None:
                        self.server.server_close()
                finally:
                    if self.temporary is not None:
                        self.temporary.cleanup()

    def close_requests(self) -> None:
        with self.process_lock:
            child = self.child
        if child is not None:
            transfer.stop_transfer(child)
        if self.action_lock.acquire(timeout=3):
            self.action_lock.release()
        if self.worker is not None:
            self.worker.join(timeout=3)
