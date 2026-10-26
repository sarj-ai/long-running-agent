from __future__ import annotations

import hashlib
from pathlib import Path
import platform
import shutil
import tempfile
from typing import TYPE_CHECKING, Final, Protocol

from long_running_agent.process import Completed, run
from long_running_agent.runtime.settings import RuntimePaths, user_environment


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class NativeRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
    ) -> Completed: ...


VERSION: Final = "0.45.1"
ARCHIVE_NAME: Final = "zellij-x86_64-unknown-linux-musl.tar.gz"
ARCHIVE_URL: Final = f"https://github.com/zellij-org/zellij/releases/download/v{VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256: Final = "40bcc2e03f5d5ae8e054e39f676081fe12ab70871506996ba595834c3718eefc"


def install_archive(
    archive: Path, destination: Path, *, expected_sha256: str = ARCHIVE_SHA256, runner: NativeRunner = run
) -> None:
    with archive.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != expected_sha256:
        message = "The pinned Zellij download failed its SHA256 verification"
        raise RuntimeError(message)
    with tempfile.TemporaryDirectory(prefix=".zellij-", dir=destination.parent) as temporary:
        staging = Path(temporary)
        runner(["tar", "-xzf", str(archive), "-C", str(staging), "zellij"], capture_output=True, timeout=60)
        executable = staging / "zellij"
        executable.chmod(0o700)
        version = runner([str(executable), "--version"], capture_output=True, timeout=15)
        if version.stdout.strip() != f"zellij {VERSION}":
            message = "The downloaded Zellij version does not match the pinned release"
            raise RuntimeError(message)
        # Publish without replacing a binary installed by a concurrent setup.
        destination.hardlink_to(executable)


def install_zellij(paths: RuntimePaths, *, runner: NativeRunner = run) -> None:
    if shutil.which("zellij", path=user_environment(paths)["PATH"]):
        return
    destination = paths.home / ".local/bin/zellij"
    if destination.exists() or destination.is_symlink():
        message = "An existing Zellij launcher was preserved; make it executable before using terminal mode"
        raise RuntimeError(message)
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        message = "The pinned Zellij host runtime requires x86-64 Linux"
        raise RuntimeError(message)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".zellij-download-", dir=destination.parent) as temporary:
        archive = Path(temporary) / ARCHIVE_NAME
        runner(
            [
                "curl",
                "--retry",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                "180",
                "-fsSL",
                ARCHIVE_URL,
                "-o",
                str(archive),
            ],
            capture_output=True,
            timeout=190,
        )
        install_archive(archive, destination, runner=runner)
