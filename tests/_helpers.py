"""Shared test helpers for the cai test suite.

Avoids byte-identical fixture duplication across the merge-test
trio (``test_merge_approach_mismatch``, ``test_merge_low_to_revision``,
``test_merge_workflow_review_label``). See issue #1319.
"""


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
