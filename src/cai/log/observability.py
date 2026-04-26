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
