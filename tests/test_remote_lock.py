"""Tests for cai_lib.github._acquire_remote_lock / _release_remote_lock and
the dispatcher's drive-time integration.

The lock is exercised against an in-memory fake gh backend so the test is
deterministic and doesn't need network access. The fake stores per-target
labels and comments and serves them back through the same _gh_json /
_set_labels / _post_*_comment / _delete_issue_comment seams that the
production code uses.

Note on hostname/PID resolution: ``INSTANCE_ID`` is computed at import
time from the running PID, so tests that simulate two instances must
monkeypatch ``cai_lib.config.INSTANCE_ID`` AND ``cai_lib.github.INSTANCE_ID``
between calls (github.py imports the symbol by name).
"""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import dispatcher, github
from cai_lib.config import LABEL_LOCKED
from cai_lib.fsm import IssueState


class _FakeGitHub:
    """In-memory backend for issues/PRs with labels + comments.

    Comment ids are monotonic. created_at is a synthetic ISO8601 timestamp
    that increments per post so ordering is deterministic.
    """

    def __init__(self):
        # number -> {"labels": set, "comments": [{id, body, created_at}]}
        self.targets: dict[int, dict] = {}
        self._next_id = 1
        self._tick = 0

    def _ensure(self, number: int) -> dict:
        return self.targets.setdefault(
            number, {"labels": set(), "comments": []}
        )

    def list_comments(self, number: int) -> list[dict]:
        return list(self._ensure(number)["comments"])

    def post_comment(self, number: int, body: str) -> int:
        self._tick += 1
        cid = self._next_id
        self._next_id += 1
        self._ensure(number)["comments"].append({
            "id": cid,
            "body": body,
            "created_at": f"2026-04-19T00:00:{self._tick:02d}Z",
        })
        return cid

    def delete_comment(self, comment_id: int) -> bool:
        for t in self.targets.values():
            t["comments"] = [c for c in t["comments"] if c["id"] != comment_id]
        return True

    def add_label(self, number: int, label: str) -> bool:
        self._ensure(number)["labels"].add(label)
        return True

    def remove_label(self, number: int, label: str) -> bool:
        self._ensure(number)["labels"].discard(label)
        return True


def _install_fake(fake: _FakeGitHub):
    """Return a list of patcher contexts that wire ``fake`` into github.*.

    Use as ``with contextlib.ExitStack() as stack: [stack.enter_context(p) for p in _install_fake(fake)]``.
    """
    def fake_set_labels(number, *, add=(), remove=(), log_prefix="cai"):
        for lb in add:
            fake.add_label(number, lb)
        for lb in remove:
            fake.remove_label(number, lb)
        return True

    def fake_set_pr_labels(number, *, add=(), remove=(), log_prefix="cai"):
        for lb in add:
            fake.add_label(number, lb)
        for lb in remove:
            fake.remove_label(number, lb)
        return True

    def fake_post_issue_comment(number, body, *, log_prefix="cai"):
        fake.post_comment(number, body)
        return True

    def fake_post_pr_comment(number, body, *, log_prefix="cai"):
        fake.post_comment(number, body)
        return True

    def fake_delete(comment_id, *, log_prefix="cai"):
        return fake.delete_comment(comment_id)

    def fake_gh_json(args):
        # Only the comments-list call goes through here for the lock helpers.
        # Args look like ["api", "/repos/.../issues/<n>/comments", "--paginate"].
        if len(args) >= 2 and args[0] == "api" and "/comments" in args[1]:
            # Parse /repos/<owner>/<repo>/issues/<n>/comments
            path_parts = args[1].split("/")
            try:
                idx = path_parts.index("issues")
                number = int(path_parts[idx + 1])
            except (ValueError, IndexError):
                return []
            return fake.list_comments(number)
        return []

    return [
        patch.object(github, "_set_labels", side_effect=fake_set_labels),
        patch.object(github, "_set_pr_labels", side_effect=fake_set_pr_labels),
        patch.object(github, "_post_issue_comment", side_effect=fake_post_issue_comment),
        patch.object(github, "_post_pr_comment", side_effect=fake_post_pr_comment),
        patch.object(github, "_delete_issue_comment", side_effect=fake_delete),
        patch.object(github, "_gh_json", side_effect=fake_gh_json),
    ]


