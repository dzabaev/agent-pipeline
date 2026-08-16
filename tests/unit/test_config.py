import tempfile
import unittest
from pathlib import Path

from agent_pipeline import settings  # pyright: ignore[reportMissingImports]


VALID = {
    "APP_ENV": "development",
    "PORT": "8000",
    "DATABASE_PATH": "var/app.db",
    "REPOSITORY_PATH": "var/repository.git",
    "WORKTREE_ROOT": "var/worktrees",
    "MAX_CONCURRENT_AGENTS": "3",
    "AGENT_TIMEOUT_SECONDS": "60",
    "PI_EXECUTABLE": "pi",
    "PI_RUNNER_USER": "",
    "TEST_RUNNER_USER": "",
    "TEST_COMMAND": "./tests.sh",
    "GITHUB_REPOSITORY": "owner/repository",
    "GITHUB_TOKEN": "token",
    "GITHUB_WEBHOOK_SECRET": "secret",
    "GITHUB_BOT_LOGIN": "pipeline-bot",
    "GITHUB_API_URL": "https://api.github.test",
    "DASHBOARD_USER": "admin",
    "DASHBOARD_PASSWORD": "password",
}


class SettingsTests(unittest.TestCase):
    def test_loads_valid_settings_and_resolves_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            loaded = settings.Settings.from_mapping(VALID, root=root)

        self.assertEqual(loaded.max_concurrent_agents, 3)
        self.assertEqual(loaded.github_owner, "owner")
        self.assertEqual(loaded.github_name, "repository")
        self.assertEqual(loaded.database_path, root / "var/app.db")
        self.assertEqual(loaded.worktree_root, root / "var/worktrees")

    def test_rejects_missing_secrets_and_invalid_concurrency(self) -> None:
        values = VALID | {
            "GITHUB_TOKEN": "",
            "DASHBOARD_PASSWORD": "",
            "MAX_CONCURRENT_AGENTS": "0",
        }

        with self.assertRaises(settings.ConfigError) as raised:
            settings.Settings.from_mapping(values)

        message = str(raised.exception)
        self.assertIn("GITHUB_TOKEN", message)
        self.assertIn("DASHBOARD_PASSWORD", message)
        self.assertIn("MAX_CONCURRENT_AGENTS", message)

    def test_production_requires_separate_agent_and_test_users(self) -> None:
        with self.assertRaises(settings.ConfigError) as raised:
            settings.Settings.from_mapping(VALID | {"APP_ENV": "production"})

        self.assertIn("PI_RUNNER_USER", str(raised.exception))
        self.assertIn("TEST_RUNNER_USER", str(raised.exception))

    def test_production_rejects_service_user_as_runner(self) -> None:
        with self.assertRaises(settings.ConfigError) as raised:
            settings.Settings.from_mapping(
                VALID
                | {
                    "APP_ENV": "production",
                    "PI_RUNNER_USER": "agent-pipeline",
                    "TEST_RUNNER_USER": "agent-test",
                }
            )

        self.assertIn("SERVICE_USER", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
