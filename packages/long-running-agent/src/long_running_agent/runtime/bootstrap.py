from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import fcntl
import os
from pathlib import Path
import shutil
import stat
import subprocess  # ruff: ignore[suspicious-subprocess-import] — first-boot worker is isolated system Python; commands are allowlisted.
import sys
import tempfile
from typing import TYPE_CHECKING, Final, Protocol


if TYPE_CHECKING:
    from collections.abc import Sequence

PACKAGES: Final = (
    "build-essential",
    "bubblewrap",
    "socat",
    "apparmor-profiles",
    "apparmor-utils",
    "git",
    "git-lfs",
    "curl",
    "ca-certificates",
    "unzip",
    "zip",
    "jq",
    "ripgrep",
    "fd-find",
    "fzf",
    "bat",
    "btop",
    "tree",
    "lsof",
    "dnsutils",
    "iputils-ping",
    "netcat-openbsd",
    "sqlite3",
    "pkg-config",
    "libssl-dev",
    "gnupg",
    "gh",
    "python3-venv",
    "unattended-upgrades",
    "needrestart",
    "update-manager-core",
    "locales",
    "docker.io",
    "docker-compose-v2",
    "dbus-user-session",
    "nftables",
    "sudo",
)
EXECUTABLES: Final = frozenset(
    {
        "/usr/bin/apt-get",
        "/usr/bin/curl",
        "/usr/bin/gpg",
        "/usr/bin/fallocate",
        "/usr/sbin/mkswap",
        "/usr/sbin/swapon",
        "/usr/sbin/sysctl",
        "/usr/sbin/locale-gen",
        "/usr/sbin/update-locale",
        "/usr/bin/systemctl",
        "/usr/sbin/sshd",
        "/usr/sbin/apparmor_parser",
    }
)
SSH_CONFIGURATION: Final = """PasswordAuthentication no
PermitRootLogin no
ClientAliveInterval 60
ClientAliveCountMax 3
"""
WATCHER_CONFIGURATION: Final = """fs.inotify.max_user_watches=524288
fs.inotify.max_user_instances=1024
"""
SWAP_CONFIGURATION: Final = "vm.swappiness=10\n"
SWAP_ENTRY: Final = "/swapfile.sarj none swap sw 0 0\n"
GCLOUD_REPOSITORY: Final = (
    "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main\n"
)
GITHUB_REPOSITORY: Final = (
    "deb [signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n"
)
APT: Final = ("/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=600")


@dataclass(frozen=True, slots=True)
class HostPaths:
    root: Path

    def path(self, absolute: str) -> Path:
        return self.root / absolute.removeprefix("/")

    @property
    def ready(self) -> Path:
        return self.path("/var/lib/sarj-codex/ready")

    @property
    def failed(self) -> Path:
        return self.path("/var/lib/sarj-codex/failed")


class CommandRunner(Protocol):
    def __call__(
        self, arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]: ...


def execute(
    arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    if not arguments or arguments[0] not in EXECUTABLES:
        message = "Unsupported first-boot executable"
        raise RuntimeError(message)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] — absolute executable allowlist and argument arrays, without a shell.
        arguments,
        check=True,
        text=True,
        capture_output=capture,
        input=input_text,
        env={
            "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
            "DEBIAN_FRONTEND": "noninteractive",
            "LANG": "C.UTF-8",
            "CLOUDSDK_SKIP_PY_COMPILATION": "1",
        },
    )


def write_configuration(paths: HostPaths, destination: str, content: str) -> None:
    path = paths.path(destination)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_text(content, encoding="utf-8")
    path.chmod(0o644)


def configure_swap(paths: HostPaths, runner: CommandRunner) -> None:
    swap = paths.path("/swapfile.sarj")
    if not swap.exists():
        descriptor, temporary = tempfile.mkstemp(prefix=".swapfile.sarj-", dir=paths.root)
        os.close(descriptor)
        candidate = Path(temporary)
        try:
            runner(["/usr/bin/fallocate", "-l", "4G", str(candidate)])
            runner(["/usr/sbin/mkswap", str(candidate)])
            candidate.replace(swap)
        finally:
            candidate.unlink(missing_ok=True)
    metadata = swap.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or metadata.st_mode & 0o077:
        message = "Swap must be a private root-owned regular file"
        raise RuntimeError(message)
    active = runner(["/usr/sbin/swapon", "--noheadings", "--show=NAME"], capture=True).stdout.splitlines()
    if str(swap) not in active:
        runner(["/usr/sbin/swapon", str(swap)])
    fstab = paths.path("/etc/fstab")
    original = fstab.read_text(encoding="utf-8")
    if not any(line.split()[:1] == ["/swapfile.sarj"] for line in original.splitlines()):
        with fstab.open("a", encoding="utf-8") as stream:
            stream.write(("" if not original or original.endswith("\n") else "\n") + SWAP_ENTRY)
    write_configuration(paths, "/etc/sysctl.d/70-sarj-swap.conf", SWAP_CONFIGURATION)
    runner(["/usr/sbin/sysctl", "-p", str(paths.path("/etc/sysctl.d/70-sarj-swap.conf"))])


