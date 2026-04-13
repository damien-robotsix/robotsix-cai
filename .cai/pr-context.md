# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#499

## Files touched
- docs/architecture.md — new file: pipeline overview, issue lifecycle, supporting processes, orchestration
- docs/cli.md — new file: subcommands grouped by category (core pipeline, issue processing, maintenance, utilities)
- docs/agents.md — new file: all 21 agents grouped by role with model, access level, and purpose
- docs/configuration.md — new file: environment variables and Docker volumes

## Files read (not touched) that matter
- docs/index.md — existing home page; not modified (just-the-docs builds sidebar from nav_order automatically)
- docs/_config.yml — confirmed just-the-docs theme is in use; nav_order frontmatter drives sidebar
- cai.py:8472-8562 — argparse subcommand definitions used as source of truth for cli.md descriptions
- .claude/agents/*.md — frontmatter of all 21 agents used as source of truth for agents.md

## Key symbols
- `nav_order` (docs/*.md frontmatter) — controls sidebar ordering in just-the-docs; required for visibility
- `sub.add_parser` (cai.py:8475-8535) — authoritative subcommand help strings

## Design decisions
- No changes to index.md — just-the-docs sidebar is theme-driven via nav_order, not a hand-maintained link list
- architecture.md gets nav_order:1, cli.md nav_order:2, agents.md nav_order:3, configuration.md nav_order:4
- cai-cost-optimize classified as read-only (tools: Read, Grep, Glob only — verified from frontmatter)
- Agents table includes all 21 agents found in .claude/agents/

## Out of scope / known gaps
- index.md not touched per plan; already has title:Home which places it first in sidebar
- No exhaustive API-level reference; stubs with accurate summaries only
- cai-propose-review is in review group (not maintenance) since it gates creative proposals before they enter pipeline

## Invariants this change relies on
- just-the-docs theme renders nav_order as sidebar position; pages without nav_order may be hidden
- Subcommand descriptions sourced from cai.py argparse help strings — will drift if cai.py is updated

## Revision 1 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/cli.md:31 — `analyze` description corrected to match argparse help string ("Run the analyzer + publish findings")
- docs/cli.md:38 — `audit-triage` description corrected to match argparse help string ("no PRs" not "no PRs opened")
- docs/cli.md:46 — `init` description corrected to match argparse help string ("if no transcripts exist" not "yet")

### Decisions this revision
- All three changes are verbatim corrections to match cai.py argparse help strings — no paraphrasing

### New gaps / deferred
- None

## Revision 2 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/cli.md:39 — `code-audit` description corrected to match argparse help string (removed "and dead code")

### Decisions this revision
- Verbatim correction to match cai.py:8495 argparse help string exactly per dossier invariant

### New gaps / deferred
- None

## Revision 3 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/architecture.md:36-45 — replaced incorrect issue lifecycle labels (`:planned`, `:review`, `:confirmed`) with actual labels from cai.py constants (`:raised`, `:refined`, `:in-progress`, `:pr-open`, `:merged`, `:solved`)

### Decisions this revision
- Accurate labels sourced from cai.py lines 193-212 (LABEL_* constants) and cross-checked against README.md lifecycle diagram (lines 89-130)
- Added `:no-action` and re-queue / `needs-human-review` outcomes to match the full state machine

### New gaps / deferred
- None

## Revision 4 (2026-04-13)

### Rebase
- resolved: docs/agents.md, docs/architecture.md, docs/cli.md, docs/configuration.md (both-added conflicts with main; HEAD kept throughout — main's versions from PR #492 were more complete)

### Files touched this revision
- docs/configuration.md — added Agent Schedules section with all 17 CAI_*_SCHEDULE variables and defaults; added Transcript Analysis Variables section (CAI_TRANSCRIPT_WINDOW_DAYS, CAI_TRANSCRIPT_MAX_FILES)
- docs/architecture.md — Lifecycle Labels table now includes all labels: `:requested`, `:revising`, `auto-improve:parent`, `audit:needs-human`, `merge-blocked`, `needs-human-review`

### Decisions this revision
- Conflict resolution kept HEAD (main) content for all 4 files — main had nav_order frontmatter, correct structure, and review-cycle fixes from PR #492
- Schedule vars and label additions address the two `missing_co_change` review findings from @damien-robotsix in the same pass as conflict resolution
- No separate editing pass needed since HEAD content was written with the review fixes already incorporated

### New gaps / deferred
- None

## Revision 5 (2026-04-13)

### Rebase
- clean

### Files touched this revision
- docs/configuration.md:17 — introductory sentence updated: "most are also explicitly configured in `docker-compose.yml`" instead of "both files set all defaults"

### Decisions this revision
- Used reviewer's suggested wording verbatim: clarifies that only most (not all) schedule vars appear in docker-compose.yml
- Did not add CAI_SPIKE_SCHEDULE/CAI_COST_OPTIMIZE_SCHEDULE to docker-compose.yml — behavioral change outside this docs PR's scope

### New gaps / deferred
- None
