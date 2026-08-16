from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .contracts import RunKind, RunStatus


@dataclass(frozen=True, slots=True)
class RunRecord:
    id: str
    delivery_id: str
    issue_number: int
    reply_number: int
    kind: RunKind
    decision_action: str | None
    tokens_consumed: int
    agent_history_json: str
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


@dataclass(frozen=True, slots=True)
class DeliveryRecord:
    id: str
    event: str
    action: str
    disposition: str
    created_at: str


@dataclass(frozen=True, slots=True)
class IssueRecord:
    number: int
    plan_run_id: str | None
    plan_pr_number: int | None
    plan_head_sha: str | None
    plan_text: str | None
    implementation_run_id: str | None
    implementation_pr_number: int | None
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
                    reply_number INTEGER NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('plan', 'review', 'implementation', 'decision')),
                    decision_action TEXT,
                    tokens_consumed INTEGER NOT NULL DEFAULT 0,
                    agent_history_json TEXT NOT NULL DEFAULT '[]',
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
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "reply_number" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN reply_number INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "UPDATE runs SET reply_number = issue_number WHERE reply_number = 0"
                )
            run_schema = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'runs'"
            ).fetchone()["sql"]
            if "'decision'" not in run_schema:
                connection.executescript(
                    """
                    CREATE TABLE runs_v2 (
                        id TEXT PRIMARY KEY,
                        delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                        issue_number INTEGER NOT NULL,
                        reply_number INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('plan', 'review', 'implementation', 'decision')),
                        decision_action TEXT,
                        tokens_consumed INTEGER NOT NULL DEFAULT 0,
                        agent_history_json TEXT NOT NULL DEFAULT '[]',
                        status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'publishing', 'succeeded', 'failed', 'interrupted')),
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
                    INSERT INTO runs_v2(
                        id, delivery_id, issue_number, reply_number, kind,
                        decision_action, tokens_consumed, agent_history_json, status,
                        attempt, actor, actor_permission, prompt_context, output,
                        error, branch, worktree_path, github_url, created_at, updated_at
                    )
                    SELECT
                        id, delivery_id, issue_number, reply_number, kind, NULL,
                        0, '[]', status, attempt, actor, actor_permission,
                        prompt_context, output, error, branch, worktree_path,
                        github_url, created_at, updated_at
                    FROM runs;
                    DROP TABLE runs;
                    ALTER TABLE runs_v2 RENAME TO runs;
                    CREATE INDEX runs_queue ON runs(status, created_at);
                    """
                )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "decision_action" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN decision_action TEXT")
            if "tokens_consumed" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN tokens_consumed INTEGER NOT NULL DEFAULT 0"
                )
            if "agent_history_json" not in columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN agent_history_json TEXT NOT NULL DEFAULT '[]'"
                )

    def healthcheck(self) -> None:
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()

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

    def ingest_run(
        self,
        *,
        delivery_id: str,
        event: str,
        action: str,
        payload_json: str,
        issue_number: int,
        kind: RunKind,
        actor: str,
        prompt_context: str,
        reply_number: int | None = None,
        reserve_implementation: bool = False,
    ) -> str | None:
        run_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO deliveries(
                    id, event, action, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (delivery_id, event, action, payload_json, now),
            ).rowcount
            if not inserted:
                connection.commit()
                return None
            if reserve_implementation:
                reserved = connection.execute(
                    """
                    UPDATE issues SET implementation_run_id = ?, updated_at = ?
                    WHERE number = ?
                    AND plan_pr_number IS NOT NULL
                    AND implementation_run_id IS NULL
                    AND implementation_pr_number IS NULL
                    """,
                    (run_id, now, issue_number),
                ).rowcount
                if not reserved:
                    connection.commit()
                    return ""
            connection.execute(
                """
                INSERT INTO runs(
                    id, delivery_id, issue_number, reply_number, kind, status, actor,
                    prompt_context, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    delivery_id,
                    issue_number,
                    reply_number or issue_number,
                    kind,
                    RunStatus.QUEUED,
                    actor,
                    prompt_context,
                    now,
                    now,
                ),
            )
            connection.commit()
        return run_id

    def enqueue_run(
        self,
        *,
        delivery_id: str,
        issue_number: int,
        kind: RunKind,
        actor: str,
        prompt_context: str,
        actor_permission: str | None = None,
        reply_number: int | None = None,
    ) -> str:
        run_id = str(uuid.uuid4())
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(
                    id, delivery_id, issue_number, reply_number, kind, status, actor,
                    actor_permission, prompt_context, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    delivery_id,
                    issue_number,
                    reply_number or issue_number,
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

    def record_decision_action(self, run_id: str, action: str) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE runs SET decision_action = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    action,
                    _now(),
                    run_id,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).rowcount
        if not updated:
            raise KeyError(run_id)

    def append_agent_history(
        self,
        run_id: str,
        kind: RunKind,
        messages: tuple[dict[str, str], ...],
        tokens: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT agent_history_json FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(run_id)
            try:
                history = json.loads(row["agent_history_json"])
            except json.JSONDecodeError:
                history = []
            if not isinstance(history, list):
                history = []
            history.append({"kind": str(kind), "messages": messages})
            updated = connection.execute(
                """
                UPDATE runs
                SET agent_history_json = ?,
                    tokens_consumed = tokens_consumed + ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    json.dumps(history),
                    max(tokens, 0),
                    _now(),
                    run_id,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).rowcount
            connection.commit()
        if not updated:
            raise KeyError(run_id)

    def list_runs(self, limit: int = 100) -> tuple[RunRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(_run_from_row(row) for row in rows)

    def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM deliveries WHERE id = ?", (delivery_id,)
            ).fetchone()
        if row is None:
            raise KeyError(delivery_id)
        return _delivery_from_row(row)

    def list_deliveries(self, limit: int = 100) -> tuple[DeliveryRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, event, action, disposition, created_at
                FROM deliveries ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(_delivery_from_row(row) for row in rows)

    def retry_run(self, run_id: str) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE runs
                SET status = ?, error = NULL, output = NULL,
                    worktree_path = NULL, decision_action = NULL, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    RunStatus.QUEUED,
                    _now(),
                    run_id,
                    RunStatus.FAILED,
                    RunStatus.INTERRUPTED,
                ),
            ).rowcount
        return bool(updated)

    def reserve_plan(
        self,
        issue_number: int,
        run_id: str,
        *,
        previous_pull_request_number: int | None = None,
        previous_run_id: str | None = None,
    ) -> bool:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if previous_pull_request_number is None:
                inserted = connection.execute(
                    """
                    INSERT OR IGNORE INTO issues(
                        number, plan_run_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (issue_number, run_id, now, now),
                ).rowcount
                if inserted:
                    connection.commit()
                    return True
                updated = connection.execute(
                    """
                    UPDATE issues SET plan_run_id = ?, updated_at = ?
                    WHERE number = ? AND plan_pr_number IS NULL
                    AND plan_run_id IS NULL
                    """,
                    (run_id, now, issue_number),
                ).rowcount
            else:
                updated = connection.execute(
                    """
                    UPDATE issues SET plan_run_id = ?, updated_at = ?
                    WHERE number = ? AND plan_pr_number = ?
                    AND plan_run_id IS ?
                    """,
                    (
                        run_id,
                        now,
                        issue_number,
                        previous_pull_request_number,
                        previous_run_id,
                    ),
                ).rowcount
            connection.commit()
        return bool(updated)

    def record_plan(
        self,
        *,
        issue_number: int,
        run_id: str,
        pull_request_number: int,
        head_sha: str,
        plan_text: str,
    ) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO issues(
                    number, plan_run_id, plan_pr_number, plan_head_sha,
                    plan_text, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(number) DO UPDATE SET
                    plan_run_id = excluded.plan_run_id,
                    plan_pr_number = excluded.plan_pr_number,
                    plan_head_sha = excluded.plan_head_sha,
                    plan_text = excluded.plan_text,
                    updated_at = excluded.updated_at
                """,
                (
                    issue_number,
                    run_id,
                    pull_request_number,
                    head_sha,
                    plan_text,
                    now,
                    now,
                ),
            )

    def get_issue(self, issue_number: int) -> IssueRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM issues WHERE number = ?", (issue_number,)
            ).fetchone()
        if row is None:
            raise KeyError(issue_number)
        return _issue_from_row(row)

    def find_issue(self, issue_number: int) -> IssueRecord | None:
        try:
            return self.get_issue(issue_number)
        except KeyError:
            return None

    def issue_for_pull_request(self, pull_request_number: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT number FROM issues
                WHERE plan_pr_number = ? OR implementation_pr_number = ?
                """,
                (pull_request_number, pull_request_number),
            ).fetchone()
        if row is None:
            return None
        number = row["number"]
        return number if isinstance(number, int) else None

    def reserve_implementation(self, issue_number: int, run_id: str) -> bool:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE issues
                SET implementation_run_id = ?, updated_at = ?
                WHERE number = ? AND plan_pr_number IS NOT NULL
                  AND (implementation_run_id IS NULL OR implementation_run_id = ?)
                """,
                (run_id, _now(), issue_number, run_id),
            ).rowcount
            connection.commit()
        return bool(updated)

    def reserve_implementation_replacement(
        self,
        issue_number: int,
        run_id: str,
        previous_pull_request_number: int,
        previous_run_id: str | None = None,
    ) -> bool:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE issues
                SET implementation_run_id = ?, updated_at = ?
                WHERE number = ? AND implementation_pr_number = ?
                AND implementation_run_id IS ?
                """,
                (
                    run_id,
                    _now(),
                    issue_number,
                    previous_pull_request_number,
                    previous_run_id,
                ),
            ).rowcount
        return bool(updated)

    def record_implementation(
        self,
        *,
        issue_number: int,
        run_id: str,
        pull_request_number: int,
    ) -> None:
        with self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE issues
                SET implementation_pr_number = ?, updated_at = ?
                WHERE number = ? AND implementation_run_id = ?
                """,
                (pull_request_number, _now(), issue_number, run_id),
            ).rowcount
        if not updated:
            raise KeyError(issue_number)

    def finish_run(
        self,
        run_id: str,
        *,
        output: str,
        github_url: str | None = None,
        branch: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE runs
                SET status = ?, output = ?, error = NULL,
                    github_url = ?, branch = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    RunStatus.SUCCEEDED,
                    output,
                    github_url,
                    branch,
                    _now(),
                    run_id,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).rowcount
            if updated:
                connection.execute(
                    """
                    UPDATE issues SET implementation_run_id = NULL, updated_at = ?
                    WHERE implementation_run_id = ?
                    AND implementation_pr_number IS NULL
                    """,
                    (_now(), run_id),
                )
            connection.commit()
        if not updated:
            raise KeyError(run_id)

    def fail_run(self, run_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE runs
                SET status = ?, error = ?, updated_at = ?
                WHERE id = ? AND status IN (?, ?)
                """,
                (
                    RunStatus.FAILED,
                    error,
                    _now(),
                    run_id,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).rowcount
            if updated:
                now = _now()
                connection.execute(
                    """
                    UPDATE issues SET implementation_run_id = NULL, updated_at = ?
                    WHERE implementation_run_id = ?
                    """,
                    (now, run_id),
                )
                connection.execute(
                    """
                    UPDATE issues SET plan_run_id = NULL, updated_at = ?
                    WHERE plan_run_id = ?
                    """,
                    (now, run_id),
                )
            connection.commit()
        if not updated:
            raise KeyError(run_id)

    def pending_count(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count FROM runs
                WHERE status IN (?, ?, ?)
                """,
                (
                    RunStatus.QUEUED,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).fetchone()
        count = row["count"]
        return count if isinstance(count, int) else 0

    def mark_interrupted(self) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            now = _now()
            updated = connection.execute(
                """
                UPDATE runs SET status = ?, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    RunStatus.INTERRUPTED,
                    now,
                    RunStatus.RUNNING,
                    RunStatus.PUBLISHING,
                ),
            ).rowcount
            connection.execute(
                """
                UPDATE issues SET implementation_run_id = NULL, updated_at = ?
                WHERE implementation_pr_number IS NULL
                AND implementation_run_id IN (
                    SELECT id FROM runs WHERE status = ?
                )
                """,
                (now, RunStatus.INTERRUPTED),
            )
            connection.execute(
                """
                UPDATE issues SET plan_run_id = NULL, updated_at = ?
                WHERE plan_run_id IN (
                    SELECT id FROM runs WHERE status = ?
                )
                """,
                (now, RunStatus.INTERRUPTED),
            )
            connection.commit()
        return updated

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
        reply_number=row["reply_number"],
        kind=RunKind(row["kind"]),
        decision_action=row["decision_action"],
        tokens_consumed=row["tokens_consumed"],
        agent_history_json=row["agent_history_json"],
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


def _delivery_from_row(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=row["id"],
        event=row["event"],
        action=row["action"],
        disposition=row["disposition"],
        created_at=row["created_at"],
    )


def _issue_from_row(row: sqlite3.Row) -> IssueRecord:
    return IssueRecord(
        number=row["number"],
        plan_run_id=row["plan_run_id"],
        plan_pr_number=row["plan_pr_number"],
        plan_head_sha=row["plan_head_sha"],
        plan_text=row["plan_text"],
        implementation_run_id=row["implementation_run_id"],
        implementation_pr_number=row["implementation_pr_number"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
