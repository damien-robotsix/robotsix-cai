# Configuration

## Environment Variables

| Variable | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | API authentication for headless `claude` invocations inside the container | Required |
| `CAI_ADMIN_LOGINS` | Comma-separated list of GitHub logins authorized to use the `human:solved` label to unblock stuck issues and PRs. Without this, the `human:solved` workflow is silently ignored and parked tasks remain unblocked. See `cai unblock` in the CLI reference for details. | _(optional; unblock workflow disabled if not set)_ |
| `CAI_MERGE_CONFIDENCE_THRESHOLD` | Minimum confidence level for `cai merge` auto-merge (`high`, `medium`, `disabled`) | `high` |
| `CAI_MERGE_MAX_DIFF_LEN` | Maximum character length for PR diffs passed to the merge agent. Diffs over this cap are packed in natural (alphabetical) file order and any files that don't fit are listed as omitted. | `200000` |

`CAI_MERGE_CONFIDENCE_THRESHOLD` controls how aggressively the merge agent promotes PRs:
- `high` — only auto-merge when `cai-merge` emits a `high` confidence verdict
- `medium` — also auto-merge `medium` confidence verdicts
- `disabled` — skip auto-merge entirely (useful during testing)

## Agent Schedules

Cron schedules are configurable via environment variables. Default values are set in `entrypoint.sh`; most are also explicitly configured in `docker-compose.yml`.

`CAI_CYCLE_SCHEDULE` drives the unified dispatcher: each tick runs restart-recover → `dispatch_oldest_actionable()`, which picks the oldest open issue or PR whose lifecycle state has a handler and runs the matching handler in `cai_lib/actions/`. A flock serializes overlapping runs. The planner confidence gate: HIGH auto-promotes to `:plan-approved`; MEDIUM with explicit anchor-based risk mitigation also auto-promotes; MEDIUM documentation-only plans (those touching only `docs/` files) also auto-promote; plans flagged `requires_human_review=true` (when cai-select chose a plan knowingly diverging from refined-issue preference) divert with a bespoke "Plan diverges from preference" message; MEDIUM / LOW / missing diverts to `:human-needed` with a pending marker and a comment explaining why the plan didn't reach HIGH confidence (e.g., unverified assumptions, ambiguous scope). An admin comment resumes it via `cai unblock`. `cai dispatch --issue N` / `cai dispatch --pr N` remains callable manually or from GitHub Actions for targeted retries. Verify and audit run on their own independent cron schedules (`CAI_VERIFY_SCHEDULE`, `CAI_AUDIT_SCHEDULE`).

| Variable | Default | Description |
|---|---|---|
| `CAI_CYCLE_SCHEDULE` | `0 * * * *` | Restart-recovery + dispatch one actionable issue/PR |
| `CAI_VERIFY_SCHEDULE` | `15 * * * *` | Label-state reconciliation (cmd_verify) — removes deprecated cai-managed labels from open issues, then keeps :pr-open / :merged / etc. consistent with actual GitHub state. |
| `CAI_ANALYZER_SCHEDULE` | `0 0 * * *` | Daily transcript analysis and issue raising |
| `CAI_AUDIT_SCHEDULE` | `0 */6 * * *` | Every 6 hours — queue/PR lifecycle audit |
| `CAI_CODE_AUDIT_SCHEDULE` | `0 3 * * 0` | Weekly (Sunday 03:00 UTC) — source tree audit |
| `CAI_PROPOSE_SCHEDULE` | `0 4 * * 0` | Weekly (Sunday 04:00 UTC) — creative proposals |
| `CAI_COST_OPTIMIZE_SCHEDULE` | `0 5 * * 0` | Weekly (Sunday 05:00 UTC) — cost-reduction analysis |
| `CAI_UPDATE_CHECK_SCHEDULE` | `0 4 * * 1` | Weekly (Monday 04:00 UTC) — Claude Code release check |
| `CAI_EXTERNAL_SCOUT_SCHEDULE` | `0 6 * * 1` | Weekly (Monday 06:00 UTC) — scout for open-source libraries to replace in-house plumbing |
| `CAI_HEALTH_REPORT_SCHEDULE` | `0 7 * * 1` | Weekly (Monday 07:00 UTC) — pipeline health report |
| `CAI_CHECK_WORKFLOWS_SCHEDULE` | `0 */6 * * *` | Every 6 hours — GitHub Actions workflow check |
| `CAI_AGENT_AUDIT_SCHEDULE` | `0 6 * * 0` | Weekly (Sunday 06:00 UTC) — agent audit |
| `CAI_WORKSPACES_CONFIG` | `/app/workspaces.json` | Path to a JSON file listing additional repositories to maintain (optional; see Multi-workspace section below) |

