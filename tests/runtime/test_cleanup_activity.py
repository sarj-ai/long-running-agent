from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import TYPE_CHECKING
from uuid import uuid7

from long_running_agent.runtime import cleanup_activity as activity
import pytest


if TYPE_CHECKING:
    from collections.abc import Sequence


@dataclass(frozen=True)
class SavedThread:
    cwd: str
    archived: bool = False
    source: str = "cli"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    directory = tmp_path / "home"
    directory.mkdir(mode=0o700)
    return directory


def native_database(store: Path, threads: Sequence[SavedThread] = ()) -> Path:
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    database = store / "state_5.sqlite"
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(  # sarj-noqa: SARJ429 -- Bootstrap the external native Codex metadata schema; cleanup has no database writer API.
            "CREATE TABLE threads (id TEXT PRIMARY KEY,cwd TEXT NOT NULL,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,archived INTEGER NOT NULL,source TEXT NOT NULL);"
            "CREATE TABLE project_roots (project_id TEXT NOT NULL,position INTEGER NOT NULL,path TEXT NOT NULL);"
            "CREATE TABLE backfill_state (id INTEGER PRIMARY KEY,status TEXT NOT NULL);"
            "INSERT INTO backfill_state VALUES (1,'complete');"
            "CREATE TABLE rollout_migration_skipped_rollouts (migration_id TEXT NOT NULL,rollout_path TEXT NOT NULL);"
        )
        connection.executemany(  # sarj-noqa: SARJ429 -- Seed only external thread path/archive/source metadata, never conversation or authentication content.
            "INSERT INTO threads VALUES (:id,:cwd,1,1,:archived,:source)",
            [
                {"id": str(uuid7()), "cwd": thread.cwd, "archived": int(thread.archived), "source": thread.source}
                for thread in threads
            ],
        )
        connection.commit()
    return database


def registry_record(home: Path, store: Path, *, uid: int | None = None, agent: str = "codex") -> Path:
    directory = home / ".local/share/long-running-agent/activity"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    identifier = str(uuid7())
    path = directory / f"{identifier}.json"
    path.write_text(
        json.dumps({"id": identifier, "uid": os.getuid() if uid is None else uid, "agent": agent, "store": str(store)})
    )
    path.chmod(0o600)
    return path


def test_all_saved_threads_and_project_roots_protect_paths(home: Path) -> None:
    active = home / "code/active"
    archived = home / "code/archived"
    helper = home / "code/helper"
    project = home / "code/project"
    database = native_database(
        home / ".codex",
        [
            SavedThread(str(active)),
            SavedThread(str(archived), archived=True),
            SavedThread(str(helper), source="subAgent"),
        ],
    )
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Seed the external native project-root metadata boundary, without conversation data.
            "INSERT INTO project_roots VALUES ('fixture',0,?)", (str(project),)
        )
        connection.commit()
    result = activity.collect(home)
    assert result.paths == frozenset({active, archived, helper, project})
    assert result.homes == 1
    assert result.databases == 1


def test_managed_and_registered_homes_are_combined_and_database_aliases_deduplicated(home: Path) -> None:
    default = home / ".codex"
    explicit = home / "custom-codex"
    registered = home / "registered-codex"
    profiles = home / ".local/share/long-running-agent/account-switch"
    profile = profiles / "fixture-profile"
    sources = [default, explicit, registered, profile]
    paths = [home / f"code/work-{index}" for index in range(len(sources))]
    for store, path in zip(sources, paths, strict=True):
        native_database(store, [SavedThread(str(path))])
    profiles.chmod(0o700)
    backups = profiles / "backups"
    backups.mkdir(mode=0o700)
    (backups / "state_5.sqlite").write_text("must never inspect backup state\n")
    registry_record(home, registered)
    result = activity.collect(home, codex_home=explicit)
    assert result.paths == frozenset(paths)
    assert result.homes == 4
    assert result.databases == 4
    assert set(sources) | {default / "state_5.sqlite"} <= result.storage


