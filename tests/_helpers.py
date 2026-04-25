"""Shared test helpers for the cai test suite.

Avoids byte-identical fixture duplication across the merge-test
trio (``test_merge_approach_mismatch``, ``test_merge_low_to_revision``,
``test_merge_workflow_review_label``). See issue #1319.

``_mock_query`` is a shared async-iterator replacement for
``cai_lib.subagent.core.query`` used across the SDK-level tests. See
issue #1320.

``_rsync_available`` gates rsync-dependent tests in ``test_cost_sync``
and ``test_transcript_sync``. See issue #1321.
"""

import subprocess


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
