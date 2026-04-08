"""Phase C.2 entry point — smoke test, self-analyzer, publish findings.

Each `docker compose up` does three things, in order:

1. **Auth check.** Verifies `gh auth status` succeeds. The installer
   runs `gh auth login` once and persists credentials in a Docker
   volume; if that's been skipped or wiped, we fail fast with a clear
   pointer back to the install step.

2. **Smoke test.** A trivial "say hello" prompt. Proves the runtime
   envelope (Python, Node, claude-code, container auth) is healthy
   and — importantly — produces a fresh JSONL transcript under
   `/root/.claude/projects/-app/`. That transcript becomes input for
   the analyzer on the *next* run, which seeds Lane 1's recursive
   self-improvement loop.

3. **Analyzer + publish.** Runs `parse.py` against the transcript
   directory, combines the parsed summary with the prompt at
   `prompts/backend-auto-improve.md`, pipes it through `claude -p` to
   produce structured findings, then pipes those findings into
   `publish.py` to create GitHub issues (deduped by fingerprint).

No third-party Python dependencies — only stdlib.
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
PUBLISH_SCRIPT = Path("/app/publish.py")
ANALYZER_PROMPT = Path("/app/prompts/backend-auto-improve.md")


def check_gh_auth() -> int:
    """Fail fast if `gh` is not authenticated."""
    result = subprocess.run(
        ["gh", "auth", "status"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[cai] ERROR: gh is not authenticated in this container.", file=sys.stderr)
        print("       Credentials are expected in the cai_gh_config volume.", file=sys.stderr)
        print("       Run the installer's login step, or do it manually:", file=sys.stderr)
        print("         docker compose run --rm cai gh auth login", file=sys.stderr)
        print(file=sys.stderr)
        print(result.stderr.strip() or result.stdout.strip(), file=sys.stderr)
        return 1
    return 0


def run_smoke_test() -> int:
    """Run the trivial 'say hello' prompt; let output flow to logs."""
    print("[cai] running smoke test", flush=True)
    result = subprocess.run(
        ["claude", "-p", SMOKE_PROMPT],
        check=False,
    )
    return result.returncode


def run_analyzer_and_publish() -> int:
    """Parse prior transcripts, ask claude to analyze, publish findings."""
    print("[cai] running self-analyzer", flush=True)

    if not TRANSCRIPT_DIR.exists():
        print(
            f"[cai] no transcript dir at {TRANSCRIPT_DIR}; nothing to analyze",
            flush=True,
        )
        return 0

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

    # Capture the analyzer output so we can both print it to logs and
    # pipe it into publish.py.
    analyzer = subprocess.run(
        ["claude", "-p"],
        input=full_prompt,
        text=True,
        check=False,
        capture_output=True,
    )
    print(analyzer.stdout, flush=True)
    if analyzer.returncode != 0:
        print(
            f"[cai] analyzer claude -p failed (exit {analyzer.returncode}):\n"
            f"{analyzer.stderr}",
            flush=True,
        )
        return analyzer.returncode

    print("[cai] publishing findings to GitHub", flush=True)
    published = subprocess.run(
        ["python", str(PUBLISH_SCRIPT)],
        input=analyzer.stdout,
        text=True,
        check=False,
    )
    return published.returncode


def main() -> int:
    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    smoke_rc = run_smoke_test()
    if smoke_rc != 0:
        print(f"[cai] smoke test failed (exit {smoke_rc})", flush=True)
        return smoke_rc

    return run_analyzer_and_publish()


if __name__ == "__main__":
    sys.exit(main())
