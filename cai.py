"""Phase C.1 entry point — smoke test + self-analyzer.

Each `docker compose up` runs two `claude -p` calls in sequence:

1. **Smoke test.** A trivial "say hello" prompt. Proves the runtime
   envelope (Python, Node, claude-code, container auth) is healthy and —
   importantly — produces a fresh JSONL transcript under
   `/root/.claude/projects/-app/`. That transcript becomes input for the
   analyzer on the *next* run, which is how Lane 1's recursive
   self-improvement loop is seeded on first launch.

2. **Analyzer.** Runs `parse.py` against the transcript directory to
   extract a deterministic activity summary, combines that summary with
   the prompt at `prompts/backend-auto-improve.md`, and pipes the
   combined prompt into a second `claude -p` invocation. The model's
   findings are written to docker logs. Phase C.1 stops here — Phase
   C.2 will turn those findings into GitHub issues.

No third-party Python dependencies — only stdlib `subprocess`, `pathlib`.
"""

import subprocess
import sys
from pathlib import Path


SMOKE_PROMPT = "Say hello in one short sentence."

# Where claude-code writes session transcripts when invoked from /app
# inside the container. The path encodes the cwd: `/app` -> `-app`.
TRANSCRIPT_DIR = Path("/root/.claude/projects/-app")

# Files baked into the image alongside cai.py.
PARSE_SCRIPT = Path("/app/parse.py")
ANALYZER_PROMPT = Path("/app/prompts/backend-auto-improve.md")


def run_smoke_test() -> int:
    """Run the trivial 'say hello' prompt; let output flow to logs."""
    print("[cai] running smoke test", flush=True)
    result = subprocess.run(
        ["claude", "-p", SMOKE_PROMPT],
        check=False,
    )
    return result.returncode


def run_analyzer() -> int:
    """Parse prior transcripts and ask claude to analyze them."""
    print("[cai] running self-analyzer", flush=True)

    if not TRANSCRIPT_DIR.exists():
        print(
            f"[cai] no transcript dir at {TRANSCRIPT_DIR}; nothing to analyze",
            flush=True,
        )
        return 0

    # Parse transcripts deterministically. parse.py emits JSON to stdout.
    parsed = subprocess.run(
        ["python", str(PARSE_SCRIPT), str(TRANSCRIPT_DIR)],
        check=False,
        capture_output=True,
        text=True,
    )
    if parsed.returncode != 0:
        print(
            f"[cai] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
            flush=True,
        )
        return parsed.returncode

    parsed_signals = parsed.stdout.strip()
    prompt_text = ANALYZER_PROMPT.read_text()

    full_prompt = (
        f"{prompt_text}\n\n"
        "## Parsed signals\n\n"
        "```json\n"
        f"{parsed_signals}\n"
        "```\n"
    )

    result = subprocess.run(
        ["claude", "-p"],
        input=full_prompt,
        text=True,
        check=False,
    )
    return result.returncode


def main() -> int:
    smoke_rc = run_smoke_test()
    if smoke_rc != 0:
        print(f"[cai] smoke test failed (exit {smoke_rc})", flush=True)
        return smoke_rc

    return run_analyzer()


if __name__ == "__main__":
    sys.exit(main())
