from pathlib import Path

import pytest

from app.config import Settings, _env_bool, _env_int


REQUIRED = {
    "APP_BASE_URL": "https://example.test/",
    "SECRET_KEY": "secret",
    "GITHUB_CLIENT_ID": "client",
    "GITHUB_CLIENT_SECRET": "client-secret",
    "GITHUB_APP_ID": "1",
    "GITHUB_PRIVATE_KEY": "private-key",
    "GITHUB_WEBHOOK_SECRET": "webhook",
    "GITHUB_ALLOWED_USER_ID": "42",
    "PI_RUN_AS_USER": "current",
}


def test_environment_parsing_and_validation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in REQUIRED:
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="Missing required settings"):
        Settings.from_env()

    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("REQUIRE_CGROUP_ISOLATION", "no")
    monkeypatch.setenv("WORKER_CONCURRENCY", "1")
    settings = Settings.from_env()
    assert settings.base_url == "https://example.test"
    assert settings.github_allowed_user_id == 42
    assert settings.pi_run_as_user is None
    assert settings.data_dir == (tmp_path / "data").resolve()
    assert not settings.require_cgroup_isolation

    assert _env_bool("UNSET_BOOL", True)
    monkeypatch.setenv("BAD_BOOL", "sometimes")
    with pytest.raises(RuntimeError, match="must be true or false"):
        _env_bool("BAD_BOOL", False)
    monkeypatch.setenv("BAD_INT", "many")
    with pytest.raises(RuntimeError, match="must be an integer"):
        _env_int("BAD_INT", "1")
    monkeypatch.setenv("BAD_INT", "0")
    with pytest.raises(RuntimeError, match="at least 1"):
        _env_int("BAD_INT", "1", minimum=1)


def test_private_key_file_and_prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_PRIVATE_KEY")
    key = tmp_path / "app.pem"
    key.write_text("key-from-file")
    monkeypatch.setenv("GITHUB_PRIVATE_KEY_PATH", str(key))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PI_RUN_AS_USER", "worker")
    settings = Settings.from_env()
    assert settings.github_private_key == "key-from-file"
    assert settings.pi_run_as_user == "worker"
    settings.prepare()
    assert (settings.data_dir / "sessions").is_dir()

    bad = Settings(**{**settings.__dict__}) if hasattr(settings, "__dict__") else Settings(
        settings.base_url,
        settings.secret_key,
        settings.github_client_id,
        settings.github_client_secret,
        settings.github_app_id,
        settings.github_private_key,
        settings.github_webhook_secret,
        settings.github_allowed_user_id,
        data_dir=tmp_path / "other",
        worker_concurrency=2,
    )
    with pytest.raises(RuntimeError, match="WORKER_CONCURRENCY=1"):
        bad.prepare()

    key.unlink()
    with pytest.raises(RuntimeError, match="Cannot read GITHUB_PRIVATE_KEY_PATH"):
        Settings.from_env()


def test_prepare_reports_unusable_data_path(tmp_path: Path) -> None:
    path = tmp_path / "file"
    path.write_text("not a directory")
    settings = Settings("http://x", "s", "c", "cs", "1", "k", "w", 1, data_dir=path)
    with pytest.raises(RuntimeError, match="Cannot prepare data directory"):
        settings.prepare()
