from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .contracts import AgentRequest, AgentResult
from .process import terminate_process_group


class AgentExecutionError(RuntimeError):
    """Raised when an agent process fails or returns invalid output."""


class PiAgentRunner:
    def __init__(self, executable: str = "pi", runner_user: str | None = None) -> None:
        self.executable = executable
        self.runner_user = runner_user

    async def run(self, request: AgentRequest) -> AgentResult:
        if not request.worktree.is_dir():
            raise AgentExecutionError(f"worktree does not exist: {request.worktree}")

        command = [
            self.executable,
            "--mode",
            "json",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--tools",
            ",".join(request.tools),
            request.prompt,
        ]
        if self.runner_user:
            worktree_root = request.worktree.parent
            command = [
                "sudo",
                "-n",
                "-H",
                "-u",
                self.runner_user,
                "--",
                "/usr/bin/bwrap",
                "--die-with-parent",
                "--new-session",
                "--unshare-pid",
                "--unshare-ipc",
                "--unshare-uts",
                "--ro-bind",
                "/",
                "/",
                "--tmpfs",
                "/mnt",
                "--dir",
                "/mnt/agent-pipeline",
                "--dir",
                "/mnt/agent-pipeline/worktree",
                "--bind",
                str(request.worktree),
                "/mnt/agent-pipeline/worktree",
                "--tmpfs",
                str(worktree_root),
                "--dir",
                str(request.worktree),
                "--bind",
                "/mnt/agent-pipeline/worktree",
                str(request.worktree),
                "--tmpfs",
                "/tmp",
                "--tmpfs",
                "/var/tmp",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--chdir",
                str(request.worktree),
                "--",
                "/bin/sh",
                "-c",
                'umask 0007; exec "$@"',
                "agent-pipeline",
                *command,
            ]

        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=request.worktree,
            env=_sanitized_environment(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=request.timeout_seconds
            )
        except TimeoutError as error:
            await terminate_process_group(process)
            raise AgentExecutionError(
                f"Pi timed out after {request.timeout_seconds} seconds"
            ) from error
        except asyncio.CancelledError:
            await terminate_process_group(process)
            raise

        if process.returncode:
            detail = stderr.decode(errors="replace").strip()[-4000:]
            raise AgentExecutionError(
                f"Pi exited with code {process.returncode}: {detail}"
            )

        events = _parse_events(stdout)
        output = _final_text(events)
        if not output:
            raise AgentExecutionError("Pi returned no final assistant text")
        return AgentResult(output=output, events=tuple(events[-500:]))


def _sanitized_environment() -> dict[str, str]:
    blocked = {
        "DASHBOARD_PASSWORD",
        "GITHUB_TOKEN",
        "GITHUB_WEBHOOK_SECRET",
    }
    environment = {
        key: value for key, value in os.environ.items() if key not in blocked
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _parse_events(output: bytes) -> list[Mapping[str, Any]]:
    events: list[Mapping[str, Any]] = []
    for line_number, line in enumerate(output.decode(errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AgentExecutionError(
                f"Pi emitted invalid JSON on line {line_number}"
            ) from error
        if not isinstance(event, dict):
            raise AgentExecutionError(
                f"Pi emitted non-object JSON on line {line_number}"
            )
        events.append(event)
    return events


def _final_text(events: list[Mapping[str, Any]]) -> str:
    for event in reversed(events):
        if event.get("type") != "message_end":
            continue
        message = event.get("message")
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        text = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ).strip()
        if text:
            return text
    return ""
