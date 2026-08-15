# pyright: reportMissingImports=false
import hashlib
import hmac
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from app.config import Settings
from app.db import Database
from app.main import create_app


class FakeForge:
    def __init__(self) -> None:
        self.user: dict[str, Any] | BaseException = {"id": 1, "login": "alice"}
        self.repository: dict[str, Any] = {
            "id": 10,
            "full_name": "owner/repo",
            "default_branch": "main",
        }
        self.paths = ["src/main.py"]
        self.files_error = False

    def oauth_url(self, state: str, challenge: str) -> str:
        return f"https://github.test/oauth?state={state}&challenge={challenge}"

    async def authenticate_user(self, code: str, verifier: str) -> dict[str, Any]:
        if isinstance(self.user, BaseException):
            raise self.user
        return self.user

    async def get_repository(self, full_name: str, installation_id: int) -> dict[str, Any]:
        return self.repository

    async def get_pull_files(self, full_name: str, installation_id: int, number: int) -> list[str]:
        if self.files_error:
            raise RuntimeError("GitHub unavailable")
        return self.paths


class FakeWorkers:
    def __init__(self) -> None:
        self.accept = True
        self.calls: list[tuple[str, str, str | None]] = []

    async def control(self, run_id: str, kind: str, body: str | None) -> bool:
        self.calls.append((run_id, kind, body))
        return self.accept


def make_settings(tmp_path: Path) -> Settings:
    return Settings(
        "http://testserver",
        "secret-key-for-tests",
        "client",
        "client-secret",
        "1",
        "invalid-test-key",
        "webhook-secret",
        1,
        data_dir=tmp_path,
    )


def make_client(tmp_path: Path) -> tuple[TestClient, Any, Database, FakeForge, FakeWorkers]:
    app = create_app(make_settings(tmp_path))
    database = Database(tmp_path / "agent-pipeline.sqlite3")
    database.initialize(1)
    forge = FakeForge()
    workers = FakeWorkers()
    app.state.database = database
    app.state.forge = forge
    app.state.workers = workers
    return TestClient(app), app, database, forge, workers


def login(client: TestClient) -> str:
    response = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    callback = client.get(f"/auth/callback?code=code&state={state}", follow_redirects=False)
    assert callback.status_code == 303
    dashboard = client.get("/")
    match = re.search(r'name="csrf" value="([^"]+)"', dashboard.text)
    assert match
    return match.group(1)


def signed_headers(body: bytes, delivery: str, event: str) -> dict[str, str]:
    signature = "sha256=" + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest()
    return {
        "x-hub-signature-256": signature,
        "x-github-delivery": delivery,
        "x-github-event": event,
        "content-type": "application/json",
    }


def payload(*, sender_type: str = "User") -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"id": 10, "full_name": "owner/repo"},
        "installation": {"id": 20},
        "sender": {"login": "alice", "type": sender_type},
        "issue": {
            "number": 3,
            "title": "Bug",
            "body": "Please review",
            "labels": [],
            "author_association": "OWNER",
        },
    }


def add_repository(database: Database) -> int:
    return database.add_repository(
        github_repo_id=10,
        installation_id=20,
        full_name="owner/repo",
        default_branch="main",
        verification_command="true",
    )


