---
title: Home
---

# robotsix-cai

**Claude Auto Improve** — a self-tuning backend that analyzes its own
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
runtime sessions and proposes improvements to itself via pull requests.

## Status

**Pre-alpha.** v0 (Lane 1 — self-improvement only) is under active
development. This documentation will grow as the project matures.

- [v0 development tracker](https://github.com/damien-robotsix/robotsix-cai/issues/1)
- [Architectural design RFC](https://github.com/damien-robotsix/claude-auto-tune-hub/issues/51)
- [GitHub repository](https://github.com/damien-robotsix/robotsix-cai)

## What it does

robotsix-cai runs as a long-lived service in a Docker container. On a
schedule, it:

1. Reads transcripts of its own recent Claude Code runtime sessions
2. Analyzes them with a Claude prompt to find bugs, inefficiencies, and
   prompt gaps in its own code
3. Files issues (and, where confident, opens pull requests) in this
   repository
4. After human review and merge, the deploy pipeline rolls out the
   improvement
5. The next run uses the improved code, closing the loop

This is **Lane 1** of a two-lane design described in the RFC. Lane 2
(analyzing other workspaces' Claude Code sessions) is deferred to a
later milestone.

## Quick start

At Phase A the container is a single-shot smoke test: it invokes
`claude -p "Say hello in one short sentence."` and prints the response
to the docker logs. Real analyzer behavior lands in later phases.

### Quick install (recommended)

The installer is a small bash script that asks a couple of questions
and writes a minimal `docker-compose.yml` configured for your auth
setup. No repo clone, no manual editing of compose files.

```bash
wget https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh
less install.sh    # review before running
bash install.sh
```

You can also pipe it (skips the review step):

```bash
wget -qO- https://raw.githubusercontent.com/damien-robotsix/robotsix-cai/main/install.sh | bash
```

The installer asks for the **auth mode**:

1. **In-container OAuth login** — recommended. The installer opens
   the claude REPL inside the container automatically; the REPL
   auto-prompts for OAuth login on first start. Complete the
   browser flow, exit the REPL gracefully (`/exit` or Ctrl-D), and
   the credentials persist in the `cai_home` named volume. No
   static secret is stored in the container env, and no host file
   dependency.
2. **Anthropic API key** — paste an `sk-ant-...` key when prompted;
   it's written to a `.env` file (chmod 600).

Optional environment variables you can set before running the script:

- `INSTALL_DIR` — directory to install into (default: `./robotsix-cai`)
- `IMAGE_TAG`   — Docker image tag to pin (default: `latest`; you can
  pin a `sha-<short>` for reproducibility)

After the installer finishes, follow the printed next steps:

```bash
cd robotsix-cai
docker compose pull
docker compose up
```

Expected output: a single greeting line (`Hello! How can I help you
today?` or similar) and the container exits with code 0.

### One-shot smoke test (no install)

If you just want to verify the published image works without writing
any files at all, one `docker run` is enough.

**With OAuth credentials from the host:**

```bash
docker run --rm \
  -v ~/.claude/.credentials.json:/home/cai/.claude/.credentials.json \
  robotsix/cai:latest
```

(The mount is read-write on purpose — claude-code refreshes the OAuth
access token in place when it expires. A `:ro` mount blocks the
refresh and 401s after the token's lifetime is up.)

**With an API key:**

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  robotsix/cai:latest
```

### Build from source (local dev)

```bash
git clone https://github.com/damien-robotsix/robotsix-cai.git
cd robotsix-cai
docker compose build
docker compose up
```

## Persistent data

The container uses two Docker named volumes:

- **`cai_home`** (mounted at `/home/cai`) — the cai user's entire
  home directory. Holds Claude OAuth credentials
  (`~/.claude/.credentials.json`), Claude Code's runtime config
  (`~/.claude.json` — a sibling file outside the `.claude/`
  directory), session transcripts under `~/.claude/projects/`, the
  gh CLI credential store at `~/.config/gh/`, and any other
  claude-code or gh state under the user's home. One volume for
  all user state.
- **`cai_agent_memory`** (mounted at `/app/.claude/agent-memory`) —
  per-agent durable memory accumulated by the declarative subagents.

The container runs as the non-root `cai` user (uid 1000) — see
Dockerfile for the rationale.

Inspect a volume from outside the container:

```bash
docker volume inspect cai_home
docker run --rm -v cai_home:/data alpine ls -R /data
```

Wipe everything (deletes credentials, transcripts, gh config, and
per-agent memory — re-running `install.sh` is the easiest way):

```bash
docker compose down --volumes        # if you used compose
docker volume rm cai_home cai_agent_memory cai_logs   # standalone
```

## License

[MIT](https://github.com/damien-robotsix/robotsix-cai/blob/main/LICENSE)
