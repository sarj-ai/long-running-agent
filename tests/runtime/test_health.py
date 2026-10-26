from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from long_running_agent.process import run
from long_running_agent.runtime.health import GIB, Health, Memory, Pressure, collect_health, health_lines, pressure
import pytest


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path


@pytest.mark.parametrize(
    ("service_code", "service_text", "expected_units"),
    [
        (0, "", ()),
        (0, "fixture.service loaded failed failed Fixture\n", ("fixture.service",)),
        (1, "", None),
    ],
)
def test_health_reads_kernel_metrics_without_network_or_auth(
    tmp_path: Path, service_code: int, service_text: str, expected_units: tuple[str, ...] | None
) -> None:
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "stat").write_text("cpu 1 2 3\ncpu0 1 2\ncpu1 1 2\nintr 1 2\n", encoding="utf-8")
    (proc / "loadavg").write_text("4.10 3.20 2.30 1/200 999\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal: 33554432 kB\nMemAvailable: 26214400 kB\nSwapTotal: 4194304 kB\nSwapFree: 4194304 kB\n",
        encoding="utf-8",
    )
    (proc / "pressure/cpu").write_text("some avg10=38.25 avg60=20.00\nfull avg10=0.00\n", encoding="utf-8")
    for resource in ("memory", "io"):
        (proc / "pressure" / resource).write_text("some avg10=0.00\nfull avg10=0.00\n", encoding="utf-8")
    commands: list[list[str]] = []

    def runner(
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        input_text: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        assert capture_output
        assert not check
        assert timeout == 5
        assert input_text is None
        assert cwd is None
        assert env is None
        commands.append(list(arguments))
        return subprocess.CompletedProcess(arguments, service_code, service_text, "")

    value = collect_health(proc, tmp_path, runner)
    assert value.cpus == 2
    assert value.memory == Memory(32 * GIB, 25 * GIB, 4 * GIB, 0)
    assert value.cpu.some == pytest.approx(38.25)
    assert value.failed_units == expected_units
    output = "\n".join(health_lines(value))
    assert "load 1/5/15m: 4.10 / 3.20 / 2.30" in output
    assert "CPU waiting 38.2%" in output
    assert "25.0 / 32.0 GiB available" in output
    assert "CPU contention" in output
    assert "Low RAM" not in output
    assert commands == [["systemctl", "--system", "--failed", "--plain", "--no-legend", "--no-pager"]]


@pytest.mark.parametrize("value", ["nan", "inf", "-1", "101", "not-a-number"])
def test_invalid_pressure_never_looks_like_zero(tmp_path: Path, value: str) -> None:
    source = tmp_path / "pressure"
    source.write_text(f"some avg10={value}\n", encoding="utf-8")
    assert pressure(source) == Pressure(None, None)


def test_unavailable_metrics_are_explicit_and_resource_headroom_is_actionable() -> None:
    unavailable = Pressure(None, None)
    value = Health(
        8,
        "14 / 8 / 4",
        unavailable,
        Memory(32 * GIB, GIB, 4 * GIB, GIB),
        unavailable,
        unavailable,
        200 * GIB,
        GIB,
        ("example.service",),
    )
    output = "\n".join(health_lines(value))
    assert "CPU waiting unavailable" in output
    assert "Low RAM headroom" in output
    assert "Low disk headroom" in output
    assert "Failed system units: 1 (example.service)" in output
    assert "systemctl --failed" in output
    assert "CPU contention" not in output


def test_non_linux_health_has_clear_error_before_running_commands(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="vm health"):
        collect_health(tmp_path / "missing", tmp_path, run)
