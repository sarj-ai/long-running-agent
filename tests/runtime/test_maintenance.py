import ast
from importlib.resources import files
from pathlib import Path

from long_running_agent.runtime import maintenance
from long_running_agent.runtime.maintenance_worker import patch_packages, upgrade_release
import pytest


def test_root_worker_remains_stdlib_python312_and_rejects_unapproved_mutations() -> None:
    source = files("long_running_agent.runtime").joinpath("maintenance_worker.py").read_text()
    tree = ast.parse(source, feature_version=(3, 12))
    imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert not any(name and name.startswith(("long_running_agent", "typer", "pydantic")) for name in imports)
    with pytest.raises(RuntimeError, match="No changes made"):
        patch_packages(maintenance_window=False)
    with pytest.raises(RuntimeError, match="No changes made"):
        upgrade_release(maintenance_window=False)


@pytest.mark.parametrize("symlink", [False, True], ids=["user-owned-file", "symlink-parent"])
def test_privileged_maintenance_refuses_an_unsafe_parent_before_inspecting_or_executing_worker(
    tmp_path: Path, *, symlink: bool
) -> None:
    if symlink:
        directory = tmp_path / "user-controlled-parent"
        directory.symlink_to("/usr/bin", target_is_directory=True)
        worker = directory / "true"
        assert worker.stat().st_uid == 0
    else:
        worker = tmp_path / "worker.py"
        worker.write_text("# fixture worker must never run\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="root-owned, non-writable managed path"):
        maintenance.validated_worker(worker)


def test_privileged_worker_validation_accepts_a_regular_root_owned_system_path() -> None:
    executable = Path("/usr/bin/true")
    assert maintenance.validated_worker(executable) == executable
