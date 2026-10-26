from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import TYPE_CHECKING, Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, RootModel

from long_running_agent.process import CommandError, environment_snapshot


if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from long_running_agent.process import Completed


MAX_PULL_REQUESTS: Final = 30
MAX_RESPONSE_BYTES: Final = 1024 * 1024
COMMAND_SECONDS: Final = 10
PUBLICATION_QUERY: Final = """
query CleanupPublication($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    databaseId nameWithOwner defaultBranchRef { name }
    pullRequest(number: $number) {
      number state merged mergedAt headRefOid headRefName baseRefName
      headRepository { databaseId nameWithOwner }
      baseRepository { databaseId nameWithOwner defaultBranchRef { name } }
      mergeCommit { oid }
    }
  }
}
"""
Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]


class Inspection(Protocol):
    def __call__(
        self,
        arguments: Sequence[str],
        *,
        timeout: float,
        check: bool = True,
        env: Mapping[str, str] | None = None,
    ) -> Completed: ...


class NativeGit(Protocol):
    def __call__(self, path: Path, *arguments: str, timeout: float = 15, check: bool = True) -> Completed: ...


class Repository(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    id: int = Field(gt=0)
    full_name: str
    default_branch: str


class Reference(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    ref: str
    sha: Sha
    repo: Repository | None


class PullRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    number: int = Field(gt=0)
    state: Literal["open", "closed"]
    merged: bool | None = None
    merged_at: datetime | None
    merge_commit_sha: Sha | None = None
    head: Reference
    base: Reference


class PullRequests(RootModel[list[PullRequest]]):
    model_config = ConfigDict(strict=True)


class Branch(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    name: str


class GraphRepository(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    database_id: int = Field(gt=0, alias="databaseId")
    name_with_owner: str = Field(alias="nameWithOwner")
    default_branch_ref: Branch | None = Field(default=None, alias="defaultBranchRef")


class Commit(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    oid: Sha


class GraphPullRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    number: int = Field(gt=0)
    state: Literal["OPEN", "CLOSED", "MERGED"]
    merged: bool
    merged_at: datetime | None = Field(alias="mergedAt")
    head_ref_oid: Sha = Field(alias="headRefOid")
    head_ref_name: str = Field(alias="headRefName")
    base_ref_name: str = Field(alias="baseRefName")
    head_repository: GraphRepository | None = Field(alias="headRepository")
    base_repository: GraphRepository | None = Field(alias="baseRepository")
    merge_commit: Commit | None = Field(alias="mergeCommit")


class GraphRepositoryPullRequest(GraphRepository):
    pull_request: GraphPullRequest | None = Field(alias="pullRequest")


class GraphData(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    repository: GraphRepositoryPullRequest | None


class GraphError(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    message: str


class GraphResponse(BaseModel):
    model_config = ConfigDict(strict=True, extra="ignore", hide_input_in_errors=True)
    data: GraphData | None
    errors: list[GraphError] = Field(default_factory=list)


@dataclass(frozen=True)
class PublicationProof:
    repository: str
    number: int
    head: str
    branch: str
    merge_commit: str
    default_head: str
    default_branch: str


def github_repository(origin: str) -> str | None:
    matched = re.fullmatch(
        r"(?:https://github\.com/|ssh://git@github\.com/|git@github\.com:)"
        r"([a-z0-9][a-z0-9-]{0,38})/([a-z0-9_.-]{1,100}?)(?:\.git)?/?",
        origin,
        re.IGNORECASE,
    )
    if matched is None or matched[2] in {".", ".."}:
        return None
    return f"{matched[1]}/{matched[2]}".casefold()


def github_environment() -> dict[str, str]:
    environment = environment_snapshot()
    for key in ("GH_REPO", "GH_DEBUG", "DEBUG", "GH_HTTP_UNIX_SOCKET"):
        environment.pop(key, None)
    environment.update({"GH_HOST": "github.com", "GH_PROMPT_DISABLED": "1", "GH_NO_UPDATE_NOTIFIER": "1"})
    return environment


def api_response(endpoint: str, inspect: Inspection, *, fields: Sequence[str] = ()) -> str:
    result = inspect(
        [
            "gh",
            "api",
            "--hostname",
            "github.com",
            "--method",
            "POST" if fields else "GET",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2026-03-10",
            *fields,
            endpoint,
        ],
        timeout=COMMAND_SECONDS,
        check=False,
        env=github_environment(),
    )
    if result.returncode or result.stderr or len(result.stdout.encode()) > MAX_RESPONSE_BYTES:
        message = "GitHub publication evidence is unavailable"
        raise ValueError(message)
    return result.stdout


def matching_publication(pull: PullRequest, *, repository: str, head: str, branch: str, default_branch: str) -> bool:
    return (
        pull.state == "closed"
        and pull.merged is not False
        and pull.merged_at is not None
        and pull.merged_at.utcoffset() is not None
        and pull.head.sha == head
        and pull.head.ref == branch
        and pull.head.repo is not None
        and pull.base.repo is not None
        and pull.head.repo.full_name.casefold() == repository
        and pull.base.repo.full_name.casefold() == repository
        and pull.head.repo.id == pull.base.repo.id
        and pull.base.ref == default_branch
        and pull.base.repo.default_branch == default_branch
    )


def matching_graph_publication(pull: GraphPullRequest, discovery: PullRequest) -> bool:
    return (
        pull.number == discovery.number
        and pull.state == "MERGED"
        and pull.merged
        and pull.merged_at == discovery.merged_at
        and pull.head_ref_oid == discovery.head.sha
        and pull.head_ref_name == discovery.head.ref
        and pull.base_ref_name == discovery.base.ref
        and pull.head_repository is not None
        and discovery.head.repo is not None
        and pull.head_repository.database_id == discovery.head.repo.id
        and pull.head_repository.name_with_owner.casefold() == discovery.head.repo.full_name.casefold()
        and pull.base_repository is not None
        and discovery.base.repo is not None
        and pull.base_repository.database_id == discovery.base.repo.id
        and pull.base_repository.name_with_owner.casefold() == discovery.base.repo.full_name.casefold()
        and pull.base_repository.default_branch_ref is not None
        and pull.base_repository.default_branch_ref.name == discovery.base.repo.default_branch
        and pull.merge_commit is not None
        and (discovery.merge_commit_sha is None or discovery.merge_commit_sha == pull.merge_commit.oid)
    )


def inspect_github_publication(
    repository: Path,
    *,
    identity: str,
    head: str,
    branch: str,
    default_head: str,
    default_branch: str,
    inspect: Inspection,
    native_git: NativeGit,
) -> PublicationProof | None:
    local_branch = branch.removeprefix("refs/heads/")
    current_origin = native_git(repository, "remote", "get-url", "origin", timeout=COMMAND_SECONDS, check=False)
    if (
        current_origin.returncode
        or current_origin.stderr
        or github_repository(current_origin.stdout.strip()) != identity
    ):
        return None
    candidates = PullRequests.model_validate_json(
        api_response(f"repos/{identity}/commits/{head}/pulls?per_page={MAX_PULL_REQUESTS}", inspect)
    ).root
    # A full page is potentially truncated. Never turn incomplete discovery into positive evidence.
    if len(candidates) >= MAX_PULL_REQUESTS:
        return None
    matched = [
        pull
        for pull in candidates
        if matching_publication(
            pull, repository=identity, head=head, branch=local_branch, default_branch=default_branch
        )
    ]
    if len(matched) != 1:
        return None
    selected = matched[0]
    owner, name = identity.split("/", 1)
    graph = GraphResponse.model_validate_json(
        api_response(
            "graphql",
            inspect,
            fields=(
                "--raw-field",
                f"query={PUBLICATION_QUERY}",
                "--raw-field",
                f"owner={owner}",
                "--raw-field",
                f"name={name}",
                "--field",
                f"number={selected.number}",
            ),
        )
    )
    if graph.errors or graph.data is None or graph.data.repository is None:
        return None
    remote = graph.data.repository
    detail = remote.pull_request
    if detail is None or selected.base.repo is None:
        return None
    if (
        remote.name_with_owner.casefold() != identity
        or remote.default_branch_ref is None
        or remote.default_branch_ref.name != default_branch
        or remote.database_id != selected.base.repo.id
        or not matching_graph_publication(detail, selected)
    ):
        return None
    # The PR's retained head ref supplies a separate exact-head check, independent of a reused branch name.
    reference = f"refs/pull/{detail.number}/head"
    retained_head = native_git(
        repository, "ls-remote", "--exit-code", "--refs", "origin", reference, timeout=COMMAND_SECONDS, check=False
    )
    if (
        retained_head.returncode
        or retained_head.stderr
        or retained_head.stdout.splitlines() != [f"{head}\t{reference}"]
    ):
        return None
    merge_commit = detail.merge_commit.oid if detail.merge_commit is not None else None
    if merge_commit is None:
        return None
    ancestry = native_git(
        repository, "merge-base", "--is-ancestor", merge_commit, default_head, timeout=COMMAND_SECONDS, check=False
    )
    if ancestry.returncode or ancestry.stderr or ancestry.stdout:
        return None
    return PublicationProof(identity, detail.number, head, branch, merge_commit, default_head, default_branch)


def prove_github_publication(
    repository: Path,
    *,
    origin: str,
    head: str,
    branch: str,
    default_head: str,
    default_branch: str,
    inspect: Inspection,
    native_git: NativeGit,
) -> PublicationProof | None:
    identity = github_repository(origin)
    if identity is None:
        return None
    if (
        re.fullmatch(r"[0-9a-f]{40}", head) is None
        or re.fullmatch(r"[0-9a-f]{40}", default_head) is None
        or not branch.startswith("refs/heads/")
        or not branch.removeprefix("refs/heads/")
        or not default_branch
    ):
        return None
    try:
        return inspect_github_publication(
            repository,
            identity=identity,
            head=head,
            branch=branch,
            default_head=default_head,
            default_branch=default_branch,
            inspect=inspect,
            native_git=native_git,
        )
    except OSError, ValueError, CommandError:
        return None
