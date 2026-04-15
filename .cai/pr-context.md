# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#627

## Files touched
- `cai_lib/github.py:234` — added `close_issue_not_planned()` helper function
- `cai_lib/config.py:57` — replaced `LABEL_NO_ACTION` with tombstone comment
- `cai_lib/config.py:116` — replaced `_STALE_NO_ACTION_DAYS` with tombstone comment
- `cai_lib/__init__.py:34` — removed `LABEL_NO_ACTION` from imports and `__all__`
- `cai_lib/__init__.py:52` — removed `_STALE_NO_ACTION_DAYS` from imports and `__all__`
- `publish.py:90` — removed `auto-improve:no-action` from `LABELS`; added to `LABELS_TO_DELETE`
- `cai_lib/actions/implement.py:35` — removed `LABEL_NO_ACTION` import; added `close_issue_not_planned`
- `cai_lib/actions/implement.py:550` — replaced no-action else branch with `close_issue_not_planned()` call; spike branch now has standalone complete logic
- `cai_lib/actions/merge.py:26` — removed `LABEL_NO_ACTION` import; added `close_issue_not_planned`
- `cai_lib/actions/merge.py:427` — replaced `_set_labels(add=[LABEL_NO_ACTION])` block with `close_issue_not_planned()` call
- `cai.py:241` — added `close_issue_not_planned` to `from cai_lib.github import`
- `cai.py:193` — removed `_STALE_NO_ACTION_DAYS` from explicit import
- `cai.py:203` — removed `LABEL_NO_ACTION` from `_ALL_MANAGED_ISSUE_LABELS`
- `cai.py:915` — deleted `_unstuck_stale_no_action()` function
- `cai.py:1030` — deleted `_apply_no_action_to_unlabeled_closed()` function
- `cai.py:1009` — added `_migrate_no_action_labels()` function before `cmd_audit`
- `cai.py:1036` — replaced `unstuck_no_action = _unstuck_stale_no_action()` with `_migrate_no_action_labels()` call
- `cai.py:1056` — removed `no_action_applied = _apply_no_action_to_unlabeled_closed()` call
- `cai.py:1149,1164` — removed `unstuck_no_action` and `no_action_applied` reporting blocks
- `cai.py:1206,1259` — removed `no_action_unstuck` and `no_action_applied` kwargs from both `log_run("audit", ...)` calls
- `cai.py:2687` — removed `("no-action", LABEL_NO_ACTION)` from throughput counter

## Files read (not touched) that matter
- `cai_lib/actions/implement.py` — needed to understand spike vs. no-action if/else structure before refactoring

## Key symbols
- `close_issue_not_planned` (`cai_lib/github.py:234`) — new shared helper; closes issue via `gh issue close --reason "not planned"` with a marker comment
- `_migrate_no_action_labels` (`cai.py:~1009`) — one-time migration helper called from `cmd_audit`; idempotent after label deletion

## Design decisions
- Spike branch in implement.py given standalone complete logic (print + comment + label) instead of shared post-if/else code — original shared code was only compatible with label-based flow
- `locked = False` moved into both if and else branches of implement.py to maintain per-branch early returns cleanly
- `_migrate_no_action_labels` uses try/except for `CalledProcessError` so it's safe after label is deleted from GitHub
- Rejected: inlining `subprocess.run` in each caller — shared helper avoids duplication

## Out of scope / known gaps
- `.claude/agents/cai-audit.md` still references `stale_no_action_unstuck` and `no_action_applied_retroactively` action names in its prompt text — these are informational references that won't cause runtime errors but are now stale documentation; scope guardrails prohibit editing agent files
- merge.py comment at line 411 says "mark issue no-action" — now outdated but harmless

## Invariants this change relies on
- GitHub allows label operations on closed issues (`_set_labels` remove after close in implement.py is safe)
- `gh issue list --label auto-improve:no-action` returns `CalledProcessError` once label is deleted from GitHub (migration helper's try/except relies on this)
- `close_issue_not_planned` in `cai_lib/github.py` has access to `REPO`, `subprocess`, `sys` (all already imported in that module)
