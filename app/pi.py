from __future__ import annotations

import asyncio
import json
import os
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .isolation import ProcessCgroup


EventHandler = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class PiResult:
    text: str
    session_file: str | None


@dataclass(slots=True)
class _ActiveProcess:
    process: asyncio.subprocess.Process
    lock: asyncio.Lock


class PiRunner:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.active: dict[str, _ActiveProcess] = {}

    async def run(
        self,
        run_id: str,
        cwd: Path,
        prompt: str,
        on_event: EventHandler,
        *,
        model: str | None = None,
        thinking: str | None = None,
        continuation_session: str | None = None,
    ) -> PiResult:
        args = [
            self.settings.pi_bin,
            "--mode",
            "rpc",
            "--session-dir",
            str(self.settings.data_dir / "sessions"),
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--tools",
            "read,bash,edit,write,grep,find,ls",
        ]
        if self.settings.pi_run_as_user:
            args = [
                "sudo",
                "-n",
                "-H",
                "-u",
                self.settings.pi_run_as_user,
                "--",
                "/bin/sh",
                "-c",
                'umask 0002; exec "$@"',
                "agent-pipeline",
                *args,
            ]
        selected_model = model or self.settings.pi_model
        if selected_model:
            args.extend(("--model", selected_model))
        args.extend(("--thinking", thinking or self.settings.pi_thinking))
        if continuation_session:
            args.extend(("--fork", continuation_session))

        cgroup = ProcessCgroup.create(
            run_id, "pi", required=self.settings.require_cgroup_isolation
        )
        args = cgroup.wrap(args)
        process = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._environment(),
            start_new_session=True,
            limit=1024 * 1024,
        )
        active = _ActiveProcess(process, asyncio.Lock())
        self.active[run_id] = active
        stderr_task = asyncio.create_task(self._read_stderr(process))
        try:
            await self._send(active, {"id": "initial", "type": "prompt", "message": prompt})
            return await asyncio.wait_for(
                self._read_run(active, on_event), timeout=self.settings.job_timeout_seconds
            )
        except TimeoutError as exc:
            await self._stop(process)
            raise RuntimeError("Pi run timed out") from exc
        finally:
            self.active.pop(run_id, None)
            await self._stop(process)
            cgroup_error: RuntimeError | None = None
            try:
                await cgroup.kill()
            except RuntimeError as exc:
                cgroup_error = exc
            try:
                stderr = await asyncio.wait_for(stderr_task, timeout=5)
            except TimeoutError:
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
                stderr = ""
            if cgroup_error:
                raise cgroup_error
            if process.returncode not in (0, -signal.SIGTERM, -signal.SIGKILL) and stderr:
                await on_event({"type": "pi_stderr", "text": stderr})

    async def steer(self, run_id: str, message: str) -> bool:
        active = self.active.get(run_id)
        if active is None:
            return False
        await self._send(active, {"type": "steer", "message": message})
        return True

    async def follow_up(self, run_id: str, message: str) -> bool:
        active = self.active.get(run_id)
        if active is None:
            return False
        await self._send(active, {"type": "follow_up", "message": message})
        return True

    async def abort(self, run_id: str) -> bool:
        active = self.active.get(run_id)
        if active is None:
            return False
        await self._send(active, {"type": "abort"})
        return True

    async def _read_run(self, active: _ActiveProcess, on_event: EventHandler) -> PiResult:
        process = active.process
        if process.stdout is None:
            raise RuntimeError("Pi stdout unavailable")
        settled = False
        final_text: str | None = None
        session_file: str | None = None
        while True:
            line = await process.stdout.readline()
            if not line:
                if process.returncode is None:
                    await process.wait()
                if not settled:
                    raise RuntimeError("Pi exited before run settled")
                break
            try:
                event = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("Pi emitted invalid JSONL") from exc
            if not isinstance(event, dict):
                continue
            await on_event(event)
            if event.get("type") == "agent_settled" and not settled:
                settled = True
                await self._send(active, {"id": "final-text", "type": "get_last_assistant_text"})
                await self._send(active, {"id": "final-state", "type": "get_state"})
                continue
            if event.get("type") != "response":
                continue
            response_id = event.get("id")
            if response_id in {"initial", "final-text", "final-state"} and not event.get("success"):
                raise RuntimeError(f"Pi RPC command failed: {event.get('error') or response_id}")
            if response_id == "final-text":
                data = event.get("data")
                if isinstance(data, dict):
                    text = data.get("text")
                    if text is None or isinstance(text, str):
                        final_text = text or ""
            elif response_id == "final-state":
                data = event.get("data")
                if isinstance(data, dict) and isinstance(data.get("sessionFile"), str):
                    session_file = data["sessionFile"]
            if settled and final_text is not None and session_file is not None:
                break
        if final_text is None:
            raise RuntimeError("Pi completed without assistant output")
        return PiResult(final_text, session_file)

    @staticmethod
    async def _send(active: _ActiveProcess, command: dict[str, Any]) -> None:
        if active.process.stdin is None or active.process.returncode is not None:
            raise RuntimeError("Pi process is not running")
        payload = json.dumps(command, separators=(",", ":")).encode() + b"\n"
        async with active.lock:
            active.process.stdin.write(payload)
            await active.process.stdin.drain()

    @staticmethod
    async def _read_stderr(process: asyncio.subprocess.Process) -> str:
        if process.stderr is None:
            return ""
        buffered = bytearray()
        while chunk := await process.stderr.read(8192):
            buffered.extend(chunk)
            if len(buffered) > 64 * 1024:
                del buffered[: len(buffered) - 64 * 1024]
        return buffered.decode(errors="replace")

    @staticmethod
    async def _stop(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except TimeoutError:
            process.kill()
            await process.wait()

    @staticmethod
    def _environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "HOME",
            "LANG",
            "LC_ALL",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "ANTHROPIC_API_KEY",
            "OPENAI_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "OPENROUTER_API_KEY",
            "AZURE_OPENAI_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "PI_CODING_AGENT_DIR",
        }
        environment = {name: value for name, value in os.environ.items() if name in allowed}
        environment["GIT_OPTIONAL_LOCKS"] = "0"
        return environment
