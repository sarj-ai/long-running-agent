from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import selectors
import sys
import time
from typing import TYPE_CHECKING
from uuid import UUID

from long_running_agent.process import DEVNULL, PIPE, ProcessTimeout, spawn
from long_running_agent.runtime import cleanup_daemon, cleanup_probe, cleanup_state
from long_running_agent.runtime.settings import private_write
import pytest


if TYPE_CHECKING:
    from collections.abc import Generator

    from long_running_agent.process import ChildProcess


pytestmark = pytest.mark.skipif(not Path("/proc/self").exists(), reason="requires native Linux process references")


@pytest.mark.parametrize("storage", ["oversized", "hardlink", "public", "fifo"])
def test_failure_diagnostic_storage_cannot_read_untrusted_or_unbounded_native_objects(
    tmp_path: Path, storage: str
) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    selected = state / cleanup_daemon.FAILURE_FILE
    payload = cleanup_daemon.Failure(stage="worker-read", kind="deadline", at=1000).model_dump_json()
    match storage:
        case "oversized":
            private_write(selected, payload + " " * cleanup_state.PRIVATE_LIMIT)
        case "hardlink":
            other = state / "unrelated-fixture-record"
            private_write(other, payload)
            selected.hardlink_to(other)
        case "public":
            private_write(selected, payload)
            selected.chmod(0o644)
        case _:
            os.mkfifo(selected, 0o600)
    before = time.monotonic()
    assert cleanup_daemon.load_failure(state) is None
    assert time.monotonic() - before < 5
    assert selected.exists()
    if storage == "hardlink":
        assert selected.read_text() == payload
        assert selected.stat().st_nlink == 2


def test_optional_diagnostics_cannot_refill_or_rewrite_the_native_removal_budget(tmp_path: Path) -> None:
    stamp = cleanup_state.AttemptStamp(wall=1_000_000.0, uptime=1000.0, boot_id=UUID(int=1))
    for name in ("first", "second"):
        assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / name, "a" * 40)
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    schedule = cleanup_daemon.Schedule.new()
    schedule.context.stage = "worker-exit"
    schedule.failed(time.monotonic())
    cleanup_daemon.record_failure(
        tmp_path, schedule, cleanup_daemon.WorkerInspectionError("fixture", kind="exit-status")
    )
    assert cleanup_daemon.load_failure(tmp_path) == schedule.last_failure
    private_write(tmp_path / cleanup_daemon.FAILURE_FILE, "malformed fixture diagnostics")
    assert cleanup_daemon.load_failure(tmp_path) is None
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 0
    assert not cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / "third", "b" * 40)
    assert (ledger.read_bytes(), marker.read_bytes()) == before


@contextmanager
def holding_child(directory: Path, mode: str) -> Generator[ChildProcess]:
    script = (
        "import os,sys\n"
        "if sys.argv[2] == 'cwd':\n"
        " os.chdir(sys.argv[1])\n"
        "else:\n"
        " held=open(os.path.join(sys.argv[1],'held'),'rb')\n"
        "os.write(1,b'ready\\n')\n"
        "sys.stdin.buffer.read()\n"
    )
    child = spawn(
        [sys.executable, "-c", script, str(directory), mode],
        stdin=PIPE,
        stdout=PIPE,
        stderr=DEVNULL,
    )
    try:
        assert child.stdout is not None
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            assert selector.select(timeout=5), "The owned reference fixture did not become ready"
            assert os.read(child.stdout.fileno(), 6) == b"ready\n"
        yield child
    finally:
        try:
            if child.stdin is not None:
                child.stdin.close()
            try:
                child.wait(timeout=3)
            except ProcessTimeout:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except ProcessTimeout:
                    child.kill()
                    child.wait(timeout=2)
        finally:
            if child.stdout is not None:
                child.stdout.close()


@pytest.mark.parametrize("mode", ["cwd", "fd", "unlinked-fd"])
def test_quarantine_never_turns_a_live_fixture_reference_into_empty_protection(tmp_path: Path, mode: str) -> None:
    selected = tmp_path / "selected-fixture"
    selected.mkdir(mode=0o700)
    held = selected / "held"
    held.write_bytes(b"private reference fixture")
    original = selected.stat()
    quarantine = tmp_path / "quarantined-fixture"
    control = tmp_path / "unused-control"
    control.mkdir(mode=0o700)
    with holding_child(selected, mode) as child:
        if mode == "unlinked-fd":
            held.unlink()
        selected.rename(quarantine)
        assert (quarantine.stat().st_dev, quarantine.stat().st_ino) == (original.st_dev, original.st_ino)
        budget = cleanup_probe.Budget(limits=cleanup_probe.Limits(seconds=10), deadline=time.monotonic() + 10)
        found = cleanup_probe.inspect_process(
            Path("/proc") / str(child.pid), (quarantine, control), os.getuid(), budget
        )
        assert found.protected == {quarantine}
        assert found.process_count == 1
    assert not selected.exists()
    assert quarantine.is_dir()
    assert control.is_dir()
