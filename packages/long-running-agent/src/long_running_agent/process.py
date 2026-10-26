from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] — This module is the application's explicit subprocess boundary.
from typing import TYPE_CHECKING, NoReturn


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import IO

type ChildProcess = subprocess.Popen[bytes]
type Completed = subprocess.CompletedProcess[str]
ProcessTimeout = subprocess.TimeoutExpired
DEVNULL = subprocess.DEVNULL
PIPE = subprocess.PIPE


class CommandError(RuntimeError):
    def __init__(self, program: str, returncode: int) -> None:
        super().__init__(f"{Path(program).name} exited with status {returncode}")
        self.program = program
        self.returncode = returncode


def environment_snapshot() -> dict[str, str]:
    return dict(os.environ)  # ruff: ignore[banned-api] — Preserve the complete child environment, including locale and SDK settings.


def executable_arguments(arguments: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    if not arguments:
        message = "A command must name an executable"
        raise ValueError(message)
    search_path = None if env is None else env.get("PATH")
    executable = shutil.which(arguments[0], path=search_path)
    if executable is None:
        message = f"Required executable is unavailable: {arguments[0]}"
        raise FileNotFoundError(message)
    return [executable, *arguments[1:]]


def run(
    arguments: Sequence[str],
    *,
    capture_output: bool = False,
    check: bool = True,
    timeout: float | None = None,
    input_text: str | None = None,
    cwd: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    resolved = executable_arguments(arguments, env)
    try:
        result = subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] — Resolve the executable; pass an argument vector with shell disabled.
            resolved,
            capture_output=capture_output,
            check=False,
            text=True,
            timeout=timeout,
            input=input_text,
            cwd=cwd,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise CommandError(arguments[0], 124) from None
    if check and result.returncode:
        raise CommandError(arguments[0], result.returncode)
    return result


def exec_command(arguments: Sequence[str], *, env: Mapping[str, str] | None = None) -> NoReturn:
    resolved = executable_arguments(arguments, env)
    os.execve(resolved[0], resolved, environment_snapshot() if env is None else dict(env))  # ruff: ignore[start-process-with-no-shell] — Replace this process using a resolved executable and argument vector.


def spawn(
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    stdin: int | IO[bytes] | None = None,
    stdout: int | IO[bytes] | None = None,
    stderr: int | IO[bytes] | None = None,
    start_new_session: bool = False,
) -> subprocess.Popen[bytes]:
    return subprocess.Popen(  # ruff: ignore[subprocess-without-shell-equals-true] — Resolve the executable; pass an argument vector with shell disabled.
        executable_arguments(arguments, env),
        env=env,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        start_new_session=start_new_session,
    )
