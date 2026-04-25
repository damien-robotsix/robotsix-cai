"""Agent-SDK execution core: :class:`SubAgent` and its helpers.

:class:`SubAgent` is a Pydantic model: options (and identity:
``category`` + ``agent``) are fixed at construction, and each
:meth:`run` call takes a fresh prompt. One instance can be reused
across many prompts — its cost history accumulates on the embedded
:class:`~cai_lib.subagent.cost_tracker.CostTracker`. Instance state
(``runs``, ``last_result``) survives between runs and can be
introspected between calls.

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
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from pydantic import BaseModel, ConfigDict, Field

from .cost_tracker import CostRow, CostTracker
from .errors import _sdk_error_summary
from .transcript import (
    AssistantTextEvent,
    ResultMessageModel,
    RunTranscript,
    SubAgentNode,
    ToolResultEvent,
    ToolUseEvent,
)


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

    All fields are Pydantic-native so :meth:`model_dump_json` round-trips
    cleanly end-to-end (issue #1281).
    """

    status: RunStatus = Field(..., description="Outcome classification.")
    stdout: str = Field(..., description="Extracted agent stdout (same priority order as before).")
    result: ResultMessageModel | None = Field(
        None, description="The final result as a Pydantic mirror (issue #1281); None on exception or no-result."
    )
    error_summary: str | None = Field(
        None,
        description=(
            "_sdk_error_summary(result) on SDK_ERROR; str(exc) on EXCEPTION; "
            "no_ResultMessage preview on NO_RESULT; None on OK."
        ),
    )
    transcript: RunTranscript | None = Field(
        default=None,
        description=(
            "Full typed transcript of the run; None on exception "
            "before query() started (issue #1280)."
        ),
    )

    @property
    def ok(self) -> bool:
        """True when :attr:`status` is :attr:`RunStatus.OK`."""
        return self.status == RunStatus.OK


