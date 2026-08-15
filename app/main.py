# pyright: reportMissingImports=false
from __future__ import annotations

import fcntl
import hmac
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .config import Settings
from .db import Database
from .github import (
    GitHubForge,
    normalize_event,
    pkce_challenge,
    rule_matches,
    rule_needs_changed_paths,
    verify_signature,
)
from .jobs import TERMINAL_STATUSES, WorkerPool, WorkspaceManager
from .pi import PiRunner


TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")
EVENTS = ("issues", "issue_comment", "pull_request", "pull_request_review")


def _integer(value: str | None, field: str, *, minimum: int = 1) -> int:
    try:
        parsed = int(value or "")
    except ValueError as exc:
        raise HTTPException(400, f"{field} must be an integer") from exc
    if parsed < minimum:
        raise HTTPException(400, f"{field} must be at least {minimum}")
    return parsed


async def _form(request: Request) -> dict[str, str]:
    body = await request.body()
    if len(body) > 256_000:
        raise HTTPException(413, "Form too large")
    try:
        parsed = parse_qs(body.decode(), keep_blank_values=True, strict_parsing=False)
    except UnicodeDecodeError as exc:
        raise HTTPException(400, "Invalid form encoding") from exc
    return {key: values[-1] for key, values in parsed.items() if values}


def _csv(value: str | None) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _require_user(request: Request) -> dict[str, Any]:
    user = request.session.get("user")
    if not isinstance(user, dict):
        raise HTTPException(401, "Login required")
    return user


def _csrf(request: Request, form: dict[str, str]) -> None:
    expected = request.session.get("csrf")
    supplied = form.get("csrf")
    if not isinstance(expected, str) or not supplied or not hmac.compare_digest(expected, supplied):
        raise HTTPException(403, "Invalid CSRF token")


def _dashboard_context(request: Request) -> dict[str, Any]:
    database: Database = request.app.state.database
    return {
        "request": request,
        "user": _require_user(request),
        "csrf": request.session["csrf"],
        "repositories": database.list_repositories(),
        "rules": database.list_rules(),
        "runs": database.list_runs(),
        "events": EVENTS,
        "worker_concurrency": database.get_worker_concurrency(1),
    }


