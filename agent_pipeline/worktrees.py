from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Mapping


_RUN_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class WorktreeError(RuntimeError):
    """Raised when git workspace preparation or validation fails."""


class WorktreeManager:
    def __init__(
        self,
        *,
        repository_path: Path,
        worktree_root: Path,
        remote_url: str,
        git_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.worktree_root = worktree_root.resolve()
        self.remote_url = remote_url
        self.git_environment = dict(git_environment or os.environ)
        self._metadata_lock = asyncio.Lock()

    async def create(
        self,
        run_id: str,
        *,
        ref: str,
        branch: str | None = None,
    ) -> Path:
        if not _RUN_ID.fullmatch(run_id):
            raise WorktreeError("run ID contains unsafe path characters")
        path = self.worktree_root / run_id
        if path.exists():
            raise WorktreeError(f"worktree already exists: {path}")

        async with self._metadata_lock:
            await self._ensure_repository()
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            arguments = [
                "--git-dir",
                str(self.repository_path),
                "worktree",
                "add",
            ]
            if branch:
                arguments.extend(["-b", branch])
            else:
                arguments.append("--detach")
            arguments.extend([str(path), ref])
            try:
                await self._git(*arguments)
            except Exception:
                if path.exists():
                    await asyncio.to_thread(_remove_directory, path)
                raise
        return path

    async def remove(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved.parent != self.worktree_root:
            raise WorktreeError(f"refusing to remove path outside worktree root: {path}")
        async with self._metadata_lock:
            if self.repository_path.exists():
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "worktree",
                    "remove",
                    "--force",
                    str(resolved),
                    check=False,
                )
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "worktree",
                    "prune",
                    check=False,
                )
            if resolved.exists():
                await asyncio.to_thread(_remove_directory, resolved)

    async def changed_files(self, worktree: Path) -> tuple[str, ...]:
        tracked = await self._git(
            "-C",
            str(worktree),
            "diff",
            "--name-only",
            "-z",
            "HEAD",
        )
        untracked = await self._git(
            "-C",
            str(worktree),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        names = {
            name
            for name in (tracked + untracked).decode(errors="replace").split("\0")
            if name
        }
        return tuple(sorted(names))

    async def head(self, worktree: Path) -> str:
        output = await self._git("-C", str(worktree), "rev-parse", "HEAD")
        return output.decode().strip()

    async def commit(self, worktree: Path, message: str) -> str:
        if not await self.changed_files(worktree):
            raise WorktreeError("cannot commit an empty worktree")
        await self._git("-C", str(worktree), "add", "--all")
        await self._git(
            "-C",
            str(worktree),
            "-c",
            "user.name=Agent Pipeline",
            "-c",
            "user.email=agent-pipeline@localhost",
            "commit",
            "-m",
            message,
        )
        return await self.head(worktree)

    async def run_command(
        self,
        worktree: Path,
        command: tuple[str, ...],
    ) -> str:
        if not command:
            raise WorktreeError("test command cannot be empty")
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=worktree,
            env=self.git_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await process.communicate()
        output = stdout.decode(errors="replace")
        if process.returncode:
            raise WorktreeError(
                f"command exited with code {process.returncode}: {output[-4000:]}"
            )
        return output

    async def _ensure_repository(self) -> None:
        if not self.repository_path.exists():
            self.repository_path.parent.mkdir(parents=True, exist_ok=True)
            await self._git(
                "clone",
                "--mirror",
                self.remote_url,
                str(self.repository_path),
            )
            return
        await self._git(
            "--git-dir",
            str(self.repository_path),
            "fetch",
            "--prune",
            "origin",
        )

    async def _git(self, *arguments: str, check: bool = True) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            env=self.git_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if check and process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise WorktreeError(f"git {' '.join(arguments)} failed: {detail}")
        return stdout


def _remove_directory(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
