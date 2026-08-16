from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


class ConfigError(ValueError):
    """Raised when required application settings are invalid."""


@dataclass(frozen=True, slots=True)
class Settings:
    root: Path
    app_env: str
    port: int
    database_path: Path
    repository_path: Path
    worktree_root: Path
    max_concurrent_agents: int
    agent_timeout_seconds: int
    pi_executable: str
    model: str
    reasoning_level: str
    pi_runner_user: str | None
    test_runner_user: str | None
    test_command: str
    github_repository: str
    github_token: str
    github_webhook_secret: str
    github_bot_login: str
    github_api_url: str
    dashboard_user: str
    dashboard_password: str

    @property
    def github_owner(self) -> str:
        return self.github_repository.split("/", 1)[0]

    @property
    def github_name(self) -> str:
        return self.github_repository.split("/", 1)[1]

    @classmethod
    def from_env(cls, *, root: Path | None = None) -> Settings:
        return cls.from_mapping(os.environ, root=root)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        root: Path | None = None,
    ) -> Settings:
        project_root = (root or Path.cwd()).resolve()
        errors: list[str] = []

        def required(name: str) -> str:
            value = values.get(name, "").strip()
            if not value:
                errors.append(f"{name} is required")
            return value

        def positive_int(name: str, default: str) -> int:
            raw = values.get(name, default).strip()
            try:
                value = int(raw)
            except ValueError:
                errors.append(f"{name} must be an integer")
                return 0
            if value < 1:
                errors.append(f"{name} must be greater than zero")
            return value

        def path(name: str, default: str) -> Path:
            value = Path(values.get(name, default)).expanduser()
            return value if value.is_absolute() else project_root / value

        repository = required("GITHUB_REPOSITORY")
        if repository.count("/") != 1 or any(
            not part for part in repository.split("/", 1)
        ):
            errors.append("GITHUB_REPOSITORY must be owner/name")

        port = positive_int("PORT", "8000")
        if port > 65535:
            errors.append("PORT must be at most 65535")

        app_env = values.get("APP_ENV", "development").strip() or "development"
        reasoning_level = required("REASONING_LEVEL")
        if reasoning_level and reasoning_level not in {
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        }:
            errors.append("REASONING_LEVEL is not supported")
        pi_runner_user = values.get("PI_RUNNER_USER", "").strip() or None
        test_runner_user = values.get("TEST_RUNNER_USER", "").strip() or None
        if app_env == "production":
            service_user = values.get("SERVICE_USER", "agent-pipeline").strip()
            if pi_runner_user is None:
                errors.append("PI_RUNNER_USER is required in production")
            if test_runner_user is None:
                errors.append("TEST_RUNNER_USER is required in production")
            if pi_runner_user == test_runner_user and pi_runner_user is not None:
                errors.append("PI_RUNNER_USER and TEST_RUNNER_USER must differ")
            if service_user in {pi_runner_user, test_runner_user}:
                errors.append("runner users must differ from SERVICE_USER")

        settings = cls(
            root=project_root,
            app_env=app_env,
            port=port,
            database_path=path("DATABASE_PATH", "var/agent-pipeline.db"),
            repository_path=path("REPOSITORY_PATH", "var/repository.git"),
            worktree_root=path("WORKTREE_ROOT", "var/worktrees"),
            max_concurrent_agents=positive_int("MAX_CONCURRENT_AGENTS", "1"),
            agent_timeout_seconds=positive_int("AGENT_TIMEOUT_SECONDS", "1800"),
            pi_executable=values.get("PI_EXECUTABLE", "pi").strip() or "pi",
            model=required("MODEL"),
            reasoning_level=reasoning_level,
            pi_runner_user=pi_runner_user,
            test_runner_user=test_runner_user,
            test_command=values.get("TEST_COMMAND", "./tests.sh").strip()
            or "./tests.sh",
            github_repository=repository,
            github_token=required("GITHUB_TOKEN"),
            github_webhook_secret=required("GITHUB_WEBHOOK_SECRET"),
            github_bot_login=required("GITHUB_BOT_LOGIN"),
            github_api_url=values.get(
                "GITHUB_API_URL", "https://api.github.com"
            ).rstrip("/"),
            dashboard_user=required("DASHBOARD_USER"),
            dashboard_password=required("DASHBOARD_PASSWORD"),
        )
        if errors:
            raise ConfigError("; ".join(errors))
        return settings
