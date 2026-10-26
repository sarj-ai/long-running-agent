from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from uuid import UUID

from long_running_agent.runtime import cleanup_state
from long_running_agent.runtime.settings import private_write
import pytest


if TYPE_CHECKING:
    from pathlib import Path


HEAD = "a" * 40


@pytest.fixture
def stamp() -> cleanup_state.AttemptStamp:
    return cleanup_state.AttemptStamp(wall=1000000.0, uptime=1000.0, boot_id=UUID(int=1))


def reserve(state: Path, stamp: cleanup_state.AttemptStamp) -> bool:
    return cleanup_state.reserve_attempt(state, stamp, state / "checkout", HEAD)


def test_attempt_budget_survives_repeated_invocations_and_records_checkout(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    assert reserve(tmp_path, stamp)
    assert reserve(tmp_path, stamp)
    assert not reserve(tmp_path, stamp)
    history = cleanup_state.AttemptHistory.model_validate_json(
        cleanup_state.private_payload(tmp_path / "cleanup-attempts.json")
    )
    assert len(history.root) == 2
    assert all(receipt.path == tmp_path / "checkout" and receipt.head == HEAD for receipt in history.root)
    assert all(receipt.stamp == stamp for receipt in history.root)


def test_capacity_budget_preserves_existing_receipts_and_does_not_refill_normal_policy(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    assert reserve(tmp_path, stamp)
    assert reserve(tmp_path, stamp)
    ledger = tmp_path / "cleanup-attempts.json"
    before = cleanup_state.AttemptHistory.model_validate_json(ledger.read_bytes()).root
    marker = (tmp_path / "cleanup-attempts-initialized").read_bytes()
    assert cleanup_state.remaining_attempts(tmp_path, stamp, limit=48) == 46
    for number in range(46):
        assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / str(number), HEAD, limit=48)
    full = ledger.read_bytes()
    assert not cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / "overflow", HEAD, limit=48)
    assert ledger.read_bytes() == full
    after = cleanup_state.AttemptHistory.model_validate_json(ledger.read_bytes()).root
    assert len(after) == 48
    assert after[:2] == before
    assert (tmp_path / "cleanup-attempts-initialized").read_bytes() == marker
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 0
    assert not reserve(tmp_path, stamp)


def test_larger_capacity_budget_preserves_the_existing_24_receipts_when_crossing_the_old_limit(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    for number in range(24):
        assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / str(number), HEAD, limit=24)
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    receipts = cleanup_state.AttemptHistory.model_validate_json(before[0]).root
    assert cleanup_state.remaining_attempts(tmp_path, stamp, limit=48) == 24
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / "new-capacity", HEAD, limit=48)
    after = ledger.read_bytes()
    assert after.startswith(before[0].removesuffix(b"]\n") + b",")
    assert cleanup_state.AttemptHistory.model_validate_json(after).root[:24] == receipts
    assert len(cleanup_state.AttemptHistory.model_validate_json(after).root) == 25
    assert marker.read_bytes() == before[1]
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 0
    assert not reserve(tmp_path, stamp)


@pytest.mark.parametrize("limit", [0, -1, 257, True])
def test_invalid_capacity_budgets_cannot_authorize_removal(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp, limit: int
) -> None:
    with pytest.raises(ValueError, match="invalid daily limit"):
        cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / "held", HEAD, limit=limit)
    assert not (tmp_path / "cleanup-attempts.json").exists()


