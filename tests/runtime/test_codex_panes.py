from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
from functools import partial
import json
import os
from pathlib import Path
import signal
import sqlite3
import stat
import subprocess
import sys
from typing import TYPE_CHECKING

from long_running_agent.process import DEVNULL, PIPE, run as native_run, spawn
from long_running_agent.runtime import codex_panes as panes
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence

    from long_running_agent.process import Completed

ROOT_ID = "11111111-1111-1111-1111-111111111111"
OLD_ID = "22222222-2222-2222-2222-222222222222"
CHILD_ID = "33333333-3333-3333-3333-333333333333"
PROMPT = "private startup prompt must never be replayed or checkpointed"


@dataclass(frozen=True)
class ProcessFixture:
    proc: Path
    process: Path


def fixture_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, source: str = "cli") -> ProcessFixture:
    proc = tmp_path / "proc"
    process = proc / "100"
    (process / "fd").mkdir(parents=True)
    (process / "fdinfo").mkdir()
    executable = tmp_path / "bin/codex"
    executable.parent.mkdir()
    executable.touch()
    (process / "exe").symlink_to(executable)
    (process / "cwd").symlink_to(tmp_path)
    (process / "fd/0").symlink_to("/dev/pts/2")
    fields = ["S", "1", "100", "100", "34817", "100", *(["0"] * 13), "1234"]
    (process / "stat").write_text("100 (codex) " + " ".join(fields))
    (process / "cmdline").write_bytes(
        f'codex\0--approve-for-me\0-c\0model_reasoning_effort="high"\0resume\0{OLD_ID}\0{PROMPT}\0'.encode()
    )
    home = tmp_path / ".codex"
    home.mkdir(mode=0o700)
    (process / "environ").write_bytes(
        f"HOME={tmp_path}\0CODEX_HOME={home}\0ZELLIJ_SESSION_NAME=fixture\0ZELLIJ_PANE_ID=3\0".encode()
    )
    for number, identifier in enumerate((ROOT_ID, CHILD_ID), start=4):
        target = home / "sessions" / f"rollout-now-{identifier}.jsonl"
        (process / "fd" / str(number)).symlink_to(target)
        (process / "fdinfo" / str(number)).write_text("flags:\t0100001\n")
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Bootstrap Codex's external checkpoint index; this reader has no writer API.
            "CREATE TABLE threads(id TEXT PRIMARY KEY, source TEXT)"
        )
        connection.executemany(  # sarj-noqa: SARJ429 -- Seed native root/subagent metadata without opening the user's real Codex database.
            "INSERT INTO threads VALUES (?,?)", [(ROOT_ID, source), (CHILD_ID, '{"subagent":{"thread_spawn":{}}}')]
        )
        connection.commit()

    zellij = executable.with_name("zellij")
    zellij.write_text(
        f"#!{sys.executable}\n"
        "import pathlib,sys\n"
        "if sys.argv[1:]==['list-sessions','--no-formatting']:\n"
        " print('fixture [Created 1s ago]')\n"
        "elif sys.argv[1:]==['--session','fixture','action','list-panes','--json']:\n"
        " print(pathlib.Path(__file__).with_name('zellij-panes.json').read_text())\n"
        "else: sys.exit(1)\n"
    )
    zellij.chmod(0o700)
    (executable.parent / "zellij-panes.json").write_text(
        json.dumps([{"id": 3, "tab_id": 1, "is_plugin": False, "exited": False}])
    )
    monkeypatch.setenv("PATH", str(executable.parent))
    return ProcessFixture(proc, process)


def fixture_replacement(process: Path, marker: str) -> Path:
    replacement = process.with_name("101")
    process.rename(replacement)
    fields = (replacement / "stat").read_text().rsplit(")", 1)[1].split()
    fields[2], fields[5], fields[19] = "101", "101", "new-birth"
    (replacement / "stat").write_text("101 (codex) " + " ".join(fields))
    environment = replacement / "environ"
    environment.write_bytes(
        environment.read_bytes().replace(b"ZELLIJ_PANE_ID=3\0", b"ZELLIJ_PANE_ID=99\0")
        + f"LRA_CODEX_RESTART_ID={marker}\0".encode()
    )

    (process.parent.parent / "bin/zellij-panes.json").write_text(
        json.dumps([{"id": 99, "tab_id": 1, "is_plugin": False, "exited": False}])
    )
    return replacement


