import hashlib
import hmac
import json
import unittest

from agent_pipeline.contracts import EventKind  # pyright: ignore[reportMissingImports]
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


if __name__ == "__main__":
    unittest.main()
