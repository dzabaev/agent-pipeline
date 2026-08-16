from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx  # pyright: ignore[reportMissingImports]

from .contracts import (
    CodeHostEvent,
    ConversationContext,
    EventKind,
    PullRequest,
    WebhookError,
)
from .process import terminate_process_group


PUBLICATION_MARKER = "<!-- agent-pipeline:"


WebhookRejected = WebhookError


class GitHubError(RuntimeError):
    """Raised when GitHub API or git publication fails."""


class GitHubCodeHost:
    def __init__(
        self,
        *,
        repository: str,
        token: str,
        webhook_secret: str,
        bot_login: str,
        api_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.repository = repository
        self.token = token
        self.webhook_secret = webhook_secret.encode()
        self.bot_login = bot_login.casefold()
        self.api_url = api_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    @property
    def remote_url(self) -> str:
        host = urlsplit(self.api_url).hostname or "github.com"
        if host == "api.github.com":
            host = "github.com"
        return f"https://{host}/{self.repository}.git"

    @property
    def git_environment(self) -> Mapping[str, str]:
        return self._git_environment()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def fetch_context(self, event: CodeHostEvent) -> ConversationContext:
        issue = await self._get_mapping(f"issues/{event.issue_number}")
        repository = await self._get_mapping("")
        default_branch = str(repository.get("default_branch", "main"))
        commit = await self._get_mapping(f"commits/{default_branch}")
        comments = await self._get_list(f"issues/{event.issue_number}/comments")
        rendered_comments = tuple(
            f"{_login(comment)}: {_text(comment.get('body'))}"
            for comment in comments
        )
        return ConversationContext(
            issue_number=event.issue_number,
            title=_text(issue.get("title")),
            body=_text(issue.get("body")),
            source_url=_text(issue.get("html_url")),
            comments=rendered_comments,
            default_branch=default_branch,
            base_sha=_text(commit.get("sha")),
        )

    async def has_write_permission(self, actor: str) -> bool:
        response = await self._client.get(
            self._url(f"collaborators/{actor}/permission")
        )
        if response.status_code == 404:
            return False
        self._raise_for_status(response)
        permission = _response_mapping(response).get("permission")
        return permission in {"write", "maintain", "admin"}

    async def post_comment(self, issue_number: int, body: str) -> str:
        marker = _publication_marker(body)
        if marker:
            comments = await self._get_list(f"issues/{issue_number}/comments")
            for comment in comments:
                if marker in _text(comment.get("body")):
                    return _text(comment.get("html_url"))
        response = await self._request(
            "POST", f"issues/{issue_number}/comments", json={"body": body}
        )
        return _text(_response_mapping(response).get("html_url"))

    async def pull_request(self, number: int) -> PullRequest:
        return _pull_request(await self._get_mapping(f"pulls/{number}"))

    async def pull_request_files(self, number: int) -> Mapping[str, str]:
        files = await self._get_list(f"pulls/{number}/files")
        return {
            _text(item.get("filename")): _text(item.get("status"))
            for item in files
        }

    async def file_content(self, path: str, ref: str) -> str:
        response = await self._request(
            "GET", f"contents/{path}", params={"ref": ref}
        )
        payload = _response_mapping(response)
        content = payload.get("content")
        if not isinstance(content, str):
            raise GitHubError(f"GitHub returned no content for {path}")
        try:
            return base64.b64decode(content).decode()
        except (ValueError, UnicodeDecodeError) as error:
            raise GitHubError(f"GitHub returned invalid content for {path}") from error

    async def push_branch(self, repository: Path, branch: str) -> None:
        remote_ref = f"refs/heads/{branch}"
        remote = await self._run_git(
            repository,
            "ls-remote",
            "--refs",
            "origin",
            remote_ref,
        )
        expected_sha = remote.decode().partition("\t")[0].strip()
        await self._run_git(
            repository,
            "push",
            "--no-verify",
            f"--force-with-lease={remote_ref}:{expected_sha}",
            "origin",
            f"HEAD:{remote_ref}",
        )

    async def _run_git(self, repository: Path, *arguments: str) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(repository),
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "remote.origin.mirror=false",
            *arguments,
            env=self._git_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        if process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise GitHubError(f"git {arguments[0]} failed: {detail}")
        return stdout

    async def open_pull_request(
        self,
        *,
        issue_number: int,
        branch: str,
        title: str,
        body: str,
        draft: bool,
    ) -> PullRequest:
        owner = self.repository.split("/", 1)[0]
        response = await self._request(
            "GET",
            "pulls",
            params={"state": "all", "head": f"{owner}:{branch}", "per_page": 100},
        )
        existing = response.json()
        if not isinstance(existing, list):
            raise GitHubError("GitHub returned an invalid pull request list")
        if existing:
            return _pull_request(_mapping(existing[0]))

        response = await self._request(
            "POST",
            "pulls",
            json={
                "title": title,
                "head": branch,
                "base": await self.default_branch(),
                "body": f"{body}\n\n<!-- agent-pipeline-issue:{issue_number} -->",
                "draft": draft,
            },
        )
        return _pull_request(_response_mapping(response))

    async def default_branch(self) -> str:
        repository = await self._get_mapping("")
        return _text(repository.get("default_branch")) or "main"

    async def _get_mapping(self, path: str) -> Mapping[str, Any]:
        return _response_mapping(await self._request("GET", path))

    async def _get_list(self, path: str) -> list[Mapping[str, Any]]:
        items: list[Mapping[str, Any]] = []
        page = 1
        while True:
            response = await self._request(
                "GET", path, params={"per_page": 100, "page": page}
            )
            payload = response.json()
            if not isinstance(payload, list):
                raise GitHubError("GitHub returned an invalid list response")
            page_items = [_mapping(item) for item in payload]
            items.extend(page_items)
            if len(page_items) < 100:
                return items
            page += 1

    async def _request(
        self,
        method: str,
        path: str,
        **kwargs: Any,
    ) -> httpx.Response:
        response = await self._client.request(method, self._url(path), **kwargs)
        self._raise_for_status(response)
        return response

    def _url(self, path: str) -> str:
        base = f"{self.api_url}/repos/{self.repository}"
        return f"{base}/{path}" if path else base

    def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        detail = response.text[-2000:]
        raise GitHubError(f"GitHub API returned {response.status_code}: {detail}")

    def _git_environment(self) -> dict[str, str]:
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"GITHUB_TOKEN", "GITHUB_WEBHOOK_SECRET"}
        }
        credential = base64.b64encode(
            f"x-access-token:{self.token}".encode()
        ).decode()
        host = urlsplit(self.remote_url).hostname or "github.com"
        environment.update(
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": f"http.https://{host}/.extraheader",
                "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {credential}",
            }
        )
        return environment

    def parse_webhook(
        self,
        headers: Mapping[str, str],
        body: bytes,
    ) -> CodeHostEvent | None:
        delivery_id = _header(headers, "X-GitHub-Delivery")
        event_name = _header(headers, "X-GitHub-Event")
        signature = _header(headers, "X-Hub-Signature-256")
        if not delivery_id or not event_name or not signature:
            raise WebhookRejected("missing required GitHub webhook headers")

        expected = "sha256=" + hmac.new(
            self.webhook_secret, body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise WebhookRejected("invalid GitHub webhook signature")

        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WebhookRejected("invalid GitHub webhook JSON") from error
        if not isinstance(payload, dict):
            raise WebhookRejected("GitHub webhook payload must be an object")

        action = str(payload.get("action", ""))
        sender = _mapping(payload.get("sender"))
        actor = str(sender.get("login", ""))
        if not actor:
            raise WebhookRejected("GitHub webhook sender is missing")

        if event_name == "issues" and action == "opened":
            issue = _mapping(payload.get("issue"))
            return _event(
                delivery_id,
                EventKind.ISSUE_OPENED,
                event_name,
                action,
                issue,
                actor,
                payload,
            )

        if event_name == "issue_comment" and action == "created":
            issue = _mapping(payload.get("issue"))
            comment = _mapping(payload.get("comment"))
            body_text = str(comment.get("body", ""))
            if self._ignore_comment(sender, actor, body_text):
                return None
            return _event(
                delivery_id,
                EventKind.COMMENT,
                event_name,
                action,
                issue,
                actor,
                payload,
                body=body_text,
                pull_request_number=(
                    _number(issue) if "pull_request" in issue else None
                ),
            )

        if event_name == "pull_request_review_comment" and action == "created":
            pull_request = _mapping(payload.get("pull_request"))
            comment = _mapping(payload.get("comment"))
            body_text = str(comment.get("body", ""))
            if self._ignore_comment(sender, actor, body_text):
                return None
            number = _number(pull_request)
            return CodeHostEvent(
                delivery_id=delivery_id,
                kind=EventKind.COMMENT,
                event_name=event_name,
                action=action,
                issue_number=number,
                actor=actor,
                body=body_text,
                source_url=str(comment.get("html_url", "")),
                pull_request_number=number,
                raw_payload=payload,
            )

        if event_name == "pull_request_review" and action == "submitted":
            pull_request = _mapping(payload.get("pull_request"))
            review = _mapping(payload.get("review"))
            body_text = str(review.get("body", "")).strip()
            if not body_text or self._ignore_comment(sender, actor, body_text):
                return None
            number = _number(pull_request)
            return CodeHostEvent(
                delivery_id=delivery_id,
                kind=EventKind.COMMENT,
                event_name=event_name,
                action=action,
                issue_number=number,
                actor=actor,
                body=body_text,
                source_url=str(review.get("html_url", "")),
                pull_request_number=number,
                raw_payload=payload,
            )

        if event_name == "pull_request" and action == "closed":
            pull_request = _mapping(payload.get("pull_request"))
            head = _mapping(pull_request.get("head"))
            branch = str(head.get("ref", ""))
            if bool(pull_request.get("merged")) and branch.startswith("agent/plan-"):
                try:
                    issue_number = int(branch.removeprefix("agent/plan-"))
                except ValueError:
                    return None
                return CodeHostEvent(
                    delivery_id=delivery_id,
                    kind=EventKind.PLAN_MERGED,
                    event_name=event_name,
                    action=action,
                    issue_number=issue_number,
                    actor=actor,
                    source_url=str(pull_request.get("html_url", "")),
                    pull_request_number=_number(pull_request),
                    raw_payload=payload,
                )

        return None

    def _ignore_comment(
        self,
        sender: Mapping[str, Any],
        actor: str,
        body: str,
    ) -> bool:
        return (
            actor.casefold() == self.bot_login
            or str(sender.get("type", "")).casefold() == "bot"
            or PUBLICATION_MARKER in body
        )


def _event(
    delivery_id: str,
    kind: EventKind,
    event_name: str,
    action: str,
    item: Mapping[str, Any],
    actor: str,
    payload: Mapping[str, Any],
    *,
    body: str | None = None,
    pull_request_number: int | None = None,
) -> CodeHostEvent:
    return CodeHostEvent(
        delivery_id=delivery_id,
        kind=kind,
        event_name=event_name,
        action=action,
        issue_number=_number(item),
        actor=actor,
        body=str(item.get("body", "")) if body is None else body,
        source_url=str(item.get("html_url", "")),
        pull_request_number=pull_request_number,
        raw_payload=payload,
    )


def _pull_request(payload: Mapping[str, Any]) -> PullRequest:
    head = _mapping(payload.get("head"))
    return PullRequest(
        number=_number(payload),
        url=_text(payload.get("html_url")),
        branch=_text(head.get("ref")),
        head_sha=_text(head.get("sha")),
        merged=bool(payload.get("merged")),
    )


def _response_mapping(response: httpx.Response) -> Mapping[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError as error:
        raise GitHubError("GitHub returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise GitHubError("GitHub returned an invalid object response")
    return payload


def _login(payload: Mapping[str, Any]) -> str:
    user = payload.get("user")
    return _text(user.get("login")) if isinstance(user, dict) else "unknown"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise WebhookRejected("GitHub webhook payload has an invalid object")
    return value


def _number(value: Mapping[str, Any]) -> int:
    number = value.get("number")
    if not isinstance(number, int) or number < 1:
        raise WebhookRejected("GitHub webhook item number is invalid")
    return number


def _publication_marker(body: str) -> str:
    return next(
        (
            line.strip()
            for line in body.splitlines()
            if line.strip().startswith(PUBLICATION_MARKER)
        ),
        "",
    )


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if key.casefold() == wanted),
        "",
    )
