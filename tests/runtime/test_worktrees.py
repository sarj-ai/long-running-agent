import hashlib
from pathlib import Path
import shutil

from long_running_agent.process import run
from long_running_agent.runtime import worktrees
import pytest


def git(folder: Path, *arguments: str) -> str:
    return run(["git", "-C", str(folder), *arguments], capture_output=True).stdout.strip()


def test_nested_worktree_move_preserves_git_index_unsaved_and_untracked_files(tmp_path: Path) -> None:
    home = tmp_path / "home"
    repository = home / "repository"
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@example.invalid")
    (repository / "source.txt").write_text("committed\n")
    git(repository, "add", ".")
    git(repository, "-c", "commit.gpgsign=false", "commit", "-qm", "fixture")
    volatile = tmp_path / "volatile"
    source = volatile / "nested/worktree"
    source.parent.mkdir(parents=True)
    git(repository, "worktree", "add", "-qb", "fixture", str(source))
    (source / "source.txt").write_text("staged\n")
    git(source, "add", "source.txt")
    (source / "source.txt").write_text("unsaved\n")
    (source / "untracked.txt").write_text("untracked\n")
    metadata = Path(git(source, "rev-parse", "--absolute-git-dir"))
    before = hashlib.sha256((metadata / "index").read_bytes()).hexdigest()
    durable = home / "durable"
    durable.mkdir()
    target = worktrees.preserve(source, durable, home=home)
    rules = worktrees.parse_boot_policy(worktrees.configuration({str(source): str(target)}, root=volatile))
    assert worktrees.discover(volatile, home=home, rules=rules) == [source]
    assert worktrees.unsafe_paths([source], rules=rules, home=home) == []
    assert hashlib.sha256((metadata / "index").read_bytes()).hexdigest() == before
    assert (source / "source.txt").read_text() == "unsaved\n"
    assert (source / "untracked.txt").read_text() == "untracked\n"
    source.unlink()
    assert worktrees.discover(volatile, home=home, rules=rules) == [source]
    assert worktrees.unsafe_paths([source], rules=rules, home=home) == [source]
    assert (target / "source.txt").read_text() == "unsaved\n"


def test_policy_parser_rejects_historical_quoted_final_targets(tmp_path: Path) -> None:
    source = tmp_path / "source space%quote"
    target = tmp_path / 'target space%"\\quote'
    correct = worktrees.configuration({str(source): str(target)}, root=tmp_path)
    assert worktrees.parse_boot_policy(correct) == {str(source): str(target)}
    broken = "L+ " + worktrees.quote(source) + " - - - - " + worktrees.quote(target)
    with pytest.raises(ValueError, match="unquoted targets"):
        worktrees.parse_boot_policy(broken)


