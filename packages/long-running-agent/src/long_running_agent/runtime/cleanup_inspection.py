from __future__ import annotations

import os
from pathlib import Path
import re
import secrets
import socket
import stat
import struct
import tempfile
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from long_running_agent.process import run

from .admin_worker import root_owned
from .cleanup_probe import REQUEST_SECONDS


if TYPE_CHECKING:
    from collections.abc import Sequence


SOCKET: Final = Path("/run/lra-cleanup-probe.sock")
WORKER: Final = Path("/opt/long-running-agent/cleanup_probe.py")
SOCKET_UNIT: Final = "lra-cleanup-probe.socket"
SERVICE_UNIT: Final = "lra-cleanup-probe@.service"
MAX_MESSAGE: Final = 2 * 1024 * 1024
MAX_CANDIDATES: Final = 1024
MAX_PATH: Final = 4096
TIMEOUT: Final = REQUEST_SECONDS + 5
PROBE_ATTEMPTS: Final = 3
RETRY_SECONDS: Final = 0.05
Candidate = Annotated[str, Field(min_length=1, max_length=MAX_PATH)]


class ProbeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = Field(default=1, alias="schema")
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    candidates: list[Candidate] = Field(max_length=MAX_CANDIDATES)


class ProbeReply(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    schema_version: Literal[1] = Field(alias="schema")
    nonce: str = Field(pattern=r"^[0-9a-f]{32}$")
    uid: int = Field(ge=1000)
    protected: list[Candidate] = Field(max_length=MAX_CANDIDATES)
    package_writer: bool
    zellij_running: bool
    process_count: int = Field(ge=1, le=16384)


type ProbeFailureReason = Literal[
    "unavailable",
    "request-invalid",
    "socket-trust",
    "connect-missing",
    "connect-refused",
    "transport",
    "deadline",
    "worker-rejected",
    "reply-empty",
    "reply-oversize",
    "reply-invalid",
    "reply-identity",
    "reply-scope",
    "worker-incomplete",
    "worker-deadline",
    "worker-process-limit",
    "worker-descriptor-limit",
    "worker-byte-budget",
    "worker-maps-too-large",
    "worker-metadata-too-large",
]
PROBE_FAILURE_REASONS: Final = frozenset(
    {
        "unavailable",
        "request-invalid",
        "socket-trust",
        "connect-missing",
        "connect-refused",
        "transport",
        "deadline",
        "worker-rejected",
        "reply-empty",
        "reply-oversize",
        "reply-invalid",
        "reply-identity",
        "reply-scope",
        "worker-incomplete",
        "worker-deadline",
        "worker-process-limit",
        "worker-descriptor-limit",
        "worker-byte-budget",
        "worker-maps-too-large",
        "worker-metadata-too-large",
    }
)


class ProbeUnavailableError(RuntimeError):
    def __init__(self, reason: ProbeFailureReason = "unavailable") -> None:
        self.reason: ProbeFailureReason = (
            reason if type(reason) is str and reason in PROBE_FAILURE_REASONS else "unavailable"
        )
        super().__init__("Read-only process inspection is unavailable; retain all worktrees")


def trusted_socket(endpoint: Path, *, owner: int) -> os.stat_result:
    reason: ProbeFailureReason
    for parent in reversed(endpoint.parents):
        metadata = parent.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != owner or metadata.st_mode & 0o022:
            reason = "socket-trust"
            raise ProbeUnavailableError(reason)
    metadata = endpoint.lstat()
    if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != owner:
        reason = "socket-trust"
        raise ProbeUnavailableError(reason)
    return metadata


def exchange(
    request: ProbeRequest, endpoint: Path = SOCKET, *, owner: int = 0, deadline: float | None = None
) -> ProbeReply:
    reason: ProbeFailureReason
    try:
        payload = request.model_dump_json(by_alias=True).encode() + b"\n"
    except OSError, ValueError:
        reason = "request-invalid"
        raise ProbeUnavailableError(reason) from None
    if len(payload) > MAX_MESSAGE:
        reason = "request-invalid"
        raise ProbeUnavailableError(reason)
    try:
        before = trusted_socket(endpoint, owner=owner)
    except FileNotFoundError:
        reason = "connect-missing"
        raise ProbeUnavailableError(reason) from None
    except OSError:
        reason = "socket-trust"
        raise ProbeUnavailableError(reason) from None
    maximum = time.monotonic() + TIMEOUT
    deadline = maximum if deadline is None else min(deadline, maximum)
    try:
        response = exchange_transport(payload, endpoint, owner, before, deadline)
    except TimeoutError:
        reason = "deadline"
        raise ProbeUnavailableError(reason) from None
    except OSError:
        reason = "transport"
        raise ProbeUnavailableError(reason) from None
    return checked_reply(request, response)


def exchange_transport(
    payload: bytes, endpoint: Path, owner: int, before: os.stat_result, deadline: float
) -> bytearray:
    reason: ProbeFailureReason
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "deadline"
            raise ProbeUnavailableError(reason)
        connection.settimeout(remaining)
        try:
            connection.connect(str(endpoint))
        except FileNotFoundError:
            reason = "connect-missing"
            raise ProbeUnavailableError(reason) from None
        except ConnectionRefusedError:
            reason = "connect-refused"
            raise ProbeUnavailableError(reason) from None
        try:
            _pid, peer_uid, _gid = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            after = trusted_socket(endpoint, owner=owner)
        except OSError, ValueError, struct.error:
            reason = "socket-trust"
            raise ProbeUnavailableError(reason) from None
        if peer_uid != owner or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            reason = "socket-trust"
            raise ProbeUnavailableError(reason)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            reason = "deadline"
            raise ProbeUnavailableError(reason)
        connection.settimeout(remaining)
        connection.sendall(payload)
        connection.shutdown(socket.SHUT_WR)
        response = bytearray()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "deadline"
                raise ProbeUnavailableError(reason)
            connection.settimeout(remaining)
            chunk = connection.recv(min(65536, MAX_MESSAGE + 1 - len(response)))
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_MESSAGE:
                reason = "reply-oversize"
                raise ProbeUnavailableError(reason)
    return response


WORKER_REASONS: Final[dict[bytes, ProbeFailureReason]] = {
    b"incomplete": "worker-incomplete",
    b"deadline": "worker-deadline",
    b"process-limit": "worker-process-limit",
    b"descriptor-limit": "worker-descriptor-limit",
    b"byte-budget": "worker-byte-budget",
    b"maps-too-large": "worker-maps-too-large",
    b"metadata-too-large": "worker-metadata-too-large",
}


def worker_failure(response: bytearray) -> ProbeFailureReason | None:
    whitespace = rb"[ \t\r\n]*"
    error = rb'"error"' + whitespace + rb":" + whitespace + rb'"inspection incomplete"'
    code = rb'"code"' + whitespace + rb":" + whitespace + rb'"([a-z-]{1,32})"'
    prefix, suffix = whitespace + rb"\{" + whitespace, whitespace + rb"\}" + whitespace
    if re.fullmatch(prefix + error + suffix, response):
        return "worker-rejected"
    for members in (error + whitespace + rb"," + whitespace + code, code + whitespace + rb"," + whitespace + error):
        if (match := re.fullmatch(prefix + members + suffix, response)) is not None:
            return WORKER_REASONS.get(match.group(1), "reply-invalid")
    return None


def checked_reply(request: ProbeRequest, response: bytearray) -> ProbeReply:
    reason: ProbeFailureReason
    if len(response) > MAX_MESSAGE:
        reason = "reply-oversize"
        raise ProbeUnavailableError(reason)
    if not response:
        reason = "reply-empty"
        raise ProbeUnavailableError(reason)
    failure_reason = worker_failure(response)
    if failure_reason is not None:
        raise ProbeUnavailableError(failure_reason)
    try:
        reply = ProbeReply.model_validate_json(response)
    except ValueError:
        reason = "reply-invalid"
        raise ProbeUnavailableError(reason) from None
    if reply.uid != os.getuid() or reply.nonce != request.nonce:
        reason = "reply-identity"
        raise ProbeUnavailableError(reason)
    protected = set(reply.protected)
    if len(protected) != len(reply.protected) or not protected.issubset(request.candidates):
        reason = "reply-scope"
        raise ProbeUnavailableError(reason)
    return reply


def probe_request(candidates: Sequence[Path]) -> ProbeRequest:
    reason: ProbeFailureReason
    try:
        return ProbeRequest(nonce=secrets.token_hex(16), candidates=[str(path) for path in candidates])
    except OSError, ValueError:
        reason = "request-invalid"
        raise ProbeUnavailableError(reason) from None


def inspect(candidates: Sequence[Path]) -> ProbeReply:
    reason: ProbeFailureReason
    deadline = time.monotonic() + TIMEOUT
    for attempt in range(PROBE_ATTEMPTS):
        # Each retry requires a fresh complete authenticated snapshot and nonce.
        request = probe_request(candidates)
        try:
            return exchange(request, deadline=deadline)
        except ProbeUnavailableError as error:
            if error.reason != "worker-incomplete" or attempt + 1 == PROBE_ATTEMPTS:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = "deadline"
                raise ProbeUnavailableError(reason) from None
            time.sleep(min(RETRY_SECONDS, remaining))
        except OSError, ValueError:
            reason = "transport"
            raise ProbeUnavailableError(reason) from None
    reason = "worker-incomplete"
    raise ProbeUnavailableError(reason)


def socket_text() -> str:
    return (
        "[Unit]\nDescription=Read-only process metadata for guarded developer cleanup\n"
        "[Socket]\nListenStream=/run/lra-cleanup-probe.sock\nAccept=yes\nSocketMode=0666\n"
        "SocketUser=root\nSocketGroup=root\nMaxConnections=2\nMaxConnectionsPerSource=1\n"
        "RemoveOnStop=true\n[Install]\nWantedBy=sockets.target\n"
    )


def service_text() -> str:
    return (
        "[Unit]\nDescription=Inspect only the connecting user's process references\n"
        "[Service]\nType=exec\nUser=root\nGroup=root\nUMask=0077\n"
        "ExecStart=/usr/bin/python3 -I /opt/long-running-agent/cleanup_probe.py\n"
        "StandardInput=socket\nStandardOutput=socket\nStandardError=journal\n"
        "NoNewPrivileges=true\nCapabilityBoundingSet=CAP_SYS_PTRACE CAP_DAC_READ_SEARCH\n"
        "ProtectSystem=strict\nProtectHome=read-only\nPrivateDevices=true\n"
        "ProtectKernelTunables=true\nProtectKernelModules=true\nProtectControlGroups=true\n"
        "RestrictSUIDSGID=true\nLockPersonality=true\nRestrictAddressFamilies=AF_UNIX\n"
        "Nice=19\nIOSchedulingClass=idle\nCPUQuota=50%\nMemoryMax=256M\nMemorySwapMax=0\n"
        "TasksMax=4\nRuntimeMaxSec=90\nTimeoutStopSec=2\nKillMode=control-group\nLimitCORE=0\n"
        "LogRateLimitIntervalSec=1h\nLogRateLimitBurst=10\n"
    )


def installation_commands(staging: Path) -> list[list[str]]:
    commands = [["sudo", "--", "/usr/bin/install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(WORKER.parent)]]
    commands.extend(
        ["sudo", "--", "/usr/bin/install", "-o", "root", "-g", "root", "-m", "0644", str(staging / name), str(target)]
        for name, target in (
            (WORKER.name, WORKER),
            (SOCKET_UNIT, Path("/etc/systemd/system") / SOCKET_UNIT),
            (SERVICE_UNIT, Path("/etc/systemd/system") / SERVICE_UNIT),
        )
    )
    return [
        *commands,
        ["sudo", "--", "/usr/bin/systemctl", "daemon-reload"],
        ["sudo", "--", "/usr/bin/systemctl", "enable", "--now", SOCKET_UNIT],
        ["sudo", "--", "/usr/bin/systemctl", "is-active", "--quiet", SOCKET_UNIT],
    ]


def prepare_host_probe() -> None:
    # Administrative installation is explicit; the unprivileged daemon never invokes sudo.
    for parent in (Path("/opt"), Path("/etc/systemd/system")):
        for component in reversed((parent, *parent.parents)):
            root_owned(component, directory=True)
    for target in (
        WORKER.parent,
        WORKER,
        Path("/etc/systemd/system") / SOCKET_UNIT,
        Path("/etc/systemd/system") / SERVICE_UNIT,
    ):
        if target.exists() or target.is_symlink():
            root_owned(target, directory=target == WORKER.parent)
    with tempfile.TemporaryDirectory(prefix="lra-cleanup-probe-") as temporary:
        staging = Path(temporary)
        (staging / WORKER.name).write_bytes(Path(__file__).with_name(WORKER.name).read_bytes())
        (staging / SOCKET_UNIT).write_text(socket_text(), encoding="utf-8")
        (staging / SERVICE_UNIT).write_text(service_text(), encoding="utf-8")
        for command in installation_commands(staging):
            run(command, capture_output=True, timeout=20)
