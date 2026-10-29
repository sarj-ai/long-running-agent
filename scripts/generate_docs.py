from __future__ import annotations

import ast
from dataclasses import dataclass
from enum import Enum
import json
from pathlib import Path
import tomllib
from typing import TYPE_CHECKING, Final, Protocol, TypeGuard

import hcl2
from long_running_agent import cli
from long_running_agent.config import Settings
from long_running_agent.runtime import host
from pydantic import JsonValue, RootModel
import typer
from typer.core import TyperArgument, TyperCommand, TyperGroup, TyperOption
from typer.main import get_command


if TYPE_CHECKING:
    from collections.abc import Callable


type Document = dict[str, JsonValue]
ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "packages/long-running-agent"
SOURCE = PACKAGE / "src/long_running_agent"
INVOKE_ARGUMENT_OFFSET = 2
APPLICATIONS: Final[dict[str, typer.Typer]] = {
    "long_running_agent.cli:app": cli.app,
    "long_running_agent.cli:connect_app": cli.connect_app,
    "long_running_agent.runtime.host:app": host.app,
}


class DocumentModel(RootModel[Document]):
    pass


@dataclass(frozen=True)
class DefaultValue:
    kind: str
    value: JsonValue


@dataclass(frozen=True)
class EntrypointAdapter:
    command: TyperCommand | TyperGroup
    default_command: str | None = None


class DefaultParameter(Protocol):
    @property
    def default(self) -> object: ...


class HclDocument(Protocol):
    def serialize(self, options: hcl2.SerializationOptions) -> object: ...


def literal(node: ast.AST, evaluator: Callable[[ast.AST], object] = ast.literal_eval) -> object:
    return evaluator(node)


def is_dictionary(value: object) -> TypeGuard[dict[object, object]]:
    return isinstance(value, dict)


def is_sequence(value: object) -> TypeGuard[list[object] | tuple[object, ...]]:
    return isinstance(value, list | tuple)


def json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | bool | int | float):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Enum):
        return value.name
    if is_sequence(value):
        return [json_value(item) for item in value]
    if is_dictionary(value):
        result: Document = {}
        for key, item in value.items():
            if not isinstance(key, str):
                message = "Documentation source has a non-text mapping key"
                raise TypeError(message)
            result[key] = json_value(item)
        return result
    message = "Documentation defaults must be literals or source-described factories"
    raise TypeError(message)


def read_toml(path: Path) -> Document:
    with path.open("rb") as stream:
        return DocumentModel.model_validate(tomllib.load(stream)).root


def dictionary(value: JsonValue) -> Document:
    if not isinstance(value, dict):
        message = "Documentation source must contain an object"
        raise TypeError(message)
    return value


def array(value: JsonValue) -> list[JsonValue]:
    if not isinstance(value, list):
        message = "Documentation source must contain an array"
        raise TypeError(message)
    return value


def text(value: JsonValue) -> str:
    if not isinstance(value, str):
        message = "Documentation source must contain text"
        raise TypeError(message)
    return value


def module_path(module: str) -> Path:
    return SOURCE.parent / (module.replace(".", "/") + ".py")


def tree(module: str) -> ast.Module:
    return ast.parse(module_path(module).read_text(encoding="utf-8"))


def function(module: ast.Module, name: str) -> ast.FunctionDef:
    return next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == name)


def command_object(value: object) -> TyperCommand | TyperGroup:
    if not isinstance(value, TyperCommand | TyperGroup):
        message = "Entrypoint contains an unsupported command implementation"
        raise TypeError(message)
    return value


def parameter_default(parameter: DefaultParameter) -> DefaultValue:
    value = parameter.default
    if callable(value):
        return DefaultValue("factory", None)
    try:
        return DefaultValue("value", json_value(value))
    except TypeError:
        return DefaultValue("unset", None)


def parameter_reference(parameter: object, context: typer.Context) -> Document:
    if not isinstance(parameter, TyperArgument | TyperOption):
        message = "Entrypoint contains an unsupported parameter implementation"
        raise TypeError(message)
    default = parameter_default(parameter)
    return {
        "name": parameter.name or "",
        "kind": "option" if isinstance(parameter, TyperOption) else "argument",
        "type": parameter.type.name,
        "metavar": parameter.make_metavar(context),
        "help": parameter.help or "",
        "options": json_value(parameter.opts),
        "secondaryOptions": json_value(parameter.secondary_opts),
        "required": parameter.required,
        "isFlag": parameter.is_flag if isinstance(parameter, TyperOption) else False,
        "multiple": parameter.multiple,
        "nargs": parameter.nargs,
        "default": default.value,
        "defaultKind": default.kind,
        "hidden": parameter.hidden,
        "envvar": json_value(parameter.envvar),
    }


