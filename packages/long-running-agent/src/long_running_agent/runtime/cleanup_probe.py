from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import pwd
import re
import select
import signal
import socket
import stat
import struct
import sys
import time
from typing import TYPE_CHECKING, Final, Literal, Protocol, TypeGuard


if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Set as AbstractSet
    from types import FrameType
    from typing import NoReturn


USERS: Final = Path("/etc/long-running-agent/users")
PROC: Final = Path("/proc")
MAX_MESSAGE_BYTES: Final = 2 * 1024 * 1024
MAX_REGISTRATION_BYTES: Final = 65536
MAP_BYTES: Final = 8 * 1024 * 1024
MAX_CANDIDATES: Final = 1024
MAX_PATH_BYTES: Final = 4096
MIN_USER_UID: Final = 1000
STAT_FIELDS: Final = 20
MAP_FIELDS: Final = 6
ERROR: Final = "inspection incomplete"
REQUEST_SECONDS: Final = 70
PROCESS_ATTEMPTS: Final = 2
PROCESS_EXIT_WAIT_SECONDS: Final = 0.1
PACKAGE_MANAGERS: Final = frozenset({"uv", "uvx", "pip", "pip3", "pnpm", "npm", "npx", "yarn", "bun"})


type InspectionCode = Literal[
    "incomplete",
    "deadline",
    "process-limit",
    "descriptor-limit",
    "byte-budget",
    "maps-too-large",
    "metadata-too-large",
]
INSPECTION_CODES: Final = frozenset(
    {
        "incomplete",
        "deadline",
        "process-limit",
        "descriptor-limit",
        "byte-budget",
        "maps-too-large",
        "metadata-too-large",
    }
)


class InspectionError(RuntimeError):
    def __init__(self, code: InspectionCode = "incomplete") -> None:
        self.code: InspectionCode = code if type(code) is str and code in INSPECTION_CODES else "incomplete"
        super().__init__(ERROR)


class InspectionDeadlineError(InspectionError):
    def __init__(self) -> None:
        super().__init__("deadline")


@dataclass(frozen=True)
class Limits:
    processes: int = 16384
    descriptors: int = 32768
    per_file_bytes: int = 1024 * 1024
    total_bytes: int = 32 * 1024 * 1024
    seconds: float = 60
    maps_bytes: int = MAP_BYTES


DEFAULT_LIMITS: Final = Limits()


@dataclass
class Budget:
    limits: Limits
    deadline: float
    processes: int = 0
    descriptors: int = 0
    bytes_read: int = 0

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise InspectionDeadlineError

    def process(self) -> None:
        self.check()
        self.processes += 1
        if self.processes > self.limits.processes:
            code = "process-limit"
            raise InspectionError(code)

    def exit_wait(self) -> float:
        self.check()
        return max(0.0, min(PROCESS_EXIT_WAIT_SECONDS, self.deadline - time.monotonic()))

    def descriptor(self) -> None:
        self.check()
        self.descriptors += 1
        if self.descriptors > self.limits.descriptors:
            code = "descriptor-limit"
            raise InspectionError(code)

    def read(self, directory: int, name: str) -> bytes:
        self.check()
        field_bytes = self.limits.maps_bytes if name == "maps" else self.limits.per_file_bytes
        maximum = min(field_bytes, self.limits.total_bytes - self.bytes_read)
        if maximum < 0:
            code = "byte-budget"
            raise InspectionError(code)
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=directory)
        try:
            stream = os.fdopen(descriptor, "rb")
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            payload = stream.read(maximum + 1)
        self.bytes_read += len(payload)
        self.check()
        if len(payload) > maximum:
            if self.bytes_read > self.limits.total_bytes:
                code = "byte-budget"
                raise InspectionError(code)
            code = "maps-too-large" if name == "maps" else "metadata-too-large"
            raise InspectionError(code)
        return payload


@dataclass(frozen=True)
class ProcessIdentity:
    uid: int
    start: int
    state: str


@dataclass(frozen=True)
class FileIdentity:
    device: int
    inode: int


