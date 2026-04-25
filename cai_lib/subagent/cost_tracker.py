"""Cost-row recorder for :class:`cai_lib.subagent.core.SubAgent`.

A :class:`SubAgent` holds one :class:`CostTracker` that accumulates a
:class:`CostRow` per successful run. :meth:`CostTracker._emit` is a
no-op hook repo-specific subclasses override to ship the row onward
(e.g. :class:`cai_lib.cai_subagent.CaiCostTracker` writes to disk and
mirrors a comment onto the target issue/PR).

Kept in its own module so :mod:`cai_lib.subagent.core` stays focused on
the SDK query loop and CompletedProcess shape, and so subclasses can
override the emit boundary without touching core execution.

The row schema is declared explicitly on :class:`CostRow` — every
field carries a ``Field(..., description="…")`` so the schema is the
contract. On-disk ``cai-cost.jsonl`` entries are
``CostRow.model_dump(exclude_none=True)``: optional fields that
callers leave unset default to ``None`` and are dropped from the
serialised dict, preserving byte-identical output with the
pre-refactor ``dict``-based shape.

Row construction is owned by :meth:`CostRow.from_result_message`: a
classmethod factory on the data class that derives the flat token
counters, aggregate and per-model ``cacheHitRate``, and SHA256
prompt fingerprint from the SDK's :class:`ResultMessage`. Constructing
fresh :class:`ModelUsage` instances (via ``model_validate``) also
fixes the issue-#1272 comment bug where the legacy builder mutated
``result.model_usage`` in place — polluting
``SubAgent.last_result.model_usage`` with ``cacheHitRate`` keys the
SDK never emitted.
"""

from __future__ import annotations

import hashlib
import socket
from datetime import datetime, timezone

from claude_agent_sdk.types import ResultMessage
from pydantic import BaseModel, ConfigDict, Field


class ModelUsage(BaseModel):
    """Per-model rollup entry inside :attr:`CostRow.models`.

    Mirrors the camelCase keys emitted by the SDK's
    ``ResultMessage.model_usage[model]`` dict. ``extra="allow"`` so
    extra keys added by a future SDK release are preserved without
    schema churn. Every declared field defaults to ``None`` so
    ``model_validate({})`` round-trips to ``{}`` under
    ``model_dump(exclude_none=True)`` — preserving the pre-refactor
    shape for SDK entries that carry no keys.

    ``cacheHitRate`` is the one field derived by
    :meth:`CostRow.from_result_message` (issue #1205); it is omitted
    when the per-model denominator
    (``cacheReadInputTokens + cacheCreationInputTokens + inputTokens``)
    is zero.
    """

    model_config = ConfigDict(extra="allow")

    inputTokens: int | None = Field(
        default=None, description="SDK model_usage[m].inputTokens",
    )
    outputTokens: int | None = Field(
        default=None, description="SDK model_usage[m].outputTokens",
    )
    cacheReadInputTokens: int | None = Field(
        default=None,
        description="SDK model_usage[m].cacheReadInputTokens",
    )
    cacheCreationInputTokens: int | None = Field(
        default=None,
        description="SDK model_usage[m].cacheCreationInputTokens",
    )
    costUSD: float | None = Field(
        default=None,
        description="SDK model_usage[m].costUSD — client-side estimate (see CostRow.cost_usd); omitted when absent.",
    )
    cacheHitRate: float | None = Field(
        default=None,
        description=(
            "Derived per-model: cacheReadInputTokens / "
            "(cacheReadInputTokens + cacheCreationInputTokens + "
            "inputTokens); omitted when that denominator is 0 "
            "(issue #1205)."
        ),
    )


