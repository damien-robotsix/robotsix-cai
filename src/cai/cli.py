"""Default `cai` entry point: launch an interactive Claude Code session."""
from __future__ import annotations

import os
import sys


def main() -> None:
    os.execvp("claude", ["claude", "--dangerously-skip-permissions", *sys.argv[1:]])


if __name__ == "__main__":
    main()
