#!/usr/bin/env python3
"""Thin shim — real implementation lives in cai_lib/publish.py."""
import sys
from cai_lib.publish import *  # noqa: F401,F403

if __name__ == "__main__":
    sys.exit(main())  # noqa: F405
