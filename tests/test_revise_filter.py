"""Tests for _filter_comments_with_haiku in cai_lib.actions.revise."""

import json
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the function under test.
from cai_lib.actions.revise import _filter_comments_with_haiku


def _make_comment(idx_hint, author, body, created="2026-01-01T00:00:00Z"):
    """Helper: build a comment dict in the shape used by handle_revise."""
    return {
        "author": {"login": author},
        "body": body,
        "createdAt": created,
    }


# ── Fixture comments ────────────────────────────────────────────────────────
#
# (a) Pre-rebase human comment — should be UNRESOLVED.
HUMAN_COMMENT = _make_comment(0, "reviewer", "Please add docstrings to the public API.",
                               "2026-04-01T10:00:00Z")

# (b) Post-rebase bot comment (Revise subagent header) — should be RESOLVED.
BOT_COMMENT = _make_comment(1, "damien-robotsix",
                             "## Revise subagent: no additional changes\n\nRebase was clean.",
                             "2026-04-01T22:00:00Z")

# (c) Resolved review thread marker — should be RESOLVED.
RESOLVED_THREAD = _make_comment(2, "reviewer",
                                 "resolved: true\n\nLooks good now.",
                                 "2026-04-01T11:00:00Z")

# (d) Another human comment covered by a later "no additional changes" reply.
COVERED_COMMENT = _make_comment(3, "reviewer2",
                                 "Can you add a type annotation to `parse()`?",
                                 "2026-04-01T09:00:00Z")

NO_CHANGES_MARKER = _make_comment(4, "damien-robotsix",
                                   "## Revise subagent: no additional changes\n\n"
                                   "Reviewed `parse()` signature — annotation not warranted.",
                                   "2026-04-01T23:00:00Z")

# (e) A pre-merge review finding that revise deferred as out-of-scope.
#     On its own this would look unresolved (diff doesn't address it),
#     but a LATER `(clean)` pre-merge review supersedes it — see rule 6.
PREMERGE_FINDING = _make_comment(
    5, "damien-robotsix",
    "## cai pre-merge review — abc1234\n\n"
    "### Finding: missing_co_change\n\n"
    "File X references symbol Y but isn't updated.",
    "2026-04-02T10:00:00Z",
)

PREMERGE_CLEAN = _make_comment(
    6, "damien-robotsix",
    "## cai pre-merge review (clean) — def5678\n\nNo ripple effects found.",
    "2026-04-02T11:00:00Z",
)


def _mock_claude_p_returning(payload: dict):
    """Return a mock subprocess.CompletedProcess that looks like a cai-comment-filter result."""
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = json.dumps(payload)
    return proc


