from __future__ import annotations

from dataclasses import dataclass, replace
import os
from pathlib import Path

from long_running_agent.runtime import cleanup_capacity
from long_running_agent.runtime.cleanup_state import private_payload
from long_running_agent.runtime.settings import private_write
import pytest


GIB = 1024**3
KEY = "a" * 64


def volume(
    *,
    total: int = 1000 * GIB,
    allocated: int = 300 * GIB,
    reserved: int = 10 * GIB,
    unit: int = 4096,
    key: str | None = KEY,
) -> cleanup_capacity.VolumeReading:
    return cleanup_capacity.VolumeReading(
        total=total, available=total - allocated - reserved, free=total - allocated, unit=unit, key=key
    )


@dataclass
class CapacityFixture:
    home: Path
    state: Path
    reading: cleanup_capacity.VolumeReading

    def read(self, home: Path) -> cleanup_capacity.VolumeReading:
        assert home == self.home
        return self.reading

    def record(self) -> cleanup_capacity.Calibration:
        return cleanup_capacity.Calibration.model_validate_json(
            private_payload(self.state / cleanup_capacity.BASELINE_FILE)
        )

    def observe(self, now: float, *, epoch: str = "fixture-boot", eligible: bool = True) -> bool:
        return cleanup_capacity.observe_lower(self.home, now=now, epoch=epoch, eligible=eligible)


@pytest.fixture
def capacity_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CapacityFixture:
    state = tmp_path / ".local/state/long-running-agent"
    state.mkdir(parents=True, mode=0o700)
    fixture = CapacityFixture(tmp_path, state, volume())
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace the native capacity boundary; all calibration writes stay in this private fixture home.
        cleanup_capacity, "read_volume", fixture.read
    )
    return fixture


def test_uncalibrated_full_host_does_not_adopt_current_usage(capacity_fixture: CapacityFixture) -> None:
    capacity_fixture.reading = volume(allocated=800 * GIB)
    result = cleanup_capacity.policy(capacity_fixture.home)
    assert result.thresholds.mode == "bootstrap"
    assert result.thresholds.enter == 250 * GIB
    assert result.thresholds.stop == 200 * GIB
    assert result.thresholds.baseline_allocated is None
    for now in (0.0, 900.0, 1800.0):
        assert not capacity_fixture.observe(now)
    assert not (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).exists()


def test_preview_computes_real_footprint_without_initializing_state(capacity_fixture: CapacityFixture) -> None:
    preview = cleanup_capacity.calibrate(capacity_fixture.home)
    assert preview.thresholds.mode == "preview"
    assert preview.thresholds.baseline_allocated == 300 * GIB
    assert preview.volume.used == 310 * GIB
    assert not (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).exists()
    assert not (capacity_fixture.state / cleanup_capacity.INITIALIZED_FILE).exists()


@pytest.mark.parametrize("allocated", [30 * GIB, 300 * GIB], ids=["nearly-empty-vm", "cleaned-mac-footprint"])
def test_explicit_calibration_targets_each_hosts_actual_footprint(
    capacity_fixture: CapacityFixture, allocated: int
) -> None:
    capacity_fixture.reading = volume(allocated=allocated)
    result = cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    assert result.thresholds.mode == "calibrated"
    assert result.thresholds.stop == allocated + result.volume.reserved + result.thresholds.headroom
    assert result.thresholds.enter - result.thresholds.stop == result.thresholds.headroom
    assert 0 < result.thresholds.headroom <= 2 * GIB
    assert cleanup_capacity.policy(capacity_fixture.home) == result
    record = capacity_fixture.record()
    assert record.allocated == allocated
    assert record.provenance == "explicit-post-cleanup"
    assert (capacity_fixture.state / cleanup_capacity.INITIALIZED_FILE).read_bytes() == b"1\n"
    assert (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).stat().st_mode & 0o077 == 0


