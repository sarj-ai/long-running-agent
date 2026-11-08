from __future__ import annotations

from typing import Annotated

from rich.console import Console
import typer

from long_running_agent.client.azure import prepare_client as prepare_azure_client
from long_running_agent.client.controller import Client
from long_running_agent.client.terminal import validate_session_name
from long_running_agent.client.tunnel import tunnel
from long_running_agent.config import Settings, load_settings
from long_running_agent.host_connection import website_url


app = typer.Typer(help="Connect to your persistent VM terminal and browser.", no_args_is_help=True)
console = Console(stderr=True)


def _client(ctx: typer.Context) -> Client:
    settings: object = ctx.find_root().obj  # pyright: ignore[reportAny] -- Typer stores arbitrary context objects.
    if callable(settings):
        settings = settings()
    return Client(settings if isinstance(settings, Settings) else load_settings())


@app.command()
def terminal(
    ctx: typer.Context,
    session: Annotated[
        str, typer.Argument(help="Your personal Zellij session; existing windows are preserved.")
    ] = "sarj-home",
    *,
    azure: Annotated[bool, typer.Option("--azure", help="Use the Azure VM; the default is GCP.")] = False,
    no_host: Annotated[
        bool, typer.Option(help="Attach without the Mac return channel, for an older VM runtime.", hidden=True)
    ] = False,
) -> None:
    """Open your persistent terminal. Reconnect to return to the same windows."""
    validate_session_name(session)
    controller = prepare_azure_client() if azure else _client(ctx)
    raise typer.Exit(controller.terminal(session, host_bridge=not no_host))


@app.command()
def browser(
    ctx: typer.Context,
    url: Annotated[str | None, typer.Argument(help="Website to open in the existing VM Chrome profile.")] = None,
    *,
    azure: Annotated[bool, typer.Option("--azure", help="Use the Azure VM; the default is GCP.")] = False,
    no_open: Annotated[
        bool, typer.Option("--no-open", help="Forward the browser and print its local address.")
    ] = False,
) -> None:
    """Open your VM browser with its saved tabs and sign-ins."""
    if url is not None:
        website_url(url)
    controller = prepare_azure_client() if azure else _client(ctx)
    raise typer.Exit(controller.browser(no_open=no_open, url=url))


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
