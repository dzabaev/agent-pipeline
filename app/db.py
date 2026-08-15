from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(UTC).isoformat()


def _last_id(cursor: sqlite3.Cursor) -> int:
    value = cursor.lastrowid
    if value is None:
        raise RuntimeError("SQLite did not return an inserted row ID")
    return value


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY,
    github_repo_id INTEGER NOT NULL UNIQUE,
    installation_id INTEGER NOT NULL,
    full_name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    default_branch TEXT NOT NULL,
    verification_command TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rules (
    id INTEGER PRIMARY KEY,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    event TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('advisory', 'change')),
    instruction TEXT NOT NULL,
    filters_json TEXT NOT NULL DEFAULT '{}',
    model TEXT,
    thinking_level TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    disposition TEXT NOT NULL DEFAULT 'received',
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    publication_key TEXT NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
    rule_id INTEGER REFERENCES rules(id) ON DELETE SET NULL,
    delivery_id TEXT REFERENCES deliveries(delivery_id) ON DELETE SET NULL,
    source TEXT NOT NULL CHECK(source IN ('automatic', 'manual')),
    kind TEXT NOT NULL CHECK(kind IN ('advisory', 'change')),
    status TEXT NOT NULL,
    instruction TEXT NOT NULL,
    context_json TEXT NOT NULL DEFAULT '{}',
    target_kind TEXT,
    target_number INTEGER,
    continuation_run_id TEXT,
    model TEXT,
    thinking_level TEXT,
    worktree_path TEXT,
    session_file TEXT,
    output_text TEXT,
    verification_output TEXT,
    branch_name TEXT,
    commit_sha TEXT,
    github_comment_url TEXT,
    pull_request_url TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    queued_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    cleanup_after TEXT,
    worker_id TEXT,
    UNIQUE(delivery_id, rule_id)
);
CREATE INDEX IF NOT EXISTS runs_queue ON runs(status, queued_at);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);