@dataclass
class ProcessReferences:
    protected: set[Path] = field(default_factory=set)
    package_writer: bool = False
    zellij_running: bool = False
    process_count: int = 0

    def include(self, other: ProcessReferences) -> None:
        self.protected.update(other.protected)
        self.package_writer |= other.package_writer
        self.zellij_running |= other.zellij_running
        self.process_count += other.process_count


@dataclass(frozen=True)
class Request:
    nonce: str
    candidates: tuple[Path, ...]


class JsonLoader(Protocol):
    def __call__(
        self,
        payload: bytes,
        /,
        *,
        object_pairs_hook: Callable[[list[tuple[str, object]]], dict[str, object]],
    ) -> object: ...


def is_dictionary(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def fields(value: object) -> dict[str, object]:
    if not is_dictionary(value):
        raise InspectionError
    result: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise InspectionError
        result[key] = item
    return result


def unique_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InspectionError
        result[key] = value
    return result


def decode(payload: bytes, *, loader: JsonLoader = json.loads) -> dict[str, object]:
    return fields(loader(payload, object_pairs_hook=unique_fields))


def candidate_path(value: object, roots: tuple[Path, ...]) -> Path:
    if (
        not isinstance(value, str)
        or len(os.fsencode(value)) > MAX_PATH_BYTES
        or any(not char.isprintable() for char in value)
    ):
        raise InspectionError
    candidate = Path(value)
    if not candidate.is_absolute() or ".." in candidate.parts or str(candidate) != value:
        raise InspectionError
    if candidate in roots or not any(candidate.is_relative_to(root) for root in roots):
        raise InspectionError
    return candidate


def request(payload: bytes, home: Path) -> Request:
    if len(payload) > MAX_MESSAGE_BYTES:
        raise InspectionError
    value = decode(payload)
    if set(value) != {"schema", "nonce", "candidates"} or type(value["schema"]) is not int or value["schema"] != 1:
        raise InspectionError
    nonce = value["nonce"]
    selected = value["candidates"]
    if not isinstance(nonce, str) or re.fullmatch(r"[0-9a-f]{32}", nonce) is None:
        raise InspectionError
    if not is_object_list(selected):
        raise InspectionError
    if len(selected) > MAX_CANDIDATES:
        raise InspectionError
    candidates: dict[Path, None] = {}
    roots = (home / "code", Path("/tmp"), Path("/var/tmp"))  # ruff: ignore[hardcoded-temp-file] -- Fixed read-only query roots; this parser creates no files and grants no deletion authority.
    for item in selected:
        candidate = candidate_path(item, roots)
        if candidate in candidates:
            raise InspectionError
        candidates[candidate] = None
    return Request(nonce=nonce, candidates=tuple(candidates))


def root_owned(path: Path, *, directory: bool) -> None:
    metadata = path.lstat()
    correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not correct_type or metadata.st_uid != 0 or metadata.st_mode & 0o022:
        raise InspectionError


def registration_home(value: dict[str, object], uid: int, account: pwd.struct_passwd) -> Path:
    home = value.get("home")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise InspectionError
    if type(value.get("uid")) is not int or value["uid"] != uid:
        raise InspectionError
    if value.get("username") != account.pw_name or home != account.pw_dir or not isinstance(home, str):
        raise InspectionError
    path = Path(home)
    if not path.is_absolute() or path.parent != Path("/home") or str(path) != home:
        raise InspectionError
    return path


def registered_home(uid: int, users: Path = USERS) -> Path:
    if uid < MIN_USER_UID:
        raise InspectionError
    for ancestor in (*reversed(users.parents), users):
        root_owned(ancestor, directory=True)
    path = users / f"{uid}.json"
    root_owned(path, directory=False)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_mode & 0o022
            or metadata.st_nlink != 1
            or metadata.st_size > MAX_REGISTRATION_BYTES
        ):
            raise InspectionError
        payload = stream.read(MAX_REGISTRATION_BYTES + 1)
    if len(payload) > MAX_REGISTRATION_BYTES:
        raise InspectionError
    current = path.lstat()
    if (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino):
        raise InspectionError
    home = registration_home(decode(payload), uid, pwd.getpwuid(uid))
    root_owned(home.parent, directory=True)
    home_metadata = home.lstat()
    if not stat.S_ISDIR(home_metadata.st_mode) or home_metadata.st_uid != uid or home_metadata.st_mode & 0o077:
        raise InspectionError
    return home


