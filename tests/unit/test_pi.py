import json
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from agent_pipeline.contracts import AgentRequest, RunKind  # pyright: ignore[reportMissingImports]
from agent_pipeline.pi import PiAgentRunner  # pyright: ignore[reportMissingImports]


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
                result = await PiAgentRunner(str(executable)).run(request)
            invocation = json.loads(capture.read_text())

        self.assertEqual(result.output, "done")
        self.assertEqual(invocation["cwd"], str(worktree))
        self.assertIsNone(invocation["github_token"])
        self.assertIn("--mode", invocation["args"])
        self.assertIn("read,grep", invocation["args"])


if __name__ == "__main__":
    unittest.main()
