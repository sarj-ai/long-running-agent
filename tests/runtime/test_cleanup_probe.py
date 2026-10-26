from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import pwd
import select
import socket
import sys
from threading import Thread
import time
from typing import TYPE_CHECKING, override

from long_running_agent.process import DEVNULL, PIPE, ChildProcess, ProcessTimeout, spawn
from long_running_agent.runtime import cleanup_probe as probe
import pytest


if TYPE_CHECKING:
    from collections.abc import Callable, Generator


pytestmark = pytest.mark.skipif(
    not Path("/proc/self").exists(), reason="requires Linux procfs and Unix peer credentials"
)
NONCE = "a" * 32
NATIVE_CHILD = """import ctypes, json, mmap, os, subprocess, sys, threading
paths = json.loads(sys.stdin.buffer.readline())
os.chdir(paths['cwd'])
held = open(paths['held'], 'rb')
mapped = open(paths['mapped'], 'rb')
mapping = mmap.mmap(mapped.fileno(), 0, access=mmap.ACCESS_READ)
if paths['deleted_cwd']:
    os.rmdir(paths['cwd'])
if paths['procfs_cwd']:
    helper = subprocess.Popen([sys.executable, '-c', 'import sys;sys.stdin.buffer.read()'], stdin=subprocess.PIPE)
    descriptor = os.open('/proc/' + str(helper.pid) + '/fdinfo', os.O_RDONLY | os.O_DIRECTORY)
    os.fchdir(descriptor)
    os.close(descriptor)
    helper.stdin.close()
    helper.wait(timeout=3)
if paths['nondumpable']:
    assert ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0) == 0
if paths['zombie_leader'] or paths['zombie_after_maps']:
    def worker():
        os.write(1, b'ready\\n' + str(threading.get_native_id()).encode() + b'\\n')
        if paths['zombie_after_maps']:
            threading.Event().wait()
        sys.stdin.buffer.read()
        os._exit(0)
    threading.Thread(target=worker).start()
    libc = ctypes.CDLL(None)
    libc.pthread_exit.argtypes = [ctypes.c_void_p]
    libc.pthread_exit.restype = None
    if paths['zombie_after_maps']:
        os.read(0, 1)
    libc.pthread_exit(None)
print('ready', flush=True)
sys.stdin.buffer.read()
"""


@dataclass(frozen=True)
class ProcessFixture:
    proc: Path
    home: Path
    process: Path
    candidates: tuple[Path, ...]


def stat_line(pid: int, start: int = 100) -> str:
    return f"{pid} (test process) " + " ".join(["S", "1", *(["0"] * 17), str(start)]) + "\n"


@pytest.fixture
def process(tmp_path: Path) -> ProcessFixture:
    home = tmp_path / "home"
    candidates = tuple(home / "code" / name for name in ("cwd", "argument", "opened", "mapped", "unused"))
    for candidate in candidates:
        candidate.mkdir(parents=True)
    proc = tmp_path / "proc"
    entry = proc / "123"
    descriptors = entry / "fd"
    descriptors.mkdir(parents=True)
    (entry / "cwd").symlink_to(candidates[0], target_is_directory=True)
    (entry / "stat").write_text(stat_line(123))
    (entry / "cmdline").write_bytes(b"python\0" + os.fsencode(candidates[1]) + b"\0npm install\0zellij\0")
    (descriptors / "3").symlink_to(candidates[2] / "held (deleted)")
    (entry / "maps").write_text(f"1000-2000 r--p 00000000 00:00 1 {candidates[3]}/mapped file\n")
    return ProcessFixture(proc, home, entry, candidates)


def new_budget(limits: probe.Limits = probe.DEFAULT_LIMITS) -> probe.Budget:
    return probe.Budget(limits=limits, deadline=time.monotonic() + limits.seconds)


def request_bytes(candidates: tuple[Path, ...], *, uid: int | None = None) -> bytes:
    payload: dict[str, object] = {"schema": 1, "nonce": NONCE, "candidates": list(map(str, candidates))}
    if uid is not None:
        payload["uid"] = uid
    return json.dumps(payload).encode()


def test_complete_process_inventory_protects_cwd_arguments_fds_and_maps_without_returning_them(
    process: ProcessFixture,
) -> None:
    found = probe.collect(process.candidates, os.getuid(), process.proc)
    assert found.protected == set(process.candidates[:4])
    assert found.package_writer
    assert found.zellij_running
    assert found.process_count == 1


def test_other_uid_is_skipped_before_reading_process_references(process: ProcessFixture) -> None:
    (process.process / "maps").unlink()
    found = probe.collect(process.candidates, os.getuid() + 1, process.proc)
    assert found == probe.ProcessReferences()


@pytest.mark.parametrize("missing", ["cwd", "cmdline", "maps", "stat", "fd"])
def test_missing_live_reference_never_yields_a_partial_result(process: ProcessFixture, missing: str) -> None:
    selected = process.process / missing
    if selected.is_dir() and not selected.is_symlink():
        (selected / "3").unlink()
        selected.rmdir()
    else:
        selected.unlink()
    with pytest.raises((OSError, probe.InspectionError), match=r"No such file|inspection incomplete"):
        probe.collect(process.candidates, os.getuid(), process.proc)


