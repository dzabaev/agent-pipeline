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


if __name__ == "__main__":
    unittest.main()
