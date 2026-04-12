---
title: Configuration
nav_order: 4
---

# Configuration

## Authentication

robotsix-cai requires two auth credentials: one for Claude (the AI model)
and one for the GitHub CLI. Both are stored in the `cai_home` Docker volume
and persist across container restarts.

### Claude — OAuth (recommended)

Run `docker compose up` once, then exec into the container and start the
Claude REPL:

```bash
docker compose exec cai claude
```

The REPL auto-prompts for OAuth login on first start. Complete the browser
flow, then exit the REPL with `/exit` or Ctrl-D. The credentials are saved
at `~/.claude/.credentials.json` inside the `cai_home` volume — no static
secret is stored in the environment.

### Claude — API key

Set `ANTHROPIC_API_KEY` in the container environment (via `.env` file or
`docker-compose.yml`):

```yaml
environment:
  ANTHROPIC_API_KEY: "sk-ant-..."
```

The installer will write a `.env` file (chmod 600) if you choose API key
mode.

### GitHub CLI

Inside the container:

```bash
docker compose exec cai gh auth login
```

Follow the prompts to authenticate with GitHub. Credentials are saved at
`~/.config/gh/` inside the `cai_home` volume.

---

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | *(unset)* | Claude API key (`sk-ant-...`). Alternative to OAuth. |
| `CAI_MERGE_CONFIDENCE_THRESHOLD` | `high` | Auto-merge gate. Options: `high`, `medium`, `low`. Merges only PRs where `cai-merge` reports at least this confidence level. |
| `CAI_TRANSCRIPT_WINDOW_DAYS` | `7` | How many days of Claude Code session transcripts to include in each analysis run. |
| `CAI_TRANSCRIPT_MAX_FILES` | `50` | Maximum number of transcript files to parse per run. Set to `0` for no limit. |
| `INSTALL_DIR` | `./robotsix-cai` | Directory the installer writes files into. Set before running `install.sh`. |
| `IMAGE_TAG` | `latest` | Docker image tag to pin. Use a `sha-<short>` tag for reproducibility. Set before running `install.sh`. |

---

## Docker volumes

Three named volumes hold all durable state. See [Home](index.md) for
mount paths and detailed descriptions.

| Volume | Mount path |
|--------|-----------|
| `cai_home` | `/home/cai` |
| `cai_agent_memory` | `/app/.claude/agent-memory` |
| `cai_logs` | `/var/log/cai` |

Inspect a volume from outside the container:

```bash
docker volume inspect cai_home
docker run --rm -v cai_home:/data alpine ls -R /data
```

Wipe all state (credentials, transcripts, memory, logs):

```bash
docker compose down --volumes
```

---

## Log files

Log files are written to `/var/log/cai/` (the `cai_logs` volume):

| File | Format | Purpose |
|------|--------|---------|
| `cai.log` | `key=value` per line | Per-invocation run record |
| `cai-cost.jsonl` | JSON Lines | Per-agent Claude API cost records |
| `cai-outcomes.jsonl` | JSON Lines | Fix/revise outcome records |
| `cai-active.json` | JSON object | Currently-running job (cleared on exit) |

---

## Agent memory

Each subagent accumulates durable memory in
`/app/.claude/agent-memory/<agent-name>/MEMORY.md` (persisted in the
`cai_agent_memory` volume). These files are read by agents at the start
of each run to avoid repeating rejected approaches.

Agents with active memory files include: `cai-fix`, `cai-code-audit`,
`cai-propose`, `cai-update-check`, and `cai-cost-optimize`.

You can inspect or clear an agent's memory by editing the file directly:

```bash
docker run --rm -v cai_agent_memory:/mem alpine cat /mem/cai-fix/MEMORY.md
```
