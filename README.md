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

The container is long-lived. It runs as a **scheduler**
([supercronic](https://github.com/aptible/supercronic) as PID 1) that
fires three independent tasks on configurable cron schedules.
`cai.py` is a subcommand dispatcher so each task is its own
subprocess with no shared state.

| Subcommand | Default schedule | What it does |
|---|---|---|
| `cai.py analyze` | `0 0 * * *` (daily 00:00 UTC) | Parses transcripts, asks claude to produce structured findings, publishes them as issues with fingerprint dedup |
| `cai.py fix` | `15 * * * *` (hourly :15) | Picks the oldest eligible issue, runs a 3-phase pipeline (plan → select → implement): 3 planning agents propose fixes in parallel, a selection agent picks the best plan, then the cai-fix implementation agent executes it and opens a PR — see lifecycle below |
| `cai.py revise` | `30 * * * *` (hourly :30) | Watches `:pr-open` PRs for new comments and iterates on the same branch via force-push; also auto-rebases unmergeable PRs onto current main |
| `cai.py verify` | `45 * * * *` (hourly :45) | Mechanical, no LLM. Walks `auto-improve:pr-open` issues and updates labels based on PR merge state; also recovers issues whose `:pr-open` label was lost |
| `cai.py audit` | `0 */6 * * *` (every 6 hours) | Queue/PR consistency audit — rolls back stale `:in-progress` and `:no-action` issues, flags stale `:merged` issues for human review, deletes remote branches for merged/closed PRs, flags duplicates, stuck loops, and label corruption as `audit:raised` issues (Sonnet) |
| `cai.py review-pr` | `20 * * * *` (hourly :20) | Pre-merge consistency review of open PRs — posts ripple-effect findings as PR comments so the revise subagent can act on them |
| `cai.py merge` | `35 * * * *` (hourly :35) | Confidence-gated auto-merge — evaluates each bot PR against its linked issue, posts a verdict, and merges when confidence meets the threshold |
| `cai.py code-audit` | `0 3 * * 0` (weekly Sunday 03:00 UTC) | Source-code consistency audit — clones the repo read-only, runs a Sonnet agent to flag cross-file inconsistencies, dead code, missing references, duplicated logic, hardcoded drift, config mismatches, and registration mismatches; publishes findings as `code-audit` namespace issues |
| `cai.py confirm` | `0 2 * * *` (daily 02:00 UTC) | Re-analyzes the recent transcript window to verify whether `:merged` issues are actually solved. Patterns that disappeared → closed with `:solved`; patterns that persist → left as `:merged` (Sonnet) |
| `cai.py cycle` | _(manual/on-demand)_ | Runs verify → fix → revise → review-pr → merge → confirm in sequence. Convenience wrapper for a full pipeline pass; not included in scheduled or startup runs |

On `docker compose up -d` the entrypoint templates the crontab from
the env vars (`CAI_ANALYZER_SCHEDULE`, `CAI_FIX_SCHEDULE`,
`CAI_REVIEW_PR_SCHEDULE`, `CAI_MERGE_SCHEDULE`, `CAI_REVISE_SCHEDULE`,
`CAI_VERIFY_SCHEDULE`, `CAI_AUDIT_SCHEDULE`, `CAI_AUDIT_TRIAGE_SCHEDULE`,
`CAI_CODE_AUDIT_SCHEDULE`, `CAI_CONFIRM_SCHEDULE`), runs each
scheduled subcommand once synchronously so logs show immediate results, then execs
supercronic. (`cycle` is on-demand only and is not part of scheduled or startup runs.)

### Issue lifecycle

The fix subagent transitions issues through a label-based state
machine. The lock label (`:in-progress`) is set as the **first** gh
action so two concurrent `fix` runs can't pick the same issue.

```
                              raised  ◄──┐
                                │       │ (PR closed
                                │ fix    │  unmerged,
                                ▼        │  rolled back)
                          in-progress    │
                                │        │
                        ┌───────┴───────┐│
                        │               ││
                  empty diff      PR opened
                        │               ▼│
                        ▼        pr-open ─┘
                   no-action        │
                                    │ verify (PR merged)
                                    ▼
                                 merged
                                    │
                        ┌───────────┴───────────┐
                        │                       │
                  confirm (pattern       confirm (inconclusive
                   absent)                / unsolved)
                        ▼                       ▼
                  solved (closed)       stays :merged
                                     (reasoning posted)
```

`:no-action` means the fix subagent reviewed the issue and decided no
code change was needed. The agent's reasoning is posted as a comment
on the issue. A human can either close the issue (agreeing with the
bot) or re-label to `:raised` to retry.

### Audit findings

The `audit` subcommand uses a **separate label namespace** (`audit:*`)
to distinguish its findings from analyzer findings (`auto-improve:*`).
Audit findings flag inconsistencies in the issue/PR lifecycle.
Issues labelled `audit:raised` go through `cai.py audit-triage`
first, which relabels eligible ones to `auto-improve:raised` so the
fix subagent picks them up.

| Label | Meaning |
|---|---|
| `audit:raised` | Freshly raised audit finding |
| `audit:solved` | Addressed (manually closed or auto-resolved on next audit) |

Audit categories: `stale_lifecycle`, `lock_corruption`, `loop_stuck`,
`prompt_contradiction`, `topic_duplicate`, `silent_failure`.

There are four exceptions to "report-only": stale `:in-progress`
rollback, stale `:no-action` rollback, stale `:merged` flagging, and
merged-branch cleanup. If an issue has been `:in-progress` for more
than 6 hours with no recent fix activity in the log, the audit
subcommand automatically rolls it back to `:raised`. Stale
`:no-action` issues (7+ days) are rolled back to `:raised` so the fix
agent can retry with new context. Stale `:merged` issues (14+ days)
are flagged with `needs-human-review` since the automation cannot
determine whether the fix worked. Additionally, remote branches for
merged or closed `auto-improve/` PRs are deleted automatically.

### Comment-driven PR iteration

When the bot opens a PR, you can leave a comment asking for changes
instead of closing it. The `revise` subcommand (default: hourly at
`:30`) picks up any PR comment posted **after the most recent commit**
on the branch and feeds it to the revise subagent. It also
auto-rebases unmergeable PRs onto current main before processing
comments; if the rebase has conflicts it posts a comment for human
triage instead.

How it works:

1. Leave either an **issue-level comment** (bottom of the PR) or a
   **line-by-line review comment** (anchored to a specific line in
   the diff). Both surfaces work — the bot reads them all.
2. On the next revise tick, the bot detects any unaddressed comment,
   checks out the existing branch, and runs the revise subagent
3. The subagent makes the smallest change that addresses the comment
   and force-pushes (`--force-with-lease`) to the same branch
4. The PR updates in place — no new PR is created

The rule is simple: any comment with `createdAt` after the branch's
most recent commit is treated as unaddressed. Once the bot pushes a
new commit, all prior comments are considered addressed. Comments
generated by the bot itself (recognized by their content headers
like `## Fix subagent:` or `## Revision summary`) are filtered out
to avoid self-loops. This content-based filtering is more reliable
than login-based filtering because cai's default deployment uses
the human operator's gh token, so "the bot" has the same GitHub
identity as the operator.

If the bot can't address a comment (unclear or out of scope), it
posts a reply explaining why and exits without changes.

### Pre-merge consistency review

The `review-pr` subcommand (default: hourly at `:20`) walks all open
PRs against `main` and checks each one for **ripple effects** —
changes that are internally consistent but create inconsistencies with
the rest of the codebase (stale docs, dead config, missed cross-cutting
references, etc.).

Findings are posted as a single PR comment starting with
`## cai pre-merge review — <sha>`. The SHA prevents re-reviewing PRs
that haven't changed. Because findings are PR comments, the `revise`
subagent picks them up on the next tick and can address them
automatically — no separate issue is created.

This replaces the post-merge consistency review originally proposed in
issue #45. Pre-merge review catches ripple effects before they land in
`main`, avoiding the extra round-trip of a follow-up fix PR.

### Confidence-gated auto-merge

The `merge` subcommand (default: hourly at `:35`) closes the
autonomous loop end-to-end by auto-merging bot PRs that clearly
implement their linked issue. For each open `:pr-open` PR on an
`auto-improve/<N>-*` branch, it:

1. Applies safety filters (bot branch, `:pr-open` label, no
   unaddressed comments, no conflicts, no failed CI, not already
   evaluated at the current SHA)
2. Fetches the linked issue body, PR diff, and PR comments
3. Pipes them through `claude -p --model claude-opus-4-6` with a
   conservative merge-review prompt
4. Parses the model's verdict: a confidence level (`high`, `medium`,
   or `low`) and an independent action (`merge`, `hold`, or `reject`)
5. If the action is `merge` and confidence meets the threshold,
   merges via `gh pr merge --merge --delete-branch`
6. If the action is `reject` and confidence meets the threshold,
   closes the PR via `gh pr close --delete-branch` and transitions
   the issue to `auto-improve:no-action`
7. Otherwise, labels the issue `merge-blocked` and
   posts the verdict reasoning as a PR comment

**Confidence levels:**

| Level | Meaning |
|---|---|
| `high` | PR correctly implements every remediation step, changes are minimal, no bugs or scope creep. Safe to merge without human review. |
| `medium` | PR mostly implements the issue but has minor concerns. Better with human review. |
| `low` | Significant issues — wrong approach, missing functionality, or potential bugs. Should not be merged. |

**Threshold** (`CAI_MERGE_CONFIDENCE_THRESHOLD` env var):

| Value | Behavior |
|---|---|
| `high` (default) | Only `high` verdicts trigger auto-merge or auto-close |
| `medium` | Both `high` and `medium` verdicts trigger auto-merge or auto-close |
| `disabled` | Never auto-merge/close; still posts verdict comments |

The threshold defaults to `high` — only the most clear-cut PRs merge
or close automatically. Relax to `medium` by editing the env var once
trust builds.

`auto-improve:requested` is a separate entry point: a human applies
it to an arbitrary issue to opt it into the fix queue. The label is
restricted to repo admins by `.github/workflows/admin-only-label.yml`
— a non-admin who applies it gets the label removed and a comment
explaining why.

### Triggering tasks ad-hoc

Each subcommand also runs as a one-shot CLI command against the
running container. This is what GitHub Actions, host cron jobs, or
just-trying-things-out from the terminal would use:

```bash
docker compose exec cai python /app/cai.py analyze
docker compose exec cai python /app/cai.py fix              # oldest eligible
docker compose exec cai python /app/cai.py fix --issue 12   # specific issue
docker compose exec cai python /app/cai.py review-pr
docker compose exec cai python /app/cai.py revise
docker compose exec cai python /app/cai.py verify
docker compose exec cai python /app/cai.py audit
docker compose exec cai python /app/cai.py confirm
docker compose exec cai python /app/cai.py merge
```

A short alias makes this trivial:

```bash
alias cai='docker compose -f ~/robotsix-cai/docker-compose.yml exec cai python /app/cai.py'
cai fix --issue 12
cai review-pr
cai revise
cai verify
cai audit
cai confirm
cai merge
```

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

The installer also asks whether to enable **Watchtower** — a small
sidecar container that polls Docker Hub every 30 minutes and
automatically pulls + restarts cai when a new image is published.
Default is **no** (manual updates). If you answer yes, the generated
`docker-compose.yml` includes a `watchtower` service alongside `cai`.

The image used is
[`nickfedor/watchtower`](https://hub.docker.com/r/nickfedor/watchtower)
(pinned to a specific version), an actively-maintained community fork.
The original `containrrr/watchtower` is no longer being updated and
its `:latest` tag ships an embedded Docker client too old for modern
Docker daemons (≥ API 1.44), causing watchtower to crash-loop with
`client version 1.25 is too old`.

**Mid-fix restart caveat:** if Watchtower restarts cai while a fix
subagent is running, the in-flight fix is killed and the issue may be
left stuck in `auto-improve:in-progress`. Manual relabelling back to
`:raised` is needed until the audit feature (tracked separately) lands
to handle automatic recovery.

To change the polling interval, edit the `--interval` value (in
seconds) in the `watchtower` service's `command:` block and run
`docker compose up -d`.

To **enable Watchtower on an existing install**: re-run `install.sh`
and answer yes, or manually edit your `docker-compose.yml` — add the
`watchtower` service and the `com.centurylinklabs.watchtower.enable=true`
label on the `cai` service (see the repo's `docker-compose.yml` for the
commented-out template).

To **disable Watchtower**: comment out (or remove) the `watchtower`
service and the `cai` label in your `docker-compose.yml`, then run
`docker compose up -d`.

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
docker compose up -d           # start the scheduler
docker compose logs -f cai     # watch the first cycle
```

Expected output: the templated crontab, the initial `cai.py init` (a
greeting on the very first run, otherwise skipped), the initial
`cai.py analyze`, then supercronic standing by for the next cron tick.

### Changing the schedule

Edit the `CAI_ANALYZER_SCHEDULE` environment variable in the generated
`docker-compose.yml` (any valid 5-field cron expression, or `@hourly`,
`@daily`, etc.) and restart the service:

```bash
docker compose up -d
```

### Triggering a run ad-hoc

You don't have to wait for the next cron tick — any subcommand can be
invoked directly against the running container, which is what
GitHub Actions or a host cron job would use to kick off a task:

```bash
docker compose exec cai python /app/cai.py analyze
```

### One-shot smoke test (no install)

If you just want to verify the published image works without writing
any files at all, one `docker run` is enough.

**With OAuth credentials from the host:**

```bash
docker run --rm \
  -v ~/.claude/.credentials.json:/root/.claude/.credentials.json \
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

The transcript parser (`parse.py`) only considers sessions whose JSONL
file was modified within a configurable window. This prevents stale
historical data from polluting the analyzer's signal after a fix has
landed. All subcommands that call `parse.py` (analyze, confirm) use
the same global window settings.

- **`CAI_TRANSCRIPT_WINDOW_DAYS`** — number of days of transcript
  history to include in the analysis. Default: `7`. Set to `0` to
  include all sessions (useful for debugging or initial seeding).
- **`CAI_TRANSCRIPT_MAX_FILES`** — maximum number of transcript files
  to read (most recent first by mtime). Default: `50`. Set to `0` to
  disable the count limit. Both knobs apply together — a file must be
  within the time window AND in the top N most recent to be included.
- **`CAI_MERGE_CONFIDENCE_THRESHOLD`** — confidence level required for
  auto-merge. One of `high` (default), `medium`, or `disabled`. See
  the [Confidence-gated auto-merge](#confidence-gated-auto-merge)
  section for details.

**Troubleshooting: `cannot run ssh` errors.** If `cai.py fix` fails
with `error: cannot run ssh: No such file or directory`, your
`cai_gh_config` volume has `git_protocol` set to `ssh` (the container
has no SSH client). Fix it without reinstalling:

```bash
docker compose exec cai gh config set git_protocol https
```

New installs set HTTPS automatically via `--git-protocol https` in the
`gh auth login` step.

Inspect a volume from outside the container:

```bash
docker volume inspect cai_transcripts
docker run --rm -v cai_transcripts:/data alpine ls -R /data
```

A **run log** is written to `./logs/cai.log` (bind-mounted from
`/var/log/cai/cai.log` inside the container). Each `init`, `analyze`,
`fix`, `review-pr`, `revise`, `verify`, `audit`, `confirm`, and `merge` invocation appends one key=value line so you can
watch cycle activity from the host without `docker exec`:

```bash
tail -f ~/robotsix-cai/logs/cai.log
```

Wipe everything (deletes transcripts and gh credentials — you'll need
to re-authenticate afterwards):

```bash
docker compose down --volumes        # if you used compose
docker volume rm cai_transcripts cai_gh_config   # standalone
```

## License

[MIT](LICENSE)