async def _collect_results(
    prompt: str,
    options: ClaudeAgentOptions,
) -> RunTranscript:
    """Drive ``query()`` to completion and build a :class:`RunTranscript`.

    The returned :class:`RunTranscript` captures the SDK stream as a
    tree: top-level :class:`AssistantTextEvent` / :class:`ToolUseEvent`
    / :class:`ToolResultEvent`, with each ``Task`` spawn becoming a
    :class:`SubAgentNode` whose ``events`` recursively hold the
    subagent's own stream. Routing uses ``parent_tool_use_id``: messages
    with no parent (or whose parent id is unknown) land at the top
    level; messages whose ``parent_tool_use_id`` matches a registered
    Task ``ToolUseBlock.id`` land inside that node's ``events``. The
    singular terminating :class:`ResultMessage` (issue #1279) is
    stored on :attr:`RunTranscript.result`.

    Existing ``_collect_results`` projections — ``last_assistant``,
    ``parent_model``, ``subagent_counts`` — are exposed as derived
    properties on :class:`RunTranscript` so :class:`SubAgent` callers
    are unaffected.

    Kept as a module-level function for backwards import stability;
    :class:`SubAgent` is the primary consumer.
    """
    transcript = RunTranscript()
    nodes_by_tool_use_id: dict[str, SubAgentNode] = {}

    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, ResultMessage):
            transcript.result = ResultMessageModel.from_sdk(msg)
            continue

        parent_id = getattr(msg, "parent_tool_use_id", None)
        if parent_id is not None:
            if parent_id in nodes_by_tool_use_id:
                container = nodes_by_tool_use_id[parent_id].events
            else:
                # parent_tool_use_id is set but not registered as a Task
                # node (e.g. a haiku-backed helper that ran before the
                # parent's first Task call). Skip rather than routing to
                # top-level — preserves the parent_model invariant that
                # only parent_tool_use_id=None messages are top-level.
                continue
        else:
            container = transcript.events

        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    container.append(AssistantTextEvent(
                        model=msg.model or "",
                        text=block.text,
                    ))
                elif isinstance(block, ToolUseBlock) and block.name == "Task":
                    sub_type = (
                        (block.input or {}).get("subagent_type")
                        or "general-purpose"
                    )
                    node = SubAgentNode(
                        tool_use_id=block.id,
                        subagent_type=sub_type,
                    )
                    nodes_by_tool_use_id[block.id] = node
                    container.append(node)
                elif isinstance(block, ToolUseBlock):
                    container.append(ToolUseEvent(
                        tool_use_id=block.id,
                        name=block.name,
                        input=dict(block.input or {}),
                    ))
        elif isinstance(msg, UserMessage):
            content = msg.content
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        container.append(ToolResultEvent(
                            tool_use_id=block.tool_use_id,
                            content=block.content,
                        ))

    return transcript


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
    options: ClaudeAgentOptions = Field(
        exclude=True,
        description=(
            "Runtime-only SDK options; excluded from model_dump() so "
            "SubAgent serialises cleanly (issue #1281)."
        ),
    )
    timeout: float | None = None
    cost_tracker: CostTracker = Field(default_factory=CostTracker)

    runs: int = 0
    last_result: ResultMessageModel | None = None
    last_assistant: str = ""

    def run(self, prompt: str) -> RunResult:
        """Drive one full run against ``prompt`` and return the RunResult."""
        self._prepare_options()
        try:
            transcript = self._drive_query(prompt)
        except Exception as exc:  # noqa: BLE001
            self.runs += 1
            return self._to_run_result(exc=exc)

        self.runs += 1
        last_assistant = transcript.last_assistant_text
        self.last_assistant = last_assistant

        if transcript.result is None:
            return self._to_run_result(
                stdout=last_assistant or "",
                transcript=transcript,
            )

        result = transcript.result
        self.last_result = result

        self.cost_tracker.record(CostRow.from_result_message(
            category=self.category,
            agent=self.agent,
            prompt=prompt,
            system_prompt=self.options.system_prompt,
            result=result,
            parent_model=transcript.parent_model,
            subagent_counts=transcript.subagent_counts,
        ))

        stdout = self._extract_stdout(result, last_assistant)
        return self._to_run_result(
            result=result, stdout=stdout, transcript=transcript,
        )

    def _prepare_options(self) -> None:
        """Per-run options hook for subclasses.

        No-op in the base class. Subclasses (e.g.
        :class:`cai_lib.cai_subagent.CaiSubAgent`) override to perform
        repo-specific setup such as pinning ``options.cli_path`` or
        injecting a local plugin.
        """

    def _drive_query(
        self,
        prompt: str,
    ) -> RunTranscript:
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
        self, result: ResultMessageModel, last_assistant: str,
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
        result: ResultMessageModel | None = None,
        stdout: str = "",
        exc: Exception | None = None,
        transcript: RunTranscript | None = None,
    ) -> RunResult:
        """Build a :class:`RunResult` from the run outcome.

        Covers four cases:

        - ``exc`` set → :attr:`RunStatus.EXCEPTION`
        - ``result`` is ``None`` (and ``exc`` is ``None``) → :attr:`RunStatus.NO_RESULT`
        - ``result.is_error`` → :attr:`RunStatus.SDK_ERROR`
        - otherwise → :attr:`RunStatus.OK`

        ``transcript`` is forwarded onto the :class:`RunResult` for every
        non-exception path. Exception paths leave ``transcript=None``
        because the failure happened before / during the SDK stream was
        drained (issue #1280).
        """
        if exc is not None:
            preview = str(exc)[:200].replace("\n", " ")
            logging.getLogger(__name__).warning(
                "[cai cost] claude-agent-sdk query failed (%s/%s): %s",
                self.category, self.agent, preview,
            )
            return RunResult(
                status=RunStatus.EXCEPTION,
                stdout="",
                result=None,
                error_summary=str(exc),
                transcript=transcript,
            )
        if result is None:
            preview = (self.last_assistant or "")[:120].replace("\n", " ")
            logging.getLogger(__name__).warning(
                "[cai cost] no ResultMessage from claude-agent-sdk "
                "(%s/%s); last assistant starts with: %r",
                self.category, self.agent, preview,
            )
            return RunResult(
                status=RunStatus.NO_RESULT,
                stdout=stdout,
                result=None,
                error_summary=f"no_ResultMessage last_assistant={preview!r}",
                transcript=transcript,
            )
        if result.is_error:
            return RunResult(
                status=RunStatus.SDK_ERROR,
                stdout=stdout,
                result=result,
                error_summary=_sdk_error_summary(result),
                transcript=transcript,
            )
        return RunResult(
            status=RunStatus.OK,
            stdout=stdout,
            result=result,
            error_summary=None,
            transcript=transcript,
        )
