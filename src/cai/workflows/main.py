from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cai.workflows.fsm import refine_files


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cai-refine",
        description="Refine a cai-issue JSON+MD pair in place. Prints the updated metadata as JSON.",
    )
    parser.add_argument("path", type=Path, help="Path to the issue <n>.json file.")
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="Repository root to expose to the explore agent for grep/read (default: cwd).",
    )
    args = parser.parse_args()

    new_meta = refine_files(args.path, repo_root=args.repo_root)
    json.dump(new_meta.model_dump(), sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