@pytest.mark.parametrize(
    ("agent", "database_count"),
    [pytest.param("codex", 1, id="native-codex-metadata"), pytest.param("claude", 0, id="claude-path-only")],
)
def test_registered_harness_storage_is_protected_independently_of_saved_workspaces(
    home: Path, agent: str, database_count: int
) -> None:
    store = home / "code/worktrees/dormant/node_modules/private-harness"
    elsewhere = home / "code/unrelated-workspace"
    if agent == "codex":
        native_database(store, [SavedThread(str(elsewhere), archived=True)])
    else:
        store.mkdir(parents=True, mode=0o700)
        database = store / "state_5.sqlite"
        database.write_bytes(b"not a Codex database; never inspect a Claude store\n")
        database.chmod(0)
    for name in ("auth.json", "history.jsonl"):
        path = store / name
        path.write_bytes(b"synthetic private data; never open\n")
        path.chmod(0)
    registry_record(home, store, agent=agent)
    result = activity.collect(home)
    assert store in result.storage
    assert result.paths == (frozenset({elsewhere}) if agent == "codex" else frozenset())
    assert result.homes == result.databases == database_count
    assert (store / "auth.json").stat().st_mode & 0o777 == 0
    assert (store / "history.jsonl").stat().st_mode & 0o777 == 0


@pytest.mark.parametrize(
    "alias", [pytest.param("directory", id="sqlite-home-alias"), pytest.param("database", id="native-database-alias")]
)
def test_sqlite_storage_preserves_declared_and_resolved_paths_when_cwd_is_elsewhere(home: Path, alias: str) -> None:
    store = home / ".codex"
    store.mkdir(mode=0o700)
    elsewhere = home / "code/unrelated-workspace"
    database = native_database(home / "private-databases", [SavedThread(str(elsewhere), archived=True)])
    match alias:
        case "directory":
            declared = home / "code/worktrees/dormant/node_modules/sqlite-alias"
            declared.parent.mkdir(parents=True)
            declared.symlink_to(database.parent, target_is_directory=True)
            (store / "config.toml").write_text(f"sqlite_home = {json.dumps(str(declared))}\n")
            target = database.parent
        case "database":
            declared = store / database.name
            declared.symlink_to(database)
            target = database
        case _:
            pytest.fail("Unknown native SQLite storage alias fixture")
    result = activity.collect(home)
    assert {store, declared, target, database} <= result.storage
    assert result.paths == frozenset({elsewhere})
    assert result.homes == result.databases == 1


@pytest.mark.parametrize(
    "unsafe",
    [
        pytest.param("missing", id="missing-registered-claude"),
        pytest.param("writable", id="writable-registered-claude"),
    ],
)
def test_registered_non_codex_storage_unknowns_fail_closed(home: Path, unsafe: str) -> None:
    store = home / "registered-claude"
    registry_record(home, store, agent="claude")
    if unsafe == "missing":
        with pytest.raises(FileNotFoundError, match="registered-claude"):
            activity.collect(home)
    else:
        store.mkdir(mode=0o770)
        store.chmod(0o770)
        with pytest.raises(RuntimeError, match="unsafe type or permissions"):
            activity.collect(home)


def test_custom_sqlite_storage_uses_only_the_native_storage_key(home: Path) -> None:
    store = home / ".codex"
    store.mkdir(mode=0o700)
    database_home = home / "sqlite storage"
    workspace = home / "code/retained"
    native_database(database_home, [SavedThread(str(workspace))])
    (store / "config.toml").write_text(
        'unrelated = "this value does not define a SQLite storage root"\n'
        f"sqlite_home = {json.dumps(str(database_home))}\n"
    )
    assert activity.collect(home).paths == frozenset({workspace})