CREATE TABLE IF NOT EXISTS run_controls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    body TEXT,
    status TEXT NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def initialize(self, worker_concurrency: int) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript(SCHEMA)
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
            if "publication_key" not in columns:
                connection.execute("ALTER TABLE runs ADD COLUMN publication_key TEXT")
                connection.execute("UPDATE runs SET publication_key=id WHERE publication_key IS NULL")
            connection.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS runs_active_publication
                   ON runs(publication_key)
                   WHERE status IN ('queued','preparing','running','verifying','publishing')"""
            )
            connection.execute(
                "INSERT OR IGNORE INTO app_settings(key, value) VALUES ('worker_concurrency', ?)",
                (str(worker_concurrency),),
            )
            connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError as exc:
            raise RuntimeError(f"Cannot secure database file {self.path}: {exc}") from exc

    def recover_interrupted(self) -> int:
        with closing(self.connect()) as connection:
            stamp = now()
            cursor = connection.execute(
                """UPDATE runs SET status='interrupted', finished_at=?,
                   error=COALESCE(error, 'Server restarted during job'), cleanup_after=?
                   WHERE status IN ('preparing','running','verifying','publishing')""",
                (stamp, (datetime.now(UTC) + timedelta(days=3)).isoformat()),
            )
            connection.commit()
            return cursor.rowcount

    def add_repository(
        self,
        *,
        github_repo_id: int,
        installation_id: int,
        full_name: str,
        default_branch: str,
        verification_command: str,
    ) -> int:
        stamp = now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO repositories(
                    github_repo_id, installation_id, full_name, default_branch,
                    verification_command, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(github_repo_id) DO UPDATE SET
                    installation_id=excluded.installation_id,
                    full_name=excluded.full_name,
                    default_branch=excluded.default_branch,
                    verification_command=excluded.verification_command,
                    updated_at=excluded.updated_at""",
                (
                    github_repo_id,
                    installation_id,
                    full_name,
                    default_branch,
                    verification_command,
                    stamp,
                    stamp,
                ),
            )
            row = connection.execute(
                "SELECT id FROM repositories WHERE github_repo_id=?", (github_repo_id,)
            ).fetchone()
            connection.commit()
            if row is None or not isinstance(row["id"], int):
                raise RuntimeError("Repository upsert did not return a row")
            return row["id"]

    def get_repository(self, repository_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM repositories WHERE id=?", (repository_id,))

    def get_repository_by_github_id(self, github_repo_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM repositories WHERE github_repo_id=?", (github_repo_id,))

    def list_repositories(self) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM repositories ORDER BY full_name")

    def toggle_repository(self, repository_id: int) -> None:
        self._execute(
            "UPDATE repositories SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END, updated_at=? WHERE id=?",
            (now(), repository_id),
        )

    def add_rule(
        self,
        *,
        repository_id: int,
        name: str,
        event: str,
        actions: list[str],
        kind: str,
        instruction: str,
        filters: dict[str, Any],
        model: str | None,
        thinking_level: str | None,
    ) -> int:
        stamp = now()
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO rules(
                    repository_id,name,event,actions_json,kind,instruction,filters_json,
                    model,thinking_level,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    repository_id,
                    name,
                    event,
                    json.dumps(actions),
                    kind,
                    instruction,
                    json.dumps(filters),
                    model,
                    thinking_level,
                    stamp,
                    stamp,
                ),
            )
            connection.commit()
            return _last_id(cursor)

    def list_rules(self, repository_id: int | None = None) -> list[dict[str, Any]]:
        if repository_id is None:
            return self._all(
                """SELECT rules.*, repositories.full_name FROM rules
                   JOIN repositories ON repositories.id=rules.repository_id
                   ORDER BY rules.id DESC"""
            )
        return self._all(
            "SELECT * FROM rules WHERE repository_id=? ORDER BY id DESC", (repository_id,)
        )

    def toggle_rule(self, rule_id: int) -> None:
        self._execute(
            "UPDATE rules SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END, updated_at=? WHERE id=?",
            (now(), rule_id),
        )

    def has_delivery(self, delivery_id: str) -> bool:
        return self._one("SELECT delivery_id FROM deliveries WHERE delivery_id=?", (delivery_id,)) is not None

    def record_delivery(self, delivery_id: str, event: str, payload: dict[str, Any]) -> bool:
        try:
            self._execute(
                "INSERT INTO deliveries(delivery_id,event,payload_json,received_at) VALUES (?,?,?,?)",
                (delivery_id, event, json.dumps(payload), now()),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def set_delivery_disposition(self, delivery_id: str, disposition: str) -> None:
        self._execute(
            "UPDATE deliveries SET disposition=? WHERE delivery_id=?",
            (disposition, delivery_id),
        )

    def delete_delivery(self, delivery_id: str) -> None:
        self._execute("DELETE FROM deliveries WHERE delivery_id=?", (delivery_id,))

    def enqueue_delivery(
        self,
        delivery_id: str,
        event: str,
        payload: dict[str, Any],
        disposition: str,
        run_specs: list[dict[str, Any]],
    ) -> tuple[bool, list[str]]:
        stamp = now()
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """INSERT INTO deliveries(
                        delivery_id,event,payload_json,disposition,received_at
                    ) VALUES (?,?,?,?,?)""",
                    (delivery_id, event, json.dumps(payload), disposition, stamp),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                return False, []
            run_ids: list[str] = []
            for spec in run_specs:
                run_id = str(uuid.uuid4())
                publication_key = str(spec.get("publication_key") or run_id)
                connection.execute(
                    """INSERT INTO runs(
                        id,publication_key,repository_id,rule_id,delivery_id,source,kind,status,
                        instruction,context_json,target_kind,target_number,model,thinking_level,queued_at
                    ) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?)""",
                    (
                        run_id,
                        publication_key,
                        spec["repository_id"],
                        spec.get("rule_id"),
                        delivery_id,
                        spec.get("source", "automatic"),
                        spec["kind"],
                        spec["instruction"],
                        json.dumps(spec.get("context") or {}),
                        spec.get("target_kind"),
                        spec.get("target_number"),
                        spec.get("model"),
                        spec.get("thinking_level"),
                        stamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO run_events(
                        run_id,sequence,kind,payload_json,created_at
                    ) VALUES (?,1,'queued',?,?)""",
                    (run_id, json.dumps({"source": spec.get("source", "automatic"), "kind": spec["kind"]}), stamp),
                )
                run_ids.append(run_id)
            connection.commit()
            return True, run_ids

    def create_run(
        self,
        *,
        repository_id: int,
        source: str,
        kind: str,
        instruction: str,
        context: dict[str, Any] | None = None,
        rule_id: int | None = None,
        delivery_id: str | None = None,
        target_kind: str | None = None,
        target_number: int | None = None,
        continuation_run_id: str | None = None,
        model: str | None = None,
        thinking_level: str | None = None,
        publication_key: str | None = None,
    ) -> str | None:
        run_id = str(uuid.uuid4())
        try:
            self._execute(
                """INSERT INTO runs(
                    id,publication_key,repository_id,rule_id,delivery_id,source,kind,status,instruction,
                    context_json,target_kind,target_number,continuation_run_id,model,
                    thinking_level,queued_at
                ) VALUES (?,?,?,?,?,?,?,'queued',?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    publication_key or run_id,
                    repository_id,
                    rule_id,
                    delivery_id,
                    source,
                    kind,
                    instruction,
                    json.dumps(context or {}),
                    target_kind,
                    target_number,
                    continuation_run_id,
                    model,
                    thinking_level,
                    now(),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        self.add_event(run_id, "queued", {"source": source, "kind": kind})
        return run_id

    def claim_run(self, worker_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT 1 FROM runs
                   WHERE status IN ('preparing','running','verifying','publishing') LIMIT 1"""
            ).fetchone()
            if active is not None:
                connection.commit()
                return None
            row = connection.execute(
                "SELECT id FROM runs WHERE status='queued' ORDER BY queued_at LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            stamp = now()
            connection.execute(
                "UPDATE runs SET status='preparing', worker_id=?, started_at=? WHERE id=? AND status='queued'",
                (worker_id, stamp, row["id"]),
            )
            run = connection.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
            return dict(run) if run else None

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        return self._one(
            """SELECT runs.*, repositories.full_name, repositories.installation_id,
               repositories.github_repo_id, repositories.default_branch,
               repositories.verification_command
               FROM runs JOIN repositories ON repositories.id=runs.repository_id
               WHERE runs.id=?""",
            (run_id,),
        )

    def previous_publication_run(
        self, publication_key: str, current_run_id: str
    ) -> dict[str, Any] | None:
        return self._one(
            """SELECT * FROM runs
               WHERE publication_key=? AND id<>?
                 AND commit_sha IS NOT NULL AND verification_output IS NOT NULL
               ORDER BY queued_at DESC LIMIT 1""",
            (publication_key, current_run_id),
        )

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        return self._all(
            """SELECT runs.*, repositories.full_name FROM runs
               JOIN repositories ON repositories.id=runs.repository_id
               ORDER BY queued_at DESC LIMIT ?""",
            (limit,),
        )

    def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {
            "status", "worktree_path", "session_file", "output_text",
            "verification_output", "branch_name", "commit_sha",
            "github_comment_url", "pull_request_url", "error",
            "cancel_requested", "finished_at", "cleanup_after",
        }
        invalid = set(fields) - allowed
        if invalid:
            raise ValueError(f"Invalid run fields: {', '.join(sorted(invalid))}")
        if not fields:
            return
        assignments = ", ".join(f"{field}=?" for field in fields)
        self._execute(
            f"UPDATE runs SET {assignments} WHERE id=?",
            (*fields.values(), run_id),
        )

    def add_event(self, run_id: str, kind: str, payload: dict[str, Any]) -> int:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM run_events WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if row is None or not isinstance(row["next_sequence"], int):
                raise RuntimeError("Could not allocate event sequence")
            sequence = row["next_sequence"]
            cursor = connection.execute(
                "INSERT INTO run_events(run_id,sequence,kind,payload_json,created_at) VALUES (?,?,?,?,?)",
                (run_id, sequence, kind, json.dumps(payload), now()),
            )
            connection.commit()
            return _last_id(cursor)

    def get_events(self, run_id: str, after: int = 0) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM run_events WHERE run_id=? AND id>? ORDER BY id",
            (run_id, after),
        )

    def add_control(self, run_id: str, kind: str, body: str | None, status: str) -> int:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO run_controls(run_id,kind,body,status,created_at,sent_at) VALUES (?,?,?,?,?,?)",
                (run_id, kind, body, status, now(), now() if status == "sent" else None),
            )
            connection.commit()
            return _last_id(cursor)

    def request_cancel(self, run_id: str) -> str | None:
        with closing(self.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                connection.commit()
                return None
            status = str(row["status"])
            if status == "queued":
                connection.execute(
                    """UPDATE runs SET status='cancelled',cancel_requested=1,
                       finished_at=?,cleanup_after=? WHERE id=?""",
                    (now(), (datetime.now(UTC) + timedelta(days=3)).isoformat(), run_id),
                )
                connection.commit()
                return "cancelled"
            if status not in {"preparing", "running", "verifying"}:
                connection.commit()
                return None
            connection.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
            connection.commit()
            return status

    def begin_verifying(self, run_id: str) -> bool:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """UPDATE runs SET status='verifying'
                   WHERE id=? AND status='running' AND cancel_requested=0""",
                (run_id,),
            )
            connection.commit()
            return cursor.rowcount == 1

    def begin_publishing(self, run_id: str, expected_status: str) -> bool:
        with closing(self.connect()) as connection:
            cursor = connection.execute(
                """UPDATE runs SET status='publishing'
                   WHERE id=? AND status=? AND cancel_requested=0""",
                (run_id, expected_status),
            )
            connection.commit()
            return cursor.rowcount == 1

    def get_worker_concurrency(self, fallback: int) -> int:
        return 1

    def set_worker_concurrency(self, value: int) -> None:
        if value != 1:
            raise ValueError("Host-worktree mode requires worker concurrency 1")
        self._execute(
            "INSERT INTO app_settings(key,value) VALUES ('worker_concurrency',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(value),),
        )

    def due_worktrees(self) -> list[dict[str, Any]]:
        return self._all(
            """SELECT * FROM runs WHERE worktree_path IS NOT NULL
               AND cleanup_after IS NOT NULL AND cleanup_after <= ?""",
            (now(),),
        )

    def clear_worktree(self, run_id: str) -> None:
        self._execute(
            "UPDATE runs SET worktree_path=NULL, cleanup_after=NULL WHERE id=?", (run_id,)
        )

    def _execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with closing(self.connect()) as connection:
            # pi-lens-ignore: python-sql-injection
            connection.execute(sql, parameters)
            connection.commit()

    def _one(self, sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        with closing(self.connect()) as connection:
            # pi-lens-ignore: python-sql-injection
            row = connection.execute(sql, parameters).fetchone()
            return dict(row) if row else None

    def _all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with closing(self.connect()) as connection:
            # pi-lens-ignore: python-sql-injection
            return [dict(row) for row in connection.execute(sql, parameters).fetchall()]
