"""Tests for cai_lib.dup_check — verdict parser + context message builder.

The agent call itself is tested end-to-end in a live container; these
tests cover the deterministic pieces: parsing the structured verdict
the agent emits and rendering the inline user message.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.dup_check import (  # noqa: E402
    build_dup_check_message,
    parse_dup_check_verdict,
)


class TestParseDupCheckVerdict(unittest.TestCase):

    def test_duplicate_high(self):
        out = (
            "Verdict: DUPLICATE\n"
            "Target: #42\n"
            "Confidence: HIGH\n"
            "Reasoning: Both describe the same lock-watchdog bug.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertIsNotNone(v)
        self.assertEqual(v.verdict, "DUPLICATE")
        self.assertEqual(v.target, 42)
        self.assertEqual(v.confidence, "HIGH")
        self.assertTrue(v.should_close)
        self.assertIn("lock-watchdog", v.reasoning)

    def test_resolved_high(self):
        out = (
            "Verdict: RESOLVED\n"
            "CommitSha: abc123def\n"
            "Confidence: HIGH\n"
            "Reasoning: PR #500 merged last week fixes this exactly.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.verdict, "RESOLVED")
        self.assertEqual(v.commit_sha, "abc123def")
        self.assertTrue(v.should_close)

    def test_none_never_closes(self):
        out = (
            "Verdict: NONE\n"
            "Confidence: HIGH\n"
            "Reasoning: No overlap found.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.verdict, "NONE")
        self.assertFalse(v.should_close)

    def test_medium_confidence_never_closes(self):
        out = (
            "Verdict: DUPLICATE\n"
            "Target: #7\n"
            "Confidence: MEDIUM\n"
            "Reasoning: Similar but not certain.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.confidence, "MEDIUM")
        self.assertFalse(v.should_close)

    def test_duplicate_without_target_is_downgraded(self):
        out = (
            "Verdict: DUPLICATE\n"
            "Confidence: HIGH\n"
            "Reasoning: malformed — no target specified.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.confidence, "LOW")
        self.assertFalse(v.should_close)

    def test_resolved_without_commit_is_downgraded(self):
        out = (
            "Verdict: RESOLVED\n"
            "Confidence: HIGH\n"
            "Reasoning: malformed — no sha specified.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.confidence, "LOW")
        self.assertFalse(v.should_close)

    def test_missing_required_fields_returns_none(self):
        self.assertIsNone(parse_dup_check_verdict(""))
        self.assertIsNone(parse_dup_check_verdict("random chatter"))
        self.assertIsNone(parse_dup_check_verdict("Verdict: DUPLICATE\n"))  # no confidence

    def test_unrecognised_verdict_returns_none(self):
        out = "Verdict: MAYBE\nConfidence: HIGH\n"
        self.assertIsNone(parse_dup_check_verdict(out))

    def test_unrecognised_confidence_downgrades_to_low(self):
        out = (
            "Verdict: NONE\n"
            "Confidence: SUPER-HIGH\n"
            "Reasoning: none.\n"
        )
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.confidence, "LOW")
        self.assertFalse(v.should_close)

    def test_target_with_leading_hash(self):
        out = "Verdict: DUPLICATE\nTarget: 42\nConfidence: HIGH\nReasoning: x.\n"
        v = parse_dup_check_verdict(out)
        self.assertEqual(v.target, 42)


class TestBuildDupCheckMessage(unittest.TestCase):

    def _issue(self, n=10, title="t", body="b", labels=None):
        return {
            "number": n,
            "title": title,
            "body": body,
            "labels": [{"name": lb} for lb in (labels or [])],
        }

    def test_includes_target_and_other_sections(self):
        target = self._issue(n=99, title="watchdog races", body="details here",
                             labels=["auto-improve:raised"])
        others = [self._issue(n=7, title="other bug", body="xxx")]
        prs = [{"number": 500, "title": "fix race", "body": "addresses the race",
                "mergedAt": "2026-04-01T00:00:00Z"}]
        msg = build_dup_check_message(target, others, prs)
        self.assertIn("#99", msg)
        self.assertIn("watchdog races", msg)
        self.assertIn("auto-improve:raised", msg)
        self.assertIn("#7", msg)
        self.assertIn("PR #500", msg)
        self.assertIn("2026-04-01", msg)

    def test_empty_context_renders_none_sentinels(self):
        target = self._issue()
        msg = build_dup_check_message(target, [], [])
        self.assertIn("## Other open issues", msg)
        self.assertIn("## Recent merged PRs", msg)
        self.assertIn("(none)", msg)

    def test_unmerged_prs_are_filtered_out(self):
        target = self._issue()
        prs = [
            {"number": 1, "title": "open pr", "body": "x", "mergedAt": None},
            {"number": 2, "title": "merged pr", "body": "y",
             "mergedAt": "2026-04-01T00:00:00Z"},
        ]
        msg = build_dup_check_message(target, [], prs)
        self.assertIn("PR #2", msg)
        self.assertNotIn("PR #1", msg)


if __name__ == "__main__":
    unittest.main()
