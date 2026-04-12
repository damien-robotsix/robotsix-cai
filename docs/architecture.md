---
title: Architecture
nav_order: 3
---

# Architecture

## Overview

robotsix-cai is a self-tuning backend that runs inside a Docker container
and dispatches Claude Code headless subagents to analyze, improve, and
maintain its own source code. The entry point is `cai.py`; each
`cai <subcommand>` invocation runs one or more subagents and processes
the results.

The system is designed for **unattended operation**: a cron schedule
drives `cai cycle` (or individual pipeline steps), which selects work
from a GitHub issue queue, acts on it, and updates labels to record
progress.

---

## Agent dispatch model

`cai.py` is the single orchestrator. Every `cmd_*` function:

1. Queries the GitHub API to select the next work item (issue or PR)
2. Assembles a context payload (issue body, PR diff, transcript signals,
   cost data, etc.)
3. Invokes one or more subagents via `claude -p --agent <name>` with the
   context as the prompt
4. Parses the agent's stdout and updates GitHub labels, posts comments,
   or opens/merges PRs based on the result

Agent definitions live in `.claude/agents/*.md` as YAML-frontmatter +
Markdown instruction files. There are 21 agents total, each specialised
for one step of the pipeline.

---

## Auto-improve lifecycle

Issues flow through a label-based state machine. Labels are managed
exclusively by `cai.py` — agents never call `gh` directly.

### Label states

| Label | Meaning |
|-------|---------|
| `auto-improve:raised` | New finding, not yet structured |
| `auto-improve:refined` | Structured plan ready for the fix agent |
| `auto-improve:requested` | Human-filed issue, elevated priority |
| `auto-improve:in-progress` | Fix agent is actively working |
| `auto-improve:pr-open` | PR exists, awaiting review/merge |
| `auto-improve:revising` | PR has unaddressed review comments |
| `auto-improve:merged` | PR merged, awaiting confirmation |
| `auto-improve:solved` | Confirmed fixed and closed |
| `auto-improve:no-action` | Ambiguous or rejected, returned to queue |
| `auto-improve:needs-spike` | Needs research before a fix can be written |
| `auto-improve:needs-exploration` | Needs empirical measurement/benchmarking |
| `auto-improve:parent` | Tracking/umbrella issue, not acted on directly |
| `needs-human-review` | Blocked — requires human decision before merge |

### Main flow

```
raised → (refine) → refined → (fix) → in-progress → pr-open
       → (review-pr, review-docs)
       → (merge) → merged → (confirm) → solved
```

### Branch paths

- `in-progress` → `needs-spike` — fix agent emitted a `## Needs Spike`
  marker; spike agent takes over
- `in-progress` → `no-action` — fix agent exited with zero diff and no
  spike marker; issue re-enters the queue at `:raised`
- `pr-open` → `revising` — reviewer left unaddressed comments; revise
  agent iterates
- `pr-open` → `needs-human-review` — merge agent blocked; human must
  decide
- closed PR, unmerged — `verify` moves the issue back to `:refined` so
  the fix agent can try again

---

## Fix pipeline detail

`cai fix` runs a multi-step pipeline for each issue:

1. **Target selection** — scores all `:refined` and `:requested` issues;
   picks the highest scorer (or uses `--issue N`)
2. **Pre-screen** — Haiku model does a fast check; issues that are
   obviously wrong-shaped are skipped
3. **Dual plan generation** — two `cai-plan` agents run in parallel
   (each capped at $1) producing independent implementation plans
4. **Plan selection** — `cai-select` agent evaluates both plans and
   chooses the better one
5. **Fix agent** — `cai-fix` executes the selected plan in an isolated
   git worktree, commits the changes, and exits
6. **PR opening** — `cai.py` pushes the branch and calls `gh pr create`

---

## Persistence

Three Docker named volumes hold all durable state:

| Volume | Container path | Contents |
|--------|---------------|----------|
| `cai_home` | `/home/cai` | Claude OAuth credentials, gh CLI config, Claude Code session transcripts, runtime config |
| `cai_agent_memory` | `/app/.claude/agent-memory` | Per-agent durable memory files, accumulated across runs |
| `cai_logs` | `/var/log/cai` | Run logs and cost/outcome records |

The container runs as a non-root `cai` user (uid 1000). See the
`Dockerfile` for volume declaration details.

---

## Log files

All log files live under `/var/log/cai/` (the `cai_logs` volume):

| File | Format | Contents |
|------|--------|---------|
| `cai.log` | `key=value` per line | One entry per `cai` invocation: command, duration, outcome |
| `cai-cost.jsonl` | JSON Lines | Per-agent Claude API call cost records |
| `cai-outcomes.jsonl` | JSON Lines | Fix/revise outcome records (issue number, action taken, PR URL) |
| `cai-active.json` | JSON object | Currently-running job metadata (cleared on completion) |

---

## Transcript parsing

The analyzer pipeline reads Claude Code session transcripts from
`~/.claude/projects/` (inside `cai_home`). `parse.py` extracts tool
calls, costs, and outcome signals from the JSONL transcript files.
Two environment variables control the scope:

- `CAI_TRANSCRIPT_WINDOW_DAYS` — how many days of history to include
- `CAI_TRANSCRIPT_MAX_FILES` — maximum number of transcript files to parse
