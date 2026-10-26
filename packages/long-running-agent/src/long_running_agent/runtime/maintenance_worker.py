from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Sequence

from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
import fcntl
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import] -- This isolated root-owned stdlib worker cannot import per-user package code.
import sys
import tempfile
from typing import Final


APT_POLICY: Final[str] = """// Daily security patches, with reboots reserved for maintenance windows.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Automatic-Reboot-WithUsers "false";
"""
RESTART_POLICY: Final[str] = """# Keep interactive Codex, SSH, browser, and build sessions alive.
# Patched libraries may remain in memory until the next maintenance window.
# Package maintainer scripts can still restart their own services.
$nrconf{restart} = 'l';
"""
POLICIES: Final[dict[str, str]] = {
    "/etc/apt/apt.conf.d/99-sarj-maintenance": APT_POLICY,
    "/etc/needrestart/conf.d/99-sarj-sessions.conf": RESTART_POLICY,
}
ENV: Final[dict[str, str]] = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "HOME": "/root",
    "LC_ALL": "C",
    "NEEDRESTART_MODE": "l",
}


def emit(text: str, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(text + "\n")
    stream.flush()


def execute(
    args: Sequence[str],
    *,
    environment: dict[str, str] | None = None,
    captured: bool = False,
    timeout: float | None = 60,
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which(args[0], path=ENV["PATH"])
    if executable is None:
        message = f"Required system command is missing: {args[0]}"
        raise FileNotFoundError(message)
    return subprocess.run(  # ruff: ignore[subprocess-without-shell-equals-true] -- Root-owned standalone boundary resolves a fixed system PATH and never invokes a shell.
        [executable, *args[1:]],
        text=True,
        capture_output=captured,
        env=ENV if environment is None else environment,
        timeout=timeout,
        check=False,
    )


def capture(args: Sequence[str], timeout: float = 60) -> str:
    result = execute(args, captured=True, timeout=timeout)
    if result.returncode:
        message = f"System command failed: {Path(args[0]).name} ({result.returncode})"
        raise RuntimeError(message)
    return result.stdout.strip()


def read_optional(path: str | Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""


@dataclass(frozen=True)
class AuditReport:
    checked_at_utc: str
    os: str
    version_id: str
    kernel: str
    package_versions: list[str]
    apt_index_last_success_utc: str | None
    upgrade_summary: str
    pending_package_changes: list[str]
    held_packages: list[str]
    dpkg_audit: str
    reboot_required: bool
    reboot_required_packages: list[str]
    apt_periodic_and_reboot_policy: list[str]
    needrestart_list_only_policy_installed: bool
    policy_files_match: dict[str, bool]
    offered_release: str | None = None


def audit(*, check_release: bool = False) -> AuditReport:
    release: dict[str, str] = {}
    for line in read_optional("/etc/os-release").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            release[key] = value.strip('"')
    simulation = capture(["apt-get", "--simulate", "full-upgrade"])
    summary = next(
        (line for line in simulation.splitlines() if re.match(r"^\d+ upgraded,", line)),
        "",
    )
    changed = [line for line in simulation.splitlines() if line.startswith(("Inst ", "Remv ", "Conf "))]
    stamp = Path("/var/lib/apt/periodic/update-success-stamp")
    restart_policy = read_optional("/etc/needrestart/conf.d/99-sarj-sessions.conf")
    report = AuditReport(
        checked_at_utc=datetime.now(UTC).isoformat(),
        os=release.get("PRETTY_NAME", ""),
        version_id=release.get("VERSION_ID", ""),
        kernel=platform.release(),
        package_versions=capture(
            [
                "dpkg-query",
                "-W",
                "-f=${Package} ${Version}\n",
                "base-files",
                "linux-image-gcp",
                "google-guest-agent",
                "google-compute-engine-oslogin",
                "google-osconfig-agent",
                "google-cloud-cli",
                "apparmor",
                "bubblewrap",
                "unattended-upgrades",
                "needrestart",
            ]
        ).splitlines(),
        apt_index_last_success_utc=(
            datetime.fromtimestamp(stamp.stat().st_mtime, UTC).isoformat() if stamp.exists() else None
        ),
        upgrade_summary=summary,
        pending_package_changes=changed,
        held_packages=capture(["apt-mark", "showhold"]).splitlines(),
        dpkg_audit=capture(["dpkg", "--audit"]),
        reboot_required=Path("/run/reboot-required").exists(),
        reboot_required_packages=read_optional("/run/reboot-required.pkgs").splitlines(),
        apt_periodic_and_reboot_policy=[
            line
            for line in capture(["apt-config", "dump"]).splitlines()
            if line.startswith(("APT::Periodic::", "Unattended-Upgrade::Automatic-Reboot"))
        ],
        needrestart_list_only_policy_installed="$nrconf{restart} = 'l';" in restart_policy,
        policy_files_match={name: read_optional(name) == text.strip() for name, text in POLICIES.items()},
    )
    if check_release:
        return replace(report, offered_release=capture(["do-release-upgrade", "--check-dist-upgrade-only"]))
    return report


def require_root() -> None:
    if os.geteuid() != 0:
        msg = "This action needs root. Use sudo only after reviewing the action."
        raise RuntimeError(msg)


def install_policy() -> None:
    require_root()
    changed: list[str] = []
    for name, text in POLICIES.items():
        target = Path(name)
        if read_optional(name) == text.strip():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".sarj-", dir=target.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(text)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o644)
            Path(temporary).replace(target)
        finally:
            Path(temporary).unlink(missing_ok=True)
        changed.append(name)
    # Parse the configuration without invoking unattended-upgrades or restarting anything.
    capture(["apt-config", "dump"])
    emit(json.dumps({"policy_files_changed": changed, "services_restarted": False, "reboot_requested": False}))


def patch_packages(*, maintenance_window: bool) -> None:
    if not maintenance_window:
        msg = (
            "Package upgrades can interrupt tasks. Wait until work is saved, "
            "then pass --maintenance-window. No changes made."
        )
        raise RuntimeError(msg)
    require_root()
    with Path("/var/lock/sarj-maintenance.lock").open("w", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        install_policy()
        env = {**ENV, "DEBIAN_FRONTEND": "noninteractive"}
        common = ["apt-get", "-o", "DPkg::Lock::Timeout=600"]
        capture([*common, "update"], timeout=600)
        # Do not silently remove packages or accept an in-place distro transition.
        # Preserve phased rollouts for routine maintenance.
        result = execute([*common, "-y", "--no-remove", "full-upgrade"], environment=env, timeout=None)
        if result.returncode:
            message = "Package maintenance failed; inspect apt before retrying"
            raise RuntimeError(message)
        emit(json.dumps(asdict(audit()), indent=2))
        emit("No reboot was requested. Review service restarts and schedule any required reboot.")


def upgrade_release(*, maintenance_window: bool) -> None:
    if not maintenance_window:
        msg = (
            "A release upgrade interrupts services and needs a final reboot. "
            "Wait until tasks finish, take a boot-disk snapshot, "
            "then pass --maintenance-window. No changes made."
        )
        raise RuntimeError(msg)
    require_root()
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        msg = "Release upgrades must be attended in an interactive SSH terminal."
        raise RuntimeError(msg)
    release = read_optional("/etc/os-release")
    if 'VERSION_ID="24.04"' not in release:
        msg = "This upgrade path only supports Ubuntu 24.04 to 26.04 LTS."
        raise RuntimeError(msg)
    prompt = read_optional("/etc/update-manager/release-upgrades")
    if not re.search(r"^Prompt=lts\s*$", prompt, re.MULTILINE):
        msg = "Set Prompt=lts in /etc/update-manager/release-upgrades first."
        raise RuntimeError(msg)
    offered = capture(["do-release-upgrade", "--check-dist-upgrade-only"])
    if not re.search(r"New release '26\.04(?:\.\d+)? LTS' available", offered):
        msg = "The supported Ubuntu 26.04 LTS upgrade is not currently offered."
        raise RuntimeError(msg)
    if shutil.disk_usage("/").free < 10 * 1024**3:
        msg = "Keep at least 10 GiB free for the release upgrade."
        raise RuntimeError(msg)
    if capture(["dpkg", "--audit"]):
        msg = "Resolve dpkg audit findings before the release upgrade."
        raise RuntimeError(msg)
    if input("Type upgrade-to-26.04-lts after saving all work and taking the snapshot: ") != "upgrade-to-26.04-lts":
        msg = "Release upgrade cancelled. No changes made."
        raise RuntimeError(msg)
    patch_packages(maintenance_window=True)
    if Path("/run/reboot-required").exists():
        msg = (
            "Current-release patches require a reboot first. "
            "Reboot in this maintenance window, reconnect, then rerun release."
        )
        raise RuntimeError(msg)
    # No -d and no source-list rewrite: use Canonical's offered LTS upgrade.
    # The upstream tool prompts for configuration changes and the final reboot.
    result = execute(["do-release-upgrade"], timeout=None)
    if result.returncode:
        message = "Release upgrade did not complete successfully"
        raise RuntimeError(message)


def main(arguments: Sequence[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not arguments or arguments[0] in {"--help", "-h"}:
        emit("Internal root worker. Use lra runtime maintenance --help.")
        return
    action, *options = arguments
    if action not in {"audit", "plan", "policy", "patch", "release"} or not set(options).issubset(
        {"--check-release", "--maintenance-window"}
    ):
        message = "Unsupported maintenance worker request"
        raise ValueError(message)
    match action:
        case "audit":
            emit(json.dumps(asdict(audit(check_release="--check-release" in options)), indent=2))
        case "plan":
            emit(capture(["apt-get", "--simulate", "full-upgrade"]))
        case "policy":
            install_policy()
        case "release":
            upgrade_release(maintenance_window="--maintenance-window" in options)
        case _:
            patch_packages(maintenance_window="--maintenance-window" in options)


if __name__ == "__main__":
    try:
        main()
    except (EOFError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        emit(f"maintenance: {error}", error=True)
        sys.exit(1)
