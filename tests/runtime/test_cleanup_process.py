from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import select
import sys
import time
from typing import TYPE_CHECKING

from long_running_agent.process import PIPE, ProcessTimeout, spawn
from long_running_agent.runtime import cleanup, cleanup_activity, cleanup_inspection
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from long_running_agent.process import ChildProcess


ZOMBIE_LEADER = """import ctypes, os, sys, threading
held = open(sys.argv[1], 'rb')
def worker():
    os.write(1, b'ready\\n')
    sys.stdin.buffer.read()
    os._exit(0)
threading.Thread(target=worker).start()
libc = ctypes.CDLL(None)
libc.pthread_exit.argtypes = [ctypes.c_void_p]
libc.pthread_exit.restype = None
libc.pthread_exit(None)
"""


LATE_ZOMBIE_LEADER = """import ctypes, os, sys, threading
held = open(sys.argv[1], 'rb')
def worker():
    os.write(1, b'ready\\n')
    threading.Event().wait()
threading.Thread(target=worker).start()
sys.stdin.buffer.readline()
libc = ctypes.CDLL(None)
libc.pthread_exit.argtypes = [ctypes.c_void_p]
libc.pthread_exit.restype = None
libc.pthread_exit(None)
"""


def process_stat(*, state: bytes = b"S", start: bytes = b"100") -> bytes:
    return b"1 (fixture) " + state + b" " + b"0 " * 18 + start + b"\n"


def native_process(proc: Path, number: int, cwd: Path, command: bytes) -> Path:
    process = proc / str(number)
    process.mkdir()
    (process / "stat").write_bytes(process_stat())
    (process / "cwd").symlink_to(cwd, target_is_directory=True)
    (process / "cmdline").write_bytes(command)
    (process / "fd").mkdir()
    (process / "maps").write_bytes(b"")
    return process