@pytest.mark.parametrize(
    ("limits", "code"),
    [
        pytest.param(probe.Limits(processes=0), "process-limit", id="process-limit"),
        pytest.param(probe.Limits(descriptors=0), "descriptor-limit", id="descriptor-limit"),
        pytest.param(probe.Limits(per_file_bytes=8), "metadata-too-large", id="per-file-limit"),
        pytest.param(probe.Limits(total_bytes=8), "byte-budget", id="aggregate-byte-limit"),
        pytest.param(probe.Limits(seconds=0), "deadline", id="deadline"),
    ],
)
def test_resource_limits_fail_closed(process: ProcessFixture, limits: probe.Limits, code: probe.InspectionCode) -> None:
    with pytest.raises(probe.InspectionError, match="inspection incomplete") as failure:
        probe.collect(process.candidates, os.getuid(), process.proc, limits=limits)
    assert failure.value.code == code


def test_reused_pid_start_time_is_rejected(process: ProcessFixture) -> None:
    class ReusedProcessBudget(probe.Budget):
        @override
        def read(self, directory: int, name: str) -> bytes:
            payload = super().read(directory, name)
            if name == "maps":
                (process.process / "stat").write_text(stat_line(123, 200))
            return payload

    budget = ReusedProcessBudget(limits=probe.Limits(), deadline=time.monotonic() + 2)
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.inspect_process(process.process, process.candidates, os.getuid(), budget, proc=process.proc)


def test_uid_change_after_initial_process_metadata_is_rejected(
    process: ProcessFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    native_identity = probe.identity

    def changed_identity(directory: int, budget: probe.Budget) -> probe.ProcessIdentity:
        observed = native_identity(directory, budget)
        return probe.ProcessIdentity(uid=observed.uid + 1, start=observed.start, state=observed.state)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Reproduce a kernel UID transition unavailable to an unprivileged native fixture.
        probe, "identity", changed_identity
    )
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.inspect_process(process.process, process.candidates, os.getuid(), new_budget(), proc=process.proc)


@pytest.mark.parametrize(
    "maps",
    [
        "ambiguous",
        "1000-2000 ???? 0 00:00 0",
        "1000-2000 r--p 0 bad-device 0",
        "1000-2000 r--p 0 00:00 1 /tmp/line\\012break",
    ],
)
def test_unknown_mapping_syntax_retains_every_candidate(process: ProcessFixture, maps: str) -> None:
    (process.process / "maps").write_text(maps + "\n")
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.collect(process.candidates, os.getuid(), process.proc)


def test_procfs_cwd_requires_kernel_device_proof_without_assuming_reader_namespace(process: ProcessFixture) -> None:
    native = Path(f"/proc/{os.getpid()}/fdinfo")
    assert probe.procfs_cwd(native, native.stat(), Path("/proc"))
    assert not probe.procfs_cwd(process.candidates[0], native.stat(), Path("/proc"))
    assert not probe.procfs_cwd(native, process.candidates[0].stat(), Path("/proc"))


def test_literal_and_resolved_paths_both_protect_candidates(process: ProcessFixture) -> None:
    alias = process.candidates[0] / "retargeted"
    alias.symlink_to(process.candidates[4], target_is_directory=True)
    result = probe.ProcessReferences()
    probe.protect(str(alias), frozenset(process.candidates), result)
    assert result.protected == {process.candidates[0], process.candidates[4]}


@pytest.mark.parametrize("retarget", [False, True], ids=["stable-alias", "alias-moves-after-snapshot"])
def test_duplicate_map_segments_normalize_one_snapshot_and_keep_literal_and_resolved_protection(
    process: ProcessFixture, monkeypatch: pytest.MonkeyPatch, retarget: bool
) -> None:
    alias = process.candidates[0] / "mapped-alias"
    alias.symlink_to(process.candidates[3], target_is_directory=True)
    mapped = alias / "library.so"
    (process.candidates[3] / "library.so").write_bytes(b"private mapped fixture")
    resolutions: list[Path] = []
    native_resolve = Path.resolve

    def counted_resolve(path: Path, strict: bool = False) -> Path:
        resolved = native_resolve(path, strict=strict)
        if path == mapped:
            resolutions.append(resolved)
            if retarget:
                alias.unlink()
                alias.symlink_to(process.candidates[4], target_is_directory=True)
        return resolved

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Count actual filesystem normalization and exercise a real alias retarget after the captured resolution.
        Path, "resolve", counted_resolve
    )
    payload = "".join(f"{index + 1:x}000-{index + 2:x}000 r--p 00000000 00:00 1 {mapped}\n" for index in range(32))
    result = probe.ProcessReferences()
    probe.mappings(payload.encode(), frozenset(process.candidates), new_budget(), result)
    assert resolutions == [process.candidates[3] / "library.so"]
    assert result.protected == {process.candidates[0], process.candidates[3]}


@pytest.mark.parametrize(
    "columns",
    [
        pytest.param("???? 00000000 00:00 1", id="invalid-permissions"),
        pytest.param("r--p 00000000 invalid 1", id="invalid-device"),
        pytest.param("r--p 00000000 00:00 invalid", id="invalid-inode"),
    ],
)
def test_duplicate_map_path_does_not_bypass_later_row_validation(process: ProcessFixture, columns: str) -> None:
    mapped = process.candidates[3] / "library.so"
    payload = f"1000-2000 r--p 00000000 00:00 1 {mapped}\n2000-3000 {columns} {mapped}\n"
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.mappings(payload.encode(), frozenset({process.candidates[3]}), new_budget(), probe.ProcessReferences())


