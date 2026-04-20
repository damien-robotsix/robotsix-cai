"""Tests for cai_lib.issues.close_completed_parents."""
import unittest
from unittest.mock import patch

from cai_lib import issues


class TestCloseCompletedParents(unittest.TestCase):
    def _run(self, open_parents, subs_by_parent):
        """Drive close_completed_parents with fake gh/list_sub_issues/_run.

        *open_parents* is an initial set/list of parent numbers currently
        open (the fake ``gh issue list --state open`` returns these; when
        a parent is closed via the fake ``gh issue close``, it is
        removed so the second pass sees it gone).

        *subs_by_parent* maps parent_number -> list of sub-issue dicts.
        When a parent is closed, the fake walks every OTHER parent's
        sub-issue list and flips any entry pointing at the closed
        number to ``state=closed`` — so nested parents can be covered
        in a second pass.

        Returns (closed_count, list_of_closed_parent_numbers).
        """
        live_parents = set(open_parents)
        closed: list[int] = []

        def fake_gh_json(cmd):
            if "issue" in cmd and "list" in cmd:
                return [{"number": p} for p in sorted(live_parents)]
            raise AssertionError(f"unexpected _gh_json call: {cmd}")

        def fake_list_sub_issues(num):
            return subs_by_parent.get(num, [])

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        def fake_run(cmd, **_kw):
            if "issue" in cmd and "close" in cmd:
                num = int(cmd[cmd.index("close") + 1])
                closed.append(num)
                live_parents.discard(num)
                for subs in subs_by_parent.values():
                    for s in subs:
                        if s["number"] == num:
                            s["state"] = "closed"
            return _Result()

        with patch.object(issues, "_gh_json", side_effect=fake_gh_json), \
             patch.object(issues, "list_sub_issues",
                          side_effect=fake_list_sub_issues), \
             patch.object(issues, "_run", side_effect=fake_run):
            n = issues.close_completed_parents(log_prefix="test")
        return n, closed

    def test_closes_parent_with_all_sub_issues_closed(self):
        n, closed = self._run(
            open_parents=[100],
            subs_by_parent={100: [
                {"number": 101, "state": "closed"},
                {"number": 102, "state": "closed"},
            ]},
        )
        self.assertEqual(n, 1)
        self.assertEqual(closed, [100])

    def test_skips_parent_with_open_sub_issue(self):
        n, closed = self._run(
            open_parents=[200],
            subs_by_parent={200: [
                {"number": 201, "state": "closed"},
                {"number": 202, "state": "open"},
            ]},
        )
        self.assertEqual(n, 0)
        self.assertEqual(closed, [])

    def test_skips_parent_with_no_sub_issues(self):
        # all_sub_issues_closed returns None when the parent has no
        # native sub-issues — the helper must NOT close it.
        n, closed = self._run(
            open_parents=[300],
            subs_by_parent={300: []},
        )
        self.assertEqual(n, 0)
        self.assertEqual(closed, [])

    def test_two_pass_closes_nested_parent(self):
        # Pass 1 closes #501 (its only child #510 is already closed).
        # When #501 is closed, the #500 -> #501 entry flips to
        # state=closed. Pass 2 then closes #500.
        # This mirrors the real-world case of issue #885 (all its
        # sub-issues closed, blocking siblings under #884).
        n, closed = self._run(
            open_parents=[500, 501],
            subs_by_parent={
                500: [{"number": 501, "state": "open"}],
                501: [{"number": 510, "state": "closed"}],
            },
        )
        self.assertEqual(n, 2)
        self.assertEqual(closed, [501, 500])


if __name__ == "__main__":
    unittest.main()