def identity(directory: int, budget: Budget) -> ProcessIdentity:
    metadata = os.fstat(directory)
    contents = budget.read(directory, "stat")
    end = contents.rfind(b")")
    if end < 0:
        raise InspectionError
    columns = contents[end + 2 :].split()
    if len(columns) < STAT_FIELDS or columns[0] not in {
        b"R",
        b"S",
        b"D",
        b"Z",
        b"T",
        b"t",
        b"X",
        b"x",
        b"K",
        b"W",
        b"P",
        b"I",
    }:
        raise InspectionError
    start = int(columns[19])
    if start < 0:
        raise InspectionError
    return ProcessIdentity(uid=metadata.st_uid, start=start, state=columns[0].decode("ascii"))


def object_identity(metadata: os.stat_result) -> FileIdentity:
    return FileIdentity(device=metadata.st_dev, inode=metadata.st_ino)


def procfs_cwd(target: Path, metadata: os.stat_result, proc: Path) -> bool:
    if re.fullmatch(r"/proc/[0-9]+/fdinfo", str(target)) is None:
        return False
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    # A held procfs fdinfo directory can survive its original PID. Its kernel
    # object cannot contain a Git checkout; mount namespace equality is not
    # required when a hardened reader has its own read-only mount namespace.
    if metadata.st_dev == proc.stat().st_dev:
        return True
    try:
        return metadata.st_dev == (proc / "1/root/proc").stat().st_dev
    except OSError:
        return False


def cwd(directory: int, proc: Path) -> Path:
    descriptor = os.open("cwd", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=directory)
    try:
        metadata = os.fstat(descriptor)
        target = Path(os.readlink("cwd", dir_fd=directory))
        if not target.is_absolute() or object_identity(os.stat("cwd", dir_fd=directory)) != object_identity(metadata):
            raise InspectionError
        try:
            normalized = target.resolve(strict=True)
        except FileNotFoundError:
            # An unlinked directory cannot contain an existing checkout. Keep
            # its former path and still inspect every other process reference.
            if (metadata.st_nlink == 0 and str(target).endswith(" (deleted)")) or procfs_cwd(target, metadata, proc):
                return target
            raise InspectionError from None
        if object_identity(normalized.stat()) != object_identity(metadata):
            raise InspectionError
        return normalized
    finally:
        os.close(descriptor)


def protect(path: str, candidates: AbstractSet[Path], result: ProcessReferences) -> None:
    if not candidates or not path.startswith("/"):
        return
    for value in {path, path.removesuffix(" (deleted)")}:
        literal = Path(value)
        for selected in {literal, literal.resolve()}:
            result.protected.update(candidate for candidate in (selected, *selected.parents) if candidate in candidates)


def arguments(payload: bytes, candidates: AbstractSet[Path], result: ProcessReferences) -> None:
    for word in os.fsdecode(payload).split("\0"):
        title = Path(word.partition(" ")[0]).name
        result.package_writer |= title in PACKAGE_MANAGERS or "pnpm.cjs" in word or "npm-cli.js" in word
        result.zellij_running |= title == "zellij"
        result.protected.update(candidate for candidate in candidates if str(candidate) in word)


def descriptors(directory: int, candidates: AbstractSet[Path], budget: Budget, result: ProcessReferences) -> None:
    folder = os.open("fd", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=directory)
    try:
        with os.scandir(folder) as entries:
            for entry in entries:
                budget.descriptor()
                if not entry.name.isdigit():
                    raise InspectionError
                try:
                    target = os.readlink(entry.name, dir_fd=folder)
                except FileNotFoundError:
                    continue
                protect(target, candidates, result)
    finally:
        os.close(folder)


