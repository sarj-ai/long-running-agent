from __future__ import annotations

import ast
from pathlib import Path
import shutil
import subprocess
import sys
from typing import TYPE_CHECKING

from long_running_agent.runtime import bootstrap
from long_running_agent.runtime.admin import USER_SHIM, WORKERS, prepare_host_commands
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


def test_existing_ready_host_skips_every_system_command(tmp_path: Path) -> None:
    paths = bootstrap.HostPaths(tmp_path)
    paths.ready.parent.mkdir(parents=True)
    paths.ready.write_text("existing deployment\n", encoding="utf-8")
    paths.failed.write_text("previous receipt\n", encoding="utf-8")

    def must_not_run(
        arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        pytest.fail(
            f"An existing ready host tried to run {arguments[0]} (capture={capture}, input={input_text is not None})"
        )

    assert bootstrap.bootstrap(paths, must_not_run) is False
    assert paths.ready.read_text(encoding="utf-8") == "existing deployment\n"
    assert paths.failed.read_text(encoding="utf-8") == "previous receipt\n"
    assert not paths.path("/etc").exists()


def test_failure_records_failed_and_never_ready(tmp_path: Path) -> None:
    paths = bootstrap.HostPaths(tmp_path)
    observed: list[str] = []

    def fail_first_command(
        arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        observed.append(arguments[0])
        message = f"fixture command failure (capture={capture}, input={input_text is not None})"
        raise RuntimeError(message)

    with pytest.raises(RuntimeError, match="fixture command failure"):
        bootstrap.bootstrap(paths, fail_first_command)
    assert observed == ["/usr/bin/fallocate"]
    assert paths.failed.is_file()
    assert not paths.ready.exists()
    assert not paths.path("/swapfile.sarj").exists()
    assert not list(tmp_path.glob(".swapfile.sarj-*"))


def test_fresh_host_security_settings_preserve_sshd_and_kernel_scope(tmp_path: Path) -> None:
    paths = bootstrap.HostPaths(tmp_path)
    profile = paths.path("/usr/share/apparmor/extra-profiles/bwrap-userns-restrict")
    profile.parent.mkdir(parents=True)
    profile.write_text("fixture scoped bubblewrap profile\n", encoding="utf-8")
    paths.path("/etc/apparmor.d").mkdir(parents=True)
    commands: list[tuple[str, ...]] = []

    def record(
        arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(arguments))
        assert capture is False
        assert input_text is None
        return subprocess.CompletedProcess(arguments, 0, "", "")

    bootstrap.configure_host(paths, record)
    sshd = paths.path("/etc/ssh/sshd_config.d/00-sarj-devbox.conf").read_text(encoding="utf-8")
    assert "PasswordAuthentication no" in sshd
    assert "PermitRootLogin no" in sshd
    assert commands.index(("/usr/sbin/sshd", "-t")) < commands.index(("/usr/bin/systemctl", "reload", "ssh"))
    assert ("/usr/bin/systemctl", "restart", "ssh") not in commands
    assert paths.path("/etc/apparmor.d/bwrap-userns-restrict").read_text(encoding="utf-8") == profile.read_text(
        encoding="utf-8"
    )
    assert not any(
        "kernel.apparmor_restrict_unprivileged_userns=0" in argument for command in commands for argument in command
    )


def test_fresh_host_keeps_security_without_the_retired_browser_or_terminal_stack() -> None:
    assert {
        "nftables",
        "sudo",
        "bubblewrap",
        "docker.io",
        "git",
        "gh",
        "unattended-upgrades",
        "needrestart",
        "update-manager-core",
    } <= set(bootstrap.PACKAGES)
    assert {"xvfb", "xauth", "x11vnc", "novnc", "websockify", "openbox", "tmux", "xclip"}.isdisjoint(bootstrap.PACKAGES)
    assert "signed-by=/usr/share/keyrings/cloud.google.gpg" in bootstrap.GCLOUD_REPOSITORY
    assert bootstrap.SWAP_CONFIGURATION == "vm.swappiness=10\n"


def test_prepare_host_only_executes_installed_system_python_workers(tmp_path: Path) -> None:
    commands = prepare_host_commands(tmp_path)
    executions = [arguments for arguments in commands if "/usr/bin/python3" in arguments]
    assert executions == [
        ["sudo", "--", "/usr/bin/python3", "-I", "/opt/long-running-agent/bootstrap.py"],
        ["sudo", "--", "/usr/bin/python3", "-I", "/opt/long-running-agent/maintenance_worker.py", "policy"],
        [
            "sudo",
            "--",
            "/usr/bin/python3",
            "-I",
            "/opt/long-running-agent/admin_worker.py",
            "install-self-registration",
        ],
    ]
    assert USER_SHIM == '#!/bin/sh\nexec /usr/bin/python3 -I /opt/long-running-agent/admin_worker.py "$@"\n'
    for name in WORKERS:
        source = Path(bootstrap.__file__).with_name(name).read_text(encoding="utf-8")
        tree = ast.parse(source, feature_version=(3, 12))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                assert node.level == 0
                modules = [node.module or ""]
            else:
                continue
            assert all(module.partition(".")[0] in sys.stdlib_module_names for module in modules)


def test_root_workers_compile_on_actual_system_python312() -> None:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("The development workspace requires uv to locate system Python3.12")
    located = subprocess.run(
        [uv, "python", "find", "--no-python-downloads", "3.12"], capture_output=True, text=True, check=False
    )
    if located.returncode:
        pytest.skip("Install Python3.12 with uv python install3.12; CI installs it explicitly")
    compiler = 'from pathlib import Path; import sys; compile(Path(sys.argv[1]).read_bytes(), sys.argv[1], "exec")'
    for name in WORKERS:
        source = Path(bootstrap.__file__).with_name(name)
        result = subprocess.run(
            [located.stdout.strip(), "-I", "-c", compiler, str(source)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, result.stderr


def test_first_boot_installs_github_cli_from_its_signed_repository(tmp_path: Path) -> None:
    paths = bootstrap.HostPaths(tmp_path)
    commands: list[tuple[str, ...]] = []
    github_key = paths.path("/etc/apt/keyrings/githubcli-archive-keyring.gpg")
    github_source = paths.path("/etc/apt/sources.list.d/github-cli.list")
    github_key.parent.mkdir(parents=True, mode=0o700)

    def record(
        arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(arguments))
        if arguments[0] == "/usr/bin/curl" and "-o" in arguments:
            assert capture is False
            assert input_text is None
            assert arguments[-1] == "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
            candidate = Path(arguments[arguments.index("-o") + 1])
            assert candidate.parent == github_key.parent
            assert candidate != github_key
            assert candidate.stat().st_mode & 0o777 == 0o600
            candidate.write_bytes(b"fixture binary keyring\x00")
        elif arguments[0] == "/usr/bin/gpg":
            assert input_text == "fixture armored Google key"
            Path(arguments[-1]).write_bytes(b"fixture Google keyring")
        elif tuple(arguments) == (*bootstrap.APT, "install", "-y", "google-cloud-cli", "gh"):
            assert github_key.read_bytes() == b"fixture binary keyring\x00"
            assert github_key.stat().st_mode & 0o777 == 0o644
            assert github_key.parent.stat().st_mode & 0o777 == 0o755
            assert github_source.read_text(encoding="utf-8") == (
                "deb [signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] "
                "https://cli.github.com/packages stable main\n"
            )
            assert commands[-2] == (*bootstrap.APT, "update")
        return subprocess.CompletedProcess(arguments, 0, "fixture armored Google key" if capture else "", "")

    bootstrap.install_packages(paths, record)
    assert commands[-1] == (*bootstrap.APT, "install", "-y", "google-cloud-cli", "gh")
    assert github_source.stat().st_mode & 0o777 == 0o644


def test_github_key_download_failure_preserves_the_previous_repository_key(tmp_path: Path) -> None:
    paths = bootstrap.HostPaths(tmp_path)
    key = paths.path("/etc/apt/keyrings/githubcli-archive-keyring.gpg")
    key.parent.mkdir(parents=True)
    key.write_bytes(b"previous usable key")
    source = paths.path("/etc/apt/sources.list.d/github-cli.list")
    source.parent.mkdir(parents=True)
    source.write_text(bootstrap.GITHUB_REPOSITORY, encoding="utf-8")

    def interrupted_download(
        arguments: Sequence[str], *, capture: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        assert arguments[0] == "/usr/bin/curl"
        assert capture is False
        assert input_text is None
        candidate = Path(arguments[arguments.index("-o") + 1])
        candidate.write_bytes(b"interrupted download")
        raise subprocess.CalledProcessError(22, arguments)

    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.configure_github_repository(paths, interrupted_download)
    assert key.read_bytes() == b"previous usable key"
    assert source.read_text(encoding="utf-8") == bootstrap.GITHUB_REPOSITORY
    assert sorted(path.name for path in key.parent.iterdir()) == [key.name]
