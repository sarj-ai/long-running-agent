import typer

from . import admin, cleanup, cleanup_daemon, codex, host, maintenance, transfer, worktrees
from .doctor import doctor
from .health import health
from .setup import setup_personal


app = typer.Typer(
    help="Persistent terminal setup, browser isolation, and Linux host administration.", no_args_is_help=True
)
cleanup.app.add_typer(cleanup_daemon.app, name="daemon")
app.add_typer(admin.app, name="admin")
app.add_typer(maintenance.app, name="maintenance")
app.add_typer(worktrees.app, name="worktrees")
app.add_typer(cleanup.app, name="cleanup")
app.add_typer(host.app, name="host")
app.add_typer(transfer.app, name="transfer")
app.add_typer(codex.app, name="codex")
app.command("doctor", help="Check native providers, sign-ins, configured identities, and present repositories.")(doctor)
app.command("health", help="Show current capacity and pressure without sign-in or network checks.")(health)


@app.command("setup")
def setup(*, tools: bool = True) -> None:
    """Set up terminal tools and browser isolation without replacing personal configuration."""
    setup_personal(tools=tools)