def mappings(payload: bytes, candidates: AbstractSet[Path], budget: Budget, result: ProcessReferences) -> None:
    normalized_paths: set[str] = set()
    for line in os.fsdecode(payload).splitlines():
        budget.check()
        columns = line.split(maxsplit=5)
        if len(columns) not in {5, 6} or re.fullmatch(r"[0-9a-f]+-[0-9a-f]+", columns[0]) is None:
            raise InspectionError
        if re.fullmatch(r"[r-][w-][x-][ps]", columns[1]) is None:
            raise InspectionError
        if (
            re.fullmatch(r"[0-9a-f]+", columns[2]) is None
            or re.fullmatch(r"[0-9a-f]+:[0-9a-f]+", columns[3]) is None
            or not columns[4].isdigit()
        ):
            raise InspectionError
        if len(columns) == MAP_FIELDS and columns[5].startswith("/"):
            if "\\012" in columns[5]:
                raise InspectionError
            path = columns[5]
            # Segments from this captured maps payload share their exact pathname snapshot.
            if path not in normalized_paths:
                protect(path, candidates, result)
                normalized_paths.add(path)


def inspect_process(
    process: Path, candidates: Collection[Path], uid: int, budget: Budget, *, proc: Path = PROC
) -> ProcessReferences:
    budget.check()
    directory = os.open(process, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        if os.fstat(directory).st_uid != uid:
            return ProcessReferences()
        before = identity(directory, budget)
        if before.uid != uid:
            raise InspectionError
        if before.state == "Z":
            if not confirmed_exit(process, timeout=budget.exit_wait()):
                raise InspectionError
            return ProcessReferences()
        result = ProcessReferences(process_count=1)
        selected = frozenset(candidates)
        protect(str(cwd(directory, proc)), selected, result)
        arguments(budget.read(directory, "cmdline"), selected, result)
        descriptors(directory, selected, budget, result)
        mappings(budget.read(directory, "maps"), selected, budget, result)
        after = identity(directory, budget)
        if after.uid != before.uid or after.start != before.start:
            raise InspectionError
        if after.state == "Z" and not confirmed_exit(process, timeout=budget.exit_wait()):
            raise InspectionError
        budget.check()
        return result
    finally:
        os.close(directory)


def confirmed_exit(path: Path, *, timeout: float = 0) -> bool:
    # A zombie leader may still have live threads; require a process pidfd.
    try:
        directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        directory = None
    try:
        try:
            descriptor = os.pidfd_open(int(path.name), 0)
        except ProcessLookupError:
            try:
                path.lstat()
            except FileNotFoundError:
                return True
            return False
        try:
            poller = select.poll()
            accepted = select.POLLIN | select.POLLHUP
            poller.register(descriptor, accepted)
            # Zombie state can precede the whole-group pidfd readiness signal.
            # Waiting never substitutes for that signal or resets the budget.
            milliseconds = int(max(0.0, min(timeout, PROCESS_EXIT_WAIT_SECONDS)) * 1000)
            events = poller.poll(milliseconds)
            if len(events) != 1:
                return False
            event_descriptor, mask = events[0]
            if event_descriptor != descriptor or not mask or mask & ~accepted:
                return False
            try:
                current = path.lstat()
            except FileNotFoundError:
                return True
            return (
                directory is not None
                and stat.S_ISDIR(current.st_mode)
                and object_identity(current) == object_identity(os.fstat(directory))
            )
        finally:
            os.close(descriptor)
    finally:
        if directory is not None:
            os.close(directory)


def collect(
    candidates: Collection[Path],
    uid: int,
    proc: Path = PROC,
    *,
    limits: Limits = DEFAULT_LIMITS,
    deadline: float | None = None,
) -> ProcessReferences:
    budget = Budget(
        limits=limits, deadline=min(time.monotonic() + limits.seconds, float("inf") if deadline is None else deadline)
    )
    result = ProcessReferences()
    selected = frozenset(candidates)
    with os.scandir(proc) as processes:
        for process in processes:
            budget.check()
            if process.name.isdigit():
                budget.process()
                path = Path(process.path)
                result.include(stable_process(path, selected, uid, budget, proc=proc))
    budget.check()
    return result


def stable_process(
    path: Path, candidates: Collection[Path], uid: int, budget: Budget, *, proc: Path = PROC
) -> ProcessReferences:
    for attempt in range(PROCESS_ATTEMPTS):
        try:
            return inspect_process(path, candidates, uid, budget, proc=proc)
        except (OSError, InspectionError) as error:
            if isinstance(error, InspectionError) and error.code != "incomplete":
                raise
            # procfs can report ESRCH, EACCES, or incomplete metadata during
            # exit, not just ENOENT. Only a whole-process kernel exit proof
            # permits skipping it. A live leader with remaining threads fails.
            if confirmed_exit(path):
                return ProcessReferences()
            if attempt + 1 == PROCESS_ATTEMPTS:
                raise
            budget.check()
    raise InspectionError


def receive(channel: socket.socket, deadline: float) -> bytes:
    payload = bytearray()
    while len(payload) <= MAX_MESSAGE_BYTES:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise InspectionDeadlineError
        channel.settimeout(remaining)
        piece = channel.recv(min(65536, MAX_MESSAGE_BYTES + 1 - len(payload)))
        if not piece:
            raise InspectionError
        payload.extend(piece)
        if b"\n" in piece:
            line, _, suffix = payload.partition(b"\n")
            if suffix or len(line) > MAX_MESSAGE_BYTES:
                raise InspectionError
            return bytes(line)
    raise InspectionError


def peer_uid(channel: socket.socket) -> int:
    if channel.family != socket.AF_UNIX or channel.getsockopt(socket.SOL_SOCKET, socket.SO_TYPE) != socket.SOCK_STREAM:
        raise InspectionError
    credentials: tuple[object, ...] = struct.unpack(
        "=iII", channel.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("=iII"))
    )
    pid, uid, _gid = credentials
    if type(pid) is not int or type(uid) is not int:
        raise InspectionError
    if pid <= 0 or uid < MIN_USER_UID:
        raise InspectionError
    return uid


