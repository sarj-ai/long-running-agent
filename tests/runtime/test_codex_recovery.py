from __future__ import annotations

import configparser
from contextlib import closing
import json
import shlex
import shutil
import sqlite3
import sys
from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from pathlib import Path

from long_running_agent.process import run
from long_running_agent.runtime import codex, codex_events, codex_panes, codex_recovery as recovery
from long_running_agent.runtime.settings import RuntimePaths, private_write
import pytest

from tests.runtime.test_codex_panes import fixture_process


ERRORS = (
    "■ Selected model is at capacity. Please try a different model.",
    "■ The selected model is currently at capacity. Please try again later.",
    "Error: Selected model is at capacity.",
    "⚠ Selected model is at capacity.\n  Please try a different model.",
    "\x1b[31m■ Selected model is at capacity. Please try a different model.\x1b[0m",
    "■ unexpected status 503 Service Unavailable: Selected model is at capacity.\nPlease try a different model., url: http://127.0.0.1:8080/v1/responses",
)


def record_failure(home: Path, sequence: int = 0, *, active: bool = False, success: bool = False) -> None:
    path = home / ".codex/sessions/rollout-now-11111111-1111-1111-1111-111111111111.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.touch(mode=0o600)
    event = {
        "type": "task_started" if active else "task_complete",
        "turn_id": f"fixture-turn-{sequence}",
        "error": None
        if active or success
        else {"message": "Selected model is at capacity. Please try a different model."},
    }
    with path.open("a") as stream:
        stream.write(
            json.dumps({"type": "event_msg", "timestamp": f"fixture-time-{sequence}", "payload": event}) + "\n"
        )


@pytest.mark.parametrize("pass_number", range(1, 51), ids=[f"capacity-pass-{value:02}" for value in range(1, 51)])
def test_capacity_recovery_pass(pass_number: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = fixture_process(tmp_path, monkeypatch, source="cli" if pass_number % 2 else "exec")
    record_failure(tmp_path)
    error = ERRORS[(pass_number - 1) % len(ERRORS)]
    padding = "\n" * (pass_number % 4)
    composer = "› \x1b[2mAsk Codex to do anything\x1b[22m" if pass_number % 2 else "›"
    screen = [f"{padding}{error}\n\n{composer}\n  ? for shortcuts\n"]
    calls: list[codex_panes.PaneTarget] = []

    def continue_and_fail(target: codex_panes.PaneTarget) -> None:
        calls.append(target)
        record_failure(tmp_path, len(calls))

    io = recovery.PaneIO(screen=lambda _target: screen[0], submit=continue_and_fail)
    now = float(1000 + pass_number * 37)
    preview = recovery.sweep(tmp_path, proc=fixture.proc, io=io, now=now)
    assert preview.capacity == 1
    assert not (recovery.directory(tmp_path) / "retries.json").exists()
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=now).waiting == 1
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=now + 29).continued == 0
    assert not calls
    ready = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=now + 30)
    assert ready.continued == 1
    assert calls[0].thread_id == "11111111-1111-1111-1111-111111111111"
    assert calls[0].pane_id == 3
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=now + 31).continued == 0
    root = recovery.directory(tmp_path)
    state = recovery.ledger(root)
    assert next(iter(state.attempts.values())).due == now + 90
    for expected_attempts in range(2, 9):
        due = next(iter(recovery.ledger(root).attempts.values())).due
        report = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=due)
        assert report.continued == 1
        assert len(calls) == expected_attempts
    due = next(iter(recovery.ledger(root).attempts.values())).due
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=due - 1).exhausted == 1
    assert len(calls) == 8
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=due).waiting == 1
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=due + 30).continued == 1
    assert len(calls) == 9
    record = (root / "retries.json").read_text()
    assert "Selected model" not in record
    assert "private startup prompt" not in record
    assert (root / "retries.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "screen",
    [
        "■ Selected model is at capacity. Please try a different model.\n› my unfinished draft\n",
        "› Explain the error 'selected model is at capacity'\n",
        "The assistant mentioned selected model is at capacity.\n›\n",
        "■ Selected model is at capacity. Please try a different model.\nAssistant finished the task.\n›\n",
        "■ Selected model is at capacity. Please try a different model.\n• Working\n›\n",
        "■ Selected model is at capacity. Please try a different model.\n›\nesc to interrupt\n",
        "■ Selected model is at capacity. Please try a different model.\nSign in to continue\n›\n",
        "■ You've hit your usage limit.\n›\n",
        "■ Selected model is at capacity. Please try a different model.\nApprove this command?\n›\n",
        "■ Selected model is at capacity. Please try a different model.\n$\n",
    ],
    ids=(
        "draft",
        "quoted-user-text",
        "assistant-text",
        "old-error",
        "working",
        "interrupt",
        "login",
        "quota",
        "approval",
        "shell",
    ),
)
def test_capacity_detection_refuses_noncapacity_and_nonidle_windows(screen: str) -> None:
    assert recovery.capacity_error(screen) is None


