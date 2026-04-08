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

### Smoke test on a fresh server (one `docker run`, no clone)

The fastest way to verify the published image works on your server.
The image is published to Docker Hub on every push to `main`, so a single
`docker run` is enough — no repo clone, no `docker-compose.yml`.

**With an API key:**

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  robotsix/cai:latest
```

**With OAuth credentials from the host** (preferred — no static secret in
the container env). Requires `claude login` to have been run on the
server, OR `~/.claude/.credentials.json` copied over from another machine
where you've already logged in:

```bash
docker run --rm \
  -v ~/.claude/.credentials.json:/root/.claude/.credentials.json:ro \
  robotsix/cai:latest
```

Expected output:

```
Hello! How can I help you today?
```

(Or similar — the exact response varies.) If you see a non-empty greeting
and the container exits with code 0, the published image, your Docker
setup, and your auth all work end-to-end.

### Persistent setup with `docker-compose`

For repeatable runs and the eventual long-running daemon mode, use the
published `docker-compose.yml` from the repo:

```bash
git clone https://github.com/damien-robotsix/robotsix-cai.git
cd robotsix-cai
docker compose pull
```

Then pick one auth mode:

**Option A — API key in `.env`:**

```bash
cp .env.example .env
echo 'ANTHROPIC_API_KEY=sk-ant-...' > .env
docker compose up
```

**Option B — mounted OAuth credentials:**

Open `docker-compose.yml` and uncomment the `volumes:` block. Then:

```bash
docker compose up
```

### Build from source (local dev)

```bash
git clone https://github.com/damien-robotsix/robotsix-cai.git
cd robotsix-cai
docker compose build
docker compose up
```

Same auth-mode picks as the persistent setup.

## License

[MIT](LICENSE)
