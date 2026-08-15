from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any, Mapping

from .contracts import CodeHostEvent, EventKind


PUBLICATION_MARKER = "<!-- agent-pipeline:"


class WebhookRejected(ValueError):
    """Raised when a GitHub webhook cannot be trusted or decoded."""


class GitHubCodeHost:
    def __init__(
        self,
        *,
        repository: str,
        token: str,
        webhook_secret: str,
        bot_login: str,
        api_url: str,
    ) -> None:
        self.repository = repository
        self.token = token
        self.webhook_secret = webhook_secret.encode()
        self.bot_login = bot_login.casefold()
        self.api_url = api_url.rstrip("/")

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


def _mapping(value: object) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise WebhookRejected("GitHub webhook payload has an invalid object")
    return value


def _number(value: Mapping[str, Any]) -> int:
    number = value.get("number")
    if not isinstance(number, int) or number < 1:
        raise WebhookRejected("GitHub webhook item number is invalid")
    return number


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    return next(
        (str(value) for key, value in headers.items() if key.casefold() == wanted),
        "",
    )
