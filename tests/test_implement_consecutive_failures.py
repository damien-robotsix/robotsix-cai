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


if __name__ == "__main__":
    unittest.main()
