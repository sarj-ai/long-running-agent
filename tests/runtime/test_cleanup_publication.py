from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import subprocess
import time
from typing import TYPE_CHECKING, TypedDict, Unpack

from long_running_agent.process import CommandError, run
from long_running_agent.runtime import cleanup, cleanup_publication, cleanup_state
from long_running_agent.runtime.settings import RuntimePaths
import pytest


if TYPE_CHECKING:
    from collections.abc import Callable, Sequence


class NativeOptions(TypedDict, total=False):
    capture_output: bool
    check: bool
    timeout: float | None
    input_text: str | None
    cwd: Path | None
    env: Mapping[str, str] | None


@dataclass(frozen=True)
class PublishedRepository:
    path: Path
    remote: Path
    head: str
    merge_commit: str
    default_head: str

    def native_git(
        self, path: Path, *arguments: str, timeout: float = 15, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        # Replace only the external GitHub Git transport with the private native bare-repository fixture.
        if arguments[:3] == ("ls-remote", "--exit-code", "--refs"):
            assert arguments[3] == "origin"
            arguments = (*arguments[:3], str(self.remote), *arguments[4:])
        return cleanup.git_process(path, *arguments, timeout=timeout, check=check)


@dataclass
class GitHubBoundary:
    responses: list[subprocess.CompletedProcess[str]]
    calls: list[tuple[str, ...]] = field(default_factory=list)
    environments: list[Mapping[str, str]] = field(default_factory=list)
    failure: CommandError | cleanup.InspectionDeferredError | None = None

    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(tuple(arguments))
        assert timeout == cleanup_publication.COMMAND_SECONDS
        assert not check
        assert env is not None
        self.environments.append(env)
        if self.failure is not None:
            raise self.failure
        return self.responses.pop(0)


def git_commit(repository: Path, message: str) -> str:
    cleanup.git(repository, "add", ".")
    cleanup.git(
        repository,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        message,
    )
    return cleanup.git(repository, "rev-parse", "HEAD").strip()


@pytest.fixture
def published(tmp_path: Path) -> PublishedRepository:
    repository = tmp_path / "code/repository"
    repository.mkdir(parents=True)
    cleanup.git(repository, "init", "-q", "-b", "main")
    (repository / "source.txt").write_text("base source\n")
    git_commit(repository, "base")
    cleanup.git(repository, "checkout", "-q", "-b", "feature")
    (repository / "feature.txt").write_text("published feature\n")
    head = git_commit(repository, "feature commits remain locally preserved")
    cleanup.git(repository, "checkout", "-q", "main")
    cleanup.git(repository, "merge", "--squash", "feature")
    merged = git_commit(repository, "squashed publication")
    (repository / "later.txt").write_text("later default branch change\n")
    default = git_commit(repository, "later default commit")
    remote = tmp_path / "remote.git"
    cleanup.git(repository, "clone", "--bare", "-q", str(repository), str(remote))
    cleanup.git(remote, "update-ref", "refs/pull/7/head", head)
    cleanup.git(repository, "remote", "add", "origin", "https://github.com/Fixture/Project.git")
    return PublishedRepository(repository, remote, head, merged, default)


@pytest.fixture
def filesystem_capacity(monkeypatch: pytest.MonkeyPatch) -> Callable[[Path, int], None]:
    native = os.statvfs

    def provide(home: Path, used_percent: int) -> None:
        original = native(home)
        available = 100 - used_percent
        snapshot = os.statvfs_result(
            (
                original.f_bsize,
                original.f_frsize,
                100,
                available,
                available,
                original.f_files,
                original.f_ffree,
                original.f_favail,
                original.f_flag,
                original.f_namemax,
            )
        )

        def statvfs(path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes]) -> os.statvfs_result:
            return snapshot if path == home else native(path)

        monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Model only the native statvfs capacity result for the private fixture home; real recovery admission and publication-budget selection remain unchanged.
            os, "statvfs", statvfs
        )

    return provide


def pull_request(published: PublishedRepository) -> cleanup_publication.PullRequest:
    return cleanup_publication.PullRequest(
        number=7,
        state="closed",
        merged=True,
        merged_at=datetime(2026, 10, 1, tzinfo=UTC),
        merge_commit_sha=published.merge_commit,
        head=cleanup_publication.Reference(
            ref="feature",
            sha=published.head,
            repo=cleanup_publication.Repository(id=1, full_name="Fixture/Project", default_branch="main"),
        ),
        base=cleanup_publication.Reference(
            ref="main",
            sha=published.default_head,
            repo=cleanup_publication.Repository(id=1, full_name="Fixture/Project", default_branch="main"),
        ),
    )


