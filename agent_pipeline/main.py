from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import (  # pyright: ignore[reportMissingImports]
    FastAPI,
    HTTPException,
    Request,
    status,
)

from .contracts import AgentRunner, CodeHost, EventKind, RunKind
from .db import Database
from .github import (  # pyright: ignore[reportMissingImports]
    GitHubCodeHost,
    WebhookRejected,
)
from .pi import PiAgentRunner  # pyright: ignore[reportMissingImports]
from .settings import Settings
from .worker import WorkerPool  # pyright: ignore[reportMissingImports]
from .workflow import (  # pyright: ignore[reportMissingImports]
    WorkflowProcessor,
    is_implementation_command,
)
from .worktrees import WorktreeManager  # pyright: ignore[reportMissingImports]


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.database.initialize()
    pool: WorkerPool | None = app.state.worker_pool
    host: CodeHost = app.state.code_host
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
    if start_workers:
        if configured is None:
            raise RuntimeError("settings are required to start workers")
        active_runner = agent_runner or PiAgentRunner(
            configured.pi_executable,
            configured.pi_runner_user,
        )
        active_worktrees = worktrees or WorktreeManager(
            repository_path=configured.repository_path,
            worktree_root=configured.worktree_root,
            remote_url=active_code_host.remote_url,
            git_environment=active_code_host.git_environment,
        )
        processor = WorkflowProcessor(
            database=active_database,
            code_host=active_code_host,
            agent_runner=active_runner,
            worktrees=active_worktrees,
            agent_timeout_seconds=configured.agent_timeout_seconds,
            test_command=configured.test_command,
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

    @app.post("/webhooks/github", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> dict[str, str]:
        body = await request.body()
        try:
            event = active_code_host.parse_webhook(request.headers, body)
        except WebhookRejected as error:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="invalid GitHub webhook",
            ) from error

        if event is None:
            return {"status": "ignored"}

        run_kind = {
            EventKind.ISSUE_OPENED: RunKind.PLAN,
            EventKind.COMMENT: (
                RunKind.IMPLEMENTATION
                if is_implementation_command(event.body)
                else RunKind.REVIEW
            ),
            EventKind.PLAN_MERGED: RunKind.IMPLEMENTATION,
        }[event.kind]
        run_id = active_database.ingest_run(
            delivery_id=event.delivery_id,
            event=event.event_name,
            action=event.action,
            payload_json=body.decode("utf-8"),
            issue_number=event.issue_number,
            kind=run_kind,
            actor=event.actor,
            prompt_context=event.body,
        )
        if run_id is None:
            return {"status": "duplicate"}
        return {"status": "queued", "run_id": run_id}

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