def test_duplicate_map_path_does_not_bypass_its_row_deadline(process: ProcessFixture) -> None:
    checks: list[int] = []

    class ExpiringMapBudget(probe.Budget):
        @override
        def check(self) -> None:
            checks.append(1)
            if len(checks) == 2:
                self.deadline = 0
            super().check()

    mapped = process.candidates[3] / "library.so"
    payload = (f"1000-2000 r--p 00000000 00:00 1 {mapped}\n" * 2).encode()
    budget = ExpiringMapBudget(limits=probe.Limits(), deadline=time.monotonic() + 10)
    with pytest.raises(probe.InspectionDeadlineError, match="inspection incomplete"):
        probe.mappings(payload, frozenset({process.candidates[3]}), budget, probe.ProcessReferences())
    assert len(checks) == 2


def test_map_snapshot_dedup_never_survives_a_later_payload_with_same_inode_alias_targets(
    process: ProcessFixture,
) -> None:
    first = process.candidates[3] / "library.so"
    second = process.candidates[4] / "library.so"
    first.write_bytes(b"same inode with different protected ancestors")
    second.hardlink_to(first)
    alias = process.candidates[0] / "mapped-alias"
    alias.symlink_to(process.candidates[3], target_is_directory=True)
    payload = (f"1000-2000 r--p 00000000 00:00 1 {alias}/library.so\n" * 2).encode()
    original = probe.ProcessReferences()
    probe.mappings(payload, frozenset(process.candidates), new_budget(), original)
    alias.unlink()
    alias.symlink_to(process.candidates[4], target_is_directory=True)
    current = probe.ProcessReferences()
    probe.mappings(payload, frozenset(process.candidates), new_budget(), current)
    assert (first.stat().st_dev, first.stat().st_ino) == (second.stat().st_dev, second.stat().st_ino)
    assert original.protected == {process.candidates[0], process.candidates[3]}
    assert current.protected == {process.candidates[0], process.candidates[4]}


def test_raw_deleted_map_names_remain_distinct_snapshots(
    process: ProcessFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    mapped = process.candidates[3] / "library.so"
    names = [str(mapped), f"{mapped} (deleted)"]
    resolutions: list[str] = []
    native_resolve = Path.resolve

    def counted_resolve(path: Path, strict: bool = False) -> Path:
        if str(path) in names:
            resolutions.append(str(path))
        return native_resolve(path, strict=strict)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe actual path normalization; deleted and extant raw kernel names must not share a cached entry.
        Path, "resolve", counted_resolve
    )
    payload = "".join(f"1000-2000 r--p 00000000 00:00 1 {name}\n" for name in [*names, *names])
    result = probe.ProcessReferences()
    probe.mappings(payload.encode(), frozenset({process.candidates[3]}), new_budget(), result)
    assert resolutions.count(names[0]) == 2
    assert resolutions.count(names[1]) == 1
    assert result.protected == {process.candidates[3]}


def test_closing_fd_during_inventory_is_safe_when_process_identity_remains_stable(process: ProcessFixture) -> None:
    class ClosingDescriptorBudget(probe.Budget):
        @override
        def descriptor(self) -> None:
            super().descriptor()
            (process.process / "fd/3").unlink()

    budget = ClosingDescriptorBudget(limits=probe.Limits(), deadline=time.monotonic() + 2)
    result = probe.inspect_process(process.process, process.candidates, os.getuid(), budget, proc=process.proc)
    assert result.protected == set(process.candidates[:2]) | {process.candidates[3]}
    assert result.process_count == 1


def test_empty_candidates_still_report_package_writer_and_zellij(process: ProcessFixture) -> None:
    selected = probe.request(request_bytes(()), process.home)
    result = probe.collect(selected.candidates, os.getuid(), process.proc)
    assert not result.protected
    assert result.package_writer
    assert result.zellij_running


def test_missing_proc_path_is_not_an_exit_when_kernel_pid_is_still_alive(tmp_path: Path) -> None:
    assert not probe.confirmed_exit(tmp_path / str(os.getpid()))


