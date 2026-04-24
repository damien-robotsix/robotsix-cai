"""Cost-row build helpers and cost-attribution comment posting.

Owns the split-by-category helper, the ``<!-- cai-cost … -->`` marker
format, and the best-effort issue/PR comment post that fires after
``log_cost(row)`` when ``_run_claude_p``/``run_subagent`` are called
with a target kind and number. See ``cai_lib.config.CAI_COST_COMMENT_RE``
and ``_strip_cost_comments`` for the matching reader side.

Previously located at :mod:`cai_lib.subagent.cost`; moved here to
decouple the base :mod:`cai_lib.subagent` package from repo-specific
dependencies (issue #1269).
"""

from __future__ import annotations

import sys


# Maximum length of the machine-parsable marker body on a cost comment.
# The marker carries a handful of short key=value tokens; cap defensively
# so a huge ``agent`` or ``category`` string cannot blow past GitHub's
# 65 536-char comment limit.
_COST_COMMENT_MAX_CHARS = 800


# Claude 4.x pricing ratios relative to the input-token rate. These
# hold across Opus / Sonnet / Haiku and across 200k / 1M context
# windows because Anthropic scales all four rates uniformly per model.
# Used to split a known ``costUSD`` into per-category dollars without
# having to hardcode per-model prices (which drift with new model
# releases). The breakdown is informational — the authoritative total
# is still ``costUSD`` as reported by the SDK.
_TOKEN_COST_RATIOS = {
    "input": 1.0,
    "output": 5.0,
    "cache_read": 0.1,
    "cache_write": 1.25,
}


