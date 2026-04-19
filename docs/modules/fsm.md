# fsm

FSM core — enums for issue/PR states, transition data, confidence parsing,
and the doc generator that renders the Mermaid diagrams in `docs/fsm.md`.

## Entry points
- `cai_lib/fsm.py` — Umbrella re-exporter for states, transitions, confidence.
- `cai_lib/fsm_states.py` — `IssueState` and `PRState` enums.
- `cai_lib/fsm_transitions.py` — Transition data and apply/query helpers.
- `cai_lib/fsm_confidence.py` — `Confidence` enum and parser.
- `scripts/generate-fsm-docs.py` — Generator for `docs/fsm.md`.
