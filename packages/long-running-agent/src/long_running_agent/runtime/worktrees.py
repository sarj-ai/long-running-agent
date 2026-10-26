from __future__ import annotations

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping

import codecs
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Annotated, Final, Literal
import warnings

import typer

from long_running_agent.process import run

from .settings import RuntimePaths


PRUNED_DIRECTORIES: Final = frozenset(
    {
        ".git",
        "node_modules",
        "__pycache__",
        ".cache",
        ".venv",
        "venv",
        ".npm",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)
MAX_DEPTH: Final = 12
MAX_ENTRIES: Final = 50000
TEMPORARY_ROOT: Final = Path("/tmp")  # ruff: ignore[hardcoded-temp-file] -- This guard intentionally inventories the system boot-cleaned root, never creates a predictable temporary file.
PRINTABLE_FIRST: Final = ord("!")
PRINTABLE_LAST: Final = ord("~")
ESCAPED_BYTES: Final = frozenset(b"\"'\\")


@dataclass(frozen=True)
class EntryClassification:
    kind: Literal["skip", "worktree", "directory"]
    path: Path


@dataclass(frozen=True)
class PendingDirectory:
    path: Path
    depth: int


@dataclass(frozen=True)
class WorktreeInventory:
    paths: list[Path]
    unsafe: list[Path]


@dataclass(frozen=True)
class BootAlias:
    source: str
    target: str


def directory_entries(directory: Path, uid: int, root: Path) -> Iterator[os.DirEntry[str]]:
    try:
        with os.scandir(directory) as entries:
            yield from entries
    except PermissionError as error:
        if directory == root or directory.stat().st_uid == uid:
            msg = f"Temporary worktree inventory could not inspect {directory}; shutdown is unsafe"
            raise RuntimeError(msg) from error
        # Other users' private directories are outside this user's scope.
    except OSError as error:
        msg = f"Temporary worktree inventory could not inspect {directory}; shutdown is unsafe"
        raise RuntimeError(msg) from error


def owned_durable_alias(path: Path, home: Path, uid: int) -> bool:
    try:
        target = path.resolve(strict=True)
    except FileNotFoundError:
        return False
    return (
        target.is_relative_to(home) and target.is_dir() and target.stat().st_uid == uid and (target / ".git").exists()
    )


def classify_entry(entry: os.DirEntry[str], home: Path, uid: int) -> EntryClassification:
    path = Path(entry.path)
    if entry.name in PRUNED_DIRECTORIES:
        return EntryClassification("skip", path)
    try:
        if entry.is_symlink():
            # Inspect durable aliases, never traverse a link into another tree.
            if owned_durable_alias(path, home, uid):
                return EntryClassification("worktree", path)
            return EntryClassification("skip", path)
        if entry.is_dir(follow_symlinks=False):
            # Terraform's generated provider binary cache is not source. Keep
            # modules and neighboring trees in scope, including a Git checkout
            # deliberately placed at the provider-cache directory itself.
            if path.name == "providers" and path.parent.name == ".terraform" and not (path / ".git").exists():
                return EntryClassification("skip", path)
            return EntryClassification("directory", path)
        return EntryClassification("skip", path)
    except OSError as error:
        msg = f"Temporary worktree inventory could not inspect {path}; shutdown is unsafe"
        raise RuntimeError(msg) from error


def discover(
    root: Path = TEMPORARY_ROOT,
    uid: int | None = None,
    *,
    home: Path | None = None,
    max_depth: int = MAX_DEPTH,
    max_entries: int = MAX_ENTRIES,
    rules: Mapping[str, str] | None = None,
) -> list[Path]:
    uid = os.getuid() if uid is None else uid
    home = (Path.home() if home is None else Path(home)).resolve()
    root = Path(root)
    # Boot registrations remain inventory even when their alias is missing or
    # broken. Otherwise an unsuccessful boot could turn a protected worktree
    # into a misleading zero-worktree PASS.
    rules = boot_aliases(home=home, uid=uid) if rules is None else rules
    result = set(registered_paths(rules, root, home))
    result.update(scan_temporary_worktrees(root, home, uid, max_depth, max_entries))
    return sorted(result)


def scan_temporary_worktrees(root: Path, home: Path, uid: int, max_depth: int, max_entries: int) -> set[Path]:
    result: set[Path] = set()
    pending = [PendingDirectory(root, 0)]
    inspected = 0
    while pending:
        current = pending.pop()
        directory, depth = current.path, current.depth
        if depth > max_depth:
            msg = f"Temporary worktree inventory exceeded its depth limit ({max_depth}) at {directory}; shutdown is unsafe"
            raise RuntimeError(msg)
        if directory != root and (directory / ".git").exists():
            if directory.stat().st_uid == uid:
                result.add(directory)
            continue
        for entry in directory_entries(directory, uid, root):
            inspected += 1
            if inspected > max_entries:
                msg = f"Temporary worktree inventory exceeded its entry limit ({max_entries}) at {entry.path}; shutdown is unsafe"
                raise RuntimeError(msg)
            classified = classify_entry(entry, home, uid)
            if classified.kind == "worktree":
                result.add(classified.path)
            elif classified.kind == "directory":
                pending.append(PendingDirectory(classified.path, depth + 1))
    return result


def quote(value: str | Path) -> str:
    return '"' + escape_path(value) + '"'


def escape_path(value: str | Path) -> str:
    # The seventh tmpfiles field is the unquoted remainder of the line, not a
    # shell word. Byte escapes preserve arbitrary filesystem names and avoid
    # whitespace, quote and backslash ambiguity in either kind of field.
    result: list[str] = []
    for byte in os.fsencode(value):
        if byte == 0:
            msg = "Worktree paths must not contain NUL"
            raise ValueError(msg)
        if byte == ord("%"):
            result.append("%%")
        elif PRINTABLE_FIRST <= byte <= PRINTABLE_LAST and byte not in ESCAPED_BYTES:
            result.append(chr(byte))
        else:
            result.append(f"\\x{byte:02x}")
    return "".join(result)


def configuration(aliases: Mapping[str, str], *, root: Path = TEMPORARY_ROOT) -> str:
    lines = ["# Actual Git files live under the user home, outside boot cleanup."]
    parents: set[Path] = set()
    for source in aliases:
        path = Path(source)
        if not path.is_relative_to(root) or path == root:
            msg = "Worktree aliases must stay under the temporary directory root"
            raise RuntimeError(msg)
        for parent in path.parents:
            if parent == root:
                break
            parents.add(parent)
    # tmpfiles recreates missing parents as root unless their ownership is
    # explicit. Preserve each original parent's permissions and owner.
    for parent in sorted(parents, key=lambda item: (len(item.parts), str(item))):
        metadata = parent.stat()
        if parent.is_symlink():
            msg = "Worktree alias parents must not be symlinks"
            raise RuntimeError(msg)
        lines.append(f"d {quote(parent)} {stat.S_IMODE(metadata.st_mode):04o} {metadata.st_uid} {metadata.st_gid} -")
    for source, target in sorted(aliases.items()):
        lines.append("L+ " + quote(source) + " - - - - " + escape_path(target))
    return "\n".join(lines) + "\n"


def durable_target(path: Path, base: Path) -> Path:
    digest = hashlib.sha256(os.fsencode(path.absolute())).hexdigest()[:16]
    return base / f"{path.name}-{digest}"


def preserve(path: Path, base: Path, *, home: Path | None = None) -> Path:
    home = (Path.home() if home is None else Path(home)).resolve()
    if path.is_symlink():
        target = path.resolve(strict=True)
        if not target.is_relative_to(home):
            msg = "Existing worktree alias must point under the user home"
            raise RuntimeError(msg)
        return target
    target = durable_target(path, base)
    if target.exists() or target.is_symlink():
        msg = "Durable worktree destination already exists; refusing to replace it"
        raise RuntimeError(msg)
    # Atomic rename keeps all tracked, unstaged, untracked, ignored and open
    # files. EXDEV fails safely before changing anything rather than silently
    # making an inconsistent live copy. The original Git backlink stays valid
    # through the restored alias; no Git index or branch is changed.
    path.rename(target)
    try:
        path.symlink_to(target, target_is_directory=True)
    except BaseException:
        target.rename(path)
        raise
    return target


def unescape_path(value: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        decoded = os.fsdecode(codecs.escape_decode(os.fsencode(value))[0])
    # Only a doubled percent is literal in a boot policy; accepting unresolved
    # specifiers would certify a different target from the one tmpfiles uses.
    if re.search(r"%(?!%)", decoded.replace("%%", "")):
        msg = "Worktree boot paths must escape percent specifiers"
        raise ValueError(msg)
    return decoded.replace("%%", "%")


def alias_rule(line: str) -> BootAlias | None:
    # tmpfiles permits quotes on the first six fields only. In particular,
    # stripping quotes from the last argument would hide a broken boot rule.
    fields = re.match(
        r"""^\s*(L\+)\s+("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\S+)\s+\S+\s+\S+\s+\S+\s+\S+\s+(.+)$""",
        line,
    )
    if fields is None:
        if line.lstrip().startswith("L+"):
            msg = "Malformed worktree boot alias"
            raise ValueError(msg)
        return None
    source, target = fields[2], fields[3]
    if source.startswith(('"', "'")):
        source = source[1:-1]
    source, target = unescape_path(source), unescape_path(target)
    if not Path(source).is_absolute() or not Path(target).is_absolute():
        msg = "Worktree boot aliases require absolute unquoted targets"
        raise ValueError(msg)
    return BootAlias(source, target)


def parse_boot_policy(text: str) -> dict[str, str]:
    rules: dict[str, str] = {}
    for line in text.splitlines():
        parsed = alias_rule(line)
        if parsed is None:
            continue
        if parsed.source in rules and rules[parsed.source] != parsed.target:
            message = "Conflicting worktree boot aliases"
            raise ValueError(message)
        rules[parsed.source] = parsed.target
    return rules


def read_boot_policy(candidate: Path) -> dict[str, str]:
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return {}
    if metadata.st_uid != 0 or metadata.st_mode & 0o022 or not stat.S_ISREG(metadata.st_mode):
        msg = f"Untrusted worktree boot policy {candidate}; shutdown is unsafe"
        raise RuntimeError(msg)
    try:
        rules = parse_boot_policy(candidate.read_text(encoding="utf-8"))
    except (OSError, ValueError, DeprecationWarning) as error:
        msg = f"Invalid worktree boot policy {candidate}; shutdown is unsafe"
        raise RuntimeError(msg) from error
    return rules


def boot_aliases(path: Path | None = None, *, home: Path | None = None, uid: int | None = None) -> dict[str, str]:
    uid = os.getuid() if uid is None else uid
    home = (Path.home() if home is None else Path(home)).resolve()
    paths = (
        [Path(path)]
        if path is not None
        else [
            Path("/etc/tmpfiles.d/sarj-preserve-worktrees.conf"),
            Path(f"/etc/tmpfiles.d/sarj-preserve-worktrees-{uid}.conf"),
        ]
    )
    rules: dict[str, str] = {}
    for candidate in paths:
        for source, target in read_boot_policy(candidate).items():
            if not Path(target).is_relative_to(home):
                if path is not None or candidate.name.endswith(f"-{uid}.conf"):
                    msg = "Worktree boot policy escapes the user home; shutdown is unsafe"
                    raise RuntimeError(msg)
                continue  # A legacy shared policy may also contain peers' aliases.
            if source in rules and rules[source] != target:
                msg = "Conflicting trusted worktree boot aliases; shutdown is unsafe"
                raise RuntimeError(msg)
            rules[source] = target
    return rules


def registered_paths(rules: Mapping[str, str], root: Path, home: Path) -> Iterator[Path]:
    for source_text, target_text in rules.items():
        source, target = Path(source_text), Path(target_text)
        if not source.is_absolute() or not target.is_absolute():
            msg = "Invalid registered worktree path; shutdown is unsafe"
            raise RuntimeError(msg)
        if source != root and source.is_relative_to(root) and target.is_relative_to(home):
            yield source


def unsafe_paths(
    paths: Iterable[Path], *, rules: Mapping[str, str] | None = None, home: Path | None = None
) -> list[Path]:
    rules = boot_aliases() if rules is None else rules
    home = (Path.home() if home is None else Path(home)).resolve()
    result: list[Path] = []
    for path in paths:
        try:
            target = path.resolve(strict=True)
            configured = rules.get(str(path))
            safe = (
                path.is_symlink()
                and target.is_relative_to(home)
                and target.is_dir()
                and target.stat().st_uid == os.getuid()
                and (target / ".git").exists()
                and configured is not None
                and Path(configured).resolve(strict=True) == target
            )
        except OSError:
            safe = False
        if not safe:
            result.append(path)
    return result


app = typer.Typer(help="Preserve temporary Git worktrees across VM shutdowns.", no_args_is_help=True)


def inventory(root: Path = TEMPORARY_ROOT) -> WorktreeInventory:
    rules = boot_aliases()
    paths = discover(root, rules=rules)
    return WorktreeInventory(paths, unsafe_paths(paths, rules=rules))


@app.command("check")
def check_worktrees(
    root: Annotated[
        Path, typer.Option(help="Temporary directory to inspect without modifying files.")
    ] = TEMPORARY_ROOT,
) -> None:
    """Fail closed if temporary worktrees lack durable files and valid boot aliases."""
    result = inventory(root)
    typer.echo(
        json.dumps(
            {
                "inventory_complete": True,
                "tmp_git_worktrees": len(result.paths),
                "durable_worktrees": len(result.paths) - len(result.unsafe),
                "unsafe_worktrees": len(result.unsafe),
            }
        )
    )
    if result.unsafe:
        raise typer.Exit(1)


@app.command("preserve")
def preserve_worktrees() -> None:
    """Atomically move temporary Git worktrees to your home and install boot aliases."""
    # Confirm installation authority before moving any live paths. Ordinary
    # shared users need an administrator to arrange their boot registrations.
    run(["sudo", "-n", "true"])
    paths = inventory().paths
    base = RuntimePaths.current().code / "worktrees/preserved-tmp"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    aliases = {str(path): str(preserve(path, base)) for path in paths}
    destination = f"/etc/tmpfiles.d/sarj-preserve-worktrees-{os.getuid()}.conf"
    with tempfile.TemporaryDirectory(prefix="lra-worktrees-") as folder:
        policy = Path(folder) / "worktrees.conf"
        policy.write_text(configuration(aliases))
        run(["sudo", "-n", "install", "-o", "root", "-g", "root", "-m", "0644", str(policy), destination])
    if unsafe_paths(paths):
        msg = "Installed boot aliases did not validate"
        raise RuntimeError(msg)
    typer.echo(f"Protected {len(paths)} temporary Git worktrees.")
