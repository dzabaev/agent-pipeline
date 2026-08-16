import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import httpx  # pyright: ignore[reportMissingImports]

from agent_pipeline.contracts import RunKind  # pyright: ignore[reportMissingImports]
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]
from agent_pipeline.github import GitHubCodeHost  # pyright: ignore[reportMissingImports]
from agent_pipeline.main import create_app  # pyright: ignore[reportMissingImports]


class WebhookIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_delivery_queues_only_one_plan_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            host = GitHubCodeHost(
                repository="owner/repository",
                token="token",
                webhook_secret="secret",
                bot_login="pipeline-bot",
                api_url="https://api.github.test",
            )
            app = create_app(database=database, code_host=host, start_workers=False)
            body = json.dumps(
                {
                    "action": "opened",
                    "issue": {
                        "number": 12,
                        "body": "Please fix it",
                        "html_url": "https://github.test/issues/12",
                    },
                    "sender": {"login": "alice", "type": "User"},
                }
            ).encode()
            headers = self._headers(body)

            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=transport, base_url="https://app.test"
                ) as client:
                    first = await client.post(
                        "/webhooks/github", content=body, headers=headers
                    )
                    duplicate = await client.post(
                        "/webhooks/github", content=body, headers=headers
                    )

            claimed = database.claim_next_run()
            no_second_run = database.claim_next_run()

        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()["status"], "queued")
        self.assertEqual(duplicate.status_code, 202)
        self.assertEqual(duplicate.json()["status"], "duplicate")
        if claimed is None:
            self.fail("plan run was not queued")
        self.assertEqual(claimed.issue_number, 12)
        self.assertEqual(claimed.kind, RunKind.DECISION)
        self.assertIsNone(no_second_run)

    async def test_plan_pull_request_comment_maps_back_to_issue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            plan_run_id = database.ingest_run(
                delivery_id="plan-delivery",
                event="issues",
                action="opened",
                payload_json="{}",
                issue_number=12,
                kind=RunKind.PLAN,
                actor="alice",
                prompt_context="Plan this",
            )
            if plan_run_id is None:
                self.fail("plan run was not queued")
            database.record_plan(
                issue_number=12,
                run_id=plan_run_id,
                pull_request_number=101,
                head_sha="plan-head",
                plan_text="Plan",
            )
            claimed_plan = database.claim_next_run()
            if claimed_plan is None:
                self.fail("plan run was not claimable")
            database.finish_run(claimed_plan.id, output="done")

            host = GitHubCodeHost(
                repository="owner/repository",
                token="token",
                webhook_secret="secret",
                bot_login="pipeline-bot",
                api_url="https://api.github.test",
            )
            app = create_app(database=database, code_host=host, start_workers=False)
            body = json.dumps(
                {
                    "action": "created",
                    "issue": {"number": 101, "pull_request": {}},
                    "comment": {"body": "Please revise", "user": {"login": "alice"}},
                    "sender": {"login": "alice", "type": "User"},
                }
            ).encode()
            headers = self._headers(
                body,
                delivery="comment-delivery",
                event="issue_comment",
            )
            forged_body = json.dumps(
                {
                    "action": "closed",
                    "pull_request": {
                        "number": 999,
                        "merged": True,
                        "head": {"ref": "agent/plan-12"},
                    },
                    "sender": {"login": "alice", "type": "User"},
                }
            ).encode()
            forged_headers = self._headers(
                forged_body,
                delivery="forged-merge",
                event="pull_request",
            )

            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="https://app.test",
                ) as client:
                    response = await client.post(
                        "/webhooks/github", content=body, headers=headers
                    )
                    forged = await client.post(
                        "/webhooks/github",
                        content=forged_body,
                        headers=forged_headers,
                    )
            claimed = database.claim_next_run()
            no_forged_run = database.claim_next_run()

        self.assertEqual(response.status_code, 202)
        self.assertEqual(forged.json()["status"], "ignored")
        if claimed is None:
            self.fail("review run was not queued")
        self.assertEqual(claimed.issue_number, 12)
        self.assertEqual(claimed.reply_number, 101)
        self.assertEqual(claimed.kind, RunKind.DECISION)
        self.assertIsNone(no_forged_run)

    @staticmethod
    def _headers(
        body: bytes,
        *,
        delivery: str = "delivery-1",
        event: str = "issues",
    ) -> dict[str, str]:
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        return {
            "X-GitHub-Delivery": delivery,
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": f"sha256={digest}",
        }


if __name__ == "__main__":
    unittest.main()
