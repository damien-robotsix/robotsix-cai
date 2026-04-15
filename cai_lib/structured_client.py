"""Structured Anthropic API client for gate-critical agents.

Wraps direct Anthropic API calls with forced tool-use so agents that drive
FSM transitions return structured JSON fields rather than regex-parsed text.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import anthropic

from cai_lib.logging_utils import log_cost


def call_with_tool(
    model: str,
    system_prompt: str,
    user_message: str,
    tool_def: dict,
    *,
    category: str,
    agent: str,
) -> dict:
    """Call the Anthropic messages API with forced tool-use and return the tool input.

    Builds a messages API call with ``tool_choice={"type": "tool", "name":
    tool_def["name"]}`` so the model is forced to call the named tool.
    Extracts the first ``tool_use`` content block, logs cost via
    ``log_cost`` (mirroring ``_run_claude_p`` fields: ``category``,
    ``agent``, ``input_tokens``, ``output_tokens``, ``cost_usd``,
    ``duration_ms``), and returns ``tool_use.input`` as a plain dict.

    Raises ``anthropic.APIError`` on API error.
    Raises ``ValueError`` if no ``tool_use`` block is present in the response.
    """
    client = anthropic.Anthropic()
    t0 = time.monotonic()

    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        tools=[tool_def],
        tool_choice={"type": "tool", "name": tool_def["name"]},
    )

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Extract the first tool_use block from the response.
    tool_input: dict | None = None
    for block in response.content:
        if block.type == "tool_use":
            tool_input = dict(block.input)
            break

    if tool_input is None:
        raise ValueError(
            f"[cai structured_client] no tool_use block in response from "
            f"{model} ({category}/{agent})"
        )

    # Log cost — mirror _run_claude_p fields.  The messages API does not
    # return total_cost_usd directly, so we record None for that field.
    usage = response.usage
    row: dict = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "category": category,
        "agent": agent,
        "cost_usd": None,
        "duration_ms": duration_ms,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "is_error": False,
    }
    for attr in ("cache_creation_input_tokens", "cache_read_input_tokens"):
        val = getattr(usage, attr, None)
        if val is not None:
            row[attr] = val
    log_cost(row)

    return tool_input
