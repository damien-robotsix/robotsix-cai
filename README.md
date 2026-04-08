# robotsix-cai

**Claude Auto Improve** — a self-tuning backend that analyzes its own
[Claude Code](https://docs.claude.com/en/docs/claude-code/overview)
runtime sessions and proposes improvements to itself via pull requests.

## Status

Pre-alpha. v0 (Lane 1 — self-improvement only) is under active development.
See the [v0 tracking issue](https://github.com/damien-robotsix/robotsix-cai/issues/1)
for current progress.

The architectural design lives in
[damien-robotsix/claude-auto-tune-hub#51](https://github.com/damien-robotsix/claude-auto-tune-hub/issues/51).

## What it does

`robotsix-cai` runs as a long-lived service in a Docker container. On a
schedule, it:

1. Reads transcripts of its own recent Claude Code runtime sessions
2. Runs an analyzer prompt against them to find bugs, inefficiencies, and
   prompt gaps in its own code and prompts
3. Files issues (and, where confident, opens pull requests) in this
   repository
4. After human review and merge, the deploy pipeline rolls out the
   improvement
5. The next run uses the improved code, closing the loop

This is **Lane 1** of the two-lane design described in the RFC. Lane 2
(analyzing other workspaces' Claude Code sessions) is deferred to a later
milestone.

## Two-lane design

| | Lane 1 (this v0) | Lane 2 (deferred) |
|---|---|---|
| **Input** | The backend's own runtime sessions | Other workspaces' Claude Code sessions |
| **Trigger** | Self-recorded transcripts | OIDC-authenticated `POST /ingest` from workspace CI |
| **Target** | Issues and PRs in this repository | Issues and PRs in workspace repos |
| **Status** | In development | Planned |

## Quick start

At Phase A the container is a single-shot smoke test: it invokes
`claude -p "Say hello in one short sentence."` and prints the response to
the docker logs. Real analyzer behavior lands in later phases (see the
[tracking issue](https://github.com/damien-robotsix/robotsix-cai/issues/1)).

### Run the published image (server-style)

```bash
docker compose pull
docker compose up
```

The image at `docker.io/robotsix/cai:latest` is published from this repo on
every push to `main` (see [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)).

### Build and run from source (local dev)

```bash
git clone git@github.com:damien-robotsix/robotsix-cai.git
cd robotsix-cai
docker compose build
docker compose up
```

### Authentication — pick one

`claude -p` accepts either an API key in the environment **or** a mounted
OAuth credentials file from the host. Pick whichever is more convenient.

**Option A — API key in `.env`:**

```bash
cp .env.example .env
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
docker compose up
```

**Option B — mounted OAuth credentials (preferred for self-hosted):**

If you've already run `claude login` interactively on the host, the file
`~/.claude/.credentials.json` exists and `docker-compose.yml` can mount it
into the container read-only. Open `docker-compose.yml` and uncomment the
`volumes:` block. With this mode, no `ANTHROPIC_API_KEY` is needed and no
static secret is held in the container's environment.

### Expected output

```
Hello! How can I help you today?
```

(Or similar — the exact response varies. Any non-empty Claude response in
the logs means the runtime envelope is working.)

## License

[MIT](LICENSE)
