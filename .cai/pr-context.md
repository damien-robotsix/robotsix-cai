# PR Context Dossier
Refs: robotsix/robotsix-cai#454

## Files touched
- `.cai-staging/agents/cai-review-docs.md` — new agent definition for pre-merge docs review
- `cai.py:2556` — added `"## cai docs review (clean)"` to `_BOT_COMMENT_MARKERS`
- `cai.py:5732` — added `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` / `_DOCS_REVIEW_COMMENT_HEADING_CLEAN` constants
- `cai.py:5751` — added `cmd_review_docs` function (mirrors `cmd_review_pr`)
- `cai.py:6211` — added safety filter 7b in `cmd_merge`: gate on `review-docs` having reviewed head SHA
- `cai.py:7143` — added `review-docs` step to `_drain_pending_prs`
- `cai.py:7701` — registered `review-docs` argparse subcommand
- `cai.py:7757` — registered `review-docs` in handlers dict

## Files read (not touched) that matter
- `.claude/agents/cai-review-pr.md` — primary template for the new agent definition and `cmd_review_docs` structure

## Key symbols
- `cmd_review_docs` (`cai.py:5751`) — new function; mirrors `cmd_review_pr` exactly but targets `cai-review-docs` agent and uses docs-review headings
- `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` (`cai.py:5732`) — heading for actionable findings comments; NOT in `_BOT_COMMENT_MARKERS` so revise picks them up
- `_DOCS_REVIEW_COMMENT_HEADING_CLEAN` (`cai.py:5733`) — heading for clean comments; IS in `_BOT_COMMENT_MARKERS` so revise skips them
- `has_docs_review_at_sha` (`cai.py:6211`) — merge gate variable analogous to `has_review_at_sha`

## Design decisions
- Modeled `cmd_review_docs` exactly on `cmd_review_pr` for consistency — same clone strategy, same comment structure, same SHA-idempotency check
- Merge gate (safety filter 7b) runs after the review-pr gate so both reviews must pass before merge
- Clean heading added to `_BOT_COMMENT_MARKERS` so revise ignores "no docs needed" comments
- Findings heading NOT added to `_BOT_COMMENT_MARKERS` so revise sees and addresses docs findings
- Rejected: reusing `_log_review_pr_findings` for docs — the log is specifically named for review-pr patterns; docs findings don't need the same analytics

## Out of scope / known gaps
- No `REVIEW_DOCS_PATTERN_LOG` analytics — kept minimal per issue scope
- The `docs/` directory is currently empty; agent handles this gracefully with "No documentation updates needed."

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `cai.py:7376` — updated `cmd_cycle` docstring flow: "revise → review-pr → merge" → "revise → review-pr → review-docs → merge"
- `cai.py:7929` — updated cycle subcommand help text to include `review-docs` in the pipeline list

### Decisions this revision
- Both flow-description locations updated to match the actual `_drain_pending_prs` pipeline order (already updated by the PR)

### New gaps / deferred
- None

## Revision 2 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `README.md:67` — cycle table row pipeline description: added `review-docs` between `review-pr` and `merge`
- `README.md:290` — ad-hoc docker compose example: added `review-docs` after `review-pr`
- `README.md:303` — alias convenience example: added `cai review-docs` after `cai review-pr`
- `README.md:540` — run log description: added `review-docs` to the command list

### Decisions this revision
- All four stale README locations updated per reviewer findings

### New gaps / deferred
- None

## Revision 3 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `entrypoint.sh:24` — cycle comment: added `review-docs` between `review-pr` and `merge`
- `cai.py:122` — module docstring subcommand list: added `review-docs` between `review-pr` and `merge`

### Decisions this revision
- Both locations are pure documentation; no logic changed

### New gaps / deferred
- None

## Revision 4 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `cai.py:181` — added `cai-review-docs` to cloned-worktree agents list in AGENT_MEMORY_DIR comment
- `cai.py:1903` — added `cai-review-docs` to cloned-worktree agents list in `_work_directory_block` docstring
- `docker-compose.yml:82` — added `review-docs` to cloned-worktree agents list in volume comment
- `README.md:494` — added `review-docs` to cloned-worktree agents list in volumes section

### Decisions this revision
- All four missing_co_change locations updated to include `cai-review-docs` / `review-docs` after `review-pr`

### New gaps / deferred
- None

## Invariants this change relies on
- `_BOT_COMMENT_MARKERS` clean-heading match suppresses revise re-processing; findings heading absence ensures revise acts on docs gaps
- The `_DOCS_REVIEW_COMMENT_HEADING_FINDINGS` prefix (`"## cai docs review"`) is distinct from `"## cai pre-merge review"` — no collision
- `_drain_pending_prs` runs steps sequentially; `review-docs` runs after `review-pr` and before `merge`
