# audit

Audit subsystem — on-demand read-only agents that raise
`auto-improve:raised` issues for per-module code, cost, workflow,
and external-library findings. The `cai_lib/audit/` package
provides helper libraries (cost reporting, the `docs/modules.yaml`
schema loader plus coverage check). `.claude/agents/audit/*.md`
defines the subagents themselves; each is invoked by a `cmd_*`
function in `cai_lib/cmd_agents.py` or `cai_lib/cmd_misc.py`.

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
- [`cai_lib/audit/runner.py`](../../cai_lib/audit/runner.py) — on-demand
  per-module audit runner for `cai audit-module --kind <kind>` subcommand. Dispatches
  audit agents (cost-reduction, code-reduction, good-practices, workflow-enhancement)
  over all modules; loads manifests from `docs/modules.yaml`.
- [`cai_lib/audit/__init__.py`](../../cai_lib/audit/__init__.py) —
  package init.
- [`.claude/agents/audit/cai-audit-code-reduction.md`](../../.claude/agents/audit/cai-audit-code-reduction.md),
  [`cai-audit-cost-reduction.md`](../../.claude/agents/audit/cai-audit-cost-reduction.md),
  [`cai-audit-external-libs.md`](../../.claude/agents/audit/cai-audit-external-libs.md),
  [`cai-audit-good-practices.md`](../../.claude/agents/audit/cai-audit-good-practices.md),
  [`cai-audit-workflow-enhancement.md`](../../.claude/agents/audit/cai-audit-workflow-enhancement.md)
  — on-demand per-module audits (code shrink, spend, external
  libraries, best practices, workflow).
- [`.claude/agents/audit/cai-audit-audit-health.md`](../../.claude/agents/audit/cai-audit-audit-health.md)
  — on-demand audit-health monitor; reads `/var/log/cai/audit/*/*.jsonl` and
  raises findings for error rows, stale audits, cost anomalies, and
  degenerate zero-findings runs. Invoked by `cmd_audit_health` via
  `cai audit-health`.
- [`.claude/agents/audit/cai-transcript-finder.md`](../../.claude/agents/audit/cai-transcript-finder.md)
  — haiku helper that searches Claude Code session transcripts for a module-scoped query and returns ranked excerpts.

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
- **Audit log path convention.** `cai_lib/audit/runner.py` writes one
  structured JSONL file per `(kind, module)` pair under
  `/var/log/cai/audit/<kind>/<module>.jsonl` (e.g.
  `/var/log/cai/audit/code-reduction/actions.jsonl`). Each line is a
  JSON object with keys `ts`, `level`, `kind`, `module`, `agent`,
  `session_id`, `event` (`start` / `finish` / `error`), `message`,
  `cost_usd`, `duration_ms`, `num_turns`, `tokens`, `findings_count`,
  `exit_code`, and `error_class`. The path root is `AUDIT_LOG_DIR` in
  `cai_lib/config.py`; the helper `audit_log_path(kind, module)` in the
  same file returns the full path. The log is an additive append-only
  sink alongside the existing `cai-cost.jsonl` and `cai.log` files.
- **Querying audit runs.** To see recent runs for a kind:
  `tail -n 20 /var/log/cai/audit/code-reduction/actions.jsonl | python3 -m json.tool`
  To find all errors across all kinds/modules:
  `grep '"event":"error"' /var/log/cai/audit/**/*.jsonl`
- **Cost sensitivity — very high.** The on-demand per-module
  auditors (`cai-audit-code-reduction`, `cai-audit-cost-reduction`,
  `cai-audit-external-libs`, `cai-audit-good-practices`,
  `cai-audit-workflow-enhancement`) are opus-tier and spend
  dominates when they run. `cai-cost-optimize` proposes targeted
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
