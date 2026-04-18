"""Tests for cai_lib.transcript_sync — the no-op path and helper composition.

The actual rsync transport is deliberately NOT tested here (it would
require an SSH target). We cover the boundary: when sync is disabled
the public entry points return 0 and don't touch anything; when the
aggregate dir is empty, parse_source falls back to the local dir.
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


# Make the repo root importable regardless of cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _reload_with_env(env: dict[str, str]):
    """Reload config + transcript_sync under a fresh os.environ snapshot."""
    with mock.patch.dict(os.environ, env, clear=False):
        import cai_lib.config as config_mod
        import cai_lib.transcript_sync as sync_mod
        importlib.reload(config_mod)
        importlib.reload(sync_mod)
        return config_mod, sync_mod


class TestDisabled(unittest.TestCase):
    def test_sync_url_unset_disables_feature(self):
        config_mod, sync_mod = _reload_with_env({
            "CAI_TRANSCRIPT_SYNC_URL": "",
            "CAI_MACHINE_ID": "whatever",
        })
        self.assertFalse(sync_mod.transcript_sync_enabled())
        self.assertEqual(sync_mod.push(), 0)
        self.assertEqual(sync_mod.pull(), 0)
        self.assertEqual(sync_mod.sync(), 0)

    def test_missing_machine_id_disables_even_with_url(self):
        # MACHINE_ID must resolve to non-empty for the feature to engage.
        with mock.patch.dict(os.environ, {
            "CAI_TRANSCRIPT_SYNC_URL": "user@host:/tmp",
            "CAI_MACHINE_ID": "",
        }, clear=False), mock.patch(
            "cai_lib.config._HOST_MACHINE_ID_PATH",
            Path("/nonexistent/machine-id"),
        ):
            import cai_lib.config as config_mod
            import cai_lib.transcript_sync as sync_mod
            importlib.reload(config_mod)
            importlib.reload(sync_mod)
            self.assertEqual(config_mod.MACHINE_ID, "")
            self.assertFalse(sync_mod.transcript_sync_enabled())


class TestParseSource(unittest.TestCase):
    def test_falls_back_to_local_when_aggregate_empty(self):
        config_mod, sync_mod = _reload_with_env({
            "CAI_TRANSCRIPT_SYNC_URL": "",
        })
        # Aggregate dir doesn't exist — source must be the local dir.
        self.assertEqual(sync_mod.parse_source(), config_mod.TRANSCRIPT_DIR)

    def test_uses_aggregate_when_enabled_and_populated(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            agg = tmp_path / "aggregate"
            agg.mkdir()
            (agg / "machine-a").mkdir()
            (agg / "machine-a" / "session-1.jsonl").write_text(
                '{"type":"user"}\n'
            )

            with mock.patch.dict(os.environ, {
                "CAI_TRANSCRIPT_SYNC_URL": "user@host:/tmp",
                "CAI_MACHINE_ID": "machine-a",
            }, clear=False):
                import cai_lib.config as config_mod
                import cai_lib.transcript_sync as sync_mod
                importlib.reload(config_mod)
                importlib.reload(sync_mod)
                # Point the module at our temp aggregate.
                with mock.patch.object(sync_mod, "TRANSCRIPT_AGGREGATE_DIR", agg):
                    self.assertTrue(sync_mod.transcript_sync_enabled())
                    self.assertEqual(sync_mod.parse_source(), agg)


class TestRepoSlug(unittest.TestCase):
    def test_slash_becomes_underscore(self):
        config_mod, _ = _reload_with_env({
            "CAI_REPO": "owner/repo-name",
        })
        self.assertEqual(config_mod.REPO_SLUG, "owner_repo-name")


if __name__ == "__main__":
    unittest.main()
