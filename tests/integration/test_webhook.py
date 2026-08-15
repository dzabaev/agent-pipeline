import hashlib
import hmac
import json
import tempfile
import unittest
from pathlib import Path

import httpx  # pyright: ignore[reportMissingImports]

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
        self.assertEqual(claimed.kind, "plan")
        self.assertIsNone(no_second_run)

    @staticmethod
    def _headers(body: bytes) -> dict[str, str]:
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        return {
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": "issues",
            "X-Hub-Signature-256": f"sha256={digest}",
        }


if __name__ == "__main__":
    unittest.main()
