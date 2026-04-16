"""Tests for cai_lib.cmd_unblock — pure-logic helpers.

The command path (`cmd_unblock`) invokes the `cai-unblock` Haiku
agent via `claude -p` and is tested end-to-end in a live container.
These tests cover the deterministic pieces that don't need claude:
admin-comment filtering and agent-input formatting.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# CAI_ADMIN_LOGINS is read into a frozenset at the first import of
# cai_lib.config — by the time this test file loads, cai_lib.config
# may already be imported with an empty set. Patch the frozenset in
# place so is_admin_login sees our test logins.
from cai_lib import cmd_unblock as U  # noqa: E402
from cai_lib import config as _config  # noqa: E402
_config.ADMIN_LOGINS = frozenset({"alice", "bob"})


class TestExtractAdminComments(unittest.TestCase):

    def _issue(self, comments):
        return {"number": 1, "title": "t", "body": "b", "comments": comments}

    def test_filters_out_non_admin(self):
        issue = self._issue([
            {"author": {"login": "alice"}, "body": "admin says do X"},
            {"author": {"login": "charlie"}, "body": "non-admin"},
            {"author": {"login": "bob"}, "body": "admin says do Y"},
        ])
        kept = U._extract_admin_comments(issue)
        logins = [(c["author"]["login"]) for c in kept]
        self.assertEqual(logins, ["alice", "bob"])

    def test_missing_author_is_ignored(self):
        issue = self._issue([
            {"body": "no author object"},
            {"author": {}, "body": "empty author"},
        ])
        self.assertEqual(U._extract_admin_comments(issue), [])

    def test_empty_comments(self):
        self.assertEqual(U._extract_admin_comments({"comments": []}), [])
        self.assertEqual(U._extract_admin_comments({}), [])


class TestBuildUnblockMessage(unittest.TestCase):

    def _fixture(self):
        return {
            "number": 42,
            "title": "widget broke",
            "body": "original body text",
            "labels": [
                {"name": "auto-improve:human-needed"},
                {"name": "human:solved"},
            ],
            "comments": [
                {"author": {"login": "automation-bot"},
                 "createdAt": "2026-04-14T10:00:00Z",
                 "body": "automation context note"},
                {"author": {"login": "alice"},
                 "createdAt": "2026-04-14T12:00:00Z",
                 "body": "please re-try as plan-approved"},
            ],
        }

    def test_contains_required_sections(self):
        issue = self._fixture()
        msg = U._build_unblock_message(kind="issue", issue=issue)
        self.assertIn("Kind: issue", msg)
        self.assertIn("## Labels", msg)
        self.assertIn("auto-improve:human-needed", msg)
        self.assertIn("human:solved", msg)
        self.assertIn("widget broke", msg)
        self.assertIn("original body text", msg)
        self.assertIn("alice", msg)
        self.assertIn("[admin]", msg)
        self.assertIn("please re-try as plan-approved", msg)
        # Non-admin comments are included unfiltered for context.
        self.assertIn("automation-bot", msg)
        self.assertIn("automation context note", msg)

    def test_no_comments_placeholder(self):
        issue = {
            "number": 42,
            "title": "widget broke",
            "body": "original body text",
            "labels": [],
            "comments": [],
        }
        msg = U._build_unblock_message(kind="issue", issue=issue)
        self.assertIn("(no comments)", msg)


class TestTryUnblockIssueSkips(unittest.TestCase):
    """The no-op branch does not invoke claude."""

    def test_no_admin_comment(self):
        issue = {"number": 2, "title": "t", "body": "issue text",
                 "labels": [], "comments": [
                     {"author": {"login": "stranger"}, "body": "hi"},
                 ]}
        with mock.patch.object(U, "_run_claude_p") as fake:
            result = U._try_unblock_issue(issue)
        self.assertEqual(result, "no_admin_comment")
        fake.assert_not_called()


class TestListHumanNeededIssuesFiltersByLabel(unittest.TestCase):
    """_list_human_needed_issues must require BOTH :human-needed and human:solved.

    The label-gated handoff is the whole point of PR 3 — if this query
    regresses to a single --label filter the classifier will start
    firing on every parked issue again.
    """

    def test_queries_both_labels(self):
        captured: list[list[str]] = []

        def fake_gh(args):
            captured.append(args)
            return []

        with mock.patch.object(U, "_gh_json", side_effect=fake_gh):
            U._list_human_needed_issues()

        self.assertEqual(len(captured), 1)
        args = captured[0]
        # --label appears twice, once for each required label.
        label_flags = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        self.assertIn("auto-improve:human-needed", label_flags)
        self.assertIn("human:solved", label_flags)
        self.assertEqual(len(label_flags), 2)


class TestResumeStripsHumanSolvedLabel(unittest.TestCase):
    """A successful resume must remove human:solved so the signal is one-shot."""

    def test_apply_transition_receives_human_solved_in_extra_remove(self):
        issue = {
            "number": 77,
            "title": "t",
            "body": "issue text",
            "labels": [
                {"name": "auto-improve:human-needed"},
                {"name": "human:solved"},
            ],
            "comments": [
                {"author": {"login": "alice"},
                 "createdAt": "2026-04-14T12:00:00Z",
                 "body": "please retry"},
            ],
        }

        agent_stdout = "ResumeTo: REFINING\nConfidence: HIGH\n"
        fake_agent = mock.MagicMock()
        fake_agent.returncode = 0
        fake_agent.stdout = agent_stdout
        fake_agent.stderr = ""

        captured: dict = {}

        def fake_apply(issue_number, transition_name, **kwargs):
            captured["issue_number"] = issue_number
            captured["transition_name"] = transition_name
            captured["kwargs"] = kwargs
            return True

        with mock.patch.object(U, "_run_claude_p", return_value=fake_agent), \
             mock.patch.object(U, "apply_transition", side_effect=fake_apply):
            result = U._try_unblock_issue(issue)

        self.assertEqual(result, "resumed")
        self.assertEqual(captured["issue_number"], 77)
        self.assertEqual(captured["transition_name"], "human_to_refining")
        self.assertIn("human:solved", captured["kwargs"].get("extra_remove", []))


class TestHandleHumanNeeded(unittest.TestCase):
    """Dispatcher hook — gated on ``human:solved`` to avoid spinning on
    parked-waiting issues each cycle tick."""

    def test_noop_without_human_solved(self):
        issue = {"number": 1, "labels": [{"name": "auto-improve:human-needed"}]}
        with mock.patch.object(U, "_try_unblock_issue") as fake:
            rc = U.handle_human_needed(issue)
        self.assertEqual(rc, 0)
        fake.assert_not_called()

    def test_delegates_when_human_solved_present(self):
        issue = {
            "number": 2,
            "labels": [
                {"name": "auto-improve:human-needed"},
                {"name": "human:solved"},
            ],
        }
        with mock.patch.object(U, "_try_unblock_issue", return_value="resumed") as fake:
            rc = U.handle_human_needed(issue)
        self.assertEqual(rc, 0)
        fake.assert_called_once_with(issue)

    def test_agent_failed_returns_nonzero(self):
        issue = {
            "number": 3,
            "labels": [
                {"name": "auto-improve:human-needed"},
                {"name": "human:solved"},
            ],
        }
        with mock.patch.object(U, "_try_unblock_issue", return_value="agent_failed"):
            rc = U.handle_human_needed(issue)
        self.assertEqual(rc, 1)


class TestListPrHumanNeededFiltersByLabel(unittest.TestCase):
    """PR-side picker must require BOTH :pr-human-needed and human:solved."""

    def test_queries_both_labels(self):
        captured: list[list[str]] = []

        def fake_gh(args):
            captured.append(args)
            return []

        with mock.patch.object(U, "_gh_json", side_effect=fake_gh):
            U._list_pr_human_needed_prs()

        self.assertEqual(len(captured), 1)
        args = captured[0]
        label_flags = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
        self.assertIn("auto-improve:pr-human-needed", label_flags)
        self.assertIn("human:solved", label_flags)
        self.assertEqual(len(label_flags), 2)


class TestTryUnblockPrSkips(unittest.TestCase):
    """The no-op branches on PR resume do not invoke claude."""

    def test_no_admin_comment(self):
        pr = {"number": 700, "title": "t", "body": "pr body", "labels": [],
              "comments": [{"author": {"login": "stranger"}, "body": "hi"}]}
        with mock.patch.object(U, "_run_claude_p") as fake:
            result = U._try_unblock_pr(pr)
        self.assertEqual(result, "no_admin_comment")
        fake.assert_not_called()

    def test_resumed_clears_human_solved(self):
        pr = {
            "number": 701,
            "title": "t",
            "body": "pr body without marker",
            "labels": [
                {"name": "auto-improve:pr-human-needed"},
                {"name": "human:solved"},
            ],
            "comments": [
                {"author": {"login": "alice"},
                 "createdAt": "2026-04-15T12:00:00Z",
                 "body": "looks good, merge it"},
            ],
        }
        agent_stdout = "ResumeTo: APPROVED\nConfidence: HIGH\n"
        fake_agent = mock.MagicMock()
        fake_agent.returncode = 0
        fake_agent.stdout = agent_stdout
        fake_agent.stderr = ""

        transitions: list[str] = []

        def fake_pr_transition(pr_number, transition_name, **kwargs):
            transitions.append(transition_name)
            return True

        set_label_calls: list[dict] = []

        def fake_set_pr_labels(pr_number, *, add=(), remove=(), log_prefix=""):
            set_label_calls.append({"add": list(add), "remove": list(remove)})
            return True

        with mock.patch.object(U, "_run_claude_p", return_value=fake_agent), \
             mock.patch.object(U, "apply_pr_transition", side_effect=fake_pr_transition), \
             mock.patch.object(U, "_set_pr_labels", side_effect=fake_set_pr_labels):
            result = U._try_unblock_pr(pr)

        self.assertEqual(result, "resumed")
        self.assertEqual(transitions, ["pr_human_to_approved"])
        self.assertTrue(
            any("human:solved" in c["remove"] for c in set_label_calls),
            f"human:solved not cleared: {set_label_calls}",
        )


class TestHandlePrHumanNeeded(unittest.TestCase):

    def test_noop_without_human_solved(self):
        pr = {"number": 1, "labels": [{"name": "auto-improve:pr-human-needed"}]}
        with mock.patch.object(U, "_try_unblock_pr") as fake:
            rc = U.handle_pr_human_needed(pr)
        self.assertEqual(rc, 0)
        fake.assert_not_called()

    def test_delegates_when_human_solved_present(self):
        pr = {
            "number": 2,
            "labels": [
                {"name": "auto-improve:pr-human-needed"},
                {"name": "human:solved"},
            ],
        }
        with mock.patch.object(U, "_try_unblock_pr", return_value="resumed") as fake:
            rc = U.handle_pr_human_needed(pr)
        self.assertEqual(rc, 0)
        fake.assert_called_once_with(pr)


if __name__ == "__main__":
    unittest.main()