Schedule values use standard cron format: `minute hour day month weekday`. To disable a scheduled agent, set its variable to an empty string or a comment value.

## Transcript Analysis Variables

| Variable | Default | Description |
|---|---|---|
| `CAI_TRANSCRIPT_WINDOW_DAYS` | `7` | Only parse session transcripts from the last N days |
| `CAI_TRANSCRIPT_MAX_FILES` | `50` | Read at most N recent transcript files (0 = no limit) |

## Cross-host Transcript Sync

When you run cai for the same repository on multiple machines, each
container only sees the sessions that happened on its own host.
`cai analyze` and `cai confirm` then only reason about a local slice of
activity, missing signals from the rest of the fleet.

The transcript-sync feature addresses this by pushing each host's
transcripts to a central SSH server you own (any cheap VPS works — OVH,
Hetzner, a home lab box) and pulling the union back before
analyze/confirm run.

### Enabling

The easiest path is to re-run `install.sh` and answer **yes** to the
"Enable transcript sync?" prompt. The installer:

1. Prompts for the SSH destination (e.g. `cai@vps.example.com:/srv/cai-transcripts`).
2. Generates a dedicated `cai_transcript_key` (ed25519) in the install directory.
3. Prints the public key for you to add to the remote user's
   `~/.ssh/authorized_keys`.
4. Wires the key + `/etc/machine-id` bind mounts + sync env vars into
   the generated `docker-compose.yml`.

### Two transports: SSH vs local path

`CAI_TRANSCRIPT_SYNC_URL` supports two URL shapes and picks the transport
from the shape:

- **SSH** — `<user>@<host>:<absolute-path>` (contains `:`). The
  container rsyncs over SSH with the key at
  `CAI_TRANSCRIPT_SYNC_SSH_KEY`. Use this from any machine that is NOT
  hosting the transcript store itself.

- **Local path** — an absolute filesystem path with no `:` (e.g.
  `/srv/cai-transcripts`). The container rsyncs directly against a
  bind-mount of that path. Use this on the host that is ALSO the
  central store, so its own pushes/pulls don't SSH-loopback
  unnecessarily. The path must be bind-mounted into the container
  (the installer does this automatically when you pick local mode)
  and writable by UID 1000.

Both modes share the same server layout and same machine-id logic —
the only difference is whether the container takes the SSH path or the
loopback path.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `CAI_TRANSCRIPT_SYNC_URL` | _(unset → disabled)_ | Transport + destination. SSH form: `<user>@<host>:<absolute-path>`. Local form: plain absolute path with no colon. Feature is a no-op when unset. |
| `CAI_TRANSCRIPT_SYNC_SSH_KEY` | `/home/cai/.ssh/cai_transcript_key` | Path inside the container to the private key used for rsync-over-SSH. Ignored in local-path mode. |
| `CAI_TRANSCRIPT_SYNC_SCHEDULE` | `*/15 * * * *` | Cron expression for the push+pull job. Only appended to the crontab when `CAI_TRANSCRIPT_SYNC_URL` is set. |
| `CAI_MACHINE_ID` | _(from `/etc/machine-id`)_ | Stable per-host identifier used as the server bucket name. Defaults to the first 12 chars of the host's `/etc/machine-id` (bind-mounted into the container at `/etc/host-machine-id`). Set explicitly for human-readable bucket names (e.g. `laptop`, `ovh-box`). |

### Server layout

```
<CAI_TRANSCRIPT_SYNC_URL>/
  <repo-slug>/                 # e.g. damien-robotsix_robotsix-cai
    <machine-id>/              # from CAI_MACHINE_ID or /etc/machine-id
      <encoded-cwd>/
        <session-id>.jsonl
```

