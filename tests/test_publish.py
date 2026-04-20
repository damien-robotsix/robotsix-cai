"""Tests for publish module."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

# Ensure the repo root is on the import path so `import cai_lib` works
# regardless of how the test runner is invoked.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cai_lib import publish as publish_mod  # noqa: E402
from cai_lib.dup_check import DupCheckVerdict  # noqa: E402
from cai_lib.publish import (  # noqa: E402
    CHECK_WORKFLOWS_LABELS,
    Finding,
    LABELS_TO_DELETE,
    _finding_body_for_dupcheck,
)


class TestCheckWorkflowsLabels(unittest.TestCase):

    def test_check_workflows_raised_not_in_labels(self):
        """check-workflows:raised must NOT appear in CHECK_WORKFLOWS_LABELS (retired label)."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertNotIn("check-workflows:raised", label_names)

    def test_check_workflows_in_labels(self):
        """check-workflows source tag must remain in CHECK_WORKFLOWS_LABELS."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("check-workflows", label_names)

    def test_auto_improve_raised_in_check_workflows_labels(self):
        """auto-improve:raised must be in CHECK_WORKFLOWS_LABELS so new findings enter the FSM."""
        label_names = [name for name, _, _ in CHECK_WORKFLOWS_LABELS]
        self.assertIn("auto-improve:raised", label_names)

    def test_check_workflows_raised_in_labels_to_delete(self):
        """check-workflows:raised must be in LABELS_TO_DELETE so it gets cleaned up on publish runs."""
        self.assertIn("check-workflows:raised", LABELS_TO_DELETE)


class TestFindingBodyForDupCheck(unittest.TestCase):

    def test_body_contains_evidence_and_remediation(self):
        f = Finding(
            title="t", category="reliability", key="k",
            confidence="high",
            evidence="Ev text", remediation="Rm text",
        )
        body = _finding_body_for_dupcheck(f)
        self.assertIn("Ev text", body)
        self.assertIn("Rm text", body)
        self.assertIn("reliability", body)


def _write_findings(findings: list[dict]) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"findings": findings}, fh)
    fh.close()
    return fh.name


class TestSemanticDupCheckIntegration(unittest.TestCase):
    """Exercise ``main()``'s pre-publish dup-check branch end-to-end."""

    def _one_finding(self):
        return [{
            "title": "Flaky watchdog",
            "category": "reliability",
            "key": "fp-1",
            "confidence": "high",
            "evidence": "evidence",
            "remediation": "remediation",
        }]

    def test_semantic_duplicate_skips_create(self):
        path = _write_findings(self._one_finding())
        try:
            dup_verdict = DupCheckVerdict(
                verdict="DUPLICATE", confidence="HIGH",
                target=42, commit_sha=None,
                reasoning="Already covered by #42",
            )
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=dup_verdict) as m_dup, \
                 mock.patch.object(publish_mod, "create_issue") as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_dup.assert_called_once()
            m_create.assert_not_called()
        finally:
            os.unlink(path)

    def test_non_duplicate_proceeds_to_create(self):
        path = _write_findings(self._one_finding())
        try:
            none_verdict = DupCheckVerdict(
                verdict="NONE", confidence="HIGH",
                target=None, commit_sha=None, reasoning="distinct",
            )
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=none_verdict), \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_create.assert_called_once()
        finally:
            os.unlink(path)

    def test_env_var_disables_dupcheck(self):
        path = _write_findings(self._one_finding())
        try:
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate") as m_dup, \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ,
                                  {"CAI_SKIP_DUPCHECK_ON_PUBLISH": "1"},
                                  clear=False):
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_dup.assert_not_called()
            m_create.assert_called_once()
        finally:
            os.unlink(path)

    def test_agent_failure_proceeds_to_create(self):
        """A ``None`` verdict (agent crash / parse failure) must NOT block publish."""
        path = _write_findings(self._one_finding())
        try:
            with mock.patch.object(publish_mod, "issue_exists", return_value=False), \
                 mock.patch.object(publish_mod, "ensure_labels"), \
                 mock.patch.object(publish_mod, "check_finding_duplicate",
                                    return_value=None), \
                 mock.patch.object(publish_mod, "create_issue",
                                    return_value=0) as m_create, \
                 mock.patch.object(sys, "argv",
                                    ["publish.py", "--findings-file", path]), \
                 mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CAI_SKIP_DUPCHECK_ON_PUBLISH", None)
                rc = publish_mod.main()
            self.assertEqual(rc, 0)
            m_create.assert_called_once()
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
