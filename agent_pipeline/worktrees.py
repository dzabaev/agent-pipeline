from __future__ import annotations

import asyncio
import hashlib
import os
import re
import stat
from itertools import chain
from pathlib import Path
from typing import Mapping

from .process import terminate_process_group


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
        test_runner_user: str | None = None,
        test_runner_helper: str = "/usr/local/libexec/agent-pipeline-run-tests",
    ) -> None:
        self.repository_path = repository_path.resolve()
        self.worktree_root = worktree_root.resolve()
        self.remote_url = remote_url
        self.git_environment = dict(git_environment or os.environ)
        self.test_runner_user = test_runner_user
        self.test_runner_helper = test_runner_helper
        self._metadata_lock = asyncio.Lock()
        self._repository_access_ready = False
        self._git_directories: dict[Path, Path] = {}

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

        async with self._metadata_lock:
            self.worktree_root.mkdir(parents=True, exist_ok=True)
            await self._ensure_repository()
            if not self._repository_access_ready:
                await asyncio.to_thread(
                    _grant_repository_read_access,
                    self.repository_path,
                    self.worktree_root.stat().st_gid,
                )
                self._repository_access_ready = True
            if path.exists():
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "worktree",
                    "remove",
                    "--force",
                    str(path),
                    check=False,
                )
                await asyncio.to_thread(_remove_directory, path)
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "worktree",
                    "prune",
                    check=False,
                )
            if branch:
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "branch",
                    "--delete",
                    "--force",
                    branch,
                    check=False,
                )
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
                git_directory = Path(
                    (
                        await self._git(
                            "-C", str(path), "rev-parse", "--absolute-git-dir"
                        )
                    )
                    .decode()
                    .strip()
                ).resolve()
                if not git_directory.is_relative_to(self.repository_path):
                    raise WorktreeError("worktree git directory escaped repository")
                self._git_directories[path.resolve()] = git_directory
                await asyncio.to_thread(_grant_group_access, path)
            except Exception:
                self._git_directories.pop(path.resolve(), None)
                if path.exists():
                    await asyncio.to_thread(_remove_directory, path)
                raise
        return path

    async def remove(self, path: Path) -> None:
        resolved = path.resolve()
        if resolved.parent != self.worktree_root:
            raise WorktreeError(f"refusing to remove path outside worktree root: {path}")
        async with self._metadata_lock:
            self._git_directories.pop(resolved, None)
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

    async def cleanup_all(self) -> None:
        async with self._metadata_lock:
            if not self.worktree_root.exists():
                return
            for path in self.worktree_root.iterdir():
                if self.repository_path.exists():
                    await self._git(
                        "--git-dir",
                        str(self.repository_path),
                        "worktree",
                        "remove",
                        "--force",
                        str(path),
                        check=False,
                    )
                if path.is_symlink() or path.is_file():
                    path.unlink(missing_ok=True)
                else:
                    await asyncio.to_thread(_remove_directory, path)
            if self.repository_path.exists():
                await self._git(
                    "--git-dir",
                    str(self.repository_path),
                    "worktree",
                    "prune",
                    check=False,
                )
            self._git_directories.clear()

    async def changed_files(self, worktree: Path) -> tuple[str, ...]:
        tracked = await self._worktree_git(
            worktree,
            "diff",
            "--name-only",
            "-z",
            "HEAD",
        )
        untracked = await self._worktree_git(
            worktree,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        )
        try:
            decoded = (tracked + untracked).decode()
        except UnicodeDecodeError as error:
            raise WorktreeError("changed file path is not UTF-8") from error
        names = {name for name in decoded.split("\0") if name}
        return tuple(sorted(names))

    async def snapshot(self, worktree: Path) -> tuple[tuple[str, str], ...]:
        files = await self.changed_files(worktree)
        return await asyncio.to_thread(_snapshot_files, worktree.resolve(), files)

    async def restore_git_metadata(self, worktree: Path) -> None:
        resolved = worktree.resolve()
        git_directory = self._trusted_git_directory(resolved)
        await asyncio.to_thread(
            _restore_git_pointer,
            resolved / ".git",
            git_directory,
        )

    async def head(self, worktree: Path) -> str:
        output = await self._worktree_git(worktree, "rev-parse", "HEAD")
        return output.decode().strip()

    async def commit(self, worktree: Path, message: str) -> str:
        await self.restore_git_metadata(worktree)
        if not await self.changed_files(worktree):
            raise WorktreeError("cannot commit an empty worktree")
        await self._worktree_git(worktree, "add", "--all")
        await self._worktree_git(
            worktree,
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
        timeout_seconds: int,
    ) -> str:
        if not command:
            raise WorktreeError("test command cannot be empty")
        invocation = list(command)
        cwd = worktree
        if self.test_runner_user:
            invocation = [
                "sudo",
                "-n",
                "-H",
                "-u",
                self.test_runner_user,
                "--",
                self.test_runner_helper,
                str(self.worktree_root),
                str(worktree),
                *command,
            ]
            cwd = None
        process = await asyncio.create_subprocess_exec(
            *invocation,
            cwd=cwd,
            env=_command_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=timeout_seconds
            )
        except TimeoutError as error:
            await terminate_process_group(process)
            raise WorktreeError(
                f"command timed out after {timeout_seconds} seconds"
            ) from error
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        output = stdout.decode(errors="replace")
        if process.returncode:
            raise WorktreeError(
                f"command exited with code {process.returncode}: {output[-4000:]}"
            )
        return output

    def _trusted_git_directory(self, worktree: Path) -> Path:
        resolved = worktree.resolve()
        if resolved.parent != self.worktree_root:
            raise WorktreeError("worktree path escaped worktree root")
        try:
            return self._git_directories[resolved]
        except KeyError as error:
            raise WorktreeError("worktree is not registered") from error

    async def _worktree_git(self, worktree: Path, *arguments: str) -> bytes:
        resolved = worktree.resolve()
        await self.restore_git_metadata(resolved)
        return await self._git(
            "-C",
            str(resolved),
            "-c",
            "core.hooksPath=/dev/null",
            *arguments,
        )

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
            "origin",
            "+refs/heads/*:refs/heads/*",
            "^refs/heads/agent/*",
        )

    async def _git(self, *arguments: str, check: bool = True) -> bytes:
        process = await asyncio.create_subprocess_exec(
            "git",
            *arguments,
            env=self.git_environment,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await process.communicate()
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise
        if check and process.returncode:
            detail = stderr.decode(errors="replace").strip()
            raise WorktreeError(f"git {' '.join(arguments)} failed: {detail}")
        return stdout


def _command_environment() -> dict[str, str]:
    allowed = {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_FILE",
        "TERM",
        "TZ",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}


def _snapshot_files(
    root: Path,
    names: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    snapshot: list[tuple[str, str]] = []
    for name in names:
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorktreeError("git returned an unsafe changed path")
        candidate = root / relative
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            snapshot.append((name, "deleted"))
            continue
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            value = f"symlink:{mode:o}:{os.readlink(candidate)}"
        elif stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256()
            with candidate.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    digest.update(chunk)
            value = f"file:{mode:o}:{digest.hexdigest()}"
        else:
            raise WorktreeError(f"unsupported changed file type: {name}")
        snapshot.append((name, value))
    return tuple(snapshot)


def _restore_git_pointer(marker: Path, git_directory: Path) -> None:
    metadata = None
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        metadata = None
    if metadata is not None:
        if stat.S_ISDIR(metadata.st_mode):
            _remove_directory(marker)
        else:
            marker.unlink()
    marker.write_text(f"gitdir: {git_directory}\n")
    marker.chmod(0o644)


def _grant_repository_read_access(path: Path, group_id: int) -> None:
    for item in chain((path,), path.rglob("*")):
        if item.is_symlink():
            continue
        os.chown(item, -1, group_id)
        mode = item.stat().st_mode & ~stat.S_IWGRP
        group_mode = stat.S_IRGRP
        if item.is_dir() or mode & stat.S_IXUSR:
            group_mode |= stat.S_IXGRP
        if item.is_dir():
            group_mode |= stat.S_ISGID
        item.chmod(mode | group_mode)


def _grant_group_access(path: Path) -> None:
    for item in chain((path,), path.rglob("*")):
        if item.is_symlink():
            continue
        mode = item.stat().st_mode
        group_mode = stat.S_IRGRP | stat.S_IWGRP
        if item.is_dir() or mode & stat.S_IXUSR:
            group_mode |= stat.S_IXGRP
        if item.is_dir():
            group_mode |= stat.S_ISGID
        item.chmod(mode | group_mode)


def _remove_directory(path: Path) -> None:
    import shutil

    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        return