def response(payload: str, *, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["gh", "api"], returncode, payload, stderr)


def graph_response(pull: cleanup_publication.PullRequest) -> subprocess.CompletedProcess[str]:
    assert pull.head.repo is not None
    assert pull.base.repo is not None
    return response(
        json.dumps(
            {
                "data": {
                    "repository": {
                        "databaseId": pull.base.repo.id,
                        "nameWithOwner": pull.base.repo.full_name,
                        "defaultBranchRef": {"name": pull.base.repo.default_branch},
                        "pullRequest": {
                            "number": pull.number,
                            "state": "MERGED" if pull.merged else pull.state.upper(),
                            "merged": pull.merged,
                            "mergedAt": pull.merged_at.isoformat() if pull.merged_at is not None else None,
                            "headRefOid": pull.head.sha,
                            "headRefName": pull.head.ref,
                            "headRepository": {
                                "databaseId": pull.head.repo.id,
                                "nameWithOwner": pull.head.repo.full_name,
                            },
                            "baseRefName": pull.base.ref,
                            "baseRepository": {
                                "databaseId": pull.base.repo.id,
                                "nameWithOwner": pull.base.repo.full_name,
                                "defaultBranchRef": {"name": pull.base.repo.default_branch},
                            },
                            "mergeCommit": {"oid": pull.merge_commit_sha}
                            if pull.merge_commit_sha is not None
                            else None,
                        },
                    }
                },
            }
        )
    )


def github(published: PublishedRepository) -> GitHubBoundary:
    pull = pull_request(published)
    listing = pull.model_copy(update={"merged": None})
    return GitHubBoundary(
        [response(cleanup_publication.PullRequests([listing]).model_dump_json()), graph_response(pull)]
    )


def prove(
    published: PublishedRepository,
    boundary: GitHubBoundary,
    *,
    head: str | None = None,
    default_head: str | None = None,
    branch: str = "refs/heads/feature",
    native_git: cleanup_publication.NativeGit | None = None,
) -> cleanup_publication.PublicationProof | None:
    return cleanup_publication.prove_github_publication(
        published.path,
        origin="https://github.com/Fixture/Project.git",
        head=published.head if head is None else head,
        branch=branch,
        default_head=published.default_head if default_head is None else default_head,
        default_branch="main",
        inspect=boundary,
        native_git=published.native_git if native_git is None else native_git,
    )


