import sqlite3
import tempfile
import unittest
from pathlib import Path

from agent_pipeline.contracts import RunKind, RunStatus  # pyright: ignore[reportMissingImports]
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]


class DatabaseTests(unittest.TestCase):
    def test_delivery_is_idempotent_and_queued_run_is_claimed_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()

            first = database.record_delivery(
                "delivery-1", "issue_comment", "created", '{"ok": true}'
            )
            second = database.record_delivery(
                "delivery-1", "issue_comment", "created", '{"ok": true}'
            )
            run_id = database.enqueue_run(
                delivery_id="delivery-1",
                issue_number=7,
                kind=RunKind.REVIEW,
                actor="alice",
                prompt_context="review this",
            )
            claimed = database.claim_next_run()
            no_second_claim = database.claim_next_run()

        self.assertTrue(first)
        self.assertFalse(second)
        if claimed is None:
            self.fail("queued run was not claimed")
        self.assertEqual(claimed.id, run_id)
        self.assertEqual(claimed.status, RunStatus.RUNNING)
        self.assertIsNone(no_second_claim)

    def test_only_one_run_can_reserve_issue_implementation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=9,
                run_id="plan-run",
                pull_request_number=10,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )

            first = database.reserve_implementation(9, "run-1")
            second = database.reserve_implementation(9, "run-2")

        self.assertTrue(first)
        self.assertFalse(second)

    def test_merge_and_approval_ingest_one_implementation_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=9,
                run_id="plan-run",
                pull_request_number=10,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )

            first = database.ingest_run(
                delivery_id="merge",
                event="pull_request",
                action="closed",
                payload_json="{}",
                issue_number=9,
                kind=RunKind.IMPLEMENTATION,
                actor="alice",
                prompt_context="",
                reserve_implementation=True,
            )
            second = database.ingest_run(
                delivery_id="approval",
                event="issue_comment",
                action="created",
                payload_json="{}",
                issue_number=9,
                kind=RunKind.IMPLEMENTATION,
                actor="alice",
                prompt_context="yes",
                reserve_implementation=True,
            )
            run_count = len(database.list_runs())

        self.assertTrue(first)
        self.assertEqual(second, "")
        self.assertEqual(run_count, 1)

    def test_failed_run_releases_unpublished_implementation_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            database.record_delivery("delivery", "issue_comment", "created", "{}")
            run_id = database.enqueue_run(
                delivery_id="delivery",
                issue_number=9,
                kind=RunKind.IMPLEMENTATION,
                actor="alice",
                prompt_context="implement",
            )
            database.record_plan(
                issue_number=9,
                run_id="plan-run",
                pull_request_number=10,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )
            claimed = database.claim_next_run()
            if claimed is None:
                self.fail("implementation run was not claimed")
            self.assertTrue(database.reserve_implementation(9, run_id))

            database.fail_run(run_id, "failed")

            self.assertTrue(database.reserve_implementation(9, "retry-run"))

    def test_initialize_migrates_existing_runs_for_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "app.db"
            with sqlite3.connect(path) as connection:
                connection.executescript(
                    """
                    CREATE TABLE deliveries (
                        id TEXT PRIMARY KEY, event TEXT NOT NULL,
                        action TEXT NOT NULL, payload_json TEXT NOT NULL,
                        disposition TEXT NOT NULL DEFAULT 'received',
                        created_at TEXT NOT NULL
                    );
                    CREATE TABLE runs (
                        id TEXT PRIMARY KEY,
                        delivery_id TEXT NOT NULL REFERENCES deliveries(id),
                        issue_number INTEGER NOT NULL,
                        kind TEXT NOT NULL CHECK (kind IN ('plan', 'review', 'implementation')),
                        status TEXT NOT NULL,
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
                    ALTER TABLE runs ADD COLUMN reply_number INTEGER NOT NULL DEFAULT 0;
                    INSERT INTO deliveries(id, event, action, payload_json, created_at)
                    VALUES ('old-delivery', 'issue_comment', 'created', '{}', 'now');
                    INSERT INTO runs(
                        id, delivery_id, issue_number, reply_number, kind, status,
                        actor, prompt_context, created_at, updated_at
                    ) VALUES (
                        'old-run', 'old-delivery', 7, 9, 'review', 'succeeded',
                        'alice', 'old', 'now', 'now'
                    );
                    """
                )
            database = Database(path)
            database.initialize()
            database.record_delivery(
                "decision-delivery", "issue_comment", "created", "{}"
            )
            decision_run = database.enqueue_run(
                delivery_id="decision-delivery",
                issue_number=7,
                kind=RunKind.DECISION,
                actor="alice",
                prompt_context="new",
            )

            migrated = database.get_run("old-run")
            decision = database.get_run(decision_run)

        self.assertEqual(migrated.reply_number, 9)
        self.assertEqual(migrated.kind, RunKind.REVIEW)
        self.assertEqual(decision.kind, RunKind.DECISION)

    def test_only_one_run_can_reserve_plan_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="old-plan-run",
                pull_request_number=42,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )

            first = database.reserve_plan(
                7,
                "replacement-a",
                previous_pull_request_number=42,
                previous_run_id="old-plan-run",
            )
            second = database.reserve_plan(
                7,
                "replacement-b",
                previous_pull_request_number=42,
                previous_run_id="old-plan-run",
            )

        self.assertTrue(first)
        self.assertFalse(second)

    def test_only_one_run_can_reserve_implementation_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            database.record_plan(
                issue_number=7,
                run_id="plan-run",
                pull_request_number=42,
                head_sha="plan-sha",
                plan_text="# Plan\n",
            )
            database.reserve_implementation(7, "old-run")
            database.record_implementation(
                issue_number=7,
                run_id="old-run",
                pull_request_number=43,
            )

            first = database.reserve_implementation_replacement(
                7, "replacement-a", 43, "old-run"
            )
            second = database.reserve_implementation_replacement(
                7, "replacement-b", 43, "old-run"
            )

        self.assertTrue(first)
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
