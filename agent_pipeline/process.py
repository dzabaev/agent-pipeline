from __future__ import annotations

import asyncio
import os
import signal


async def terminate_process_group(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 5,
) -> None:
    _signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
        return
    except TimeoutError:
        _signal_group(process.pid, signal.SIGKILL)
    try:
        await asyncio.wait_for(process.wait(), timeout=grace_seconds)
    except TimeoutError:
        return


def _signal_group(pid: int | None, requested_signal: signal.Signals) -> None:
    if pid is None:
        return
    try:
        os.killpg(pid, requested_signal)
    except ProcessLookupError:
        return