def test_nested_tables_and_arrays_preserve_all_configured_sqlite_storage(home: Path) -> None:
    store = home / ".codex"
    default = home / "code/default"
    nested = home / "code/nested-profile"
    array = home / "code/array-profile"
    native_database(store, [SavedThread(str(default))])
    nested_store, array_store = home / "nested-sqlite", home / "array-sqlite"
    native_database(nested_store, [SavedThread(str(nested), archived=True)])
    native_database(array_store, [SavedThread(str(array))])
    (store / "config.toml").write_text(
        "[profiles.fixture]\n"
        f"sqlite_home = {json.dumps(str(nested_store))}\n"
        "[[providers]]\n"
        f"sqlite_home = {json.dumps(str(array_store))}\n"
    )
    result = activity.collect(home)
    assert result.paths == frozenset({default, nested, array})
    assert result.databases == 3


def test_multiline_instruction_strings_cannot_invent_sqlite_storage_keys(home: Path) -> None:
    store = home / ".codex"
    workspace = home / "code/retained"
    native_database(store, [SavedThread(str(workspace))])
    (store / "config.toml").write_text(
        f'instructions = """\nsqlite_home = "{home / "not-a-configured-database"}"\n"""\n'
    )
    result = activity.collect(home)
    assert result.paths == frozenset({workspace})
    assert result.databases == 1


def test_native_multiline_sqlite_path_is_protected(home: Path) -> None:
    store = home / ".codex"
    store.mkdir()
    custom = home / "multiline-sqlite"
    workspace = home / "code/multiline-retained"
    native_database(custom, [SavedThread(str(workspace), archived=True)])
    (store / "config.toml").write_text(f'sqlite_home = """\n{custom}"""\n')
    assert activity.collect(home).paths == frozenset({workspace})


def test_native_config_table_traversal_shares_the_metadata_entry_budget(home: Path) -> None:
    store = home / ".codex"
    native_database(store)
    (store / "config.toml").write_text("".join(f"fixture_{index} = 0\n" for index in range(activity.MAX_ENTRIES)))
    with pytest.raises(RuntimeError, match="directory-entry limit"):
        activity.collect(home)


def test_invalid_native_toml_fails_closed_with_sanitized_error(home: Path) -> None:
    store = home / ".codex"
    native_database(store)
    (store / "config.toml").write_text('private_fixture = "unterminated\n')
    with pytest.raises(RuntimeError, match="storage configuration is invalid") as failure:
        activity.collect(home)
    assert "private_fixture" not in str(failure.value)


def test_committed_wal_metadata_is_visible_without_checkpointing_or_writing(home: Path) -> None:
    store = home / ".codex"
    database = native_database(store)
    workspace = home / "code/only-in-wal"
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute(  # sarj-noqa: SARJ429 -- Leave committed native metadata in a live WAL to verify the read-only collector sees it.
            "INSERT INTO threads VALUES ('wal-fixture',?,1,1,0,'cli')", (str(workspace),)
        )
        writer.commit()
        baseline = database.read_bytes()
        assert database.with_name(database.name + "-wal").stat().st_size > 0
        result = activity.collect(home)
        assert result.paths == frozenset({workspace})
        assert database.read_bytes() == baseline


def test_history_and_authentication_files_are_never_opened(home: Path) -> None:
    store = home / ".codex"
    workspace = home / "code/retained"
    native_database(store, [SavedThread(str(workspace))])
    for name in ("auth.json", "history.jsonl", "session_index.jsonl"):
        path = store / name
        path.write_bytes(b"\xff not metadata\x00")
        path.chmod(0)
    sessions = store / "sessions"
    sessions.mkdir()
    transcript = sessions / "conversation.jsonl"
    transcript.write_bytes(b"\xff never read conversation bodies\x00")
    transcript.chmod(0)
    assert activity.collect(home).paths == frozenset({workspace})


def test_missing_unused_homes_are_not_created(home: Path) -> None:
    result = activity.collect(home)
    assert result.paths == frozenset()
    assert result.homes == 0
    assert result.databases == 0
    assert not (home / ".codex").exists()