class CostRow(BaseModel):
    """Structured in-memory shape of one cost-log entry.

    :meth:`model_dump(exclude_none=True)` produces the on-disk JSONL
    shape. ``extra="forbid"`` declares the schema as the contract —
    any new optional field must be added here first. Use
    :meth:`from_result_message` to build a :class:`CostRow` from a
    SDK :class:`ResultMessage` plus caller stamps; do not construct
    ``CostRow`` manually outside the factory except for tests that
    need a specific fixture.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    ts: str = Field(..., description="ISO-8601 UTC timestamp at row build time")
    category: str = Field(
        ..., description="cost-log category (e.g. 'refine', 'plan.plan')",
    )
    agent: str = Field(
        ..., description="subagent name (e.g. 'cai-refine')",
    )
    cost_usd: float = Field(
        ..., description="SDK result.total_cost_usd — client-side estimate (price-table at SDK build time); not authoritative billing data, do not use for end-user billing.",
    )
    duration_ms: int = Field(
        ..., description="SDK result.duration_ms (wall)",
    )
    duration_api_ms: int = Field(
        ..., description="SDK result.duration_api_ms",
    )
    num_turns: int = Field(..., description="SDK result.num_turns")
    session_id: str = Field(..., description="SDK result.session_id")
    host: str = Field(
        ..., description="socket.gethostname() of the row producer",
    )
    exit: int = Field(
        ..., description="derived: 1 iff is_error, else 0",
    )
    is_error: bool = Field(..., description="SDK result.is_error")
    prompt_fingerprint: str = Field(
        ...,
        description=(
            "16-char SHA256 prefix of fingerprint_payload (when the "
            "caller supplied one) or (system_prompt + '\\n---\\n' + "
            "prompt). Used for cache-health regression detection "
            "(issue #1207)."
        ),
    )

    input_tokens: int | None = Field(
        default=None, description="SDK usage.input_tokens (optional)",
    )
    output_tokens: int | None = Field(
        default=None, description="SDK usage.output_tokens (optional)",
    )
    cache_creation_input_tokens: int | None = Field(
        default=None,
        description="SDK usage.cache_creation_input_tokens (optional)",
    )
    cache_read_input_tokens: int | None = Field(
        default=None,
        description="SDK usage.cache_read_input_tokens (optional)",
    )

    cache_hit_rate: float | None = Field(
        default=None,
        description=(
            "Derived aggregate: cache_read / "
            "(cache_read + cache_create + input), rounded to 4dp; "
            "omitted when denom=0 (issue #1205)."
        ),
    )

    models: dict[str, ModelUsage] | None = Field(
        default=None,
        description=(
            "Per-model rollup (issue #1205). Omitted when the SDK "
            "returns no per-model data or only non-dict entries."
        ),
    )
    parent_model: str | None = Field(
        default=None,
        description=(
            "Top-level agent model name — first parent-level "
            "AssistantMessage's .model (issue #1205)."
        ),
    )
    subagents: dict[str, int] | None = Field(
        default=None,
        description="Subagent invocation counts keyed by subagent_type.",
    )

    fsm_state: str | None = Field(
        default=None,
        description=(
            "Dispatcher funnel state via the _CURRENT_FSM_STATE "
            "contextvar (issue #1203). Stamped by the repo-specific "
            "emitter layer after construction, before serialisation."
        ),
    )
    module: str | None = Field(
        default=None,
        description="Caller-supplied module name (issue #1206).",
    )
    scope_files: list[str] | None = Field(
        default=None,
        description=(
            "Caller-supplied file list, capped at 10 by "
            ":meth:`from_result_message` (issue #1206)."
        ),
    )
    target_kind: str | None = Field(
        default=None,
        description="'issue' or 'pr' (issue #1210).",
    )
    target_number: int | None = Field(
        default=None,
        description="issue/PR number (issue #1210).",
    )
    fix_attempt_count: int | None = Field(
        default=None,
        description=(
            "Prior-fix-attempt count stamped by implement / revise / "
            "fix-ci; zero is significant (first attempt). Use "
            "`is not None` when reading — never truthiness (issue #1204)."
        ),
    )

    @classmethod
    def from_result_message(
        cls,
        *,
        category: str,
        agent: str,
        result: ResultMessage,
        prompt: str = "",
        system_prompt: str | None = None,
        parent_model: str | None = None,
        subagent_counts: dict[str, int] | None = None,
        fingerprint_payload: str | None = None,
        module: str | None = None,
        scope_files: list[str] | None = None,
        target_kind: str | None = None,
        target_number: int | None = None,
        fix_attempt_count: int | None = None,
    ) -> "CostRow":
        """Build a :class:`CostRow` from an SDK :class:`ResultMessage`.

        Derives the flat token counters, aggregate and per-model
        ``cacheHitRate``, and SHA256 fingerprint. Constructs **fresh**
        :class:`ModelUsage` instances via :meth:`ModelUsage.model_validate`
        so ``result.model_usage`` is never mutated — fixing the issue
        #1272 SDK-dict-mutation bug. Optional stamps default to ``None``
        and are excluded from the serialised dict, preserving the
        pre-refactor row shape for non-participating call sites.
        """
        usage = result.usage or {}
        flat_keys = (
            "input_tokens",
            "output_tokens",
            "cache_creation_input_tokens",
            "cache_read_input_tokens",
        )
        flat = {
            k: usage[k] for k in flat_keys
            if isinstance(usage.get(k), (int, float))
        }
        returncode = 1 if result.is_error else 0

        # Aggregate cache hit rate (issue #1205) — omitted when denom=0.
        cr = flat.get("cache_read_input_tokens") or 0
        cc = flat.get("cache_creation_input_tokens") or 0
        it = flat.get("input_tokens") or 0
        denom = cr + cc + it
        cache_hit_rate = round(cr / denom, 4) if denom > 0 else None

        # Per-model rollup: build a FRESH ModelUsage per SDK entry so
        # ``result.model_usage`` is never mutated in place. Non-dict
        # entries are defensively skipped (the SDK should never emit
        # them). An empty or all-non-dict ``model_usage`` yields
        # ``models=None`` which ``exclude_none=True`` drops.
        models_out: dict[str, ModelUsage] | None = None
        src = result.model_usage if isinstance(result.model_usage, dict) else None
        if src:
            tmp: dict[str, ModelUsage] = {}
            for m, mu in src.items():
                if not isinstance(mu, dict):
                    continue
                fresh = ModelUsage.model_validate(mu)
                m_cr = fresh.cacheReadInputTokens or 0
                m_cc = fresh.cacheCreationInputTokens or 0
                m_it = fresh.inputTokens or 0
                m_denom = m_cr + m_cc + m_it
                if m_denom > 0:
                    fresh.cacheHitRate = round(m_cr / m_denom, 4)
                tmp[m] = fresh
            if tmp:
                models_out = tmp

        # Fingerprint (issue #1207): explicit payload wins, else
        # fall back to (system_prompt + '\n---\n' + prompt).
        fp_src = (
            fingerprint_payload
            if fingerprint_payload is not None
            else (system_prompt or "") + "\n---\n" + (prompt or "")
        )
        fingerprint = hashlib.sha256(fp_src.encode()).hexdigest()[:16]

        return cls(
            ts=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            category=category,
            agent=agent,
            cost_usd=result.total_cost_usd or 0.0,
            duration_ms=result.duration_ms,
            duration_api_ms=result.duration_api_ms,
            num_turns=result.num_turns,
            session_id=result.session_id,
            host=socket.gethostname(),
            exit=returncode,
            is_error=bool(result.is_error),
            prompt_fingerprint=fingerprint,
            input_tokens=flat.get("input_tokens"),
            output_tokens=flat.get("output_tokens"),
            cache_creation_input_tokens=flat.get(
                "cache_creation_input_tokens",
            ),
            cache_read_input_tokens=flat.get("cache_read_input_tokens"),
            cache_hit_rate=cache_hit_rate,
            models=models_out,
            parent_model=parent_model or None,
            subagents=(
                dict(subagent_counts) if subagent_counts else None
            ),
            module=module,
            scope_files=(
                list(scope_files)[:10] if scope_files else None
            ),
            target_kind=target_kind,
            target_number=target_number,
            fix_attempt_count=fix_attempt_count,
        )


class CostTracker(BaseModel):
    """Accumulates :class:`CostRow` instances for a SubAgent; emit is a subclass hook.

    Running totals (:attr:`total_cost_usd`, :attr:`total_duration_ms`,
    :attr:`total_num_turns`) are pure computed properties over
    :attr:`cost_rows` — no duplicated bookkeeping. Target metadata is
    carried on the tracker for subclasses that ship rows to a specific
    issue/PR; the base :meth:`_emit` ignores it.

    Row construction lives on :class:`CostRow` itself (the
    :meth:`CostRow.from_result_message` factory); :meth:`record`
    appends the caller-built row and hands it to :meth:`_emit`. This
    keeps the tracker a pure accumulator and avoids a second copy of
    the construction logic on the class.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    target_kind: str | None = None
    target_number: int | None = None
    extra_target_kind: str | None = None
    extra_target_number: int | None = None

    cost_rows: list[CostRow] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum((r.cost_usd or 0) for r in self.cost_rows)

    @property
    def total_duration_ms(self) -> int:
        return sum((r.duration_ms or 0) for r in self.cost_rows)

    @property
    def total_num_turns(self) -> int:
        return sum((r.num_turns or 0) for r in self.cost_rows)

    def record(self, row: CostRow) -> CostRow:
        """Append a pre-built :class:`CostRow` and invoke :meth:`_emit`.

        Construction logic lives on :meth:`CostRow.from_result_message`
        — this method is a pure accumulator. Returned for chain-friendly
        call sites that want the same reference that was just appended.
        """
        self.cost_rows.append(row)
        self._emit(row, row.agent)
        return row

    def _emit(self, row: CostRow, agent: str) -> None:
        """No-op base implementation — override in repo-specific subclasses."""
        pass
