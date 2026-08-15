import tempfile
import unittest
from pathlib import Path
from typing import cast

from agent_pipeline.contracts import (  # pyright: ignore[reportMissingImports]
    AgentRequest,
    AgentResult,
    AgentRunner,
    CodeHost,
    ConversationContext,
    PullRequest,
    RunKind,
)
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]
from agent_pipeline.workflow import WorkflowProcessor  # pyright: ignore[reportMissingImports]


class FakeCodeHost:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.write_permission = True

    async def fetch_context(self, _event: object) -> ConversationContext:
        return ConversationContext(
            issue_number=7,
            title="Problem",
            body="Something broke",
            source_url="https://github.test/issues/7",
            comments=("alice: details",),
            base_sha="base-sha",
        )

    async def has_write_permission(self, actor: str) -> bool:
        return self.write_permission

    async def post_comment(self, issue_number: int, body: str) -> str:
        self.comments.append((issue_number, body))
        return "https://github.test/comments/1"

    async def pull_request(self, number: int) -> PullRequest:
        return PullRequest(
            number=number,
            url=f"https://github.test/pulls/{number}",
            branch="agent/plan-7",
            head_sha="plan-sha",
        )

    async def pull_request_files(self, number: int) -> dict[str, str]:
        return {"plans/issues/7.md": "added"}

    async def file_content(self, path: str, ref: str) -> str:
        return "# Approved plan\n"

    async def push_branch(self, repository: Path, branch: str) -> None:
        self.pushed = (repository, branch)

    async def open_pull_request(
        self,
        *,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        number = 43 if draft else 42
        return PullRequest(
            number=number,
            url=f"https://github.test/pulls/{number}",
            branch=branch,
            head_sha="implementation-sha" if draft else "plan-sha",
        )


class FakeAgentRunner:
    def __init__(self) -> None:
        self.requests: list[AgentRequest] = []

    async def run(self, request: AgentRequest) -> AgentResult:
        self.requests.append(request)
        return AgentResult(output="Suggested answer")


class FakeWorktrees:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[Path] = []

    async def create(
        self, run_id: str, *, ref: str, branch: str | None = None
    ) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True)
        return path

    async def changed_files(self, worktree: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(path.relative_to(worktree))
                for path in worktree.rglob("*")
                if path.is_file()
            )
        )

    async def head(self, worktree: Path) -> str:
        return "base-sha"

    async def commit(self, worktree: Path, message: str) -> str:
        self.commit_message = message
        return "plan-sha"

    async def run_command(
        self, worktree: Path, command: tuple[str, ...]
    ) -> str:
        return "tests passed"

    async def remove(self, path: Path) -> None:
        self.removed.append(path)


class WorkflowProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_review_uses_agent_boundary_and_posts_one_reply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_delivery("delivery-1", "issue_comment", "created", "{}")
            database.enqueue_run(
                delivery_id="delivery-1",
                issue_number=7,
                kind=RunKind.REVIEW,
                actor="alice",
                prompt_context="Please help",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            runner = FakeAgentRunner()
            worktrees = FakeWorktrees(root / "worktrees")
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=worktrees,
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            outcome = await processor(run)

        self.assertEqual(outcome.output, "Suggested answer")
        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(runner.requests[0].kind, RunKind.REVIEW)
        self.assertEqual(host.comments[0][0], 7)
        self.assertIn("Suggested answer", host.comments[0][1])
        self.assertIn("<!-- agent-pipeline:", host.comments[0][1])
        self.assertEqual(worktrees.removed, [runner.requests[0].worktree])

    async def test_new_issue_creates_plan_only_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_delivery("delivery-2", "issues", "opened", "{}")
            database.enqueue_run(
                delivery_id="delivery-2",
                issue_number=7,
                kind=RunKind.PLAN,
                actor="alice",
                prompt_context="Something broke",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            runner = FakeAgentRunner()
            worktrees = FakeWorktrees(root / "worktrees")
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=worktrees,
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )
            runner_output = "# Plan\n\n1. Add regression test.\n"
            runner.run = _result_runner(runner, runner_output)

            outcome = await processor(run)
            issue = database.get_issue(issue_number=7)
            plan_path = runner.requests[0].worktree / "plans/issues/7.md"
            plan_content = plan_path.read_text()

        self.assertEqual(outcome.github_url, "https://github.test/pulls/42")
        self.assertEqual(plan_content, runner_output)
        self.assertEqual(issue.plan_pr_number, 42)
        self.assertEqual(issue.plan_text, runner_output)
        self.assertEqual(host.pushed[1], "agent/plan-7")

    async def test_authorized_approval_creates_one_implementation_pr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="plan-run",
                pull_request_number=42,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )
            database.record_delivery(
                "implementation-delivery", "issue_comment", "created", "{}"
            )
            database.enqueue_run(
                delivery_id="implementation-delivery",
                issue_number=7,
                kind=RunKind.IMPLEMENTATION,
                actor="alice",
                prompt_context="yes",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            runner = FakeAgentRunner()
            worktrees = FakeWorktrees(root / "worktrees")

            async def implement(request: AgentRequest) -> AgentResult:
                runner.requests.append(request)
                (request.worktree / "feature.py").write_text("VALUE = 1\n")
                return AgentResult(output="Implemented feature")

            runner.run = implement
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=worktrees,
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            outcome = await processor(run)
            issue = database.get_issue(issue_number=7)

        self.assertEqual(outcome.github_url, "https://github.test/pulls/43")
        self.assertEqual(runner.requests[0].kind, RunKind.IMPLEMENTATION)
        self.assertEqual(issue.implementation_run_id, run.id)
        self.assertEqual(issue.implementation_pr_number, 43)
        self.assertEqual(host.pushed[1], "agent/issue-7")


def _result_runner(runner: FakeAgentRunner, output: str):
    async def run(request: AgentRequest) -> AgentResult:
        runner.requests.append(request)
        return AgentResult(output=output)

    return run


if __name__ == "__main__":
    unittest.main()
