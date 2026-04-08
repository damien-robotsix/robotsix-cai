"""Phase A entry point.

The smallest meaningful thing this backend can do: invoke `claude -p` once
with a trivial prompt and let the response flow to docker logs. This proves
the runtime envelope (Python, Node, claude-code, container auth) works
end-to-end before any analyzer logic is added.

No third-party Python dependencies — only stdlib `subprocess`.
"""

import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        ["claude", "-p", "Say hello in one short sentence."],
        check=False,
    )
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