def test_missing_explicit_home_is_unknown_and_never_created(home: Path) -> None:
    explicit = home / "missing-custom-codex"
    with pytest.raises(FileNotFoundError, match="No such file or directory"):
        activity.collect(home, codex_home=explicit)
    assert not explicit.exists()


def test_existing_unused_native_home_without_database_is_preserved(home: Path) -> None:
    store = home / ".codex"
    store.mkdir(mode=0o700)
    (store / "auth.json").write_bytes(b"\xff never opened\x00")
    result = activity.collect(home)
    assert result.homes == 1
    assert result.databases == 0
    assert next(store.glob("state_*.sqlite"), None) is None


def test_saved_history_without_metadata_database_fails_closed(home: Path) -> None:
    sessions = home / ".codex/sessions"
    sessions.mkdir(parents=True)
    (sessions / "saved.jsonl").write_bytes(b"\xff never parse this history\x00")
    with pytest.raises(RuntimeError, match="no readable native metadata database"):
        activity.collect(home)


@pytest.mark.parametrize(
    "cwd",
    [
        pytest.param("relative/worktree", id="relative"),
        pytest.param("", id="empty"),
        pytest.param("../other", id="parent-relative"),
    ],
)
def test_unknown_saved_working_directory_fails_closed(home: Path, cwd: str) -> None:
    native_database(home / ".codex", [SavedThread(cwd)])
    with pytest.raises(RuntimeError, match="unknown workspace path"):
        activity.collect(home)


def test_unknown_native_schema_is_not_treated_as_empty_activity(home: Path) -> None:
    store = home / ".codex"
    store.mkdir()
    with closing(sqlite3.connect(store / "state_5.sqlite")) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Deliberately bootstrap an incompatible native database schema to verify fail-closed handling.
            "CREATE TABLE threads (cwd TEXT)"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="database schema is unknown"):
        activity.collect(home)


def test_corrupt_native_database_fails_closed(home: Path) -> None:
    store = home / ".codex"
    store.mkdir()
    (store / "state_5.sqlite").write_bytes(b"invalid database\n")
    with pytest.raises(RuntimeError, match="database could not be verified"):
        activity.collect(home)


def test_registered_missing_native_store_is_not_silently_ignored(home: Path) -> None:
    registry_record(home, home / "missing-store")
    with pytest.raises(FileNotFoundError, match="missing-store"):
        activity.collect(home)


def test_registry_rejects_wrong_owner_identity_and_sanitizes_schema_errors(home: Path) -> None:
    path = registry_record(home, home / ".codex", uid=os.getuid() + 1)
    with pytest.raises(RuntimeError, match="registry identity is invalid"):
        activity.collect(home)
    path.write_text('{"private_fixture":"must not appear in diagnostics"}')
    with pytest.raises(RuntimeError, match="registry record has an unknown schema") as failure:
        activity.collect(home)
    assert "private_fixture" not in str(failure.value)


def test_registry_symbolic_link_is_not_followed(home: Path) -> None:
    path = registry_record(home, home / ".codex")
    target = home / "registry-target.json"
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="unexpected symbolic link"):
        activity.collect(home)


