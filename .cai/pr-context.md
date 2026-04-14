# PR Context Dossier
Refs: damien-robotsix/robotsix-cai#622

## Files touched
- cai_lib/config.py:74 — added LABEL_TRIAGING, LABEL_KIND_CODE, LABEL_KIND_MAINTENANCE constants
- cai_lib/fsm.py:16 — added LABEL_TRIAGING to import block
- cai_lib/fsm.py:99 — added IssueState.TRIAGING enum value between RAISED and REFINING
- cai_lib/fsm.py:146 — added raise_to_triaging, triaging_to_refining, triaging_to_human transitions
- publish.py:93 — inserted auto-improve:triaging, kind:code, kind:maintenance label entries
- cai.py:4641 — added _parse_issue_triage_verdict() helper after _parse_triage_verdicts()
- cai.py:7622 — added cmd_triage() function before cmd_refine
- cai.py:1278 — updated cmd_plan_all() to track refining state and route through triage
- cai.py:9587 — added triage subparser
- cai.py:9681 — added "triage": cmd_triage to handlers dict
- tests/test_fsm.py:389 — updated test_raised_only_reaches_refining_or_human to use assertIn
- tests/test_fsm.py:463 — added TestTriagingState test class
- .cai-staging/agents/cai-triage.md — new inline-only triage agent definition

## Files read (not touched) that matter
- cai_lib/github.py — _gh_json, _set_labels signatures
- cai_lib/subprocess_utils.py — _run, _run_claude_p signatures
- cai_lib/logging_utils.py — log_run, _write_active_job, _clear_active_job signatures

## Key symbols
- `cmd_triage` (cai.py) — new entry-point function, routes :raised issues through cai-triage agent
- `_parse_issue_triage_verdict` (cai.py) — parses RoutingDecision/RoutingConfidence/Kind/DuplicateOf/Reasoning from agent output
- `IssueState.TRIAGING` (cai_lib/fsm.py) — new transient state between RAISED and REFINING
- `raise_to_triaging` (cai_lib/fsm.py) — normal entry from RAISED
- `triaging_to_refining` (cai_lib/fsm.py) — normal exit to REFINING with kind label
- `triaging_to_human` (cai_lib/fsm.py) — divert to HUMAN_NEEDED
- `raise_to_refining` (cai_lib/fsm.py) — bypass still exists for direct --issue targeting

## Design decisions
- cmd_triage uses _run() + capture_output for gh issue close (same pattern as cmd_audit_triage)
- DISMISS at non-HIGH confidence falls through to REFINE (not blocked)
- kind label applied via separate _set_labels call after triaging_to_refining fires (no extra_add on apply_transition)
- cmd_plan_all now tracks 3-tuple (raised, refining, refined) for stuck detection
- raise_to_refining bypass kept intact — cmd_refine --issue N still works without triage
- Rejected: adding min_confidence to triaging_to_refining — triage transitions are deterministic, not confidence-gated

## Out of scope / known gaps
- LABEL_APPLYING, LABEL_APPLIED, skip-ahead transitions not added (Step 2+ content)
- audit:raised and check-workflows:raised pipelines not migrated (Steps 4-5)
- auto-improve:no-action and raise_to_refining not retired
- cmd_refine not modified (it already handles :refining pool)

## Invariants this change relies on
- _gh_json raises subprocess.CalledProcessError on failure (used for error handling in cmd_triage)
- _run() returns CompletedProcess with .returncode and .stderr attributes
- from cai_lib.config import * in cai.py makes LABEL_TRIAGING/LABEL_KIND_CODE/LABEL_KIND_MAINTENANCE available without explicit import
