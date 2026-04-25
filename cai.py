#!/usr/bin/env python3
"""Thin entry point. Launches an interactive Claude Code session.

Subcommands will land here as the rewrite progresses.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.execvp("claude", ["claude", "--dangerously-skip-permissions", *sys.argv[1:]])


if __name__ == "__main__":
    main()