def test_profile_symbolic_link_is_unknown_native_storage(home: Path) -> None:
    profile_root = home / ".local/share/long-running-agent/account-switch"
    profile_root.mkdir(parents=True, mode=0o700)
    target = home / "profile-target"
    target.mkdir()
    (profile_root / "fixture").symlink_to(target, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unexpected symbolic link"):
        activity.collect(home)


def test_database_alias_cannot_escape_the_owned_user_home(home: Path, tmp_path: Path) -> None:
    outside = native_database(tmp_path / "outside")
    store = home / ".codex"
    store.mkdir()
    (store / "state_5.sqlite").symlink_to(outside)
    with pytest.raises(RuntimeError, match="escapes the owned user home"):
        activity.collect(home)


def test_group_writable_storage_is_not_a_trusted_reference_source(home: Path) -> None:
    store = home / ".codex"
    native_database(store)
    store.chmod(0o770)
    with pytest.raises(RuntimeError, match="unsafe type or permissions"):
        activity.collect(home)


def test_native_sqlite_row_budget_is_shared_across_streamed_rows(home: Path) -> None:
    database = native_database(home / ".codex", [SavedThread(str(home / "one")), SavedThread(str(home / "two"))])
    budget = activity.InventoryBudget(time.monotonic() + 10, rows=activity.MAX_ROWS - 1)
    with pytest.raises(RuntimeError, match="row limit"):
        activity.read_database(database, home, budget)


def test_native_sqlite_path_bytes_are_bounded(home: Path) -> None:
    database = native_database(home / ".codex", [SavedThread(str(home / "workspace"))])
    budget = activity.InventoryBudget(time.monotonic() + 10, bytes_read=activity.MAX_BYTES)
    with pytest.raises(RuntimeError, match="byte limit"):
        activity.read_database(database, home, budget)


def test_native_sqlite_read_respects_expired_collection_deadline(home: Path) -> None:
    database = native_database(home / ".codex", [SavedThread(str(home / "workspace"))])
    budget = activity.InventoryBudget(time.monotonic() - 1)
    with pytest.raises(RuntimeError, match="inspection deadline"):
        activity.read_database(database, home, budget)


def test_metadata_entry_scan_obeys_its_shared_budget(home: Path) -> None:
    (home / "one").mkdir()
    (home / "two").mkdir()
    budget = activity.InventoryBudget(time.monotonic() + 10, entries=activity.MAX_ENTRIES - 1)
    with pytest.raises(RuntimeError, match="directory-entry limit"):
        list(activity.entries(home, budget))


def test_database_hard_links_cannot_select_an_unrelated_native_wal(home: Path) -> None:
    database = native_database(home / ".codex")
    os.link(database, home / "ambiguous.sqlite")
    with pytest.raises(RuntimeError, match="ambiguous hard links"):
        activity.collect(home)


def test_closed_wal_database_preserves_saved_references_with_native_coordination(home: Path) -> None:
    workspace = home / "code/retained-after-checkpoint"
    database = native_database(home / ".codex", [SavedThread(str(workspace))])
    with closing(sqlite3.connect(database)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
    before = {path.name for path in database.parent.iterdir()}
    contents = database.read_bytes()
    assert workspace in activity.collect(home).paths
    after = {path.name for path in database.parent.iterdir()}
    sidecars = {database.name + "-wal", database.name + "-shm"}
    assert before <= after
    assert after <= before | sidecars
    assert (database.name + "-wal" in after) == (database.name + "-shm" in after)
    assert database.read_bytes() == contents


def test_wal_sidecar_symbolic_links_are_not_followed(home: Path) -> None:
    database = native_database(home / ".codex")
    target = home / "unrelated-file"
    target.write_bytes(b"native sidecar must not follow this\n")
    database.with_name(database.name + "-wal").symlink_to(target)
    database.with_name(database.name + "-shm").touch()
    with pytest.raises(RuntimeError, match="unexpected symbolic link"):
        activity.collect(home)
    assert target.read_bytes() == b"native sidecar must not follow this\n"


@pytest.mark.parametrize(
    "status",
    [
        pytest.param("in_progress", id="in-progress"),
        pytest.param("pending", id="pending"),
        pytest.param("unknown", id="unknown"),
    ],
)
def test_unfinished_native_backfill_is_unknown_activity(home: Path, status: str) -> None:
    database = native_database(home / ".codex")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Native backfill completion controls whether the external saved-thread metadata is exhaustive.
            "UPDATE backfill_state SET status=?", (status,)
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="backfill is incomplete or unknown"):
        activity.collect(home)


def test_skipped_native_rollout_does_not_prove_saved_activity_empty(home: Path) -> None:
    database = native_database(home / ".codex")
    with closing(sqlite3.connect(database)) as connection:
        connection.execute(  # sarj-noqa: SARJ429 -- Seed only the native migration skip marker; its rollout conversation file is never opened.
            "INSERT INTO rollout_migration_skipped_rollouts VALUES ('fixture','never-opened.jsonl')"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="skipped native rollouts"):
        activity.collect(home)


def test_unknown_backfill_view_is_rejected_before_reading_its_values(home: Path) -> None:
    database = native_database(home / ".codex")
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(  # sarj-noqa: SARJ429 -- Replace the external completeness table with a view to exercise schema drift at the native boundary.
            "DROP TABLE backfill_state;CREATE VIEW backfill_state AS SELECT 'complete' AS status;"
        )
        connection.commit()
    with pytest.raises(RuntimeError, match="completeness metadata schema is unknown"):
        activity.collect(home)


def test_configured_sqlite_root_without_database_cannot_hide_behind_an_old_default_database(home: Path) -> None:
    store = home / ".codex"
    native_database(store)
    custom = home / "custom-sqlite"
    custom.mkdir()
    (store / "config.toml").write_text(f"sqlite_home={json.dumps(str(custom))}\n")
    with pytest.raises(RuntimeError, match="Configured Codex SQLite storage has no native metadata database"):
        activity.collect(home)


def test_config_encoding_failure_is_sanitized(home: Path) -> None:
    store = home / ".codex"
    native_database(store)
    (store / "config.toml").write_bytes(b"\xff private fixture must not appear\n")
    with pytest.raises(RuntimeError, match="unknown encoding") as failure:
        activity.collect(home)
    assert "private fixture" not in str(failure.value)


def test_escaped_quoted_sqlite_home_key_cannot_hide_custom_storage(home: Path) -> None:
    store = home / ".codex"
    store.mkdir()
    custom = home / "custom-sqlite"
    workspace = home / "code/retained"
    native_database(custom, [SavedThread(str(workspace))])
    (store / "config.toml").write_text(f'"sqlite_\\u0068ome"={json.dumps(str(custom))}\n')
    assert activity.collect(home).paths == frozenset({workspace})


def test_registry_record_matches_the_native_byte_bound(home: Path) -> None:
    path = registry_record(home, home / ".codex")
    path.write_text(" " * (activity.MAX_RECORD_BYTES + 1))
    with pytest.raises(RuntimeError, match="file exceeded its byte limit"):
        activity.collect(home)


def test_saved_symlink_workspaces_protect_the_alias_and_resolved_target(home: Path) -> None:
    target = home / "code/target"
    target.mkdir(parents=True)
    alias = home / "workspace-alias"
    alias.symlink_to(target, target_is_directory=True)
    native_database(home / ".codex", [SavedThread(str(alias))])
    assert activity.collect(home).paths == frozenset({alias, target})


def test_broad_saved_workspace_is_preserved_for_conservative_intersections(home: Path) -> None:
    native_database(home / ".codex", [SavedThread("/")])
    assert activity.collect(home).paths == frozenset({Path("/")})


def test_filesystem_latency_does_not_consume_the_sqlite_progress_deadline(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspaces = {home / "code" / f"saved-{index}" for index in range(800)}
    native_database(home / ".codex", [SavedThread(str(path)) for path in workspaces])
    original = Path.resolve

    def slow_filesystem(path: Path, strict: bool = False) -> Path:
        if path in workspaces:
            time.sleep(0.002)
        return original(path, strict=strict)

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model slow native filesystem lookups while a real SQLite metadata query remains cheap.
        Path, "resolve", slow_filesystem
    )
    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Shorten the external SQLite progress deadline to expose filesystem time incorrectly charged as SQL work.
        activity, "QUERY_SECONDS", 0.2
    )
    assert activity.collect(home).paths == frozenset(workspaces)


def test_deferred_workspace_resolution_keeps_the_collection_deadline() -> None:
    budget = activity.InventoryBudget(time.monotonic() - 1, paths={Path("/fixture/workspace")})
    with pytest.raises(RuntimeError, match="inspection deadline"):
        budget.resolved_paths()
