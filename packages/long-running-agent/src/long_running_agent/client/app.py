from __future__ import annotations

import json
from typing import Annotated

from rich.console import Console
import typer

from long_running_agent.client.controller import Client
from long_running_agent.client.tunnel import tunnel
from long_running_agent.config import Settings, load_settings


app = typer.Typer(help="Connect to your persistent VM terminal and browser.", no_args_is_help=True)
console = Console(stderr=True)


def _client(ctx: typer.Context) -> Client:
    settings: object = ctx.find_root().obj  # pyright: ignore[reportAny] -- Typer stores arbitrary context objects.
    return Client(settings if isinstance(settings, Settings) else load_settings())


@app.command()
def terminal(
    ctx: typer.Context,
    session: Annotated[
        str, typer.Argument(help="Your personal Zellij session; existing windows are preserved.")
    ] = "sarj-home",
    *,
    no_host: Annotated[
        bool, typer.Option(help="Attach without the Mac return channel, for an older VM runtime.", hidden=True)
    ] = False,
) -> None:
    """Open your persistent terminal. Reconnect to return to the same windows."""
    raise typer.Exit(_client(ctx).terminal(session, host_bridge=not no_host))


@app.command()
def browser(
    ctx: typer.Context,
    url: Annotated[str | None, typer.Argument(help="Website to open in the existing VM Chrome profile.")] = None,
    *,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Forward the browser and print its local address.")
    ] = False,
) -> None:
    """Open your VM browser with its saved tabs and sign-ins."""
    raise typer.Exit(_client(ctx).browser(no_open=no_open, url=url))


@app.command()
def info(
    ctx: typer.Context,
    *,
    json_output: Annotated[bool, typer.Option("--json", help="Print compact JSON for scripts.")] = False,
) -> None:
    """Show VM identity, state, and capacity without waking it."""
    client = _client(ctx)
    state = client.cloud.state(timeout=15)
    result: dict[str, object] = {
        "instance": client.settings.instance,
        "project": client.settings.project,
        "zone": client.settings.zone,
        "state": state,
    }
    if state == "RUNNING":
        reply = client.remote("runtime host info", capture_output=True, timeout=15)
        result["runtime"] = json.loads(reply.stdout)
    if json_output:
        typer.echo(json.dumps(result, separators=(",", ":")))
    else:
        typer.echo(json.dumps(result, indent=2))


@app.command("ssh-config", hidden=True)
def ssh_config(ctx: typer.Context) -> None:
    """Install your private IAP/OS Login SSH host configuration."""
    console.print(str(_client(ctx).configure_ssh()), markup=False)


@app.command(hidden=True)
def setup(ctx: typer.Context) -> None:
    """Configure SSH and the VM user's terminal runtime."""
    _client(ctx).setup()


@app.command("tunnel", hidden=True)
def iap_tunnel(ctx: typer.Context, port: int) -> None:
    tunnel(_client(ctx).settings, port)
