"""Tests for cost-log push/pull/sync_cost functions in cai_lib.transcript_sync.

Tests patch attributes on ``cai_lib.config`` (the module ``transcript_sync``
reads from at call time) rather than reloading modules — same pattern as
test_transcript_sync.py.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


from cai_lib import config, transcript_sync  # noqa: E402


def _rsync_available() -> bool:
    try:
        subprocess.run(["rsync", "--version"], check=True, capture_output=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


class TestCostSyncDisabled(unittest.TestCase):
    def test_push_cost_noop_when_sync_url_unset(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", ""), \
             mock.patch.object(config, "MACHINE_ID", "m1"):
            self.assertEqual(transcript_sync.push_cost(), 0)

    def test_pull_cost_noop_when_sync_url_unset(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", ""), \
             mock.patch.object(config, "MACHINE_ID", "m1"):
            self.assertEqual(transcript_sync.pull_cost(), 0)

    def test_push_cost_noop_when_machine_id_missing(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/tmp"), \
             mock.patch.object(config, "MACHINE_ID", ""):
            self.assertEqual(transcript_sync.push_cost(), 0)

    def test_push_cost_noop_when_log_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "nonexistent.jsonl"
            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/tmp"), \
                 mock.patch.object(config, "MACHINE_ID", "m1"), \
                 mock.patch.object(config, "COST_LOG_PATH", missing):
                self.assertEqual(transcript_sync.push_cost(), 0)

    def test_push_cost_noop_when_log_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "cai-cost.jsonl"
            empty.write_text("")
            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/tmp"), \
                 mock.patch.object(config, "MACHINE_ID", "m1"), \
                 mock.patch.object(config, "COST_LOG_PATH", empty):
                self.assertEqual(transcript_sync.push_cost(), 0)


class TestCostServerPaths(unittest.TestCase):
    def test_cost_server_bucket_format(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/srv/cai"), \
             mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
             mock.patch.object(config, "MACHINE_ID", "abc123"):
            bucket = transcript_sync._cost_server_bucket()
        self.assertEqual(bucket, "user@host:/srv/cai/owner_repo-cost/abc123")

    def test_cost_server_slug_format(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/srv/cai"), \
             mock.patch.object(config, "REPO_SLUG", "owner_repo"):
            slug = transcript_sync._cost_server_slug()
        self.assertEqual(slug, "user@host:/srv/cai/owner_repo-cost")

    def test_cost_server_bucket_strips_trailing_slash(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/srv/cai/"), \
             mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
             mock.patch.object(config, "MACHINE_ID", "abc"):
            bucket = transcript_sync._cost_server_bucket()
        self.assertNotIn("//", bucket)


@unittest.skipUnless(_rsync_available(), "rsync not installed on this host")
class TestCostLocalPushPull(unittest.TestCase):
    """End-to-end cost-log push/pull using real rsync against a temp dir."""

    def test_push_and_pull_cost_roundtrip_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cost_log = tmp_path / "cai-cost.jsonl"
            cost_log.write_text('{"ts":"2099-01-01T00:00:00Z","cost_usd":0.01}\n')

            store = tmp_path / "store"
            store.mkdir()
            aggregate = tmp_path / "cost-agg"

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", str(store)), \
                 mock.patch.object(config, "MACHINE_ID", "test-host"), \
                 mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
                 mock.patch.object(config, "COST_LOG_PATH", cost_log), \
                 mock.patch.object(config, "COST_LOG_AGGREGATE_DIR", aggregate):
                self.assertEqual(transcript_sync.push_cost(), 0)
                pushed = store / "owner_repo-cost" / "test-host" / "cai-cost.jsonl"
                self.assertTrue(pushed.exists(), "expected pushed cost file in server bucket")

                self.assertEqual(transcript_sync.pull_cost(), 0)
                pulled = aggregate / "test-host" / "cai-cost.jsonl"
                self.assertTrue(pulled.exists(), "expected cost file in aggregate mirror")
                self.assertEqual(
                    pulled.read_text(),
                    '{"ts":"2099-01-01T00:00:00Z","cost_usd":0.01}\n',
                )

    def test_pull_cost_missing_server_skips_gracefully(self):
        """When the server-side cost bucket doesn't exist yet, pull returns 0."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = tmp_path / "store"
            store.mkdir()  # cost bucket does not exist under store
            aggregate = tmp_path / "cost-agg"

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", str(store)), \
                 mock.patch.object(config, "MACHINE_ID", "m1"), \
                 mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
                 mock.patch.object(config, "COST_LOG_AGGREGATE_DIR", aggregate):
                rc = transcript_sync.pull_cost()
            self.assertEqual(rc, 0)

    def test_sync_cost_runs_push_then_pull(self):
        """sync_cost() pushes then pulls; aggregate contains the pushed file."""
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            cost_log = tmp_path / "cai-cost.jsonl"
            cost_log.write_text('{"ts":"2099-06-01T00:00:00Z","cost_usd":0.05}\n')
            store = tmp_path / "store"
            store.mkdir()
            aggregate = tmp_path / "cost-agg"

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", str(store)), \
                 mock.patch.object(config, "MACHINE_ID", "host-x"), \
                 mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
                 mock.patch.object(config, "COST_LOG_PATH", cost_log), \
                 mock.patch.object(config, "COST_LOG_AGGREGATE_DIR", aggregate):
                rc = transcript_sync.push_cost()
                self.assertEqual(rc, 0)
                rc = transcript_sync.pull_cost()
                self.assertEqual(rc, 0)
            pulled = aggregate / "host-x" / "cai-cost.jsonl"
            self.assertTrue(pulled.exists())


if __name__ == "__main__":
    unittest.main()
