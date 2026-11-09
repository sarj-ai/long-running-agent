from __future__ import annotations

import json
from pathlib import Path
import tempfile
from typing import Annotated, Final

from pydantic import BaseModel, ConfigDict, Field
import typer

from long_running_agent.process import run
from long_running_agent.runtime import admin_worker

from . import cleanup_inspection


app = typer.Typer(help="Register OS Login users and inspect their browser isolation.", no_args_is_help=True)
WORKER_DIRECTORY: Final = admin_worker.REQUEST_DIRECTORY
WORKERS: Final = ("admin_worker.py", "maintenance_worker.py", "bootstrap.py")
REQUESTS: Final = ("connection", "check", "firewall-load", "install-self-registration")
USER_SHIM: Final = '#!/bin/sh\nexec /usr/bin/python3 -I /opt/long-running-agent/admin_worker.py "$@"\n'


class RegistrationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    username: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.-]{0,63}$")]
    uid: Annotated[int, Field(ge=1000)] | None = None
    legacy_browser: bool = False
    allow_privileged: bool = False
    memory_high: Annotated[str, Field(pattern=r"^(?:[1-9][0-9]*[KMGT]?|infinity)$")] | None = None
    tasks_max: Annotated[str, Field(pattern=r"^(?:[1-9][0-9]*|infinity)$")] | None = None
    cpu_weight: Annotated[int, Field(ge=1, le=10000)] = 100

    def request(self) -> str:
        return json.dumps(
            {
                "action": "register",
                "username": self.username,
                "uid": self.uid,
                "legacy_browser": self.legacy_browser,
                "allow_privileged": self.allow_privileged,
                "memory_high": self.memory_high,
                "tasks_max": self.tasks_max,
                "cpu_weight": self.cpu_weight,
            }
        )


def invoke(action: str, *, registration: RegistrationInput | None = None, privileged: bool = True) -> None:
    executable = Path(admin_worker.EXECUTABLE)
    admin_worker.root_owned(executable.parent, directory=True)
    admin_worker.root_owned(executable)
    request = None if registration is None else registration.request()
    argument = (
        "/dev/stdin"
        if request is not None
        else ("register-self" if action == "register-self" else str(WORKER_DIRECTORY / f"{action}.json"))
    )
    command = [str(executable), argument]
    if privileged:
        command = ["sudo", "--", *command]
    run(command, input_text=request)


@app.command("register")
def register(
    username: str,
    *,
    uid: int | None = None,
    legacy_browser: bool = False,
    allow_privileged: bool = False,
    memory_high: str | None = None,
    tasks_max: str | None = None,
    cpu_weight: int = 100,
) -> None:
    """Register an existing ordinary OS Login account; this grants no IAM or local groups."""
    request = RegistrationInput(
        username=username,
        uid=uid,
        legacy_browser=legacy_browser,
        allow_privileged=allow_privileged,
        memory_high=memory_high,
        tasks_max=tasks_max,
        cpu_weight=cpu_weight,
    )
    invoke("register", registration=request)


@app.command("register-self")
def register_self() -> None:
    """Register only the current sudo caller, with ordinary-user policy and no identity options."""
    invoke("register-self")


@app.command("connection")
def connection() -> None:
    """Read the current UID's registered browser endpoints without sudo."""
    invoke("connection", privileged=False)


@app.command("check")
def check() -> None:
    """Check registered accounts, private homes, resource policies, and current firewall receipts."""
    invoke("check")


@app.command("firewall-load")
def firewall_load() -> None:
    """Reload only the dedicated browser UID table, serialized with account registration."""
    invoke("firewall-load")


@app.command("install-self-registration")
def install_self_registration() -> None:
    """Install the validated exact-command sudo rule and private defaults for future logins."""
    invoke("install-self-registration")


def guarded_install_destination(path: Path, *, directory: bool = False) -> None:
    if path.exists() or path.is_symlink():
        admin_worker.root_owned(path, directory=directory)


def prepare_host_commands(staging: Path) -> list[list[str]]:
    commands = [
        ["sudo", "--", "/usr/bin/install", "-d", "-o", "root", "-g", "root", "-m", "0755", str(WORKER_DIRECTORY)]
    ]
    commands.extend(
        [
            "sudo",
            "--",
            "/usr/bin/install",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0644",
            str(staging / name),
            str(WORKER_DIRECTORY / name),
        ]
        for name in (*WORKERS, *(f"{action}.json" for action in REQUESTS))
    )
    return [
        *commands,
        [
            "sudo",
            "--",
            "/usr/bin/install",
            "-o",
            "root",
            "-g",
            "root",
            "-m",
            "0755",
            str(staging / "lra-user"),
            admin_worker.EXECUTABLE,
        ],
        ["sudo", "--", "/usr/bin/python3", "-I", str(WORKER_DIRECTORY / "bootstrap.py")],
        ["sudo", "--", "/usr/bin/python3", "-I", str(WORKER_DIRECTORY / "maintenance_worker.py"), "policy"],
        [
            "sudo",
            "--",
            "/usr/bin/python3",
            "-I",
            str(WORKER_DIRECTORY / "admin_worker.py"),
            str(WORKER_DIRECTORY / "install-self-registration.json"),
        ],
    ]


@app.command("prepare-host")
def prepare_host() -> None:
    """Install or upgrade the reviewed root workers and request files.

    Run after upgrading LRA on an existing host; existing sessions are not restarted.
    """
    for parent in (Path("/opt"), Path("/usr/local/sbin")):
        admin_worker.root_owned(parent, directory=True)
    guarded_install_destination(WORKER_DIRECTORY, directory=True)
    for name in (*WORKERS, *(f"{action}.json" for action in REQUESTS)):
        guarded_install_destination(WORKER_DIRECTORY / name)
    guarded_install_destination(Path(admin_worker.EXECUTABLE))
    with tempfile.TemporaryDirectory(prefix="lra-prepare-host-") as temporary:
        staging = Path(temporary)
        for name in WORKERS:
            (staging / name).write_bytes(Path(__file__).with_name(name).read_bytes())
        for action in REQUESTS:
            (staging / f"{action}.json").write_text(json.dumps({"action": action}) + "\n", encoding="utf-8")
        (staging / "lra-user").write_text(USER_SHIM, encoding="utf-8")
        for arguments in prepare_host_commands(staging):
            run(arguments)
    typer.echo(
        "Host workers installed. Register existing administrators explicitly before enabling ordinary team access."
    )


@app.command("prepare-cleanup-probe")
def prepare_cleanup_probe() -> None:
    """Install the reviewed read-only process probe; cleanup and deletion remain unprivileged."""
    cleanup_inspection.prepare_host_probe()
    typer.echo("Read-only process probe enabled. It inspects only the connecting registered user's references.")
