import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BootstrapIntegrationTests(unittest.TestCase):
    def test_run_script_explains_missing_setup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "run.sh"
            shutil.copy2(ROOT / "run.sh", script)
            result = subprocess.run(
                [str(script)],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Run ./setup.sh first", result.stderr)


if __name__ == "__main__":
    unittest.main()
