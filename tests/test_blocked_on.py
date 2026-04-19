"""Tests for the blocked-on:<N> label mechanic.

Covers:
- blocking_issue_numbers: label parsing (dict shape, string shape, malformed, empty)
- open_blockers: resolver with mixed open/closed states, CalledProcessError, cache
- _pick_oldest_actionable_target: skips issues/PRs with open blockers
- _list_unresolved_human_needed_issues: skips issues with open blockers
- _list_unresolved_pr_human_needed_prs: skips PRs with open blockers
"""
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, call

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib.github import blocking_issue_numbers, open_blockers
from cai_lib import cmd_rescue as R
from cai_lib import dispatcher


# ---------------------------------------------------------------------------
# blocking_issue_numbers
# ---------------------------------------------------------------------------

class TestBlockingIssueNumbers(unittest.TestCase):

    def test_dict_shape_label(self):
        labels = [{"name": "blocked-on:42"}, {"name": "auto-improve:refined"}]
        self.assertEqual(blocking_issue_numbers(labels), {42})

    def test_string_shape_label(self):
        labels = ["blocked-on:42", "auto-improve:refined"]
        self.assertEqual(blocking_issue_numbers(labels), {42})

    def test_multiple_blockers(self):
        labels = [{"name": "blocked-on:10"}, {"name": "blocked-on:20"}]
        self.assertEqual(blocking_issue_numbers(labels), {10, 20})

    def test_deduplication(self):
        labels = [{"name": "blocked-on:42"}, {"name": "blocked-on:42"}]
        self.assertEqual(blocking_issue_numbers(labels), {42})

    def test_ignores_malformed_hash_variant(self):
        labels = [{"name": "blocked-on:#42"}]
        self.assertEqual(blocking_issue_numbers(labels), set())

    def test_ignores_malformed_alpha(self):
        labels = [{"name": "blocked-on:abc"}]
        self.assertEqual(blocking_issue_numbers(labels), set())

    def test_ignores_wrong_prefix(self):
        labels = [{"name": "blocked:42"}]
        self.assertEqual(blocking_issue_numbers(labels), set())

    def test_empty_list(self):
        self.assertEqual(blocking_issue_numbers([]), set())

    def test_none(self):
        self.assertEqual(blocking_issue_numbers(None), set())


# ---------------------------------------------------------------------------
# open_blockers
# ---------------------------------------------------------------------------

class TestOpenBlockers(unittest.TestCase):

    def _make_gh_json_side_effect(self, states: dict):
        """Return a side_effect callable that returns issue state dicts."""
        def _side_effect(args):
            # args is like ["issue", "view", "42", "--repo", ..., "--json", ...]
            number = int(args[2])
            state = states[number]
            return {"number": number, "state": state}
        return _side_effect

    def test_returns_only_open_blockers(self):
        states = {10: "OPEN", 20: "CLOSED"}
        with patch("cai_lib.github._gh_json",
                   side_effect=self._make_gh_json_side_effect(states)):
            result = open_blockers({10, 20})
        self.assertEqual(result, {10})

    def test_called_process_error_treated_as_not_blocking(self):
        def _side_effect(args):
            raise subprocess.CalledProcessError(1, "gh")

        with patch("cai_lib.github._gh_json", side_effect=_side_effect):
            result = open_blockers({99})
        self.assertEqual(result, set())

    def test_cache_is_populated_and_reused(self):
        states = {42: "OPEN"}
        call_count = [0]

        def _side_effect(args):
            call_count[0] += 1
            return {"number": 42, "state": "OPEN"}

        cache: dict = {}
        with patch("cai_lib.github._gh_json", side_effect=_side_effect):
            open_blockers({42}, cache=cache)
            # Second call should use the cache, not gh.
            open_blockers({42}, cache=cache)

        self.assertEqual(call_count[0], 1)
        self.assertIn(42, cache)
        self.assertTrue(cache[42])

    def test_empty_blocker_set(self):
        result = open_blockers(set())
        self.assertEqual(result, set())


# ---------------------------------------------------------------------------
# Dispatcher picker skips issues/PRs with open blockers
# ---------------------------------------------------------------------------

def _make_issue(number, labels, created_at="2024-01-01T00:00:00Z"):
    return {
        "number": number,
        "labels": [{"name": lb} for lb in labels],
        "createdAt": created_at,
    }


def _make_pr(number, labels, created_at="2024-01-01T00:00:00Z"):
    return {
        "number": number,
        "labels": [{"name": lb} for lb in labels],
        "createdAt": created_at,
        "mergedAt": None,
        "state": "OPEN",
        "mergeable": "MERGEABLE",
        "merged": False,
    }


def _make_combined_gh_json(issues, prs, blocker_states=None):
    """Return a side_effect callable usable for both dispatcher and github patches."""
    blocker_states = blocker_states or {}

    def _side_effect(args):
        if "issue" in args and "list" in args:
            return issues
        if "pr" in args and "list" in args:
            return prs
        if "issue" in args and "view" in args:
            number = int(args[args.index("view") + 1])
            state = blocker_states.get(number, "CLOSED")
            return {"number": number, "state": state}
        return None

    return _side_effect


