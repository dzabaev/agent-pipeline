# pyright: reportMissingImports=false
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import asdict, dataclass, field
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import quote, urlencode

import httpx
import jwt

from app.config import Settings


SUPPORTED_EVENTS = {"issues", "issue_comment", "pull_request", "pull_request_review"}


@dataclass(slots=True)
class EventFacts:
    event: str
    action: str
    repository_id: int
    repository: str
    installation_id: int
    sender: str
    sender_type: str
    subject_kind: str
    subject_number: int
    title: str
    body: str
    labels: list[str]
    author_association: str
    base_branch: str | None = None
    head_branch: str | None = None
    head_repository: str | None = None
    head_sha: str | None = None
    review_state: str | None = None
    draft: bool | None = None
    html_url: str | None = None
    changed_paths: list[str] = field(default_factory=list)

    def context(self) -> dict[str, Any]:
        return asdict(self)


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def _integer(value: Any, field: str) -> int:
    if not isinstance(value, int):
        raise ValueError(f"Missing or invalid {field}")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"Missing or invalid {field}")
    return value


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _truncate_utf8(value: str, limit: int) -> str:
    return value.encode()[:limit].decode(errors="ignore")


def normalize_event(event: str, payload: dict[str, Any]) -> EventFacts | None:
    if event not in SUPPORTED_EVENTS:
        return None
    action = _text(payload.get("action"))
    repository = _mapping(payload.get("repository"), "repository")
    installation = _mapping(payload.get("installation"), "installation")
    sender = _mapping(payload.get("sender"), "sender")

    subject: dict[str, Any]
    subject_kind: str
    body_source: dict[str, Any]
    review_state: str | None = None
    if event == "issues":
        subject = _mapping(payload.get("issue"), "issue")
        subject_kind = "issue"
        body_source = subject
    elif event == "issue_comment":
        subject = _mapping(payload.get("issue"), "issue")
        subject_kind = "pull_request" if isinstance(subject.get("pull_request"), dict) else "issue"
        body_source = _mapping(payload.get("comment"), "comment")
    elif event == "pull_request_review":
        subject = _mapping(payload.get("pull_request"), "pull_request")
        subject_kind = "pull_request"
        body_source = _mapping(payload.get("review"), "review")
        review_state = _text(body_source.get("state")).lower() or None
    else:
        subject = _mapping(payload.get("pull_request"), "pull_request")
        subject_kind = "pull_request"
        body_source = subject

    labels = [
        _text(label.get("name"))
        for label in subject.get("labels", [])
        if isinstance(label, dict) and _text(label.get("name"))
    ]
    base_value = subject.get("base")
    head_value = subject.get("head")
    base: dict[str, Any] = base_value if isinstance(base_value, dict) else {}
    head: dict[str, Any] = head_value if isinstance(head_value, dict) else {}
    head_repo_value = head.get("repo")
    head_repo: dict[str, Any] = head_repo_value if isinstance(head_repo_value, dict) else {}

    return EventFacts(
        event=event,
        action=action,
        repository_id=_integer(repository.get("id"), "repository.id"),
        repository=_text(repository.get("full_name")),
        installation_id=_integer(installation.get("id"), "installation.id"),
        sender=_text(sender.get("login")),
        sender_type=_text(sender.get("type")),
        subject_kind=subject_kind,
        subject_number=_integer(subject.get("number"), "subject.number"),
        title=_text(subject.get("title")),
        body=_text(body_source.get("body")),
        labels=labels,
        author_association=_text(body_source.get("author_association") or subject.get("author_association")),
        base_branch=_text(base.get("ref")) or None,
        head_branch=_text(head.get("ref")) or None,
        head_repository=_text(head_repo.get("full_name")) or None,
        head_sha=_text(head.get("sha")) or None,
        review_state=review_state,
        draft=subject.get("draft") if isinstance(subject.get("draft"), bool) else None,
        html_url=_text(subject.get("html_url")) or None,
    )