def test_confirmed_native_exit_can_be_skipped() -> None:
    child = spawn([sys.executable, "-c", "pass"], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
    child.wait(timeout=3)
    path = Path("/proc") / str(child.pid)
    assert not path.exists()
    assert probe.confirmed_exit(path)


@pytest.mark.parametrize(
    "value",
    [
        "/etc",
        "/home/other/code/private",
        "/home/other/.ssh",
        "{home}/.ssh",
        "{home}/code",
        "/tmp",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only rejection value; never creates a file.
        "/var/tmp",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only rejection value; never creates a file.
        "/tmp-other/private",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only prefix rejection; no file creation.
        "/var/tmp-other/private",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only prefix rejection; no file creation.
        "{home}/code/../escape",
        "{home}/code/alias/../tree",
        "{home}/code/double//slash",
        "{home}/code/control\0path",
        "{home}/code/line\nbreak",
        "/tmp/../home/other/private",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only traversal rejection; no file creation.
        "/var/tmp/alias/../private",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only traversal rejection; no file creation.
        "/tmp/double//slash",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only normalization rejection; no file creation.
        "/var/tmp/control\0path",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only control-character rejection; no file creation.
        "/tmp/line\nbreak",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only control-character rejection; no file creation.
        "relative",
    ],
)
def test_candidate_scope_is_strictly_lexical_and_uses_only_explicit_inspection_roots(value: str) -> None:
    home = Path("/home/registered-inspection-user")
    selected = value.format(home=home)
    payload = json.dumps({"schema": 1, "nonce": NONCE, "candidates": [selected]}).encode()
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(payload, home)


@pytest.mark.parametrize(
    "selected",
    [
        pytest.param("/home/registered-inspection-user/code/closed", id="registered-code-child"),
        pytest.param(
            "/tmp/closed-pytest-allocation",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only accepted value; never creates a file.
            id="temporary-child",
        ),
        pytest.param(
            "/var/tmp/closed-pytest-allocation",  # ruff: ignore[hardcoded-temp-file] -- Protocol-only accepted value; never creates a file.
            id="durable-temporary-child",
        ),
    ],
)
def test_explicit_inspection_roots_accept_child_paths_without_resolving_or_creating_them(selected: str) -> None:
    home = Path("/home/registered-inspection-user")
    candidates = (Path(selected),)
    assert probe.request(request_bytes(candidates), home) == probe.Request(NONCE, candidates)


def test_probe_request_keeps_candidate_order_and_rejects_duplicates() -> None:
    home = Path("/home/registered-inspection-user")
    candidates = (home / "code/second", home / "code/first")
    assert probe.request(request_bytes(candidates), home).candidates == candidates
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(request_bytes((*candidates, candidates[0])), home)


def test_request_cannot_select_another_uid_or_exceed_candidate_bounds(process: ProcessFixture) -> None:
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(request_bytes(process.candidates, uid=os.getuid() + 1), process.home)
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(request_bytes((process.candidates[0],) * 513), process.home)
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(b"x" * (probe.MAX_MESSAGE_BYTES + 1), process.home)
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(b'{"schema":1,"schema":1,"nonce":"' + NONCE.encode() + b'","candidates":[]}', process.home)


@pytest.mark.parametrize("value", [None, [], True, {1: "private-invalid-field"}])
def test_probe_boundary_rejects_nonobjects_and_nontext_keys_without_echoing_values(value: object) -> None:
    with pytest.raises(probe.InspectionError, match="inspection incomplete") as caught:
        probe.fields(value)
    assert "private-invalid-field" not in str(caught.value)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "utf-32"])
def test_probe_decoder_preserves_native_json_encodings_and_duplicate_key_rejection(encoding: str) -> None:
    assert probe.decode('{"schema":1}'.encode(encoding)) == {"schema": 1}
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.decode('{"schema":1,"schema":1}'.encode(encoding))


@pytest.mark.parametrize("candidates", [None, {}, "path", True, [1], [None]])
def test_probe_request_rejects_invalid_candidate_types(candidates: object) -> None:
    payload = json.dumps({"schema": 1, "nonce": NONCE, "candidates": candidates}).encode()
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.request(payload, Path("/home/registered-inspection-user"))


def test_registration_must_match_native_account_and_root_controlled_scope(tmp_path: Path) -> None:
    account = pwd.struct_passwd(("sample", "x", 1400, 1400, "", "/home/sample", "/bin/sh"))
    value: dict[str, object] = {
        "schema_version": 1,
        "uid": 1400,
        "username": "sample",
        "home": "/home/sample",
        "browser": {},
    }
    assert probe.registration_home(value, 1400, account) == Path("/home/sample")
    for changed in ({"uid": 1401}, {"schema_version": True}, {"username": "other"}, {"home": str(tmp_path / "sample")}):
        with pytest.raises(probe.InspectionError, match="inspection incomplete"):
            probe.registration_home({**value, **changed}, 1400, account)
    with pytest.raises(probe.InspectionError, match="inspection incomplete"):
        probe.registered_home(os.getuid(), tmp_path)


def socket_exchange(
    process: ProcessFixture,
    payload: bytes,
    resolver: Callable[[int], Path] | None = None,
    *,
    limits: probe.Limits = probe.DEFAULT_LIMITS,
) -> bytes:
    observed: list[int] = []

    def home_for_uid(uid: int) -> Path:
        observed.append(uid)
        return process.home if resolver is None else resolver(uid)

    server, client = socket.socketpair()
    with server, client:
        worker = Thread(
            target=probe.serve,
            args=(server,),
            kwargs={"home_for_uid": home_for_uid, "proc": process.proc, "limits": limits},
        )
        worker.start()
        client.settimeout(3)
        try:
            client.sendall(payload)
            reply = bytearray()
            while not reply.endswith(b"\n"):
                reply.extend(client.recv(65536))
            return bytes(reply)
        finally:
            worker.join(timeout=3)
            assert not worker.is_alive()
            if os.getuid() >= probe.MIN_USER_UID:
                assert observed == [os.getuid()]


@pytest.mark.skipif(os.getuid() < probe.MIN_USER_UID, reason="requires an ordinary registered-user peer UID")
def test_real_socket_peer_uid_and_sanitized_response_contract(process: ProcessFixture) -> None:
    payload = socket_exchange(process, request_bytes(process.candidates) + b"\n")
    expected = {
        "schema": 1,
        "nonce": NONCE,
        "uid": os.getuid(),
        "protected": sorted(map(str, process.candidates[:4])),
        "package_writer": True,
        "process_count": 1,
        "zellij_running": True,
    }
    assert probe.decode(payload) == expected
    assert b"npm install" not in payload
    assert b"mapped file" not in payload


