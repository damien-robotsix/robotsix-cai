"""Tests for cai_lib.audit_logging — audit_log_path, audit_log_start,
audit_log_finish, and _run_one_module integration."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cai_lib.config as _config


class TestAuditLogPath(unittest.TestCase):
    """audit_log_path returns the expected Path."""

    def test_basic(self):
        from cai_lib.config import audit_log_path, AUDIT_LOG_DIR
        result = audit_log_path("code-reduction", "actions")
        self.assertEqual(result, AUDIT_LOG_DIR / "code-reduction" / "actions.jsonl")

    def test_hyphenated_kind(self):
        from cai_lib.config import audit_log_path, AUDIT_LOG_DIR
        result = audit_log_path("good-practices", "cai")
        self.assertEqual(result, AUDIT_LOG_DIR / "good-practices" / "cai.jsonl")

    def test_returns_path_object(self):
        from cai_lib.config import audit_log_path
        self.assertIsInstance(audit_log_path("k", "m"), Path)


class TestAuditLogStart(unittest.TestCase):
    """audit_log_start writes a start event."""

    def _call(self, kind, module, agent, log_dir):
        orig = _config.AUDIT_LOG_DIR
        _config.AUDIT_LOG_DIR = Path(log_dir)
        try:
            from cai_lib.audit_logging import audit_log_start
            audit_log_start(kind, module, agent)
        finally:
            _config.AUDIT_LOG_DIR = orig

    def test_creates_file_with_start_event(self):
        with tempfile.TemporaryDirectory() as td:
            self._call("code-reduction", "actions", "cai-audit-code-reduction", td)
            log_path = Path(td) / "code-reduction" / "actions.jsonl"
            self.assertTrue(log_path.exists())
            line = log_path.read_text().strip()
            row = json.loads(line)
            self.assertEqual(row["event"], "start")
            self.assertEqual(row["level"], "INFO")
            self.assertEqual(row["kind"], "code-reduction")
            self.assertEqual(row["module"], "actions")
            self.assertEqual(row["agent"], "cai-audit-code-reduction")
            self.assertIsNone(row["exit_code"])
            self.assertIsNone(row["error_class"])

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            self._call("new-kind", "new-module", "some-agent", td)
            log_path = Path(td) / "new-kind" / "new-module.jsonl"
            self.assertTrue(log_path.exists())

    def test_never_raises_on_bad_path(self):
        # Should silently swallow errors — log directory is not writable.
        orig = _config.AUDIT_LOG_DIR
        _config.AUDIT_LOG_DIR = Path("/proc/impossible-path-that-cannot-exist")
        try:
            from cai_lib.audit_logging import audit_log_start
            # Must not raise.
            audit_log_start("k", "m", "a")
        finally:
            _config.AUDIT_LOG_DIR = orig


class TestAuditLogFinish(unittest.TestCase):
    """audit_log_finish round-trips every schema key."""

    def _call(self, log_dir, proc, findings_count, exit_code,
              error_class=None, message=""):
        orig = _config.AUDIT_LOG_DIR
        _config.AUDIT_LOG_DIR = Path(log_dir)
        try:
            from cai_lib.audit_logging import audit_log_finish
            audit_log_finish(
                "code-reduction", "actions", "cai-audit-code-reduction",
                proc=proc,
                findings_count=findings_count,
                exit_code=exit_code,
                error_class=error_class,
                message=message,
            )
        finally:
            _config.AUDIT_LOG_DIR = orig

    def test_success_event_keys(self):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as td:
            self._call(td, proc, findings_count=3, exit_code=0)
            row = json.loads(
                (Path(td) / "code-reduction" / "actions.jsonl").read_text().strip()
            )
            # All schema keys must be present.
            required = {
                "ts", "level", "kind", "module", "agent", "session_id",
                "event", "message", "cost_usd", "duration_ms", "num_turns",
                "tokens", "findings_count", "exit_code", "error_class",
            }
            self.assertEqual(required, set(row.keys()))
            self.assertEqual(row["event"], "finish")
            self.assertEqual(row["level"], "INFO")
            self.assertEqual(row["exit_code"], 0)
            self.assertEqual(row["findings_count"], 3)
            self.assertIsNone(row["error_class"])

    def test_error_event(self):
        proc = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as td:
            self._call(td, proc, findings_count=None, exit_code=1,
                       error_class="agent_nonzero", message="agent exited 1")
            row = json.loads(
                (Path(td) / "code-reduction" / "actions.jsonl").read_text().strip()
            )
            self.assertEqual(row["event"], "error")
            self.assertEqual(row["level"], "ERROR")
            self.assertEqual(row["exit_code"], 1)
            self.assertEqual(row["error_class"], "agent_nonzero")
            self.assertEqual(row["message"], "agent exited 1")

    def test_none_proc_is_safe(self):
        with tempfile.TemporaryDirectory() as td:
            self._call(td, None, findings_count=None, exit_code=1,
                       error_class="unexpected_exception")
            row = json.loads(
                (Path(td) / "code-reduction" / "actions.jsonl").read_text().strip()
            )
            self.assertIsNone(row["cost_usd"])
            self.assertIsNone(row["session_id"])

    def test_appends_multiple_lines(self):
        proc = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as td:
            self._call(td, proc, findings_count=1, exit_code=0)
            self._call(td, proc, findings_count=2, exit_code=0)
            lines = (
                Path(td) / "code-reduction" / "actions.jsonl"
            ).read_text().strip().splitlines()
            self.assertEqual(len(lines), 2)
            counts = [json.loads(l)["findings_count"] for l in lines]
            self.assertEqual(counts, [1, 2])


class TestRunOneModuleLogging(unittest.TestCase):
    """_run_one_module emits exactly one start and one finish/error event."""

    def _read_log_lines(self, log_dir, kind, module):
        p = Path(log_dir) / kind / f"{module}.jsonl"
        if not p.exists():
            return []
        return [json.loads(l) for l in p.read_text().strip().splitlines() if l]

    def _fake_entry(self, name="testmod"):
        entry = MagicMock()
        entry.name = name
        entry.globs = []
        entry.summary = "test"
        entry.doc = None
        return entry

    def test_success_emits_start_and_finish(self):
        """Full success path: one start + one finish event, exit_code=0, findings_count set."""
        def fake_run_claude_p(args, **_kw):
            # Recover work_dir from the args list and seed findings.json.
            work_dir = Path(args[args.index("--add-dir") + 1])
            (work_dir / "findings.json").write_text(
                '{"findings": [{"title": "t", "body": "b"}]}'
            )
            return subprocess.CompletedProcess(
                args=args, returncode=0, stdout="", stderr=""
            )

        fake_publish = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            orig = _config.AUDIT_LOG_DIR
            _config.AUDIT_LOG_DIR = log_dir
            try:
                from cai_lib.audit.runner import _run_one_module
                entry = self._fake_entry("successmod")
                with patch("cai_lib.audit.runner._run_claude_p",
                           side_effect=fake_run_claude_p), \
                     patch("cai_lib.audit.runner._run",
                           return_value=fake_publish), \
                     patch("cai_lib.audit.runner._build_module_prompt",
                           return_value="prompt"):
                    rc = _run_one_module(
                        "good-practices", "cai-audit-good-practices", entry
                    )
            finally:
                _config.AUDIT_LOG_DIR = orig
            self.assertEqual(rc, 0)
            rows = self._read_log_lines(log_dir, "good-practices", "successmod")
            self.assertEqual(len(rows), 2)
            self.assertEqual([r["event"] for r in rows], ["start", "finish"])
            finish_row = rows[1]
            self.assertEqual(finish_row["level"], "INFO")
            self.assertEqual(finish_row["exit_code"], 0)
            self.assertEqual(finish_row["findings_count"], 1)
            self.assertIsNone(finish_row["error_class"])

    def test_agent_nonzero_emits_error(self):
        """When agent returns non-zero, one start + one error event are written."""
        fake_proc = subprocess.CompletedProcess(
            args=[], returncode=2, stdout="", stderr=""
        )
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            orig = _config.AUDIT_LOG_DIR
            _config.AUDIT_LOG_DIR = log_dir
            try:
                from cai_lib.audit.runner import _run_one_module
                entry = self._fake_entry("mymod")
                with patch("cai_lib.audit.runner._run_claude_p", return_value=fake_proc), \
                     patch("cai_lib.audit.runner._build_module_prompt", return_value="p"):
                    rc = _run_one_module("good-practices", "cai-audit-good-practices", entry)
            finally:
                _config.AUDIT_LOG_DIR = orig
            self.assertEqual(rc, 1)
            rows = self._read_log_lines(log_dir, "good-practices", "mymod")
            self.assertEqual(len(rows), 2)
            events = [r["event"] for r in rows]
            self.assertIn("start", events)
            self.assertIn("error", events)
            error_row = next(r for r in rows if r["event"] == "error")
            self.assertEqual(error_row["error_class"], "agent_nonzero")

    def test_unexpected_exception_emits_error(self):
        """When _run_claude_p raises, one start + one error event are written."""
        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td) / "logs"
            orig = _config.AUDIT_LOG_DIR
            _config.AUDIT_LOG_DIR = log_dir
            try:
                from cai_lib.audit.runner import _run_one_module
                entry = self._fake_entry("crashmod")
                with patch("cai_lib.audit.runner._run_claude_p",
                           side_effect=RuntimeError("boom")), \
                     patch("cai_lib.audit.runner._build_module_prompt", return_value="p"):
                    rc = _run_one_module("cost-reduction", "cai-audit-cost-reduction", entry)
            finally:
                _config.AUDIT_LOG_DIR = orig
            self.assertEqual(rc, 1)
            rows = self._read_log_lines(log_dir, "cost-reduction", "crashmod")
            self.assertEqual(len(rows), 2)
            error_row = next(r for r in rows if r["event"] == "error")
            self.assertEqual(error_row["error_class"], "unexpected_exception")


if __name__ == "__main__":
    unittest.main()
