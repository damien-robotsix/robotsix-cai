"""Lint check: ruff must report zero violations across the whole repo."""
import os
import shutil
import subprocess
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestLint(unittest.TestCase):

    @unittest.skipUnless(shutil.which("ruff"), "ruff not installed")
    def test_lint_passes(self):
        result = subprocess.run(
            ["ruff", "check", "."],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"ruff reported violations:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()