def configure_repositories(paths: HostPaths, runner: CommandRunner) -> None:
    response = runner(
        [
            "/usr/bin/curl",
            "--retry",
            "3",
            "--connect-timeout",
            "15",
            "--max-time",
            "120",
            "-fsSL",
            "https://packages.cloud.google.com/apt/doc/apt-key.gpg",
        ],
        capture=True,
    )
    key_path = paths.path("/usr/share/keyrings/cloud.google.gpg")
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    runner(["/usr/bin/gpg", "--batch", "--yes", "--dearmor", "-o", str(key_path)], input_text=response.stdout)
    key_path.chmod(0o644)
    write_configuration(paths, "/etc/apt/sources.list.d/google-cloud-sdk.list", GCLOUD_REPOSITORY)
    configure_github_repository(paths, runner)


def configure_github_repository(paths: HostPaths, runner: CommandRunner) -> None:
    key = paths.path("/etc/apt/keyrings/githubcli-archive-keyring.gpg")
    key.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    key.parent.chmod(0o755)
    descriptor, temporary = tempfile.mkstemp(prefix=".github-cli-", dir=key.parent)
    os.close(descriptor)
    candidate = Path(temporary)
    try:
        runner(
            [
                "/usr/bin/curl",
                "--retry",
                "3",
                "--connect-timeout",
                "15",
                "--max-time",
                "120",
                "-fsSL",
                "-o",
                str(candidate),
                "https://cli.github.com/packages/githubcli-archive-keyring.gpg",
            ]
        )
        candidate.chmod(0o644)
        candidate.replace(key)
    finally:
        candidate.unlink(missing_ok=True)
    write_configuration(paths, "/etc/apt/sources.list.d/github-cli.list", GITHUB_REPOSITORY)


def configure_host(paths: HostPaths, runner: CommandRunner) -> None:
    profile = paths.path("/usr/share/apparmor/extra-profiles/bwrap-userns-restrict")
    destination = paths.path("/etc/apparmor.d/bwrap-userns-restrict")
    shutil.copyfile(profile, destination)
    destination.chmod(0o644)
    runner(["/usr/sbin/apparmor_parser", "-r", str(destination)])
    runner(["/usr/sbin/locale-gen", "en_US.UTF-8"])
    runner(["/usr/sbin/update-locale", "LANG=en_US.UTF-8"])
    runner(["/usr/bin/systemctl", "enable", "--now", "docker"])
    write_configuration(paths, "/etc/ssh/sshd_config.d/00-sarj-devbox.conf", SSH_CONFIGURATION)
    runner(["/usr/sbin/sshd", "-t"])
    runner(["/usr/bin/systemctl", "reload", "ssh"])
    write_configuration(paths, "/etc/sysctl.d/60-sarj-devbox.conf", WATCHER_CONFIGURATION)
    runner(["/usr/sbin/sysctl", "--system"])


def install_packages(paths: HostPaths, runner: CommandRunner) -> None:
    runner([*APT, "update"])
    runner([*APT, "install", "-y", *PACKAGES])
    configure_repositories(paths, runner)
    runner([*APT, "update"])
    runner([*APT, "install", "-y", "google-cloud-cli", "gh"])


def install(paths: HostPaths, runner: CommandRunner) -> None:
    configure_swap(paths, runner)
    install_packages(paths, runner)
    configure_host(paths, runner)


def bootstrap(paths: HostPaths, runner: CommandRunner) -> bool:
    paths.ready.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    lock = paths.path("/var/lock/sarj-codex-bootstrap.lock")
    lock.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    with lock.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        if paths.ready.is_file():
            return False
        paths.failed.unlink(missing_ok=True)
        try:
            install(paths, runner)
        except Exception:
            paths.failed.touch()
            raise
        paths.ready.touch()
    return True


def main() -> None:
    if os.geteuid() != 0:
        message = "First-boot host installation requires root"
        raise RuntimeError(message)
    os.umask(0o077)
    paths = HostPaths(Path("/"))
    if paths.ready.is_file():
        return
    log = paths.path("/var/log/sarj-codex-bootstrap.log")
    with log.open("a", encoding="utf-8") as stream:
        os.dup2(stream.fileno(), sys.stdout.fileno())
        os.dup2(stream.fileno(), sys.stderr.fileno())
        if bootstrap(paths, execute):
            sys.stdout.write(f"\nSarj base installation finished: {datetime.now(UTC).isoformat()}\n")


if __name__ == "__main__":
    main()