def command_reference(command: TyperCommand | TyperGroup, path: list[str]) -> Document:
    context = typer.Context(command, info_name=path[-1])
    children: list[JsonValue] = []
    if isinstance(command, TyperGroup):
        children = [
            command_reference(command_object(child), [*path, name])
            for name, child in command.commands.items()
            if not child.hidden
        ]
    return {
        "name": path[-1],
        "path": json_value(path),
        "help": command.help or "",
        "hidden": command.hidden,
        "deprecated": command.deprecated,
        "parameters": [
            parameter_reference(parameter, context)
            for parameter in command.get_params(context)
            if isinstance(parameter, TyperArgument | TyperOption) and not parameter.hidden and parameter.name != "help"
        ],
        "commands": children,
    }


def adapted_command(source: ast.Module, arguments: ast.expr, command: TyperCommand | TyperGroup) -> EntrypointAdapter:
    if not isinstance(arguments, ast.Call) or not isinstance(arguments.func, ast.Name):
        message = "Entrypoint argument adapter must be source-defined"
        raise TypeError(message)
    helper = function(source, arguments.func.id)
    returned = next(node.value for node in helper.body if isinstance(node, ast.Return))
    if not isinstance(returned, ast.List) or not isinstance(command, TyperGroup):
        message = "Entrypoint argument adapter must return a command prefix"
        raise TypeError(message)
    prefix = literal(returned.elts[0])
    if not isinstance(prefix, str):
        message = "Entrypoint command prefix must be literal text"
        raise TypeError(message)
    fallback = next(node for node in ast.walk(returned) if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or))
    defaults = literal(fallback.values[-1])
    if not is_sequence(defaults) or not all(isinstance(item, str) for item in defaults):
        message = "Entrypoint fallback must contain literal command names"
        raise TypeError(message)
    return EntrypointAdapter(command_object(command.commands[prefix]), " ".join(str(item) for item in defaults))


def adapter(target: str) -> EntrypointAdapter:
    module, name = target.split(":", 1)
    source = tree(module)
    entry = function(source, name)
    invocation = next(
        node
        for node in ast.walk(entry)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "invoke"
    )
    application = invocation.args[0]
    if not isinstance(application, ast.Name):
        message = "Entrypoint must invoke a named source application"
        raise TypeError(message)
    command = command_object(get_command(APPLICATIONS[f"{module}:{application.id}"]))
    if len(invocation.args) > INVOKE_ARGUMENT_OFFSET:
        return adapted_command(source, invocation.args[INVOKE_ARGUMENT_OFFSET], command)
    return EntrypointAdapter(command)


def profile_path(source: ast.Module) -> str:
    relative = next(
        node.right.value
        for node in ast.walk(function(source, "settings_path"))
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    )
    return "~/" + relative


def settings_reference() -> Document:
    source = tree("long_running_agent.config")
    schema = DocumentModel.model_validate(Settings.model_json_schema()).root
    properties = dictionary(schema["properties"])
    return {
        "schema": schema,
        "profilePath": profile_path(source),
        "example": "\n".join(
            name + " = " + json.dumps(array(dictionary(detail)["examples"])[0]) for name, detail in properties.items()
        ),
    }


def hcl_document(document: HclDocument) -> Document:
    return DocumentModel.model_validate(
        document.serialize(
            options=hcl2.SerializationOptions(
                with_comments=False, explicit_blocks=False, preserve_heredocs=False, strip_string_quotes=True
            )
        )
    ).root


