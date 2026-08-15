from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from agent_pipeline.contracts import RunKind, RunStatus


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    delivery_id: str
    issue_number: int
    kind: RunKind
    status: RunStatus
    attempt: int
    actor: str
    actor_permission: str | None
    prompt_context: str
    output: str | None
    error: str | None
    branch: str | None
    worktree_path: str | None
    github_url: str | None
    created_at: str
    updated_at: str


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    id TEXT PRIMARY KEY,
                    event TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    disposition TEXT NOT NULL DEFAULT 'received',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS issues (
                    number INTEGER PRIMARY KEY,
                    plan_run_id TEXT,
                    plan_pr_number INTEGER,
                    plan_head_sha TEXT,
                    plan_text TEXT,
                    implementation_run_id TEXT UNIQUE,
                    implementation_pr_number INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY,
                    delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                    issue_number INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('plan', 'review', 'implementation')),
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'publishing', 'succeeded', 'failed', 'interrupted')
                    ),
                    attempt INTEGER NOT NULL DEFAULT 0,
                    actor TEXT NOT NULL,
                    actor_permission TEXT,
                    prompt_context TEXT NOT NULL,
                    output TEXT,
                    error TEXT,
                    branch TEXT,
                    worktree_path TEXT,
                    github_url TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS runs_queue
                    ON runs(status, created_at);
                """
            )

    def record_delivery(
        self,
        delivery_id: str,
        event: str,
        action: str,
        payload_json: str,
    ) -> bool:
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO deliveries(id, event, action, payload_json, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (delivery_id, event, action, payload_json, _now()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def enqueue_run(
        self,
        *,
        delivery_id: str,
        issue_number: int,
        kind: RunKind,
        actor: str,
        prompt_context: str,
        actor_permission: str | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, delivery_id, issue_number, kind, status, actor,
                    actor_permission, prompt_context, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    delivery_id,
                    issue_number,
                    kind,
                    RunStatus.QUEUED,
                    actor,
                    actor_permission,
                    prompt_context,
                    now,
                    now,
                ),
            )
        return run_id

    def claim_next_run(self) -> RunRecord | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT * FROM runs
                WHERE status = ?
                ORDER BY created_at, id
                LIMIT 1
                """,
                (RunStatus.QUEUED,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE runs
                SET status = ?, attempt = attempt + 1, updated_at = ?
                WHERE id = ?
                """,
                (RunStatus.RUNNING, _now(), row["id"]),
            )
            connection.commit()
        return self.get_run(row["id"])

    def get_run(self, run_id: str) -> RunRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(run_id)
        return _run_from_row(row)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        id=row["id"],
        delivery_id=row["delivery_id"],
        issue_number=row["issue_number"],
        kind=RunKind(row["kind"]),
        status=RunStatus(row["status"]),
        attempt=row["attempt"],
        actor=row["actor"],
        actor_permission=row["actor_permission"],
        prompt_context=row["prompt_context"],
        output=row["output"],
        error=row["error"],
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        github_url=row["github_url"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
