"""Regression tests for the cai-managed label whitelist in cmd_misc.

Tracking issue #944: the hourly `_issue_label_sweep` strips any
``auto-improve:*`` label that isn't in ``_ALL_MANAGED_ISSUE_LABELS``.
``auto-improve:opus-attempted`` was missing from that whitelist, so the
one-shot Opus-escalation marker set by ``cai rescue`` did not survive
the next sweep — breaking the second-escalation guard. These tests
pin the whitelist membership and the sweep behaviour.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import cmd_misc as M  # noqa: E402
from cai_lib.config import LABEL_OPUS_ATTEMPTED  # noqa: E402


class TestManagedIssueLabelsWhitelist(unittest.TestCase):
    """The cai-managed whitelist must include every cai-owned label."""

    def test_opus_attempted_label_is_whitelisted(self):
        # Issue #944: without this entry, the hourly sweep strips the
        # label and the one-shot Opus-escalation guard becomes a no-op.
        self.assertIn(LABEL_OPUS_ATTEMPTED, M._ALL_MANAGED_ISSUE_LABELS)


class TestIssueLabelSweepRetainsOpusAttempted(unittest.TestCase):
    """``_issue_label_sweep`` must NOT remove ``LABEL_OPUS_ATTEMPTED``."""

    def test_sweep_does_not_strip_opus_attempted(self):
        issue = {
            "number": 42,
            "labels": [
                {"name": "auto-improve"},
                {"name": "auto-improve:human-needed"},
                {"name": LABEL_OPUS_ATTEMPTED},
            ],
        }
        # ``_issue_label_sweep`` queries ``gh`` three times (once per
        # base namespace: auto-improve, audit, check-workflows). Seed
        # the first reply with the issue and the other two empty.
        with mock.patch.object(
            M, "_gh_json", side_effect=[[issue], [], []]
        ), mock.patch.object(M, "_set_labels") as sl:
            M._issue_label_sweep()

        # No labels on the issue are "stale" once the whitelist is
        # correct, so ``_set_labels`` must not be invoked at all.
        sl.assert_not_called()

    def test_sweep_still_strips_unmanaged_labels(self):
        # Sanity: a bogus ``auto-improve:legacy`` label must still be
        # flagged as stale so we don't accidentally disable the sweep.
        issue = {
            "number": 43,
            "labels": [
                {"name": "auto-improve"},
                {"name": "auto-improve:legacy-garbage"},
                {"name": LABEL_OPUS_ATTEMPTED},
            ],
        }
        with mock.patch.object(
            M, "_gh_json", side_effect=[[issue], [], []]
        ), mock.patch.object(M, "_set_labels", return_value=True) as sl:
            M._issue_label_sweep()

        sl.assert_called_once()
        removed = sl.call_args.kwargs["remove"]
        self.assertIn("auto-improve:legacy-garbage", removed)
        self.assertNotIn(LABEL_OPUS_ATTEMPTED, removed)


if __name__ == "__main__":
    unittest.main()
