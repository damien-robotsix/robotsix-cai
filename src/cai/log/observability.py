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
import re
import sys

import asyncio
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded

from genai_prices.update_prices import UpdatePrices as _UpdatePrices

# Kick off a background price-data refresh so models added after the last
# package release (e.g. newly released Claude versions) get correct costs.
try:
    _UpdatePrices().start()
except RuntimeError:
    pass  # already started in another module or by a previous import

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

    os.environ.setdefault("LANGFUSE_TIMEOUT", "300")

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


_CAI_BRANCH_RE = re.compile(r"^cai/solve-(\d+)$")


def session_id_for_pr(pr_number: int, branch: str | None) -> str:
    """Group a PR's traces under its originating issue when the branch is cai-owned.

    cai-solve names branches ``cai/solve-<issue>``; reuse the issue id so the
    issue run, the resulting PR's review-thread runs, and any later
    conflict-resolves all share one Langfuse session. Human-created PRs fall
    back to ``pr-<n>``.
    """
    if branch:
        m = _CAI_BRANCH_RE.match(branch)
        if m:
            return f"issue-{m.group(1)}"
    return f"pr-{pr_number}"


@contextmanager
def langfuse_workflow(
    name: str,
    *,
    input: Any = None,
    metadata: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> Generator[None, None, None]:
    """Wrap a block in a named Langfuse parent span (type=agent).

    All pydantic-ai agent runs inside the block are nested under this span
    via OTel context propagation, grouping explore + refine into one trace.
    Falls through silently when Langfuse is not configured.
    """
    if not setup_langfuse():
        yield
        return

    from contextlib import ExitStack

    from langfuse import get_client, propagate_attributes

    client = get_client()
    with ExitStack() as stack:
        stack.enter_context(
            client.start_as_current_observation(
                name=name,
                as_type="agent",
                input=input,
                metadata=metadata,
            )
        )
        attrs: dict = {"trace_name": name}
        if session_id is not None:
            attrs["session_id"] = session_id
        stack.enter_context(propagate_attributes(**attrs))
        yield


def _is_langfuse_initialized() -> bool:
    """Check whether Langfuse has been initialised via :func:`setup_langfuse`.

    Looks up the current ``_initialized`` flag from ``sys.modules``
    rather than from the defining module's namespace, so the check
    stays correct even when ``cai.log.observability`` is reloaded
    (e.g. during tests that delete and re-import the module).
    """
    mod = sys.modules.get(__name__)
    if mod is None:
        return False
    return getattr(mod, "_initialized", False)


async def _do_run(
    name: str,
    agent: Any,
    prompt: str,
    **kwargs: Any,
) -> Any:
    """Core implementation: run an agent inside a named Langfuse span."""
    if not _is_langfuse_initialized():
        return await agent.run(prompt, **kwargs)

    from langfuse import get_client

    client = get_client()
    with client.start_as_current_observation(
        name=name,
        as_type="span",
        input=prompt,
    ):
        return await agent.run(prompt, **kwargs)


async def traced_agent_run(
    name: str,
    agent: Any,
    prompt: str,
    **kwargs: Any,
) -> Any:
    """Run an agent inside a named Langfuse span (type=span).

    When Langfuse is configured, the span nests under the active
    parent observation set up by :func:`langfuse_workflow`, so every
    sub-agent appears as a child in the root trace rather than as a
    separate top-level trace.

    Falls through to a plain ``await agent.run(...)`` when Langfuse
    is not configured.

    If ``UsageLimitExceeded`` is raised, the ``request_limit`` is
    bumped by 50% for a one-shot soft retry.  The second failure
    bubbles up so the workflow fails as it does today.

    If ``ModelHTTPError`` with status 404 and a body containing
    ``"No endpoints found"`` (an OpenRouter transient routing flake)
    is raised, sleeps 30 s and retries exactly once.  Other
    ``ModelHTTPError`` instances are re-raised immediately.
    """
    try:
        return await _do_run(name, agent, prompt, **kwargs)
    except UsageLimitExceeded:
        ul = kwargs.get("usage_limits")
        if ul is not None:
            import dataclasses
            kwargs["usage_limits"] = dataclasses.replace(
                ul, request_limit=int(ul.request_limit * 1.5)
            )
        if _is_langfuse_initialized():
            from langfuse import get_client
            get_client().update_current_span(metadata={"soft_retry": True})
        return await _do_run(name, agent, prompt, **kwargs)
    except ModelHTTPError as exc:
        if exc.status_code != 404 or "No endpoints found" not in str(exc.body or ""):
            raise
        # Provider routing flake — wait briefly and retry once.
        await asyncio.sleep(30)
        if _is_langfuse_initialized():
            from langfuse import get_client
            get_client().update_current_span(
                metadata={"soft_retry": "provider_404"},
            )
        return await _do_run(name, agent, prompt, **kwargs)
