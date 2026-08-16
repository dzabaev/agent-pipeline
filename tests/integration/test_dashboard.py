import re
import tempfile
import unittest
from pathlib import Path

import httpx  # pyright: ignore[reportMissingImports]

from agent_pipeline.contracts import RunKind, RunStatus  # pyright: ignore[reportMissingImports]
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]
from agent_pipeline.github import GitHubCodeHost  # pyright: ignore[reportMissingImports]
from agent_pipeline.main import create_app  # pyright: ignore[reportMissingImports]
from agent_pipeline.settings import Settings  # pyright: ignore[reportMissingImports]


class DashboardIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_requires_auth_and_retries_failed_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings.from_mapping(
                {
                    "GITHUB_REPOSITORY": "owner/repository",
                    "MODEL": "test-model",
                    "REASONING_LEVEL": "medium",
                    "GITHUB_TOKEN": "token",
                    "GITHUB_WEBHOOK_SECRET": "secret",
                    "GITHUB_BOT_LOGIN": "pipeline-bot",
                    "DASHBOARD_USER": "admin",
                    "DASHBOARD_PASSWORD": "password",
                },
                root=root,
            )
            database = Database(settings.database_path)
            database.initialize()
            database.record_delivery("delivery-1", "issues", "opened", "{}")
            run_id = database.enqueue_run(
                delivery_id="delivery-1",
                issue_number=7,
                kind=RunKind.PLAN,
                actor="alice",
                prompt_context="plan",
            )
            claimed = database.claim_next_run()
            if claimed is None:
                self.fail("run was not queued")
            database.fail_run(run_id, "failed deliberately")
            host = GitHubCodeHost(
                repository=settings.github_repository,
                token=settings.github_token,
                webhook_secret=settings.github_webhook_secret,
                bot_login=settings.github_bot_login,
                api_url=settings.github_api_url,
            )
            app = create_app(
                settings=settings,
                database=database,
                code_host=host,
                start_workers=False,
            )
            transport = httpx.ASGITransport(
                app=app,
                root_path="/agent_runner",
            )

            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=transport,
                    base_url="https://app.test",
                    follow_redirects=False,
                ) as client:
                    unauthorized = await client.get("/")
                    dashboard = await client.get(
                        "/", auth=("admin", "password")
                    )
                    token_match = re.search(
                        r'name="csrf"\s+value="([a-f0-9]+)"', dashboard.text
                    )
                    if token_match is None:
                        self.fail("dashboard did not render retry token")
                    retried = await client.post(
                        f"/runs/{run_id}/retry",
                        auth=("admin", "password"),
                        data={"csrf": token_match.group(1)},
                    )

            run = database.get_run(run_id)

        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("failed deliberately", dashboard.text)
        self.assertIn(
            'href="https://app.test/agent_runner/static/app.css"',
            dashboard.text,
        )
        self.assertIn(
            f'href="https://app.test/agent_runner/runs/{run_id}"',
            dashboard.text,
        )
        self.assertIn(
            f'action="https://app.test/agent_runner/runs/{run_id}/retry"',
            dashboard.text,
        )
        self.assertEqual(retried.status_code, 303)
        self.assertEqual(
            retried.headers["location"],
            f"https://app.test/agent_runner/runs/{run_id}",
        )
        self.assertEqual(run.status, RunStatus.QUEUED)


if __name__ == "__main__":
    unittest.main()
