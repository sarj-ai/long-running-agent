from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

from dataclasses import dataclass, field
from pathlib import Path
import shutil

from pydantic import BaseModel, ConfigDict
import typer

from long_running_agent.process import CommandError, environment_snapshot, run

from .health import collect_health, health_lines
from .settings import RuntimePaths, preferences, user_environment


class AdcConfiguration(BaseModel):
    model_config = ConfigDict(extra="allow")
    quota_project_id: str = ""


@dataclass
class Reporter:
    environment: dict[str, str] = field(default_factory=environment_snapshot)
    failures: list[str] = field(default_factory=list)

    def report(self, label: str, *, ok: bool, detail: str = "") -> None:
        typer.echo(f"[{'OK' if ok else 'FIX'}] {label}" + (f": {detail}" if detail else ""))
        if not ok:
            self.failures.append(label)

    def check(self, label: str, arguments: Sequence[str]) -> bool:
        try:
            result = run(arguments, capture_output=True, check=False, timeout=25, env=self.environment)
        except OSError, CommandError:
            self.report(label, ok=False)
            return False
        passed = result.returncode == 0
        self.report(label, ok=passed)
        return passed

    def identity(self, label: str, arguments: Sequence[str], expected: str = "") -> str:
        try:
            result = run(arguments, capture_output=True, check=False, timeout=25, env=self.environment)
        except OSError, CommandError:
            self.report(label, ok=False)
            return ""
        active = result.stdout.strip()
        valid = result.returncode == 0 and active not in {"", "(unset)"}
        if expected and active != expected:
            self.report(label, ok=False, detail=f"Active {active or 'unset'}; expected {expected}")
            return ""
        self.report(
            label, ok=valid, detail=f"Active {active}" + (" (no expected identity configured)" if not expected else "")
        )
        return active if valid else ""


def check_adc(report: Reporter, paths: RuntimePaths, project: str) -> None:
    report.check("Google ADC token refresh", ["gcloud", "auth", "application-default", "print-access-token"])
    try:
        value = AdcConfiguration.model_validate_json(
            (paths.home / ".config/gcloud/application_default_credentials.json").read_text()
        )
        report.report(
            "Google ADC quota project",
            ok=not value.quota_project_id or not project or value.quota_project_id == project,
            detail=value.quota_project_id
            or "not configured; optional and requires permission for the API's quota project",
        )
    except OSError, ValueError:
        report.report("Google ADC configuration", ok=False)


def repository_checkouts(paths: RuntimePaths, organization: str = "") -> Iterator[Path]:
    if organization and (organization in {".", ".."} or Path(organization).name != organization):
        message = "LRA_GITHUB_ORG must name one organization directory under your code directory"
        raise ValueError(message)
    base = paths.code / organization
    if not base.is_dir() or base.is_symlink():
        return
    for entry in sorted(base.iterdir()):
        if entry.name.startswith(".") or entry.name == "worktrees" or entry.is_symlink() or not entry.is_dir():
            continue
        if (entry / ".git").exists():
            yield entry
        elif not organization:
            for checkout in sorted(entry.iterdir()):
                if (
                    not checkout.name.startswith(".")
                    and checkout.is_dir()
                    and not checkout.is_symlink()
                    and (checkout / ".git").exists()
                ):
                    yield checkout


def inspect_runtime(paths: RuntimePaths) -> Reporter:
    environment = user_environment(paths)
    report = Reporter(environment=environment)
    settings = preferences(paths, environment=environment)
    for tool in ("zellij", "uv", "gh", "gcloud"):
        available = shutil.which(tool, path=environment["PATH"]) is not None
        report.report(tool, ok=available)
    report.identity("GitHub", ["gh", "api", "user", "--jq", ".login"], settings.get("SARJ_VM_GITHUB_USER", ""))
    account = report.identity(
        "Google account", ["gcloud", "config", "get-value", "account"], settings.get("SARJ_VM_ACCOUNT", "")
    )
    project = report.identity(
        "Google project", ["gcloud", "config", "get-value", "project"], settings.get("SARJ_VM_PROJECT", "")
    )
    if account and project:
        report.check(
            "Google project API",
            [
                "gcloud",
                "projects",
                "describe",
                project,
                f"--account={account}",
                f"--project={project}",
                "--format=value(projectId)",
            ],
        )
    check_adc(report, paths, settings.get("SARJ_VM_PROJECT", "") or project)
    report.check("Git identity", ["git", "var", "GIT_AUTHOR_IDENT"])
    for checkout in repository_checkouts(paths, settings.get("LRA_GITHUB_ORG", "")):
        report.check(f"{checkout.name} checkout", ["git", "-C", str(checkout), "status", "--short", "--branch"])
        report.check(
            f"{checkout.name} origin access",
            ["git", "-C", str(checkout), "ls-remote", "--exit-code", "origin", "HEAD"],
        )
    try:
        host = collect_health(Path("/proc"), paths.home, run)
        for line in health_lines(host):
            typer.echo(line)
        report.report("System services", ok=host.failed_units == ())
    except OSError, RuntimeError:
        report.report("Host health", ok=False, detail="Run vm health to inspect capacity separately")
    return report


def doctor() -> None:
    report = inspect_runtime(RuntimePaths.current())
    if report.failures:
        typer.echo("Needs attention: " + ", ".join(report.failures))
        raise typer.Exit(1)
    typer.echo("Configured identities and present repositories passed.")