def test_reading_available_attempts_never_creates_or_prunes_state(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 2
    assert not ledger.exists()
    assert not marker.exists()
    assert reserve(tmp_path, stamp)
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 1
    assert reserve(tmp_path, stamp)
    before = ledger.read_bytes(), marker.read_bytes()
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 0
    later = cleanup_state.AttemptStamp(
        wall=stamp.wall + cleanup_state.DAY, uptime=stamp.uptime + cleanup_state.DAY, boot_id=stamp.boot_id
    )
    assert cleanup_state.remaining_attempts(tmp_path, later) == 2
    assert (ledger.read_bytes(), marker.read_bytes()) == before


def test_private_cursor_preserves_a_deleted_ordering_anchor_without_touching_the_budget(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    root = tmp_path / "code"
    root.mkdir()
    assert reserve(tmp_path, stamp)
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    cursor = cleanup_state.ReviewCursor(version=1, modified=1000000.0, path=root / "gone")
    cleanup_state.save_review_cursor(tmp_path, root, cursor)
    path = tmp_path / "cleanup-cursor.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert cleanup_state.review_cursor(tmp_path, root) == cursor
    assert not cursor.path.exists()
    assert (ledger.read_bytes(), marker.read_bytes()) == before


@pytest.mark.parametrize(
    "invalid",
    [
        "malformed",
        "version",
        "boolean-version",
        "missing-version",
        "extra",
        "negative",
        "infinite",
        "relative",
        "outside",
        "parent",
        "root",
    ],
)
def test_unknown_cursor_schema_or_scope_is_retained_without_replacement(tmp_path: Path, invalid: str) -> None:
    root = tmp_path / "code"
    root.mkdir()
    cursor = cleanup_state.ReviewCursor(version=1, modified=1000000.0, path=root / "candidate")
    payload: dict[str, object] = cursor.model_dump(mode="json")
    match invalid:
        case "malformed":
            text = "not-json"
        case _:
            match invalid:
                case "version":
                    payload["version"] = 2
                case "boolean-version":
                    payload["version"] = True
                case "missing-version":
                    payload = {name: value for name, value in payload.items() if name != "version"}
                case "extra":
                    payload["eligible"] = True
                case "negative":
                    payload["modified"] = -1.0
                case "infinite":
                    payload["modified"] = float("inf")
                case "relative":
                    payload["path"] = "relative/candidate"
                case "outside":
                    payload["path"] = str(tmp_path / "outside")
                case "parent":
                    payload["path"] = str(root / ".." / "outside")
                case "root":
                    payload["path"] = str(root)
                case _:
                    pytest.fail("Unknown private cursor fixture")
            text = json.dumps(payload)
    path = tmp_path / "cleanup-cursor.json"
    private_write(path, text)
    before = path.read_bytes(), path.stat().st_ino
    with pytest.raises((ValueError, RuntimeError), match=r"validation error|unknown checkout path"):
        cleanup_state.review_cursor(tmp_path, root)
    with pytest.raises((ValueError, RuntimeError), match=r"validation error|unknown checkout path"):
        cleanup_state.save_review_cursor(tmp_path, root, cursor)
    assert (path.read_bytes(), path.stat().st_ino) == before


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "oversized", "fifo"])
def test_cursor_requires_a_bounded_private_regular_file(tmp_path: Path, kind: str) -> None:
    root = tmp_path / "code"
    root.mkdir()
    cursor = cleanup_state.ReviewCursor(version=1, modified=1000000.0, path=root / "candidate")
    target = tmp_path / "original"
    private_write(target, cursor.model_dump_json())
    path = tmp_path / "cleanup-cursor.json"
    match kind:
        case "symlink":
            path.symlink_to(target)
        case "hardlink":
            os.link(target, path)
        case "fifo":
            os.mkfifo(path, mode=0o600)
        case "oversized":
            private_write(path, "x" * (cleanup_state.CURSOR_LIMIT + 1))
        case "public":
            private_write(path, target.read_text())
            path.chmod(0o644)
        case _:
            private_write(path, target.read_text())
    before = target.read_bytes(), path.lstat().st_ino, path.lstat().st_mode
    with pytest.raises((OSError, RuntimeError), match=r"Too many levels|private regular file"):
        cleanup_state.review_cursor(tmp_path, root)
    with pytest.raises((OSError, RuntimeError), match=r"Too many levels|private regular file"):
        cleanup_state.save_review_cursor(tmp_path, root, cursor)
    assert (target.read_bytes(), path.lstat().st_ino, path.lstat().st_mode) == before


