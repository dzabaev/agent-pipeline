import os
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class BootstrapTests(unittest.TestCase):
    def test_required_scripts_are_executable(self) -> None:
        for name in ("setup.sh", "run.sh", "tests.sh"):
            path = ROOT / name
            self.assertTrue(path.is_file(), name)
            self.assertTrue(os.access(path, os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