@asynccontextmanager
async def _lifespan(app: FastAPI):
    configured: Settings = app.state.settings
    configured.prepare()
    try:
        lock_file = (configured.data_dir / "agent-pipeline.lock").open("a+")
    except OSError as exc:
        raise RuntimeError("Cannot open Agent Pipeline server lock") from exc
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        lock_file.close()
        raise RuntimeError("Another Agent Pipeline server is using this data directory") from exc
    try:
        database = Database(configured.data_dir / "agent-pipeline.sqlite3")
        database.initialize(configured.worker_concurrency)
        database.recover_interrupted()
        forge = GitHubForge(configured)
        runner = PiRunner(configured)
        workspace = WorkspaceManager(configured, forge)
        workers = WorkerPool(configured, database, forge, runner, workspace)
        app.state.database = database
        app.state.forge = forge
        app.state.runner = runner
        app.state.workers = workers
        workers.start()
        try:
            yield
        finally:
            await workers.stop()
            await forge.close()
    finally:
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        lock_file.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    configured = settings or Settings.from_env()
    app = FastAPI(title="Agent Pipeline", lifespan=_lifespan)
    app.state.settings = configured
    app.add_middleware(
        SessionMiddleware,
        secret_key=configured.secret_key,
        session_cookie="agent_pipeline",
        same_site="lax",
        https_only=configured.base_url.startswith("https://"),
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request):
        if isinstance(request.session.get("user"), dict):
            return RedirectResponse("/", 303)
        return TEMPLATES.TemplateResponse(request, "login.html", {})

    @app.get("/auth/github")
    async def auth_github(request: Request):
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        request.session["oauth_state"] = state
        request.session["oauth_verifier"] = verifier
        return RedirectResponse(request.app.state.forge.oauth_url(state, pkce_challenge(verifier)))

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = ""):
        expected = request.session.pop("oauth_state", None)
        verifier = request.session.pop("oauth_verifier", None)
        if not code or not isinstance(expected, str) or not hmac.compare_digest(expected, state):
            raise HTTPException(400, "Invalid OAuth state")
        if not isinstance(verifier, str):
            raise HTTPException(400, "OAuth session expired")
        try:
            user = await request.app.state.forge.authenticate_user(code, verifier)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        request.session.clear()
        request.session["user"] = user
        request.session["csrf"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", 303)

    @app.post("/logout")
    async def logout(request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not isinstance(request.session.get("user"), dict):
            return RedirectResponse("/login", 303)
        return TEMPLATES.TemplateResponse(request, "dashboard.html", _dashboard_context(request))

    @app.post("/repositories")
    async def add_repository(request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        full_name = form.get("full_name", "").strip()
        if full_name.count("/") != 1:
            raise HTTPException(400, "Repository must be owner/name")
        installation_id = _integer(form.get("installation_id"), "Installation ID")
        verification_command = form.get("verification_command", "").strip()
        if not verification_command:
            raise HTTPException(400, "Verification command is required")
        repository = await request.app.state.forge.get_repository(full_name, installation_id)
        repository_id = repository.get("id")
        default_branch = repository.get("default_branch")
        canonical_name = repository.get("full_name")
        if not isinstance(repository_id, int) or not isinstance(default_branch, str) or not isinstance(canonical_name, str):
            raise HTTPException(502, "GitHub returned incomplete repository data")
        request.app.state.database.add_repository(
            github_repo_id=repository_id,
            installation_id=installation_id,
            full_name=canonical_name,
            default_branch=default_branch,
            verification_command=verification_command,
        )
        return RedirectResponse("/", 303)

    @app.post("/repositories/{repository_id}/toggle")
    async def toggle_repository(repository_id: int, request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        request.app.state.database.toggle_repository(repository_id)
        return RedirectResponse("/", 303)

    @app.post("/rules")
    async def add_rule(request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        repository_id = _integer(form.get("repository_id"), "Repository")
        event = form.get("event", "")
        kind = form.get("kind", "")
        instruction = form.get("instruction", "").strip()
        if event not in EVENTS or kind not in {"advisory", "change"} or not instruction:
            raise HTTPException(400, "Invalid rule")
        draft_value = form.get("draft", "")
        filters: dict[str, Any] = {
            "subject_kind": form.get("subject_kind", "any"),
            "labels_all": _csv(form.get("labels_all")),
            "labels_any": _csv(form.get("labels_any")),
            "sender_allow": _csv(form.get("sender_allow")),
            "author_associations": _csv(form.get("author_associations")),
            "body_contains_any": _csv(form.get("body_contains_any")),
            "base_branches": _csv(form.get("base_branches")),
            "changed_paths_any": _csv(form.get("changed_paths_any")),
            "review_states": _csv(form.get("review_states")),
        }
        if draft_value in {"true", "false"}:
            filters["draft"] = draft_value == "true"
        request.app.state.database.add_rule(
            repository_id=repository_id,
            name=form.get("name", "").strip() or f"{event} {kind}",
            event=event,
            actions=_csv(form.get("actions")),
            kind=kind,
            instruction=instruction,
            filters=filters,
            model=form.get("model", "").strip() or None,
            thinking_level=form.get("thinking_level", "").strip() or None,
        )
        return RedirectResponse("/", 303)

    @app.post("/rules/{rule_id}/toggle")
    async def toggle_rule(rule_id: int, request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        request.app.state.database.toggle_rule(rule_id)
        return RedirectResponse("/", 303)

    @app.post("/settings/concurrency")
    async def set_concurrency(request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        value = _integer(form.get("worker_concurrency"), "Worker concurrency")
        try:
            request.app.state.database.set_worker_concurrency(value)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return RedirectResponse("/", 303)

    @app.post("/runs")
    async def create_run(request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        repository_id = _integer(form.get("repository_id"), "Repository")
        repository = request.app.state.database.get_repository(repository_id)
        if not repository or not repository["enabled"]:
            raise HTTPException(404, "Repository is unavailable")
        kind = form.get("kind", "")
        instruction = form.get("instruction", "").strip()
        if kind not in {"advisory", "change"} or not instruction:
            raise HTTPException(400, "Kind and instruction are required")
        target_kind = form.get("target_kind", "").strip() or None
        if target_kind not in {None, "issue", "pull_request"}:
            raise HTTPException(400, "Invalid target kind")
        target_number = None
        if form.get("target_number", "").strip():
            target_number = _integer(form.get("target_number"), "Target number")
        if (target_kind is None) != (target_number is None):
            raise HTTPException(400, "Target kind and number must be provided together")
        continuation_run_id = form.get("continuation_run_id", "").strip() or None
        if continuation_run_id:
            previous = request.app.state.database.get_run(continuation_run_id)
            if (
                not previous
                or previous["repository_id"] != repository_id
                or previous["status"] not in TERMINAL_STATUSES
                or not previous.get("session_file")
                or not Path(str(previous["session_file"])).is_file()
            ):
                raise HTTPException(400, "Continuation session is unavailable or belongs to another repository")
        run_id = request.app.state.database.create_run(
            repository_id=repository_id,
            source="manual",
            kind=kind,
            instruction=instruction,
            context={"source": "manual", "target_kind": target_kind, "target_number": target_number},
            target_kind=target_kind,
            target_number=target_number,
            continuation_run_id=continuation_run_id,
            model=form.get("model", "").strip() or None,
            thinking_level=form.get("thinking_level", "").strip() or None,
        )
        if run_id is None:
            raise HTTPException(409, "Could not create run")
        return RedirectResponse(f"/runs/{run_id}", 303)

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_page(run_id: str, request: Request):
        _require_user(request)
        run = request.app.state.database.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        events = _decode_events(request.app.state.database.get_events(run_id))
        return TEMPLATES.TemplateResponse(
            request,
            "run.html",
            {
                "run": run,
                "events": events,
                "live_text": _live_text(events),
                "csrf": request.session["csrf"],
            },
        )

    @app.get("/runs/{run_id}/fragment", response_class=HTMLResponse)
    async def run_fragment(run_id: str, request: Request):
        _require_user(request)
        run = request.app.state.database.get_run(run_id)
        if run is None:
            raise HTTPException(404, "Run not found")
        events = _decode_events(request.app.state.database.get_events(run_id))
        return TEMPLATES.TemplateResponse(
            request,
            "_run.html",
            {"run": run, "events": events, "live_text": _live_text(events)},
        )

    @app.post("/runs/{run_id}/retry")
    async def retry_run(run_id: str, request: Request):
        _require_user(request)
        form = await _form(request)
        _csrf(request, form)
        previous = request.app.state.database.get_run(run_id)
        if previous is None:
            raise HTTPException(404, "Run not found")
        if previous["status"] not in {"failed", "cancelled", "interrupted"}:
            raise HTTPException(409, "Only failed, cancelled, or interrupted runs can be retried")
        if previous.get("github_comment_url") or previous.get("pull_request_url"):
            raise HTTPException(409, "Published runs cannot be retried")
        repository_id = previous["repository_id"]
        if not isinstance(repository_id, int):
            raise HTTPException(500, "Invalid run record")
        repository = request.app.state.database.get_repository(repository_id)
        if not repository or not repository["enabled"]:
            raise HTTPException(409, "Repository is disabled")
        try:
            context = json.loads(str(previous["context_json"]))
        except json.JSONDecodeError:
            context = {}
        if not isinstance(context, dict):
            context = {}
        retried_id = request.app.state.database.create_run(
            repository_id=repository_id,
            source="manual",
            kind=str(previous["kind"]),
            instruction=str(previous["instruction"]),
            context=context,
            target_kind=str(previous["target_kind"]) if previous.get("target_kind") else None,
            target_number=previous["target_number"] if isinstance(previous.get("target_number"), int) else None,
            model=str(previous["model"]) if previous.get("model") else None,
            thinking_level=str(previous["thinking_level"]) if previous.get("thinking_level") else None,
            publication_key=str(previous["publication_key"]),
        )
        if retried_id is None:
            raise HTTPException(409, "Could not retry run")
        return RedirectResponse(f"/runs/{retried_id}", 303)

    @app.post("/runs/{run_id}/{control}")
    async def control_run(run_id: str, control: str, request: Request):
        _require_user(request)
        if control not in {"steer", "follow-up", "cancel"}:
            raise HTTPException(404, "Unknown control")
        form = await _form(request)
        _csrf(request, form)
        kind = control.replace("-", "_")
        body = form.get("message", "").strip() or None
        if kind != "cancel" and not body:
            raise HTTPException(400, "Message is required")
        if not await request.app.state.workers.control(run_id, kind, body):
            raise HTTPException(409, "Run cannot accept this control")
        return RedirectResponse(f"/runs/{run_id}", 303)

    @app.post("/webhooks/github")
    async def github_webhook(request: Request):
        content_length = request.headers.get("content-length")
        if content_length and _integer(content_length, "Content-Length", minimum=0) > 1_048_576:
            raise HTTPException(413, "Webhook too large")
        body = await request.body()
        if len(body) > 1_048_576:
            raise HTTPException(413, "Webhook too large")
        if not verify_signature(
            configured.github_webhook_secret,
            body,
            request.headers.get("x-hub-signature-256"),
        ):
            raise HTTPException(401, "Invalid webhook signature")
        delivery_id = request.headers.get("x-github-delivery", "")
        event = request.headers.get("x-github-event", "")
        if not delivery_id or not event:
            raise HTTPException(400, "Missing webhook headers")
        try:
            payload = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(400, "Invalid webhook JSON") from exc
        if not isinstance(payload, dict):
            raise HTTPException(400, "Invalid webhook payload")
        database: Database = request.app.state.database
        if database.has_delivery(delivery_id):
            return JSONResponse({"duplicate": True}, 202)
        if event == "ping":
            inserted, _ = database.enqueue_delivery(delivery_id, event, payload, "ping", [])
            return JSONResponse({"ok": inserted, "duplicate": not inserted}, 202)
        try:
            facts = normalize_event(event, payload)
        except ValueError:
            inserted, _ = database.enqueue_delivery(delivery_id, event, payload, "invalid", [])
            return JSONResponse({"ignored": "invalid payload", "duplicate": not inserted}, 202)
        if facts is None:
            inserted, _ = database.enqueue_delivery(delivery_id, event, payload, "unsupported", [])
            return JSONResponse({"ignored": "unsupported event", "duplicate": not inserted}, 202)
        repository = database.get_repository_by_github_id(facts.repository_id)
        if (
            repository is None
            or not repository["enabled"]
            or repository["installation_id"] != facts.installation_id
            or facts.sender_type.lower() == "bot"
            or (configured.github_bot_login and facts.sender == configured.github_bot_login)
        ):
            inserted, _ = database.enqueue_delivery(delivery_id, event, payload, "ignored", [])
            return JSONResponse({"matched": 0, "duplicate": not inserted}, 202)
        repository_db_id = repository["id"]
        if not isinstance(repository_db_id, int):
            raise HTTPException(500, "Invalid repository record")
        rules = database.list_rules(repository_db_id)
        if facts.subject_kind == "pull_request" and any(rule_needs_changed_paths(rule) for rule in rules):
            try:
                facts.changed_paths = await request.app.state.forge.get_pull_files(
                    facts.repository, facts.installation_id, facts.subject_number
                )
            except RuntimeError as exc:
                raise HTTPException(503, "Could not load pull request files") from exc
        run_specs: list[dict[str, Any]] = []
        for rule in rules:
            if not rule_matches(rule, facts):
                continue
            rule_id = rule["id"]
            if not isinstance(rule_id, int):
                continue
            run_specs.append(
                {
                    "repository_id": repository_db_id,
                    "rule_id": rule_id,
                    "source": "automatic",
                    "kind": str(rule["kind"]),
                    "instruction": str(rule["instruction"]),
                    "context": facts.context(),
                    "target_kind": facts.subject_kind,
                    "target_number": facts.subject_number,
                    "model": str(rule["model"]) if rule.get("model") else None,
                    "thinking_level": str(rule["thinking_level"]) if rule.get("thinking_level") else None,
                }
            )
        disposition = "matched" if run_specs else "unmatched"
        inserted, run_ids = database.enqueue_delivery(
            delivery_id, event, payload, disposition, run_specs
        )
        if not inserted:
            return JSONResponse({"duplicate": True}, 202)
        return JSONResponse({"matched": len(run_ids)}, 202)

    return app


def _live_text(events: list[dict[str, Any]]) -> str:
    return "".join(
        str(event["payload"].get("delta", ""))
        for event in events
        if event["kind"] == "text" and isinstance(event.get("payload"), dict)
    )


def _decode_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for event in events:
        try:
            payload = json.loads(str(event["payload_json"]))
        except json.JSONDecodeError:
            payload = {"text": str(event["payload_json"])}
        event["payload"] = payload
    return events
