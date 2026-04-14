# PR Context Dossier
Refs: robotsix/robotsix-cai#567

## Files touched
- `cai.py:2499` — added `BOT_USERNAME = "github-actions[bot]"` constant before `_BOT_COMMENT_MARKERS`
- `cai.py:6897-6927` — added pipeline-reset block in `_pr_label_sweep` after DIRTY-check, before `needs` label sync

## Files read (not touched) that matter
- `cai.py` — `_pr_label_sweep` (lines 6799–6936), `_BOT_COMMENT_MARKERS` area (lines 2497–2520)

## Key symbols
- `BOT_USERNAME` (`cai.py:2499`) — new constant identifying the GitHub Actions bot login
- `_pr_label_sweep` (`cai.py:6799`) — sweep function where reset block was inserted
- `_pr_set_pipeline_state` (`cai.py:6755`) — called to reset label to `pr:edited`
- `_is_bot_comment` (`cai.py:2573`) — checks comment body prefix to identify bot comments
- `_parse_iso_ts` (`cai.py:2637`) — parses ISO timestamps for comparison
- `LABEL_PR_REVIEWED_ACCEPT`, `LABEL_PR_DOCUMENTED` (`cai.py:6736-6737`) — stale labels that trigger reset

## Design decisions
- Used `commits[-1].get("authors", [])` fallback to `[]` when field absent → treat as bot (false-negative preference)
- Only reset when `latest_bot_comment_ts is not None and commit_ts is not None` — no-op if ordering can't be established
- `LABEL_PR_REVIEWED_REJECT` excluded — rejection stands regardless of new pushes
- Rejected: resetting unconditionally on new commits — would cause spurious resets for bot self-pushes

## Out of scope / known gaps
- Multiple co-authors: only first author checked; human second author won't trigger reset (acceptable false-negative)
- No new `gh` API fields added — reuses existing `commits` field already in `--json` query

## Invariants this change relies on
- `gh pr list --json commits` returns `authors` list with `login` field per commit
- Bot pipeline comments use `createdAt` ISO 8601 UTC; commit `committedDate` is same format
- `_is_bot_comment` matches bot comments by body prefix (not author), so it correctly identifies pipeline summary comments

## Revision 1 (2026-04-14)

### Rebase
- clean

### Files touched this revision
- `cai.py:6895-6908` — replaced `_is_bot_comment(c)` with specific pipeline-state heading check using `_REVIEW_COMMENT_HEADING_CLEAN`, `_DOCS_REVIEW_COMMENT_HEADING_CLEAN`, `_DOCS_REVIEW_COMMENT_HEADING_APPLIED`

### Decisions this revision
- Used a local `_pipeline_comment_markers` tuple rather than a new module-level constant — keeps the change minimal and co-located with the comment that describes the intent

### New gaps / deferred
- None
