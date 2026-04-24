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
the :class:`RunResult`. Existing call sites and test fixtures
(``patch.object(core, "query", ...)``) are unaffected.
"""

from __future__ import annotations

import asyncio
import json
import logging
from enum import StrEnum

from claude_agent_sdk import ClaudeAgentOptions, query
from claude_agent_sdk.types import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from pydantic import BaseModel, ConfigDict, Field

from .cost_tracker import CostRow, CostTracker
from .errors import _sdk_error_summary
from .stderr_sink import _captured_stderr_text, _make_stderr_sink


class RunStatus(StrEnum):
    """Outcome classification for a :class:`SubAgent` run."""

    OK = "ok"
    """Result present and ``is_error`` is False."""
    SDK_ERROR = "sdk_error"
    """Result present but ``is_error`` is True (e.g. ``error_max_turns``)."""
    NO_RESULT = "no_result"
    """``query()`` produced no :class:`ResultMessage`."""
    EXCEPTION = "exception"
    """``query()`` raised an exception."""


class RunResult(BaseModel):
    """Typed result returned by :meth:`SubAgent.run`.

    Replaces the legacy :class:`subprocess.CompletedProcess` shape so
    callers can inspect the structured :class:`ResultMessage`, the error
    subtype, and the raw CLI stderr lines directly — without re-parsing
    opaque ``.stdout`` / ``.stderr`` strings.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    status: RunStatus = Field(..., description="Outcome classification.")
    stdout: str = Field(..., description="Extracted agent stdout (same priority order as before).")
    result: ResultMessage | None = Field(
        None, description="The final ResultMessage when present; None on exception or no-result."
    )
    error_summary: str | None = Field(
        None,
        description=(
            "_sdk_error_summary(result) on SDK_ERROR; str(exc) on EXCEPTION; "
            "no_ResultMessage preview on NO_RESULT; None on OK."
        ),
    )
    captured_stderr: list[str] = Field(
        default_factory=list,
        description="Raw CLI stderr lines collected during this run.",
    )

    @property
    def ok(self) -> bool:
        """True when :attr:`status` is :attr:`RunStatus.OK`."""
        return self.status == RunStatus.OK


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

    Kept as a module-level function for backwards import stability;
    :class:`SubAgent` is the primary consumer. Before #1274 the
    deprecated ``_run_claude_p`` argv facade imported it directly;
    post-#1274 the facade delegates to :class:`SubAgent` and no
    longer touches this helper.
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
    returns a :class:`RunResult`.

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

    The returned :class:`RunResult` contract:

    - ``.stdout`` carries ``structured_output`` (JSON-encoded) when
      present; ``""`` on
      ``subtype == "error_max_structured_output_retries"`` with a
      diagnostic log line; ``result`` text otherwise; falling back
      to the last assistant text when ``result`` is absent (e.g.
      ``subtype == "error_max_budget_usd"``).
    - ``.ok`` is ``True`` when ``status == RunStatus.OK`` (result
      present, ``is_error`` False); ``False`` on any error path.
    - ``.error_summary`` is ``None`` on success; the SDK error text,
      exception repr, or no-result preview otherwise.
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

    def run(self, prompt: str) -> RunResult:
        """Drive one full run against ``prompt`` and return the RunResult."""
        self._prepare_options()
        try:
            results, last_assistant, parent_model, subagent_counts = (
                self._drive_query(prompt)
            )
        except Exception as exc:  # noqa: BLE001
            self.runs += 1
            return self._to_run_result(exc=exc)

        self.runs += 1
        self.last_assistant = last_assistant

        if not results:
            return self._to_run_result(stdout=last_assistant or "")

        result = results[-1]
        self.last_result = result

        self.cost_tracker.record(CostRow.from_result_message(
            category=self.category,
            agent=self.agent,
            prompt=prompt,
            system_prompt=self.options.system_prompt,
            result=result,
            parent_model=parent_model,
            subagent_counts=subagent_counts,
        ))

        stdout = self._extract_stdout(result, last_assistant)
        return self._to_run_result(result=result, stdout=stdout)

    def _prepare_options(self) -> None:
        """Attach a fresh stderr sink and reset :attr:`last_captured_stderr`.

        Resets :attr:`last_captured_stderr` to a fresh list each run so a
        reused instance does not leak stderr lines across runs. Subclasses
        (e.g. :class:`cai_lib.cai_subagent.CaiSubAgent`) call ``super()``
        first and then add repo-specific setup (CLI path pin, plugin inject).
        """
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
            logging.getLogger(__name__).warning(
                "[cai cost] structured output retries exhausted "
                "(%s/%s); schema was not satisfied",
                self.category, self.agent,
            )
            return ""
        if isinstance(result.result, str):
            return result.result
        return last_assistant

    def _to_run_result(
        self,
        *,
        result: ResultMessage | None = None,
        stdout: str = "",
        exc: Exception | None = None,
    ) -> RunResult:
        """Build a :class:`RunResult` from the run outcome.

        Covers four cases:

        - ``exc`` set → :attr:`RunStatus.EXCEPTION`
        - ``result`` is ``None`` (and ``exc`` is ``None``) → :attr:`RunStatus.NO_RESULT`
        - ``result.is_error`` → :attr:`RunStatus.SDK_ERROR`
        - otherwise → :attr:`RunStatus.OK`
        """
        captured = list(self.last_captured_stderr)
        if exc is not None:
            preview = str(exc)[:200].replace("\n", " ")
            cli_text = _captured_stderr_text(captured)
            cli_preview = cli_text.replace("\n", " | ")[:400]
            msg = (
                f"[cai cost] claude-agent-sdk query failed "
                f"({self.category}/{self.agent}): {preview}"
            )
            if cli_preview:
                msg += f" | cli_stderr={cli_preview!r}"
            logging.getLogger(__name__).warning(msg)
            return RunResult(
                status=RunStatus.EXCEPTION,
                stdout="",
                result=None,
                error_summary=str(exc),
                captured_stderr=captured,
            )
        if result is None:
            preview = (self.last_assistant or "")[:120].replace("\n", " ")
            cli_text = _captured_stderr_text(captured)
            cli_preview = cli_text.replace("\n", " | ")[:400]
            msg = (
                f"[cai cost] no ResultMessage from claude-agent-sdk "
                f"({self.category}/{self.agent}); last assistant starts with: "
                f"{preview!r}"
            )
            if cli_preview:
                msg += f" | cli_stderr={cli_preview!r}"
            logging.getLogger(__name__).warning(msg)
            return RunResult(
                status=RunStatus.NO_RESULT,
                stdout=stdout,
                result=None,
                error_summary=f"no_ResultMessage last_assistant={preview!r}",
                captured_stderr=captured,
            )
        if result.is_error:
            return RunResult(
                status=RunStatus.SDK_ERROR,
                stdout=stdout,
                result=result,
                error_summary=_sdk_error_summary(result),
                captured_stderr=captured,
            )
        return RunResult(
            status=RunStatus.OK,
            stdout=stdout,
            result=result,
            error_summary=None,
            captured_stderr=captured,
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
) -> RunResult:
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
