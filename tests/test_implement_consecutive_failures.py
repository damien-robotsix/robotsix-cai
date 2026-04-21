"""Tests for _count_consecutive_tests_failed."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.actions.implement import _count_consecutive_tests_failed


class TestCountConsecutiveTestsFailed(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False,
        )
        self.tmp.close()
        self.log_path = Path(self.tmp.name)

    def tearDown(self):
        if self.log_path.exists():
            self.log_path.unlink()

    def _write(self, lines):
        self.log_path.write_text("\n".join(lines) + "\n")

    def test_empty_log_returns_zero(self):
        missing = Path("/nonexistent/cai-test-log.log")
        with patch("cai_lib.actions.implement.LOG_PATH", missing):
            self.assertEqual(_count_consecutive_tests_failed(42), 0)

    def test_all_tests_failed_returns_count(self):
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:10:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            self.assertEqual(_count_consecutive_tests_failed(42), 3)

    def test_stops_at_non_tests_failed(self):
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 result=subagent_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:10:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            self.assertEqual(_count_consecutive_tests_failed(42), 2)

    def test_other_issue_not_counted(self):
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=99 result=tests_failed exit=1",
            "2026-04-16T10:10:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            self.assertEqual(_count_consecutive_tests_failed(42), 2)

    def test_non_consecutive_resets_count(self):
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=42 result=tests_passed exit=0",
            "2026-04-16T10:10:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
            "2026-04-16T10:15:00Z [implement] repo=foo issue=42 result=tests_failed exit=1",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            self.assertEqual(_count_consecutive_tests_failed(42), 2)

    def test_tests_failed_escalated_not_counted(self):
        """Lines with result=tests_failed_escalated must not be
        counted as result=tests_failed (was a substring-match bug)."""
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=42 "
            "result=tests_failed_escalated exit=0",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            # Most recent line is `_escalated`, so the consecutive
            # count of strict tests_failed must be 0 (walks back
            # from the newest entry; the newest is not tests_failed).
            self.assertEqual(_count_consecutive_tests_failed(42), 0)

    def test_tests_failed_escalated_early_not_counted(self):
        """Same guarantee for the new pre-empted-early tag."""
        self._write([
            "2026-04-16T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-16T10:05:00Z [implement] repo=foo issue=42 "
            "result=tests_failed_escalated_early exit=0",
        ])
        with patch("cai_lib.actions.implement.LOG_PATH", self.log_path):
            self.assertEqual(_count_consecutive_tests_failed(42), 0)


class TestInProgressToRefiningTransitionExists(unittest.TestCase):
    """The new #923 transition must exist for the MEDIUM-plan auto-refine branch."""

    def test_transition_registered(self):
        from cai_lib.fsm import (
            ISSUE_TRANSITIONS,
            IssueState,
            find_transition,
        )
        t = find_transition("in_progress_to_refining")
        self.assertEqual(t.from_state, IssueState.IN_PROGRESS)
        self.assertEqual(t.to_state, IssueState.REFINING)
        self.assertIn(t, ISSUE_TRANSITIONS)


