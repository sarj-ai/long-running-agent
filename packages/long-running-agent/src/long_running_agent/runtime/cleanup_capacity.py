from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import pairwise
import json
import math
import os
import re
from socket import gethostname
import stat
import sys
import time
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from uuid import UUID, uuid7

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .cleanup_state import Timestamp, private_payload
from .settings import private_write


if TYPE_CHECKING:
    from pathlib import Path


BASELINE_FILE: Final = "cleanup-capacity.json"
INITIALIZED_FILE: Final = "cleanup-capacity-initialized"
STATE_LIMIT: Final = 8192
SAMPLE_SECONDS: Final = 15 * 60
MAX_SAMPLE_GAP: Final = 30 * 60
MAX_OBSERVATIONS: Final = 3
HOSTNAME_LIMIT: Final = 255
EPOCH_LIMIT: Final = 256
MIB: Final = 1024**2
GIB: Final = 1024**3
KEY_PATTERN: Final = r"^[0-9a-f]{64}$"


@dataclass(frozen=True)
class VolumeReading:
    total: int
    available: int
    free: int
    unit: int
    key: str | None

    def __post_init__(self) -> None:
        message = "Filesystem capacity or identity is unknown; retained"
        invalid_counters = (
            any(type(value) is not int for value in (self.total, self.available, self.free, self.unit))
            or self.unit <= 0
        )
        if invalid_counters:
            raise ValueError(message)
        invalid_geometry = not 0 <= self.available <= self.free <= self.total or self.total <= 0
        if invalid_geometry:
            raise ValueError(message)
        invalid_alignment = any(value % self.unit for value in (self.total, self.available, self.free))
        if invalid_alignment:
            raise ValueError(message)
        invalid_identity = self.key is not None and re.fullmatch(KEY_PATTERN, self.key) is None
        if invalid_identity:
            raise ValueError(message)

    @property
    def used(self) -> int:
        return self.total - self.available

    @property
    def allocated(self) -> int:
        return self.total - self.free

    @property
    def reserved(self) -> int:
        return self.free - self.available


@dataclass(frozen=True)
class Thresholds:
    total: int
    enter: int
    stop: int
    mode: str
    baseline_allocated: int | None
    headroom: int
    reason: str | None


@dataclass(frozen=True)
class Policy:
    volume: VolumeReading
    thresholds: Thresholds


