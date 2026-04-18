"""Tests for cai_lib.transcript_sync.

Tests patch attributes on ``cai_lib.config`` (the module ``transcript_sync``
reads from at call time) rather than reloading modules — reloading leaks
state into other test files via by-value imports (e.g. ``ADMIN_LOGINS``
in ``cai_lib.cmd_unblock``).
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


class TestDisabled(unittest.TestCase):
    def test_sync_url_unset_disables_feature(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", ""), \
             mock.patch.object(config, "MACHINE_ID", "anything"):
            self.assertFalse(config.transcript_sync_enabled())
            self.assertEqual(transcript_sync.push(), 0)
            self.assertEqual(transcript_sync.pull(), 0)
            self.assertEqual(transcript_sync.sync(), 0)

    def test_missing_machine_id_disables_even_with_url(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/tmp"), \
             mock.patch.object(config, "MACHINE_ID", ""):
            self.assertFalse(config.transcript_sync_enabled())


class TestParseSource(unittest.TestCase):
    def test_falls_back_to_local_when_aggregate_missing(self):
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", ""):
            self.assertEqual(transcript_sync.parse_source(), config.TRANSCRIPT_DIR)

    def test_uses_aggregate_when_enabled_and_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agg = tmp_path / "aggregate"
            agg.mkdir()
            (agg / "machine-a").mkdir()
            (agg / "machine-a" / "session-1.jsonl").write_text('{"t":"u"}\n')

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "user@host:/tmp"), \
                 mock.patch.object(config, "MACHINE_ID", "machine-a"), \
                 mock.patch.object(config, "TRANSCRIPT_AGGREGATE_DIR", agg):
                self.assertTrue(config.transcript_sync_enabled())
                self.assertEqual(transcript_sync.parse_source(), agg)


class TestRepoSlug(unittest.TestCase):
    def test_slash_becomes_underscore(self):
        self.assertEqual(config._repo_slug("owner/repo-name"), "owner_repo-name")
        self.assertEqual(config._repo_slug("a/b/c"), "a_b_c")


class TestTransportSelection(unittest.TestCase):
    def test_colon_url_is_ssh(self):
        self.assertFalse(transcript_sync._is_local_url("cai@host:/srv/cai-transcripts"))
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "cai@h:/srv/x"), \
             mock.patch.object(config, "TRANSCRIPT_SYNC_SSH_KEY", Path("/tmp/k")):
            args = transcript_sync._transport_args()
            self.assertEqual(args[0], "-e")
            self.assertIn("ssh", args[1])
            self.assertIn("/tmp/k", args[1])

    def test_plain_path_is_local(self):
        self.assertTrue(transcript_sync._is_local_url("/srv/cai-transcripts"))
        with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", "/srv/cai-transcripts"):
            self.assertEqual(transcript_sync._transport_args(), [])


@unittest.skipUnless(_rsync_available(), "rsync not installed on this host")
class TestLocalPushPull(unittest.TestCase):
    """End-to-end-ish: real rsync against a temp dir, no SSH."""

    def test_push_and_pull_roundtrip_local(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            local_src = tmp_path / "src"
            local_src.mkdir()
            (local_src / "-app").mkdir()
            (local_src / "-app" / "session1.jsonl").write_text('{"t":"u"}\n')

            store = tmp_path / "store"
            store.mkdir()
            aggregate = tmp_path / "agg"

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", str(store)), \
                 mock.patch.object(config, "MACHINE_ID", "test-host"), \
                 mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
                 mock.patch.object(config, "TRANSCRIPT_DIR", local_src), \
                 mock.patch.object(config, "TRANSCRIPT_AGGREGATE_DIR", aggregate):
                self.assertEqual(transcript_sync.push(), 0)
                bucket = store / "owner_repo" / "test-host"
                self.assertTrue(
                    (bucket / "-app" / "session1.jsonl").exists(),
                    "expected pushed file in server bucket",
                )

                self.assertEqual(transcript_sync.pull(), 0)
                pulled = aggregate / "test-host" / "-app" / "session1.jsonl"
                self.assertTrue(pulled.exists(), "expected file in aggregate mirror")
                self.assertEqual(pulled.read_text(), '{"t":"u"}\n')

    def test_empty_source_does_not_push(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            empty_src = tmp_path / "src"
            empty_src.mkdir()  # exists but no .jsonl
            store = tmp_path / "store"
            store.mkdir()
            bucket = store / "owner_repo" / "test-host"
            bucket.mkdir(parents=True)
            (bucket / "old-session.jsonl").write_text('{"t":"u"}\n')

            with mock.patch.object(config, "TRANSCRIPT_SYNC_URL", str(store)), \
                 mock.patch.object(config, "MACHINE_ID", "test-host"), \
                 mock.patch.object(config, "REPO_SLUG", "owner_repo"), \
                 mock.patch.object(config, "TRANSCRIPT_DIR", empty_src):
                self.assertEqual(transcript_sync.push(), 0)
                self.assertTrue(
                    (bucket / "old-session.jsonl").exists(),
                    "empty-source push must not --delete the bucket",
                )


if __name__ == "__main__":
    unittest.main()
