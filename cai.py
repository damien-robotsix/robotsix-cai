"""Phase D entry point — subcommand dispatcher.

Subcommands:

    python cai.py init      Smoke-test claude -p only if the transcript
                            volume has no prior sessions. Used to seed
                            the self-improvement loop on a fresh
                            install; a no-op once transcripts exist.

    python cai.py analyze   Parse prior transcripts with parse.py, pipe
                            the combined analyzer prompt through
                            claude -p, and publish findings via
                            publish.py. Safe to call repeatedly — this
                            is what supercronic invokes on its cron
                            tick.

The container runs `entrypoint.sh`, which executes `init` and `analyze`
once synchronously at startup (so `docker compose up -d` produces
immediate logs), then hands off to supercronic. Future task types
(daily report, workflow-triggered actions, etc.) add themselves as
additional subcommands here and additional lines in the crontab.

The gh auth check is intentionally done once per subcommand invocation.
Each cron tick is a fresh process, and we want a clear error message in
docker logs if credentials ever disappear from the cai_gh_config volume.

No third-party Python dependencies — only stdlib.
"""

import argparse
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


def _transcript_dir_is_empty() -> bool:
    if not TRANSCRIPT_DIR.exists():
        return True
    return not any(TRANSCRIPT_DIR.glob("*.jsonl"))


def cmd_init() -> int:
    """Seed the loop with a smoke test, only if nothing exists yet."""
    if not _transcript_dir_is_empty():
        print("[cai init] transcripts already present; skipping smoke test", flush=True)
        return 0

    print("[cai init] no prior transcripts; running smoke test to seed loop", flush=True)
    result = subprocess.run(
        ["claude", "-p", SMOKE_PROMPT],
        check=False,
    )
    if result.returncode != 0:
        print(f"[cai init] smoke test failed (exit {result.returncode})", flush=True)
    return result.returncode


def cmd_analyze() -> int:
    """Parse prior transcripts, ask claude to analyze, publish findings."""
    print("[cai analyze] running self-analyzer", flush=True)

    if not TRANSCRIPT_DIR.exists():
        print(
            f"[cai analyze] no transcript dir at {TRANSCRIPT_DIR}; nothing to analyze",
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
            f"[cai analyze] parse.py failed (exit {parsed.returncode}):\n{parsed.stderr}",
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
            f"[cai analyze] claude -p failed (exit {analyzer.returncode}):\n"
            f"{analyzer.stderr}",
            flush=True,
        )
        return analyzer.returncode

    print("[cai analyze] publishing findings", flush=True)
    published = subprocess.run(
        ["python", str(PUBLISH_SCRIPT)],
        input=analyzer.stdout,
        text=True,
        check=False,
    )
    return published.returncode


# Map subcommand name -> callable. Future phases add entries here and a
# matching crontab line in entrypoint.sh.
COMMANDS = {
    "init": cmd_init,
    "analyze": cmd_analyze,
}


def main() -> int:
    parser = argparse.ArgumentParser(prog="cai")
    parser.add_argument("command", choices=sorted(COMMANDS.keys()))
    args = parser.parse_args()

    auth_rc = check_gh_auth()
    if auth_rc != 0:
        return auth_rc

    return COMMANDS[args.command]()


if __name__ == "__main__":
    sys.exit(main())
