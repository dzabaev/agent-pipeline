import hashlib
import hmac
import json
import unittest

import httpx  # pyright: ignore[reportMissingImports]

from agent_pipeline.contracts import (  # pyright: ignore[reportMissingImports]
    CodeHostEvent,
    EventKind,
)  # pyright: ignore[reportMissingImports]
from agent_pipeline.github import (  # pyright: ignore[reportMissingImports]
    GitHubCodeHost,
    WebhookRejected,
)


class GitHubWebhookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.host = GitHubCodeHost(
            repository="owner/repository",
            token="token",
            webhook_secret="secret",
            bot_login="pipeline-bot",
            api_url="https://api.github.test",
        )

    def test_verifies_and_normalizes_opened_issue(self) -> None:
        body = json.dumps(
            {
                "action": "opened",
                "issue": {
                    "number": 12,
                    "title": "Broken thing",
                    "body": "Please fix it",
                    "html_url": "https://github.test/issues/12",
                },
                "sender": {"login": "alice", "type": "User"},
            }
        ).encode()
        event = self.host.parse_webhook(self._headers("issues", body), body)

        if event is None:
            self.fail("issue event was ignored")
        self.assertEqual(event.kind, EventKind.ISSUE_OPENED)
        self.assertEqual(event.issue_number, 12)
        self.assertEqual(event.actor, "alice")
        self.assertEqual(event.body, "Please fix it")

    def test_rejects_invalid_signature(self) -> None:
        body = b"{}"
        headers = self._headers("issues", body) | {
            "X-Hub-Signature-256": "sha256=invalid"
        }

        with self.assertRaises(WebhookRejected):
            self.host.parse_webhook(headers, body)

    def _headers(self, event: str, body: bytes) -> dict[str, str]:
        digest = hmac.new(b"secret", body, hashlib.sha256).hexdigest()
        return {
            "X-GitHub-Delivery": "delivery-1",
            "X-GitHub-Event": event,
            "X-Hub-Signature-256": f"sha256={digest}",
        }


class GitHubApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_normalized_context_and_write_permission(self) -> None:
        def respond(request: httpx.Request) -> httpx.Response:
            responses = {
                "/repos/owner/repository": {"default_branch": "main"},
                "/repos/owner/repository/commits/main": {"sha": "base-sha"},
                "/repos/owner/repository/issues/12": {
                    "title": "Broken thing",
                    "body": "Please fix it",
                    "html_url": "https://github.test/issues/12",
                },
                "/repos/owner/repository/issues/12/comments": [
                    {"user": {"login": "bob"}, "body": "I can reproduce"}
                ],
                "/repos/owner/repository/collaborators/alice/permission": {
                    "permission": "write"
                },
            }
            return httpx.Response(200, json=responses[request.url.path])

        transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            host: GitHubCodeHost = GitHubCodeHost(
                repository="owner/repository",
                token="token",
                webhook_secret="secret",
                bot_login="pipeline-bot",
                api_url="https://api.github.test",
                client=client,
            )
            event = CodeHostEvent(
                delivery_id="delivery-1",
                kind=EventKind.ISSUE_OPENED,
                event_name="issues",
                action="opened",
                issue_number=12,
                actor="alice",
            )

            context = await host.fetch_context(event)
            can_write = await host.has_write_permission("alice")

        self.assertEqual(context.title, "Broken thing")
        self.assertEqual(context.comments, ("bob: I can reproduce",))
        self.assertEqual(context.default_branch, "main")
        self.assertEqual(context.base_sha, "base-sha")
        self.assertTrue(can_write)

    async def test_closed_branch_pull_request_prevents_duplicate_publication(self) -> None:
        requests: list[httpx.Request] = []

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(
                200,
                json=[
                    {
                        "number": 44,
                        "html_url": "https://github.test/pull/44",
                        "head": {"ref": "agent/issue-12", "sha": "head"},
                        "base": {"ref": "main"},
                        "merged": False,
                    }
                ],
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond)
        ) as client:
            host = GitHubCodeHost(
                repository="owner/repository",
                token="token",
                webhook_secret="secret",
                bot_login="pipeline-bot",
                api_url="https://api.github.test",
                client=client,
            )
            pull_request = await host.open_pull_request(
                issue_number=12,
                branch="agent/issue-12",
                title="Implementation",
                body="Body",
                draft=True,
            )

        self.assertEqual(pull_request.number, 44)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.params["state"], "all")


if __name__ == "__main__":
    unittest.main()