@pytest.mark.parametrize("source", ["cli", "exec"])
def test_owned_foreground_tui_uses_current_writable_root_not_old_resume_argument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    proc = fixture_process(tmp_path, monkeypatch, source=source).proc
    plan = panes.plan_panes(proc=proc)
    target = plan.targets[0]
    assert target.thread_id == ROOT_ID
    assert target.recovery[-2:] == ("resume", ROOT_ID)
    assert "--approve-for-me" not in target.recovery
    assert 'model_reasoning_effort="high"' not in target.recovery
    assert OLD_ID not in target.recovery
    assert PROMPT not in target.recovery
    assert target.recovery[0] == str(tmp_path / "bin/codex")
    assert ROOT_ID not in repr(plan)
    assert PROMPT not in repr(target)
    assert not plan.checkpoint.exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ("codex-linux-sandbox", "--", "codex"),
        ("codex", "app-server"),
        ("codex", "--no-daemon", "exec", "work"),
        ("codex", "--codex-run-as-fs-helper"),
    ],
    ids=("sandbox-alias", "app-server", "headless-exec", "filesystem-helper"),
)
def test_non_tui_same_executable_aliases_are_excluded_before_checkpoint_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arguments: tuple[str, ...]
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    (fixture.process / "cmdline").write_bytes("\0".join((*arguments, "")).encode())
    (fixture.process / "fd/4").unlink()
    with pytest.raises(panes.PaneError, match="uniquely mapped"):
        panes.plan_panes(proc=fixture.proc)


def test_nonforeground_and_no_terminal_processes_are_excluded_before_executable_access(tmp_path: Path) -> None:
    unavailable = tmp_path / "unavailable-process"
    assert panes.candidate_metadata(panes.Process(100, "birth", 1, 100, -1), unavailable) is None
    assert panes.candidate_metadata(panes.Process(100, "birth", 1, 100, 200), unavailable) is None


def test_upgraded_running_native_tui_is_refused_instead_of_silently_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    executable = fixture.process / "exe"
    executable.unlink()
    executable.symlink_to(tmp_path / "bin/codex (deleted)")
    with pytest.raises(panes.PaneError, match="executable changed"):
        panes.plan_panes(proc=fixture.proc)


@pytest.mark.parametrize(
    "extra",
    [
        ("--remote", "https://private.invalid"),
        ("--profile", "custom"),
        ("-c", "api_key=private-secret"),
        ("-c", "unknown/path=private-secret"),
    ],
    ids=("remote-server", "named-profile", "credential-config", "unknown-config"),
)
def test_unknown_remote_and_secret_bearing_launch_modes_fail_before_auth(extra: tuple[str, ...]) -> None:
    with pytest.raises(panes.PaneError) as error:
        panes.recovery_arguments(("/native/codex", *extra), ROOT_ID)
    assert "private" not in str(error.value)


def test_duplicate_no_daemon_is_not_generated_and_prompt_is_discarded() -> None:
    assert panes.recovery_arguments(("/native/codex", "--no-daemon", "--", PROMPT), ROOT_ID) == (
        "/native/codex",
        "--no-daemon",
        "resume",
        ROOT_ID,
    )


def test_interactive_fork_reopens_its_persisted_root_without_forking_again() -> None:
    arguments = ("/native/codex", "fork", OLD_ID, PROMPT)
    assert panes.native_tui(arguments)
    assert panes.recovery_arguments(arguments, ROOT_ID) == ("/native/codex", "--no-daemon", "resume", ROOT_ID)


def test_external_executor_mode_is_rejected_before_account_switch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    environment = fixture.process / "environ"
    environment.write_bytes(environment.read_bytes() + b"CODEX_EXEC_SERVER_URL=http://private.invalid\0")
    with pytest.raises(panes.PaneError, match="external-executor") as error:
        panes.plan_panes(proc=fixture.proc)
    assert "private.invalid" not in str(error.value)


def test_native_resume_restores_current_settings_instead_of_reapplying_old_launch_policy() -> None:
    original = (
        "/native/codex",
        "--approve-for-me",
        "--model",
        "old-model",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "danger-full-access",
        "--cd",
        "/old/workspace",
        "--add-dir",
        "/old/extra",
        "-c",
        'approvals_reviewer="user"',
        "-c",
        'agents.default_subagent_model="subagent-model"',
        "--enable",
        "subagents",
        "resume",
        OLD_ID,
        "help",
    )
    assert panes.recovery_arguments(original, ROOT_ID) == (
        "/native/codex",
        "--no-daemon",
        "-c",
        'agents.default_subagent_model="subagent-model"',
        "--enable",
        "subagents",
        "resume",
        ROOT_ID,
    )


