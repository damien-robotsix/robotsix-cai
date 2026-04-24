"""Agent-SDK execution core: ``run_subagent`` and its helpers.

Owns the typed-options call path introduced in the #1226 spike plus
the two shared helpers every call path depends on: the top-level
query driver ``_collect_results`` and the ``cli_path`` pin that keeps
the SDK pointed at the npm-installed ``claude`` binary audited in
Dockerfile rather than the copy bundled with the SDK wheel.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from claude_agent_sdk import ClaudeAgentOptions, query
    from claude_agent_sdk.types import (
        AssistantMessage,
        ResultMessage,
        TextBlock,
        ToolUseBlock,
    )
    _SDK_AVAILABLE = True
except ImportError:  # pragma: no cover
    _SDK_AVAILABLE = False

from cai_lib.logging_utils import log_cost

from .cost import _post_cost_comment
from .errors import _sdk_error_summary
from .fsm_state import _CURRENT_FSM_STATE
from .stderr_sink import _captured_stderr_text, _make_stderr_sink


# Resolve the CLI path once at import time so the SDK reuses the
# npm-installed `claude` binary audited in Dockerfile instead of the
# copy bundled with the SDK wheel.
_CLI_PATH = shutil.which("claude")


async def _collect_results(
    prompt: str,
    options: ClaudeAgentOptions,
) -> tuple[list[ResultMessage], str, str | None, dict[str, int]]:
    """Drive ``query()`` to completion.

    Returns ``(result_messages, last_non_empty_assistant_text,
    parent_model, subagent_counts)``. Collects every ResultMessage
    (forward-compat: today the CLI emits exactly one) and records the
    final non-empty ``AssistantMessage`` TextBlock so the priority-4
    stdout-salvage path can fall back to it when ``result`` is absent
    (e.g. ``subtype == "error_max_budget_usd"``).

    ``parent_model`` is the model of the first ``AssistantMessage`` whose
    ``parent_tool_use_id is None`` — i.e. the top-level agent. The SDK's
    ``ResultMessage.model_usage`` aggregates every model a run touched
    (parent + any Task subagents + Claude Code's own haiku-backed helpers
    like the memory-project loader), so a bare ``next(iter(model_usage))``
    can mislabel the run with a subagent's haiku instead of the parent's
    opus. ``parent_model`` lets the cost-comment renderer pick the right
    one deterministically.

    ``subagent_counts`` maps ``subagent_type`` → invocation count, built
    from every ``ToolUseBlock`` with ``name == "Task"``. Counts every
    spawn, including nested Task calls from subagents and multiple
    invocations of the same ``subagent_type``. A ``Task`` call with no
    explicit ``subagent_type`` is bucketed as ``"general-purpose"``
    (Claude Code's documented default).
    """
    results: list[ResultMessage] = []
    last_assistant = ""
    parent_model: str | None = None
    subagent_counts: dict[str, int] = {}
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            results.append(msg)
        elif isinstance(msg, AssistantMessage):
            if parent_model is None and msg.parent_tool_use_id is None:
                parent_model = msg.model or None
            parts = [
                b.text for b in msg.content
                if isinstance(b, TextBlock) and b.text.strip()
            ]
            if parts:
                last_assistant = "".join(parts).strip()
            for block in msg.content:
                if isinstance(block, ToolUseBlock) and block.name == "Task":
                    sub = (block.input or {}).get("subagent_type") \
                        or "general-purpose"
                    subagent_counts[sub] = subagent_counts.get(sub, 0) + 1
    return results, last_assistant, parent_model, subagent_counts


def run_subagent(
    prompt: str,
    options: ClaudeAgentOptions,
    *,
    category: str,
    agent: str,
    target_kind: str | None = None,
    target_number: int | None = None,
    extra_target_kind: str | None = None,
    extra_target_number: int | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess:
    """SDK-native sibling of :func:`_run_claude_p` (issue #1226 spike).

    Accepts a typed :class:`ClaudeAgentOptions` directly instead of
    round-tripping through argv. Owns every cross-cutting concern the
    facade owns — cost-row emission, cost-mirror posting, stderr sink,
    FSM-state stamping, ``cli_path`` pinning, ``cai-skills`` plugin
    auto-inject, optional ``timeout`` — so handlers ported off
    ``_run_claude_p`` keep their downstream consumers (cost-optimize,
    cost comments, audit pipelines, the implement subagent_failed
    diagnostic) byte-for-byte equivalent.

    Returns a :class:`subprocess.CompletedProcess` whose contract
    mirrors :func:`_run_claude_p` exactly:
      - ``.stdout`` carries ``structured_output`` (JSON-encoded) when
        present, ``""`` on
        ``subtype == "error_max_structured_output_retries"`` with a
        diagnostic stderr line, ``result`` text otherwise, falling
        back to the last assistant text when ``result`` is absent.
      - ``.returncode`` is 1 on any exception or when ``is_error`` is
        True; 0 otherwise.
      - ``.args`` is a sentinel ``["run_subagent", agent]`` — no
        caller in the spiked path inspects ``args``.

    Do NOT modify :func:`_run_claude_p` to delegate to this helper —
    the spike requires both code paths to coexist for the
    ``tests/test_sdk_spike_parity.py`` regression check.
    """
    if _CLI_PATH and not getattr(options, "cli_path", None):
        options.cli_path = _CLI_PATH

    # Auto-inject the cai-skills plugin when the directory exists at
    # the caller's cwd — preserves the implicit injection that
    # ``_argv_to_options`` (lines 183-187) does for the argv path.
    skills_plugin = Path(".claude/plugins/cai-skills")
    if skills_plugin.is_dir():
        if options.plugins is None:
            options.plugins = []
        already = any(
            isinstance(p, dict) and p.get("path") == str(skills_plugin)
            for p in options.plugins
        )
        if not already:
            options.plugins.append(
                {"type": "local", "path": str(skills_plugin)}
            )

    captured_stderr: list[str] = []
    options.stderr = _make_stderr_sink(captured_stderr)

    sentinel_args = ["run_subagent", agent]

    try:
        if timeout is not None:
            results, last_assistant, parent_model, subagent_counts = \
                asyncio.run(
                    asyncio.wait_for(
                        _collect_results(prompt, options), timeout=timeout,
                    )
                )
        else:
            results, last_assistant, parent_model, subagent_counts = \
                asyncio.run(_collect_results(prompt, options))
    except Exception as exc:  # noqa: BLE001
        preview = str(exc)[:200].replace("\n", " ")
        cli_stderr = _captured_stderr_text(captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] claude-agent-sdk query failed "
            f"({category}/{agent}): {preview}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = str(exc)
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=sentinel_args, returncode=1, stdout="", stderr=combined,
        )

    if not results:
        preview = (last_assistant or "")[:120].replace("\n", " ")
        cli_stderr = _captured_stderr_text(captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] no ResultMessage from claude-agent-sdk "
            f"({category}/{agent}); last assistant starts with: {preview!r}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = f"no_ResultMessage last_assistant={preview!r}"
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=sentinel_args, returncode=1,
            stdout=last_assistant or "", stderr=combined,
        )

    result = results[-1]
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
    models = result.model_usage if isinstance(result.model_usage, dict) else {}
    returncode = 1 if result.is_error else 0

    row = {
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
    fsm_state = _CURRENT_FSM_STATE.get()
    if fsm_state:
        row["fsm_state"] = fsm_state
    fp_src = (
        (options.system_prompt or "") + "\n---\n" + (prompt or "")
    )
    row["prompt_fingerprint"] = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
    log_cost(row)

    if target_kind is not None and target_number is not None:
        _post_cost_comment(target_kind, target_number, row, agent)
    if extra_target_kind is not None and extra_target_number is not None:
        _post_cost_comment(extra_target_kind, extra_target_number, row, agent)

    if result.structured_output is not None:
        stdout = json.dumps(result.structured_output)
    elif result.subtype == "error_max_structured_output_retries":
        print(
            f"[cai cost] structured output retries exhausted "
            f"({category}/{agent}); schema was not satisfied",
            file=sys.stderr, flush=True,
        )
        stdout = ""
    elif isinstance(result.result, str):
        stdout = result.result
    else:
        stdout = last_assistant

    stderr = ""
    if returncode != 0:
        stderr = _sdk_error_summary(result)
    return subprocess.CompletedProcess(
        args=sentinel_args, returncode=returncode,
        stdout=stdout, stderr=stderr,
    )
