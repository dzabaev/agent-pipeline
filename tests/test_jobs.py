# pyright: reportMissingImports=false
import asyncio
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest

from app.config import Settings
from app.db import Database
from app.github import GitHubForge
from app.jobs import WorkerPool, Workspace, WorkspaceManager
from app.pi import PiResult


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def repository(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "tracked.txt").write_text("base\n")
    git(repo, "add", "tracked.txt")
    git(repo, "commit", "-m", "base")
    return repo


def manager(tmp_path: Path) -> WorkspaceManager:
    settings = Settings(
        "http://localhost",
        "secret",
        "client",
        "client-secret",
        "1",
        "private-key",
        "webhook",
        1,
        data_dir=tmp_path / "data",
    )
    return WorkspaceManager(settings, cast(GitHubForge, object()))


def test_verification_detects_content_change_with_same_git_status(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    (repo / "tracked.txt").write_text("agent change\n")
    workspace = manager(tmp_path)

    async def verify() -> None:
        with pytest.raises(RuntimeError, match="Verification command changed"):
            await workspace.verify("run", repo, "printf 'verification change\\n' > tracked.txt")

    asyncio.run(verify())


def test_agent_commit_is_rejected(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "--detach", str(worktree))
    start_head = git(worktree, "rev-parse", "HEAD")
    marker = (worktree / ".git").read_text()
    (worktree / "tracked.txt").write_text("committed by agent\n")
    git(worktree, "add", "tracked.txt")
    git(worktree, "commit", "-m", "agent commit")
    workspace = manager(tmp_path)

    async def check() -> None:
        with pytest.raises(RuntimeError, match="changed Git history"):
            await workspace.assert_head_unchanged(
                Workspace(worktree, None, "main", start_head, marker)
            )

    asyncio.run(check())


class LocalForge:
    def __init__(self, source: Path) -> None:
        self.source = source

    async def git_access(self, full_name: str, installation_id: int) -> tuple[str, dict[str, str]]:
        return str(self.source), {}

    async def get_pull(self, full_name: str, installation_id: int, number: int) -> dict[str, Any]:
        return {
            "head": {"ref": "topic", "repo": {"full_name": "owner/repo"}},
        }


class FakeForge:
    def __init__(self) -> None:
        self.comment_url: str | None = None
        self.remote_sha: str | None = None
        self.pull: tuple[str, str] | None = None
        self.calls: list[str] = []

    async def find_comment(self, *args: Any) -> str | None:
        return self.comment_url

    async def post_comment(self, *args: Any) -> str:
        self.calls.append("comment")
        return "https://comment"

    async def branch_sha(self, *args: Any) -> str | None:
        return self.remote_sha

    async def find_pull_by_head(self, *args: Any) -> tuple[str, str] | None:
        return self.pull

    async def create_draft_pull_request(self, *args: Any, **kwargs: Any) -> str:
        self.calls.append("pull")
        return "https://pull"

    async def get_pull(self, *args: Any) -> dict[str, Any]:
        return {"head": {"ref": "topic", "repo": {"full_name": "owner/repo"}}}


class FakeRunner:
    def __init__(self, text: str = "done") -> None:
        self.text = text
        self.controls: list[tuple[str, str]] = []

    async def run(self, run_id: str, path: Path, prompt: str, on_event: Any, **kwargs: Any) -> PiResult:
        await on_event({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "working"}})
        return PiResult(self.text, str(path / "session.jsonl"))

    async def abort(self, run_id: str) -> bool:
        self.controls.append(("abort", run_id))
        return True

    async def steer(self, run_id: str, body: str) -> bool:
        self.controls.append(("steer", body))
        return True

    async def follow_up(self, run_id: str, body: str) -> bool:
        self.controls.append(("follow_up", body))
        return True


class FakeWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.dirty = False
        self.calls: list[str] = []

    def plan(self, run: dict[str, Any]) -> tuple[Path, str | None]:
        branch = f"agent-pipeline/run-{run['publication_key']}" if run["kind"] == "change" else None
        return self.root / str(run["id"]), branch

    async def prepare(self, run: dict[str, Any]) -> Workspace:
        path, branch = self.plan(run)
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(path, branch, "main", "start", "gitdir: marker\n")

    async def assert_head_unchanged(self, workspace: Workspace) -> None:
        self.calls.append("head")

    async def status(self, path: Path) -> str:
        return " M file" if self.dirty else ""

    async def verify(self, *args: Any) -> str:
        self.calls.append("verify")
        return "tests passed"

    async def commit(self, *args: Any) -> str:
        self.calls.append("commit")
        return "commit-sha"

    async def push(self, *args: Any) -> None:
        self.calls.append("push")

    async def cleanup(self, run: dict[str, Any]) -> None:
        self.calls.append("cleanup")

    async def abort(self, run_id: str) -> bool:
        self.calls.append("abort")
        return True

    def clear_cancel(self, run_id: str) -> None:
        self.calls.append("clear")


def worker_parts(tmp_path: Path, kind: str) -> tuple[WorkerPool, Database, str, FakeForge, FakeRunner, FakeWorkspace]:
    settings = Settings(
        "http://localhost",
        "secret",
        "client",
        "client-secret",
        "1",
        "private-key",
        "webhook",
        1,
        data_dir=tmp_path,
    )
    database = Database(tmp_path / "jobs.sqlite3")
    database.initialize(1)
    repository_id = database.add_repository(
        github_repo_id=1,
        installation_id=2,
        full_name="owner/repo",
        default_branch="main",
        verification_command="pytest",
    )
    run_id = database.create_run(
        repository_id=repository_id,
        source="manual",
        kind=kind,
        instruction="Do work",
        target_kind="issue",
        target_number=3,
    )
    assert run_id and database.claim_run("worker")
    forge = FakeForge()
    runner = FakeRunner()
    workspace = FakeWorkspace(tmp_path / "worktrees")
    pool = WorkerPool(settings, database, cast(GitHubForge, forge), cast(Any, runner), cast(Any, workspace))
    return pool, database, run_id, forge, runner, workspace


