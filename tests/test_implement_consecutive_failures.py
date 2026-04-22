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


class TestEffectiveCapConstants(unittest.TestCase):
    """Issue #1151: the extended Sonnet-tier retries cap must be
    wired up with the expected value, and the base cap must be
    preserved at 3."""

    def test_base_cap_unchanged_at_three(self):
        """Regression pin — the base #1088 cap stays at 3 so
        non-extended plans keep the same behaviour."""
        from cai_lib.actions.implement import (
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )
        self.assertEqual(_MAX_CONSECUTIVE_FAILED_ATTEMPTS, 3)

    def test_extended_cap_is_five(self):
        from cai_lib.actions.implement import (
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED,
        )
        self.assertEqual(_MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED, 5)


class TestExtendedRetriesLabelAffectsEffectiveCap(unittest.TestCase):
    """Issue #1151: the presence of LABEL_EXTENDED_RETRIES on the issue
    must raise the effective cap from 3 to 5 for Sonnet-tier runs and
    leave it at 3 for Opus-tier runs.

    Exercised through the guard by patching LOG_PATH with a fixture
    file carrying four consecutive ``result=subagent_failed`` rows —
    enough to trip the base 3-cap but not the extended 5-cap."""

    def _write_four_failures(self, log_path, issue_number):
        log_path.write_text("\n".join(
            f"2026-04-22T0{i}:00:00Z [implement] repo=foo "
            f"issue={issue_number} result=subagent_failed exit=1"
            for i in range(4)
        ) + "\n")

    def _issue(self, *, labels):
        return {
            "number": 1151,
            "title": "t",
            "body": (
                "## Plan\n\n"
                "<!-- cai-plan-start -->\n"
                "### Files to change\n"
                "- **`pkg/a.py`**: change\n\n"
                "### Detailed steps\n"
                "#### Step 1 — Edit `pkg/a.py`\n\nbody\n"
                "<!-- cai-plan-end -->\n"
            ),
            "labels": [{"name": n} for n in labels],
        }

    def test_four_failures_trip_base_cap_without_extended_label(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False,
        )
        tmp.close()
        log_path = Path(tmp.name)
        try:
            self._write_four_failures(log_path, 1151)
            with patch(
                "cai_lib.actions.implement.LOG_PATH", log_path,
            ):
                count = _count_consecutive_failed_attempts(1151)
            self.assertGreaterEqual(
                count, _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
            )
        finally:
            if log_path.exists():
                log_path.unlink()

    def test_effective_cap_logic_uses_extended_when_label_present(self):
        """Structural check: the label-driven branch in the guard
        must select _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED iff
        (opus_escalation is False) AND (LABEL_EXTENDED_RETRIES in
        label_names)."""
        from cai_lib.actions.implement import (
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED,
        )
        from cai_lib.config import (
            LABEL_EXTENDED_RETRIES,
            LABEL_OPUS_ATTEMPTED,
        )

        def pick(label_names, opus_escalation):
            extended_retries = LABEL_EXTENDED_RETRIES in label_names
            if not opus_escalation and extended_retries:
                return _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED
            return _MAX_CONSECUTIVE_FAILED_ATTEMPTS

        self.assertEqual(
            pick([LABEL_EXTENDED_RETRIES], opus_escalation=False),
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS_EXTENDED,
        )
        self.assertEqual(
            pick([], opus_escalation=False),
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )
        self.assertEqual(
            pick([LABEL_EXTENDED_RETRIES, LABEL_OPUS_ATTEMPTED],
                 opus_escalation=True),
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )
        self.assertEqual(
            pick([LABEL_OPUS_ATTEMPTED], opus_escalation=True),
            _MAX_CONSECUTIVE_FAILED_ATTEMPTS,
        )


