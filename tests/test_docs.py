from __future__ import annotations

from pathlib import Path
import sys
import tomllib
from typing import TYPE_CHECKING

from long_running_agent.process import environment_snapshot, run
from pydantic import BaseModel, Field, JsonValue


if TYPE_CHECKING:
    from collections.abc import Iterator


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts/generate_docs.py"
REFERENCE = Path("apps/docs/src/generated/reference.json")


class Parameter(BaseModel):
    name: str
    options: list[str]
    required: bool


class Command(BaseModel):
    path: list[str]
    parameters: list[Parameter]
    commands: list[Command]


class Entrypoint(BaseModel):
    name: str
    alias_of: str | None = Field(alias="aliasOf")
    default_command: str | None = Field(alias="defaultCommand")
    command: Command


class ApiSymbol(BaseModel):
    name: str
    source: str


class Project(BaseModel):
    scripts: dict[str, str]


class Manifest(BaseModel):
    project: Project


class TerraformInput(BaseModel):
    name: str
    description: str


class TerraformRoot(BaseModel):
    inputs: list[TerraformInput]


class Terraform(BaseModel):
    roots: list[TerraformRoot]


class Reference(BaseModel):
    entrypoints: list[Entrypoint]
    settings: dict[str, JsonValue]
    api: list[ApiSymbol]
    terraform: Terraform


def commands(root: Command) -> Iterator[Command]:
    yield root
    for command in root.commands:
        yield from commands(command)


def generate(destination: Path, *, check: bool = False) -> int:
    return run(
        [sys.executable, str(GENERATOR), "--output-directory", str(destination), *(["--check"] if check else [])],
        capture_output=True,
        check=False,
        timeout=30,
        cwd=ROOT,
    ).returncode


def test_reference_covers_installed_entrypoints_nested_groups_and_hidden_transport(tmp_path: Path) -> None:
    assert generate(tmp_path) == 0
    reference = Reference.model_validate_json((tmp_path / REFERENCE).read_bytes())
    with (ROOT / "packages/long-running-agent/pyproject.toml").open("rb") as stream:
        project = Manifest.model_validate(tomllib.load(stream)).project
    assert {entry.name for entry in reference.entrypoints} == set(project.scripts)
    entries = {entry.name: entry for entry in reference.entrypoints}
    assert entries["sarj-vm"].alias_of == "vm"
    assert entries["vm"].default_command == "terminal"
    assert entries["vm"].command.path == ["vm"]
    paths = {tuple(command.path): command for entry in entries.values() for command in commands(entry.command)}
    assert ("lra", "runtime", "cleanup", "daemon", "run") in paths
    assert ("lra", "infra", "tf") in paths
    assert ("sarj-host", "open-site") in paths
    assert "--session" in {
        flag for parameter in paths["lra", "runtime", "host", "attach"].parameters for flag in parameter.options
    }
    assert not any("ui" in path or "t3" in path for path in paths)
    assert {symbol.name for symbol in reference.api} == {"Client", "Settings"}
    for symbol in reference.api:
        source = ROOT / symbol.source
        assert not Path(symbol.source).is_absolute()
        assert source.resolve().is_relative_to(ROOT)
        assert source.is_file()
        assert source.suffix == ".py"
    assert reference.settings["configEnv"] == "LRA_CONFIG"
    inputs = reference.terraform.roots[0].inputs
    assert any(field.name == "project_id" for field in inputs)
    assert all(not field.name.startswith('"') and not field.description.startswith('"') for field in inputs)
    readme = (tmp_path / "README.md").read_text(encoding="utf-8")
    assert "lra runtime --help" in readme
    assert len(readme.splitlines()) <= 25


def test_generation_never_invokes_commands_profiles_or_home_factories(tmp_path: Path) -> None:
    guarded = tmp_path / "guarded"
    program = (
        "import runpy,sys\n"
        "from pathlib import Path\n"
        "from long_running_agent import config,process\n"
        "def forbidden(*args,**kwargs): raise AssertionError('Documentation invoked a runtime operation')\n"
        "config.load_settings=forbidden\n"
        "process.run=process.spawn=process.exec_command=forbidden\n"
        "Path.home=forbidden\n"
        "sys.argv=[sys.argv[1],'--output-directory',sys.argv[2]]\n"
        "runpy.run_path(sys.argv[0],run_name='__main__')\n"
    )
    sentinel = "private-doc-generation-value"
    result = run(
        [sys.executable, "-c", program, str(GENERATOR), str(guarded)],
        capture_output=True,
        check=False,
        timeout=30,
        cwd=ROOT,
        env={
            **environment_snapshot(),
            "HOME": str(tmp_path / sentinel),
            "LRA_ACCOUNT": sentinel,
            "LRA_CONFIG": sentinel,
        },
    )
    assert result.returncode == 0, result.stderr
    payload = (guarded / REFERENCE).read_text(encoding="utf-8")
    assert sentinel not in payload
    ordinary = tmp_path / "ordinary"
    assert generate(ordinary) == 0
    assert (ordinary / REFERENCE).read_bytes() == (guarded / REFERENCE).read_bytes()
    assert (ordinary / "README.md").read_bytes() == (guarded / "README.md").read_bytes()


def test_check_detects_readme_drift_without_changing_outputs(tmp_path: Path) -> None:
    assert generate(tmp_path) == 0
    assert generate(tmp_path, check=True) == 0
    readme = tmp_path / "README.md"
    reference = (tmp_path / REFERENCE).read_bytes()
    changed = readme.read_text(encoding="utf-8") + "Hand-maintained drift.\n"
    readme.write_text(changed, encoding="utf-8")
    assert generate(tmp_path, check=True) == 1
    assert readme.read_text(encoding="utf-8") == changed
    assert (tmp_path / REFERENCE).read_bytes() == reference
