from __future__ import annotations

import os
import platform
from typing import TYPE_CHECKING, Protocol

import typer

from long_running_agent.process import CommandError, Completed, run

from . import admin_worker
from .settings import RuntimePaths
from .zellij import install_zellij


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class RegistrationRunner(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        capture_output: bool = False,
        check: bool = True,
        timeout: float | None = None,
    ) -> Completed: ...


def unregistered_account() -> bool:
    # The root helper does not yet expose a distinct missing-registration exit
    # status. Establish absence only inside its protected, configured registry.
    registry = admin_worker.CONFIG
    admin_worker.root_owned(registry, directory=True)
    admin_worker.root_owned(admin_worker.USERS, directory=True)
    shared = admin_worker.read_root_json(registry / "shared.json")
    if shared != {"schema_version": 1, "browser_isolation": "uid-nftables"}:
        message = "Shared UID isolation is not configured; ask an administrator to run lra runtime admin prepare-host"
        raise RuntimeError(message)
    registration = admin_worker.USERS / f"{os.getuid()}.json"
    try:
        registration.lstat()
    except FileNotFoundError:
        return True
    return False


def ensure_personal_registration(
    *, runner: RegistrationRunner = run, unregistered: Callable[[], bool] = unregistered_account
) -> None:
    helper = admin_worker.EXECUTABLE
    result = runner([helper, "connection"], capture_output=True, check=False, timeout=15)
    if result.returncode == 0:
        return
    message = (
        "Personal browser registration is not ready. Existing registrations were preserved. "
        "Ask the VM administrator to run lra runtime admin check; a new user can run "
        "sudo -n /usr/local/sbin/lra-user register-self after host preparation."
    )
    try:
        eligible = unregistered()
    except OSError, RuntimeError:
        raise RuntimeError(message) from None
    if not eligible:
        raise RuntimeError(message)
    try:
        # No identity options, named-user registration, or general sudo fallback.
        runner(["sudo", "-n", helper, "register-self"], capture_output=True, timeout=60)
        runner([helper, "connection"], capture_output=True, timeout=15)
    except CommandError, OSError:
        raise RuntimeError(message) from None


def setup_personal(*, tools: bool = True) -> None:
    if os.getuid() == 0:
        message = "Run personal setup as your own OS Login user"
        raise typer.BadParameter(message)
    if platform.system() != "Linux":
        message = "Runtime setup runs inside the Linux VM"
        raise typer.BadParameter(message)
    os.umask(0o077)
    paths = RuntimePaths.current()
    (paths.home / ".local/bin").mkdir(parents=True, exist_ok=True, mode=0o700)
    ensure_personal_registration()
    if tools:
        install_zellij(paths)
    typer.echo("Terminal and browser registration ready. Existing settings, sign-ins, and user files were preserved.")
