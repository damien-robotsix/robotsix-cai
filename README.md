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

Each `docker compose up` now runs three things in order:

1. **Auth check** — `gh auth status` must succeed; the installer runs
   `gh auth login` once and persists credentials in a Docker volume.
2. **Smoke test** — a trivial `claude -p "say hello"` call that also
   seeds a transcript for the analyzer to read on the next run.
3. **Analyzer + publish** — parses prior transcripts with `parse.py`,
   asks `claude -p` to produce structured findings against the
   `prompts/backend-auto-improve.md` prompt, and publishes the
   findings as GitHub issues via `gh` (deduped by fingerprint).

See the [tracking issue](https://github.com/damien-robotsix/robotsix-cai/issues/1)
for what lands in later phases.

### Quick install (recommended)

The installer is a small bash script that asks a couple of questions and
writes a minimal `docker-compose.yml` configured for your auth setup. No
repo clone, no manual editing of compose files.

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

1. **Mount OAuth credentials** from `${HOME}/.claude/.credentials.json` —
   recommended if you've run `claude login` on this host. No static
   secret is stored in the container env.
2. **Anthropic API key** — paste an `sk-ant-...` key when prompted; it's
   written to a `.env` file (chmod 600).

Optional environment variables you can set before running the script:

- `INSTALL_DIR` — directory to install into (default: `./robotsix-cai`)
- `IMAGE_TAG`   — Docker image tag to pin (default: `latest`; you can
  pin a `sha-<short>` for reproducibility)

The installer then pulls the image and runs `gh auth login` inside the
container — pick **GitHub.com → HTTPS → Authenticate via web browser**
when prompted. gh prints a one-time code and a URL; paste the code into
the URL from any browser (handy on a headless server). The resulting
credentials are saved in a Docker volume named `cai_gh_config`, so
subsequent runs don't need to re-authenticate.

After the installer finishes:

```bash
cd robotsix-cai
docker compose up
```

Expected output per run: the smoke-test greeting, a structured findings
report from the analyzer (or `No findings.`), and — if anything is
actionable — new issues filed in this repository with labels
`auto-improve`, `auto-improve:raised`, and `category:<kind>`.

### One-shot smoke test (no install)

If you just want to verify the published image works without writing
any files at all, one `docker run` is enough.

**With OAuth credentials from the host:**

```bash
docker run --rm \
  -v ~/.claude/.credentials.json:/root/.claude/.credentials.json:ro \
  robotsix/cai:latest
```

**With an API key:**

```bash
docker run --rm \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  robotsix/cai:latest
```

The image at `docker.io/robotsix/cai:latest` is published from this repo
on every push to `main` (see
[`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml)).

### Build from source (local dev)

```bash
git clone https://github.com/damien-robotsix/robotsix-cai.git
cd robotsix-cai
docker compose build
docker compose up
```

The repo's `docker-compose.yml` defaults to API-key auth via `.env`. To
use mounted OAuth credentials instead, uncomment the relevant entry in
the `volumes:` block.

## Persistent data

The container uses two Docker named volumes:

- **`cai_transcripts`** (mounted at `/root/.claude/projects`) —
  claude-code writes one JSONL file per session under
  `/root/.claude/projects/<sanitized-cwd>/<session-id>.jsonl`; the
  volume keeps that data across restarts so future analyzer runs can
  read it.
- **`cai_gh_config`** (mounted at `/root/.config/gh`) — the `gh` CLI's
  credential store. Populated once by the installer's
  `gh auth login` step and reused on every subsequent run.

Inspect a volume from outside the container:

```bash
docker volume inspect cai_transcripts
docker run --rm -v cai_transcripts:/data alpine ls -R /data
```

Wipe everything (deletes transcripts and gh credentials — you'll need
to re-authenticate afterwards):

```bash
docker compose down --volumes        # if you used compose
docker volume rm cai_transcripts cai_gh_config   # standalone
```

## License

[MIT](LICENSE)
