from __future__ import annotations

import sys
from typing import Annotated

from rich.console import Console
import typer

from long_running_agent import infra
from long_running_agent.client import app as client
from long_running_agent.config import load_settings
from long_running_agent.runtime import codex
from long_running_agent.runtime.app import app as runtime_app


console = Console(stderr=True)
app = typer.Typer(
    help="Persistent terminals and browser on your private VM.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(client.app, name="vm")
app.add_typer(codex.app, name="codex")
app.add_typer(runtime_app, name="runtime", hidden=True)
app.add_typer(infra.app, name="infra", hidden=True)


@app.callback()
def configure(
    ctx: typer.Context,
    *,
    account: Annotated[str | None, typer.Option("--account", hidden=True)] = None,
    project: Annotated[str | None, typer.Option("--project", hidden=True)] = None,
    instance: Annotated[str | None, typer.Option("--instance", hidden=True)] = None,
    zone: Annotated[str | None, typer.Option("--zone", hidden=True)] = None,
) -> None:
    ctx.obj = load_settings(overrides={"account": account, "project": project, "instance": instance, "zone": zone})


connect_app = typer.Typer(
    help="Open your terminal or browser.",
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
connect_app.callback()(configure)
connect_app.command("terminal")(client.terminal)
connect_app.command("browser")(client.browser)


def invoke(application: typer.Typer, name: str, arguments: list[str] | None = None) -> None:
    try:
        application(args=arguments, prog_name=name)
    except (OSError, ValueError, RuntimeError) as error:
        console.print(str(error), style="red", markup=False)
        raise SystemExit(1) from None


def main() -> None:
    invoke(app, "lra")


def sarj_connect_main() -> None:
    invoke(connect_app, "sarj-connect")


def vm_arguments(arguments: list[str]) -> list[str]:
    return ["vm", *(arguments or ["terminal"])]


def vm_main() -> None:
    invoke(app, "vm", vm_arguments(sys.argv[1:]))


if __name__ == "__main__":
    main()
