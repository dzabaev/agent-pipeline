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
    RunKind,
)
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]
from agent_pipeline.workflow import WorkflowProcessor  # pyright: ignore[reportMissingImports]


class FakeCodeHost:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []

    async def fetch_context(self, _event: object) -> ConversationContext:
        return ConversationContext(
            issue_number=7,
            title="Problem",
            body="Something broke",
            source_url="https://github.test/issues/7",
            comments=("alice: details",),
            base_sha="base-sha",
        )

    async def post_comment(self, issue_number: int, body: str) -> str:
        self.comments.append((issue_number, body))
        return "https://github.test/comments/1"


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
        path.mkdir()
        return path

    async def changed_files(self, _path: Path) -> tuple[str, ...]:
        return ()

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


if __name__ == "__main__":
    unittest.main()