@pytest.mark.skipif(os.getuid() < probe.MIN_USER_UID, reason="requires an ordinary registered-user peer UID")
@pytest.mark.parametrize(
    "root",
    [
        pytest.param(
            Path("/tmp"),  # ruff: ignore[hardcoded-temp-file] -- Only names absent protocol candidates; never creates files here.
            id="missing-tmp-paths",
        ),
        pytest.param(
            Path("/var/tmp"),  # ruff: ignore[hardcoded-temp-file] -- Only names absent protocol candidates; never creates files here.
            id="missing-var-tmp-paths",
        ),
    ],
)
def test_authenticated_temporary_queries_protect_missing_former_paths_without_reading_other_uids(
    process: ProcessFixture, root: Path
) -> None:
    former = root / f"lra-probe-former-{os.getpid()}-{process.process.stat().st_ino}"
    assert not former.exists()
    candidates = tuple(former / name for name in ("argument", "opened", "mapped", "unused"))
    (process.process / "cmdline").write_bytes(b"python\0" + os.fsencode(candidates[0]) + b"\0")
    descriptor = process.process / "fd/3"
    descriptor.unlink()
    descriptor.symlink_to(candidates[1] / "held (deleted)")
    (process.process / "maps").write_text(f"1000-2000 r--p 00000000 00:00 1 {candidates[2]}/mapped file\n")
    payload = socket_exchange(process, request_bytes(candidates) + b"\n")
    assert probe.decode(payload) == {
        "schema": 1,
        "nonce": NONCE,
        "uid": os.getuid(),
        "protected": sorted(map(str, candidates[:3])),
        "package_writer": False,
        "process_count": 1,
        "zellij_running": False,
    }
    assert probe.collect(candidates, os.getuid() + 1, process.proc) == probe.ProcessReferences()
    assert socket_exchange(process, request_bytes(candidates, uid=os.getuid() + 1) + b"\n") == (
        b'{"error": "inspection incomplete", "code": "incomplete"}\n'
    )
    assert not former.exists()
    assert b"held (deleted)" not in payload
    assert b"mapped file" not in payload


def test_incomplete_native_data_and_framing_return_only_fixed_error(
    process: ProcessFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    (process.process / "maps").write_text("private unknown process data")
    assert (
        socket_exchange(process, request_bytes(process.candidates) + b"\n")
        == b'{"error": "inspection incomplete", "code": "incomplete"}\n'
    )
    assert (
        socket_exchange(process, request_bytes(process.candidates) + b"\nextra")
        == b'{"error": "inspection incomplete", "code": "incomplete"}\n'
    )
    assert capsys.readouterr().err == "cleanup probe: inspection incomplete\n" * 2


def test_inspection_deadline_returns_fixed_error_and_path_free_diagnostic(
    process: ProcessFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        socket_exchange(process, request_bytes(process.candidates) + b"\n", limits=probe.Limits(seconds=0))
        == b'{"error": "inspection incomplete", "code": "deadline"}\n'
    )
    assert capsys.readouterr().err == "cleanup probe: inspection deadline exceeded\n"


def test_native_socket_rejects_a_frame_above_two_mebibytes(process: ProcessFixture) -> None:
    payload = b"x" * (probe.MAX_MESSAGE_BYTES + 1) + b"\n"
    assert socket_exchange(process, payload) == b'{"error": "inspection incomplete", "code": "incomplete"}\n'


def test_connected_worker_main_does_not_run_privileged_collection_for_an_unprivileged_peer() -> None:
    server, client = socket.socketpair()
    with server, client:
        child = spawn(
            [sys.executable, "-I", str(Path(probe.__file__))], stdin=server.fileno(), stdout=DEVNULL, stderr=PIPE
        )
        try:
            client.settimeout(3)
            assert client.recv(4096) == b'{"error": "inspection incomplete"}\n'
            assert child.wait(timeout=3) == 0
        finally:
            stop_child(child)


def stop_child(child: ChildProcess) -> None:
    try:
        if child.stdin is not None:
            child.stdin.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=2)
            except ProcessTimeout:
                child.kill()
                child.wait(timeout=2)
    finally:
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def native_process(
    process: ProcessFixture,
    *,
    nondumpable: bool = False,
    procfs_cwd: bool = False,
    deleted_cwd: bool = False,
    zombie_leader: bool = False,
    zombie_after_maps: bool = False,
) -> Generator[ChildProcess]:
    opened = process.candidates[2] / "held"
    mapped = process.candidates[3] / "mapped"
    opened.write_bytes(b"private held bytes")
    mapped.write_bytes(b"private mapped bytes")
    child = spawn(
        [sys.executable, "-c", NATIVE_CHILD, str(process.candidates[1])], stdin=PIPE, stdout=PIPE, stderr=PIPE
    )
    try:
        assert child.stdin is not None
        assert child.stdout is not None
        payload = {
            "cwd": str(process.candidates[0]),
            "held": str(opened),
            "mapped": str(mapped),
            "nondumpable": nondumpable,
            "procfs_cwd": procfs_cwd,
            "deleted_cwd": deleted_cwd,
            "zombie_leader": zombie_leader,
            "zombie_after_maps": zombie_after_maps,
        }
        child.stdin.write(json.dumps(payload).encode() + b"\n")
        child.stdin.flush()
        assert select.select([child.stdout], [], [], 3)[0]
        assert os.read(child.stdout.fileno(), 6) == b"ready\n"
        yield child
    finally:
        stop_child(child)