def test_uncertain_native_input_is_not_replayed_after_daemon_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    calls: list[int] = []

    def timeout(_target: codex_panes.PaneTarget) -> None:
        calls.append(1)
        message = "Native action acknowledgment lost"
        raise RuntimeError(message)

    io = recovery.PaneIO(screen=lambda _target: ERRORS[0] + "\n›\n", submit=timeout)
    recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1000.0)
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1030.0).uncertain == 1
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=20_000.0).uncertain == 1
    assert calls == [1]


def test_crash_receipt_is_uncertain_and_budget_survives_restart(tmp_path: Path) -> None:
    root = recovery.directory(tmp_path)
    state = recovery.Ledger(
        attempts={"fixture": recovery.Attempt(error="f" * 64, attempts=1, due=1234.0, phase="dispatching")}
    )
    recovery.save(root, state)
    restored = recovery.ledger(root)
    assert restored.attempts["fixture"].phase == "uncertain"
    assert restored.attempts["fixture"].attempts == 1


def test_process_replacement_between_inventory_and_input_prevents_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    screen_calls: list[int] = []

    def screen(_target: codex_panes.PaneTarget) -> str:
        screen_calls.append(1)
        if len(screen_calls) == 2:
            path = fixture.process / "stat"
            path.write_text(path.read_text().replace("1234", "9876"))
        return ERRORS[0] + "\n›\n"

    io = recovery.PaneIO(
        screen=screen, submit=lambda _target: pytest.fail("Reused process identity must not receive input")
    )
    recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1000.0)
    report = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1030.0)
    assert report.continued == 0
    assert report.blocked == 1


def test_user_typing_between_two_screen_reads_prevents_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    screens = iter((ERRORS[0] + "\n›\n", ERRORS[0] + "\n›\n", ERRORS[0] + "\n› user draft\n"))
    io = recovery.PaneIO(
        screen=lambda _target: next(screens), submit=lambda _target: pytest.fail("A user draft must not be submitted")
    )
    recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1000.0)
    report = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, io=io, now=1030.0)
    assert report.continued == 0
    assert report.blocked == 1


def test_account_switch_lock_prevents_capacity_input(tmp_path: Path) -> None:
    with recovery.guard(tmp_path), pytest.raises(recovery.RecoveryBusyError):
        recovery.sweep(tmp_path, apply=True)


@pytest.mark.parametrize("damage", ["symlink", "public", "hardlink", "invalid", "oversize"])
def test_unsafe_or_corrupt_recovery_record_cannot_authorize_input(tmp_path: Path, damage: str) -> None:
    root = recovery.directory(tmp_path)
    path = root / "retries.json"
    private_write(path, recovery.Ledger().model_dump_json())
    match damage:
        case "symlink":
            moved = root / "retained"
            path.rename(moved)
            path.symlink_to(moved)
        case "public":
            path.chmod(0o644)
        case "hardlink":
            (root / "extra").hardlink_to(path)
        case "invalid":
            path.write_text('{"attempts":{"private-secret":"invalid"}}')
        case "oversize":
            path.write_bytes(b" " * 1_048_577)
        case _:
            pytest.fail("Unsupported fixture damage")
    with pytest.raises((OSError, recovery.RecoveryError)) as error:
        recovery.ledger(root)
    assert "private-secret" not in str(error.value)


def test_service_is_private_scoped_and_preserves_native_configuration(tmp_path: Path) -> None:
    content = recovery.service(RuntimePaths(tmp_path), tmp_path / "venv/bin/python")
    assert " -I -m long_running_agent.cli codex retry run\n" in content
    assert "UMask=0077" in content
    assert "Restart=on-failure" in content
    assert "KillMode=control-group" in content
    assert "PrivateTmp" not in content
    assert "approval_policy" not in content
    assert "CODEX_HOME" not in content


