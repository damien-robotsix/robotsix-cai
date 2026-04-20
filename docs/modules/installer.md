# installer

Container image, Compose orchestration, installer script, entry
point, and example configuration templates used to deploy
robotsix-cai as a long-lived service. Everything a maintainer
needs to stand up a fresh host lives in this module.

## Key entry points
- [`Dockerfile`](../../Dockerfile) — Python 3.12 + Node +
  `@anthropic-ai/claude-code` CLI image. The `claude-code` version
  pin here is what every subagent runs; `cai-update-check`
  proposes bumps.
- [`docker-compose.yml`](../../docker-compose.yml) — multi-service
  orchestration with named volumes; defines the long-lived worker
  service and optional audit host.
- [`entrypoint.sh`](../../entrypoint.sh) — Docker entrypoint.
  Templates the crontab, runs `cai cycle` once on startup, then
  execs `supercronic` to drive recurring tasks.
- [`install.sh`](../../install.sh) — interactive installer for
  end-users. Asks for repo, token, and workspace layout, then
  writes `.env` and (optionally) `workspaces.json`.
- [`.env.example`](../../.env.example) — template for required
  environment variables (`GITHUB_TOKEN`, `REPO`, SSH transport
  knobs, etc.).
- [`workspaces.json.example`](../../workspaces.json.example) —
  multi-workspace configuration template with per-repo cycle
  schedules.
- [`.gitignore`](../../.gitignore) — git ignore rules.

## Inter-module dependencies
- Runs **cli** — the entrypoint and crontab invoke `python cai.py
  cycle` / `python cai.py dispatch` / `python cai.py analyze`.
- Builds **workflows** — `docker-publish.yml` consumes the
  Dockerfile and publishes the image to Docker Hub on pushes to
  `main`.
- Mounts **transcripts** store — named volume holds the JSONL
  transcripts that `parse.py` and `transcript_sync.py` operate
  on.
- Consumes **config** — env vars defined in `.env` resolve into
  constants in `cai_lib/config.py`.
- No Python imports — this module is shell + YAML + Dockerfile.

## Operational notes
- **Image rebuilds.** Any Dockerfile change kicks the
  `docker-publish.yml` workflow on merge to `main`; the rebuilt
  image propagates to every worker on next `docker compose pull`.
  Verify locally before merging.
- **Crontab generation.** `entrypoint.sh` templates the crontab
  from env vars; a malformed schedule will silently skip runs
  rather than crash the container. Tail the supercronic logs if
  a job stops firing.
- **Claude Code pin.** The version of `@anthropic-ai/claude-code`
  in the Dockerfile is the single source of truth for the
  harness; `cai-update-check` raises findings when a newer
  version is available.
- **CI implications.** Container builds are not gated by unit
  tests but are validated by the Docker Hub publish workflow.
- **Cost sensitivity.** Low (infrastructure-only). Indirect
  impact via cron schedule frequency — each cycle drives
  expensive audit/handler agents.
