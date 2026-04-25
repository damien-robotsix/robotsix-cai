"""Typed transcript for a :class:`~cai_lib.subagent.core.SubAgent` run.

:class:`RunTranscript` is a recursive Pydantic model that captures the
SDK stream as a tree: top-level assistant text, tool uses, tool results,
and nested :class:`SubAgentNode` instances representing each ``Task``
spawn. The terminating :class:`ResultMessage` (singular under #1279) is
stored on :attr:`RunTranscript.result`.

Existing ``_collect_results`` projections — ``last_assistant``,
``parent_model``, ``subagent_counts`` — remain available as derived
properties on :class:`RunTranscript` so existing call sites in
:class:`~cai_lib.subagent.core.SubAgent` keep working without churn.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from claude_agent_sdk.types import ResultMessage
from pydantic import BaseModel, ConfigDict, Field


class TokenUsage(BaseModel):
    """Accumulated token usage for one transcript scope (top-level or subagent).

    Populated by :func:`cai_lib.subagent.core._collect_results` as it
    routes each :class:`~claude_agent_sdk.types.AssistantMessage` to the
    correct scope (top-level :class:`RunTranscript` or the matching
    :class:`SubAgentNode`) and deduplicates by
    :attr:`~claude_agent_sdk.types.AssistantMessage.message_id` within
    each scope (issue #1286).

    :attr:`cache_hit_rate` is a derived property and is **not** serialised
    — it is recomputed from the four int fields on every access.
    """

    input_tokens: int = Field(default=0, description="Accumulated input tokens for this scope.")
    output_tokens: int = Field(default=0, description="Accumulated output tokens for this scope.")
    cache_read_input_tokens: int = Field(
        default=0,
        description="Accumulated cache-read input tokens for this scope.",
    )
    cache_creation_input_tokens: int = Field(
        default=0,
        description="Accumulated cache-creation input tokens for this scope.",
    )

    @property
    def cache_hit_rate(self) -> float | None:
        """Cache read / (cache read + cache create + input); ``None`` when denom=0."""
        denom = (
            self.cache_read_input_tokens
            + self.cache_creation_input_tokens
            + self.input_tokens
        )
        return (
            round(self.cache_read_input_tokens / denom, 4)
            if denom > 0 else None
        )


class AssistantTextEvent(BaseModel):
    """One non-empty :class:`TextBlock` from an ``AssistantMessage``.

    The SDK emits one :class:`AssistantMessage` per assistant turn; each
    such message may carry multiple :class:`TextBlock` parts. We append
    one :class:`AssistantTextEvent` per non-empty block (matching the
    issue contract) and recover ``parent_model`` / ``last_assistant_text``
    lazily on :class:`RunTranscript`.
    """

    kind: Literal["assistant"] = "assistant"
    model: str = Field(
        default="",
        description="Model name (e.g. 'claude-sonnet-4'); '' if unknown.",
    )
    text: str = Field(..., description="The TextBlock's text payload.")
    message_id: str | None = Field(
        default=None,
        description="Optional SDK message uuid (forward-compat).",
    )


class ToolUseEvent(BaseModel):
    """A non-Task :class:`ToolUseBlock` (Read, Bash, Edit, ...).

    ``Task`` invocations are represented by :class:`SubAgentNode`
    instead so nested events can be attached recursively.
    """

    kind: Literal["tool_use"] = "tool_use"
    tool_use_id: str = Field(..., description="The block's SDK id.")
    name: str = Field(..., description="Tool name (e.g. 'Read').")
    input: dict[str, Any] = Field(
        default_factory=dict,
        description="The block's input dict (verbatim from the SDK).",
    )


class ToolResultEvent(BaseModel):
    """One :class:`ToolResultBlock` from a ``UserMessage``.

    ``content`` is left as :class:`Any` because the SDK union is
    ``str | list[dict[str, Any]] | None`` and serializability is the
    consumer's concern (see issue #1280 scope guardrails).
    """

    kind: Literal["tool_result"] = "tool_result"
    tool_use_id: str = Field(
        ..., description="The id of the originating ToolUseBlock.",
    )
    content: Any = Field(
        default=None,
        description="ToolResultBlock.content (str | list | None).",
    )


class SubAgentNode(BaseModel):
    """A ``Task`` spawn — recursive container for the subagent's events.

    Created when an ``AssistantMessage`` contains a
    ``ToolUseBlock(name='Task')``. The block's ``id`` is the
    :attr:`tool_use_id`; subsequent SDK messages whose
    ``parent_tool_use_id`` matches that id route their events into
    :attr:`events` (which may itself contain further
    :class:`SubAgentNode` instances).

    :attr:`usage` accumulates the token counts from every
    :class:`~claude_agent_sdk.types.AssistantMessage` routed to this
    node's scope, deduped by ``message_id`` (issue #1286).
    """

    kind: Literal["subagent"] = "subagent"
    tool_use_id: str = Field(
        ..., description="The spawning Task ToolUseBlock.id.",
    )
    subagent_type: str = Field(
        ...,
        description=(
            "Task input.subagent_type or 'general-purpose' "
            "if unspecified."
        ),
    )
    usage: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Accumulated token usage for this subagent's scope (issue #1286).",
    )
    events: list["AgentEvent"] = Field(
        default_factory=list,
        description="Recursive child events for this subagent.",
    )


AgentEvent = Annotated[
    Union[
        AssistantTextEvent,
        ToolUseEvent,
        ToolResultEvent,
        SubAgentNode,
    ],
    Field(discriminator="kind"),
]


SubAgentNode.model_rebuild()


class RunTranscript(BaseModel):
    """Top-level container for a :class:`SubAgent` run's transcript.

    :attr:`events` carries the top-level event stream (no
    ``parent_tool_use_id``); :attr:`result` carries the singular
    terminating :class:`ResultMessage` (None on exception or no-result
    paths).

    :attr:`usage` accumulates the token counts from every top-level
    :class:`~claude_agent_sdk.types.AssistantMessage`
    (``parent_tool_use_id is None``), deduped by ``message_id``
    (issue #1286).
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    events: list[AgentEvent] = Field(
        default_factory=list,
        description="Top-level event stream.",
    )
    usage: TokenUsage = Field(
        default_factory=TokenUsage,
        description="Accumulated top-level token usage (parent_tool_use_id=None messages, issue #1286).",
    )
    result: ResultMessage | None = Field(
        default=None,
        description=(
            "Singular terminating ResultMessage (issue #1279). "
            "None on exception / no-result."
        ),
    )

    @property
    def parent_model(self) -> str | None:
        """Model of the first top-level :class:`AssistantTextEvent`.

        Top-level events only come from ``AssistantMessage``s with
        ``parent_tool_use_id is None`` (messages with an unknown
        ``parent_tool_use_id`` are skipped in ``_collect_results``),
        so this matches the prior ``parent_model`` semantics exactly.
        """
        for ev in self.events:
            if isinstance(ev, AssistantTextEvent):
                return ev.model or None
        return None

    @property
    def last_assistant_text(self) -> str:
        """Text of the last non-empty top-level AssistantTextEvent.

        Used by the priority-4 stdout-salvage path in
        :meth:`SubAgent._extract_stdout` when ``result`` is absent.
        """
        last = ""
        for ev in self.events:
            if isinstance(ev, AssistantTextEvent):
                t = ev.text.strip()
                if t:
                    last = t
        return last

    @property
    def subagent_counts(self) -> dict[str, int]:
        """Recursive count of every :class:`SubAgentNode` by ``subagent_type``.

        Walks the entire event tree (top-level + every nested
        SubAgentNode), bucketing by ``subagent_type`` — matching the
        prior flat-counter semantics in :func:`_collect_results`.
        """
        counts: dict[str, int] = {}

        def _walk(events: list[AgentEvent]) -> None:
            for ev in events:
                if isinstance(ev, SubAgentNode):
                    counts[ev.subagent_type] = counts.get(
                        ev.subagent_type, 0,
                    ) + 1
                    _walk(ev.events)

        _walk(self.events)
        return counts
