# PR Context Dossier
Refs: robotsix/robotsix-cai#456

## Files touched
- `cai.py:8339` — converted 8 bare `sub.add_parser()` calls to named parsers with `--pr`/`--issue` arguments
- `cai.py:3039` — `cmd_revise`: added `--pr` bypass using `_fetch_review_comments` + `_filter_unaddressed_comments`
- `cai.py:5650` — `cmd_confirm`: added `--issue` bypass via `gh issue view`
- `cai.py:5960` — `cmd_review_pr`: added `--pr` bypass via `gh pr view`
- `cai.py:6185` — `cmd_review_docs`: added `--pr` bypass via `gh pr view`
- `cai.py:6571` — `cmd_merge`: added `--pr` bypass via `gh pr view`
- `cai.py:7050` — `cmd_refine`: added `--issue` bypass via `gh issue view`
- `cai.py:7240` — `cmd_spike`: added `--issue` bypass via `gh issue view`
- `cai.py:7700` — `cmd_explore` Phase 2: added `--issue` bypass via `gh issue view`

## Files read (not touched) that matter
- `cai.py:2762` — `_select_revise_targets()`: defines the dict structure (`pr_number`, `issue_number`, `branch`, `comments`, `needs_rebase`) that `cmd_revise` bypass must replicate
- `cai.py:2667` — `_filter_unaddressed_comments()`: used in `cmd_revise` bypass to stay consistent with queue-based path
- `cai.py:2712` — `_fetch_review_comments()`: used in `cmd_revise` bypass to merge line-by-line review comments

## Key symbols
- `_select_revise_targets` (`cai.py:2762`) — bypassed when `--pr` is set; bypass replicates its return dict structure
- `_filter_unaddressed_comments` (`cai.py:2667`) — called in the `cmd_revise` bypass path
- `_fetch_review_comments` (`cai.py:2712`) — called in the `cmd_revise` bypass path
- `getattr(args, "pr/issue", None)` — pattern used consistently (matches `cmd_fix --issue`)

## Design decisions
- Use `getattr(args, "pr/issue", None)` guard — matches existing `cmd_fix` pattern (line 2118), safe when attr not present on namespace
- `cmd_revise --pr`: use `_filter_unaddressed_comments` with actual commit timestamp rather than treating all comments as unaddressed — more accurate and consistent
- `cmd_explore --issue`: only bypasses Phase 2 queue selection; Phase 1 (follow-up on `:exploration-done`) runs unconditionally since it's a separate concern
- No label validation for direct targets — operator is explicitly choosing the item; matches `cmd_fix --issue` behavior
- Rejected: modifying `_select_revise_targets` to accept a PR number — unnecessary coupling; bypass path is simpler

## Out of scope / known gaps
- `cmd_cycle` orchestration not changed — it still calls each command without `--pr`/`--issue`
- No label validation (e.g., requiring `:needs-spike` for spike) when targeting directly — intentional
- `cmd_revise --pr` does not skip PRs labeled `:needs-human` when targeted directly — operator's explicit choice

## Invariants this change relies on
- `args` namespace always has the new attribute when the subparser is invoked (argparse guarantees this)
- `getattr(args, "pr", None)` returns `None` when the command is invoked via `cmd_cycle` (no `--pr` arg passed)
- `_gh_json` raises `subprocess.CalledProcessError` on failure — all bypass paths handle this

## Revision 1 (2026-04-12)

### Rebase
- clean

### Files touched this revision
- `README.md:292-317` — expanded "Triggering tasks ad-hoc" section to show `--pr` and `--issue` options for all 8 newly targeted commands (revise, confirm, review-pr, review-docs, merge, refine, spike, explore); also updated alias convenience block

### Decisions this revision
- Used reviewer's suggested concrete examples (with `--pr 45` / `--issue 12`) rather than a prose note — more scannable for operators

### New gaps / deferred
- None
