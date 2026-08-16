import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agent_pipeline.contracts import AgentRequest, RunKind  # pyright: ignore[reportMissingImports]
from agent_pipeline.pi import (  # pyright: ignore[reportMissingImports]
    AgentExecutionError,
    PiAgentRunner,
)


class PiAgentRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_in_requested_worktree_and_hides_app_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            worktree = root / "worktree"
            worktree.mkdir()
            capture = root / "capture.json"
            executable = root / "fake-pi"
            executable.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env python3
                    import json, os, pathlib, sys
                    pathlib.Path(os.environ["FAKE_CAPTURE_PATH"]).write_text(json.dumps({
                        "args": sys.argv[1:],
                        "cwd": os.getcwd(),
                        "github_token": os.environ.get("GITHUB_TOKEN"),
                    }))
                    print(json.dumps({"type": "session", "id": "session-1"}))
                    print(json.dumps({
                        "type": "message_end",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "done"}],
                            "model": "claude-test-model",
                        },
                    }))
                    """
                )
            )
            executable.chmod(0o755)
            request = AgentRequest(
                run_id="run-1",
                kind=RunKind.REVIEW,
                prompt="Review this",
                worktree=worktree,
                timeout_seconds=5,
                tools=("read", "grep"),
            )
            with patch.dict(
                os.environ,
                {
                    "FAKE_CAPTURE_PATH": str(capture),
                    "GITHUB_TOKEN": "must-not-leak",
                },
            ):
                result = await PiAgentRunner(
                    str(executable), model="selected-model"
                ).run(request)
            invocation = json.loads(capture.read_text())

        self.assertEqual(result.output, "done")
        self.assertEqual(result.model_name, "claude-test-model")
        self.assertEqual(invocation["cwd"], str(worktree))
        self.assertIsNone(invocation["github_token"])
        self.assertIn("--mode", invocation["args"])
        self.assertIn("read,grep", invocation["args"])
        model_index = invocation["args"].index("--model")
        self.assertEqual(invocation["args"][model_index + 1], "selected-model")
        thinking_index = invocation["args"].index("--thinking")
        self.assertEqual(invocation["args"][thinking_index + 1], "medium")

    async def test_production_runner_hides_sibling_worktrees_with_bubblewrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory) / "worktrees" / "run-1"
            worktree.mkdir(parents=True)
            process = AsyncMock()
            process.communicate.return_value = (
                b'{"type":"message_end","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n',
                b"",
            )
            process.returncode = 0
            process.pid = 123
            spawn = AsyncMock(return_value=process)

            with patch(
                "agent_pipeline.pi.asyncio.create_subprocess_exec", spawn
            ):
                result = await PiAgentRunner(
                    "/opt/pi", "pi-runner", model="selected-model"
                ).run(_request(worktree, timeout_seconds=5))

        self.assertEqual(result.model_name, "selected-model")
        awaited = spawn.await_args
        if awaited is None:
            self.fail("Pi process was not started")
        invocation = awaited.args
        self.assertEqual(invocation[:6], ("sudo", "-n", "-H", "-u", "pi-runner", "--"))
        self.assertIn("/usr/bin/bwrap", invocation)
        self.assertIn(str(worktree.parent), invocation)
        self.assertIn("/mnt/agent-pipeline/worktree", invocation)
        self.assertEqual(invocation[-1], "Review")

    async def test_rejects_nonzero_exit_and_malformed_json(self) -> None:
        cases = (
            ("import sys; print('boom', file=sys.stderr); sys.exit(7)", "code 7"),
            ("print('not-json')", "invalid JSON"),
        )
        for script, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as directory:
                worktree = Path(directory)
                executable = worktree / "fake-pi"
                executable.write_text(f"#!/usr/bin/env python3\n{script}\n")
                executable.chmod(0o755)
                with self.assertRaises(AgentExecutionError) as raised:
                    await PiAgentRunner(
                        str(executable), model="selected-model"
                    ).run(_request(worktree, timeout_seconds=5))
                self.assertIn(expected, str(raised.exception))

    async def test_timeout_terminates_pi(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            worktree = Path(directory)
            executable = worktree / "fake-pi"
            executable.write_text(
                "#!/usr/bin/env python3\nimport time\ntime.sleep(60)\n"
            )
            executable.chmod(0o755)

            with self.assertRaises(AgentExecutionError) as raised:
                await PiAgentRunner(
                    str(executable), model="selected-model"
                ).run(_request(worktree, timeout_seconds=1))

        self.assertIn("timed out", str(raised.exception))


def _request(worktree: Path, *, timeout_seconds: int) -> AgentRequest:
    return AgentRequest(
        run_id="run-test",
        kind=RunKind.REVIEW,
        prompt="Review",
        worktree=worktree,
        timeout_seconds=timeout_seconds,
        tools=("read",),
    )


if __name__ == "__main__":
    unittest.main()