class TestFilterCommentsWithHaiku(unittest.TestCase):
    """Unit tests for _filter_comments_with_haiku."""

    def _patch_run(self, haiku_payload):
        """Patch _run (for gh pr diff) and _run_claude_p (for the haiku call)."""
        diff_proc = MagicMock()
        diff_proc.returncode = 0
        diff_proc.stdout = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n"

        claude_proc = _mock_claude_p_returning(haiku_payload)

        run_patch = patch("cai_lib.actions.revise._run", return_value=diff_proc)
        claude_patch = patch("cai_lib.actions.revise._run_claude_p", return_value=claude_proc)
        return run_patch, claude_patch

    def test_human_comment_returned_as_unresolved(self):
        """Human comment not covered by any resolved marker should come back."""
        run_p, claude_p = self._patch_run({"unresolved": [{"id": "0", "reason": "needs docstrings"}]})
        with run_p, claude_p:
            result = _filter_comments_with_haiku([HUMAN_COMMENT], pr_number=42, issue_number=100)
        self.assertEqual(result, [HUMAN_COMMENT])

    def test_bot_comment_not_in_unresolved(self):
        """If the haiku returns no unresolved items, the filter returns nothing."""
        run_p, claude_p = self._patch_run({"unresolved": []})
        with run_p, claude_p:
            result = _filter_comments_with_haiku([BOT_COMMENT], pr_number=42, issue_number=100)
        self.assertEqual(result, [])

    def test_resolved_thread_excluded(self):
        """A comment the haiku marks as resolved is excluded."""
        run_p, claude_p = self._patch_run({"unresolved": []})
        with run_p, claude_p:
            result = _filter_comments_with_haiku([RESOLVED_THREAD], pr_number=42, issue_number=100)
        self.assertEqual(result, [])

    def test_no_additional_changes_marker_covers_earlier(self):
        """The haiku should not return earlier human comment when a 'no changes' marker covers it."""
        # Haiku returns empty: the marker (idx 4) covers comment (idx 3).
        run_p, claude_p = self._patch_run({"unresolved": []})
        with run_p, claude_p:
            result = _filter_comments_with_haiku(
                [COVERED_COMMENT, NO_CHANGES_MARKER], pr_number=42, issue_number=100,
            )
        self.assertEqual(result, [])

    def test_clean_premerge_review_supersedes_earlier_finding(self):
        """A later `(clean)` pre-merge review should cause the haiku to treat
        earlier `## cai pre-merge review —` findings as resolved, even if the
        diff doesn't address them (the revise subagent may have deferred them
        as out-of-scope and the clean re-review at the newer SHA is
        authoritative)."""
        run_p, claude_p = self._patch_run({"unresolved": []})
        with run_p, claude_p:
            result = _filter_comments_with_haiku(
                [PREMERGE_FINDING, PREMERGE_CLEAN], pr_number=42, issue_number=100,
            )
        self.assertEqual(result, [])

    def test_only_genuinely_unresolved_returned(self):
        """Mixed fixture: only the unresolved human comment comes back."""
        all_comments = [HUMAN_COMMENT, BOT_COMMENT, RESOLVED_THREAD, COVERED_COMMENT, NO_CHANGES_MARKER]
        # Haiku says only idx 0 (HUMAN_COMMENT) is unresolved.
        run_p, claude_p = self._patch_run({"unresolved": [{"id": "0", "reason": "docstrings missing"}]})
        with run_p, claude_p:
            result = _filter_comments_with_haiku(all_comments, pr_number=42, issue_number=100)
        self.assertEqual(result, [HUMAN_COMMENT])

    def test_fallback_on_haiku_failure(self):
        """If the haiku agent fails, all non-bot comments are returned."""
        diff_proc = MagicMock()
        diff_proc.returncode = 0
        diff_proc.stdout = ""

        fail_proc = MagicMock()
        fail_proc.returncode = 1
        fail_proc.stdout = ""

        all_comments = [HUMAN_COMMENT, BOT_COMMENT]
        with patch("cai_lib.actions.revise._run", return_value=diff_proc):
            with patch("cai_lib.actions.revise._run_claude_p", return_value=fail_proc):
                result = _filter_comments_with_haiku(all_comments, pr_number=42, issue_number=100)

        # BOT_COMMENT starts with "## Revise subagent:" so it is a bot comment.
        self.assertIn(HUMAN_COMMENT, result)
        self.assertNotIn(BOT_COMMENT, result)

    def test_fallback_on_invalid_json(self):
        """If the haiku returns invalid JSON, fall back to all non-bot comments."""
        diff_proc = MagicMock()
        diff_proc.returncode = 0
        diff_proc.stdout = ""

        bad_proc = MagicMock()
        bad_proc.returncode = 0
        bad_proc.stdout = "not valid json {{{"

        all_comments = [HUMAN_COMMENT, BOT_COMMENT]
        with patch("cai_lib.actions.revise._run", return_value=diff_proc):
            with patch("cai_lib.actions.revise._run_claude_p", return_value=bad_proc):
                result = _filter_comments_with_haiku(all_comments, pr_number=42, issue_number=100)

        self.assertIn(HUMAN_COMMENT, result)
        self.assertNotIn(BOT_COMMENT, result)

    def test_empty_comments_returns_empty(self):
        """Empty input yields empty output without calling the haiku."""
        with patch("cai_lib.actions.revise._run_claude_p") as mock_claude:
            result = _filter_comments_with_haiku([], pr_number=42, issue_number=100)
        self.assertEqual(result, [])
        mock_claude.assert_not_called()

    def test_multiple_unresolved_returned(self):
        """Multiple unresolved comments are all returned."""
        c1 = _make_comment(0, "alice", "Please add logging.")
        c2 = _make_comment(1, "bob", "This function is too long.")
        run_p, claude_p = self._patch_run({
            "unresolved": [
                {"id": "0", "reason": "logging missing"},
                {"id": "1", "reason": "too long"},
            ],
        })
        with run_p, claude_p:
            result = _filter_comments_with_haiku([c1, c2], pr_number=99, issue_number=199)
        self.assertEqual(result, [c1, c2])


if __name__ == "__main__":
    unittest.main()
