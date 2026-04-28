"""Optional Langfuse instrumentation for pydantic-ai agents.

When ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are set
(plus ``LANGFUSE_BASE_URL`` for self-hosted), ``setup_langfuse()``
initializes the Langfuse SDK and calls ``Agent.instrument_all()`` so
all pydantic-ai agents in the process emit traces. Without those env
vars it is a silent no-op.

Follows https://langfuse.com/integrations/frameworks/pydantic-ai —
the SDK ships its own OTel pipeline, so we don't configure exporters
manually.
"""
from __future__ import annotations

import atexit
import os
import sys
from collections.abc import Generator
from collections.abc import Callable
import functools
from contextlib import contextmanager
from typing import Any

from genai_prices.update_prices import UpdatePrices as _UpdatePrices

# Kick off a background price-data refresh so models added after the last
# package release (e.g. newly released Claude versions) get correct costs.
_UpdatePrices().start()

_initialized = False


def setup_langfuse() -> bool:
    """Wire pydantic-ai → Langfuse if credentials are set.

    Idempotent. Returns True if instrumentation was enabled. Flushes
    on process exit so short CLI runs don't drop the trailing batch.
    """
    global _initialized
    if _initialized:
        return True

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        return False

    from langfuse import get_client
    from pydantic_ai import Agent

    client = get_client()
    if not client.auth_check():
        print("warning: Langfuse auth_check failed; tracing disabled", file=sys.stderr)
        return False

    def wrap_run(original_run):
        @functools.wraps(original_run)
        async def run_wrapper(self, *args, **kwargs):
            result = await original_run(self, *args, **kwargs)
            try:
                calculate_and_record_cost(self, result)
            except Exception as e:
                print(f"warning: calculate_and_record_cost failed: {e}", file=sys.stderr)
            return result
        return run_wrapper

    def wrap_run_sync(original_run_sync):
        @functools.wraps(original_run_sync)
        def run_sync_wrapper(self, *args, **kwargs):
            result = original_run_sync(self, *args, **kwargs)
            try:
                calculate_and_record_cost(self, result)
            except Exception as e:
                print(f"warning: calculate_and_record_cost failed: {e}", file=sys.stderr)
            return result
        return run_sync_wrapper

    if not hasattr(Agent, "_is_wrapped_run"):
        original_run = Agent.run
        Agent.run = wrap_run(original_run)
        Agent._is_wrapped_run = True

    if not hasattr(Agent, "_is_wrapped_run_sync"):
        original_run_sync = Agent.run_sync
        Agent.run_sync = wrap_run_sync(original_run_sync)
        Agent._is_wrapped_run_sync = True

    Agent.instrument_all()
    atexit.register(client.flush)

    _initialized = True
    return True


@contextmanager
def langfuse_workflow(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
) -> Generator[None, None, None]:
    """Wrap a block in a named Langfuse parent span (type=agent).

    All pydantic-ai agent runs inside the block are nested under this span
    via OTel context propagation, grouping explore + refine into one trace.
    Falls through silently when Langfuse is not configured.
    """
    if not setup_langfuse():
        yield
        return

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(
        name=name,
        as_type="agent",
        input=input,
        metadata=metadata,
    ):
        yield

def calculate_and_record_cost(agent: Any, result: Any) -> None:
    """Calculate the cost of the agent run and update the current Langfuse observation."""
    from langfuse import get_client
    try:
        from genai_prices import prices
        import pydantic_ai
    except ImportError:
        return

    usage = getattr(result, "usage", lambda: None)() if callable(getattr(result, "usage", None)) else None
    if not usage:
        return

    model_name = getattr(agent.model, 'model_name', None)
    if not model_name:
        return

    try:
        cost_data = prices.get_price(model_name)
    except Exception:
        cost_data = None

    if not cost_data:
        return

    input_tokens = getattr(usage, 'request_tokens', 0) or 0
    output_tokens = getattr(usage, 'response_tokens', 0) or 0
    
    input_price = cost_data.get('input_price', 0) or 0
    output_price = cost_data.get('output_price', 0) or 0

    total_cost = (input_tokens * input_price) + (output_tokens * output_price)

    client = get_client()
    try:
        client.update_current_observation(calculated_total_cost=total_cost)
    except Exception as e:
        print(f"warning: failed to update Langfuse observation cost: {e}", file=sys.stderr)
