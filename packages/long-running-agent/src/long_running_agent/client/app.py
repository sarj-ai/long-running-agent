from __future__ import annotations

import json
from pathlib import Path
import shlex
import tempfile
from typing import Annotated

from rich.console import Console
import typer

from long_running_agent.client import transfer
from long_running_agent.client.browser import start_route
from long_running_agent.client.controller import Client
from long_running_agent.client.host import HostBridge
from long_running_agent.client.tunnel import tunnel
from long_running_agent.config import Settings, load_settings


app = typer.Typer(help="Connect to your persistent VM terminal and browser.", no_args_is_help=True)
console = Console(stderr=True)
codex_app = typer.Typer(help="Switch the Codex account used by your active VM terminals.", no_args_is_help=True)
app.add_typer(codex_app, name="codex")


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
        bool, typer.Option(help="Attach without the Mac return channel, for an older VM runtime.")
    ] = False,
) -> None:
    """Wake the VM and attach to your persistent Zellij terminal windows."""
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
    """Open the existing persistent VM browser used by your agents."""
    raise typer.Exit(_client(ctx).browser(no_open=no_open, url=url))


@app.command()
def send(
    ctx: typer.Context,
    paths: Annotated[
        list[Path] | None, typer.Argument(help="Selected local files; drag files into this terminal.")
    ] = None,
    *,
    clipboard: Annotated[bool, typer.Option(help="Send the current Mac clipboard image once.")] = False,
    copy_path: Annotated[bool, typer.Option(help="Copy uploaded VM paths to the Mac clipboard.")] = False,
) -> None:
    """Send images or files to a private VM inbox and print their verified paths."""
    client = _client(ctx)
    if bool(paths) == clipboard:
        message = "Select file paths or use --clipboard"
        raise typer.BadParameter(message)
    if clipboard:
        with tempfile.TemporaryDirectory(prefix="lra-image-") as temporary:
            path = Path(temporary) / "clipboard.png"
            HostBridge(client, route_factory=start_route).clipboard_image(path)
            receipts = transfer.send(client, [path])
    else:
        receipts = transfer.send(client, paths or [])
    selected = " ".join(shlex.quote(receipt.path) for receipt in receipts)
    typer.echo(selected)
    if copy_path:
        client.runner(["/usr/bin/pbcopy"], input_text=selected, timeout=10)


@app.command("get")
def get_file(
    ctx: typer.Context,
    remote: Annotated[str, typer.Argument(help="An explicitly selected regular file in your VM home.")],
    destination: Annotated[
        Path | None, typer.Argument(help="Local destination directory; existing files are preserved.")
    ] = None,
) -> None:
    """Download a VM file, checking its size and SHA-256 before publishing it."""
    client = _client(ctx)
    receipt = transfer.get(client, remote, destination or client.home / "Downloads")
    typer.echo(shlex.quote(receipt.path))


@app.command()
def info(ctx: typer.Context, *, json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show VM identity and connection/capacity information without waking it."""
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


@codex_app.command("status")
def codex_status(ctx: typer.Context) -> None:
    """Show the VM's stored native Codex account."""
    raise typer.Exit(_client(ctx).codex("status"))


@codex_app.command("switch")
def codex_switch(ctx: typer.Context) -> None:
    """Sign in to Codex and restart its active Zellij panes from saved sessions."""
    raise typer.Exit(_client(ctx).codex("switch"))


@app.command("ssh", context_settings={"ignore_unknown_options": True, "allow_interspersed_args": False})
def shell(ctx: typer.Context, command: Annotated[list[str] | None, typer.Argument()] = None) -> None:
    """Open a raw shell, or run a command with literal argument boundaries."""
    raise typer.Exit(_client(ctx).shell(command or []))


@app.command()
def wake(ctx: typer.Context) -> None:
    """Start or resume the VM without opening a connection."""
    _client(ctx).wake()


@app.command("sleep")
def sleep_vm(ctx: typer.Context) -> None:
    """Suspend deliberately, preserving RAM while pausing active tasks."""
    _client(ctx).sleep()
    console.print("VM is sleeping. Run vm to resume.")


@app.command()
def stop(ctx: typer.Context) -> None:
    """Check durable worktrees, then stop; running processes end."""
    _client(ctx).stop()


@app.command()
def status(ctx: typer.Context) -> None:
    """Show the VM's power state and machine size without waking it."""
    client = _client(ctx)
    result = client.cloud.execute(
        [
            "compute",
            "instances",
            "describe",
            client.settings.instance,
            "--zone=" + client.settings.zone,
            "--format=yaml(name,status,machineType,disks.diskSizeGb)",
        ],
        capture_output=True,
    )
    console.print(result.stdout, markup=False)


@app.command()
def health(ctx: typer.Context) -> None:
    """Show capacity and resource pressure without waking a sleeping VM."""
    _client(ctx).health()


@app.command("ssh-config")
def ssh_config(ctx: typer.Context) -> None:
    """Install your private IAP/OS Login SSH host configuration."""
    console.print(str(_client(ctx).configure_ssh()), markup=False)


@app.command()
def setup(ctx: typer.Context) -> None:
    """Configure SSH and the VM user's terminal runtime."""
    _client(ctx).setup()


@app.command("tunnel", hidden=True)
def iap_tunnel(ctx: typer.Context, port: int) -> None:
    tunnel(_client(ctx).settings, port)