class TestDispatchPickerRespectsBlockedOn(unittest.TestCase):
    """_pick_oldest_actionable_target skips candidates whose blocker is open."""

    def setUp(self):
        self._patcher_gate = patch.object(dispatcher, "_build_ordering_gate", return_value={})
        self._patcher_gate.start()

    def tearDown(self):
        self._patcher_gate.stop()

    def test_blocked_issue_is_skipped_unblocked_picked(self):
        """Issue with open blocker is skipped; next eligible issue is picked."""
        blocked_issue = _make_issue(1, ["auto-improve:refined", "blocked-on:99"],
                                    "2023-01-01T00:00:00Z")
        unblocked_issue = _make_issue(2, ["auto-improve:refined"],
                                      "2024-01-01T00:00:00Z")

        side_effect = _make_combined_gh_json(
            [blocked_issue, unblocked_issue], [], {99: "OPEN"})
        with patch.object(dispatcher, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = dispatcher._pick_oldest_actionable_target()

        self.assertIsNotNone(result)
        self.assertEqual(result, ("issue", 2))

    def test_unblocked_issue_when_blocker_closed(self):
        """Issue is picked once its blocker is closed."""
        issue = _make_issue(1, ["auto-improve:refined", "blocked-on:99"])

        side_effect = _make_combined_gh_json([issue], [], {99: "CLOSED"})
        with patch.object(dispatcher, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = dispatcher._pick_oldest_actionable_target()

        self.assertIsNotNone(result)
        self.assertEqual(result, ("issue", 1))

    def test_blocked_pr_is_skipped(self):
        """PR with open blocker is skipped."""
        blocked_pr = _make_pr(10, ["pr:reviewing-code", "blocked-on:99"])

        side_effect = _make_combined_gh_json([], [blocked_pr], {99: "OPEN"})
        with patch.object(dispatcher, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = dispatcher._pick_oldest_actionable_target()

        self.assertIsNone(result)


# ---------------------------------------------------------------------------
# Rescue list helpers skip issues/PRs with open blockers
# ---------------------------------------------------------------------------

def _rescue_issue(number, labels):
    return {
        "number": number,
        "title": "t",
        "body": "",
        "labels": [{"name": lb} for lb in labels],
        "updatedAt": "2024-01-01T00:00:00Z",
        "comments": [],
    }


def _rescue_pr(number, labels):
    return {
        "number": number,
        "title": "t",
        "body": "",
        "labels": [{"name": lb} for lb in labels],
        "updatedAt": "2024-01-01T00:00:00Z",
        "comments": [],
    }


def _make_rescue_gh_json(items, blocker_states=None):
    """Side-effect usable for both cmd_rescue and github patches."""
    blocker_states = blocker_states or {}

    def _side_effect(args):
        if "list" in args:
            return items
        if "view" in args:
            number = int(args[args.index("view") + 1])
            state = blocker_states.get(number, "CLOSED")
            return {"number": number, "state": state}
        return None

    return _side_effect


class TestRescueListRespectsBlockedOn(unittest.TestCase):

    def test_blocked_human_needed_issue_excluded(self):
        blocked = _rescue_issue(1, ["auto-improve:human-needed", "blocked-on:99"])
        unblocked = _rescue_issue(2, ["auto-improve:human-needed"])

        side_effect = _make_rescue_gh_json([blocked, unblocked], {99: "OPEN"})
        with patch.object(R, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = R._list_unresolved_human_needed_issues()

        numbers = [i["number"] for i in result]
        self.assertNotIn(1, numbers)
        self.assertIn(2, numbers)

    def test_blocked_issue_included_when_blocker_closed(self):
        issue = _rescue_issue(1, ["auto-improve:human-needed", "blocked-on:99"])

        side_effect = _make_rescue_gh_json([issue], {99: "CLOSED"})
        with patch.object(R, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = R._list_unresolved_human_needed_issues()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 1)

    def test_blocked_pr_human_needed_excluded(self):
        blocked = _rescue_pr(10, ["auto-improve:pr-human-needed", "blocked-on:99"])
        unblocked = _rescue_pr(11, ["auto-improve:pr-human-needed"])

        side_effect = _make_rescue_gh_json([blocked, unblocked], {99: "OPEN"})
        with patch.object(R, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = R._list_unresolved_pr_human_needed_prs()

        numbers = [p["number"] for p in result]
        self.assertNotIn(10, numbers)
        self.assertIn(11, numbers)

    def test_blocked_pr_included_when_blocker_closed(self):
        pr = _rescue_pr(10, ["auto-improve:pr-human-needed", "blocked-on:99"])

        side_effect = _make_rescue_gh_json([pr], {99: "CLOSED"})
        with patch.object(R, "_gh_json", side_effect=side_effect), \
             patch("cai_lib.github._gh_json", side_effect=side_effect):
            result = R._list_unresolved_pr_human_needed_prs()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["number"], 10)


if __name__ == "__main__":
    unittest.main()