def _split_cost_by_category(
    total_cost: float,
    in_tokens: int,
    out_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> dict[str, float]:
    """Split ``total_cost`` across the four token-category buckets.

    Uses the fixed Claude 4.x pricing ratios (see ``_TOKEN_COST_RATIOS``).
    Returns zeros everywhere when the weighted-token denominator is zero
    — the only case the caller needs to guard.
    """
    weighted = (
        in_tokens          * _TOKEN_COST_RATIOS["input"]
        + out_tokens       * _TOKEN_COST_RATIOS["output"]
        + cache_read_tokens  * _TOKEN_COST_RATIOS["cache_read"]
        + cache_write_tokens * _TOKEN_COST_RATIOS["cache_write"]
    )
    if weighted <= 0 or total_cost <= 0:
        return {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}
    scale = total_cost / weighted
    return {
        "input":       in_tokens          * _TOKEN_COST_RATIOS["input"]       * scale,
        "output":      out_tokens         * _TOKEN_COST_RATIOS["output"]      * scale,
        "cache_read":  cache_read_tokens  * _TOKEN_COST_RATIOS["cache_read"]  * scale,
        "cache_write": cache_write_tokens * _TOKEN_COST_RATIOS["cache_write"] * scale,
    }


def _post_cost_comment(
    target_kind: str,
    target_number: int,
    row: dict,
    agent: str,
) -> None:
    """Best-effort post of a cost-attribution comment on an issue or PR.

    Runs immediately after ``log_cost(row)`` when ``_run_claude_p`` is
    called with both ``target_kind`` and ``target_number`` set. The
    comment body has a machine-parsable ``<!-- cai-cost … -->`` HTML
    marker (matched by ``CAI_COST_COMMENT_RE`` in ``cai_lib.config``
    and stripped out of agent-input comment streams by
    ``_strip_cost_comments``) followed by a short human-readable
    summary line so humans scanning the issue/PR see the cost in the
    GitHub UI without the marker leaking back into subsequent agent
    prompts.

    Swallows every exception: a failed ``gh issue comment`` / ``gh pr
    comment`` must never change the returned ``CompletedProcess`` or
    the wrapped agent's behaviour — the cost comment is informational
    context, not a gating signal.
    """
    try:
        from cai_lib.github import _post_issue_comment, _post_pr_comment
    except Exception as exc:  # noqa: BLE001 — defensive import guard
        print(
            f"[cai cost] failed to import comment helpers: {exc}",
            file=sys.stderr, flush=True,
        )
        return

    try:
        cost_usd = float(row.get("cost_usd") or 0.0)
        turns = int(row.get("num_turns") or 0)
        duration_ms = int(row.get("duration_ms") or 0)
        in_tokens = int(row.get("input_tokens") or 0)
        out_tokens = int(row.get("output_tokens") or 0)
        is_error = bool(row.get("is_error"))
        category = str(row.get("category") or "")
        ts = str(row.get("ts") or "")
        models_field = row.get("models") or {}
        # Prefer the parent/top-level agent's model (captured from the
        # first ``AssistantMessage`` with ``parent_tool_use_id is None``)
        # over ``next(iter(model_usage))``. The SDK's ``model_usage``
        # aggregates parent + subagents + built-in helpers (e.g. the
        # haiku-backed ``memory: project`` loader), so picking the first
        # key would mislabel opus-configured agents like ``cai-refine``
        # / ``cai-split`` / ``cai-plan`` with whichever haiku subagent
        # fired first.
        parent_model = str(row.get("parent_model") or "")
        primary_model = parent_model
        if not primary_model and isinstance(models_field, dict) \
                and models_field:
            primary_model = next(iter(models_field.keys()))
        subagent_models: list[str] = []
        if isinstance(models_field, dict) and models_field:
            subagent_models = sorted(
                m for m in models_field.keys() if m and m != primary_model
            )
        subagents_invoked = row.get("subagents") or {}
        if not isinstance(subagents_invoked, dict):
            subagents_invoked = {}
        subagents_field = ",".join(subagent_models)
        subagents_invoked_field = ",".join(
            f"{name}:{count}"
            for name, count in sorted(subagents_invoked.items())
        )
        marker = (
            f"<!-- cai-cost agent={agent} category={category} "
            f"model={primary_model} cost_usd={cost_usd:.4f} "
            f"turns={turns} duration_ms={duration_ms} "
            f"input_tokens={in_tokens} output_tokens={out_tokens} "
            f"is_error={is_error} ts={ts}"
        )
        if subagents_field:
            marker += f" subagent_models={subagents_field}"
        if subagents_invoked_field:
            marker += f" subagents_invoked={subagents_invoked_field}"
        marker += " -->"
        if len(marker) > _COST_COMMENT_MAX_CHARS:
            marker = marker[: _COST_COMMENT_MAX_CHARS - 4] + " -->"
        seconds = duration_ms / 1000.0
        summary_line = (
            f"**Agent cost:** `{agent or '(no agent)'}` on "
            f"`{primary_model or 'unknown'}` — "
            f"${cost_usd:.4f} / {turns} turn(s) / {seconds:.1f}s "
            f"(category=`{category}`)"
        )
        detail_lines: list[str] = []
        if isinstance(models_field, dict) and models_field:
            for m in sorted(
                models_field.keys(),
                key=lambda k: (k != primary_model, k),
            ):
                mu = models_field.get(m) or {}
                if not isinstance(mu, dict):
                    continue
                m_cost = float(mu.get("costUSD") or 0.0)
                m_in = int(mu.get("inputTokens") or 0)
                m_out = int(mu.get("outputTokens") or 0)
                m_cache_read = int(mu.get("cacheReadInputTokens") or 0)
                m_cache_create = int(
                    mu.get("cacheCreationInputTokens") or 0
                )
                role = "parent" if m == primary_model else "subagent"
                split = _split_cost_by_category(
                    m_cost, m_in, m_out, m_cache_read, m_cache_create,
                )
                detail_lines.append(
                    f"- `{m}` ({role}): ${m_cost:.4f} — "
                    f"in={m_in} (${split['input']:.4f}) / "
                    f"out={m_out} (${split['output']:.4f}) / "
                    f"cache_read={m_cache_read} (${split['cache_read']:.4f}) / "
                    f"cache_create={m_cache_create} (${split['cache_write']:.4f})"
                )
        if subagents_invoked:
            inv_parts = ", ".join(
                f"`{name}` ×{count}"
                for name, count in sorted(subagents_invoked.items())
            )
            detail_lines.append(f"- subagents invoked: {inv_parts}")
        body = summary_line
        if detail_lines:
            body += "\n\n" + "\n".join(detail_lines)
        body = f"{marker}\n{body}"
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cai cost] failed to format cost comment for "
            f"{target_kind} #{target_number}: {exc}",
            file=sys.stderr, flush=True,
        )
        return

    try:
        if target_kind == "issue":
            _post_issue_comment(target_number, body, log_prefix="cai cost")
        elif target_kind == "pr":
            _post_pr_comment(target_number, body, log_prefix="cai cost")
        else:
            print(
                f"[cai cost] unknown target_kind={target_kind!r}; "
                f"skipping cost comment",
                file=sys.stderr, flush=True,
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[cai cost] failed to post cost comment on "
            f"{target_kind} #{target_number}: {exc}",
            file=sys.stderr, flush=True,
        )
