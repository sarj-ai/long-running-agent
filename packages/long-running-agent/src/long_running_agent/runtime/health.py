from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Final, Protocol

import typer

from long_running_agent.process import CommandError, run


if TYPE_CHECKING:
    from collections.abc import Sequence

    from long_running_agent.process import Completed


GIB: Final = 1024**3
PERCENT_MAXIMUM: Final = 100
MEMINFO_FIELD_COUNT: Final = 3
CPU_CONTENTION_PERCENT: Final = 20
LOW_HEADROOM_RATIO: Final = 0.1


class ServiceRunner(Protocol):
    def __call__(self, arguments: Sequence[str], *, capture_output: bool, check: bool, timeout: float) -> Completed: ...


@dataclass(frozen=True)
class Pressure:
    some: float | None
    full: float | None


@dataclass(frozen=True)
class Memory:
    total: int
    available: int
    swap_total: int
    swap_used: int


@dataclass(frozen=True)
class Health:
    cpus: int
    load: str
    cpu: Pressure
    memory: Memory | None
    memory_pressure: Pressure
    io: Pressure
    disk_total: int
    disk_free: int
    failed_units: tuple[str, ...] | None


def kernel_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def pressure(path: Path) -> Pressure:
    values: dict[str, float] = {}
    for line in kernel_text(path).splitlines():
        fields = line.split()
        if not fields or fields[0] not in {"some", "full"}:
            continue
        for field in fields[1:]:
            key, _, value = field.partition("=")
            if key == "avg10":
                try:
                    amount = float(value)
                except ValueError:
                    continue
                if math.isfinite(amount) and 0 <= amount <= PERCENT_MAXIMUM:
                    values[fields[0]] = amount
    return Pressure(values.get("some"), values.get("full"))


def memory_capacity(path: Path) -> Memory | None:
    values: dict[str, int] = {}
    for line in kernel_text(path).splitlines():
        fields = line.split()
        if len(fields) == MEMINFO_FIELD_COUNT and fields[2] == "kB" and fields[1].isdigit():
            values[fields[0].rstrip(":")] = int(fields[1]) * 1024
    try:
        total, available = values["MemTotal"], values["MemAvailable"]
        swap_total, swap_free = values["SwapTotal"], values["SwapFree"]
    except KeyError:
        return None
    if total <= 0 or not 0 <= available <= total or not 0 <= swap_free <= swap_total:
        return None
    return Memory(total, available, swap_total, swap_total - swap_free)


def failed_units(runner: ServiceRunner) -> tuple[str, ...] | None:
    try:
        result = runner(
            ["systemctl", "--system", "--failed", "--plain", "--no-legend", "--no-pager"],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except OSError, CommandError:
        return None
    if result.returncode:
        return None
    return tuple(fields[0] for line in result.stdout.splitlines() if (fields := line.split()))


def collect_health(proc: Path, home: Path, runner: ServiceRunner) -> Health:
    if not (proc / "stat").is_file():
        message = "Health reads Linux kernel metrics; run vm health to inspect the VM"
        raise RuntimeError(message)
    cpus = sum(
        line.partition(" ")[0].removeprefix("cpu").isdigit()
        for line in kernel_text(proc / "stat").splitlines()
        if line.startswith("cpu")
    )
    load = " / ".join(kernel_text(proc / "loadavg").split()[:3]) or "unavailable"
    disk = shutil.disk_usage(home)
    return Health(
        cpus,
        load,
        pressure(proc / "pressure/cpu"),
        memory_capacity(proc / "meminfo"),
        pressure(proc / "pressure/memory"),
        pressure(proc / "pressure/io"),
        disk.total,
        disk.free,
        failed_units(runner),
    )


def percent(value: float | None) -> str:
    return "unavailable" if value is None else f"{value:.1f}%"


def health_lines(value: Health) -> list[str]:
    lines = [
        f"CPU: {value.cpus} vCPU | load 1/5/15m: {value.load}",
        (
            f"Pressure, last 10s: CPU waiting {percent(value.cpu.some)} | "
            f"memory some/full {percent(value.memory_pressure.some)}/{percent(value.memory_pressure.full)} | "
            f"I/O some/full {percent(value.io.some)}/{percent(value.io.full)}"
        ),
    ]
    memory = value.memory
    units = value.failed_units
    lines.extend(
        (
            (
                f"RAM: {memory.available / GIB:.1f} / {memory.total / GIB:.1f} GiB available | "
                f"swap used {memory.swap_used / GIB:.1f} / {memory.swap_total / GIB:.1f} GiB"
                if memory is not None
                else "RAM: unavailable"
            ),
            f"Disk, home filesystem: {value.disk_free / GIB:.1f} / {value.disk_total / GIB:.1f} GiB free",
            (
                "Failed system units: unavailable"
                if units is None
                else f"Failed system units: {len(units)}" + (" (" + ", ".join(units[:5]) + ")" if units else "")
            ),
        )
    )
    if value.cpu.some is not None and value.cpu.some >= CPU_CONTENTION_PERCENT:
        lines.append("CPU contention: reduce simultaneous builds or agent tasks if interaction feels slow.")
    if memory is not None and memory.available / memory.total < LOW_HEADROOM_RATIO:
        lines.append("Low RAM headroom: finish or pause memory-heavy tasks before starting more.")
    if value.disk_total and value.disk_free / value.disk_total < LOW_HEADROOM_RATIO:
        lines.append("Low disk headroom: inspect usage before installing tools or starting large builds.")
    if units:
        lines.append("Inspect failures with: systemctl --failed")
    return lines


def health() -> None:
    for line in health_lines(collect_health(Path("/proc"), Path.home(), run)):
        typer.echo(line)
