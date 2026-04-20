#!/usr/bin/env python3
"""Verify docs/modules.yaml covers every tracked file exactly once.

Exit 0 and print a summary line when coverage is perfect.
Exit 1 and print diagnostics otherwise.
"""
from __future__ import annotations

import fnmatch
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cai_lib.audit.modules import coverage_check, load_modules  # noqa: E402


def main() -> int:
    modules_path = REPO_ROOT / "docs" / "modules.yaml"

    # Load modules — skip doc-exists check here; we do it manually below
    # so we can collect all errors rather than raising on the first one.
    try:
        modules = load_modules(modules_path, check_doc_exists=False)
    except (ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}")
        return 1

    # Enumerate tracked files
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    files = [line for line in result.stdout.splitlines() if line]

    errors: list[str] = []

    # Check 1 & 2: every file must match exactly one module
    errors.extend(coverage_check(modules, files))

    # Check 3: each module's doc: path must exist on disk
    for mod in modules:
        doc_path = REPO_ROOT / mod.doc
        if not doc_path.exists():
            errors.append(
                f"module '{mod.name}': doc path does not exist: {mod.doc}"
            )

    # Check 4: each module's globs: must match at least one tracked file
    for mod in modules:
        if not any(
            fnmatch.fnmatch(f, g) for f in files for g in mod.globs
        ):
            errors.append(
                f"module '{mod.name}': no tracked files match its globs"
            )

    if errors:
        for err in errors:
            print(err)
        return 1

    print(f"{len(files)} files / {len(modules)} modules covered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
