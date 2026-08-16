from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence


class WebhookError(ValueError):
    """Raised when a code-host webhook cannot be trusted or decoded."""


class EventKind(StrEnum):
    ISSUE_OPENED = "issue_opened"
    COMMENT = "comment"
    PLAN_MERGED = "plan_merged"


class RunKind(StrEnum):
    PLAN = "plan"
    REVIEW = "review"
    IMPLEMENTATION = "implementation"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class CodeHostEvent:
    delivery_id: str
    kind: EventKind
    event_name: str
    action: str
    issue_number: int
    actor: str
    body: str = ""
    source_url: str = ""
    pull_request_number: int | None = None
    raw_payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ConversationContext:
    issue_number: int
    title: str
    body: str
    source_url: str
    comments: tuple[str, ...] = ()
    default_branch: str = "main"
    base_sha: str = ""


@dataclass(frozen=True, slots=True)
class PullRequest:
    number: int
    url: str
    branch: str
    head_sha: str
    merged: bool = False


@dataclass(frozen=True, slots=True)
class AgentRequest:
    run_id: str
    kind: RunKind
    prompt: str
    worktree: Path
    timeout_seconds: int
    tools: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AgentResult:
    output: str
    events: tuple[Mapping[str, Any], ...] = ()
    model_name: str = "Pi"


class WebhookParser(Protocol):
    def parse_webhook(
        self, headers: Mapping[str, str], body: bytes
    ) -> CodeHostEvent | None:
        raise RuntimeError("protocol method")


class CodeHost(WebhookParser, Protocol):
    @property
    def remote_url(self) -> str:
        raise RuntimeError("protocol method")

    @property
    def git_environment(self) -> Mapping[str, str]:
        raise RuntimeError("protocol method")

    async def close(self) -> None:
        raise RuntimeError("protocol method")

    async def fetch_context(self, event: CodeHostEvent) -> ConversationContext:
        raise RuntimeError("protocol method")

    async def has_write_permission(self, actor: str) -> bool:
        raise RuntimeError("protocol method")

    async def post_comment(self, issue_number: int, body: str) -> str:
        raise RuntimeError("protocol method")

    async def pull_request(self, number: int) -> PullRequest:
        raise RuntimeError("protocol method")

    async def pull_request_files(self, number: int) -> Mapping[str, str]:
        raise RuntimeError("protocol method")

    async def file_content(self, path: str, ref: str) -> str:
        raise RuntimeError("protocol method")

    async def push_branch(self, repository: Path, branch: str) -> None:
        raise RuntimeError("protocol method")

    async def open_pull_request(
        self,
        *,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        raise RuntimeError("protocol method")


class AgentRunner(Protocol):
    async def run(self, request: AgentRequest) -> AgentResult:
        raise RuntimeError("protocol method")


READ_ONLY_TOOLS: Sequence[str] = ("read", "grep", "find", "ls")
EDIT_TOOLS: Sequence[str] = ("read", "bash", "edit", "write", "grep", "find", "ls")