class LowerObservation(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    at: Timestamp
    allocated: Annotated[int, Field(ge=0)]


class Calibration(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    version: Literal[1]
    provenance: Literal["explicit-post-cleanup"]
    key: Annotated[str, Field(pattern=KEY_PATTERN)]
    generation: UUID
    revision: Annotated[int, Field(ge=0, le=2**63 - 1)]
    calibrated_at: Timestamp
    allocated: Annotated[int, Field(ge=0)]
    total: Annotated[int, Field(gt=0)]
    reserved: Annotated[int, Field(ge=0)]
    unit: Annotated[int, Field(gt=0)]
    epoch: Annotated[str, Field(min_length=1, max_length=EPOCH_LIMIT)] | None = None
    observed_at: Timestamp | None = None
    observations: Annotated[tuple[LowerObservation, ...], Field(max_length=MAX_OBSERVATIONS)] = ()

    @model_validator(mode="after")
    def consistent_geometry(self) -> Self:
        message = "The cleanup capacity calibration has inconsistent observations; retained"
        invalid_capacity = self.allocated + self.reserved > self.total or self.unit > self.total
        if invalid_capacity:
            raise ValueError(message)
        invalid_alignment = self.total % self.unit or self.reserved % self.unit
        if invalid_alignment:
            raise ValueError(message)
        missing_context = bool(self.observations) and (self.epoch is None or self.observed_at is None)
        if missing_context:
            raise ValueError(message)
        invalid_allocations = any(sample.allocated >= self.allocated for sample in self.observations)
        if invalid_allocations:
            raise ValueError(message)
        invalid_intervals = any(
            not SAMPLE_SECONDS <= later.at - earlier.at <= MAX_SAMPLE_GAP
            for earlier, later in pairwise(self.observations)
        )
        if invalid_intervals:
            raise ValueError(message)
        observed_at = self.observed_at
        future_observations = observed_at is not None and any(sample.at > observed_at for sample in self.observations)
        if future_observations:
            raise ValueError(message)
        return self


def machine_identity() -> str | None:
    try:
        descriptor = os.open("/etc/machine-id", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                return None
            payload = stream.read(129)
    except OSError:
        return None
    if re.fullmatch(rb"[0-9a-fA-F]{32}\n?", payload) is None:
        return None
    identity = payload.decode("ascii").strip().lower()
    return identity if identity != "0" * 32 else None


def host_identity() -> str | None:
    machine = machine_identity()
    if machine is not None:
        return f"{sys.platform}:machine:{machine}"
    try:
        hostname = gethostname()
    except OSError:
        return None
    if not hostname or len(hostname) > HOSTNAME_LIMIT or hostname != hostname.strip() or "\x00" in hostname:
        return None
    return f"{sys.platform}:hostname:{hostname}"


def read_volume(home: Path) -> VolumeReading:
    # Native fsids are not guaranteed persistent UUIDs. A changed fsid or fallback
    # hostname invalidates calibration, including across reboot.
    resolved = home.resolve(strict=True)
    values = os.statvfs(resolved)
    unit = values.f_frsize or values.f_bsize
    total = values.f_blocks * unit
    available = values.f_bavail * unit
    free = values.f_bfree * unit
    host = host_identity()
    filesystem: object = getattr(values, "f_fsid", None)
    key = None
    if host is not None and type(filesystem) is int and filesystem != 0:
        binding = json.dumps((host, os.getuid(), str(resolved), filesystem), ensure_ascii=True)
        key = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return VolumeReading(total=total, available=available, free=free, unit=unit, key=key)


def bootstrap_thresholds(total: int) -> Thresholds:
    if type(total) is not int or total <= 0:
        message = "Filesystem capacity is unknown; retained"
        raise ValueError(message)
    # Charged usage includes current reservations, exactly as total - bavail does.
    return Thresholds(total, (total + 3) // 4, (total + 4) // 5, "bootstrap", None, 0, "no confirmed clean baseline")


def headroom_bytes(volume: VolumeReading) -> int:
    desired = min((volume.total + 99) // 100, 2 * GIB, max(256 * MIB, (volume.total + 999) // 1000))
    return max(volume.unit, ((desired + volume.unit - 1) // volume.unit) * volume.unit)


def calibrated_thresholds(volume: VolumeReading, allocated: int, *, mode: str = "calibrated") -> Thresholds:
    headroom = headroom_bytes(volume)
    stop = allocated + volume.reserved + headroom
    enter = stop + headroom
    if allocated < 0 or not 0 <= stop < enter < volume.total:
        message = "The clean footprint leaves insufficient filesystem headroom; retained"
        raise ValueError(message)
    return Thresholds(volume.total, enter, stop, mode, allocated, headroom, None)


def state_directory(home: Path, *, required: bool = False) -> Path | None:
    resolved = home.resolve(strict=True)
    state = resolved / ".local/state/long-running-agent"
    for component in (resolved, resolved / ".local", resolved / ".local/state", state):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            if not required:
                return None
            message = "Initialize the private cleanup state directory before calibration"
            raise RuntimeError(message) from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
            or (component == state and metadata.st_mode & 0o077)
        ):
            message = "Cleanup capacity state must be in an owned private directory; retained"
            raise RuntimeError(message)
    return state


def load_calibration(home: Path) -> Calibration | None:
    state = state_directory(home)
    if state is None:
        return None
    try:
        marker = private_payload(state / INITIALIZED_FILE, 16)
    except FileNotFoundError:
        marker = None
    try:
        payload = private_payload(state / BASELINE_FILE, STATE_LIMIT)
    except FileNotFoundError:
        payload = None
    if marker is None and payload is None:
        return None
    if marker != b"1\n" or payload is None:
        message = "Cleanup capacity calibration is missing or incompletely initialized; retained"
        raise RuntimeError(message)
    return Calibration.model_validate_json(payload)


def stale_reason(volume: VolumeReading, calibration: Calibration) -> str | None:
    if volume.key is None:
        return "filesystem identity is unavailable"
    if volume.key != calibration.key:
        return "host, user, home, or filesystem identity changed"
    if volume.total < calibration.total:
        return "filesystem capacity shrank since calibration was observed"
    try:
        calibrated_thresholds(volume, calibration.allocated)
    except ValueError:
        return "the confirmed baseline no longer leaves filesystem headroom"
    return None


def policy(home: Path) -> Policy:
    volume = read_volume(home)
    calibration = load_calibration(home)
    bootstrap = bootstrap_thresholds(volume.total)
    if calibration is None:
        reason = "filesystem identity is unavailable" if volume.key is None else bootstrap.reason
        return Policy(volume, Thresholds(volume.total, bootstrap.enter, bootstrap.stop, "bootstrap", None, 0, reason))
    reason = stale_reason(volume, calibration)
    if reason is not None:
        return Policy(volume, Thresholds(volume.total, bootstrap.enter, bootstrap.stop, "stale", None, 0, reason))
    return Policy(volume, calibrated_thresholds(volume, calibration.allocated))


def save_calibration(state: Path, calibration: Calibration) -> None:
    payload = calibration.model_dump_json() + "\n"
    if len(payload.encode("utf-8")) > STATE_LIMIT:
        message = "Cleanup capacity calibration exceeds its private state limit; retained"
        raise RuntimeError(message)
    private_write(state / BASELINE_FILE, payload)


def calibrate(home: Path, *, apply: bool = False, replace: bool = False) -> Policy:
    # Writes require the caller's cleanup flock and explicit post-cleanup attestation.
    volume = read_volume(home)
    previous = load_calibration(home)
    if volume.key is None:
        if apply:
            message = "Filesystem identity is unavailable; calibration was not written"
            raise RuntimeError(message)
        bootstrap = bootstrap_thresholds(volume.total)
        return Policy(
            volume,
            Thresholds(
                volume.total,
                bootstrap.enter,
                bootstrap.stop,
                "bootstrap",
                None,
                0,
                "filesystem identity is unavailable",
            ),
        )
    reason = stale_reason(volume, previous) if previous is not None else None
    if apply and reason is not None and not replace:
        message = "Existing cleanup capacity calibration is stale; retained without replacement"
        raise RuntimeError(message)
    thresholds = calibrated_thresholds(volume, volume.allocated, mode="calibrated" if apply else "preview")
    if not apply:
        return Policy(volume, thresholds)
    state = state_directory(home, required=True)
    if state is None:
        message = "The private cleanup state directory is unavailable"
        raise RuntimeError(message)
    calibration = Calibration(
        version=1,
        provenance="explicit-post-cleanup",
        key=volume.key,
        generation=uuid7(),
        revision=0,
        calibrated_at=time.time(),
        allocated=volume.allocated,
        total=volume.total,
        reserved=volume.reserved,
        unit=volume.unit,
    )
    save_calibration(state, calibration)
    if previous is None:
        private_write(state / INITIALIZED_FILE, "1\n")
    return Policy(volume, thresholds)


def lower_observations(
    volume: VolumeReading,
    previous: Calibration,
    *,
    now: float,
    epoch: str,
    eligible: bool,
) -> tuple[LowerObservation, ...]:
    changed = (
        previous.epoch != epoch
        or previous.total != volume.total
        or previous.reserved != volume.reserved
        or previous.unit != volume.unit
        or (previous.observed_at is not None and now < previous.observed_at)
    )
    # Imported state may already contain three samples. Require a fresh spaced
    # observation and retain at most the two predecessors needed for that window.
    samples = () if changed else previous.observations[-(MAX_OBSERVATIONS - 1) :]
    headroom = headroom_bytes(volume)
    delta = max(volume.unit, headroom // 2)
    spread = max(volume.unit, headroom // 4)
    if not eligible or volume.allocated > previous.allocated - delta:
        return ()
    current = LowerObservation(at=now, allocated=volume.allocated)
    if not samples or any(sample.allocated > previous.allocated - delta for sample in samples):
        return (current,)
    values = (volume.allocated, *(sample.allocated for sample in samples))
    if now - samples[-1].at > MAX_SAMPLE_GAP or max(values) - min(values) > spread:
        return (current,)
    if now - samples[-1].at < SAMPLE_SECONDS:
        return samples
    return (*samples, current)


def observe_lower(home: Path, *, now: float, epoch: str, eligible: bool) -> bool:
    # The caller holds the native cleanup flock.
    if not math.isfinite(now) or now < 0 or not 1 <= len(epoch) <= EPOCH_LIMIT:
        message = "Cleanup capacity observation has an invalid time or epoch; retained"
        raise ValueError(message)
    volume = read_volume(home)
    previous = load_calibration(home)
    if previous is None or stale_reason(volume, previous) is not None:
        return False
    samples = lower_observations(volume, previous, now=now, epoch=epoch, eligible=eligible)
    lowered = len(samples) == MAX_OBSERVATIONS and samples[-1].at - samples[0].at >= 2 * SAMPLE_SECONDS
    allocated = max(sample.allocated for sample in samples) if lowered else previous.allocated
    calibration = Calibration(
        version=1,
        provenance="explicit-post-cleanup",
        key=previous.key,
        generation=previous.generation,
        revision=previous.revision + 1,
        calibrated_at=previous.calibrated_at,
        allocated=allocated,
        total=volume.total,
        reserved=volume.reserved,
        unit=volume.unit,
        epoch=epoch,
        observed_at=now,
        observations=() if lowered else samples,
    )
    state = state_directory(home, required=True)
    if state is None:
        message = "The private cleanup state directory is unavailable"
        raise RuntimeError(message)
    save_calibration(state, calibration)
    return lowered
