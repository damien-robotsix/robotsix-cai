"""Agent-SDK execution core: :class:`SubAgent` and its helpers.

:class:`SubAgent` is a Pydantic model: options (and identity:
``category`` + ``agent``) are fixed at construction, and each
:meth:`run` call takes a fresh prompt. One instance can be reused
across many prompts — its cost history accumulates on the embedded
:class:`~cai_lib.subagent.cost_tracker.CostTracker`. Instance state
(``runs``, ``last_result``, ``last_captured_stderr``) survives between
runs and can be introspected between calls.

:func:`run_subagent` stays as a thin module-level shim that constructs
a :class:`SubAgent` (with a :class:`CostTracker` built from the
optional target metadata), calls ``.run(prompt)`` once, and returns
the :class:`subprocess.CompletedProcess`. Existing call sites and test
fixtures (``patch.object(core, "query", ...)``) are unaffected.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from pydantic import BaseModel, ConfigDict, Field

from .cost_tracker import CostTracker
from .errors import _sdk_error_summary
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

    Kept as a module-level function (rather than a :class:`SubAgent`
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


class SubAgent(BaseModel):
    """Reusable typed-options SDK driver — one instance, many runs.

    Options (and identity: ``category`` + ``agent``) are fixed at
    construction. Each :meth:`run` call takes a fresh ``prompt`` and
    returns the :class:`subprocess.CompletedProcess` shape legacy
    callers expect.

    Held state:

    - :attr:`cost_tracker` — embedded :class:`CostTracker`; owns
      ``cost_rows``, running totals, cost-mirror target metadata, and
      GH-comment emission.
    - :attr:`runs` — number of completed :meth:`run` calls (including
      exception and no-ResultMessage paths).
    - :attr:`last_result` — the final :class:`ResultMessage` from the
      most recent successful run, or ``None``.
    - :attr:`last_captured_stderr` — CLI stderr lines from the most
      recent run. Replaced on every run (not accumulated) so callers
      can introspect a single run's sink.

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
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    category: str
    agent: str
    options: ClaudeAgentOptions
    timeout: float | None = None
    cost_tracker: CostTracker = Field(default_factory=CostTracker)

    runs: int = 0
    last_result: ResultMessage | None = None
    last_assistant: str = ""
    last_captured_stderr: list[str] = Field(default_factory=list)

    @property
    def _sentinel_args(self) -> list[str]:
        return ["run_subagent", self.agent]

    def run(self, prompt: str) -> subprocess.CompletedProcess:
        """Drive one full run against ``prompt`` and return the CompletedProcess."""
        self._prepare_options()
        try:
            results, last_assistant, parent_model, subagent_counts = (
                self._drive_query(prompt)
            )
        except Exception as exc:  # noqa: BLE001
            self.runs += 1
            return self._completed_from_exception(exc)

        self.runs += 1
        self.last_assistant = last_assistant

        if not results:
            return self._completed_from_no_results(last_assistant)

        result = results[-1]
        self.last_result = result

        self.cost_tracker.record(
            category=self.category,
            agent=self.agent,
            prompt=prompt,
            system_prompt=self.options.system_prompt,
            result=result,
            parent_model=parent_model,
            subagent_counts=subagent_counts,
        )

        stdout = self._extract_stdout(result, last_assistant)
        return self._to_completed_process(result, stdout)

    def _prepare_options(self) -> None:
        """Pin cli_path, auto-inject cai-skills plugin, attach a fresh stderr sink.

        Preserves the implicit ``cai-skills`` injection that
        ``_argv_to_options`` (legacy.py:102-104) does for the argv path
        and the ``cli_path`` pin that every call path needs to reuse
        the npm-installed ``claude`` binary. Resets
        :attr:`last_captured_stderr` to a fresh list each run so a
        reused instance does not leak stderr lines across runs.
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

        self.last_captured_stderr = []
        self.options.stderr = _make_stderr_sink(self.last_captured_stderr)

    def _drive_query(
        self,
        prompt: str,
    ) -> tuple[list[ResultMessage], str, str | None, dict[str, int]]:
        """Run :func:`_collect_results` to completion, honouring timeout."""
        if self.timeout is not None:
            return asyncio.run(
                asyncio.wait_for(
                    _collect_results(prompt, self.options),
                    timeout=self.timeout,
                )
            )
        return asyncio.run(_collect_results(prompt, self.options))

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
        cli_stderr = _captured_stderr_text(self.last_captured_stderr)
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
        cli_stderr = _captured_stderr_text(self.last_captured_stderr)
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
    """SDK-native subagent invocation — one-shot shim over :class:`SubAgent`.

    Kept as a module-level function so existing call sites
    (``actions/confirm.py``) and test fixtures that do
    ``patch.object(core, "query", ...)`` keep their import shape
    unchanged. New call sites that want to reuse one agent across
    multiple prompts — and accumulate ``cost_tracker.cost_rows`` —
    should construct :class:`SubAgent` directly.
    """
    tracker = CostTracker(
        target_kind=target_kind,
        target_number=target_number,
        extra_target_kind=extra_target_kind,
        extra_target_number=extra_target_number,
    )
    return SubAgent(
        options=options,
        category=category,
        agent=agent,
        timeout=timeout,
        cost_tracker=tracker,
    ).run(prompt)
