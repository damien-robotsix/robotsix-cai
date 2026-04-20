# audit

Audit subsystem — scheduled read-only agents that raise
`auto-improve:raised` issues for cost, code, workflow, module, and
analysis findings. The `cai_lib/audit/` package provides helper
libraries (cost reporting, the `docs/modules.yaml` schema loader
plus coverage check). `.claude/agents/audit/*.md` defines the
subagents themselves; each is invoked by a `cmd_*` function in
`cai_lib/cmd_agents.py` or `cai_lib/cmd_misc.py`.

## Key entry points
- [`cai_lib/audit/cost.py`](../../cai_lib/audit/cost.py) — `_load_outcome_counts`,
  `_load_cost_log`, `_row_ts`, `_primary_model`,
  `_build_cost_summary`; token/cost helpers consumed by
  `cmd_cost_report` and `cmd_cost_optimize`.
- [`cai_lib/audit/modules.py`](../../cai_lib/audit/modules.py) —
  `ModuleEntry` dataclass; `load_modules(path, check_doc_exists)`
  and `coverage_check(modules, files)`. Drives
  `scripts/check-modules-coverage.py` and (indirectly) the
  `cai-review-docs` pipeline stage.
- [`cai_lib/audit/__init__.py`](../../cai_lib/audit/__init__.py) —
  package init.
- [`.claude/agents/audit/cai-audit.md`](../../.claude/agents/audit/cai-audit.md)
  — queue / state-machine auditor (opus).
- [`.claude/agents/audit/cai-analyze.md`](../../.claude/agents/audit/cai-analyze.md)
  — session-transcript signal analyser.
- [`.claude/agents/audit/cai-code-audit.md`](../../.claude/agents/audit/cai-code-audit.md)
  — read-only source-tree auditor.
- [`.claude/agents/audit/cai-agent-audit.md`](../../.claude/agents/audit/cai-agent-audit.md)
  — weekly audit of `.claude/agents/*.md` definitions.
- [`.claude/agents/audit/cai-audit-code-reduction.md`](../../.claude/agents/audit/cai-audit-code-reduction.md),
  [`cai-audit-cost-reduction.md`](../../.claude/agents/audit/cai-audit-cost-reduction.md),
  [`cai-audit-external-libs.md`](../../.claude/agents/audit/cai-audit-external-libs.md),
  [`cai-audit-workflow-enhancement.md`](../../.claude/agents/audit/cai-audit-workflow-enhancement.md)
  — on-demand per-module audits (code shrink, spend, external libraries, workflow).
- [`.claude/agents/audit/cai-confirm.md`](../../.claude/agents/audit/cai-confirm.md)
  — verifies merged PRs resolved their issues.

## Inter-module dependencies
- Imports from **config** — `COST_LOG_PATH`, `OUTCOME_LOG_PATH`,
  `LOG_PATH` (consumed by `cost.py` and `logging_utils`).
- Imports from **docs** (indirect) — `modules.py` parses
  `docs/modules.yaml` and validates file coverage against every
  tracked source file.
- Imported by **cli** — `cmd_agents.py`, `cmd_misc.py`, and
  `cmd_rescue.py` invoke these subagents; `logging_utils.py`
  imports `_load_outcome_counts` from `cost.py`.
- Imported by **scripts** — `scripts/check-modules-coverage.py`
  imports `load_modules` and `coverage_check` from
  `cai_lib.audit.modules`.
- Imported by **tests** — `tests/test_audit_modules.py` pins the
  schema loader + coverage check.
- Subagents (`.claude/agents/audit/*.md`) do not import Python;
  they receive inputs via their user message and write findings
  to `findings.json`.

## Operational notes
- **Cost sensitivity — very high.** `cai-audit`, `cai-code-audit`,
  `cai-agent-audit`, `cai-audit-cost-reduction`,
  `cai-audit-workflow-enhancement`, and `cai-analyze` are all
  opus / sonnet-tier and run on cron. Prompt size and cadence
  dominate weekly spend; `cai-cost-optimize` proposes targeted
  reductions for this module.
- **Findings contract.** Every audit subagent writes exactly one
  `findings.json` file which `cai_lib/publish.py` consumes via
  `load_findings_json`. Changing the schema there requires a
  matched change in every audit agent.
- **Modules-registry invariant.** `coverage_check` must return no
  errors on `main`; `scripts/check-modules-coverage.py` is the
  gate, and PRs that add or rename source files without updating
  `docs/modules.yaml` will fail it.
- **CI implications.** `tests/test_audit_modules.py` pins the
  YAML loader; the modules registry is also touched by the
  `cai-review-docs` review stage (see agents-review).
