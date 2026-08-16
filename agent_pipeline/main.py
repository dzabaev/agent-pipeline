from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (  # pyright: ignore[reportMissingImports]
    FastAPI,
    HTTPException,
    Request,
    status,
)
from fastapi.responses import RedirectResponse  # pyright: ignore[reportMissingImports]
from fastapi.staticfiles import StaticFiles  # pyright: ignore[reportMissingImports]
from fastapi.templating import Jinja2Templates  # pyright: ignore[reportMissingImports]

from .contracts import AgentRunner, CodeHost, RunKind, WebhookError
from .db import Database
from .github import GitHubCodeHost  # pyright: ignore[reportMissingImports]
from .pi import PiAgentRunner  # pyright: ignore[reportMissingImports]
from .settings import Settings
from .worker import WorkerPool  # pyright: ignore[reportMissingImports]
from .workflow import WorkflowProcessor  # pyright: ignore[reportMissingImports]
from .worktrees import WorktreeManager  # pyright: ignore[reportMissingImports]


_PACKAGE_ROOT = Path(__file__).parent
_TEMPLATES = Jinja2Templates(directory=_PACKAGE_ROOT / "templates")


def _require_dashboard(
    request: Request,
    settings: Settings | None,
) -> Settings:
    if settings is None:
        raise HTTPException(status_code=503, detail="dashboard is not configured")
    authorization = request.headers.get("Authorization", "")
    scheme, separator, encoded = authorization.partition(" ")
    username = ""
    provided_value = ""
    if separator and scheme.casefold() == "basic":
        try:
            decoded = base64.b64decode(encoded, validate=True).decode()
            username, credential_separator, provided_value = decoded.partition(":")
            if not credential_separator:
                username = provided_value = ""
        except (binascii.Error, UnicodeDecodeError):
            username = provided_value = ""
    valid_username = secrets.compare_digest(username, settings.dashboard_user)
    valid_credential = secrets.compare_digest(
        provided_value, settings.dashboard_password
    )
    if not (valid_username and valid_credential):
        raise HTTPException(
            status_code=401,
            detail="authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    return settings


def _csrf_token(credential: str, run_id: str) -> str:
    return hmac.new(
        credential.encode(), run_id.encode(), hashlib.sha256
    ).hexdigest()


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.database.initialize()
    pool: WorkerPool | None = app.state.worker_pool
    host: CodeHost = app.state.code_host
    worktrees: WorktreeManager | None = app.state.worktrees
    if worktrees is not None:
        await worktrees.cleanup_all()
    if pool is not None:
        await pool.start()
    try:
        yield
    finally:
        if pool is not None:
            await pool.stop()
        await host.close()


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    code_host: CodeHost | None = None,
    agent_runner: AgentRunner | None = None,
    worktrees: WorktreeManager | None = None,
    start_workers: bool = True,
) -> FastAPI:
    configured = settings
    if (database is None or code_host is None or start_workers) and configured is None:
        configured = Settings.from_env()

    active_database = database
    if active_database is None:
        if configured is None:
            raise RuntimeError("settings are required to create database")
        active_database = Database(configured.database_path)

    active_code_host = code_host
    if active_code_host is None:
        if configured is None:
            raise RuntimeError("settings are required to create code host")
        active_code_host = GitHubCodeHost(
            repository=configured.github_repository,
            token=configured.github_token,
            webhook_secret=configured.github_webhook_secret,
            bot_login=configured.github_bot_login,
            api_url=configured.github_api_url,
        )

    pool: WorkerPool | None = None
    active_worktrees: WorktreeManager | None = None
    if start_workers:
        if configured is None:
            raise RuntimeError("settings are required to start workers")
        active_runner = agent_runner or PiAgentRunner(
            configured.pi_executable,
            configured.pi_runner_user,
            model=configured.model,
            reasoning_level=configured.reasoning_level,
        )
        active_worktrees = worktrees or WorktreeManager(
            repository_path=configured.repository_path,
            worktree_root=configured.worktree_root,
            remote_url=active_code_host.remote_url,
            git_environment=active_code_host.git_environment,
            test_runner_user=configured.test_runner_user,
        )
        processor = WorkflowProcessor(
            database=active_database,
            code_host=active_code_host,
            agent_runner=active_runner,
            worktrees=active_worktrees,
            agent_timeout_seconds=configured.agent_timeout_seconds,
            test_command=configured.test_command,
            model_name=configured.model,
        )
        pool = WorkerPool(
            active_database,
            processor,
            concurrency=configured.max_concurrent_agents,
        )

    app = FastAPI(title="Agent Pipeline", lifespan=_lifespan)
    app.state.settings = configured
    app.state.database = active_database
    app.state.code_host = active_code_host
    app.state.worker_pool = pool
    app.state.worktrees = active_worktrees
    app.mount(
        "/static",
        StaticFiles(directory=_PACKAGE_ROOT / "static"),
        name="static",
    )

    @app.get("/")
    async def dashboard(request: Request):
        dashboard_settings = _require_dashboard(request, configured)
        runs = active_database.list_runs()
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="dashboard.html",
            context={
                "runs": runs,
                "deliveries": active_database.list_deliveries(),
                "csrf": lambda run_id: _csrf_token(
                    dashboard_settings.dashboard_password, run_id
                ),
            },
        )

    @app.get("/runs/{run_id}")
    async def run_detail(request: Request, run_id: str):
        dashboard_settings = _require_dashboard(request, configured)
        try:
            run = active_database.get_run(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="run not found") from error
        return _TEMPLATES.TemplateResponse(
            request=request,
            name="run.html",
            context={
                "run": run,
                "csrf": _csrf_token(
                    dashboard_settings.dashboard_password, run_id
                ),
            },
        )

    @app.post("/runs/{run_id}/retry")
    async def retry_run(request: Request, run_id: str) -> RedirectResponse:
        dashboard_settings = _require_dashboard(request, configured)
        form = await request.form()
        supplied = form.get("csrf")
        expected = _csrf_token(dashboard_settings.dashboard_password, run_id)
        if not isinstance(supplied, str) or not secrets.compare_digest(
            supplied, expected
        ):
            raise HTTPException(status_code=403, detail="invalid CSRF token")
        if not active_database.retry_run(run_id):
            raise HTTPException(status_code=409, detail="run cannot be retried")
        return RedirectResponse(
            url=str(request.url_for("run_detail", run_id=run_id)),
            status_code=303,
        )

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> dict[str, str]:
        body = await request.body()
        try:
            event = active_code_host.parse_webhook(request.headers, body)
        except WebhookError as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid GitHub webhook",
            ) from error

        if event is None:
            return {"status": "ignored"}

        reply_number = event.pull_request_number or event.issue_number
        issue_number = event.issue_number
        if event.pull_request_number is not None:
            mapped_issue = active_database.issue_for_pull_request(
                event.pull_request_number
            )
            issue_number = mapped_issue or issue_number
        run_id = active_database.ingest_run(
            delivery_id=event.delivery_id,
            event=event.event_name,
            action=event.action,
            payload_json=body.decode("utf-8"),
            issue_number=issue_number,
            reply_number=reply_number,
            kind=RunKind.DECISION,
            actor=event.actor,
            prompt_context=event.body,
        )
        if run_id is None:
            return {"status": "duplicate"}
        return {"status": "queued", "run_id": run_id}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        active_database.healthcheck()
        return {"status": "ok"}

    return app
