from __future__ import annotations

from pathlib import Path
import sys
from typing import Annotated

from rich.console import Console
import typer

from long_running_agent import infra
from long_running_agent.client import app as client
from long_running_agent.config import load_settings
from long_running_agent.runtime.app import app as runtime_app


console = Console(stderr=True)
app = typer.Typer(
    help="Persistent terminals and browser on a private VM through IAP and OS Login.",
    no_args_is_help=True,
    pretty_exceptions_enable=False,
)
app.add_typer(client.app, name="vm")
app.add_typer(runtime_app, name="runtime")
app.add_typer(infra.app, name="infra")


@app.callback()
def configure(
    ctx: typer.Context,
    *,
    config: Annotated[Path | None, typer.Option("--config", help="Personal TOML connection profile.")] = None,
    account: Annotated[str | None, typer.Option("--account", help="Your Google account email.")] = None,
    project: Annotated[str | None, typer.Option("--project")] = None,
    instance: Annotated[str | None, typer.Option("--instance")] = None,
    zone: Annotated[str | None, typer.Option("--zone")] = None,
) -> None:
    ctx.obj = load_settings(
        config, overrides={"account": account, "project": project, "instance": instance, "zone": zone}
    )


connect_app = typer.Typer(
    help="Connect to your persistent VM terminal and browser.", no_args_is_help=True, pretty_exceptions_enable=False
)
connect_app.callback()(configure)
connect_app.command("terminal")(client.terminal)
connect_app.command("browser")(client.browser)
connect_app.command("send")(client.send)
connect_app.command("get")(client.get_file)
connect_app.command("info")(client.info)
connect_app.add_typer(client.codex_app, name="codex")


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