def test_native_child_cwd_fd_map_and_argument_are_independently_preserved(process: ProcessFixture) -> None:
    with native_process(process) as child:
        found = probe.inspect_process(Path("/proc") / str(child.pid), process.candidates, os.getuid(), new_budget())
        assert found.protected == set(process.candidates[:4])
        assert found.process_count == 1


def test_native_detached_procfs_cwd_keeps_every_other_reference(process: ProcessFixture) -> None:
    with native_process(process, procfs_cwd=True) as child:
        native = Path("/proc") / str(child.pid)
        assert (native / "cwd").stat().st_dev == Path("/proc").stat().st_dev
        with pytest.raises(FileNotFoundError):
            (native / "cwd").resolve(strict=True)
        found = probe.inspect_process(native, process.candidates, os.getuid(), new_budget())
        assert found.protected == set(process.candidates[1:4])
        assert found.process_count == 1


def test_native_unlinked_cwd_keeps_former_path_and_every_other_reference(process: ProcessFixture) -> None:
    with native_process(process, deleted_cwd=True) as child:
        native = Path("/proc") / str(child.pid)
        assert (native / "cwd").stat().st_nlink == 0
        assert str((native / "cwd").readlink()).endswith(" (deleted)")
        found = probe.inspect_process(native, process.candidates, os.getuid(), new_budget())
        assert found.protected == set(process.candidates[:4])
        assert found.process_count == 1


@pytest.mark.skipif(os.geteuid() == 0, reason="root capabilities can inspect a nondumpable child")
def test_nondumpable_child_is_unknown_and_never_exempted_by_identity(process: ProcessFixture) -> None:
    with native_process(process, nondumpable=True) as child, pytest.raises(PermissionError, match="Permission denied"):
        probe.inspect_process(Path("/proc") / str(child.pid), process.candidates, os.getuid(), new_budget())


def wait_for_zombie(child: ChildProcess) -> Path:
    path = Path("/proc") / str(child.pid)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        contents = (path / "stat").read_bytes()
        if contents[contents.rfind(b")") + 2 :].split()[0] == b"Z":
            return path
        time.sleep(0.01)
    pytest.fail("Owned native child did not reach a zombie leader state")


@contextmanager
def unreaped_child() -> Generator[ChildProcess]:
    child = spawn([sys.executable, "-c", "pass"], stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL)
    try:
        wait_for_zombie(child)
        yield child
    finally:
        stop_child(child)


def test_unreaped_whole_process_exit_is_proven_while_its_proc_directory_still_exists() -> None:
    with unreaped_child() as child:
        path = Path("/proc") / str(child.pid)
        assert path.is_dir()
        descriptor = os.pidfd_open(child.pid, 0)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN | select.POLLHUP)
            assert poller.poll(0)
            assert probe.confirmed_exit(path)
            assert probe.inspect_process(path, (), os.getuid(), new_budget()) == probe.ProcessReferences()
            assert path.is_dir()
        finally:
            os.close(descriptor)


def test_zombie_main_thread_with_a_live_worker_is_never_treated_as_a_whole_process_exit(
    process: ProcessFixture,
) -> None:
    with native_process(process, zombie_leader=True) as child:
        path = wait_for_zombie(child)
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 3)[0]
        payload = os.read(child.stdout.fileno(), 32)
        assert payload.endswith(b"\n")
        assert payload[:-1].isdigit()
        worker = int(payload)
        descriptors = Path("/proc") / str(worker) / "fd"
        held = process.candidates[2] / "held"
        assert any(entry.readlink() == held for entry in descriptors.iterdir())
        descriptor = os.pidfd_open(child.pid, 0)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN | select.POLLHUP)
            assert poller.poll(0) == []
            assert not probe.confirmed_exit(path)
            with pytest.raises(probe.InspectionError, match="inspection incomplete"):
                probe.inspect_process(path, process.candidates, os.getuid(), new_budget())
        finally:
            os.close(descriptor)


def test_process_exiting_during_map_inspection_can_finish_only_after_whole_group_exit(
    process: ProcessFixture,
) -> None:
    with native_process(process) as child:
        path = Path("/proc") / str(child.pid)

        class ExitingProcessBudget(probe.Budget):
            @override
            def read(self, directory: int, name: str) -> bytes:
                payload = super().read(directory, name)
                if name == "maps":
                    assert child.stdin is not None
                    child.stdin.close()
                    wait_for_zombie(child)
                return payload

        budget = ExitingProcessBudget(limits=probe.DEFAULT_LIMITS, deadline=time.monotonic() + 3)
        found = probe.inspect_process(path, process.candidates, os.getuid(), budget)
        assert found.protected == set(process.candidates[:4])
        assert path.is_dir()
        assert probe.confirmed_exit(path)