@pytest.mark.parametrize("trust", ['"trusted"', '"untrusted"'])
def test_known_native_project_trust_override_is_preserved_exactly(trust: str) -> None:
    override = f'projects."/private/work tree".trust_level={trust}'
    recovered = panes.recovery_arguments(("/native/codex", "-c", override), ROOT_ID)
    assert recovered == ("/native/codex", "--no-daemon", "-c", override, "resume", ROOT_ID)


def test_invalid_project_trust_value_is_not_written_to_recovery() -> None:
    with pytest.raises(panes.PaneError, match="manual restart") as error:
        panes.recovery_arguments(
            ("/native/codex", "-c", 'projects."/private/work tree".trust_level="private-secret"'), ROOT_ID
        )
    assert "private" not in str(error.value)


def test_multiple_writable_roots_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    with closing(sqlite3.connect(tmp_path / ".codex/state_5.sqlite")) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Corrupt only the external-schema fixture to prove ambiguous native roots fail closed.
            "UPDATE threads SET source='cli'"
        )
        connection.commit()
    with pytest.raises(panes.PaneError, match="ambiguous"):
        panes.plan_panes(proc=proc)


def test_reserved_writer_lock_is_not_a_persisted_checkpoint_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    reserved = tmp_path / ".codex/thread-writer-locks" / f"{OLD_ID}.lock"
    (fixture.process / "fd/6").symlink_to(reserved)
    (fixture.process / "fdinfo/6").write_text("flags:\t0100002\n")
    assert panes.plan_panes(proc=fixture.proc).targets[0].thread_id == ROOT_ID
    (fixture.process / "fd/4").unlink()
    (fixture.process / "fd/5").unlink()
    with pytest.raises(panes.PaneError, match="no identifiable writable checkpoint"):
        panes.plan_panes(proc=fixture.proc)


def test_other_private_home_rollout_is_not_a_checkpoint_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    unrelated = tmp_path / "other-home/sessions" / f"rollout-now-{OLD_ID}.jsonl"
    (fixture.process / "fd/6").symlink_to(unrelated)
    (fixture.process / "fdinfo/6").write_text("flags:\t0100001\n")
    assert panes.plan_panes(proc=fixture.proc).targets[0].thread_id == ROOT_ID


def test_all_targets_are_revalidated_before_receipt_or_first_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    plan = panes.plan_panes(proc=fixture.proc)
    (fixture.process / "cmdline").write_bytes(b"codex\0--search\0")

    def unexpected(_target: panes.PaneTarget, *, proc: Path, timeout: float) -> None:
        pytest.fail(f"must not signal a changed plan (proc={proc}, timeout={timeout})")

    with pytest.raises(panes.PaneError, match="changed during sign-in"):
        panes.restart_panes(plan, proc=fixture.proc, replacement=partial(panes.replace_target, stop=unexpected))
    assert not plan.checkpoint.exists()