class TestCountConsecutiveFailedAttempts(unittest.TestCase):
    """Regression tests for the issue-#1088 general failure counter."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False,
        )
        self.tmp.close()
        self.log_path = Path(self.tmp.name)

    def tearDown(self):
        if self.log_path.exists():
            self.log_path.unlink()

    def _write(self, lines):
        self.log_path.write_text("\n".join(lines) + "\n")

    def test_empty_log_returns_zero(self):
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        missing = Path("/nonexistent/cai-test-log-1088.log")
        with patch("cai_lib.actions.implement.LOG_PATH", missing):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 0,
            )

    def test_counts_mixed_failure_types(self):
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        self._write([
            "2026-04-20T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T10:05:00Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
            "2026-04-20T10:10:00Z [implement] repo=foo issue=42 "
            "result=subagent_failed exit=1",
        ])
        with patch(
            "cai_lib.actions.implement.LOG_PATH", self.log_path,
        ):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 3,
            )

    def test_stops_at_escalation_transition(self):
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        self._write([
            "2026-04-20T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T10:05:00Z [implement] repo=foo issue=42 "
            "result=tests_failed_escalated exit=0",
            "2026-04-20T10:10:00Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
            "2026-04-20T10:15:00Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
        ])
        with patch(
            "cai_lib.actions.implement.LOG_PATH", self.log_path,
        ):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 2,
            )

    def test_stops_at_successful_pr_open(self):
        """A successful PR-open line has no ``result=`` field and
        must break the streak — otherwise the counter would walk
        past a success and conflate pre- and post-success failures."""
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        self._write([
            "2026-04-20T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T10:05:00Z [implement] repo=foo issue=42 "
            "branch=auto-improve/42-x pr=100 diff_files=3 exit=0",
            "2026-04-20T10:10:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
        ])
        with patch(
            "cai_lib.actions.implement.LOG_PATH", self.log_path,
        ):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 1,
            )

    def test_other_issue_not_counted(self):
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        self._write([
            "2026-04-20T10:00:00Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T10:05:00Z [implement] repo=foo issue=99 "
            "result=tests_failed exit=1",
            "2026-04-20T10:10:00Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
        ])
        with patch(
            "cai_lib.actions.implement.LOG_PATH", self.log_path,
        ):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 2,
            )

    def test_issue_1088_post_escalation_opus_sequence(self):
        """Mirrors the #1065 log flow: 2 Sonnet tests_failed + an
        escalation transition + 3 Opus failures. The counter must
        return 3 (only the post-escalation streak), not 5 (which
        would reach across the transition tag)."""
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        self._write([
            "2026-04-20T17:53:33Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T19:34:55Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T19:34:59Z [implement] repo=foo issue=42 "
            "result=tests_failed_escalated exit=0",
            "2026-04-20T20:56:08Z [implement] repo=foo issue=42 "
            "result=tests_failed exit=1",
            "2026-04-20T21:07:00Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
            "2026-04-20T22:07:58Z [implement] repo=foo issue=42 "
            "result=unexpected_error exit=1",
        ])
        with patch(
            "cai_lib.actions.implement.LOG_PATH", self.log_path,
        ):
            self.assertEqual(
                _count_consecutive_failed_attempts(42), 3,
            )


class TestRetriesExhaustedConstants(unittest.TestCase):
    """The new #1088 cap must be wired up with the expected
    constants and failure set."""

    def test_max_consecutive_failed_attempts_is_three(self):
        from cai_lib.actions.implement import (
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )
        self.assertEqual(_MAX_CONSECUTIVE_FAILED_ATTEMPTS, 3)

    def test_counted_failures_contains_expected_tags(self):
        from cai_lib.actions.implement import (
            _COUNTED_IMPLEMENT_FAILURES,
        )
        for tag in (
            "tests_failed",
            "subagent_failed",
            "unexpected_error",
            "clone_failed",
            "fetch_existing_failed",
            "push_failed",
            "pr_create_failed",
        ):
            self.assertIn(tag, _COUNTED_IMPLEMENT_FAILURES)

    def test_counted_failures_excludes_transition_tags(self):
        """Transition tags must NOT count as failures — they are the
        signal that the pipeline already responded to the failure."""
        from cai_lib.actions.implement import (
            _COUNTED_IMPLEMENT_FAILURES,
        )
        for tag in (
            "tests_failed_escalated",
            "tests_failed_escalated_early",
            "tests_failed_auto_refine",
            "retries_exhausted",
            "no_stored_plan",
            "bad_state",
            "lock_failed",
            "dismissed_resolved",
            "human_needed",
            "pre_screen_human",
            "pre_screen_ambiguous",
        ):
            self.assertNotIn(tag, _COUNTED_IMPLEMENT_FAILURES)


if __name__ == "__main__":
    unittest.main()
