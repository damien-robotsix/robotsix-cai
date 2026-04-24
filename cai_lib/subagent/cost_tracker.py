"""Cost-row recorder for :class:`cai_lib.subagent.core.SubAgent`.

A :class:`SubAgent` holds one :class:`CostTracker` that accumulates a
``log_cost`` row per successful run and, when target metadata is set,
mirrors the row as a GH comment on the relevant issue or PR.

Kept in its own module so :mod:`cai_lib.subagent.core` stays focused on
the SDK query loop and CompletedProcess shape, and so tests can patch
``log_cost`` at the boundary the row actually crosses.
"""

from __future__ import annotations

import hashlib
import socket
from datetime import datetime, timezone

from claude_agent_sdk.types import ResultMessage
from pydantic import BaseModel, ConfigDict, Field


class CostTracker(BaseModel):
    """Accumulates cost rows for a SubAgent and mirrors them as GH comments.

    Running totals (:attr:`total_cost_usd`, :attr:`total_duration_ms`,
    :attr:`total_num_turns`) are pure computed properties over
    :attr:`cost_rows` — no duplicated bookkeeping. Target metadata is
    optional; when ``target_kind`` and ``target_number`` are both set,
    each recorded row is also posted as a cost-mirror comment on that
    target (and the optional extra target).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    target_kind: str | None = None
    target_number: int | None = None
    extra_target_kind: str | None = None
    extra_target_number: int | None = None

    cost_rows: list[dict] = Field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return sum((r.get("cost_usd") or 0) for r in self.cost_rows)

    @property
    def total_duration_ms(self) -> int:
        return sum((r.get("duration_ms") or 0) for r in self.cost_rows)

    @property
    def total_num_turns(self) -> int:
        return sum((r.get("num_turns") or 0) for r in self.cost_rows)

    def record(
        self,
        *,
        category: str,
        agent: str,
        prompt: str,
        system_prompt: str | None,
        result: ResultMessage,
        parent_model: str | None,
        subagent_counts: dict[str, int],
    ) -> dict:
        """Build one cost row, append it to :attr:`cost_rows`, log + mirror it."""
        row = self._build_row(
            category=category,
            agent=agent,
            prompt=prompt,
            system_prompt=system_prompt,
            result=result,
            parent_model=parent_model,
            subagent_counts=subagent_counts,
        )
        self.cost_rows.append(row)
        self._emit(row, agent)
        return row

    def _build_row(
        self,
        *,
        category: str,
        agent: str,
        prompt: str,
        system_prompt: str | None,
        result: ResultMessage,
        parent_model: str | None,
        subagent_counts: dict[str, int],
    ) -> dict:
        """Assemble the log_cost row from the final ResultMessage."""
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
        models = (
            result.model_usage if isinstance(result.model_usage, dict) else {}
        )
        returncode = 1 if result.is_error else 0

        row: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": category,
            "agent": agent,
            "cost_usd": result.total_cost_usd,
            "duration_ms": result.duration_ms,
            "duration_api_ms": result.duration_api_ms,
            "num_turns": result.num_turns,
            "session_id": result.session_id,
            "host": socket.gethostname(),
            "exit": returncode,
            "is_error": bool(result.is_error),
        }
        row.update(flat)
        cr = flat.get("cache_read_input_tokens") or 0
        cc = flat.get("cache_creation_input_tokens") or 0
        it = flat.get("input_tokens") or 0
        denom = cr + cc + it
        if denom > 0:
            row["cache_hit_rate"] = round(cr / denom, 4)
        if models:
            for _m, mu in models.items():
                if not isinstance(mu, dict):
                    continue
                m_cr = mu.get("cacheReadInputTokens") or 0
                m_cc = mu.get("cacheCreationInputTokens") or 0
                m_it = mu.get("inputTokens") or 0
                m_denom = m_cr + m_cc + m_it
                if m_denom > 0:
                    mu["cacheHitRate"] = round(m_cr / m_denom, 4)
            row["models"] = models
        if parent_model:
            row["parent_model"] = parent_model
        if subagent_counts:
            row["subagents"] = dict(subagent_counts)
        fp_src = (system_prompt or "") + "\n---\n" + (prompt or "")
        row["prompt_fingerprint"] = hashlib.sha256(
            fp_src.encode()
        ).hexdigest()[:16]
        return row

    def _emit(self, row: dict, agent: str) -> None:
        """No-op base implementation — override in repo-specific subclasses."""
        pass