def test_login_oauth_and_csrf_security(tmp_path: Path) -> None:
    client, _, _, forge, _ = make_client(tmp_path)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert "Sign in with GitHub" in client.get("/login").text
    assert client.post("/logout").status_code == 401
    assert client.get("/auth/callback?code=x&state=wrong").status_code == 400

    response = client.get("/auth/github", follow_redirects=False)
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    forge.user = PermissionError("not allowed")
    assert client.get(f"/auth/callback?code=x&state={state}").status_code == 403

    forge.user = {"id": 1, "login": "alice"}
    csrf = login(client)
    assert client.get("/login", follow_redirects=False).status_code == 303
    assert client.post("/logout", data={"csrf": "wrong"}).status_code == 403
    assert client.post("/logout", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.get("/").history


def test_repository_rule_and_concurrency_routes(tmp_path: Path) -> None:
    client, _, database, forge, _ = make_client(tmp_path)
    csrf = login(client)
    assert client.post("/repositories", data={"csrf": csrf, "full_name": "bad"}).status_code == 400
    assert client.post(
        "/repositories",
        data={"csrf": csrf, "full_name": "owner/repo", "installation_id": "x", "verification_command": "true"},
    ).status_code == 400
    forge.repository = {"id": 10}
    assert client.post(
        "/repositories",
        data={"csrf": csrf, "full_name": "owner/repo", "installation_id": "20", "verification_command": "true"},
    ).status_code == 502
    forge.repository = {"id": 10, "full_name": "owner/repo", "default_branch": "main"}
    assert client.post(
        "/repositories",
        data={"csrf": csrf, "full_name": "owner/repo", "installation_id": "20", "verification_command": "true"},
        follow_redirects=False,
    ).status_code == 303
    assert client.post("/repositories/1/toggle", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    database.toggle_repository(1)

    assert client.post("/rules", data={"csrf": csrf, "event": "bad"}).status_code == 400
    rule = {
        "csrf": csrf,
        "repository_id": "1",
        "name": "Review",
        "event": "issues",
        "actions": "opened, edited",
        "kind": "advisory",
        "instruction": "Review issue",
        "labels_all": "agent, bug",
        "draft": "false",
    }
    assert client.post("/rules", data=rule, follow_redirects=False).status_code == 303
    stored = database.list_rules(1)[0]
    assert json.loads(stored["actions_json"]) == ["opened", "edited"]
    assert client.post(f"/rules/{stored['id']}/toggle", data={"csrf": csrf}, follow_redirects=False).status_code == 303
    assert client.post("/settings/concurrency", data={"csrf": csrf, "worker_concurrency": "2"}).status_code == 400
    assert client.post(
        "/settings/concurrency", data={"csrf": csrf, "worker_concurrency": "1"}, follow_redirects=False
    ).status_code == 303


def test_run_routes_retry_and_controls(tmp_path: Path) -> None:
    client, _, database, _, workers = make_client(tmp_path)
    csrf = login(client)
    repository_id = add_repository(database)
    base = {"csrf": csrf, "repository_id": str(repository_id), "kind": "advisory", "instruction": "Review"}
    assert client.post("/runs", data={**base, "repository_id": "99"}).status_code == 404
    assert client.post("/runs", data={**base, "kind": "bad"}).status_code == 400
    assert client.post("/runs", data={**base, "target_kind": "bad", "target_number": "3"}).status_code == 400
    assert client.post("/runs", data={**base, "target_kind": "issue"}).status_code == 400
    created = client.post("/runs", data={**base, "target_kind": "issue", "target_number": "3"}, follow_redirects=False)
    assert created.status_code == 303
    run_id = created.headers["location"].rsplit("/", 1)[-1]
    assert client.get(f"/runs/{run_id}").status_code == 200
    assert client.get(f"/runs/{run_id}/fragment").status_code == 200
    assert client.get("/runs/missing").status_code == 404
    assert client.get("/runs/missing/fragment").status_code == 404
    assert client.post(f"/runs/{run_id}/retry", data={"csrf": csrf}).status_code == 409

    database.update_run(run_id, status="failed")
    retried = client.post(f"/runs/{run_id}/retry", data={"csrf": csrf}, follow_redirects=False)
    assert retried.status_code == 303
    retry_id = retried.headers["location"].rsplit("/", 1)[-1]
    assert retry_id != run_id

    assert client.post(f"/runs/{retry_id}/unknown", data={"csrf": csrf}).status_code == 404
    assert client.post(f"/runs/{retry_id}/steer", data={"csrf": csrf}).status_code == 400
    response = client.post(
        f"/runs/{retry_id}/follow-up", data={"csrf": csrf, "message": "continue"}, follow_redirects=False
    )
    assert response.status_code == 303 and workers.calls[-1] == (retry_id, "follow_up", "continue")
    workers.accept = False
    assert client.post(f"/runs/{retry_id}/cancel", data={"csrf": csrf}).status_code == 409


def test_webhook_validation_deduplication_and_dispositions(tmp_path: Path) -> None:
    client, app, database, _, _ = make_client(tmp_path)
    raw = json.dumps(payload()).encode()
    assert client.post("/webhooks/github", content=raw).status_code == 401
    assert client.post(
        "/webhooks/github", content=raw, headers={**signed_headers(raw, "d", "issues"), "content-length": "1048577"}
    ).status_code == 413
    assert client.post("/webhooks/github", content=b"{", headers=signed_headers(b"{", "bad", "issues")).status_code == 400
    assert client.post("/webhooks/github", content=b"[]", headers=signed_headers(b"[]", "list", "issues")).status_code == 400

    ping = client.post("/webhooks/github", content=b"{}", headers=signed_headers(b"{}", "ping", "ping"))
    assert ping.status_code == 202 and ping.json()["ok"]
    unsupported = client.post(
        "/webhooks/github", content=b"{}", headers=signed_headers(b"{}", "push", "push")
    )
    assert unsupported.json()["ignored"] == "unsupported event"
    invalid = json.dumps({"repository": None}).encode()
    response = client.post("/webhooks/github", content=invalid, headers=signed_headers(invalid, "invalid", "issues"))
    assert response.json()["ignored"] == "invalid payload"

    add_repository(database)
    bot = json.dumps(payload(sender_type="Bot")).encode()
    assert client.post("/webhooks/github", content=bot, headers=signed_headers(bot, "bot", "issues")).json()["matched"] == 0
    database.add_rule(
        repository_id=1,
        name="Review issues",
        event="issues",
        actions=["opened"],
        kind="advisory",
        instruction="Review this issue",
        filters={},
        model=None,
        thinking_level=None,
    )
    first = client.post("/webhooks/github", content=raw, headers=signed_headers(raw, "delivery-1", "issues"))
    second = client.post("/webhooks/github", content=raw, headers=signed_headers(raw, "delivery-1", "issues"))
    assert first.json() == {"matched": 1} and second.json() == {"duplicate": True}
    assert len(database.list_runs()) == 1
    object.__setattr__(app.state.settings, "github_bot_login", "alice")
    ignored = client.post("/webhooks/github", content=raw, headers=signed_headers(raw, "self", "issues"))
    assert ignored.json()["matched"] == 0