@pytest.mark.parametrize(
    ("composer", "empty"),
    [
        ("›", True),
        ("› \x1b[2mFind and fix a bug\x1b[22m", True),
        ("\x1b[38;2;188;141;253m›\x1b[39m \x1b[2mTry a task\x1b[22m", True),
        ("› my own draft", False),
        ("› \x1b[38;2;2;2;2mmy own draft", False),
        ("› \x1b[2mplaceholder\x1b[22mchanged", False),
    ],
    ids=(
        "blank",
        "dim-placeholder",
        "native-colored-placeholder",
        "typed",
        "rgb-is-not-dim",
        "typed-after-placeholder",
    ),
)
def test_only_empty_native_composers_allow_retry(composer: str, *, empty: bool) -> None:
    assert recovery.empty_composer(composer) is empty
    assert (recovery.capacity_error(ERRORS[0] + "\n" + composer + "\n") is not None) is empty


@pytest.mark.parametrize("active", [False, True], ids=("assistant-quoted-error", "active-turn"))
def test_native_turn_state_overrules_old_or_quoted_capacity_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, active: bool
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    record_failure(tmp_path, 1, active=active, success=not active)
    io = recovery.PaneIO(
        screen=lambda _target: ERRORS[0] + "\n›\n",
        submit=lambda _target: pytest.fail("Only a latest native capacity failure permits continuation"),
    )
    report = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1000.0, io=io)
    assert report.continued == 0
    assert report.capacity == 0
    assert report.blocked == 1


def test_successful_input_ack_is_not_replayed_without_a_new_failed_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    calls: list[codex_panes.PaneTarget] = []
    io = recovery.PaneIO(screen=lambda _target: ERRORS[0] + "\n›\n", submit=calls.append)
    recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1000.0, io=io)
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1030.0, io=io).uncertain == 1
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=10_000.0, io=io).uncertain == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("tail", "expected"),
    [
        (b"not-valid-private-json\n", "unknown"),
        (b'{"type":"event_msg","timestamp":"next","payload":{"type":"task_started"}}\n', "active"),
        (b'{"type":"event_msg","timestamp":"next","payload":{"type":"task_complete","error":null}}\n', "success"),
        (b'{"type":"event_msg","timestamp":"next","payload":{"type":"task_complete"}}\n', "unknown"),
        (
            b'{"type":"event_msg","timestamp":"next","payload":{"type":"task_complete","error":{"message":"Quota exceeded"}}}}\n',
            "unknown",
        ),
    ],
    ids=("corrupt-json", "new-turn", "success", "missing-completion-error", "malformed-error"),
)
def test_latest_native_record_controls_recovery(tmp_path: Path, tail: bytes, expected: str) -> None:
    record_failure(tmp_path)
    path = tmp_path / ".codex/sessions/rollout-now-11111111-1111-1111-1111-111111111111.jsonl"
    baseline = path.read_bytes()
    assert codex_events.latest_completion(baseline).state == "capacity"
    assert codex_events.latest_completion(baseline + tail).state == expected


@pytest.mark.parametrize(
    ("active", "composer", "idle"),
    [(True, "›", False), (False, "›", True), (False, "› typed draft", False)],
    ids=("working-turn", "completed-capacity-error", "draft"),
)
def test_account_switch_requires_idle_native_turn_and_empty_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, active: bool, composer: str, idle: bool
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path, active=active)
    target = codex_panes.plan_panes(proc=fixture.proc).targets[0]
    io = recovery.PaneIO(screen=lambda _target: composer)
    if idle:
        codex.require_idle(target, proc=fixture.proc, io=io)
    else:
        with pytest.raises(codex_panes.PaneError, match="let it finish"):
            codex.require_idle(target, proc=fixture.proc, io=io)