def test_native_squash_proof_binds_exact_head_and_preserves_local_branch(published: PublishedRepository) -> None:
    boundary = github(published)
    before = cleanup.git(published.path, "for-each-ref", "--format=%(refname) %(objectname)")
    native = cleanup.git_process(
        published.path, "merge-base", "--is-ancestor", published.head, published.default_head, check=False
    )
    assert native.returncode == 1
    assert prove(published, boundary) == cleanup_publication.PublicationProof(
        "fixture/project",
        7,
        published.head,
        "refs/heads/feature",
        published.merge_commit,
        published.default_head,
        "main",
    )
    assert [call[-1] for call in boundary.calls] == [
        f"repos/fixture/project/commits/{published.head}/pulls?per_page=30",
        "graphql",
    ]
    assert all(call[:4] == ("gh", "api", "--hostname", "github.com") for call in boundary.calls)
    assert cleanup.git(published.path, "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert cleanup.git(published.path, "rev-parse", "refs/heads/feature").strip() == published.head


def test_reused_live_branch_cannot_publish_a_new_unmerged_head(published: PublishedRepository) -> None:
    cleanup.git(published.path, "checkout", "-q", "feature")
    (published.path / "unpublished.txt").write_text("preserve unique local work\n")
    unique = git_commit(published.path, "reused feature branch")
    cleanup.git(published.remote, "fetch", "-q", str(published.path), "refs/heads/feature:refs/heads/feature")
    assert prove(published, github(published)) is not None
    boundary = github(published)
    assert prove(published, boundary, head=unique) is None
    assert len(boundary.calls) == 1
    assert cleanup.git(published.path, "rev-parse", "feature").strip() == unique
    assert (published.path / "unpublished.txt").read_text() == "preserve unique local work\n"


@pytest.mark.parametrize("change", ["missing", "different"])
def test_retained_pull_ref_must_still_match_the_exact_head(published: PublishedRepository, change: str) -> None:
    if change == "missing":
        cleanup.git(published.remote, "update-ref", "-d", "refs/pull/7/head")
    else:
        cleanup.git(published.remote, "update-ref", "refs/pull/7/head", published.default_head)
    assert prove(published, github(published)) is None
    assert cleanup.git(published.path, "rev-parse", "feature").strip() == published.head


def test_native_ancestry_to_the_pinned_default_commit_is_required(published: PublishedRepository) -> None:
    assert prove(published, github(published), default_head=published.head) is None
    boundary = github(published)
    pull = pull_request(published)
    pull.merge_commit_sha = "a" * 40
    boundary.responses = [
        response(cleanup_publication.PullRequests([pull]).model_dump_json()),
        graph_response(pull),
    ]
    assert prove(published, boundary) is None


@pytest.mark.parametrize(
    "mutation",
    ["open", "unmerged", "head", "head-branch", "head-repository", "base-repository", "base-branch", "default", "id"],
)
def test_misbound_publication_snapshots_never_qualify(published: PublishedRepository, mutation: str) -> None:
    pull = pull_request(published)
    assert pull.head.repo is not None
    assert pull.base.repo is not None
    match mutation:
        case "open":
            pull.state = "open"
        case "unmerged":
            pull.merged = False
            pull.merged_at = None
        case "head":
            pull.head.sha = "b" * 40
        case "head-branch":
            pull.head.ref = "other"
        case "head-repository":
            pull.head.repo.full_name = "fork/project"
        case "base-repository":
            pull.base.repo.full_name = "other/project"
        case "base-branch":
            pull.base.ref = "release"
        case "default":
            pull.base.repo.default_branch = "release"
        case "id":
            pull.head.repo.id = 2
        case _:
            pytest.fail("Unknown publication mutation")
    boundary = GitHubBoundary([response(cleanup_publication.PullRequests([pull]).model_dump_json())])
    assert prove(published, boundary) is None
    assert len(boundary.calls) == 1


def test_changed_numbered_snapshot_is_retained_without_trusting_the_discovery_page(
    published: PublishedRepository,
) -> None:
    boundary = github(published)
    changed = pull_request(published)
    changed.head.sha = "b" * 40
    boundary.responses[-1] = graph_response(changed)
    assert prove(published, boundary) is None
    assert len(boundary.calls) == 2


@pytest.mark.parametrize("payload", ["{", "{}", "null", "[1]", '{"message":"Bad credentials"}', "[]\n[]"])
def test_malformed_or_error_json_is_retaining(published: PublishedRepository, payload: str) -> None:
    boundary = GitHubBoundary([response(payload)])
    assert prove(published, boundary) is None
    assert len(boundary.calls) == 1


@pytest.mark.parametrize("failure", ["auth", "rate-limit", "diagnostic", "oversized", "timeout"])
def test_external_github_failures_are_bounded_retaining_results(published: PublishedRepository, failure: str) -> None:
    boundary = github(published)
    match failure:
        case "auth":
            boundary.responses[0] = response('{"message":"Bad credentials"}', returncode=4, stderr="private auth")
        case "rate-limit":
            boundary.responses[0] = response('{"message":"API rate limit exceeded"}', returncode=1)
        case "diagnostic":
            boundary.responses[0] = response(boundary.responses[0].stdout, stderr="unexpected diagnostic")
        case "oversized":
            boundary.responses[0] = response(" " * (cleanup_publication.MAX_RESPONSE_BYTES + 1))
        case "timeout":
            boundary.failure = CommandError("gh", 124)
        case _:
            pytest.fail("Unknown GitHub boundary failure")
    assert prove(published, boundary) is None
    assert len(boundary.calls) == 1


def test_full_or_ambiguous_discovery_page_is_never_positive_evidence(published: PublishedRepository) -> None:
    pull = pull_request(published)
    for count in (2, cleanup_publication.MAX_PULL_REQUESTS):
        boundary = GitHubBoundary([response(cleanup_publication.PullRequests([pull] * count).model_dump_json())])
        assert prove(published, boundary) is None
        assert len(boundary.calls) == 1


def test_inspection_defer_propagates_without_consuming_a_completed_review(published: PublishedRepository) -> None:
    boundary = github(published)
    boundary.failure = cleanup.InspectionDeferredError(cleanup.DEFERRED)
    with pytest.raises(cleanup.InspectionDeferredError):
        prove(published, boundary)


@pytest.mark.parametrize("branch", ["", "feature", "refs/tags/feature", "refs/heads/"])
def test_detached_and_unknown_branch_forms_do_not_enter_github_discovery(
    published: PublishedRepository, branch: str
) -> None:
    boundary = github(published)
    assert prove(published, boundary, branch=branch) is None
    assert boundary.calls == []


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://github.com/Fixture/Project.git", "fixture/project"),
        ("git@github.com:Fixture/Project.git", "fixture/project"),
        ("ssh://git@github.com/Fixture/Project", "fixture/project"),
        ("https://github.com.attacker.invalid/Fixture/Project", None),
        ("https://attacker.invalid/github.com/Fixture/Project", None),
        ("https://github.com@attacker.invalid/Fixture/Project", None),
        ("https://token@github.com/Fixture/Project", None),
        ("https://github.com/Fixture/Project?redirect=other", None),
        ("https://github.com/Fixture/../Project", None),
        ("https://github.com/Fixture/%50roject", None),
        ("https://github.com:443/Fixture/Project", None),
        ("/private/local/repository", None),
    ],
)
def test_origin_identity_requires_an_unambiguous_github_authority(origin: str, expected: str | None) -> None:
    assert cleanup_publication.github_repository(origin) == expected


