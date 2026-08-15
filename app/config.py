from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import cast


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    if raw.lower() in {"1", "true", "yes"}:
        return True
    if raw.lower() in {"0", "false", "no"}:
        return False
    raise RuntimeError(f"{name} must be true or false")


def _env_int(name: str, default: str, *, minimum: int = 0) -> int:
    raw = os.getenv(name, default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < minimum:
        raise RuntimeError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    base_url: str
    secret_key: str
    github_client_id: str
    github_client_secret: str
    github_app_id: str
    github_private_key: str
    github_webhook_secret: str
    github_allowed_user_id: int
    data_dir: Path = Path("data")
    pi_bin: str = "pi"
    pi_run_as_user: str | None = None
    pi_model: str | None = None
    pi_thinking: str = "medium"
    job_timeout_seconds: int = 1800
    verification_timeout_seconds: int = 900
    worker_concurrency: int = 1
    require_cgroup_isolation: bool = False
    github_api_url: str = "https://api.github.com"
    github_bot_login: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        required = {
            "APP_BASE_URL": os.getenv("APP_BASE_URL"),
            "SECRET_KEY": os.getenv("SECRET_KEY"),
            "GITHUB_CLIENT_ID": os.getenv("GITHUB_CLIENT_ID"),
            "GITHUB_CLIENT_SECRET": os.getenv("GITHUB_CLIENT_SECRET"),
            "GITHUB_APP_ID": os.getenv("GITHUB_APP_ID"),
            "GITHUB_WEBHOOK_SECRET": os.getenv("GITHUB_WEBHOOK_SECRET"),
            "GITHUB_ALLOWED_USER_ID": os.getenv("GITHUB_ALLOWED_USER_ID"),
            "PI_RUN_AS_USER": os.getenv("PI_RUN_AS_USER"),
        }
        missing = [name for name, value in required.items() if not value]
        key = os.getenv("GITHUB_PRIVATE_KEY")
        key_path = os.getenv("GITHUB_PRIVATE_KEY_PATH")
        if not key and key_path:
            try:
                key = Path(key_path).read_text()
            except OSError as exc:
                raise RuntimeError(f"Cannot read GITHUB_PRIVATE_KEY_PATH: {exc}") from exc
        if not key:
            missing.append("GITHUB_PRIVATE_KEY or GITHUB_PRIVATE_KEY_PATH")
        if missing:
            raise RuntimeError(f"Missing required settings: {', '.join(missing)}")

        values = cast(dict[str, str], required)
        return cls(
            base_url=values["APP_BASE_URL"].rstrip("/"),
            secret_key=values["SECRET_KEY"],
            github_client_id=values["GITHUB_CLIENT_ID"],
            github_client_secret=values["GITHUB_CLIENT_SECRET"],
            github_app_id=values["GITHUB_APP_ID"],
            github_private_key=cast(str, key),
            github_webhook_secret=values["GITHUB_WEBHOOK_SECRET"],
            github_allowed_user_id=_env_int("GITHUB_ALLOWED_USER_ID", "", minimum=1),
            data_dir=Path(os.getenv("DATA_DIR", "data")).resolve(),
            pi_bin=os.getenv("PI_BIN", "pi"),
            pi_run_as_user=None if values["PI_RUN_AS_USER"] == "current" else values["PI_RUN_AS_USER"],
            pi_model=os.getenv("PI_MODEL") or None,
            pi_thinking=os.getenv("PI_THINKING", "medium"),
            job_timeout_seconds=_env_int("JOB_TIMEOUT_SECONDS", "1800", minimum=1),
            verification_timeout_seconds=_env_int("VERIFICATION_TIMEOUT_SECONDS", "900", minimum=1),
            worker_concurrency=_env_int("WORKER_CONCURRENCY", "1", minimum=1),
            require_cgroup_isolation=_env_bool("REQUIRE_CGROUP_ISOLATION", True),
            github_api_url=os.getenv("GITHUB_API_URL", "https://api.github.com").rstrip("/"),
            github_bot_login=os.getenv("GITHUB_BOT_LOGIN") or None,
        )

    def prepare(self) -> None:
        if self.worker_concurrency != 1:
            raise RuntimeError("Host-worktree mode requires WORKER_CONCURRENCY=1")
        paths = {
            self.data_dir: 0o2750,
            self.data_dir / "repos": 0o2750,
            self.data_dir / "worktrees": 0o2770,
            self.data_dir / "sessions": 0o2770,
        }
        for path, mode in paths.items():
            try:
                path.mkdir(parents=True, exist_ok=True, mode=mode)
                path.chmod(mode)
            except OSError as exc:
                raise RuntimeError(f"Cannot prepare data directory {path}: {exc}") from exc