def test_reading_missing_initialized_ledger_cannot_restore_a_fresh_budget(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    assert reserve(tmp_path, stamp)
    ledger = tmp_path / "cleanup-attempts.json"
    ledger.unlink()
    with pytest.raises(RuntimeError, match="budget is missing or unknown"):
        cleanup_state.remaining_attempts(tmp_path, stamp)
    assert not ledger.exists()


@pytest.mark.parametrize(
    ("wall_delta", "uptime_delta", "boot", "available"),
    [
        pytest.param(2 * cleanup_state.DAY, 1, 1, False, id="forward-wall-clock-does-not-refill"),
        pytest.param(-1000, 1, 1, False, id="backward-wall-clock-does-not-refill"),
        pytest.param(1, -1, 1, False, id="unknown-backward-uptime-retains"),
        pytest.param(cleanup_state.DAY, cleanup_state.DAY - 1, 1, False, id="full-observed-day-required"),
        pytest.param(0, cleanup_state.DAY, 1, True, id="same-boot-uses-observed-uptime"),
        pytest.param(2 * cleanup_state.DAY, 0, 2, False, id="new-boot-cannot-reset-budget"),
        pytest.param(-1000, cleanup_state.DAY, 2, False, id="new-boot-needs-wall-and-uptime"),
        pytest.param(cleanup_state.DAY, cleanup_state.DAY, 2, True, id="new-boot-observes-full-day"),
    ],
)
def test_clock_changes_and_reboots_cannot_mint_removal_attempts(
    tmp_path: Path,
    stamp: cleanup_state.AttemptStamp,
    wall_delta: float,
    uptime_delta: float,
    boot: int,
    *,
    available: bool,
) -> None:
    assert reserve(tmp_path, stamp)
    assert reserve(tmp_path, stamp)
    current = cleanup_state.AttemptStamp(
        wall=stamp.wall + wall_delta, uptime=stamp.uptime + uptime_delta, boot_id=UUID(int=boot)
    )
    assert reserve(tmp_path, current) is available


def test_missing_initialized_budget_never_becomes_a_fresh_budget(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    assert reserve(tmp_path, stamp)
    ledger = tmp_path / "cleanup-attempts.json"
    ledger.unlink()
    with pytest.raises(RuntimeError, match="budget is missing or unknown"):
        reserve(tmp_path, stamp)
    assert not ledger.exists()


@pytest.mark.parametrize(
    "payload",
    ["", "{}", "[{}]", "not-json"],
    ids=["empty-ledger", "wrong-root-object", "missing-receipt-fields", "malformed-json"],
)
def test_corrupt_budget_is_retained_without_overwrite(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp, payload: str
) -> None:
    ledger = tmp_path / "cleanup-attempts.json"
    private_write(ledger, payload)
    with pytest.raises(ValueError, match="validation error"):
        reserve(tmp_path, stamp)
    assert ledger.read_text() == payload


def test_unknown_initialization_marker_blocks_budget_use(tmp_path: Path, stamp: cleanup_state.AttemptStamp) -> None:
    assert reserve(tmp_path, stamp)
    marker = tmp_path / "cleanup-attempts-initialized"
    private_write(marker, "unknown\n")
    before = (tmp_path / "cleanup-attempts.json").read_bytes()
    with pytest.raises(RuntimeError, match="budget is missing or unknown"):
        reserve(tmp_path, stamp)
    assert (tmp_path / "cleanup-attempts.json").read_bytes() == before


def test_private_state_rejects_symlinks_hardlinks_and_public_files(tmp_path: Path) -> None:
    target = tmp_path / "target"
    private_write(target, "private\n")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(OSError, match="Too many levels"):
        cleanup_state.private_payload(link)
    link.unlink()
    os.link(target, link)
    with pytest.raises(RuntimeError, match="private regular file"):
        cleanup_state.private_payload(target)
    link.unlink()
    target.chmod(0o644)
    with pytest.raises(RuntimeError, match="private regular file"):
        cleanup_state.private_payload(target)
    assert target.read_text() == "private\n"


def test_fifo_and_oversized_state_fail_without_blocking_or_reading_payload(tmp_path: Path) -> None:
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, mode=0o600)
    with pytest.raises(RuntimeError, match="private regular file"):
        cleanup_state.private_payload(fifo)
    large = tmp_path / "large"
    private_write(large, "x" * (cleanup_state.PRIVATE_LIMIT + 1))
    with pytest.raises(RuntimeError, match="bounded private regular"):
        cleanup_state.private_payload(large)


def test_oversized_next_utf8_receipt_preserves_the_private_budget_and_candidate_data(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    wide = tmp_path.joinpath(*("界" * 80 for _ in range(8)))
    receipts: list[cleanup_state.AttemptReceipt] = []
    receipt = cleanup_state.AttemptReceipt(stamp=stamp, path=wide / "0", head=HEAD)
    payload = cleanup_state.AttemptHistory([receipt]).model_dump_json() + "\n"
    while len(payload.encode("utf-8")) <= cleanup_state.PRIVATE_LIMIT:
        receipts.append(receipt)
        receipt = cleanup_state.AttemptReceipt(stamp=stamp, path=wide / str(len(receipts)), head=HEAD)
        payload = cleanup_state.AttemptHistory([*receipts, receipt]).model_dump_json() + "\n"
    assert 48 < len(receipts) < cleanup_state.CAPACITY_ATTEMPTS
    assert len(payload) < cleanup_state.PRIVATE_LIMIT
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    private_write(ledger, cleanup_state.AttemptHistory(receipts).model_dump_json() + "\n")
    private_write(marker, "1\n")
    receipt.path.mkdir(parents=True)
    retained = receipt.path / "source.txt"
    retained.write_text("Retain private candidate data.\n")
    before = ledger.read_bytes(), marker.read_bytes()
    assert cleanup_state.private_payload(ledger) == before[0]
    with pytest.raises(RuntimeError, match="next native-removal receipt exceeds the private state limit"):
        cleanup_state.reserve_attempt(tmp_path, stamp, receipt.path, HEAD, limit=cleanup_state.CAPACITY_ATTEMPTS)
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    assert cleanup_state.remaining_attempts(
        tmp_path, stamp, limit=cleanup_state.CAPACITY_ATTEMPTS
    ) == cleanup_state.CAPACITY_ATTEMPTS - len(receipts)
    assert retained.read_text() == "Retain private candidate data.\n"


def test_failed_initialization_write_conservatively_keeps_the_charge(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp, monkeypatch: pytest.MonkeyPatch
) -> None:
    def marker_full(path: Path, text: str) -> None:
        if path.name == "cleanup-attempts-initialized":
            message = "Fixture disk is full"
            raise OSError(message)
        private_write(path, text)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Atomic filesystem writes are the native boundary; simulate ENOSPC before any Git deletion.
        cleanup_state, "private_write", marker_full
    )
    with pytest.raises(OSError, match="disk is full"):
        reserve(tmp_path, stamp)
    history = cleanup_state.AttemptHistory.model_validate_json(
        cleanup_state.private_payload(tmp_path / "cleanup-attempts.json")
    )
    assert len(history.root) == 1
    assert history.root[0].path == tmp_path / "checkout"


def test_full_previous_capacity_ledger_upgrades_without_resetting_receipts_or_marker(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    for number in range(48):
        assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / str(number), HEAD, limit=48)
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    before = ledger.read_bytes(), marker.read_bytes()
    previous = cleanup_state.AttemptHistory.model_validate_json(before[0]).root
    assert cleanup_state.remaining_attempts(tmp_path, stamp, limit=256) == 208
    assert (ledger.read_bytes(), marker.read_bytes()) == before
    assert cleanup_state.reserve_attempt(tmp_path, stamp, tmp_path / "next-recovery", HEAD, limit=256)
    after = cleanup_state.AttemptHistory.model_validate_json(ledger.read_bytes()).root
    assert after[:48] == previous
    assert len(after) == 49
    assert marker.read_bytes() == before[1]
    assert cleanup_state.remaining_attempts(tmp_path, stamp) == 0


def test_full_larger_capacity_budget_is_bounded_and_can_exceed_the_old_byte_limit(
    tmp_path: Path, stamp: cleanup_state.AttemptStamp
) -> None:
    rows = [
        cleanup_state.AttemptReceipt(stamp=stamp, path=tmp_path / (str(number) + "x" * 160), head=HEAD)
        for number in range(256)
    ]
    payload = cleanup_state.AttemptHistory(rows).model_dump_json() + "\n"
    assert 65536 < len(payload.encode("utf-8")) < cleanup_state.PRIVATE_LIMIT
    ledger = tmp_path / "cleanup-attempts.json"
    marker = tmp_path / "cleanup-attempts-initialized"
    private_write(ledger, payload)
    private_write(marker, "1\n")
    retained = tmp_path / "overflow-source"
    retained.write_text("Preserve the source after the bounded daily allowance is exhausted.\n")
    before = ledger.read_bytes(), marker.read_bytes(), retained.read_bytes()
    assert cleanup_state.remaining_attempts(tmp_path, stamp, limit=256) == 0
    assert not cleanup_state.reserve_attempt(tmp_path, stamp, retained, HEAD, limit=256)
    assert (ledger.read_bytes(), marker.read_bytes(), retained.read_bytes()) == before