def test_leader_exiting_during_map_inspection_cannot_hide_a_live_worker(process: ProcessFixture) -> None:
    with native_process(process, zombie_after_maps=True) as child:
        path = Path("/proc") / str(child.pid)

        class ExitingLeaderBudget(probe.Budget):
            @override
            def read(self, directory: int, name: str) -> bytes:
                payload = super().read(directory, name)
                if name == "maps":
                    assert child.stdin is not None
                    child.stdin.write(b"x")
                    child.stdin.flush()
                    wait_for_zombie(child)
                return payload

        budget = ExitingLeaderBudget(limits=probe.DEFAULT_LIMITS, deadline=time.monotonic() + 3)
        with pytest.raises(probe.InspectionError, match="inspection incomplete"):
            probe.inspect_process(path, process.candidates, os.getuid(), budget)
        assert not probe.confirmed_exit(path)


def test_a_replaced_proc_directory_cannot_be_bound_to_an_old_exited_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with unreaped_child() as child:
        path = tmp_path / str(child.pid)
        path.mkdir()
        original = path.stat().st_ino
        pidfd_open = os.pidfd_open

        def replace_directory(pid: int, flags: int = 0) -> int:
            descriptor = pidfd_open(pid, flags)
            try:
                path.rename(tmp_path / "previous-process")
                path.mkdir()
            except BaseException:
                os.close(descriptor)
                raise
            return descriptor

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace a real directory inode at native pidfd acquisition to model otherwise nondeterministic kernel PID reuse.
            os, "pidfd_open", replace_directory
        )
        assert not probe.confirmed_exit(path)
        assert path.stat().st_ino != original


def test_esrch_never_proves_exit_while_a_proc_path_still_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / str(os.getpid())
    path.mkdir()

    def missing_pid(_pid: int, _flags: int = 0) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model native ESRCH between proc-directory acquisition and pidfd lookup while the real snapshot path remains present.
        os, "pidfd_open", missing_pid
    )
    assert not probe.confirmed_exit(path)


@pytest.mark.parametrize("event", [select.POLLERR, select.POLLNVAL], ids=["kernel-poll-error", "invalid-pidfd"])
def test_pidfd_poll_errors_never_authorize_an_exit(monkeypatch: pytest.MonkeyPatch, event: int) -> None:
    with unreaped_child() as child:

        class UncertainPoll:
            descriptor: int | None = None

            def register(self, descriptor: int, _events: int) -> None:
                self.descriptor = descriptor

            def poll(
                self, _timeout: int
            ) -> list[
                tuple[int, int]
            ]:  # sarj-noqa: SARJ026 -- Mirror the native select.poll descriptor/eventmask tuple protocol at the kernel boundary.
                assert self.descriptor is not None
                return [(self.descriptor, event)]

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- A native pidfd remains real; inject only kernel poll error flags that cannot be scheduled deterministically.
            select, "poll", UncertainPoll
        )
        assert not probe.confirmed_exit(Path("/proc") / str(child.pid))


def complete_maps_payload(size: int, path: Path) -> bytes:
    line = f"1000-2000 r--p 00000000 00:00 1 {path}\n".encode()
    copies = size // len(line) - 1
    assert copies >= 0
    rest = size - copies * len(line)
    anonymous = b"1000-2000 r--p 0 00:00 0"
    payload = line * copies + anonymous + b" " * (rest - len(anonymous) - 1) + b"\n"
    assert len(payload) == size
    return payload