def test_durable_private_recovery_precedes_stop_and_only_native_targeted_replacement_is_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    plan = panes.plan_panes(proc=fixture.proc)
    calls: list[list[str]] = []

    def stop(_target: panes.PaneTarget, *, proc: Path, timeout: float) -> None:
        assert proc == fixture.proc
        assert timeout > 0
        assert f'"{ROOT_ID}"]' in plan.checkpoint.read_text()
        assert PROMPT not in plan.checkpoint.read_text()
        assert stat.S_IMODE(plan.checkpoint.stat().st_mode) == 0o600
        calls.append(["stop"])

    def run(arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        calls.append(list(arguments))
        return native_run(
            [sys.executable, "-c", "print('terminal_99')"], capture_output=capture_output, timeout=timeout
        )

    readiness: list[panes.PaneId | None] = []

    def ready(
        _target: panes.PaneTarget, pane_id: panes.PaneId | None, *, restart_id: str, proc: Path, timeout: float
    ) -> None:
        assert restart_id
        assert proc == fixture.proc
        assert timeout > 0
        readiness.append(pane_id)

    report = panes.restart_panes(
        plan,
        proc=fixture.proc,
        replacement=partial(panes.replace_target, stop=stop, runner=run, ready=ready),
    )
    assert report.reopened == 1
    assert report.failed == 0
    assert calls[0] == ["stop"]
    assert calls[1][:6] == ["zellij", "--session", "fixture", "action", "new-pane", "--in-place"]
    assert calls[1][calls[1].index("--pane-id") + 1] == "terminal_3"
    assert "--close-replaced-pane" in calls[1]
    assert "--no-focus" in calls[1]
    assert PROMPT not in calls[1]
    assert report.outcomes[0].state == "reopened"
    assert readiness == [99]


def test_stop_failure_preserves_recovery_and_never_starts_a_second_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    plan = panes.plan_panes(proc=fixture.proc)

    def stop(_target: panes.PaneTarget, *, proc: Path, timeout: float) -> None:
        assert proc == fixture.proc
        assert timeout > 0
        message = "Old Codex processes did not exit; no replacement was started"
        raise panes.PaneError(message)

    def unexpected(arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        pytest.fail(f"must not create a competing writer ({arguments}, capture={capture_output}, timeout={timeout})")

    report = panes.restart_panes(
        plan, proc=fixture.proc, replacement=partial(panes.replace_target, stop=stop, runner=unexpected)
    )
    assert report.failed == 1
    assert report.reopened == 0
    assert plan.checkpoint.exists()


def test_pid_birth_change_is_rejected_without_sending_a_signal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    target = panes.plan_panes(proc=proc).targets[0]
    target = replace(target, process=replace(target.process, birth="stale"))

    def unexpected(_descriptor: int, _number: int) -> None:
        pytest.fail("stale PID must not receive a signal")

    with pytest.raises(panes.PaneError, match="changed"):
        panes.stop_target(target, proc=proc, timeout=1, access=panes.ProcessAccess(send=unexpected))


def test_foreign_user_descendant_and_unrelated_foreground_group_member_refuse_before_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    target = panes.plan_panes(proc=proc).targets[0]
    child = panes.Process(101, "child-birth", 100, 101, 101, uid=target.process.uid + 1)
    with pytest.raises(panes.PaneError, match="another user"):
        panes.target_members(target, {100: target.process, 101: child})
    unrelated = replace(child, parent=1, group=100, uid=target.process.uid)
    with pytest.raises(panes.PaneError, match="unrelated"):
        panes.target_members(target, {100: target.process, 101: unrelated})


def test_controller_descendant_is_refused_during_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    controller = fixture.proc / str(os.getpid())
    controller.mkdir()
    fields = ["S", "100", "200", "200", "34817", "200", *(["0"] * 13), "controller-birth"]
    (controller / "stat").write_text(f"{os.getpid()} (controller) " + " ".join(fields))
    with pytest.raises(panes.PaneError, match="controller"):
        panes.plan_panes(proc=fixture.proc)


def test_owned_freeze_tolerates_shell_foreground_reclaim_but_not_identity_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    target = panes.plan_panes(proc=proc).targets[0]
    observed = replace(target.process, foreground=999, state="T")
    with pytest.raises(panes.PaneError, match="changed"):
        panes.target_members(target, {100: observed})
    assert panes.target_members(target, {100: observed}, frozen=True) == {100}
    with pytest.raises(panes.PaneError, match="changed"):
        panes.target_members(target, {100: replace(observed, birth="reused")}, frozen=True)


def test_root_changed_during_batch_is_not_terminated_after_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    target = panes.plan_panes(proc=proc).targets[0]
    signals: list[int] = []
    closed: list[int] = []

    def states(_proc: Path) -> dict[int, panes.Process]:
        paused = signal.SIGSTOP in signals
        return {100: replace(target.process, state="T" if paused else "S", foreground=999 if paused else 100)}

    def open_pidfd(_pid: int) -> int:
        return 12345

    def send(_descriptor: int, number: int) -> None:
        signals.append(number)

    def changed_checkpoint(target: panes.PaneTarget, *, proc: Path) -> None:
        panes.verify_frozen_checkpoint(replace(target, thread_id=OLD_ID), proc=proc)

    access = panes.ProcessAccess(read=states, open=open_pidfd, send=send, close=closed.append)
    with pytest.raises(panes.PaneError, match="checkpoint or pane changed"):
        panes.stop_target(target, proc=proc, timeout=1, access=access, checkpoint=changed_checkpoint)
    assert signals == [signal.SIGSTOP, signal.SIGCONT]
    assert closed == [12345]


def test_readiness_requires_exact_root_home_executable_and_new_pane_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    target = panes.plan_panes(proc=fixture.proc).targets[0]
    fixture_replacement(fixture.process, "fixture-marker")
    panes.wait_ready(target, panes.PaneId(99), restart_id="fixture-marker", proc=fixture.proc, timeout=0.05)
    with pytest.raises(panes.PaneError, match="differs"):
        panes.wait_ready(
            replace(target, thread_id=OLD_ID),
            panes.PaneId(99),
            restart_id="fixture-marker",
            proc=fixture.proc,
            timeout=0.05,
        )
    with pytest.raises(panes.PaneError, match="needs attention"):
        panes.wait_ready(target, panes.PaneId(100), restart_id="fixture-marker", proc=fixture.proc, timeout=0.01)


def test_empty_create_ack_is_recovered_only_by_the_new_invocation_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    plan = panes.plan_panes(proc=fixture.proc)
    calls: list[list[str]] = []

    def stopped(_target: panes.PaneTarget, *, proc: Path, timeout: float) -> None:
        pass

    def created(arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        assert capture_output
        assert timeout > 0
        calls.append(list(arguments))
        marker = next(word.partition("=")[2] for word in arguments if word.startswith("LRA_CODEX_RESTART_ID="))
        fixture_replacement(fixture.process, marker)
        return subprocess.CompletedProcess(list(arguments), 0, stdout="", stderr="")

    report = panes.restart_panes(
        plan,
        proc=fixture.proc,
        timeout=0.05,
        replacement=partial(panes.replace_target, stop=stopped, runner=created),
    )
    assert report.reopened == 1
    assert report.failed == 0
    assert len(calls) == 1


@pytest.mark.parametrize("marker", ["", "foreign-invocation"])
def test_missing_or_foreign_marker_cannot_be_counted_as_the_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    target = panes.plan_panes(proc=fixture.proc).targets[0]
    fixture_replacement(fixture.process, marker)
    with pytest.raises(panes.PaneError, match="needs attention"):
        panes.wait_ready(target, None, restart_id="expected-invocation", proc=fixture.proc, timeout=0.01)


def test_only_empty_read_query_ack_is_retried_and_malformed_data_is_redacted() -> None:
    responses = ["", '[{"id":3,"tab_id":1,"is_plugin":false,"exited":false}]']
    calls: list[list[str]] = []

    def read(arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        assert capture_output
        assert timeout > 0
        calls.append(list(arguments))
        return subprocess.CompletedProcess(list(arguments), 0, stdout=responses.pop(0), stderr="")

    assert panes.session_panes("fixture", runner=read)[0].id == 3
    assert len(calls) == 2
    assert all("list-panes" in arguments for arguments in calls)
    responses.append("private-invalid-native-payload")
    with pytest.raises(panes.PaneError, match="unsupported pane metadata") as error:
        panes.session_panes("fixture", runner=read)
    assert "private-invalid" not in str(error.value)
    assert len(calls) == 3


def test_created_pane_without_verified_native_checkpoint_is_reported_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proc = fixture_process(tmp_path, monkeypatch).proc
    plan = panes.plan_panes(proc=proc)

    def stopped(_target: panes.PaneTarget, *, proc: Path, timeout: float) -> None:
        pass

    def created(_arguments: Sequence[str], *, capture_output: bool, timeout: float) -> Completed:
        return native_run(
            [sys.executable, "-c", "print('terminal_99')"], capture_output=capture_output, timeout=timeout
        )

    report = panes.restart_panes(
        plan,
        proc=proc,
        timeout=0.01,
        replacement=partial(panes.replace_target, stop=stopped, runner=created),
    )
    assert report.failed == 1
    assert report.reopened == 0
    assert report.outcomes[0].detail.startswith("Native resume needs attention")
    assert plan.checkpoint.exists()


def test_pidfd_stop_waits_for_only_its_owned_bounded_fixture_child(tmp_path: Path) -> None:
    def checkpoint_verified(_target: panes.PaneTarget, *, proc: Path) -> None:
        pass

    child = spawn(
        [sys.executable, "-c", "import sys;print('ready',flush=True);sys.stdin.read()"],
        stdin=PIPE,
        stdout=PIPE,
        stderr=DEVNULL,
        start_new_session=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline() == b"ready\n"
        state = panes.process_states(Path("/proc"))[child.pid]
        target = panes.PaneTarget(
            state, "fixture", panes.PaneId(3), panes.TabId(1), tmp_path, tmp_path, (), (), ROOT_ID
        )
        panes.stop_target(target, proc=Path("/proc"), timeout=2, checkpoint=checkpoint_verified)
        assert child.wait(timeout=2) == -signal.SIGHUP
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=2)
        if child.stdin is not None:
            child.stdin.close()
        if child.stdout is not None:
            child.stdout.close()


def test_switch_without_running_panes_uses_native_login_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    (fixture.process / "cmdline").write_bytes(b"codex\0exec\0")
    home = tmp_path / ".codex"
    executable = tmp_path / "bin/codex"
    executable.chmod(0o700)
    monkeypatch.setenv("CODEX_HOME", str(home))
    plan = panes.plan_panes(proc=fixture.proc, allow_empty=True)
    assert plan.home == home
    assert plan.executable == executable
    assert not plan.targets
    assert panes.restart_panes(plan, proc=fixture.proc).reopened == 0
    assert not plan.checkpoint.exists()
