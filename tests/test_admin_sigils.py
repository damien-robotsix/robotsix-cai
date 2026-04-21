"""Tests for cai_lib.admin_sigils — the <!-- cai-resplit --> sigil
scanner + processor wired into Phase 0.7 of ``cai cycle`` (#1142)."""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import admin_sigils
from cai_lib.admin_sigils import (
    RESPLIT_SIGIL,
    _latest_admin_comment,
    process_resplit_sigil,
    scan_resplit_sigil,
)
from cai_lib.config import LABEL_PLAN_APPROVED


class _AdminLoginPatch:
    """Context manager that temporarily swaps in a fake admin-login set."""

    def __init__(self, logins: set[str]) -> None:
        self._logins = logins
        self._orig = None

    def __enter__(self):
        import cai_lib.config as _cfg
        self._orig = _cfg.ADMIN_LOGINS
        _cfg.ADMIN_LOGINS = frozenset(self._logins)
        return self

    def __exit__(self, exc_type, exc, tb):
        import cai_lib.config as _cfg
        _cfg.ADMIN_LOGINS = self._orig


class TestLatestAdminComment(unittest.TestCase):

    def test_returns_last_admin_comment(self):
        comments = [
            {"author": {"login": "alice"}, "body": "first"},
            {"author": {"login": "bot"}, "body": "middle"},
            {"author": {"login": "alice"}, "body": "last admin"},
            {"author": {"login": "eve"}, "body": "after"},
        ]
        with _AdminLoginPatch({"alice"}):
            result = _latest_admin_comment(comments)
        self.assertIsNotNone(result)
        self.assertEqual(result["body"], "last admin")

    def test_returns_none_when_no_admin(self):
        comments = [
            {"author": {"login": "bot"}, "body": "noise"},
            {"author": {"login": "random"}, "body": "more"},
        ]
        with _AdminLoginPatch({"alice"}):
            self.assertIsNone(_latest_admin_comment(comments))

    def test_handles_missing_author_field(self):
        comments = [
            {"body": "bare"},
            {"author": None, "body": "nulled"},
        ]
        with _AdminLoginPatch({"alice"}):
            self.assertIsNone(_latest_admin_comment(comments))


class TestScanResplitSigil(unittest.TestCase):

    def _make_gh_json(self, issues):
        """Return a fake ``_gh_json`` and capture the argv it was called with."""
        captured: list = []

        def _fake(argv):
            captured.append(list(argv))
            return issues

        return _fake, captured

    def test_admin_sigil_in_latest_comment_is_detected(self):
        issues = [{
            "number": 1142,
            "comments": [
                {"author": {"login": "alice"}, "body": f"please {RESPLIT_SIGIL}"},
            ],
        }]
        fake, captured = self._make_gh_json(issues)
        with _AdminLoginPatch({"alice"}):
            result = scan_resplit_sigil(gh_json=fake)
        self.assertEqual(result, [1142])

    def test_non_admin_comment_with_sigil_is_ignored(self):
        issues = [{
            "number": 7,
            "comments": [
                {"author": {"login": "eve"}, "body": f"spoof {RESPLIT_SIGIL}"},
            ],
        }]
        fake, _ = self._make_gh_json(issues)
        with _AdminLoginPatch({"alice"}):
            self.assertEqual(scan_resplit_sigil(gh_json=fake), [])

    def test_sigil_only_in_older_admin_comment_is_ignored(self):
        # Admin posted the sigil first, then a later admin comment
        # without the sigil — the admin has moved past the re-split
        # intent. Latest-admin-only semantics require ignoring the issue.
        issues = [{
            "number": 99,
            "comments": [
                {"author": {"login": "alice"}, "body": f"earlier {RESPLIT_SIGIL}"},
                {"author": {"login": "alice"}, "body": "never mind"},
            ],
        }]
        fake, _ = self._make_gh_json(issues)
        with _AdminLoginPatch({"alice"}):
            self.assertEqual(scan_resplit_sigil(gh_json=fake), [])

    def test_gh_query_filters_by_plan_approved_label(self):
        fake, captured = self._make_gh_json([])
        with _AdminLoginPatch({"alice"}):
            scan_resplit_sigil(gh_json=fake)
        self.assertEqual(len(captured), 1)
        argv = captured[0]
        # --label must be LABEL_PLAN_APPROVED; --state must be open;
        # we must ask GH for number+comments so we can inspect authors.
        self.assertIn("--label", argv)
        self.assertEqual(argv[argv.index("--label") + 1], LABEL_PLAN_APPROVED)
        self.assertIn("--state", argv)
        self.assertEqual(argv[argv.index("--state") + 1], "open")
        self.assertIn("--json", argv)
        self.assertIn("number,comments", argv)

    def test_gh_failure_returns_empty(self):
        def _raise(_argv):
            raise RuntimeError("boom")
        with _AdminLoginPatch({"alice"}):
            self.assertEqual(scan_resplit_sigil(gh_json=_raise), [])


class TestProcessResplitSigil(unittest.TestCase):

    def test_fires_transition_and_posts_ack(self):
        fired: list = []
        posted: list = []

        def _fake_fire(number, trigger_name, **kwargs):
            fired.append({"number": number, "trigger": trigger_name, "kwargs": kwargs})
            return (True, False)

        def _fake_post(number, body, *, log_prefix="cai"):
            posted.append({"number": number, "body": body})

        ok = process_resplit_sigil(
            1142,
            fire_trigger_fn=_fake_fire,
            post_comment_fn=_fake_post,
        )
        self.assertTrue(ok)
        self.assertEqual(len(fired), 1)
        self.assertEqual(fired[0]["number"], 1142)
        self.assertEqual(fired[0]["trigger"], "plan_approved_to_refined")
        self.assertEqual(fired[0]["kwargs"].get("log_prefix"), "cai cycle")
        self.assertEqual(len(posted), 1)
        self.assertEqual(posted[0]["number"], 1142)
        self.assertIn("re-split sigil", posted[0]["body"])
        self.assertIn("`:refined`", posted[0]["body"])

    def test_returns_false_on_fire_failure_and_skips_comment(self):
        def _fake_fire(number, trigger_name, **kwargs):
            return (False, False)

        posted: list = []

        def _fake_post(number, body, *, log_prefix="cai"):
            posted.append({"number": number, "body": body})

        ok = process_resplit_sigil(
            1142,
            fire_trigger_fn=_fake_fire,
            post_comment_fn=_fake_post,
        )
        self.assertFalse(ok)
        self.assertEqual(posted, [])


if __name__ == "__main__":
    unittest.main()
