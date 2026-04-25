"""Shared test helpers for the cai test suite.

Avoids byte-identical fixture duplication across the merge-test
trio (``test_merge_approach_mismatch``, ``test_merge_low_to_revision``,
``test_merge_workflow_review_label``). See issue #1319.

``_mock_query`` is a shared async-iterator replacement for
``cai_lib.subagent.core.query`` used across the SDK-level tests. See
issue #1320.

``_rsync_available`` gates rsync-dependent tests in ``test_cost_sync``
and ``test_transcript_sync``. See issue #1321.

``_mk_result`` is a shared ``ResultMessage`` builder used across the
SDK-level tests. Uses the richest default set (from ``test_sdk_spike_parity``)
so all callers that need different values can override via ``**fields``.
See issue #1322.
"""

import subprocess

from claude_agent_sdk.types import ResultMessage


def _mk_result(**fields) -> ResultMessage:
    """Build a ResultMessage with deterministic defaults.

    Uses the richest default set (from ``test_sdk_spike_parity``) — all
    callers that need different values already override them via **fields.
    """
    return ResultMessage(
        subtype=fields.pop("subtype", "success"),
        duration_ms=fields.pop("duration_ms", 1234),
        duration_api_ms=fields.pop("duration_api_ms", 999),
        is_error=fields.pop("is_error", False),
        num_turns=fields.pop("num_turns", 3),
        session_id=fields.pop("session_id", "sess-fixed"),
        total_cost_usd=fields.pop("total_cost_usd", 0.1234),
        usage=fields.pop("usage", {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 200,
            "cache_read_input_tokens": 800,
        }),
        result=fields.pop("result", "ok"),
        structured_output=fields.pop("structured_output", None),
        model_usage=fields.pop("model_usage", {
            "claude-sonnet-4": {
                "inputTokens": 100,
                "outputTokens": 50,
                "cacheReadInputTokens": 800,
                "cacheCreationInputTokens": 200,
                "costUSD": 0.1234,
            },
        }),
    )


def _rsync_available() -> bool:
    try:
        subprocess.run(["rsync", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def _mock_query(*messages):
    """Return an async-generator replacement for cai_lib.subagent.core.query."""
    async def _gen(*, prompt, options=None, transport=None):
        for m in messages:
            yield m
    return _gen


def _pr_fixture(number: int = 1234) -> dict:
    return {
        "number": number,
        "title": "auto-improve: example",
        "headRefName": f"auto-improve/{number}-example",
        "headRefOid": "d7becb043dfd84c2796f35b7deb1353881435930",
        "labels": [{"name": "pr:approved"}],
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "mergedAt": None,
        "comments": [],
        "reviews": [],
        "createdAt": "2026-04-20T00:00:00Z",
    }
