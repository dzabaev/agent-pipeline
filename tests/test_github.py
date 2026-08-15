# pyright: reportMissingImports=false
import asyncio
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.config import Settings
from app.github import (
    EventFacts,
    GitHubForge,
    normalize_event,
    pkce_challenge,
    rule_matches,
    rule_needs_changed_paths,
    verify_signature,
)


def settings(tmp_path: Path) -> Settings:
    return Settings(
        "https://agent.test",
        "secret",
        "client",
        "client-secret",
        "1",
        "private-key",
        "webhook",
        7,
        data_dir=tmp_path,
        github_api_url="https://api.test",
    )


def issue_payload() -> dict[str, Any]:
    return {
        "action": "opened",
        "repository": {"id": 1, "full_name": "owner/repo"},
        "installation": {"id": 2},
        "sender": {"login": "alice", "type": "User"},
        "issue": {
            "number": 3,
            "title": "Bug",
            "body": "Please fix parser",
            "labels": [{"name": "agent"}, {"name": "bug"}],
            "author_association": "OWNER",
            "html_url": "https://github.test/owner/repo/issues/3",
        },
    }


def test_signature_normalization_and_rule_filters() -> None:
    body = json.dumps(issue_payload()).encode()
    signature = "sha256=" + hmac.new(b"secret", body, hashlib.sha256).hexdigest()
    assert verify_signature("secret", body, signature)
    assert not verify_signature("wrong", body, signature)
    assert not verify_signature("secret", body, None)
    assert normalize_event("push", issue_payload()) is None

    facts = normalize_event("issues", issue_payload())
    assert facts and facts.subject_kind == "issue" and facts.subject_number == 3
    facts.changed_paths = ["src/parser.py"]
    filters = {
        "subject_kind": "issue",
        "labels_all": ["agent"],
        "labels_any": ["bug", "feature"],
        "sender_allow": ["alice"],
        "author_associations": ["OWNER"],
        "body_contains_any": ["FIX"],
        "changed_paths_any": ["src/*.py"],
        "draft": False,
    }
    rule = {
        "enabled": 1,
        "event": "issues",
        "actions_json": '["opened"]',
        "filters_json": json.dumps(filters),
    }
    facts.draft = False
    assert rule_matches(rule, facts)
    for key, value in {
        "subject_kind": "pull_request",
        "labels_all": ["missing"],
        "labels_any": ["docs"],
        "sender_allow": ["bob"],
        "author_associations": ["NONE"],
        "base_branches": ["main"],
        "review_states": ["approved"],
        "body_contains_any": ["absent"],
        "changed_paths_any": ["docs/**"],
        "draft": True,
    }.items():
        changed = dict(filters)
        changed[key] = value
        rule["filters_json"] = json.dumps(changed)
        assert not rule_matches(rule, facts), key
    assert not rule_matches({**rule, "enabled": 0}, facts)
    assert not rule_matches({**rule, "event": "pull_request"}, facts)
    assert not rule_matches({**rule, "filters_json": "{"}, facts)
    assert rule_needs_changed_paths({**rule, "filters_json": '{"changed_paths_any":["src/**"]}'})
    assert not rule_needs_changed_paths({**rule, "enabled": 0})
    assert not rule_needs_changed_paths({**rule, "filters_json": "{"})


def test_all_supported_event_shapes_and_invalid_payloads() -> None:
    comment = issue_payload()
    comment["comment"] = {"body": "run review", "author_association": "MEMBER"}
    comment["issue"]["pull_request"] = {"url": "https://api.test/pulls/3"}
    facts = normalize_event("issue_comment", comment)
    assert facts and facts.subject_kind == "pull_request" and facts.body == "run review"

    pull = issue_payload()
    pull["pull_request"] = {
        "number": 4,
        "title": "PR",
        "body": "change",
        "labels": [],
        "author_association": "MEMBER",
        "base": {"ref": "main"},
        "head": {"ref": "topic", "sha": "abc", "repo": {"full_name": "owner/repo"}},
        "draft": True,
    }
    facts = normalize_event("pull_request", pull)
    assert facts and facts.base_branch == "main" and facts.head_sha == "abc" and facts.draft
    pull["review"] = {"body": "LGTM", "state": "APPROVED", "author_association": "OWNER"}
    facts = normalize_event("pull_request_review", pull)
    assert facts and facts.review_state == "approved" and facts.body == "LGTM"

    with pytest.raises(ValueError, match="repository"):
        normalize_event("issues", {"repository": None})
    broken = issue_payload()
    broken["repository"]["id"] = "1"
    with pytest.raises(ValueError, match="repository.id"):
        normalize_event("issues", broken)