def test_host_and_debug_environment_overrides_cannot_redirect_or_expose_auth(
    published: PublishedRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_HOST", "attacker.invalid")
    monkeypatch.setenv("GH_REPO", "attacker/other")
    monkeypatch.setenv("GH_DEBUG", "api")
    monkeypatch.setenv("GH_HTTP_UNIX_SOCKET", "/private/untrusted.sock")
    boundary = github(published)
    assert prove(published, boundary) is not None
    assert all(environment["GH_HOST"] == "github.com" for environment in boundary.environments)
    assert all("GH_REPO" not in environment and "GH_DEBUG" not in environment for environment in boundary.environments)
    assert all("GH_HTTP_UNIX_SOCKET" not in environment for environment in boundary.environments)


@pytest.mark.parametrize("command", ["ls-remote", "merge-base"])
def test_native_success_with_diagnostics_never_authorizes_publication(
    published: PublishedRepository, command: str
) -> None:
    def diagnostic(
        path: Path, *arguments: str, timeout: float = 15, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        result = published.native_git(path, *arguments, timeout=timeout, check=check)
        # Exercise the external Git diagnostic boundary after the same real fixture command has completed.
        if arguments[0] == command:
            return subprocess.CompletedProcess(
                ["git", *arguments], result.returncode, result.stdout, "native object diagnostic"
            )
        return result

    assert prove(published, github(published), native_git=diagnostic) is None


def test_changed_origin_is_rejected_before_any_github_call(published: PublishedRepository) -> None:
    cleanup.git(published.path, "remote", "set-url", "origin", "https://github.com/Other/Project.git")
    boundary = github(published)
    assert prove(published, boundary) is None
    assert boundary.calls == []


@pytest.mark.parametrize("omit_merge_hash", [False, True], ids=["null-rest-field", "omitted-rest-field"])
def test_null_rest_merge_hash_uses_the_numbered_graphql_merge_commit(
    published: PublishedRepository, omit_merge_hash: bool
) -> None:
    boundary = github(published)
    listing = pull_request(published)
    listing.merge_commit_sha = None
    listing.merged = None
    payload = cleanup_publication.PullRequests([listing]).model_dump_json(exclude_none=omit_merge_hash)
    if omit_merge_hash:
        assert '"merge_commit_sha"' not in payload
        assert '"merged"' not in payload
    boundary.responses[0] = response(payload)
    proof = prove(published, boundary)
    assert proof is not None
    assert proof.merge_commit == published.merge_commit
    assert boundary.calls[1][-1] == "graphql"
    assert "number=7" in boundary.calls[1]


def publication_entry(published: PublishedRepository) -> cleanup.Worktree:
    linked = published.path.parent / "linked"
    cleanup.git(published.path, "worktree", "add", "-q", str(linked), "feature")
    entries = cleanup.parse_worktrees(
        cleanup.git(published.path, "worktree", "list", "--porcelain", "-z"), published.path
    )
    return next(entry for entry in entries if entry.path == linked)


def install_publication_transport(
    published: PublishedRepository,
    boundary: GitHubBoundary,
    monkeypatch: pytest.MonkeyPatch,
    *,
    diagnostic: bool = False,
) -> None:
    native = run

    def transport(arguments: Sequence[str], **options: Unpack[NativeOptions]) -> subprocess.CompletedProcess[str]:
        if arguments[0] == "gh":
            return boundary(
                arguments,
                timeout=options.get("timeout") or 0,
                check=options.get("check", True),
                env=options.get("env"),
            )
        command = list(arguments)
        index = command.index("-C") + 2
        git_arguments = tuple(command[index:])
        if git_arguments[:3] == ("ls-remote", "--exit-code", "--refs"):
            assert git_arguments[3] == "origin"
            command[index + 3] = str(published.remote)
        result = native(command, **options)
        if diagnostic and git_arguments[:3] == ("merge-base", "--is-ancestor", published.head):
            return subprocess.CompletedProcess(command, result.returncode, result.stdout, "native ancestry diagnostic")
        return result

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Replace only external GH responses and GitHub transport at the subprocess boundary; every native Git ancestry and reference command executes against private fixtures.
        cleanup, "run", transport
    )


def test_merged_status_publication_budget_is_retaining_and_resets_between_windows(
    published: PublishedRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = publication_entry(published)
    evidence = cleanup.MergeProof(published.default_head, "https://github.com/Fixture/Project.git", "main")
    responses: list[subprocess.CompletedProcess[str]] = []
    for _ in range(cleanup.MAX_PUBLICATION_LOOKUPS + 1):
        responses.extend(github(published).responses)
    boundary = GitHubBoundary(responses)
    install_publication_transport(published, boundary, monkeypatch)
    before = cleanup.git(published.path, "for-each-ref", "--format=%(refname) %(objectname)")
    with cleanup.publication_window():
        for _ in range(cleanup.MAX_PUBLICATION_LOOKUPS):
            assert cleanup.merged_status(entry, now=time.time(), proof=evidence) == "eligible"
        with pytest.raises(cleanup.InspectionDeferredError):
            cleanup.merged_status(entry, now=time.time(), proof=evidence)
        assert len(boundary.calls) == 2 * cleanup.MAX_PUBLICATION_LOOKUPS
    assert cleanup.PUBLICATION_LOOKUPS.get() is None
    with cleanup.publication_window():
        assert cleanup.merged_status(entry, now=time.time(), proof=evidence) == "eligible"
    assert len(boundary.calls) == 2 * (cleanup.MAX_PUBLICATION_LOOKUPS + 1)
    assert cleanup.git(published.path, "for-each-ref", "--format=%(refname) %(objectname)") == before
    assert entry.path.is_dir()


def test_negative_native_ancestry_with_diagnostics_never_enters_github_fallback(
    published: PublishedRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = publication_entry(published)
    evidence = cleanup.MergeProof(published.default_head, "https://github.com/Fixture/Project.git", "main")
    boundary = github(published)
    install_publication_transport(published, boundary, monkeypatch, diagnostic=True)
    with cleanup.publication_window(), pytest.raises(RuntimeError, match="ancestry inspection reported diagnostics"):
        cleanup.merged_status(entry, now=time.time(), proof=evidence)
    assert boundary.calls == []
    assert entry.path.is_dir()


def prepare_verified_default(published: PublishedRepository) -> None:
    cleanup.git(published.path, "fetch", "-q", str(published.remote), "refs/heads/main:refs/remotes/origin/main")
    cleanup.git(published.path, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    fetch = published.path / ".git/FETCH_HEAD"
    records = fetch.read_text().splitlines()
    assert len(records) == 1
    oid, flag, description = records[0].split("\t", 2)
    assert oid == published.default_head
    prefix = "branch 'main' of "
    assert description.startswith(prefix)
    transport = description.removeprefix(prefix)
    assert cleanup.normalized_remote(transport) == cleanup.normalized_remote(str(published.remote))
    # Map only the validated private transport identity; retain native fetch's exact object and merge flag.
    fetch.write_text(f"{oid}\t{flag}\t{prefix}https://github.com/Fixture/Project.git\n")


def test_fresh_second_merge_proof_rejects_a_changed_graphql_publication_snapshot(
    published: PublishedRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = publication_entry(published)
    prepare_verified_default(published)
    evidence = cleanup.verified_default(entry, now=time.time())
    assert isinstance(evidence, cleanup.MergeProof)
    boundary = github(published)
    boundary.responses.extend(github(published).responses)
    changed = pull_request(published)
    changed.head.sha = "b" * 40
    boundary.responses[-1] = graph_response(changed)
    install_publication_transport(published, boundary, monkeypatch)
    with cleanup.publication_window():
        assert cleanup.merged_status(entry, now=time.time(), proof=evidence) == "eligible"
        assert cleanup.merged_status(entry, now=time.time()) == (
            "commits are not proven merged into origin's default branch"
        )
    assert len(boundary.calls) == 4
    assert entry.path.is_dir()
    assert cleanup.git(published.path, "rev-parse", entry.branch).strip() == published.head


@pytest.mark.parametrize(
    ("capacity_requested", "used_percent", "publication_limit"),
    [
        (False, 85, cleanup.MAX_PUBLICATION_LOOKUPS),
        (True, 85, cleanup.CAPACITY_PUBLICATION_LOOKUPS),
        (True, 10, cleanup.MAX_PUBLICATION_LOOKUPS),
        (True, 70, cleanup.CAPACITY_PUBLICATION_LOOKUPS),
    ],
    ids=["routine-six", "capacity-twenty-four", "requested-below-floor-six", "lower-target-capacity-twenty-four"],
)
def test_publication_budget_leaves_the_first_unqueried_native_candidate_pending(
    published: PublishedRepository,
    monkeypatch: pytest.MonkeyPatch,
    *,
    filesystem_capacity: Callable[[Path, int], None],
    capacity_requested: bool,
    used_percent: int,
    publication_limit: int,
) -> None:
    paths = RuntimePaths(published.path.parent.parent)
    filesystem_capacity(paths.home, used_percent)
    count = publication_limit + 2
    candidates: list[Path] = []
    branches: list[str] = []
    for index in range(count):
        candidate = paths.code / "worktrees" / f"candidate-{index}"
        branch = f"pending-{index}"
        cleanup.git(published.path, "worktree", "add", "-q", "-b", branch, str(candidate), published.head)
        os.utime(candidate, (1_000_000 + index, 1_000_000 + index))
        candidates.append(candidate)
        branches.append(branch)
    prepare_verified_default(published)
    aged_now = time.time() + 8 * cleanup.DAY
    os.utime(published.path / ".git/FETCH_HEAD", (aged_now, aged_now))

    def current_time() -> float:
        return aged_now

    monkeypatch.setattr(  # sarj-noqa: SARJ445 -- Advance only the OS wall clock to age real Git fixture ctime; native ancestry, cursor and budget logic remain unchanged.
        time, "time", current_time
    )
    boundary = GitHubBoundary([response("[]") for _ in range(2 * publication_limit)])
    install_publication_transport(published, boundary, monkeypatch)
    first = cleanup.reap(paths, apply=True, capacity_recovery=capacity_requested)
    completed = publication_limit
    assert all("not proven merged" in row.reason for row in first[:completed])
    assert [row.path for row in first[completed:]] == [str(candidate) for candidate in candidates[completed:]]
    assert all(row.reason == cleanup.DEFERRED for row in first[completed:])
    cursor = cleanup_state.review_cursor(cleanup.state_directory(paths), paths.code)
    assert cursor is not None
    assert cursor.path == candidates[completed - 1]
    assert len(boundary.calls) == completed
    assert cleanup.PUBLICATION_LOOKUPS.get() is None
    second = cleanup.reap(paths, apply=True, capacity_recovery=capacity_requested)
    assert second[0].path == str(candidates[completed])
    assert "not proven merged" in second[0].reason
    assert len(boundary.calls) == 2 * completed
    assert not any(row.attempted or row.removed for row in (*first, *second))
    assert all(candidate.is_dir() for candidate in candidates)
    assert not (cleanup.state_directory(paths) / "cleanup-attempts.json").exists()
    for branch in branches:
        assert cleanup.git(published.path, "rev-parse", branch).strip() == published.head
