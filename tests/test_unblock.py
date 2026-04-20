"""Tests for cai_lib.cmd_unblock — pure-logic helpers.

The command path (`cmd_unblock`) invokes the `cai-unblock` Haiku
agent via `claude -p` and is tested end-to-end in a live container.
These tests cover the deterministic pieces that don't need claude:
admin-comment filtering and agent-input formatting.
"""
import json
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

        agent_stdout = json.dumps({
            "resume_to": "REFINING",
            "confidence": "HIGH",
            "reasoning": "admin asked to re-run refine",
        })
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
        agent_stdout = json.dumps({
            "resume_to": "APPROVED",
            "confidence": "HIGH",
            "reasoning": "admin greenlighted merge",
        })
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


class TestCollectAmendmentComments(unittest.TestCase):
    """Short acknowledgments are noise — filter them out."""

    def test_filters_short_comments(self):
        comments = [
            {"body": "ok"},
            {"body": "lgtm"},
            {"body": "approved"},
        ]
        self.assertEqual(U._collect_amendment_comments(comments), [])

    def test_keeps_substantive_comments(self):
        long_text = (
            "Please change step 3 so that it uses json.dumps instead of "
            "repr, and add a regression test for the MEDIUM-plan branch."
        )
        comments = [
            {"body": "ok"},
            {"body": long_text},
        ]
        kept = U._collect_amendment_comments(comments)
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["body"], long_text)


class TestAppendAdminAmendmentsToPlan(unittest.TestCase):
    """Amendments are appended inside the stored plan block."""

    _BODY_WITH_PLAN = (
        "<!-- cai-plan-start -->\n"
        "## Selected Implementation Plan\n\n"
        "Do the thing.\n"
        "Confidence: MEDIUM\n"
        "Confidence reason: ambiguous scope\n"
        "<!-- cai-plan-end -->\n\n"
        "Original issue body below.\n"
    )

    def test_noop_without_amendments(self):
        with mock.patch.object(U, "_run") as fake_run:
            ok = U._append_admin_amendments_to_plan(
                42, self._BODY_WITH_PLAN, amendments=[],
            )
        self.assertFalse(ok)
        fake_run.assert_not_called()

    def test_noop_when_body_has_no_plan_block(self):
        with mock.patch.object(U, "_run") as fake_run:
            ok = U._append_admin_amendments_to_plan(
                42, "no plan here", amendments=[
                    {"author": {"login": "alice"},
                     "createdAt": "2026-04-19T10:00:00Z",
                     "body": "please tweak step 3 to use json.dumps"},
                ],
            )
        self.assertFalse(ok)
        fake_run.assert_not_called()

    def test_amendments_are_injected_into_plan_block(self):
        amendment = {
            "author": {"login": "alice"},
            "createdAt": "2026-04-19T10:00:00Z",
            "body": "please tweak step 3 to use json.dumps",
        }
        captured: dict = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["body"] = cmd[cmd.index("--body") + 1]
            result = mock.MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        with mock.patch.object(U, "_run", side_effect=fake_run):
            ok = U._append_admin_amendments_to_plan(
                42, self._BODY_WITH_PLAN, amendments=[amendment],
            )
        self.assertTrue(ok)
        new_body = captured["body"]
        # Plan markers remain and still enclose a single block.
        self.assertEqual(new_body.count("<!-- cai-plan-start -->"), 1)
        self.assertEqual(new_body.count("<!-- cai-plan-end -->"), 1)
        # Original plan text still present.
        self.assertIn("Do the thing.", new_body)
        self.assertIn("Confidence: MEDIUM", new_body)
        # Amendment content and author are inside the stored plan.
        start = new_body.index("<!-- cai-plan-start -->")
        end = new_body.index("<!-- cai-plan-end -->")
        plan_region = new_body[start:end]
        self.assertIn("## Admin Amendments", plan_region)
        self.assertIn("alice", plan_region)
        self.assertIn("please tweak step 3 to use json.dumps", plan_region)
        # Non-plan body trailer is preserved.
        self.assertIn("Original issue body below.", new_body)


class TestTryUnblockIssueAppendsAmendmentsForPlanApproved(unittest.TestCase):
    """PLAN_APPROVED resumes must fold admin amendments into the plan block."""

    def _issue_with_plan(self):
        return {
            "number": 880,
            "title": "t",
            "body": (
                "<!-- cai-plan-start -->\n"
                "## Selected Implementation Plan\n\n"
                "Original plan.\n"
                "Confidence: MEDIUM\n"
                "<!-- cai-plan-end -->\n\n"
                "Rest of body.\n"
            ),
            "labels": [
                {"name": "auto-improve:human-needed"},
                {"name": "human:solved"},
            ],
            "comments": [
                {"author": {"login": "alice"},
                 "createdAt": "2026-04-19T12:00:00Z",
                 "body": "Please adjust the plan: also strip trailing whitespace in step 2."},
            ],
        }

    def _run_with_verdict(self, resume_to: str):
        agent_stdout = json.dumps({
            "resume_to": resume_to,
            "confidence": "HIGH",
            "reasoning": "admin greenlit the plan",
        })
        fake_agent = mock.MagicMock()
        fake_agent.returncode = 0
        fake_agent.stdout = agent_stdout
        fake_agent.stderr = ""

        calls: dict = {"append": [], "transition": []}

        def fake_append(issue_number, body, amendments):
            calls["append"].append(
                {"issue": issue_number, "n": len(amendments)}
            )
            return True

        def fake_apply(issue_number, transition_name, **kwargs):
            calls["transition"].append(transition_name)
            return True

        issue = self._issue_with_plan()

        with mock.patch.object(U, "_run_claude_p", return_value=fake_agent), \
             mock.patch.object(U, "_append_admin_amendments_to_plan",
                               side_effect=fake_append), \
             mock.patch.object(U, "apply_transition", side_effect=fake_apply):
            result = U._try_unblock_issue(issue)
        return result, calls

    def test_plan_approved_triggers_append(self):
        result, calls = self._run_with_verdict("PLAN_APPROVED")
        self.assertEqual(result, "resumed")
        self.assertEqual(calls["transition"], ["human_to_plan_approved"])
        self.assertEqual(len(calls["append"]), 1)
        self.assertEqual(calls["append"][0]["n"], 1)

    def test_refining_does_not_trigger_append(self):
        result, calls = self._run_with_verdict("REFINING")
        self.assertEqual(result, "resumed")
        self.assertEqual(calls["transition"], ["human_to_refining"])
        self.assertEqual(calls["append"], [])


if __name__ == "__main__":
    unittest.main()