def test_local_process_scan_enforces_an_aggregate_byte_limit(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    command = b"python\0" + b"x" * (cleanup.MAX_PROCESS_BYTES - 64)
    for number in range(9):
        native_process(proc, number, tmp_path, command)
    with pytest.raises(RuntimeError, match="byte limit"):
        cleanup.process_references(proc)


def test_local_process_scan_checks_deadline_before_reading_a_process(tmp_path: Path) -> None:
    budget = cleanup_activity.InventoryBudget(time.monotonic() - 1)
    with pytest.raises(RuntimeError, match="inspection deadline"):
        cleanup.process_reference(tmp_path / "missing-process", budget=budget)


def test_local_process_scan_shares_its_entry_budget_across_processes(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    native_process(proc, 1, tmp_path, b"python\0")
    budget = cleanup_activity.InventoryBudget(time.monotonic() + cleanup_activity.COLLECTION_SECONDS)
    budget.entries = cleanup_activity.MAX_ENTRIES
    with pytest.raises(RuntimeError, match="directory-entry limit"):
        cleanup.process_references(proc, budget=budget)


def test_bounded_local_process_scan_preserves_cwd_argument_and_fd_references(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    proc.mkdir()
    argument = tmp_path / "argument-reference"
    opened = tmp_path / "opened-reference"
    opened.write_text("retained\n")
    process = native_process(proc, 1, tmp_path, b"python\0" + bytes(argument) + b"\0")
    (process / "fd/3").symlink_to(opened)
    result = cleanup.process_references(proc)
    assert tmp_path in result.paths
    assert opened in result.paths
    assert str(argument) in result.arguments
    assert result.fd_count == 1


@pytest.mark.parametrize("mutation", ["start", "directory"], ids=["changed-starttime", "replaced-proc-directory"])
def test_local_process_identity_changes_after_maps_cannot_authorize_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    process = native_process(tmp_path, 1, tmp_path, b"python\0")
    original = cleanup.bounded_bytes

    def change_after_maps(path: Path, *, budget: cleanup_activity.InventoryBudget | None = None) -> bytes:
        payload = original(path, budget=budget)
        if path == process / "maps":
            match mutation:
                case "start":
                    (process / "stat").write_bytes(process_stat(start=b"101"))
                case "directory":
                    process.rename(tmp_path / "original-process")
                    native_process(tmp_path, 1, tmp_path, b"python\0")
                case _:
                    pytest.fail("Unknown native process identity mutation")
        return payload

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Change real proc-fixture files at the native read boundary to exercise identity verification without replacing its implementation.
        cleanup, "bounded_bytes", change_after_maps
    )
    with pytest.raises(RuntimeError, match="identity changed during inspection"):
        cleanup.process_reference(process)
    assert process.is_dir()


@pytest.mark.parametrize(
    "contents",
    [
        pytest.param(b"", id="empty-stat"),
        pytest.param(b"1 (fixture S\n", id="missing-command-delimiter"),
        pytest.param(b"1 (fixture) S\n", id="truncated-stat"),
        pytest.param(process_stat(state=b"?"), id="unknown-state"),
        pytest.param(process_stat(start=b"not-a-number"), id="unknown-starttime"),
        pytest.param(process_stat(start=b"-1"), id="negative-starttime"),
        pytest.param(process_stat(start=b"1" * 5000), id="oversized-numeric-starttime"),
    ],
)
def test_unknown_native_process_identity_is_never_a_complete_inventory(tmp_path: Path, contents: bytes) -> None:
    process = native_process(tmp_path, 1, tmp_path, b"python\0")
    (process / "stat").write_bytes(contents)
    with pytest.raises(RuntimeError, match="identity is incomplete or unknown"):
        cleanup.process_reference(process)


def test_local_process_inode_pin_refuses_a_symbolic_link(tmp_path: Path) -> None:
    process = native_process(tmp_path, 1, tmp_path, b"python\0")
    alias = tmp_path / "process-alias"
    alias.symlink_to(process, target_is_directory=True)
    with pytest.raises(OSError, match="process-alias") as error:
        cleanup.process_reference(alias)
    assert error.value.errno in {errno.ELOOP, errno.ENOTDIR}


@contextmanager
def native_child(script: str, opened: Path) -> Generator[ChildProcess]:
    child = spawn([sys.executable, "-c", script, str(opened)], stdin=PIPE, stdout=PIPE, stderr=PIPE)
    try:
        yield child
    finally:
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


def zombie_path(child: ChildProcess) -> Path:
    path = Path("/proc") / str(child.pid)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        contents = (path / "stat").read_bytes()
        if contents[contents.rfind(b")") + 2 :].split()[0] == b"Z":
            return path
        time.sleep(0.01)
    pytest.fail("Owned child did not reach the native zombie state")


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires Linux procfs and process pidfds")
def test_local_zombie_leader_with_live_worker_requires_authenticated_exit_inspection(tmp_path: Path) -> None:
    opened = tmp_path / "retained-worktree-file"
    opened.write_text("still held by a live worker\n")
    with native_child(ZOMBIE_LEADER, opened) as child:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 3)[0]
        assert os.read(child.stdout.fileno(), 6) == b"ready\n"
        path = zombie_path(child)
        descriptor = os.pidfd_open(child.pid, 0)
        try:
            poller = select.poll()
            poller.register(descriptor, select.POLLIN | select.POLLHUP)
            assert poller.poll(0) == []
            with pytest.raises(RuntimeError, match="requires whole-process exit inspection"):
                cleanup.process_reference(path)
        finally:
            os.close(descriptor)


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires Linux procfs")
def test_local_zombie_never_silently_bypasses_the_whole_process_exit_boundary(tmp_path: Path) -> None:
    with native_child("pass", tmp_path / "unused") as child:
        path = zombie_path(child)
        with pytest.raises(RuntimeError, match="requires whole-process exit inspection"):
            cleanup.process_reference(path)
        assert path.is_dir()


@pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires Linux procfs and process pidfds")
@pytest.mark.parametrize("fallback", [False, True], ids=["local-scan-rejects", "authenticated-fallback"])
def test_leader_becoming_zombie_after_maps_never_returns_unverified_local_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fallback: bool
) -> None:
    opened = tmp_path / "still-held-file"
    opened.write_text("live worker retains this file\n")
    with native_child(LATE_ZOMBIE_LEADER, opened) as child:
        assert child.stdout is not None
        assert select.select([child.stdout], [], [], 3)[0]
        assert os.read(child.stdout.fileno(), 6) == b"ready\n"
        process = Path("/proc") / str(child.pid)
        original = cleanup.bounded_bytes
        calls: list[tuple[Path, ...]] = []

        def transition_after_maps(path: Path, *, budget: cleanup_activity.InventoryBudget | None = None) -> bytes:
            payload = original(path, budget=budget)
            if path == process / "maps":
                assert child.stdin is not None
                child.stdin.write(b"exit\n")
                child.stdin.flush()
                assert zombie_path(child) == process
                descriptor = os.pidfd_open(child.pid, 0)
                try:
                    poller = select.poll()
                    poller.register(descriptor, select.POLLIN | select.POLLHUP)
                    assert poller.poll(0) == []
                finally:
                    os.close(descriptor)
            return payload

        def only_owned_process() -> cleanup.References:
            return cleanup.process_reference(process)

        def authenticated_probe(candidates: Sequence[Path]) -> cleanup_inspection.ProbeReply:
            calls.append(tuple(candidates))
            assert not candidates
            return cleanup_inspection.ProbeReply.model_validate(
                {
                    "schema": 1,
                    "nonce": "b" * 32,
                    "uid": os.getuid(),
                    "protected": [],
                    "package_writer": True,
                    "zellij_running": True,
                    "process_count": 1,
                }
            )

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Synchronize the owned child's actual pthread_exit immediately after its native maps read; identity and zombie checks remain real.
            cleanup, "bounded_bytes", transition_after_maps
        )
        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Restrict the procfs census boundary to this test's real child instead of reading unrelated host processes.
            cleanup, "process_references", only_owned_process
        )
        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Observe the privileged IPC boundary without contacting the host service; protocol authentication has separate native socket tests.
            cleanup_inspection, "inspect", authenticated_probe
        )
        if fallback:
            result = cleanup.inspected_processes(RuntimePaths(tmp_path / "unused-home"))
            assert result.package_writer
            assert "zellij" in result.arguments
            assert calls == [()]
        else:
            with pytest.raises(RuntimeError, match="requires whole-process exit inspection"):
                cleanup.process_reference(process)
            assert not calls