class TestAcquireRelease(unittest.TestCase):
    def setUp(self):
        # Make stabilization poll fast — patch its constants.
        self._stab_timeout = patch.object(
            github, "_LOCK_STABILIZE_TIMEOUT_S", 0.05
        )
        self._stab_interval = patch.object(
            github, "_LOCK_STABILIZE_INTERVAL_S", 0.01
        )
        self._stab_timeout.start()
        self._stab_interval.start()
        # Reset module-level refcount between tests.
        github._HELD_LOCKS.clear()

    def tearDown(self):
        self._stab_timeout.stop()
        self._stab_interval.stop()
        github._HELD_LOCKS.clear()

    def _with_fake(self, fake):
        from contextlib import ExitStack
        stack = ExitStack()
        for p in _install_fake(fake):
            stack.enter_context(p)
        return stack

    def test_acquire_happy_path(self):
        fake = _FakeGitHub()
        with self._with_fake(fake):
            with patch.object(github, "INSTANCE_ID", "instance-A"):
                ok = github._acquire_remote_lock("issue", 42)
        self.assertTrue(ok)
        self.assertIn(("issue", 42), github._HELD_LOCKS)
        target = fake.targets[42]
        self.assertIn(LABEL_LOCKED, target["labels"])
        # Exactly one cai-lock comment, owned by us.
        lock_comments = [
            c for c in target["comments"] if "cai-lock" in c["body"]
        ]
        self.assertEqual(len(lock_comments), 1)
        self.assertIn("owner=instance-A", lock_comments[0]["body"])

    def test_acquire_idempotent_refcount(self):
        fake = _FakeGitHub()
        with self._with_fake(fake):
            with patch.object(github, "INSTANCE_ID", "instance-A"):
                self.assertTrue(github._acquire_remote_lock("issue", 7))
                self.assertTrue(github._acquire_remote_lock("issue", 7))
                self.assertEqual(github._HELD_LOCKS[("issue", 7)], 2)
                # Only one comment posted across two acquires.
                lock_comments = [
                    c for c in fake.targets[7]["comments"]
                    if "cai-lock" in c["body"]
                ]
                self.assertEqual(len(lock_comments), 1)
                # First release decrements but does NOT clean up.
                github._release_remote_lock("issue", 7)
                self.assertEqual(github._HELD_LOCKS[("issue", 7)], 1)
                self.assertIn(LABEL_LOCKED, fake.targets[7]["labels"])
                # Second release does the GitHub cleanup.
                github._release_remote_lock("issue", 7)
                self.assertNotIn(("issue", 7), github._HELD_LOCKS)
                self.assertNotIn(LABEL_LOCKED, fake.targets[7]["labels"])
                self.assertEqual(
                    [c for c in fake.targets[7]["comments"] if "cai-lock" in c["body"]],
                    [],
                )

    def test_two_instances_one_issue(self):
        """Second acquirer sees its claim is not the oldest and yields."""
        fake = _FakeGitHub()
        with self._with_fake(fake):
            # Instance A wins.
            with patch.object(github, "INSTANCE_ID", "instance-A"):
                self.assertTrue(github._acquire_remote_lock("issue", 99))
            # Simulate a fresh second process: clear refcount (per-process state).
            github._HELD_LOCKS.clear()
            # Instance B contends.
            with patch.object(github, "INSTANCE_ID", "instance-B"):
                ok = github._acquire_remote_lock("issue", 99)
        self.assertFalse(ok, "B should lose the race")
        self.assertNotIn(("issue", 99), github._HELD_LOCKS)
        # Exactly one cai-lock comment remains (A's), label still present
        # because A still holds it.
        lock_comments = [
            c for c in fake.targets[99]["comments"] if "cai-lock" in c["body"]
        ]
        self.assertEqual(len(lock_comments), 1)
        self.assertIn("owner=instance-A", lock_comments[0]["body"])
        self.assertIn(LABEL_LOCKED, fake.targets[99]["labels"])

    def test_release_idempotent_when_not_held(self):
        # No setup, no acquire — release on an empty refcount is a no-op.
        github._HELD_LOCKS.clear()
        ok = github._release_remote_lock("issue", 1234)
        self.assertTrue(ok)

    def test_acquire_post_comment_failure_leaves_no_label(self):
        """If posting the claim comment fails, LABEL_LOCKED is never applied.

        Regression for the ``stale_hours=inf`` orphan-label bug (#1086):
        the prior ordering set the label FIRST and relied on a
        best-effort strip-label cleanup when the subsequent post
        failed, which could leave a label without a claim if either
        step crashed. Inverting the ordering eliminates the class.
        """
        fake = _FakeGitHub()
        with self._with_fake(fake):
            # _install_fake already wires _post_issue_comment to the
            # fake; override it inside the ExitStack so this patch wins.
            with patch.object(github, "_post_issue_comment",
                              return_value=False):
                with patch.object(github, "INSTANCE_ID", "instance-A"):
                    ok = github._acquire_remote_lock("issue", 55)
        self.assertFalse(ok)
        self.assertNotIn(("issue", 55), github._HELD_LOCKS)
        target = fake.targets.get(55, {"labels": set(), "comments": []})
        self.assertNotIn(
            LABEL_LOCKED, target.get("labels", set()),
            "post-failure must not leave an orphan :locked label",
        )

    def test_acquire_label_failure_deletes_claim_comment(self):
        """If label-add fails after a successful claim post, the comment is deleted."""
        fake = _FakeGitHub()
        with self._with_fake(fake):
            with patch.object(github, "_set_labels",
                              return_value=False):
                with patch.object(github, "INSTANCE_ID", "instance-A"):
                    ok = github._acquire_remote_lock("issue", 56)
        self.assertFalse(ok)
        target = fake.targets.get(56, {"labels": set(), "comments": []})
        lock_comments = [
            c for c in target.get("comments", [])
            if "cai-lock" in c.get("body", "")
        ]
        self.assertEqual(
            lock_comments, [],
            "label-add failure after claim-post must delete the claim",
        )
        self.assertNotIn(LABEL_LOCKED, target.get("labels", set()))