def test_oauth_and_github_api_behaviors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[httpx.Request] = []
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        requests.append(request)
        path = request.url.path
        if str(request.url) == "https://github.com/login/oauth/access_token":
            return httpx.Response(200, json={"access_token": "oauth"})
        if path == "/user":
            return httpx.Response(200, json={"id": 7, "login": "alice"})
        if path.endswith("/access_tokens"):
            token_calls += 1
            return httpx.Response(201, json={"token": "installation"})
        if path == "/repos/owner/repo":
            return httpx.Response(200, json={"id": 1, "full_name": "owner/repo"})
        if path == "/repos/owner/repo/pulls/3":
            return httpx.Response(200, json={"number": 3})
        if path.endswith("/pulls/3/files"):
            return httpx.Response(200, json=[{"filename": "src/a.py"}, {"bad": True}])
        if path.endswith("/issues/3/comments") and request.method == "GET":
            return httpx.Response(200, json=[{"body": "marker", "html_url": "https://comment"}])
        if "/git/ref/heads/" in path:
            return httpx.Response(200, json={"object": {"sha": "branch-sha"}})
        if path.endswith("/pulls") and request.method == "GET":
            return httpx.Response(
                200,
                json=[{"html_url": "https://pull", "head": {"ref": "topic", "sha": "pr-sha"}}],
            )
        if path.endswith("/comments") and request.method == "POST":
            return httpx.Response(201, json={"html_url": "https://new-comment"})
        if path.endswith("/pulls") and request.method == "POST":
            return httpx.Response(201, json={"html_url": "https://new-pull"})
        return httpx.Response(500, json={"message": f"unexpected {request.method} {path}"})

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        forge = GitHubForge(settings(tmp_path), client)
        monkeypatch.setattr(forge, "app_jwt", lambda: "jwt")
        url = forge.oauth_url("state", "challenge")
        query = parse_qs(urlparse(url).query)
        assert query["state"] == ["state"] and query["code_challenge"] == ["challenge"]
        assert await forge.authenticate_user("code", "verifier") == {"id": 7, "login": "alice"}
        assert await forge.installation_token(2, "Owner/Repo") == "installation"
        assert await forge.installation_token(2, "owner/repo") == "installation"
        remote, environment = await forge.git_access("owner/repo", 2)
        assert remote.endswith("owner/repo.git") and "Authorization: Basic" in environment["GIT_CONFIG_VALUE_0"]
        assert (await forge.get_repository("owner/repo", 2))["id"] == 1
        assert (await forge.get_pull("owner/repo", 2, 3))["number"] == 3
        assert await forge.get_pull_files("owner/repo", 2, 3) == ["src/a.py"]
        assert await forge.find_comment("owner/repo", 2, 3, "marker") == "https://comment"
        assert await forge.branch_sha("owner/repo", 2, "topic/slash") == "branch-sha"
        assert await forge.find_pull_by_head("owner/repo", 2, "topic") == ("https://pull", "pr-sha")
        assert await forge.post_comment("owner/repo", 2, 3, "x" * 70_000) == "https://new-comment"
        assert await forge.create_draft_pull_request(
            "owner/repo", 2, title="t" * 300, head="topic", base="main", body="body"
        ) == "https://new-pull"
        await forge.close()

    asyncio.run(exercise())
    assert token_calls == 1
    posted = [request for request in requests if request.method == "POST" and request.url.path.endswith("/comments")]
    assert len(json.loads(posted[0].content)["body"].encode()) == 60_000
    assert pkce_challenge("verifier")


def test_github_api_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [
            httpx.Response(404, json={"message": "missing"}),
            httpx.Response(200, json=[]),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(500, json={"message": "down"}),
            httpx.Response(200, json={}),
            httpx.Response(404, json={}),
            httpx.Response(200, json={"object": {}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return next(responses)

    async def exercise() -> None:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        forge = GitHubForge(settings(tmp_path), client)
        with pytest.raises(RuntimeError, match=r"failed \(404\)"):
            await forge._request("GET", "/missing", token="token")
        with pytest.raises(RuntimeError, match="unexpected data"):
            await forge._request("GET", "/list", token="token")
        with pytest.raises(RuntimeError, match="invalid JSON"):
            await forge._get_list("/bad-json", "token", {})
        with pytest.raises(RuntimeError, match=r"failed \(500\)"):
            await forge._get_list("/down", "token", {})
        with pytest.raises(RuntimeError, match="installation token"):
            monkeypatch.setattr(forge, "app_jwt", lambda: "jwt")
            await forge.installation_token(2, "owner/repo")
        monkeypatch.setattr(forge, "installation_token", lambda *_: _value("token"))
        assert await forge.branch_sha("owner/repo", 2, "missing") is None
        with pytest.raises(RuntimeError, match="no commit SHA"):
            await forge.branch_sha("owner/repo", 2, "bad")
        await client.aclose()

    asyncio.run(exercise())


async def _value(value: str) -> str:
    return value
