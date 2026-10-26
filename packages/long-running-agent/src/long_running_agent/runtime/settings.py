from __future__ import annotations

from typing import TYPE_CHECKING, Final


if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Self

from dataclasses import dataclass
import os
from pathlib import Path
import shlex
import tempfile

from long_running_agent.process import environment_snapshot


CONCURRENCY_DEFAULTS: Final[dict[str, str]] = {
    "PYTEST_XDIST_AUTO_NUM_WORKERS": "2",
    "UV_CONCURRENT_BUILDS": "1",
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
}
ASCII_CONTROL_LIMIT: Final = 32


@dataclass(frozen=True)
class RuntimePaths:
    home: Path

    @classmethod
    def current(cls) -> Self:
        return cls(Path.home().resolve())

    @property
    def settings(self) -> Path:
        return self.home / ".config/sarj"

    @property
    def code(self) -> Path:
        return self.home / "code"


def read_settings(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.removeprefix("export ").partition("=")
        if not separator or not key.strip().replace("_", "").isalnum():
            msg = f"Invalid setting in {path}"
            raise ValueError(msg)
        words = shlex.split(value, comments=True)
        if len(words) > 1:
            msg = f"Settings must contain literal values: {path}"
            raise ValueError(msg)
        values[key.strip()] = words[0] if words else ""
    return values


def preferences(paths: RuntimePaths, environment: Mapping[str, str] | None = None) -> dict[str, str]:
    return {
        **read_settings(paths.settings / "context.env"),
        **read_settings(paths.settings / "agent.env"),
        **(environment_snapshot() if environment is None else environment),
    }


def private_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        Path(temporary).replace(path)
        # Commit the renamed receipt before its caller dispatches native input.
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def systemd_value(value: str) -> str:
    if any(ord(character) < ASCII_CONTROL_LIMIT for character in value):
        message = "Systemd values cannot contain control characters"
        raise ValueError(message)
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def user_environment(paths: RuntimePaths) -> dict[str, str]:
    environment = environment_snapshot()
    environment["PATH"] = ":".join(
        (
            str(paths.home / ".local/bin"),
            str(paths.home / ".local/share/mise/shims"),
            environment.get("PATH", "/usr/bin:/bin"),
        )
    )
    for key, value in CONCURRENCY_DEFAULTS.items():
        environment.setdefault(key, value)
    return environment
