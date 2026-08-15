from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class ProcessCgroup:
    path: Path | None

    @classmethod
    def create(cls, run_id: str, label: str, *, required: bool) -> "ProcessCgroup":
        try:
            line = Path("/proc/self/cgroup").read_text().splitlines()[0]
            hierarchy, _, relative = line.partition("::")
            if hierarchy != "0" or not relative:
                raise RuntimeError("cgroup v2 is unavailable")
            root = Path("/sys/fs/cgroup") / relative.lstrip("/") / "agent-pipeline-jobs"
            root.mkdir(mode=0o700, exist_ok=True)
            path = root / f"{run_id}-{label}-{uuid.uuid4().hex[:8]}"
            path.mkdir(mode=0o700)
            if not (path / "cgroup.kill").exists():
                raise RuntimeError("cgroup.kill is unavailable")
            return cls(path)
        except (IndexError, OSError, RuntimeError) as exc:
            if required:
                raise RuntimeError(f"Process cgroup isolation unavailable: {exc}") from exc
            return cls(None)

    def wrap(self, command: list[str]) -> list[str]:
        if self.path is None:
            return command
        return [
            "/bin/sh",
            "-c",
            'printf "%s" "$$" > "$1"; shift; exec "$@"',
            "agent-pipeline-cgroup",
            str(self.path / "cgroup.procs"),
            *command,
        ]

    async def kill(self) -> None:
        path = self.path
        if path is None:
            return
        try:
            (path / "cgroup.kill").write_text("1")
        except OSError as exc:
            raise RuntimeError(f"Cannot kill isolated process group: {exc}") from exc
        for _ in range(20):
            try:
                path.rmdir()
                self.path = None
                return
            except OSError:
                await asyncio.sleep(0.05)
        raise RuntimeError(f"Cannot remove process cgroup {path}")