def _list(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def rule_matches(rule: dict[str, Any], facts: EventFacts) -> bool:
    if not rule.get("enabled") or rule.get("event") != facts.event:
        return False
    try:
        actions = _list(json.loads(_text(rule.get("actions_json"))))
        filters = json.loads(_text(rule.get("filters_json")) or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(filters, dict) or (actions and facts.action not in actions):
        return False

    subject_kind = _text(filters.get("subject_kind"))
    if subject_kind and subject_kind != "any" and subject_kind != facts.subject_kind:
        return False
    labels = set(facts.labels)
    labels_all = set(_list(filters.get("labels_all")))
    labels_any = set(_list(filters.get("labels_any")))
    if labels_all and not labels_all.issubset(labels):
        return False
    if labels_any and labels.isdisjoint(labels_any):
        return False
    sender_allow = _list(filters.get("sender_allow"))
    if sender_allow and facts.sender not in sender_allow:
        return False
    associations = _list(filters.get("author_associations"))
    if associations and facts.author_association not in associations:
        return False
    branches = _list(filters.get("base_branches"))
    if branches and facts.base_branch not in branches:
        return False
    review_states = [state.lower() for state in _list(filters.get("review_states"))]
    if review_states and facts.review_state not in review_states:
        return False
    needles = [needle.lower() for needle in _list(filters.get("body_contains_any"))]
    if needles and not any(needle in facts.body.lower() for needle in needles):
        return False
    path_patterns = _list(filters.get("changed_paths_any"))
    if path_patterns and not any(
        fnmatchcase(path, pattern) for path in facts.changed_paths for pattern in path_patterns
    ):
        return False
    draft = filters.get("draft")
    return not isinstance(draft, bool) or facts.draft is draft


def rule_needs_changed_paths(rule: dict[str, Any]) -> bool:
    if not rule.get("enabled"):
        return False
    try:
        filters = json.loads(_text(rule.get("filters_json")) or "{}")
    except json.JSONDecodeError:
        return False
    return isinstance(filters, dict) and bool(_list(filters.get("changed_paths_any")))


class GitHubForge:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._tokens: dict[tuple[int, str], tuple[str, float]] = {}

    async def close(self) -> None:
        if self._owns_client:
            await self.client.aclose()

    def oauth_url(self, state: str, challenge: str) -> str:
        query = urlencode(
            {
                "client_id": self.settings.github_client_id,
                "redirect_uri": f"{self.settings.base_url}/auth/callback",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"https://github.com/login/oauth/authorize?{query}"

    async def authenticate_user(self, code: str, verifier: str) -> dict[str, Any]:
        response = await self.client.post(
            "https://github.com/login/oauth/access_token",
            headers={"Accept": "application/json"},
            data={
                "client_id": self.settings.github_client_id,
                "client_secret": self.settings.github_client_secret,
                "code": code,
                "redirect_uri": f"{self.settings.base_url}/auth/callback",
                "code_verifier": verifier,
            },
        )
        data = self._json(response, "OAuth exchange")
        token = _text(data.get("access_token"))
        if not token:
            raise RuntimeError(f"GitHub OAuth failed: {_text(data.get('error_description')) or 'missing token'}")
        user = await self._request("GET", "/user", token=token)
        if _integer(user.get("id"), "user.id") != self.settings.github_allowed_user_id:
            raise PermissionError("GitHub account is not allowed")
        return {"id": user["id"], "login": _text(user.get("login"))}

    def app_jwt(self) -> str:
        issued = time.time_ns() // 1_000_000_000 - 60
        return jwt.encode(
            {"iat": issued, "exp": issued + 600, "iss": self.settings.github_app_id},
            self.settings.github_private_key,
            algorithm="RS256",
        )

    async def installation_token(self, installation_id: int, repository: str) -> str:
        key = (installation_id, repository.lower())
        cached = self._tokens.get(key)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        name = repository.rsplit("/", 1)[-1]
        data = await self._request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            token=self.app_jwt(),
            payload={"repositories": [name]},
        )
        token = _text(data.get("token"))
        if not token:
            raise RuntimeError("GitHub did not return an installation token")
        self._tokens[key] = (token, time.time() + 50 * 60)
        return token

    async def git_access(self, full_name: str, installation_id: int) -> tuple[str, dict[str, str]]:
        token = await self.installation_token(installation_id, full_name)
        encoded = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        return (
            f"https://github.com/{full_name}.git",
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Basic {encoded}",
                "GIT_TERMINAL_PROMPT": "0",
            },
        )

    async def get_repository(self, full_name: str, installation_id: int) -> dict[str, Any]:
        token = await self.installation_token(installation_id, full_name)
        return await self._request("GET", f"/repos/{full_name}", token=token)

    async def get_pull(self, full_name: str, installation_id: int, number: int) -> dict[str, Any]:
        token = await self.installation_token(installation_id, full_name)
        return await self._request("GET", f"/repos/{full_name}/pulls/{number}", token=token)

    async def get_pull_files(self, full_name: str, installation_id: int, number: int) -> list[str]:
        token = await self.installation_token(installation_id, full_name)
        paths: list[str] = []
        for page in range(1, 31):
            response = await self.client.get(
                f"{self.settings.github_api_url}/repos/{full_name}/pulls/{number}/files",
                headers={
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {token}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "agent-pipeline",
                },
                params={"per_page": 100, "page": page},
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("GitHub pull files returned invalid JSON") from exc
            if response.is_error:
                message = _text(data.get("message")) if isinstance(data, dict) else "request failed"
                raise RuntimeError(f"GitHub pull files failed ({response.status_code}): {message}")
            if not isinstance(data, list):
                raise RuntimeError("GitHub pull files returned unexpected data")
            for item in data:
                if isinstance(item, dict) and isinstance(item.get("filename"), str):
                    paths.append(item["filename"])
            if len(data) < 100:
                return paths
        raise RuntimeError("Pull request has more than 3000 changed files")

    async def find_comment(
        self, full_name: str, installation_id: int, number: int, marker: str
    ) -> str | None:
        token = await self.installation_token(installation_id, full_name)
        for page in range(1, 31):
            comments = await self._get_list(
                f"/repos/{full_name}/issues/{number}/comments",
                token,
                {"per_page": 100, "page": page},
            )
            for comment in comments:
                if isinstance(comment, dict) and marker in _text(comment.get("body")):
                    return _text(comment.get("html_url")) or None
            if len(comments) < 100:
                return None
        return None

    async def branch_sha(self, full_name: str, installation_id: int, branch: str) -> str | None:
        token = await self.installation_token(installation_id, full_name)
        response = await self.client.get(
            f"{self.settings.github_api_url}/repos/{full_name}/git/ref/heads/{quote(branch, safe='')}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-pipeline",
            },
        )
        if response.status_code == 404:
            return None
        data = self._json(response, "GitHub branch lookup")
        target = data.get("object")
        if not isinstance(target, dict) or not _text(target.get("sha")):
            raise RuntimeError("GitHub branch lookup returned no commit SHA")
        return _text(target.get("sha"))

    async def find_pull_by_head(
        self, full_name: str, installation_id: int, branch: str
    ) -> tuple[str, str] | None:
        token = await self.installation_token(installation_id, full_name)
        owner = full_name.split("/", 1)[0]
        pulls = await self._get_list(
            f"/repos/{full_name}/pulls",
            token,
            {"state": "open", "head": f"{owner}:{branch}", "per_page": 100},
        )
        for pull in pulls:
            if not isinstance(pull, dict):
                continue
            head = pull.get("head")
            if isinstance(head, dict) and _text(head.get("ref")) == branch:
                url = _text(pull.get("html_url"))
                sha = _text(head.get("sha"))
                if url and sha:
                    return url, sha
        return None

    async def post_comment(self, full_name: str, installation_id: int, number: int, body: str) -> str:
        token = await self.installation_token(installation_id, full_name)
        data = await self._request(
            "POST",
            f"/repos/{full_name}/issues/{number}/comments",
            token=token,
            payload={"body": _truncate_utf8(body, 60_000)},
        )
        return _text(data.get("html_url"))

    async def create_draft_pull_request(
        self,
        full_name: str,
        installation_id: int,
        *,
        title: str,
        head: str,
        base: str,
        body: str,
    ) -> str:
        token = await self.installation_token(installation_id, full_name)
        data = await self._request(
            "POST",
            f"/repos/{full_name}/pulls",
            token=token,
            payload={
                "title": title[:256],
                "head": head,
                "base": base,
                "body": _truncate_utf8(body, 60_000),
                "draft": True,
            },
        )
        return _text(data.get("html_url"))

    async def _get_list(
        self, path: str, token: str, params: dict[str, Any]
    ) -> list[Any]:
        response = await self.client.get(
            f"{self.settings.github_api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-pipeline",
            },
            params=params,
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"GitHub GET {path} returned invalid JSON") from exc
        if response.is_error:
            message = _text(data.get("message")) if isinstance(data, dict) else "request failed"
            raise RuntimeError(f"GitHub GET {path} failed ({response.status_code}): {message}")
        if not isinstance(data, list):
            raise RuntimeError(f"GitHub GET {path} returned unexpected data")
        return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = await self.client.request(
            method,
            f"{self.settings.github_api_url}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "agent-pipeline",
            },
            json=payload,
        )
        return self._json(response, f"GitHub {method} {path}")

    @staticmethod
    def _json(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"{operation} returned invalid JSON ({response.status_code})") from exc
        if response.is_error:
            message = _text(data.get("message")) if isinstance(data, dict) else "request failed"
            raise RuntimeError(f"{operation} failed ({response.status_code}): {message}")
        if not isinstance(data, dict):
            raise RuntimeError(f"{operation} returned unexpected data")
        return data


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