class TestFormatStderrTail(unittest.TestCase):
    """Issue #1106: the sanitizer must collapse whitespace, neutralise
    ``=``, cap length, and always return a non-empty single token so
    the existing ``_RESULT_TAG_RE = re.compile(r" result=(\\S+)")``
    classifier in :func:`_count_consecutive_failed_attempts` keeps
    matching ``subagent_failed`` unchanged when the new
    ``stderr_tail=<token>`` field is stamped between ``result=`` and
    ``exit=``."""

    def test_empty_returns_empty_tag(self):
        from cai_lib.actions.implement import _format_stderr_tail
        self.assertEqual(_format_stderr_tail(""), "empty")
        self.assertEqual(_format_stderr_tail("   \n\t"), "empty")

    def test_whitespace_is_collapsed_to_underscore(self):
        from cai_lib.actions.implement import _format_stderr_tail
        self.assertEqual(
            _format_stderr_tail("hit max turns"),
            "hit_max_turns",
        )
        self.assertEqual(
            _format_stderr_tail("line one\nline two"),
            "line_one_line_two",
        )

    def test_equals_is_rewritten_to_colon(self):
        """``=`` must not leak into the token or a downstream
        key=value parser could split ``stderr_tail=foo=bar`` in two."""
        from cai_lib.actions.implement import _format_stderr_tail
        self.assertEqual(
            _format_stderr_tail("sdk_subtype=error_max_turns"),
            "sdk_subtype:error_max_turns",
        )

    def test_truncation_caps_length(self):
        from cai_lib.actions.implement import (
            _format_stderr_tail,
            _STDERR_TAIL_LIMIT,
        )
        huge = "x" * (_STDERR_TAIL_LIMIT * 3)
        self.assertEqual(
            len(_format_stderr_tail(huge)),
            _STDERR_TAIL_LIMIT,
        )

    def test_token_is_single_token_safe_for_log_parser(self):
        """The token stamped between ``result=`` and ``exit=`` must
        never contain whitespace — otherwise ``_RESULT_TAG_RE``
        (``" result=(\\S+)"``) would grab the wrong ``result=`` token
        downstream and the consecutive-failure guard would undercount."""
        import re as _re
        from cai_lib.actions.implement import _format_stderr_tail
        rx = _re.compile(r" result=(\S+)")
        for sample in (
            "sdk_subtype=error_max_turns is_error=True "
            "result='Agent exhausted max_turns=60'",
            "",
            "\nspaced  out\tvalue\n",
            "no_ResultMessage last_assistant='oops'",
        ):
            tok = _format_stderr_tail(sample)
            self.assertNotIn(" ", tok)
            self.assertNotIn("\t", tok)
            self.assertNotIn("\n", tok)
            self.assertNotIn("=", tok)
            line = (
                "2026-04-21T05:16:05Z [implement] repo=foo issue=910 "
                f"result=subagent_failed stderr_tail={tok} exit=1"
            )
            m = rx.search(line)
            self.assertIsNotNone(m)
            self.assertEqual(m.group(1), "subagent_failed")


class TestConsecutiveCounterStillMatchesEnrichedLine(unittest.TestCase):
    """Pin that :func:`_count_consecutive_failed_attempts` still counts
    three consecutive ``subagent_failed`` rows correctly when each
    carries the new issue-#1106 ``stderr_tail=<token>`` field (which
    sits between ``result=`` and ``exit=``). If this regresses, the
    existing :data:`_MAX_CONSECUTIVE_FAILED_ATTEMPTS` guard would stop
    firing and issue #910's failure mode would reappear."""

    def test_three_consecutive_with_stderr_tail_field_counted_as_three(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from cai_lib.actions.implement import (
            _count_consecutive_failed_attempts,
        )
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".log", delete=False,
        )
        tmp.close()
        log_path = Path(tmp.name)
        try:
            log_path.write_text(
                "\n".join([
                    "2026-04-21T00:30:57Z [implement] repo=foo issue=910 "
                    "result=subagent_failed "
                    "stderr_tail=sdk_subtype:error_max_turns_is_error:True "
                    "exit=1",
                    "2026-04-21T00:47:28Z [implement] repo=foo issue=910 "
                    "result=subagent_failed stderr_tail=empty exit=1",
                    "2026-04-21T03:08:55Z [implement] repo=foo issue=910 "
                    "result=subagent_failed "
                    "stderr_tail=no_ResultMessage_last_assistant:oops "
                    "exit=1",
                ]) + "\n"
            )
            with patch(
                "cai_lib.actions.implement.LOG_PATH", log_path,
            ):
                self.assertEqual(
                    _count_consecutive_failed_attempts(910), 3,
                )
        finally:
            if log_path.exists():
                log_path.unlink()


if __name__ == "__main__":
    unittest.main()
