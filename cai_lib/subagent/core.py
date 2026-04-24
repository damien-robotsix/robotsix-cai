"""Agent-SDK execution core: :class:`SubagentRun` and its helpers.

Owns the typed-options SDK call path (issue #1226 spike) plus the two
shared helpers every call path depends on: the top-level query driver
:func:`_collect_results` and the :data:`_CLI_PATH` pin that keeps the
SDK pointed at the npm-installed ``claude`` binary audited in
Dockerfile rather than the copy bundled with the SDK wheel.

:class:`SubagentRun` encapsulates one full run: option preparation, the
async ``query()`` loop, cost-row build, cost-log + cost-comment emission,
stdout salvage priority, and the final :class:`subprocess.CompletedProcess`
shape. :func:`run_subagent` stays as a thin module-level shim over
``SubagentRun(...).execute()`` so existing callers (and the
``patch.object(core, "query", ...)`` test fixtures) keep working
byte-for-byte. The class shape is the adapter surface for the planned
LangGraph node wiring tracked in #1223 — a node body can construct
``SubagentRun`` directly and introspect individual phases (cost row
before stdout extraction, etc.) instead of funnelling through the
function's single return value.
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

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from cai_lib.utils.log import log_cost

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

    Kept as a module-level function (rather than a :class:`SubagentRun`
    method) because :mod:`cai_lib.subagent.legacy` imports it directly
    to drive the deprecated ``_run_claude_p`` argv facade without
    pulling in the cost-row / cost-comment / FSM-state plumbing the
    class owns.
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


class SubagentRun:
    """One typed-options SDK call: option prep, query loop, cost, stdout.

    Phases (each a method, all called in order by :meth:`execute`):

    1. :meth:`_prepare_options` — pin ``cli_path``, auto-inject the
       ``cai-skills`` plugin when present, attach the stderr sink.
    2. :meth:`_drive_query` — run :func:`_collect_results` to completion,
       honouring the optional ``timeout``.
    3. :meth:`_build_cost_row` — assemble the ``log_cost`` row from the
       final :class:`ResultMessage` (flat token keys, per-model rollup
       with pre-computed ``cacheHitRate``, ``parent_model``, subagent
       counts, FSM-state stamp, 16-char prompt fingerprint).
    4. :meth:`_emit_cost` — :func:`log_cost` + best-effort
       :func:`_post_cost_comment` on target and extra-target.
    5. :meth:`_extract_stdout` — priority chain: structured output →
       retry-exhausted diagnostic → ``result`` text → last-assistant
       salvage.
    6. :meth:`_to_completed_process` — wrap into
       :class:`subprocess.CompletedProcess` with the same contract
       :func:`cai_lib.subagent.legacy._run_claude_p` returns.

    The returned :class:`subprocess.CompletedProcess` contract:

    - ``.stdout`` carries ``structured_output`` (JSON-encoded) when
      present; ``""`` on
      ``subtype == "error_max_structured_output_retries"`` with a
      diagnostic stderr line; ``result`` text otherwise; falling back
      to the last assistant text when ``result`` is absent (e.g.
      ``subtype == "error_max_budget_usd"``).
    - ``.returncode`` is 1 on any exception or when ``is_error`` is
      True; 0 otherwise.
    - ``.args`` is the sentinel ``["run_subagent", agent]`` — no
      caller inspects ``args``.

    Instance attributes populated during :meth:`execute`:
    ``_captured_stderr`` (the CLI stderr sink buffer). Intermediate
    results (``results``, ``last_assistant``, ``parent_model``,
    ``subagent_counts``) are returned by :meth:`_drive_query` and
    consumed by later phases; they are NOT stashed on ``self`` so a
    ``SubagentRun`` instance can be reused (though the common pattern
    is one-shot via :func:`run_subagent`).
    """

    def __init__(
        self,
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
    ) -> None:
        self.prompt = prompt
        self.options = options
        self.category = category
        self.agent = agent
        self.target_kind = target_kind
        self.target_number = target_number
        self.extra_target_kind = extra_target_kind
        self.extra_target_number = extra_target_number
        self.timeout = timeout
        self._captured_stderr: list[str] = []
        self._sentinel_args: list[str] = ["run_subagent", agent]

    def execute(self) -> subprocess.CompletedProcess:
        """Drive the full run and return the CompletedProcess."""
        self._prepare_options()
        try:
            results, last_assistant, parent_model, subagent_counts = (
                self._drive_query()
            )
        except Exception as exc:  # noqa: BLE001
            return self._completed_from_exception(exc)

        if not results:
            return self._completed_from_no_results(last_assistant)

        result = results[-1]
        row = self._build_cost_row(result, parent_model, subagent_counts)
        self._emit_cost(row)
        stdout = self._extract_stdout(result, last_assistant)
        return self._to_completed_process(result, stdout)

    def _prepare_options(self) -> None:
        """Pin cli_path, auto-inject cai-skills plugin, attach stderr sink.

        Preserves the implicit ``cai-skills`` injection that
        ``_argv_to_options`` (legacy.py:102-104) does for the argv path
        and the ``cli_path`` pin that every call path needs to reuse
        the npm-installed ``claude`` binary.
        """
        if _CLI_PATH and not getattr(self.options, "cli_path", None):
            self.options.cli_path = _CLI_PATH

        skills_plugin = Path(".claude/plugins/cai-skills")
        if skills_plugin.is_dir():
            if self.options.plugins is None:
                self.options.plugins = []
            already = any(
                isinstance(p, dict) and p.get("path") == str(skills_plugin)
                for p in self.options.plugins
            )
            if not already:
                self.options.plugins.append(
                    {"type": "local", "path": str(skills_plugin)}
                )

        self.options.stderr = _make_stderr_sink(self._captured_stderr)

    def _drive_query(
        self,
    ) -> tuple[list[ResultMessage], str, str | None, dict[str, int]]:
        """Run :func:`_collect_results` to completion, honouring timeout."""
        if self.timeout is not None:
            return asyncio.run(
                asyncio.wait_for(
                    _collect_results(self.prompt, self.options),
                    timeout=self.timeout,
                )
            )
        return asyncio.run(_collect_results(self.prompt, self.options))

    def _build_cost_row(
        self,
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
            "category": self.category,
            "agent": self.agent,
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
            (self.options.system_prompt or "") + "\n---\n" + (self.prompt or "")
        )
        row["prompt_fingerprint"] = hashlib.sha256(
            fp_src.encode()
        ).hexdigest()[:16]
        return row

    def _emit_cost(self, row: dict) -> None:
        """Append the cost row to the jsonl log and mirror as a GH comment."""
        log_cost(row)
        if self.target_kind is not None and self.target_number is not None:
            _post_cost_comment(
                self.target_kind, self.target_number, row, self.agent,
            )
        if (
            self.extra_target_kind is not None
            and self.extra_target_number is not None
        ):
            _post_cost_comment(
                self.extra_target_kind, self.extra_target_number,
                row, self.agent,
            )

    def _extract_stdout(
        self, result: ResultMessage, last_assistant: str,
    ) -> str:
        """Stdout priority: structured → retry-exhausted → result → salvage."""
        if result.structured_output is not None:
            return json.dumps(result.structured_output)
        if result.subtype == "error_max_structured_output_retries":
            print(
                f"[cai cost] structured output retries exhausted "
                f"({self.category}/{self.agent}); schema was not satisfied",
                file=sys.stderr, flush=True,
            )
            return ""
        if isinstance(result.result, str):
            return result.result
        return last_assistant

    def _to_completed_process(
        self, result: ResultMessage, stdout: str,
    ) -> subprocess.CompletedProcess:
        """Wrap the successful-run outputs into a CompletedProcess."""
        returncode = 1 if result.is_error else 0
        stderr = _sdk_error_summary(result) if returncode != 0 else ""
        return subprocess.CompletedProcess(
            args=self._sentinel_args, returncode=returncode,
            stdout=stdout, stderr=stderr,
        )

    def _completed_from_exception(
        self, exc: Exception,
    ) -> subprocess.CompletedProcess:
        """CompletedProcess for the SDK-raised-exception path."""
        preview = str(exc)[:200].replace("\n", " ")
        cli_stderr = _captured_stderr_text(self._captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] claude-agent-sdk query failed "
            f"({self.category}/{self.agent}): {preview}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = str(exc)
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=self._sentinel_args, returncode=1,
            stdout="", stderr=combined,
        )

    def _completed_from_no_results(
        self, last_assistant: str,
    ) -> subprocess.CompletedProcess:
        """CompletedProcess for the empty-ResultMessage-list path."""
        preview = (last_assistant or "")[:120].replace("\n", " ")
        cli_stderr = _captured_stderr_text(self._captured_stderr)
        cli_stderr_preview = cli_stderr.replace("\n", " | ")[:400]
        msg = (
            f"[cai cost] no ResultMessage from claude-agent-sdk "
            f"({self.category}/{self.agent}); last assistant starts with: "
            f"{preview!r}"
        )
        if cli_stderr_preview:
            msg += f" | cli_stderr={cli_stderr_preview!r}"
        print(msg, file=sys.stderr, flush=True)
        combined = f"no_ResultMessage last_assistant={preview!r}"
        if cli_stderr:
            combined = f"{combined}\n--- cli stderr ---\n{cli_stderr}"
        return subprocess.CompletedProcess(
            args=self._sentinel_args, returncode=1,
            stdout=last_assistant or "", stderr=combined,
        )


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
    """SDK-native subagent invocation — thin shim over :class:`SubagentRun`.

    Kept as a module-level function so existing call sites
    (``actions/confirm.py``) and test fixtures that do
    ``patch.object(core, "query", ...)`` keep their import shape
    unchanged. New call sites and the planned LangGraph node adapter
    (#1223) should construct :class:`SubagentRun` directly when they
    need access to individual phases (cost row before stdout
    extraction, introspection of ``_captured_stderr``, etc.).
    """
    return SubagentRun(
        prompt, options,
        category=category, agent=agent,
        target_kind=target_kind, target_number=target_number,
        extra_target_kind=extra_target_kind,
        extra_target_number=extra_target_number,
        timeout=timeout,
    ).execute()
