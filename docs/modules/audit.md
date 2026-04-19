# audit

Audit subsystem — scheduled read-only agents that raise
`auto-improve:raised` issues for cost, code, workflow, module, and
analysis findings. The `cai_lib/audit/` package provides helper
libraries (cost reporting, the `docs/modules.yaml` schema loader +
coverage check). `.claude/agents/audit/*.md` defines the subagents
themselves.

## Entry points
- `cai_lib/audit/cost.py` — Token/cost audit helpers.
- `cai_lib/audit/modules.py` — `docs/modules.yaml` schema loader + coverage check.
- `.claude/agents/audit/*.md` — Audit subagents (cai-audit, cai-analyze, cai-code-audit, cai-audit-code-reduction, cai-audit-cost-reduction, cai-audit-workflow-enhancement, cai-agent-audit, cai-confirm).
