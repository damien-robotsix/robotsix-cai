# cai-lib

Core Python library. Contains the FSM (states, transitions, confidence
parsing), the dispatcher that routes issues and PRs to per-state handlers,
the `actions/` handler modules, and shared helpers for git, GitHub, issues,
subprocess, logging, transcript sync, and audit/cost analysis.

## Entry points
- `cai_lib/dispatcher.py` — FSM dispatcher routing issues/PRs to handlers.
- `cai_lib/fsm.py` — FSM re-exporter for states, transitions, confidence.
- `cai_lib/fsm_states.py` — IssueState and PRState enums.
- `cai_lib/fsm_transitions.py` — Transition data and apply/query functions.
- `cai_lib/fsm_confidence.py` — Confidence enum and parsing.
- `cai_lib/actions/*.py` — Per-state handlers (triage, refine, plan, implement, explore, confirm, review_pr, revise, review_docs, merge, fix_ci, open_pr, pr_bounce, rebase, maintain).
- `cai_lib/parse.py` — Deterministic signal extractor from JSONL transcripts.
- `cai_lib/publish.py` — GitHub issue publisher with fingerprint dedup.
- `cai_lib/github.py` — `gh` CLI helpers and label utilities.
- `cai_lib/issues.py` — Issue-lifecycle helpers.
- `cai_lib/dup_check.py` — Pre-triage duplicate / already-resolved check.
- `cai_lib/config.py` — Shared constants and path definitions.
- `cai_lib/cmd_*.py` — CLI subcommand implementations (agents, cycle, implement, misc, rescue, unblock) and shared helpers (cmd_helpers, cmd_helpers_git, cmd_helpers_github, cmd_helpers_issues).
- `cai_lib/watchdog.py` — Stale-lock watchdog.
- `cai_lib/transcript_sync.py` — Cross-host transcript sync.
- `cai_lib/logging_utils.py` — Logging utilities.
- `cai_lib/subprocess_utils.py` — Subprocess helpers.
- `cai_lib/audit/` — Cost and module audit helpers.

## Dependencies
- `agents` — handlers invoke subagents defined under `.claude/agents/`.
