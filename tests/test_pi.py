# pyright: reportMissingImports=false
import asyncio
from pathlib import Path
from typing import cast

import pytest

from app.config import Settings
from app.pi import PiRunner, _ActiveProcess


FAKE_PI = '''#!/usr/bin/env python3
import json, sys
last_text = "done"
for line in sys.stdin:
    command = json.loads(line)
    kind = command.get("type")
    if kind == "prompt":
        last_text = None if command.get("message") == "No output" else "done"
        print(json.dumps({"id": command.get("id"), "type": "response", "command": "prompt", "success": True}), flush=True)
        print(json.dumps({"type": "agent_start"}), flush=True)
        if last_text:
            print(json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": last_text}}), flush=True)
        print(json.dumps({"type": "agent_settled"}), flush=True)
    elif kind == "get_last_assistant_text":
        print(json.dumps({"id": command["id"], "type": "response", "success": True, "data": {"text": last_text}}), flush=True)
    elif kind == "get_state":
        print(json.dumps({"id": command["id"], "type": "response", "success": True, "data": {"sessionFile": "/tmp/session.jsonl"}}), flush=True)
'''


@pytest.mark.parametrize(("prompt", "expected"), [("Do work", "done"), ("No output", "")])
def test_pi_rpc_run(tmp_path: Path, prompt: str, expected: str) -> None:
    executable = tmp_path / "fake-pi"
    executable.write_text(FAKE_PI)
    executable.chmod(0o755)
    settings = Settings(
        "http://localhost:8000",
        "secret",
        "client",
        "client-secret",
        "1",
        "private-key",
        "webhook",
        1,
        data_dir=tmp_path,
        pi_bin=str(executable),
        job_timeout_seconds=5,
    )
    settings.prepare()
    events = []

    async def run() -> None:
        result = await PiRunner(settings).run(
            "run-1", tmp_path, prompt, lambda event: _capture(events, event)
        )
        assert result.text == expected
        assert result.session_file == "/tmp/session.jsonl"

    asyncio.run(run())
    assert any(event.get("type") == "agent_settled" for event in events)


async def _capture(events: list[dict], event: dict) -> None:
    events.append(event)


class Writer:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    def write(self, payload: bytes) -> None:
        self.payloads.append(payload)

    async def drain(self) -> None:
        return None


class Process:
    def __init__(self, lines: bytes, *, running: bool = False) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(lines)
        self.stdout.feed_eof()
        self.stdin = Writer()
        self.stderr = None
        self.returncode = None if running else 0
        self.pid = 1

    async def wait(self) -> int:
        self.returncode = 0
        return 0

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9


def test_rpc_controls_and_protocol_errors(tmp_path: Path) -> None:
    runner = PiRunner(
        Settings("http://x", "s", "c", "cs", "1", "k", "w", 1, data_dir=tmp_path)
    )

    async def exercise() -> None:
        process = Process(b"", running=True)
        active = _ActiveProcess(cast(asyncio.subprocess.Process, process), asyncio.Lock())
        runner.active["run"] = active
        assert await runner.steer("run", "focus")
        assert await runner.follow_up("run", "more")
        assert await runner.abort("run")
        assert not await runner.abort("missing")
        assert len(process.stdin.payloads) == 3

        invalid = _ActiveProcess(
            cast(asyncio.subprocess.Process, Process(b"not-json\n")), asyncio.Lock()
        )
        with pytest.raises(RuntimeError, match="invalid JSONL"):
            await runner._read_run(invalid, lambda event: _capture([], event))

        early = _ActiveProcess(
            cast(asyncio.subprocess.Process, Process(b'{"type":"agent_start"}\n')),
            asyncio.Lock(),
        )
        with pytest.raises(RuntimeError, match="before run settled"):
            await runner._read_run(early, lambda event: _capture([], event))

        failed = _ActiveProcess(
            cast(
                asyncio.subprocess.Process,
                Process(
                    b'{"type":"agent_settled"}\n'
                    b'{"id":"final-text","type":"response","success":false,"error":"bad"}\n',
                    running=True,
                ),
            ),
            asyncio.Lock(),
        )
        with pytest.raises(RuntimeError, match="RPC command failed: bad"):
            await runner._read_run(failed, lambda event: _capture([], event))

    asyncio.run(exercise())
    environment = runner._environment()
    assert environment["GIT_OPTIONAL_LOCKS"] == "0"


def test_stderr_buffer_stop_and_timeout(tmp_path: Path) -> None:
    executable = tmp_path / "slow-pi"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n")
    executable.chmod(0o755)
    settings = Settings(
        "http://x",
        "s",
        "c",
        "cs",
        "1",
        "k",
        "w",
        1,
        data_dir=tmp_path,
        pi_bin=str(executable),
        job_timeout_seconds=1,
    )

    async def exercise() -> None:
        with pytest.raises(RuntimeError, match="timed out"):
            await PiRunner(settings).run("slow", tmp_path, "work", lambda event: _capture([], event))
        process = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", "printf error >&2", stderr=asyncio.subprocess.PIPE
        )
        assert await PiRunner._read_stderr(process) == "error"
        await process.wait()
        await PiRunner._stop(process)
        sleeper = await asyncio.create_subprocess_exec("sleep", "5")
        await PiRunner._stop(sleeper)
        assert sleeper.returncode is not None

    asyncio.run(exercise())
