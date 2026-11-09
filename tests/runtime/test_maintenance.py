import ast
from importlib.resources import files
import json
from pathlib import Path
import subprocess
import sys

from long_running_agent.runtime import maintenance, maintenance_worker
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
    executable = Path("/usr/bin/true").resolve(strict=True)
    assert maintenance.validated_worker(executable) == executable


@pytest.mark.parametrize("action", ["patch", "release"])
def test_isolated_maintenance_transport_preserves_the_window_guard(action: str) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(Path(maintenance_worker.__file__)), "/dev/stdin"],
        input=json.dumps({"action": action, "maintenance_window": False}),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "No changes made" in result.stderr


def test_maintenance_file_request_preserves_attended_standard_input(tmp_path: Path) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"action": "release", "maintenance_window": True}), encoding="utf-8")
    program = (
        "import sys\nfrom pathlib import Path\n"
        "from long_running_agent.runtime.maintenance_worker import read_request\n"
        "request = read_request(Path(sys.argv[1]))\n"
        "assert request.action == 'release' and request.maintenance_window\n"
        "print(sys.stdin.read(), end='')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(path)],
        input="attended-answer\n",
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "attended-answer\n"


@pytest.mark.parametrize(
    ("payload", "error_type"),
    [
        pytest.param({"action": "patch", "maintenance_window": "true"}, TypeError, id="string-window"),
        pytest.param({"action": "policy", "maintenance_window": True}, ValueError, id="policy-options"),
        pytest.param({"action": "release", "check_release": True}, ValueError, id="release-audit-option"),
        pytest.param({"action": "unsupported-private-value"}, ValueError, id="unknown-action"),
    ],
)
def test_maintenance_request_rejects_wrong_types_and_cross_action_options(
    tmp_path: Path,
    payload: dict[str, str | bool],
    error_type: type[TypeError | ValueError],
) -> None:
    path = tmp_path / "request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(error_type, match=r"Expected a boolean|Unsupported isolated maintenance"):
        maintenance_worker.read_request(path)