def test_provider_binary_cache_is_pruned_while_real_neighbor_and_module_worktrees_remain_visible(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    repository = home / "repository"
    repository.mkdir(parents=True)
    git(repository, "init", "-q")
    git(repository, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-qm", "fixture")
    temporary = tmp_path / "volatile"
    generated = temporary / "test-fixture/.terraform/providers/registry.terraform.io/hashicorp/google/7.46.1/linux_amd64"
    generated.mkdir(parents=True)
    (generated / "terraform-provider-google").write_bytes(b"generated binary fixture")
    neighbors = [temporary / "test-fixture/source", temporary / "test-fixture/.terraform/modules/source"]
    for index, source in enumerate(neighbors):
        git(repository, "worktree", "add", "-qb", f"fixture-{index}", str(source))
        (source / "unsaved.txt").write_text("ongoing work\n", encoding="utf-8")
    before = {source: (source.stat().st_ino, (source / "unsaved.txt").read_bytes()) for source in neighbors}
    assert worktrees.discover(temporary, home=home, max_depth=4, rules={}) == sorted(neighbors)
    assert worktrees.unsafe_paths(neighbors, rules={}, home=home) == neighbors
    assert {source: (source.stat().st_ino, (source / "unsaved.txt").read_bytes()) for source in neighbors} == before
    assert (generated / "terraform-provider-google").read_bytes() == b"generated binary fixture"


def test_provider_directory_with_its_own_git_marker_is_never_pruned(tmp_path: Path) -> None:
    temporary = tmp_path / "volatile"
    source = temporary / ".terraform/providers"
    source.mkdir(parents=True)
    git(source, "init", "-q")
    assert worktrees.discover(temporary, home=tmp_path, rules={}) == [source]


def test_arbitrary_deep_source_still_fails_closed_with_the_exact_blocked_path(tmp_path: Path) -> None:
    source = tmp_path / "source/nested/deep"
    source.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="depth limit \\(2\\)") as caught:
        worktrees.discover(tmp_path, home=tmp_path, max_depth=2, rules={})
    assert str(source) in str(caught.value)
    assert "shutdown is unsafe" in str(caught.value)


def test_entry_limit_still_fails_closed_with_actionable_location(tmp_path: Path) -> None:
    for name in ("first", "second"):
        (tmp_path / name).write_text("fixture\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="entry limit \\(1\\)") as caught:
        worktrees.discover(tmp_path, home=tmp_path, max_entries=1, rules={})
    assert str(tmp_path) in str(caught.value)
    assert "shutdown is unsafe" in str(caught.value)


def test_missing_registered_alias_and_missing_durable_target_remain_unsafe(tmp_path: Path) -> None:
    temporary = tmp_path / "volatile"
    temporary.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    source = temporary / "lost-alias"
    rules = {str(source): str(home / "missing-durable-source")}
    assert worktrees.discover(temporary, home=home, rules=rules) == [source]
    assert worktrees.unsafe_paths([source], home=home, rules=rules) == [source]


def test_conflicting_boot_alias_targets_never_become_a_durability_pass(tmp_path: Path) -> None:
    source = tmp_path / "alias"
    lines = [
        "L+ " + worktrees.quote(source) + " - - - - " + worktrees.escape_path(tmp_path / name)
        for name in ("first-target", "second-target")
    ]
    with pytest.raises(ValueError, match="Conflicting worktree boot aliases"):
        worktrees.parse_boot_policy("\n".join(lines))


def test_unresolved_systemd_percent_specifier_is_not_certified_as_a_literal_target(tmp_path: Path) -> None:
    line = "L+ " + worktrees.quote(tmp_path / "alias") + " - - - - " + str(tmp_path / "home/%u")
    with pytest.raises(ValueError, match="escape percent specifiers"):
        worktrees.parse_boot_policy(line)


def test_writable_boot_policy_fails_before_its_aliases_can_be_trusted(tmp_path: Path) -> None:
    policy = tmp_path / "worktrees.conf"
    policy.write_text("# a writable policy cannot certify shutdown\n", encoding="utf-8")
    policy.chmod(0o666)
    with pytest.raises(RuntimeError, match="Untrusted worktree boot policy"):
        worktrees.read_boot_policy(policy)


def test_alias_configuration_rejects_a_symlink_parent(tmp_path: Path) -> None:
    directory = tmp_path / "real-directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)
    with pytest.raises(RuntimeError, match="parents must not be symlinks"):
        worktrees.configuration({str(link / "alias"): str(tmp_path / "durable")}, root=tmp_path)


def test_equal_basename_sources_keep_distinct_stable_durable_destinations(tmp_path: Path) -> None:
    base = tmp_path / "durable"
    first = worktrees.durable_target(tmp_path / "first/source", base)
    second = worktrees.durable_target(tmp_path / "second/source", base)
    assert first != second
    assert first == worktrees.durable_target(tmp_path / "first/source", base)
    assert first.parent == second.parent == base


def test_directory_symlink_cycle_is_never_traversed(tmp_path: Path) -> None:
    temporary = tmp_path / "volatile"
    temporary.mkdir()
    (temporary / "cycle").symlink_to(temporary, target_is_directory=True)
    assert worktrees.discover(temporary, home=tmp_path, max_depth=1, max_entries=1, rules={}) == []


@pytest.mark.skipif(shutil.which("systemd-tmpfiles") is None, reason="requires native systemd-tmpfiles")
def test_actual_generated_boot_rules_recreate_exact_alias_and_preserve_data(tmp_path: Path) -> None:
    virtual_source = Path('/tmp/work space%"\\quote')  # ruff: ignore[hardcoded-temp-file] -- Virtual path is interpreted only beneath systemd-tmpfiles --root fixture.
    virtual_target = Path('/home/fixture/work space%"\\quote')
    temporary = tmp_path / "tmp"
    temporary.mkdir()
    durable = tmp_path / virtual_target.relative_to("/")
    durable.mkdir(parents=True)
    payload = durable / "source.txt"
    payload.write_bytes(b"unsaved developer work\n")
    before = hashlib.sha256(payload.read_bytes()).hexdigest()
    alias = tmp_path / virtual_source.relative_to("/")
    alias.symlink_to(virtual_target, target_is_directory=True)
    configuration = tmp_path / "etc/tmpfiles.d/regression.conf"
    configuration.parent.mkdir(parents=True)
    configuration.write_text(
        "D /tmp 1777 - - -\n" + worktrees.configuration({str(virtual_source): str(virtual_target)})
    )
    command = ["systemd-tmpfiles", f"--root={tmp_path}", "--boot"]
    run([*command, "--remove"], capture_output=True, timeout=15)
    assert not alias.is_symlink()
    run([*command, "--create"], capture_output=True, timeout=15)
    assert alias.readlink() == virtual_target
    assert hashlib.sha256(payload.read_bytes()).hexdigest() == before