def test_stable_lower_footprint_lowers_to_largest_sample(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    original = capacity_fixture.record()
    readings = (200 * GIB, 200 * GIB + 64 * 1024**2, 200 * GIB)
    outcomes: list[bool] = []
    for index, allocated in enumerate(readings):
        capacity_fixture.reading = volume(allocated=allocated)
        outcomes.append(capacity_fixture.observe(index * 900.0))
    assert outcomes == [False, False, True]
    record = capacity_fixture.record()
    assert record.allocated == max(readings)
    assert record.generation == original.generation
    assert record.revision == 3
    assert not record.observations


def test_growth_and_repeated_success_never_raise_baseline(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=700 * GIB)
    for now in (0.0, 900.0, 1800.0, 3600.0):
        assert not capacity_fixture.observe(now)
    assert capacity_fixture.record().allocated == 300 * GIB


def import_full_lower_window(capacity_fixture: CapacityFixture) -> None:
    previous = capacity_fixture.record()
    imported = cleanup_capacity.Calibration(
        version=1,
        provenance=previous.provenance,
        key=previous.key,
        generation=previous.generation,
        revision=previous.revision,
        calibrated_at=previous.calibrated_at,
        allocated=previous.allocated,
        total=previous.total,
        reserved=previous.reserved,
        unit=previous.unit,
        epoch="fixture-boot",
        observed_at=1800.0,
        observations=tuple(
            cleanup_capacity.LowerObservation(at=now, allocated=200 * GIB) for now in (0.0, 900.0, 1800.0)
        ),
    )
    private_write(capacity_fixture.state / cleanup_capacity.BASELINE_FILE, imported.model_dump_json())


def test_imported_full_window_waits_for_a_fresh_spaced_observation(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    import_full_lower_window(capacity_fixture)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(1830.0)
    pending = capacity_fixture.record()
    assert pending.allocated == 300 * GIB
    assert len(pending.observations) == 2
    assert capacity_fixture.observe(2700.0)
    assert capacity_fixture.record().allocated == 200 * GIB


@pytest.mark.parametrize("allocated", [200 * GIB, 150 * GIB], ids=["stable-window", "new-low-outlier"])
def test_imported_full_window_never_grows_past_three_samples(capacity_fixture: CapacityFixture, allocated: int) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    import_full_lower_window(capacity_fixture)
    capacity_fixture.reading = volume(allocated=allocated)
    lowered = capacity_fixture.observe(2700.0)
    record = capacity_fixture.record()
    assert lowered == (allocated == 200 * GIB)
    assert record.allocated == (200 * GIB if lowered else 300 * GIB)
    assert len(record.observations) <= 3


def test_single_low_outlier_does_not_become_baseline(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    for now, allocated in ((0.0, 200 * GIB), (900.0, 100 * GIB), (1800.0, 200 * GIB)):
        capacity_fixture.reading = volume(allocated=allocated)
        assert not capacity_fixture.observe(now)
    assert capacity_fixture.record().allocated == 300 * GIB
    assert len(capacity_fixture.record().observations) == 1


def test_frequent_samples_cannot_accelerate_calibration(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    for now in (0.0, 30.0, 60.0, 899.0, 900.0, 1799.0):
        assert not capacity_fixture.observe(now)
    assert capacity_fixture.observe(1800.0)


def test_failed_eligibility_clears_pending_learning(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(0.0)
    assert not capacity_fixture.observe(900.0, eligible=False)
    assert not capacity_fixture.record().observations
    assert not capacity_fixture.observe(1800.0)
    assert not capacity_fixture.observe(2700.0)
    assert capacity_fixture.observe(3600.0)


def test_epoch_change_discards_samples_from_previous_boot(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(0.0, epoch="first-boot")
    assert not capacity_fixture.observe(900.0, epoch="second-boot")
    assert not capacity_fixture.observe(1800.0, epoch="second-boot")
    assert capacity_fixture.observe(2700.0, epoch="second-boot")


def test_backwards_time_discards_pending_observations(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(900.0)
    assert not capacity_fixture.observe(1800.0)
    assert not capacity_fixture.observe(100.0)
    assert not capacity_fixture.observe(1000.0)
    assert capacity_fixture.observe(1900.0)


def test_long_gap_does_not_join_unrelated_low_observations(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(0.0)
    assert not capacity_fixture.observe(3600.0)
    assert not capacity_fixture.observe(4500.0)
    assert capacity_fixture.observe(5400.0)


@pytest.mark.parametrize("change", ["capacity", "reservation", "unit"])
def test_geometry_changes_restart_pending_learning(capacity_fixture: CapacityFixture, change: str) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(allocated=200 * GIB)
    assert not capacity_fixture.observe(0.0)
    match change:
        case "capacity":
            capacity_fixture.reading = volume(total=2000 * GIB, allocated=200 * GIB)
        case "reservation":
            capacity_fixture.reading = volume(allocated=200 * GIB, reserved=20 * GIB)
        case _:
            capacity_fixture.reading = volume(allocated=200 * GIB, unit=8192)
    assert not capacity_fixture.observe(900.0)
    assert not capacity_fixture.observe(1800.0)
    assert capacity_fixture.observe(2700.0)


def test_expansion_preserves_baseline_bytes_and_uses_current_reservations(capacity_fixture: CapacityFixture) -> None:
    original = cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    capacity_fixture.reading = volume(total=2000 * GIB, allocated=300 * GIB, reserved=20 * GIB)
    expanded = cleanup_capacity.policy(capacity_fixture.home)
    assert expanded.thresholds.mode == "calibrated"
    assert expanded.thresholds.baseline_allocated == original.thresholds.baseline_allocated
    assert expanded.thresholds.stop == 320 * GIB + expanded.thresholds.headroom
    assert expanded.thresholds.headroom > original.thresholds.headroom
    assert not capacity_fixture.observe(0.0, eligible=False)
    capacity_fixture.reading = volume(total=1500 * GIB, allocated=300 * GIB)
    assert cleanup_capacity.policy(capacity_fixture.home).thresholds.mode == "stale"


@pytest.mark.parametrize("change", ["identity", "shrink", "insufficient-headroom"])
def test_stale_state_is_explicit_and_never_overwritten(capacity_fixture: CapacityFixture, change: str) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    original = (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).read_bytes()
    match change:
        case "identity":
            capacity_fixture.reading = volume(key="b" * 64)
        case "shrink":
            capacity_fixture.reading = volume(total=900 * GIB)
        case _:
            capacity_fixture.reading = volume(allocated=100 * GIB, reserved=899 * GIB)
    result = cleanup_capacity.policy(capacity_fixture.home)
    assert result.thresholds.mode == "stale"
    assert result.thresholds.reason
    assert result.thresholds.enter == (result.volume.total + 3) // 4
    assert not capacity_fixture.observe(0.0)
    with pytest.raises(RuntimeError, match="calibration is stale"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    assert (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).read_bytes() == original


def test_explicit_recalibration_can_record_intentional_permanent_growth(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    original = capacity_fixture.record()
    capacity_fixture.reading = volume(allocated=400 * GIB)
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    record = capacity_fixture.record()
    assert record.allocated == 400 * GIB
    assert record.generation != original.generation
    assert record.revision == 0


def test_stale_volume_requires_explicit_post_cleanup_replacement(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    original = capacity_fixture.record()
    baseline_path = capacity_fixture.state / cleanup_capacity.BASELINE_FILE
    marker_path = capacity_fixture.state / cleanup_capacity.INITIALIZED_FILE
    original_payload = baseline_path.read_bytes()
    marker_identity = marker_path.stat().st_ino
    capacity_fixture.reading = volume(total=600 * GIB, allocated=40 * GIB, key="b" * 64)
    with pytest.raises(RuntimeError, match="calibration is stale"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    preview = cleanup_capacity.calibrate(capacity_fixture.home, replace=True)
    assert preview.thresholds.mode == "preview"
    assert preview.thresholds.baseline_allocated == 40 * GIB
    assert baseline_path.read_bytes() == original_payload
    replaced = cleanup_capacity.calibrate(capacity_fixture.home, apply=True, replace=True)
    current = capacity_fixture.record()
    assert replaced.thresholds.mode == "calibrated"
    assert replaced.thresholds.baseline_allocated == 40 * GIB
    assert current.key == "b" * 64
    assert current.key != original.key
    assert current.generation != original.generation
    assert current.revision == 0
    assert cleanup_capacity.policy(capacity_fixture.home) == replaced
    assert marker_path.read_bytes() == b"1\n"
    assert marker_path.stat().st_ino == marker_identity


@pytest.mark.parametrize("replace_existing", [False, True], ids=["normal-calibration", "explicit-replacement"])
def test_unsupported_identity_bootstraps_but_cannot_be_confirmed(
    capacity_fixture: CapacityFixture, replace_existing: bool
) -> None:
    capacity_fixture.reading = volume(key=None)
    assert cleanup_capacity.policy(capacity_fixture.home).thresholds.mode == "bootstrap"
    assert cleanup_capacity.calibrate(capacity_fixture.home).thresholds.mode == "bootstrap"
    with pytest.raises(RuntimeError, match="identity is unavailable"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True, replace=replace_existing)
    assert not (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).exists()


@pytest.mark.parametrize("missing", [cleanup_capacity.BASELINE_FILE, cleanup_capacity.INITIALIZED_FILE])
def test_incomplete_initialized_state_fails_closed(capacity_fixture: CapacityFixture, missing: str) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    (capacity_fixture.state / missing).unlink()
    with pytest.raises(RuntimeError, match="missing or incompletely initialized"):
        cleanup_capacity.policy(capacity_fixture.home)
    with pytest.raises(RuntimeError, match="missing or incompletely initialized"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True, replace=True)


@pytest.mark.parametrize("change", ["invalid-json", "unknown-version", "oversized", "missing-provenance"])
def test_corrupt_unknown_or_oversized_state_is_not_reset(capacity_fixture: CapacityFixture, change: str) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    path = capacity_fixture.state / cleanup_capacity.BASELINE_FILE
    original = path.read_text()
    match change:
        case "invalid-json":
            payload = "not-json"
        case "unknown-version":
            payload = original.replace('"version":1', '"version":2')
        case "missing-provenance":
            payload = original.replace('"provenance":"explicit-post-cleanup",', "")
        case _:
            payload = "x" * 8193
    assert payload != original
    private_write(path, payload)
    with pytest.raises((ValueError, RuntimeError), match=r"validation error|bounded"):
        cleanup_capacity.policy(capacity_fixture.home)
    with pytest.raises((ValueError, RuntimeError), match=r"validation error|bounded"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True, replace=True)
    assert path.read_text() == payload


def test_unknown_initialization_marker_does_not_reset_calibration(capacity_fixture: CapacityFixture) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    path = capacity_fixture.state / cleanup_capacity.INITIALIZED_FILE
    private_write(path, "2\n")
    with pytest.raises(RuntimeError, match="missing or incompletely initialized"):
        cleanup_capacity.policy(capacity_fixture.home)


@pytest.mark.parametrize("kind", ["permissions", "hardlink", "symlink"])
def test_nonprivate_or_aliased_state_is_rejected(capacity_fixture: CapacityFixture, kind: str) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    path = capacity_fixture.state / cleanup_capacity.BASELINE_FILE
    match kind:
        case "permissions":
            path.chmod(0o644)
        case "hardlink":
            (capacity_fixture.home / "baseline-hardlink").hardlink_to(path)
        case _:
            target = capacity_fixture.state / "retained-baseline"
            path.rename(target)
            path.symlink_to(target)
    with pytest.raises((OSError, RuntimeError), match=r"private|symbolic|links"):
        cleanup_capacity.policy(capacity_fixture.home)
    with pytest.raises((OSError, RuntimeError), match=r"private|symbolic|links"):
        cleanup_capacity.calibrate(capacity_fixture.home, apply=True, replace=True)


def test_state_directory_symlink_is_rejected(capacity_fixture: CapacityFixture) -> None:
    capacity_fixture.state.rmdir()
    target = capacity_fixture.home / "private-target"
    target.mkdir(mode=0o700)
    capacity_fixture.state.symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="owned private directory"):
        cleanup_capacity.policy(capacity_fixture.home)


def test_uninitialized_home_can_be_read_but_not_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fixture_volume(_home: Path) -> cleanup_capacity.VolumeReading:
        return volume()

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Native capacity is fixed while the test checks only private fixture initialization permissions.
        cleanup_capacity, "read_volume", fixture_volume
    )
    assert cleanup_capacity.policy(tmp_path).thresholds.mode == "bootstrap"
    with pytest.raises(RuntimeError, match="Initialize the private cleanup state"):
        cleanup_capacity.calibrate(tmp_path, apply=True)


def test_bootstrap_uses_exact_integer_boundaries_for_large_and_uneven_totals() -> None:
    uneven = cleanup_capacity.bootstrap_thresholds(101)
    assert uneven.enter == 26
    assert uneven.stop == 21
    total = 2**63 + 11
    large = cleanup_capacity.bootstrap_thresholds(total)
    assert large.enter * 4 >= total > (large.enter - 1) * 4
    assert large.stop * 5 >= total > (large.stop - 1) * 5


def test_small_volume_headroom_remains_system_relative(capacity_fixture: CapacityFixture) -> None:
    capacity_fixture.reading = volume(total=1024**2, allocated=0, reserved=0)
    preview = cleanup_capacity.calibrate(capacity_fixture.home)
    assert preview.thresholds.headroom <= preview.volume.total // 100 + preview.volume.unit
    assert preview.thresholds.enter < preview.volume.total


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1.0])
def test_invalid_observation_time_cannot_mutate_state(capacity_fixture: CapacityFixture, now: float) -> None:
    cleanup_capacity.calibrate(capacity_fixture.home, apply=True)
    original = (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).read_bytes()
    with pytest.raises(ValueError, match="invalid time or epoch"):
        capacity_fixture.observe(now)
    assert (capacity_fixture.state / cleanup_capacity.BASELINE_FILE).read_bytes() == original


def test_read_volume_uses_native_user_available_and_free_counters(tmp_path: Path) -> None:
    native = os.statvfs(tmp_path)
    reading = cleanup_capacity.read_volume(tmp_path)
    unit = native.f_frsize or native.f_bsize
    assert reading.total == native.f_blocks * unit
    assert reading.unit == unit
    assert reading.used == reading.allocated + reading.reserved
    assert 0 <= reading.available <= reading.free <= reading.total


def test_unknown_host_identity_disables_durable_calibration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def unknown_host() -> None:
        return None

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Simulate an unsupported native host identity without touching host files or persistent state.
        cleanup_capacity, "host_identity", unknown_host
    )
    assert cleanup_capacity.read_volume(tmp_path).key is None


def test_hostname_fallback_retains_platform_and_native_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    def unavailable_machine() -> None:
        return None

    def fixture_hostname() -> str:
        return "mac-fixture"

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Cover the native-hostname identity fallback on a platform without a machine-id file.
        cleanup_capacity, "machine_identity", unavailable_machine
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Supply the bounded native hostname without changing the actual VM hostname.
        cleanup_capacity, "gethostname", fixture_hostname
    )
    identity = cleanup_capacity.host_identity()
    assert identity is not None
    assert identity.endswith(":hostname:mac-fixture")


def test_volume_reading_rejects_inconsistent_native_counters() -> None:
    with pytest.raises(ValueError, match="capacity or identity is unknown"):
        replace(volume(), available=1000 * GIB)
