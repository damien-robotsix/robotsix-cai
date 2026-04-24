# github-glue

GitHub glue layer — `gh` CLI wrappers, the issue publisher with
fingerprint dedup, the duplicate / already-resolved pre-triage
check, and the issue-model helpers shared by handlers. Every
pipeline component that reads from or writes to GitHub flows
through this module.

## Key entry points
- [`cai_lib/github.py`](../../cai_lib/github.py) — `_gh_json(args)`
  invokes `gh api` and parses JSON output; `check_gh_auth`,
  `check_claude_auth` are the readiness gates; label/comment
  helpers `_set_labels`, `_set_pr_labels`, `_post_issue_comment`,
  `_post_pr_comment`, `_issue_has_label`; message builders
  `_build_issue_block`, `_fetch_linked_issue_block`,
  `_build_implement_user_message`; closure helpers
  `close_issue_not_planned`, `close_issue_completed`;
  stale-PR recovery `_recover_stale_pr_open`,
  `_close_orphaned_prs`; remote-lock primitives
  `_acquire_remote_lock`, `_release_remote_lock`,
  `_stabilize_lock_comments`, `_delete_issue_comment`,
  `_list_lock_comments`; blocker helpers `blocking_issue_numbers`,
  `open_blockers`.
- [`cai_lib/publish.py`](../../cai_lib/publish.py) — `Finding`
  dataclass; `load_findings_json(path, valid_categories)`;
  `ensure_labels` / `ensure_all_labels`; `issue_exists(key)`
  fingerprint lookup; `create_issue(f, namespace)`; `main()` is
  the CLI entry (also exposed via the root-level `publish.py`
  shim).
- [`cai_lib/issues.py`](../../cai_lib/issues.py) — native
  sub-issues API wrappers: `create_issue(title, body, labels)`,
  `link_sub_issue(parent_number, child_id)`,
  `list_sub_issues(parent_number)`,
  `get_parent_issue(issue_number)` (walks GitHub parent chain),
  `all_sub_issues_closed(parent_number)`.
- [`cai_lib/dup_check.py`](../../cai_lib/dup_check.py) —
  `DupCheckVerdict` dataclass; `parse_dup_check_verdict(text)`,
  `build_dup_check_message(…)`, `_fetch_context(issue_number)`,
  `check_duplicate_or_resolved(issue)`.

## Inter-module dependencies
- Imports from **config** — `REPO`, label constants, log paths.
- Imports from **subprocess_utils** — `_run`, `_run_claude_p` (the
  latter used by `dup_check.py` to call the haiku subagent).
- Imports from **utils.log** — `log_run`.
- Imports from **fsm** — `watchdog.py` pulls in state/transition
  helpers, not this module directly; `github.py` does not import
  fsm but `publish.py`'s callers do.
- Imported by **actions** — every handler uses
  `_set_labels`/`_post_*_comment`/`_gh_json`.
- Imported by **cli** — the `cmd_*` functions (especially
  `cmd_audit_module`, `cmd_analyze`) call
  `publish.create_issue` and `github.*` helpers.
- Imported by **tests** — `tests/test_publish.py`,
  `tests/test_dup_check.py`, `tests/test_orphaned_prs.py`,
  `tests/test_remote_lock.py`.

## Operational notes
- **Remote lock invariant.** Two dispatcher runs on different
  hosts can race on the same issue/PR; `_acquire_remote_lock`
  emits a stabilising comment that carries a UUID, and
  `_release_remote_lock` deletes it. Any handler that writes must
  go through these helpers — skipping the lock is the primary
  source of race-condition bugs.
- **Fingerprint dedup.** `publish.create_issue` enforces
  per-finding uniqueness via the `cai-fp:<sha>` footer it embeds
  in every issue body; `issue_exists(key)` is the lookup. Stripping
  or renaming the footer would allow duplicate issues.
- **Cost sensitivity.** Low — `dup_check` calls a haiku subagent
  (cheap); `gh` CLI calls are free. The dominant cost here is
  indirect, via issues raised that kick off expensive downstream
  handlers.
- **CI implications.** `gh` is invoked with `--repo $REPO`; in
  tests these calls are stubbed. The real `gh auth status` check
  runs in `cmd_verify` and gates the cycle.
- **Root shim.** `publish.py` at repo root re-exports
  `cai_lib/publish.py` — preserve it.
