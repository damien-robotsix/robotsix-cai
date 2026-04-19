#!/usr/bin/env python3
"""Verify docs/modules.yaml covers every tracked file exactly once.

Exit 0 if coverage is perfect; exit 1 with errors printed to stderr
otherwise. Invoked manually by maintainers and optionally from CI.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cai_lib.audit.modules import coverage_check, load_modules  # noqa: E402


def main() -> int:
    modules = load_modules(
        REPO_ROOT / "docs" / "modules.yaml",
        check_doc_exists=True,
    )
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = [line for line in result.stdout.splitlines() if line]
    errors = coverage_check(modules, files)
    for err in errors:
        print(err, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
