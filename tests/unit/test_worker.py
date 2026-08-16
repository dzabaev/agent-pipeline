import asyncio
import tempfile
import unittest
from pathlib import Path

from agent_pipeline.contracts import RunKind  # pyright: ignore[reportMissingImports]
from agent_pipeline.db import Database  # pyright: ignore[reportMissingImports]
from agent_pipeline.worker import RunOutcome, WorkerPool  # pyright: ignore[reportMissingImports]


class WorkerPoolTests(unittest.IsolatedAsyncioTestCase):
    async def test_limits_parallel_runs_to_configured_count(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Database(Path(directory) / "app.db")
            database.initialize()
            for number in range(1, 5):
                delivery_id = f"delivery-{number}"
                database.record_delivery(delivery_id, "issues", "opened", "{}")
                database.enqueue_run(
                    delivery_id=delivery_id,
                    issue_number=number,
                    kind=RunKind.PLAN,
                    actor="alice",
                    prompt_context="plan",
                )

            active = 0
            peak = 0
            processed = 0
            three_started = asyncio.Event()
            release = asyncio.Event()
            all_done = asyncio.Event()
            lock = asyncio.Lock()

            async def process(_run: object) -> RunOutcome:
                nonlocal active, peak, processed
                async with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 3:
                        three_started.set()
                await release.wait()
                async with lock:
                    active -= 1
                    processed += 1
                    if processed == 4:
                        all_done.set()
                return RunOutcome(output="done")

            pool = WorkerPool(database, process, concurrency=3, poll_interval=0.01)
            await pool.start()
            await asyncio.wait_for(three_started.wait(), timeout=2)
            release.set()
            await asyncio.wait_for(all_done.wait(), timeout=2)
            await pool.join(timeout=2)
            await pool.stop()

        self.assertEqual(peak, 3)
        self.assertEqual(processed, 4)


if __name__ == "__main__":
    unittest.main()
