# PR Context Dossier
Refs: robotsix/robotsix-cai#623

## Files touched
- `cai_lib/config.py:72` — added LABEL_APPLYING and LABEL_APPLIED constants
- `cai_lib/fsm.py:17-22` — added LABEL_APPLYING, LABEL_APPLIED to config import
- `cai_lib/fsm.py:103-104` — added APPLYING and APPLIED to IssueState enum
- `cai_lib/fsm.py:134-145` — changed min_confidence to Optional[Confidence]; updated accepts() for None case
- `cai_lib/fsm.py:529-530` — updated render_fsm_mermaid to emit [caller-gated] for None min_confidence
- `cai_lib/fsm.py:162-181` — added 5 new transitions (triaging_to_plan_approved, triaging_to_applying, applying_to_applied, applying_to_human, applied_to_solved)
- `publish.py:103-104` — added applying and applied label tuples
- `cai.py:209-215` — added Confidence to cai_lib.fsm import
- `cai.py:8100-8150` — added _TRIAGE_SKIP_CONFIDENCE_RE, _TRIAGE_PLAN_BLOCK_RE, _TRIAGE_OPS_BLOCK_RE and three parse functions
- `cai.py:7817-7875` — added PLAN_APPROVE/APPLY dual-gate elif branch in cmd_triage
- `tests/test_fsm.py:18-22` — added LABEL_APPLYING, LABEL_APPLIED to config import
- `tests/test_fsm.py:68-74` — updated mermaid test to handle None min_confidence → [caller-gated]
- `tests/test_fsm.py:502-548` — added TestTriagingSkipAheadPaths test class
- `.cai-staging/agents/cai-triage.md` — extended output spec with SkipConfidence, Plan, Ops fields and behavior matrix

## Files read (not touched) that matter
- `cai_lib/fsm.py` — existing Transition dataclass, IssueState, ISSUE_TRANSITIONS, accepts() method
- `cai.py` — cmd_triage function structure and verdict dispatch pattern
- `tests/test_fsm.py` — existing test patterns, BFS reachability test

## Key symbols
- `Transition.min_confidence` (cai_lib/fsm.py:134) — changed to Optional[Confidence]; None means caller-gated
- `Transition.accepts()` (cai_lib/fsm.py:138) — updated to return True when min_confidence is None
- `render_fsm_mermaid` (cai_lib/fsm.py:523) — updated label format for None min_confidence
- `cmd_triage` (cai.py:7658) — extended with PLAN_APPROVE/APPLY dual-gate elif block
- `_parse_triage_skip_confidence` (cai.py) — new parser for SkipConfidence field
- `_parse_triage_plan` (cai.py) — new parser for Plan block
- `_parse_triage_ops` (cai.py) — new parser for Ops block
- `IssueState.APPLYING` (cai_lib/fsm.py) — new transient state; cmd_maintain (Step 3) drains it
- `IssueState.APPLIED` (cai_lib/fsm.py) — new waypoint state; applied_to_solved advances to SOLVED

## Design decisions
- `triaging_to_plan_approved` and `triaging_to_applying` use `min_confidence=None` — gating is at application level in cmd_triage, not FSM infrastructure; this is semantically cleaner since two separate confidence values (RoutingConfidence + SkipConfidence) gate the skip
- `accepts()` returns True for None min_confidence — consistent with "no FSM gate" semantics; callers that call apply_transition (not apply_transition_with_confidence) bypass the gate anyway
- Rejected: setting min_confidence=Confidence.HIGH on skip-ahead transitions — misleading because the actual gate is the dual-check in cmd_triage, not a single confidence value
- Rejected: extending _parse_issue_triage_verdict to include SkipConfidence/Plan/Ops — separate parsers follow the codebase pattern (cf. _parse_refine_next_step) and are independently testable

## Out of scope / known gaps
- cmd_maintain not implemented (Step 3) — APPLYING state is wired but no command drains it yet
- No resume transitions for APPLYING or APPLIED from HUMAN_NEEDED — deferred to Step 3
- _parse_triage_plan and _parse_triage_ops use a greedy regex that may mis-match if Plan/Ops body contains lines of the form "Word: value"; acceptable for now as Step 3 can refine

## Revision 1 (2026-04-14)

### Rebase
- clean

### Files touched this revision
- `cai.py:4174` — added LABEL_APPLYING and LABEL_APPLIED to cmd_verify recovery label-removal tuple

### Decisions this revision
- Follow existing ordering convention (after LABEL_PLAN_APPROVED, before LABEL_RAISED) for new labels in the tuple

### New gaps / deferred
- none

## Invariants this change relies on
- Step 1 artifacts (IssueState.TRIAGING, LABEL_TRIAGING, cmd_triage, raise_to_triaging transition) must exist — verified before implementation
- APPLYING and APPLIED are reachable from RAISED via RAISED→TRIAGING→APPLYING→APPLIED, satisfying the BFS reachability test
- The existing test_transition_accepts test still passes because accepts() only changes behavior for None min_confidence, not for Confidence-valued thresholds
