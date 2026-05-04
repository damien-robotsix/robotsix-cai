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

from pydantic_ai import AgentRunResult
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded

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

    On a transient ``ModelHTTPError`` — provider-routing flakes (404
    "No endpoints found"), upstream provider credit/billing failures
    (402 "Insufficient Balance"; the *upstream* provider's account ran
    out, our OpenRouter wallet is fine), rate limits (429), and
    upstream timeouts/5xx — sleeps with exponential backoff and retries
    up to ``_TRANSIENT_RETRY_ATTEMPTS`` times. OpenRouter routes each
    retry afresh, so a different provider is likely picked. Other
    ``ModelHTTPError`` instances re-raise immediately.
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
        if not _is_transient_http_error(exc):
            raise
        return await _retry_transient(name, agent, prompt, exc, **kwargs)
    except UnexpectedModelBehavior as exc:
        if "exceeded max retries count of" in str(exc):
            if _is_langfuse_initialized():
                from langfuse import get_client
                get_client().update_current_span(
                    metadata={"exhausted_retries": True, "detail": str(exc)}
                )
            return AgentRunResult(output=_AgentExhaustedSentinel())
        raise


# Retry policy for transient ModelHTTPErrors.  Each attempt is preceded by
# an exponential-backoff sleep with jitter; the first retry waits ~5 s,
# the last ~40 s.  Three retries cover the vast majority of provider-routing
# blips without holding the workflow hostage.
_TRANSIENT_RETRY_ATTEMPTS = 3
_TRANSIENT_RETRY_BASE_SECONDS = 5.0


class _AgentExhaustedSentinel:
    exhausted: bool = True
    summary: str = "Agent exhausted all retries before completing the task."
    related_files: list[str] = []
    files_changed: list[str] = []
    commit_message: str = "Agent exhausted"
    required_checks: list[str] = []
    replies: list = []
    title: str = "Agent exhausted"
    reference_files: list[str] = []
    sub_issues: list[str] = []


def _is_transient_http_error(exc: "ModelHTTPError") -> bool:
    """Return True for HTTP errors a soft retry is likely to clear.

    Categories:

    * 404 "No endpoints found" — OpenRouter routing flake (no provider
      currently advertises support for the request shape).
    * 402 "Insufficient Balance" — the *upstream* provider's account
      (e.g. DeepSeek's direct backend) ran out; OpenRouter routes each
      retry afresh and another provider may pick up.
    * 429 — rate limit; backoff usually clears it.
    * 5xx (>=500, <600) — upstream timeout / server error.

    Caller-side errors (auth, invalid request) are not retried.
    """
    status = exc.status_code
    body = str(exc.body or "")
    if status == 404 and "No endpoints found" in body:
        return True
    if status == 402 and "Insufficient" in body:
        return True
    if status == 429:
        return True
    if 500 <= status < 600:
        return True
    return False


async def _retry_transient(
    name: str,
    agent: Any,
    prompt: str,
    first_exc: "ModelHTTPError",
    **kwargs: Any,
) -> Any:
    """Re-run ``agent`` with exponential-backoff sleeps between attempts.

    Re-raises the *last* exception if every attempt fails.
    """
    import random

    last_exc: BaseException = first_exc
    for attempt in range(1, _TRANSIENT_RETRY_ATTEMPTS + 1):
        wait = _TRANSIENT_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
        wait += random.uniform(0, wait * 0.25)  # ±25% jitter to avoid thundering herd
        await asyncio.sleep(wait)
        if _is_langfuse_initialized():
            from langfuse import get_client
            get_client().update_current_span(
                metadata={
                    "soft_retry": f"transient_http_{last_exc.status_code}",
                    "attempt": attempt,
                    "wait_seconds": round(wait, 2),
                },
            )
        try:
            return await _do_run(name, agent, prompt, **kwargs)
        except ModelHTTPError as exc:
            if not _is_transient_http_error(exc):
                raise
            last_exc = exc
    raise last_exc