def read_fixture_metadata(process: ProcessFixture, name: str, budget: probe.Budget) -> bytes:
    directory = os.open(process.process, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        return budget.read(directory, name)
    finally:
        os.close(directory)


def test_maps_above_legacy_limit_preserve_every_reference_and_global_flag(process: ProcessFixture) -> None:
    payload = complete_maps_payload(1024 * 1024 + 4096, process.candidates[3] / "held")
    (process.process / "maps").write_bytes(payload)
    found = probe.collect(process.candidates, os.getuid(), process.proc)
    assert found.protected == set(process.candidates[:4])
    assert found.package_writer
    assert found.zellij_running
    assert found.process_count == 1


def test_maps_exact_eight_mebibytes_remain_complete(process: ProcessFixture) -> None:
    payload = complete_maps_payload(probe.MAP_BYTES, process.candidates[3] / "held")
    (process.process / "maps").write_bytes(payload)
    budget = new_budget()
    captured = read_fixture_metadata(process, "maps", budget)
    assert captured == payload
    assert budget.bytes_read == probe.MAP_BYTES
    result = probe.ProcessReferences()
    probe.mappings(captured, frozenset(process.candidates), budget, result)
    assert result.protected == {process.candidates[3]}


def test_maps_above_new_limit_fail_with_code_and_fresh_recovery(process: ProcessFixture) -> None:
    (process.process / "maps").write_bytes(b"x" * (probe.MAP_BYTES + 1))
    with pytest.raises(probe.InspectionError) as failure:
        probe.collect(process.candidates, os.getuid(), process.proc)
    assert failure.value.code == "maps-too-large"
    assert str(failure.value) == probe.ERROR
    (process.process / "maps").write_bytes(complete_maps_payload(1024 * 1024 + 64, process.candidates[3] / "held"))
    assert probe.collect(process.candidates, os.getuid(), process.proc).protected == set(process.candidates[:4])


def test_large_maps_still_share_the_aggregate_budget_and_recover(process: ProcessFixture) -> None:
    payload = complete_maps_payload(1024 * 1024 + 64, process.candidates[3] / "held")
    (process.process / "maps").write_bytes(payload)
    limits = probe.Limits(total_bytes=len(payload) * 2 - 1)
    budget = new_budget(limits)
    assert read_fixture_metadata(process, "maps", budget) == payload
    with pytest.raises(probe.InspectionError) as failure:
        read_fixture_metadata(process, "maps", budget)
    assert failure.value.code == "byte-budget"
    assert budget.bytes_read == limits.total_bytes + 1
    assert probe.DEFAULT_LIMITS.total_bytes == 32 * 1024 * 1024
    assert probe.collect(process.candidates, os.getuid(), process.proc).process_count == 1


def test_stat_keeps_its_legacy_limit_and_recovers_with_native_identity(process: ProcessFixture) -> None:
    (process.process / "stat").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(probe.InspectionError) as failure:
        read_fixture_metadata(process, "stat", new_budget())
    assert failure.value.code == "metadata-too-large"
    (process.process / "stat").write_text(stat_line(123))
    assert probe.collect(process.candidates, os.getuid(), process.proc).protected == set(process.candidates[:4])


def test_cmdline_keeps_legacy_limit_then_recovers_global_flags(process: ProcessFixture) -> None:
    original = (process.process / "cmdline").read_bytes()
    (process.process / "cmdline").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(probe.InspectionError) as failure:
        probe.collect(process.candidates, os.getuid(), process.proc)
    assert failure.value.code == "metadata-too-large"
    (process.process / "cmdline").write_bytes(original)
    found = probe.collect(process.candidates, os.getuid(), process.proc)
    assert found.package_writer
    assert found.zellij_running
    assert found.protected == set(process.candidates[:4])


def test_maps_field_limit_is_independent_of_stat_limit_and_fresh_each_request(process: ProcessFixture) -> None:
    payload = complete_maps_payload(512, process.candidates[3] / "held")
    (process.process / "maps").write_bytes(payload + b"x")
    limits = probe.Limits(maps_bytes=512)
    with pytest.raises(probe.InspectionError) as failure:
        probe.collect(process.candidates, os.getuid(), process.proc, limits=limits)
    assert failure.value.code == "maps-too-large"
    (process.process / "maps").write_bytes(payload)
    assert probe.collect(process.candidates, os.getuid(), process.proc, limits=limits).process_count == 1
    assert limits.per_file_bytes == 1024 * 1024


def test_large_maps_never_bypass_deadline_and_later_fresh_budget_can_succeed(process: ProcessFixture) -> None:
    (process.process / "maps").write_bytes(complete_maps_payload(1024 * 1024 + 64, process.candidates[3] / "held"))
    expired = probe.Budget(probe.DEFAULT_LIMITS, time.monotonic() - 1)
    with pytest.raises(probe.InspectionDeadlineError) as failure:
        read_fixture_metadata(process, "maps", expired)
    assert failure.value.code == "deadline"
    assert expired.bytes_read == 0
    assert probe.collect(process.candidates, os.getuid(), process.proc).protected == set(process.candidates[:4])


def test_invalid_row_after_large_duplicate_map_prefix_cannot_be_ignored(process: ProcessFixture) -> None:
    payload = complete_maps_payload(1024 * 1024 + 64, process.candidates[3] / "held")
    (process.process / "maps").write_bytes(payload + b"invalid private map row\n")
    with pytest.raises(probe.InspectionError) as failure:
        probe.collect(process.candidates, os.getuid(), process.proc)
    assert failure.value.code == "incomplete"
    assert "private" not in str(failure.value)
    (process.process / "maps").write_bytes(payload)
    assert probe.collect(process.candidates, os.getuid(), process.proc).protected == set(process.candidates[:4])


def test_identity_change_after_large_map_read_retains_then_stable_retry_recovers(process: ProcessFixture) -> None:
    (process.process / "maps").write_bytes(complete_maps_payload(1024 * 1024 + 64, process.candidates[3] / "held"))

    class ChangingBudget(probe.Budget):
        @override
        def read(self, directory: int, name: str) -> bytes:
            payload = super().read(directory, name)
            if name == "maps":
                (process.process / "stat").write_text(stat_line(123, 200))
            return payload

    budget = ChangingBudget(probe.DEFAULT_LIMITS, time.monotonic() + 60)
    with pytest.raises(probe.InspectionError) as failure:
        probe.inspect_process(process.process, process.candidates, os.getuid(), budget, proc=process.proc)
    assert failure.value.code == "incomplete"
    assert probe.collect(process.candidates, os.getuid(), process.proc).protected == set(process.candidates[:4])


def test_failure_boundary_clamps_mutated_error_code_then_fresh_request_recovers(process: ProcessFixture) -> None:
    private = "private-error-code-never-emitted"
    error = probe.InspectionError()
    vars(error)["code"] = private

    def unavailable_home(_uid: int) -> Path:
        raise error

    payload = socket_exchange(process, request_bytes(process.candidates) + b"\n", resolver=unavailable_home)
    assert probe.decode(payload) == {"error": probe.ERROR, "code": "incomplete"}
    assert private.encode() not in payload
    recovered = probe.decode(socket_exchange(process, request_bytes(process.candidates) + b"\n"))
    assert recovered["protected"] == sorted(map(str, process.candidates[:4]))
    assert "error" not in recovered
