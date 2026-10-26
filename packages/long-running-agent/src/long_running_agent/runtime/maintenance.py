from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import stat

import typer

from long_running_agent.process import run

from .admin_worker import root_owned
from .maintenance_worker import audit, capture


app = typer.Typer(help="Inspect OS patches and perform attended maintenance.", no_args_is_help=True)


def validated_worker(worker: Path) -> Path:
    for directory in reversed(worker.parents):
        root_owned(directory, directory=True)
    metadata = worker.lstat()
    if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
        msg = "The maintenance worker must be an administrator-installed, root-owned regular file."
        raise RuntimeError(msg)
    return worker


def privileged_action(action: str, *, maintenance_window: bool = False) -> None:
    worker = validated_worker(Path("/opt/long-running-agent/maintenance_worker.py"))
    command = ["sudo", "--", "/usr/bin/python3", "-I", str(worker), action]
    if maintenance_window:
        command.append("--maintenance-window")
    run(command)


@app.command("audit")
def audit_system(*, check_release: bool = False) -> None:
    """Inspect pending patches, reboot state, and the supported distro offer."""
    typer.echo(json.dumps(asdict(audit(check_release=check_release)), indent=2))


@app.command("plan")
def plan() -> None:
    """Show apt's proposed package changes without installing anything."""
    typer.echo(capture(["apt-get", "--simulate", "full-upgrade"]))


@app.command("policy")
def policy() -> None:
    """Keep daily security updates while reserving broad restarts for maintenance."""
    privileged_action("policy")


@app.command("patch")
def patch(*, maintenance_window: bool = False) -> None:
    """Patch the current distro only after all work is saved."""
    if not maintenance_window:
        msg = "Save work and pass --maintenance-window before patching."
        raise typer.BadParameter(msg)
    privileged_action("patch", maintenance_window=maintenance_window)


@app.command("release")
def release(*, maintenance_window: bool = False) -> None:
    """Perform the officially offered LTS upgrade in an attended SSH terminal."""
    if not maintenance_window:
        msg = "Save work, take a snapshot, and pass --maintenance-window."
        raise typer.BadParameter(msg)
    privileged_action("release", maintenance_window=maintenance_window)
