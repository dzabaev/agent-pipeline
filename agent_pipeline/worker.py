from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .db import Database, RunRecord


@dataclass(frozen=True, slots=True)
class RunOutcome:
    output: str
    github_url: str | None = None
    branch: str | None = None


RunProcessor = Callable[[RunRecord], Awaitable[RunOutcome]]


class WorkerPool:
    def __init__(
        self,
        database: Database,
        processor: RunProcessor,
        *,
        concurrency: int,
        poll_interval: float = 0.25,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be greater than zero")
        self.database = database
        self.processor = processor
        self.concurrency = concurrency
        self.poll_interval = poll_interval
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        if self._tasks:
            return
        await asyncio.to_thread(self.database.mark_interrupted)
        self._stop.clear()
        self._tasks = [
            asyncio.create_task(self._work(), name=f"agent-worker-{index + 1}")
            for index in range(self.concurrency)
        ]

    async def stop(self) -> None:
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await asyncio.to_thread(self.database.mark_interrupted)

    async def join(self, timeout: float | None = None) -> None:
        async def wait_until_idle() -> None:
            while await asyncio.to_thread(self.database.pending_count):
                await asyncio.sleep(self.poll_interval)

        await asyncio.wait_for(wait_until_idle(), timeout=timeout)

    async def _work(self) -> None:
        while not self._stop.is_set():
            run = await asyncio.to_thread(self.database.claim_next_run)
            if run is None:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(), timeout=self.poll_interval
                    )
                except TimeoutError:
                    continue
                continue

            try:
                outcome = await self.processor(run)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await asyncio.to_thread(
                    self.database.fail_run,
                    run.id,
                    f"{type(error).__name__}: {error}",
                )
            else:
                await asyncio.to_thread(
                    self.database.finish_run,
                    run.id,
                    output=outcome.output,
                    github_url=outcome.github_url,
                    branch=outcome.branch,
                )
