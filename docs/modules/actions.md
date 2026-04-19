# actions

Per-state handlers invoked by the FSM dispatcher — one module per
lifecycle state. Each handler reads the current state, drives the
relevant Claude subagent(s), and applies a transition to the next
state via `cai_lib/fsm_transitions.py`.

## Entry points
- `cai_lib/actions/*.py` — One handler per FSM state (triage, refine, plan, implement, explore, confirm, review_pr, revise, review_docs, merge, fix_ci, open_pr, pr_bounce, rebase, maintain).