class TestDispatcherLockIntegration(unittest.TestCase):
    """The drain driver must release the lock on every exit path —
    including uncaught handler exceptions — via the outer try/finally.
    """

    def setUp(self):
        github._HELD_LOCKS.clear()

    def tearDown(self):
        github._HELD_LOCKS.clear()

    def test_drain_skips_when_acquire_fails(self):
        touched: set = set()
        with patch.object(dispatcher, "_acquire_remote_lock", return_value=False), \
             patch.object(dispatcher, "_release_remote_lock") as rel, \
             patch.object(dispatcher, "_fetch_issue_state") as fetch, \
             patch.object(dispatcher, "dispatch_issue") as di:
            rc = dispatcher._drive_target_to_completion("issue", 1, touched)
        self.assertEqual(rc, 0)
        # Touched marks the target so the outer drain doesn't re-pick it.
        self.assertIn(("issue", 1), touched)
        di.assert_not_called()
        fetch.assert_not_called()
        # No release because we never held it.
        rel.assert_not_called()

    def test_drive_releases_on_handler_exception(self):
        """When dispatch_issue raises mid-loop, the finally must still release."""
        touched: set = set()
        # Force one loop iteration that calls dispatch_issue and raises.
        with patch.object(dispatcher, "_acquire_remote_lock", return_value=True), \
             patch.object(dispatcher, "_release_remote_lock") as rel, \
             patch.object(dispatcher, "_fetch_issue_state",
                          return_value=IssueState.REFINING), \
             patch.object(dispatcher, "dispatch_issue",
                          side_effect=RuntimeError("boom")):
            rc = dispatcher._drive_target_to_completion("issue", 1, touched)
        # Driver swallowed the exception and returned.
        self.assertEqual(rc, 1)
        # Critical: release MUST have been called by the finally.
        rel.assert_called_once_with("issue", 1)


if __name__ == "__main__":
    unittest.main()