def test_worker_executes_advisory_and_controls(tmp_path: Path) -> None:
    pool, database, run_id, forge, runner, workspace = worker_parts(tmp_path, "advisory")

    async def exercise() -> None:
        assert await pool.control(run_id, "steer", "focus")
        assert await pool.control(run_id, "follow_up", "continue")
        assert not await pool.control(run_id, "unknown")
        await pool._execute(run_id)

    asyncio.run(exercise())
    run = database.get_run(run_id)
    assert run and run["status"] == "succeeded" and run["github_comment_url"] == "https://comment"
    assert forge.calls == ["comment"]
    assert ("steer", "focus") in runner.controls
    assert "cleanup" in workspace.calls
    assert any(event["kind"] == "text" for event in database.get_events(run_id))


def test_worker_executes_change_and_handles_failure(tmp_path: Path) -> None:
    pool, database, run_id, forge, runner, workspace = worker_parts(tmp_path, "change")
    workspace.dirty = True
    asyncio.run(pool._execute(run_id))
    run = database.get_run(run_id)
    assert run and run["status"] == "succeeded"
    assert run["verification_output"] == "tests passed" and run["commit_sha"] == "commit-sha"
    assert run["pull_request_url"] == "https://pull"
    assert workspace.calls.count("verify") == 1 and "push" in workspace.calls

    failed_pool, failed_db, failed_id, _, failed_runner, _ = worker_parts(tmp_path / "failed", "advisory")
    failed_runner.text = ""
    asyncio.run(failed_pool._execute(failed_id))
    failed = failed_db.get_run(failed_id)
    assert failed and failed["status"] == "failed" and "without output" in failed["error"]


def test_worker_recovers_verified_publication(tmp_path: Path) -> None:
    pool, database, original_id, forge, _, _ = worker_parts(tmp_path, "change")
    original = database.get_run(original_id)
    assert original
    database.update_run(
        original_id,
        status="failed",
        commit_sha="remote-sha",
        verification_output="passed",
        output_text="summary",
    )
    retry_id = database.create_run(
        repository_id=1,
        source="manual",
        kind="change",
        instruction="Do work",
        target_kind="issue",
        target_number=3,
        publication_key=str(original["publication_key"]),
    )
    assert retry_id and database.claim_run("worker")
    forge.remote_sha = "remote-sha"

    async def recover() -> None:
        retry = database.get_run(retry_id)
        assert retry and await pool._recover_change_publication(retry)

    asyncio.run(recover())
    retry = database.get_run(retry_id)
    assert retry and retry["status"] == "succeeded" and retry["pull_request_url"] == "https://pull"


def test_workspace_prepares_and_cleans_local_worktrees(tmp_path: Path) -> None:
    source = repository(tmp_path / "source")
    default_branch = git(source, "branch", "--show-current")
    git(source, "update-ref", "refs/pull/3/head", "HEAD")
    settings = Settings(
        "http://localhost",
        "secret",
        "client",
        "client-secret",
        "1",
        "private-key",
        "webhook",
        1,
        data_dir=tmp_path / "data",
    )
    settings.prepare()
    workspace = WorkspaceManager(settings, cast(GitHubForge, LocalForge(source)))
    advisory = {
        "id": "advisory",
        "publication_key": "advisory",
        "repository_id": 1,
        "full_name": "owner/repo",
        "installation_id": 2,
        "default_branch": default_branch,
        "kind": "advisory",
        "target_kind": None,
        "target_number": None,
    }
    change = {
        **advisory,
        "id": "change",
        "publication_key": "change",
        "kind": "change",
        "target_kind": "pull_request",
        "target_number": 3,
    }

    async def exercise() -> None:
        first = await workspace.prepare(advisory)
        assert first.branch is None and first.start_head == git(source, "rev-parse", "HEAD")
        advisory["worktree_path"] = str(first.path)
        await workspace.cleanup(advisory)
        second = await workspace.prepare(change)
        assert second.branch == "agent-pipeline/run-change" and second.pull_request_base == "topic"
        change["worktree_path"] = str(second.path)
        await workspace.cleanup(change)
        await workspace.cleanup(change)

    asyncio.run(exercise())


def test_workspace_command_security_and_cancel_race(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    workspace = manager(tmp_path)
    marker = (repo / ".git").read_text() if (repo / ".git").is_file() else ""

    async def exercise() -> None:
        assert await workspace.verify("verify", repo, "true") == ""
        assert await workspace.status(repo) == ""
        assert len(await workspace.head(repo)) == 40
        assert await workspace.abort("before-start")
        with pytest.raises(asyncio.CancelledError):
            await workspace._command("before-start", "true")
        with pytest.raises(RuntimeError, match="Command failed"):
            await workspace._command("", "false")
        with pytest.raises(RuntimeError, match="Verification failed"):
            await workspace._shell_command("fail", repo, "exit 2", 5)

    asyncio.run(exercise())
    assert workspace._safe_environment()["GIT_OPTIONAL_LOCKS"] == "0"
    with pytest.raises(RuntimeError, match="Invalid repository_id"):
        workspace._integer("1", "repository_id")
    if marker:
        with pytest.raises(RuntimeError, match="Git metadata"):
            workspace._assert_git_marker(repo, "wrong")
