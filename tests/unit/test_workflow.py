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
from agent_pipeline.workflow import (  # pyright: ignore[reportMissingImports]
    WorkflowProcessor,
    is_implementation_command,
)


class FakeCodeHost:
    def __init__(self) -> None:
        self.comments: list[tuple[int, str]] = []
        self.pull_request_bodies: list[str] = []
        self.pull_requests: dict[int, PullRequest] = {}
        self.next_pull_request_number: int | None = None
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
        return self.pull_requests.get(
            number,
            PullRequest(
                number=number,
                url=f"https://github.test/pulls/{number}",
                branch="agent/plan-7",
                head_sha="plan-sha",
            ),
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
        self.pull_request_bodies.append(body)
        number = self.next_pull_request_number or (43 if draft else 42)
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
        return AgentResult("Suggested answer", (), "test-model")


class FakeWorktrees:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.removed: list[Path] = []

    async def create(
        self, run_id: str, *, ref: str, branch: str | None = None
    ) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    async def changed_files(self, worktree: Path) -> tuple[str, ...]:
        return tuple(
            sorted(
                str(path.relative_to(worktree))
                for path in worktree.rglob("*")
                if path.is_file()
            )
        )

    async def snapshot(self, worktree: Path) -> tuple[tuple[str, str], ...]:
        return tuple(
            (name, (worktree / name).read_bytes().hex())
            for name in await self.changed_files(worktree)
        )

    async def restore_git_metadata(self, worktree: Path) -> None:
        return None

    async def head(self, worktree: Path) -> str:
        return "base-sha"

    async def commit(self, worktree: Path, message: str) -> str:
        self.commit_message = message
        return "plan-sha"

    async def run_command(
        self,
        worktree: Path,
        command: tuple[str, ...],
        timeout_seconds: int,
    ) -> str:
        return "tests passed"

    async def remove(self, path: Path) -> None:
        self.removed.append(path)


class ApprovalCommandTests(unittest.TestCase):
    def test_accepts_only_complete_approval_phrases(self) -> None:
        self.assertTrue(is_implementation_command("Yes."))
        self.assertTrue(is_implementation_command("/pi implement"))
        self.assertFalse(is_implementation_command("ok, but change the plan first"))
        self.assertFalse(is_implementation_command("the answer is yes"))


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
        self.assertIn(
            "> Please help\n\n@alice Suggested answer",
            host.comments[0][1],
        )
        self.assertIn("<!-- agent-pipeline:", host.comments[0][1])
        self.assertTrue(
            host.comments[0][1].endswith(
                "<sub> Made with test-model </sub>"
            )
        )
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
        self.assertTrue(
            host.pull_request_bodies[0].endswith(
                "<sub> Made with test-model </sub>"
            )
        )
        self.assertTrue(
            host.comments[0][1].endswith(
                "<sub> Made with test-model </sub>"
            )
        )

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
                return AgentResult(
                    "Implemented feature", (), "implementation-model"
                )

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
        self.assertTrue(
            host.pull_request_bodies[0].endswith(
                "<sub> Made with implementation-model </sub>"
            )
        )
        self.assertIn("> yes\n\n@alice Implementation ready:", host.comments[0][1])
        self.assertTrue(
            host.comments[0][1].endswith(
                "<sub> Made with implementation-model </sub>"
            )
        )


    async def test_decision_recreates_closed_plan_pull_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="old-plan-run",
                pull_request_number=42,
                head_sha="old-plan-sha",
                plan_text="# Old plan\n",
            )
            database.record_delivery(
                "replacement-delivery", "issue_comment", "created", "{}"
            )
            database.enqueue_run(
                delivery_id="replacement-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="alice",
                prompt_context="The first PR was discarded; create a new one.",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            host.pull_requests[42] = PullRequest(
                number=42,
                url="https://github.test/pulls/42",
                branch="agent/plan-7",
                head_sha="old-plan-sha",
                closed=True,
            )
            host.next_pull_request_number = 44
            runner = FakeAgentRunner()
            results = iter(
                (
                    AgentResult(
                        '{"action":"recreate_plan","message":"",'
                        '"evidence":"create a new one"}',
                        (),
                        "decision-model",
                    ),
                    AgentResult("# Replacement plan", (), "planning-model"),
                )
            )

            async def run_agent(request: AgentRequest) -> AgentResult:
                runner.requests.append(request)
                return next(results)

            runner.run = run_agent
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=FakeWorktrees(root / "worktrees"),
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            outcome = await processor(run)
            issue = database.get_issue(7)

        self.assertEqual([request.kind for request in runner.requests], [
            RunKind.DECISION,
            RunKind.PLAN,
        ])
        self.assertEqual(issue.plan_pr_number, 44)
        self.assertEqual(outcome.github_url, "https://github.test/pulls/44")
        if outcome.branch is None:
            self.fail("replacement branch was not recorded")
        self.assertTrue(outcome.branch.startswith("agent/plan-7-"))

    async def test_decision_blocks_recreation_without_write_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="old-plan-run",
                pull_request_number=42,
                head_sha="old-plan-sha",
                plan_text="# Old plan\n",
            )
            database.record_delivery(
                "blocked-delivery", "issue_comment", "created", "{}"
            )
            database.enqueue_run(
                delivery_id="blocked-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="mallory",
                prompt_context="Create a new plan PR.",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            host.write_permission = False
            host.pull_requests[42] = PullRequest(
                number=42,
                url="https://github.test/pulls/42",
                branch="agent/plan-7",
                head_sha="old-plan-sha",
                closed=True,
            )
            runner = FakeAgentRunner()
            runner.run = _result_runner(
                runner,
                '{"action":"recreate_plan","message":"",'
                '"evidence":"Create a new plan PR"}',
            )
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=FakeWorktrees(root / "worktrees"),
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            await processor(run)

        self.assertEqual(len(runner.requests), 1)
        self.assertIn("write access", host.comments[0][1])

    async def test_lifecycle_event_body_cannot_authorize_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="old-plan-run",
                pull_request_number=42,
                head_sha="old-plan-sha",
                plan_text="# Old plan\n",
            )
            database.record_delivery("edited-delivery", "issues", "edited", "{}")
            database.enqueue_run(
                delivery_id="edited-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="maintainer",
                prompt_context="Create a new plan PR.",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            host.pull_requests[42] = PullRequest(
                number=42,
                url="https://github.test/pulls/42",
                branch="agent/plan-7",
                head_sha="old-plan-sha",
                closed=True,
            )
            runner = FakeAgentRunner()
            runner.run = _result_runner(
                runner,
                '{"action":"recreate_plan","message":"",'
                '"evidence":"Create a new plan PR"}',
            )
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=FakeWorktrees(root / "worktrees"),
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            await processor(run)

        self.assertEqual(len(runner.requests), 1)
        self.assertIn("explicitly confirm", host.comments[0][1])

    async def test_decision_recreates_closed_implementation_pr(self) -> None:
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
            database.reserve_implementation(7, "old-implementation-run")
            database.record_implementation(
                issue_number=7,
                run_id="old-implementation-run",
                pull_request_number=43,
            )
            database.record_delivery(
                "replacement-delivery", "issue_comment", "created", "{}"
            )
            database.enqueue_run(
                delivery_id="replacement-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="alice",
                prompt_context="Create a new implementation PR.",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            host.pull_requests[43] = PullRequest(
                number=43,
                url="https://github.test/pulls/43",
                branch="agent/issue-7",
                head_sha="old-implementation-sha",
                closed=True,
            )
            host.next_pull_request_number = 45
            runner = FakeAgentRunner()
            calls = 0

            async def run_agent(request: AgentRequest) -> AgentResult:
                nonlocal calls
                calls += 1
                runner.requests.append(request)
                if calls == 1:
                    return AgentResult(
                        '{"action":"recreate_implementation","message":"",'
                        '"evidence":"Create a new implementation PR"}',
                        (),
                        "decision-model",
                    )
                (request.worktree / "feature.py").write_text("VALUE = 2\n")
                return AgentResult("Implemented replacement", (), "coding-model")

            runner.run = run_agent
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=FakeWorktrees(root / "worktrees"),
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            outcome = await processor(run)
            issue = database.get_issue(7)

        self.assertEqual([request.kind for request in runner.requests], [
            RunKind.DECISION,
            RunKind.IMPLEMENTATION,
        ])
        self.assertEqual(issue.implementation_pr_number, 45)
        self.assertEqual(outcome.github_url, "https://github.test/pulls/45")
        if outcome.branch is None:
            self.fail("replacement branch was not recorded")
        self.assertTrue(outcome.branch.startswith("agent/issue-7-"))

    async def test_uncertain_decision_asks_in_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = Database(root / "app.db")
            database.initialize()
            database.record_delivery("question-delivery", "issues", "edited", "{}")
            database.enqueue_run(
                delivery_id="question-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="alice",
                prompt_context="Maybe change this somehow",
            )
            run = database.claim_next_run()
            if run is None:
                self.fail("run was not queued")
            host = FakeCodeHost()
            runner = FakeAgentRunner()
            runner.run = _result_runner(
                runner,
                '{"action":"ask","message":"Which change do you want?",'
                '"evidence":""}',
                events=(
                    {
                        "type": "agent_end",
                        "messages": [
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "Which change do you want?",
                                    }
                                ],
                                "usage": {
                                    "input": 10,
                                    "output": 5,
                                    "cacheRead": 2,
                                    "cacheWrite": 1,
                                },
                            }
                        ],
                    },
                ),
            )
            processor = WorkflowProcessor(
                database=database,
                code_host=cast(CodeHost, host),
                agent_runner=cast(AgentRunner, runner),
                worktrees=FakeWorktrees(root / "worktrees"),
                agent_timeout_seconds=30,
                test_command="./tests.sh",
            )

            await processor(run)
            recorded_run = database.get_run(run.id)

        self.assertEqual(len(runner.requests), 1)
        self.assertEqual(runner.requests[0].kind, RunKind.DECISION)
        self.assertEqual(recorded_run.decision_action, "comment")
        self.assertEqual(recorded_run.tokens_consumed, 18)
        self.assertIn("Which change do you want?", recorded_run.agent_history_json)
        self.assertIn(
            "> Maybe change this somehow\n\n@alice Which change do you want?",
            host.comments[0][1],
        )


def _result_runner(
    runner: FakeAgentRunner,
    output: str,
    *,
    events=(),
):
    async def run(request: AgentRequest) -> AgentResult:
        runner.requests.append(request)
        return AgentResult(output, tuple(events), "test-model")

    return run


if __name__ == "__main__":
    unittest.main()
