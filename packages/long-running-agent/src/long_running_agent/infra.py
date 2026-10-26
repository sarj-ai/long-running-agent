from __future__ import annotations

from pathlib import Path
import shutil
from typing import TYPE_CHECKING, Annotated, Final

from pydantic import BaseModel, ConfigDict
import typer

from long_running_agent.client.cloud import Cloud, sanitized_environment
from long_running_agent.config import Settings, load_settings
from long_running_agent.process import environment_snapshot, exec_command, run


if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

TERRAFORM_VERSION: Final = "1.17.0"
OFFLINE_COMMANDS: Final = frozenset({"fmt", "validate", "version"})
OFFLINE_FLAGS: Final = frozenset({"-h", "--help", "-help", "-version", "--version"})
app = typer.Typer(
    help="Operate the protected Terraform deployment with your own Google identity.", no_args_is_help=True
)


class TerraformVersion(BaseModel):
    model_config = ConfigDict(strict=True)
    terraform_version: str


class TerraformUnavailableError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(f"Install Terraform {TERRAFORM_VERSION} with mise before running infrastructure commands")


def find_terraform() -> str:
    mise = shutil.which("mise")
    candidate: str | None = None
    if mise:
        result = run(
            [mise, "--no-config", "--no-env", "--no-hooks", "where", f"aqua:hashicorp/terraform@{TERRAFORM_VERSION}"],
            capture_output=True,
            check=False,
            timeout=15,
        )
        installed = Path(result.stdout.strip()) / "terraform"
        if result.returncode == 0 and installed.is_file():
            candidate = str(installed)
    candidate = candidate or shutil.which("terraform")
    if candidate is None:
        raise TerraformUnavailableError
    result = run([candidate, "version", "-json"], capture_output=True, timeout=15)
    if TerraformVersion.model_validate_json(result.stdout).terraform_version != TERRAFORM_VERSION:
        raise TerraformUnavailableError
    return candidate


def terraform_directory(start: Path) -> Path:
    for parent in (start, *start.parents):
        candidate = parent / "terraform"
        if (candidate / "main.tf").is_file():
            return candidate
    message = "Run infrastructure commands from the checkout, or supply --directory with the Terraform module"
    raise FileNotFoundError(message)


def requires_cloud_token(arguments: Sequence[str]) -> bool:
    options = arguments[: arguments.index("--")] if "--" in arguments else arguments
    if not arguments or OFFLINE_FLAGS.intersection(options):
        return False
    command = next((argument for argument in arguments if not argument.startswith("-")), "")
    return command not in OFFLINE_COMMANDS and not (command == "init" and "-backend=false" in arguments)


def terraform_environment(
    settings: Settings,
    arguments: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    token_provider: Callable[[], str] | None = None,
) -> dict[str, str]:
    child = sanitized_environment(environment_snapshot() if environment is None else environment)
    child["TF_IN_AUTOMATION"] = "1"
    if requires_cloud_token(arguments):
        token = (
            token_provider()
            if token_provider is not None
            else Cloud(settings).execute(["auth", "print-access-token"], capture_output=True).stdout
        )
        if not token.strip():
            message = "Google returned an empty access token; sign in with your own account and retry"
            raise RuntimeError(message)
        child["GOOGLE_OAUTH_ACCESS_TOKEN"] = token.strip()
    return child


@app.command("tf", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def terraform_command(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Option("--directory", help="Terraform module; defaults to this checkout's terraform directory."),
    ] = None,
) -> None:
    """Run pinned Terraform. Review a saved plan before applying infrastructure changes."""
    settings: object = ctx.find_root().obj  # pyright: ignore[reportAny] -- Typer intentionally stores arbitrary context objects.
    active = settings if isinstance(settings, Settings) else load_settings()
    module = terraform_directory(Path.cwd()) if directory is None else directory.resolve()
    if not (module / "main.tf").is_file():
        message = "The selected directory is not the Terraform module"
        raise typer.BadParameter(message)
    arguments = list(ctx.args) or ["-help"]
    environment = terraform_environment(active, arguments)
    exec_command([find_terraform(), f"-chdir={module}", *arguments], env=environment)