def success(selected: Request, result: ProcessReferences, uid: int) -> bytes:
    response = {
        "schema": 1,
        "nonce": selected.nonce,
        "uid": uid,
        "protected": sorted(map(str, result.protected)),
        "package_writer": result.package_writer,
        "process_count": result.process_count,
        "zellij_running": result.zellij_running,
    }
    payload = json.dumps(response, ensure_ascii=False).encode() + b"\n"
    if len(payload) > MAX_MESSAGE_BYTES:
        raise InspectionError
    return payload


def inspect_request(
    channel: socket.socket, *, home_for_uid: Callable[[int], Path], proc: Path, limits: Limits, deadline: float
) -> bytes:
    uid = peer_uid(channel)
    home = home_for_uid(uid)
    selected = request(receive(channel, deadline), home)
    result = collect(selected.candidates, uid, proc, limits=limits, deadline=deadline - 0.25)
    return success(selected, result, uid)


def serve(
    channel: socket.socket,
    *,
    home_for_uid: Callable[[int], Path] = registered_home,
    proc: Path = PROC,
    limits: Limits = DEFAULT_LIMITS,
) -> None:
    deadline = time.monotonic() + REQUEST_SECONDS
    try:
        payload = inspect_request(channel, home_for_uid=home_for_uid, proc=proc, limits=limits, deadline=deadline)
    except Exception as error:  # ruff: ignore[blind-except] -- The privileged socket boundary returns no exception text or partial references.
        diagnostic = "inspection deadline exceeded" if isinstance(error, InspectionDeadlineError) else ERROR
        sys.stderr.write(f"cleanup probe: {diagnostic}\n")
        code = error.code if isinstance(error, InspectionError) else "incomplete"
        if type(code) is not str or code not in INSPECTION_CODES:
            code = "incomplete"
        payload = json.dumps({"error": ERROR, "code": code}).encode() + b"\n"
    channel.settimeout(max(0.01, deadline - time.monotonic()))
    channel.sendall(payload)


def expired(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise InspectionDeadlineError


def main() -> None:
    with socket.socket(fileno=0) as channel:
        if os.geteuid() != 0 or not sys.flags.isolated:
            channel.sendall(json.dumps({"error": ERROR}).encode() + b"\n")
            return
        signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, REQUEST_SECONDS)
        try:
            serve(channel)
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)


if __name__ == "__main__":
    main()