Each host pushes into its own `<machine-id>` bucket with
`rsync --delete`, so a machine's bucket always mirrors its current
7-day window. Analyzers pull the full `<repo-slug>` subtree — i.e. the
union of every machine's window — into
`/home/cai/.claude/projects-aggregate/` before parsing.

### Server-side cleanup

Age and size caps are enforced by `scripts/server-cleanup.sh` — copy it
to the server and add a cron entry:

```
30 3 * * * CAI_SERVER_MAX_AGE_DAYS=30 CAI_SERVER_MAX_SIZE_MB=2000 \
  /srv/cai-transcripts-cleanup.sh >> /var/log/cai-cleanup.log 2>&1
```

The script is self-contained, has a `--dry-run` mode via
`CAI_SERVER_DRY_RUN=1`, and documents all env vars at the top.

### Manual one-off sync

```
docker compose exec cai python /app/cai.py transcript-sync
```

Runs push + pull immediately, then exits. Useful right after you enable
the feature to populate the aggregate mirror without waiting for the
first cron tick.

## Multi-workspace Configuration

By default, `robotsix-cai` maintains only the primary repository (Lane 1). To extend the container to manage additional repositories, create a `workspaces.json` file listing the repos to maintain:

```json
[
  {
    "repo": "owner/repo-name",
    "cycle_schedule": "0 * * * *"
  },
  {
    "repo": "owner/another-repo",
    "cycle_schedule": "0 */6 * * *"
  }
]
```

**Field meanings:**

- **`repo`** _(required)_ — GitHub repository identifier in `owner/repo` format
- **`cycle_schedule`** _(optional)_ — cron schedule for this workspace's `cai.py cycle` runs (5-field format: `minute hour day month weekday`). If omitted, falls back to `CAI_CYCLE_SCHEDULE`. Each repo gets its own dispatcher cycle independent of the primary repository.

**Configuration:**

1. Create `workspaces.json` in your install directory (or elsewhere) with the repos you want to maintain
2. Set `CAI_WORKSPACES_CONFIG` to the file's path in your `docker-compose.yml` (default: `/app/workspaces.json`)
3. Restart the container: `docker compose up -d`

The entrypoint will:
- Parse the file and generate per-workspace cron lines appended to the generated crontab
- Run an initial `cai.py cycle` for each workspace on startup (alongside the primary repo's startup cycle)
- Schedule each workspace's cycle independently on its configured schedule

See `workspaces.json.example` in the repository root for a complete example.

## Settings File

`.claude/settings.json` configures Claude Code for both interactive and headless sessions.

Key fields:

- **`permissions.allow`** — tool allowlist rules. Headless (`claude -p`) sessions inherit these rules. The implement/revise/rebase agents run with a restricted allowlist (no `Bash`, no `git push`) enforced by per-agent `tools:` frontmatter.
- **`model`** — default model for interactive sessions (agents override this via their own `model:` frontmatter).
- **`env`** — environment variables injected into every Claude Code session.

Agent-level overrides live in `.claude/agents/<name>.md` YAML frontmatter (`tools:`, `model:`, `description:`).

## Paths and Directories

| Path | Purpose |
|---|---|
| `/home/cai/.claude/projects` | Transcript directory — Claude Code writes `.jsonl` session files here |
| `/home/cai/.claude/projects-aggregate` | Cross-host aggregate mirror (populated by `cai transcript-sync`; only present when `CAI_TRANSCRIPT_SYNC_URL` is set) |
| `/home/cai/.ssh/cai_transcript_key` | Private key used for transcript-sync SSH (bind-mounted read-only) |
| `/etc/host-machine-id` | Host's `/etc/machine-id` bind-mounted read-only to give transcript-sync a stable per-host bucket key |
| `/app/.claude/agent-memory` | Per-agent persistent memory files (checked into git) |
| `/var/log/cai/cai.log` | Structured run log (JSON lines, one entry per `cai` invocation) |
| `/var/log/cai/cai-cost.jsonl` | Per-invocation cost log (input/output tokens + USD) |
| `/var/log/cai/cai-outcomes.jsonl` | Fix/revise outcome log (issue number, verdict, PR URL) |
| `/var/log/cai/review-pr-patterns.jsonl` | Review-PR finding category log (used by `cai analyze`) |
