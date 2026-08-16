import subprocess
import tempfile
import unittest
from pathlib import Path

from agent_pipeline.github import GitHubCodeHost  # pyright: ignore[reportMissingImports]
from agent_pipeline.worktrees import WorktreeManager  # pyright: ignore[reportMissingImports]


class WorktreeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_each_run_gets_an_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            origin = root / "origin"
            origin.mkdir()
            self._git(origin, "init", "-b", "main")
            self._git(origin, "config", "user.name", "Test")
            self._git(origin, "config", "user.email", "test@example.com")
            (origin / "value.txt").write_text("base\n")
            self._git(origin, "add", "value.txt")
            self._git(origin, "commit", "-m", "base")

            manager = WorktreeManager(
                repository_path=root / "repository.git",
                worktree_root=root / "worktrees",
                remote_url=str(origin),
            )
            first = await manager.create(
                "run-1", ref="main", branch="agent/plan-1"
            )
            second = await manager.create("run-2", ref="main")
            (first / "value.txt").write_text("changed\n")

            self.assertNotEqual(first, second)
            self.assertEqual((second / "value.txt").read_text(), "base\n")
            self.assertEqual(await manager.changed_files(first), ("value.txt",))
            self.assertEqual(await manager.changed_files(second), ())

            git_marker = first / ".git"
            git_marker.unlink()
            hooks = git_marker / "hooks"
            hooks.mkdir(parents=True)
            sentinel = root / "hook-ran"
            hook = hooks / "pre-commit"
            hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
            hook.chmod(0o755)
            await manager.commit(first, "test: trusted metadata")
            self.assertFalse(sentinel.exists())
            self.assertTrue(git_marker.is_file())

            host = GitHubCodeHost(
                repository="owner/repository",
                token="token",
                webhook_secret="secret",
                bot_login="pipeline-bot",
                api_url="https://api.github.test",
            )
            await host.push_branch(first, "agent/plan-1")
            (first / "value.txt").write_text("changed again\n")
            await manager.commit(first, "test: update branch")
            await host.push_branch(first, "agent/plan-1")
            await host.close()
            self._git(origin, "rev-parse", "refs/heads/agent/plan-1")

            first = await manager.create("run-1", ref="main")
            self.assertEqual((first / "value.txt").read_text(), "base\n")

            await manager.remove(first)
            await manager.remove(second)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

            stale = await manager.create("interrupted-run", ref="main")
            await manager.cleanup_all()
            self.assertFalse(stale.exists())

    @staticmethod
    def _git(directory: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(directory), *arguments],
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