def terraform_reference(root: Path) -> Document:
    inputs: list[JsonValue] = []
    outputs: list[JsonValue] = []
    resources: list[JsonValue] = []
    for path in sorted((root / "terraform").glob("*.tf")):
        parsed = hcl_document(hcl2.parses(path.read_text(encoding="utf-8")))
        for block in array(parsed.get("variable", [])):
            for name, value in dictionary(block).items():
                fields = dictionary(value)
                inputs.append(
                    {
                        "name": name,
                        "type": fields.get("type", ""),
                        "description": fields.get("description", ""),
                        "default": fields.get("default"),
                        "required": "default" not in fields,
                        "sensitive": fields.get("sensitive", False),
                    }
                )
        for block in array(parsed.get("output", [])):
            for name, value in dictionary(block).items():
                fields = dictionary(value)
                outputs.append(
                    {
                        "name": name,
                        "description": fields.get("description", ""),
                        "sensitive": fields.get("sensitive", False),
                    }
                )
        for block in array(parsed.get("resource", [])):
            for kind, instances in dictionary(block).items():
                resources.extend(
                    {"address": kind + "." + name, "type": kind, "source": path.relative_to(root).as_posix()}
                    for name in dictionary(instances)
                )
    return {
        "roots": [
            {"name": "terraform", "path": "terraform", "inputs": inputs, "outputs": outputs, "resources": resources}
        ]
    }


def reference(root: Path = ROOT) -> Document:
    if Path(cli.__file__).resolve() != SOURCE / "cli.py":
        message = "Generate documentation with this checkout's locked Python workspace"
        raise RuntimeError(message)
    package_path = PACKAGE.relative_to(ROOT)
    project = dictionary(read_toml(root / package_path / "pyproject.toml")["project"])
    scripts = dictionary(project["scripts"])
    site = DocumentModel.model_validate_json((root / "apps/docs/package.json").read_text(encoding="utf-8")).root
    urls = dictionary(project.get("urls", {}))
    entrypoints: list[JsonValue] = []
    canonical: dict[str, str] = {}
    for name, value in scripts.items():
        target = text(value)
        route = adapter(target)
        if route.command.hidden:
            continue
        entrypoints.append(
            {
                "name": name,
                "target": target,
                "aliasOf": canonical.get(target),
                "defaultCommand": route.default_command,
                "command": command_reference(route.command, [name]),
            }
        )
        canonical.setdefault(target, name)
    settings = settings_reference()
    return {
        "schemaVersion": 2,
        "package": {
            "name": project["name"],
            "version": project["version"],
            "description": project["description"],
            "requiresPython": project["requires-python"],
            "license": project.get("license", ""),
            "repository": urls.get("Repository", ""),
            "site": site["homepage"],
            "dependencies": project.get("dependencies", []),
            "scripts": scripts,
            "install": [f"uv tool install --editable ./{package_path.as_posix()}"],
        },
        "entrypoints": entrypoints,
        "settings": settings,
        "terraform": terraform_reference(root),
    }


def readme(document: Document) -> str:
    package = dictionary(document["package"])
    primary = next(
        dictionary(entry) for entry in array(document["entrypoints"]) if dictionary(entry)["name"] == "sarj-connect"
    )
    command = dictionary(primary["command"])
    groups = [dictionary(group) for group in array(command["commands"])]
    lines = [
        "<!-- Generated by scripts/generate_docs.py; edit source metadata and command help. -->",
        "",
        "# " + text(package["name"]),
        "",
        text(command["help"]),
        "",
        f"[Documentation]({text(package['site'])}) · [Source]({text(package['repository'])})",
        "",
        "```bash",
        *[text(command) for command in array(package["install"])],
        "```",
        "",
        "| Command | Help |",
        "| --- | --- |",
        *[
            "| `"
            + " ".join(text(part) for part in array(group["path"]))
            + "` | "
            + text(group["help"]).replace("|", "\\|").replace("\n", " ")
            + " |"
            for group in groups
        ],
        "",
        "On the VM, `lra codex switch` changes accounts and reopens saved conversations.",
        "Use `lra codex retry enable` to recover temporary model-capacity failures automatically.",
        "",
        f"Version {text(package['version'])} · Python {text(package['requiresPython'])} · {text(package['license'])}",
        "",
    ]
    return "\n".join(lines)


def main(*, check: bool = False, output_directory: Path | None = None) -> None:
    document = reference()
    destination = output_directory or ROOT
    rendered = readme(document)
    readme_path = destination / "README.md"
    if check:
        if not readme_path.is_file() or readme_path.read_text(encoding="utf-8") != rendered:
            typer.echo("README.md is stale; run uv run --locked python scripts/generate_docs.py", err=True)
            raise typer.Exit(1)
        return
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(rendered, encoding="utf-8")
    generated = destination / "apps/docs/src/generated/reference.json"
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    typer.run(main)
