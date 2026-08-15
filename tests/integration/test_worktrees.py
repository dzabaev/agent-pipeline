import subprocess
import tempfile
import unittest
from pathlib import Path

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
            first = await manager.create("run-1", ref="main")
            second = await manager.create("run-2", ref="main")
            (first / "value.txt").write_text("changed\n")

            self.assertNotEqual(first, second)
            self.assertEqual((second / "value.txt").read_text(), "base\n")
            self.assertEqual(await manager.changed_files(first), ("value.txt",))
            self.assertEqual(await manager.changed_files(second), ())

            await manager.remove(first)
            await manager.remove(second)
            self.assertFalse(first.exists())
            self.assertFalse(second.exists())

    @staticmethod
    def _git(directory: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(directory), *arguments],
            check=True,
            capture_output=True,
        )


if __name__ == "__main__":
    unittest.main()