def test_cursor_rotates_across_more_than_one_page_of_native_panes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = fixture_process(tmp_path, monkeypatch)
    record_failure(tmp_path)
    home = tmp_path / ".codex"
    source = home / "sessions/rollout-now-11111111-1111-1111-1111-111111111111.jsonl"
    rows = [{"id": 3, "tab_id": 1, "is_plugin": False, "exited": False}]
    threads: list[tuple[str, str]] = []
    for index in range(1, 17):
        pid = 100 + index
        process = fixture.proc / str(pid)
        shutil.copytree(fixture.process, process, symlinks=True)
        fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
        fields[2] = fields[5] = str(pid)
        (process / "stat").write_text(f"{pid} (codex) " + " ".join(fields))
        environment = process / "environ"
        environment.write_bytes(
            environment.read_bytes().replace(b"ZELLIJ_PANE_ID=3\0", f"ZELLIJ_PANE_ID={3 + index}\0".encode())
        )
        identifier = f"{index:08x}-2222-2222-2222-222222222222"
        target = home / "sessions" / f"rollout-now-{identifier}.jsonl"
        shutil.copyfile(source, target)
        target.chmod(0o600)
        descriptor = process / "fd/4"
        descriptor.unlink()
        descriptor.symlink_to(target)
        threads.append((identifier, "cli"))
        rows.append({"id": 3 + index, "tab_id": 1, "is_plugin": False, "exited": False})
    with closing(sqlite3.connect(home / "state_5.sqlite")) as connection:
        connection.executemany(  # sarj-noqa: SARJ429 -- Seed Codex's external native index for isolated terminal fixtures.
            "INSERT INTO threads VALUES (?,?)", threads
        )
        connection.commit()
    (tmp_path / "bin/zellij-panes.json").write_text(json.dumps(rows))
    calls: list[codex_panes.PaneTarget] = []

    def continued(target: codex_panes.PaneTarget) -> None:
        calls.append(target)
        path = target.home / "sessions" / f"rollout-now-{target.thread_id}.jsonl"
        with path.open("a") as stream:
            stream.write('{"type":"event_msg","timestamp":"next","payload":{"type":"task_started"}}\n')

    io = recovery.PaneIO(screen=lambda _target: ERRORS[0] + "\n›\n", submit=continued)
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1000.0, io=io).checked == 16
    assert len(recovery.ledger(recovery.directory(tmp_path)).attempts) == 16
    assert recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1000.0, io=io).checked == 16
    assert len(recovery.ledger(recovery.directory(tmp_path)).attempts) == 17
    ready = recovery.sweep(tmp_path, apply=True, proc=fixture.proc, now=1030.0, io=io)
    assert ready.continued == ready.attempted == 1
    assert len(calls) == 1


def test_reset_archives_bad_receipts_without_losing_their_evidence(tmp_path: Path) -> None:
    root = recovery.directory(tmp_path)
    private_write(root / "retries.json", "malformed private receipt")
    private_write(root / "status.json", "previous heartbeat")
    backup = recovery.reset_receipts(tmp_path)
    assert backup is not None
    assert (backup / "retries.json").read_text() == "malformed private receipt"
    assert (backup / "status.json").read_text() == "previous heartbeat"
    assert backup.stat().st_mode & 0o777 == 0o700
    assert not recovery.ledger(root).attempts
    assert not (root / "status.json").exists()


def test_reset_preserves_symlinked_metadata_and_external_target(tmp_path: Path) -> None:
    root = recovery.directory(tmp_path)
    target = tmp_path / "retained"
    target.write_text("retained data")
    (root / "retries.json").symlink_to(target)
    with pytest.raises(recovery.RecoveryError, match="was preserved"):
        recovery.reset_receipts(tmp_path)
    assert target.read_text() == "retained data"
    assert (root / "retries.json").is_symlink()


def test_enable_keeps_the_selected_virtualenv_python(tmp_path: Path) -> None:
    tools = tmp_path / "bin"
    tools.mkdir()
    systemctl = tools / "systemctl"
    systemctl.write_text(f"#!{sys.executable}\n")
    systemctl.chmod(0o700)
    result = run(
        [sys.executable, "-I", "-m", "long_running_agent.cli", "codex", "retry", "enable"],
        env={"HOME": str(tmp_path), "PATH": str(tools)},
        capture_output=True,
        timeout=20,
    )
    assert "recovery enabled" in result.stdout
    configuration = configparser.ConfigParser(interpolation=None, strict=False)
    configuration.read(tmp_path / ".config/systemd/user/lra-codex-recovery.service")
    command = shlex.split(configuration["Service"]["ExecStart"])
    assert command == [sys.executable, "-I", "-m", "long_running_agent.cli", "codex", "retry", "run"]
